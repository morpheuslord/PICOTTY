"""The swarm face: a raw asyncio TCP server on :9000.

It only listens; it never dials out. For each node it accepts a connection,
validates the token, then reads frames in a loop. It is the single writer of
ingested history to the database: registrations, results, serial output, and
connect/disconnect events all enter here. It renders nothing.
"""

from __future__ import annotations

import asyncio
import json

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
        # Registry-only refresh (already done above); push a compact pulse.
        hub.eventbus.broadcast(
            {"event": "heartbeat", "id": state.node_id, "ts": state.last_seen, "rtt_ms": state.rtt_ms}
        )

    elif mtype == "result":
        cmd_id = msg.get("cmd_id")
        status = msg.get("status", "ok")
        payload = msg.get("payload")
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
        ts = now_ms()
        hub.db.append_output(state.node_id, text, ts, cmd_id=cmd_id)  # batched
        hub.eventbus.send_to_subscribers(
            state.node_id, {"event": "output", "id": state.node_id, "ts": ts, "text": text}
        )

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
                fw_version=fw, capabilities=caps,
            )
            hub.registry.add(state)
            await hub.audit("node_up", node_id, "registered fw %s caps %s" % (fw, ",".join(caps)))
            node_row = await hub.db.get_node(node_id)
            hub.eventbus.broadcast({"event": "node_up", "id": node_id, "meta": hub.merge_node(node_row)})

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
