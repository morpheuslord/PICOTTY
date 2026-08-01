"""The swarm face: a raw asyncio TCP server on :9000.

It only listens; it never dials out. For each node it accepts a connection,
validates the token, then reads frames in a loop. It is the single writer of
ingested history to the database: registrations, results, serial output, and
connect/disconnect events all enter here. It renders nothing.
"""

from __future__ import annotations

import asyncio
import json

from . import classifier
from .core import Hub
from .protocol import ProtocolError, read_frame
from .registry import NodeState
from .utils import now_ms, token_matches


async def _node_token_hash(hub: Hub) -> str:
    return await hub.db.get_setting_raw("node_token_hash") or ""


async def handle_message(hub: Hub, state: NodeState, msg: dict) -> None:
    """Ingest one node->hub frame. Every frame refreshes last-seen."""
    state.last_seen = now_ms()
    mtype = msg.get("type")

    if mtype == "heartbeat":
        # Carry the target-machine liveness the node reported, if any.
        if "host" in msg:
            state.host_up = bool(msg.get("host"))
        # Registry-only refresh (already done above); push a compact pulse that
        # includes the derived target state so browsers update the machine badge.
        hub.eventbus.broadcast(
            {"event": "heartbeat", "id": state.node_id, "ts": state.last_seen,
             "rtt_ms": state.rtt_ms, "target": hub.target_state(state)}
        )

    elif mtype == "result":
        cmd_id = msg.get("cmd_id")
        status = msg.get("status", "ok")
        payload = msg.get("payload")
        # Raw-serial-bridge writes carry a 'b_' cmd_id and no command row; swallow
        # their acks silently so an interactive stream doesn't flood the audit log.
        if isinstance(cmd_id, str) and cmd_id.startswith("b_"):
            return
        # OTA request/reply carries an 'r_' cmd_id and no command row; hand the
        # result straight to the awaiting requester (hub.request).
        if isinstance(cmd_id, str) and cmd_id.startswith("r_"):
            fut = state.pending_results.pop(cmd_id, None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        ts = now_ms()
        inflight = state.inflight.pop(cmd_id, None)
        db_id = inflight.db_command_id if inflight else None
        if db_id is None:
            row = await hub.db.get_command_by_cmd_id(cmd_id)
            db_id = row["id"] if row else None
        await hub.db.complete_command(cmd_id, status, ts)
        if db_id is not None:
            payload_text = payload if isinstance(payload, str) else (
                None if payload is None else json.dumps(payload)
            )
            await hub.db.insert_result(db_id, state.node_id, cmd_id, status, payload_text, ts)
        await hub.audit("result", state.node_id, "%s %s" % (cmd_id, status))
        hub.eventbus.send_to_subscribers(
            state.node_id,
            {"event": "result", "id": state.node_id, "cmd_id": cmd_id, "status": status, "payload": payload},
        )

    elif mtype == "output":
        text = msg.get("text", "")
        cmd_id = msg.get("cmd_id")
        # The node's own `ts` is monotonic-from-boot (the Picos have no RTC), so we
        # ignore it and stamp the hub's wall-clock received_at as the authoritative
        # time. Node `ts` is advisory ordering only and must never be shown as an
        # absolute time anywhere.
        ts = now_ms()
        state.last_output_at = ts  # fresh output => the target machine is alive
        hub.db.append_output(state.node_id, text, ts, cmd_id=cmd_id)  # batched
        hub.eventbus.send_to_subscribers(
            state.node_id, {"event": "output", "id": state.node_id, "ts": ts, "text": text}
        )
        # Feed the raw serial bridge (if any client is attached to this node).
        hub.feed_bridge(state.node_id, text)
        # Update the rolling tail and reclassify where the target is. A changed
        # prompt-state is a small broadcast so every browser's node badge updates
        # without subscribing to the console. The expect engine also reads this tail.
        state.tail = classifier.update_tail(state.tail, text)
        new_state = classifier.classify(state.tail)
        if new_state and new_state != state.prompt_state:
            state.prompt_state = new_state
            hub.eventbus.broadcast(
                {"event": "node_state", "id": state.node_id, "prompt_state": new_state, "ts": ts}
            )
        # Let any running expect job for this node consume the fresh output.
        hub.feed_expect(state.node_id, text)

    elif mtype == "pong":
        nonce = msg.get("nonce")
        fut = state.pending_pongs.pop(nonce, None)
        if fut is not None and not fut.done():
            fut.set_result(True)

    elif mtype == "error":
        detail = msg.get("detail", "")
        cmd_id = msg.get("cmd_id")
        if cmd_id:
            detail = "%s (%s)" % (detail, cmd_id)
        await hub.audit("error", state.node_id, detail)

    # 'bye' is handled in the read loop so it can break the connection cleanly.


def make_handler(hub: Hub):
    async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        addr = writer.get_extra_info("peername")
        node_id = None
        state = None
        try:
            # The first frame must be a valid hello with the shared token.
            hello = await read_frame(reader)
            if hello.get("type") != "hello":
                await hub.audit("error", None, "first frame from %s was not hello" % (addr,))
                return
            token = hello.get("token", "")
            token_hash = await _node_token_hash(hub)
            if not token_matches(token, token_hash):
                await hub.audit("error", hello.get("id"), "auth failed from %s" % (addr,))
                return

            node_id = hello.get("id")
            if not node_id:
                await hub.audit("error", None, "hello from %s had no id" % (addr,))
                return
            fw = hello.get("fw", "")
            caps = hello.get("cap", []) or []
            layout = hello.get("layout") or "us"
            ts = now_ms()

            # A reconnect supersedes any prior connection for this id.
            old = hub.registry.get(node_id)
            if old is not None:
                try:
                    old.writer.close()
                except Exception:
                    pass

            await hub.db.upsert_node(node_id, fw, ts)
            state = NodeState(
                node_id=node_id, writer=writer, addr=addr,
                connected_at=ts, last_seen=ts, status="online",
                fw_version=fw, capabilities=caps, layout=layout,
            )
            hub.registry.add(state)
            await hub.audit("node_up", node_id, "registered fw %s caps %s" % (fw, ",".join(caps)))
            node_row = await hub.db.get_node(node_id)
            hub.eventbus.broadcast({"event": "node_up", "id": node_id, "meta": hub.merge_node(node_row)})

            # Deliver anything queued for this node while it was offline. Done
            # after node_up so the browser shows it online before commands flow.
            try:
                await hub.drain_queue(node_id)
            except Exception as e:
                await hub.audit("error", node_id, "queue drain error: %s" % e)

            # Frame loop.
            while True:
                msg = await read_frame(reader)
                if msg.get("type") == "bye":
                    break
                await handle_message(hub, state, msg)

        except asyncio.IncompleteReadError:
            pass  # peer closed
        except (ConnectionError, OSError, ProtocolError):
            pass
        except Exception as e:  # never let one node take down the acceptor
            await hub.audit("error", node_id, "connection handler error: %s" % e)
        finally:
            if state is not None:
                await hub.mark_offline(state, "socket closed")
            try:
                writer.close()
            except Exception:
                pass

    return handle_connection


async def start_tcp_server(hub: Hub):
    from . import config
    return await asyncio.start_server(
        make_handler(hub), config.PROCESS.tcp_host, config.PROCESS.tcp_port
    )
