"""The WebSocket endpoint: one /ws per browser.

The browser upgrades once and receives a stream of event objects. It may send
a few control messages up: subscribe/unsubscribe (per-node output+result
scoping) and ping (keepalive). Backfill is done over REST, not here.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    hub = websocket.app.state.hub
    await websocket.accept()
    client = hub.eventbus.register()

    async def sender():
        try:
            while True:
                event = await client.queue.get()
                if client.gap:
                    event = dict(event)
                    event["_gap"] = True
                    client.gap = False
                await websocket.send_json(event)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            pass

    send_task = asyncio.create_task(sender())
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type") or msg.get("event")
            node_id = msg.get("node_id")
            if kind == "subscribe" and node_id:
                client.subscriptions.add(node_id)
            elif kind == "unsubscribe" and node_id:
                client.subscriptions.discard(node_id)
            elif kind == "ping":
                client.enqueue({"event": "pong", "ts": _now()})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        send_task.cancel()
        hub.eventbus.unregister(client)


def _now() -> int:
    from ..utils import now_ms
    return now_ms()
