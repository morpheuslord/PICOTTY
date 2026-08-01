"""The hub context and the command-dispatch service.

One Hub instance is shared by both faces. It owns the registry, the database,
the event bus, and a live copy of settings. It also centralizes the operations
that both faces or several endpoints need: sending a frame down a node's socket,
dispatching a command (allocate cmd_id, persist, push, announce), and measuring
RTT via ping/pong correlation.
"""

from __future__ import annotations

import asyncio
import time

from . import config
from .db import Database
from .eventbus import EventBus
from .protocol import encode_frame
from .registry import Inflight, Registry
from .utils import gen_cmd_id, gen_nonce, now_ms


def build_node_frame(cmd_id: str, command: dict) -> dict:
    """Translate an API command body into a hub->node wire frame."""
    ctype = command.get("type")
    if ctype == "type":
        frame = {"type": "type", "cmd_id": cmd_id, "text": command.get("text", "")}
        if command.get("char_delay_ms"):
            frame["char_delay_ms"] = command["char_delay_ms"]
        return frame
    if ctype == "keys":
        return {"type": "keys", "cmd_id": cmd_id, "chord": command.get("chord", [])}
    if ctype == "sequence":
        frame = {"type": "sequence", "cmd_id": cmd_id, "steps": command.get("steps", [])}
        if command.get("stop_on_error"):
            frame["stop_on_error"] = True
        return frame
    if ctype == "read":
        return {"type": "read", "cmd_id": cmd_id}
    if ctype == "send":
        # Bytes into the target's serial getty. Exactly one of raw/data; raw
        # (hex) wins if both slipped through. Validation happened at the REST edge.
        frame = {"type": "send", "cmd_id": cmd_id}
        if command.get("raw") is not None:
            frame["raw"] = command["raw"]
        else:
            frame["data"] = command.get("data", "")
        return frame
    raise ValueError("unsupported command type: %r" % ctype)


class Hub:
    def __init__(self, db: Database, registry: Registry, eventbus: EventBus):
        self.db = db
        self.registry = registry
        self.eventbus = eventbus
        self.settings: dict = dict(config.DEFAULT_SETTINGS)
        self.started_at = now_ms()
        self.loop_lag_ms = 0
        # Optional subsystems, set at startup once their tasks/servers are up.
        # Kept as attributes (not hard imports) so the output hot path can feed
        # them with a cheap None-check and features stay independently optional.
        self.expect = None    # expect.ExpectManager (phase 5)
        self.bridge = None    # serialbridge.SerialBridge (phase 8)
        self.alerts = None    # alerts.AlertDispatcher (phase 11)
        self.runbooks = None  # runbook.RunbookRunner (phase 9)
        self.ota = None       # ota.OTAManager (phase 10)
        # Per-node lock serializing queue drains: the hello-drain and an
        # enqueue-triggered drain must not both claim the same pending rows.
        self._drain_locks: dict = {}

    def feed_expect(self, node_id: str, text: str) -> None:
        """Hand a fresh output chunk to any running expect job for this node."""
        if self.expect is not None:
            self.expect.feed(node_id, text)

    def feed_bridge(self, node_id: str, text: str) -> None:
        """Mirror a fresh output chunk to any raw-serial-bridge client."""
        if self.bridge is not None:
            self.bridge.feed(node_id, text)

    async def load_settings(self) -> None:
        self.settings = await self.db.get_settings()

    def uptime_ms(self) -> int:
        return now_ms() - self.started_at

    def node_supports(self, node_id: str, capability: str) -> bool:
        """True if the node is connected and advertised `capability` in hello."""
        state = self.registry.get(node_id)
        return bool(state and capability in (state.capabilities or []))

    # -- audit + liveness -----------------------------------------------------

    async def audit(self, type_: str, node_id, detail: str) -> None:
        """Persist an audit event and push it to the live audit tail."""
        ts = now_ms()
        await self.db.insert_event(type_, node_id, detail, ts)
        self.eventbus.broadcast(
            {"event": "event", "type": type_, "node_id": node_id, "detail": detail, "ts": ts}
        )
        # Outbound alerting hangs off the audit path: this is the one place every
        # notable event passes through. Cheap no-op unless alerts are enabled.
        if self.alerts is not None:
            self.alerts.on_event(type_, node_id, detail)

    async def mark_offline(self, state, reason: str) -> None:
        """Flip a node offline exactly once. Safe if it was already replaced by a
        reconnect (in which case this is a no-op)."""
        node_id = state.node_id
        if self.registry.get(node_id) is not state:
            return  # a newer connection owns this id now
        state.status = "offline"
        self.registry.remove(node_id)
        await self.db.touch_node(node_id, now_ms())
        # Fail any awaiting OTA request futures so a push in flight ends promptly
        # instead of waiting out its timeout.
        for fut in list(state.pending_results.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("node disconnected"))
        state.pending_results.clear()
        # Close any raw-serial-bridge clients attached to this node; they'd
        # otherwise linger with a dead node behind them.
        if self.bridge is not None:
            self.bridge.close_clients(node_id)
        await self.audit("node_down", node_id, reason)
        self.eventbus.broadcast({"event": "node_down", "id": node_id, "reason": reason})

    # -- sending --------------------------------------------------------------

    async def send_frame(self, state, obj: dict) -> None:
        """Write one frame down a node's socket, serialized per node."""
        data = encode_frame(obj)
        async with state.send_lock:
            state.writer.write(data)
            await state.writer.drain()

    # -- command dispatch -----------------------------------------------------

    async def dispatch_command(self, node_id: str, command: dict, issued_by=None) -> dict:
        """The core action. Confirm online, persist a command row, push the frame,
        announce it, and return the cmd_id. Does not wait for the result."""
        state = self.registry.get(node_id)
        if state is None or state.status == "offline":
            return {"ok": False, "error": "node_offline", "detail": "node %s is not connected" % node_id}

        ctype = command.get("type")
        cmd_id = gen_cmd_id()
        try:
            frame = build_node_frame(cmd_id, command)
        except ValueError as e:
            return {"ok": False, "error": "bad_command", "detail": str(e)}

        ts = now_ms()
        db_id = await self.db.insert_command(cmd_id, node_id, ctype, command, issued_by, ts)
        state.inflight[cmd_id] = Inflight(cmd_id=cmd_id, type=ctype, sent_at=ts, db_command_id=db_id)

        try:
            await self.send_frame(state, frame)
        except (OSError, ConnectionError) as e:
            # The socket died between the online check and the write.
            state.inflight.pop(cmd_id, None)
            await self.db.complete_command(cmd_id, "failed", now_ms())
            return {"ok": False, "error": "send_failed", "detail": str(e), "cmd_id": cmd_id}

        await self.db.insert_event("cmd", node_id, "%s %s" % (ctype, cmd_id), ts)
        self.eventbus.broadcast(
            {"event": "command_issued", "id": node_id, "cmd_id": cmd_id, "type": ctype, "by": issued_by}
        )
        return {"ok": True, "cmd_id": cmd_id, "status": "sent"}

    async def drain_queue(self, node_id: str) -> int:
        """Deliver any commands staged for a node while it was offline, in issue
        order, dropping expired ones. Called right after the node registers, so
        the dashboard already shows it online before queued commands flow.
        Returns the number delivered.

        Serialized per node so a hello-drain and an enqueue-drain can't both
        read the same pending rows and deliver them twice."""
        lock = self._drain_locks.get(node_id)
        if lock is None:
            lock = asyncio.Lock()
            self._drain_locks[node_id] = lock
        async with lock:
            now = now_ms()
            pending = await self.db.take_pending_queued(node_id, now)
            delivered = 0
            for row in pending:
                res = await self.dispatch_command(node_id, row["payload"], issued_by=row.get("issued_by") or "queue")
                if res.get("ok"):
                    await self.db.mark_queued(row["id"], "delivered")
                    delivered += 1
                else:
                    # Node dropped again mid-drain; leave the rest pending for next time.
                    break
            if delivered:
                await self.audit("cmd", node_id, "delivered %d queued command(s) on connect" % delivered)
            return delivered

    async def send_control(self, node_id: str, frame: dict, audit_detail: str = None) -> dict:
        """Send a control frame (reboot/config) that expects no result row."""
        state = self.registry.get(node_id)
        if state is None or state.status == "offline":
            return {"ok": False, "error": "node_offline", "detail": "node %s is not connected" % node_id}
        try:
            await self.send_frame(state, frame)
        except (OSError, ConnectionError) as e:
            return {"ok": False, "error": "send_failed", "detail": str(e)}
        if audit_detail:
            await self.db.insert_event("cmd", node_id, audit_detail, now_ms())
        return {"ok": True}

    async def bridge_send(self, node_id: str, payload: bytes) -> bool:
        """Push raw bytes to a node's serial port for the raw serial bridge.

        A deliberately lightweight path: no command row, no inflight, no audit —
        an interactive serial stream would otherwise flood the DB with a row per
        keystroke. The cmd_id is prefixed 'b_' so the result handler swallows the
        node's ack silently. Returns False if the node isn't writable/online."""
        state = self.registry.get(node_id)
        if state is None or state.status == "offline":
            return False
        # Read-only bridge: a node that can't write serial still streams output
        # to the client. Silently drop the write but keep the connection alive
        # (returning True) so the read direction is unaffected.
        if "serial_tx" not in (state.capabilities or []):
            return True
        cmd_id = "b_" + gen_nonce()
        frame = {"type": "send", "cmd_id": cmd_id, "raw": payload.hex()}
        try:
            await self.send_frame(state, frame)
        except (OSError, ConnectionError):
            return False
        return True

    async def request(self, node_id: str, frame: dict, timeout: float = 15.0) -> dict:
        """Send a command frame and await the node's matching result, WITHOUT a
        command/inflight DB row. Used by OTA, where a per-chunk command row would
        flood the DB. The cmd_id is prefixed 'r_' so the result handler routes the
        reply straight back here. The future is registered before the send, so a
        fast reply can't be missed."""
        state = self.registry.get(node_id)
        if state is None or state.status == "offline":
            return {"ok": False, "error": "node_offline"}
        cmd_id = "r_" + gen_nonce()
        frame = dict(frame)
        frame["cmd_id"] = cmd_id
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        state.pending_results[cmd_id] = fut
        try:
            await self.send_frame(state, frame)
            result = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            state.pending_results.pop(cmd_id, None)
            return {"ok": False, "error": "timeout"}
        except (OSError, ConnectionError) as e:
            state.pending_results.pop(cmd_id, None)
            return {"ok": False, "error": "send_failed", "detail": str(e)}
        return {"ok": True, "status": result.get("status"), "payload": result.get("payload"), "result": result}

    async def ping_node(self, node_id: str, timeout: float = 2.0):
        """Send a ping and await the matching pong. Returns RTT ms or None."""
        state = self.registry.get(node_id)
        if state is None or state.status == "offline":
            return None
        nonce = gen_nonce()
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        state.pending_pongs[nonce] = fut
        sent = now_ms()
        try:
            await self.send_frame(state, {"type": "ping", "nonce": nonce})
            await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, OSError, ConnectionError):
            state.pending_pongs.pop(nonce, None)
            return None
        rtt = now_ms() - sent
        state.rtt_ms = rtt
        return rtt

    # -- view merge -----------------------------------------------------------

    def merge_node(self, db_row: dict) -> dict:
        """Merge a durable node row with its live registry fields for the API."""
        node_id = db_row["id"]
        state = self.registry.get(node_id)
        online = state is not None and state.status != "offline"
        merged = {
            "id": node_id,
            "label": db_row.get("label", ""),
            "group": db_row.get("group_name", ""),
            "notes": db_row.get("notes", ""),
            "fw_version": db_row.get("fw_version"),
            "first_seen": db_row.get("first_seen"),
            "last_seen": (state.last_seen if state else db_row.get("last_seen")),
            "status": "online" if online else "offline",
            "ip": state.ip if state else "",
            "capabilities": state.capabilities if state else [],
            "layout": state.layout if state else "us",
            "prompt_state": state.prompt_state if online else None,
            "connected_at": state.connected_at if state else None,
            "rtt_ms": state.rtt_ms if online else None,
            "inflight": len(state.inflight) if state else 0,
        }
        return merged
