"""The event relay: the sidecar's live link to the hub.

Owns the /ws connection with reconnect + backoff, routes events to the alert
engine (broadcasts) and the session manager (per-node output), polls /events for
notable audit rows the WS broadcast channel does not carry, and detects a hub
restart (uptime reset or a WS reconnect after a gap). All of this is outbound-only
HTTPS/WSS to the hub — no inbound port, the property that lets it run against an
isolated management VLAN.
"""

from __future__ import annotations

import asyncio

from picotty.client import HubClient

from .alertengine import AlertEngine
from .sessions import SessionManager


class EventRelay:
    def __init__(self, hub: HubClient, alerts: AlertEngine, sessions: SessionManager,
                 *, events_poll_interval_s: int):
        self._hub = hub
        self._alerts = alerts
        self._sessions = sessions
        self._poll_interval = events_poll_interval_s
        self._stream = None            # the active EventStream, for (un)subscribe
        self._stream_lock = asyncio.Lock()
        self._last_event_id = None     # high-water mark for /events polling
        self._last_uptime_ms = None    # for hub-restart detection
        self._seen_ws = False          # have we ever had a live WS?
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._ws_loop()),
            asyncio.create_task(self._events_loop()),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks = []

    # -- subscription control (used by the session manager) -------------------

    async def subscribe(self, node_id: str) -> None:
        async with self._stream_lock:
            if self._stream is not None:
                try:
                    await self._stream.subscribe(node_id)
                except Exception:
                    pass  # a reconnect re-subscribes all active nodes

    async def unsubscribe(self, node_id: str) -> None:
        async with self._stream_lock:
            if self._stream is not None:
                try:
                    await self._stream.unsubscribe(node_id)
                except Exception:
                    pass

    # -- WS loop --------------------------------------------------------------

    async def _ws_loop(self) -> None:
        backoff = 1
        while True:
            try:
                async with self._hub.events_stream() as stream:
                    async with self._stream_lock:
                        self._stream = stream
                    # A reconnect after we have seen a live WS before means the
                    # hub (or the link) bounced — announce it once.
                    if self._seen_ws:
                        await self._alerts.on_hub_restart()
                    self._seen_ws = True
                    backoff = 1
                    # Re-attach any live shell sessions' node subscriptions.
                    for node in self._sessions.active_nodes():
                        try:
                            await stream.subscribe(node)
                        except Exception:
                            pass
                    async for event in stream:
                        await self._on_ws_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                async with self._stream_lock:
                    self._stream = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _on_ws_event(self, event: dict) -> None:
        ev = event.get("event")
        if ev == "output":
            self._sessions.feed_output(event.get("id"), event.get("text", ""))
        elif ev in ("node_up", "node_down"):
            await self._alerts.on_ws_event(event)
        # heartbeat / node_state / result are ignored here; stats come over REST.

    # -- /events poll ---------------------------------------------------------

    async def _events_loop(self) -> None:
        # Seed the high-water mark so we do not replay the entire backlog as
        # alerts on first start.
        try:
            rows = await self._hub.events(limit=1)
            if rows:
                self._last_event_id = rows[0].get("id")
            health = await self._hub.health()
            self._last_uptime_ms = health.get("uptime_ms")
        except Exception:
            pass
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def _poll_once(self) -> None:
        # Hub-restart detection via uptime going backwards (covers a restart that
        # happened while our WS somehow stayed up).
        try:
            health = await self._hub.health()
            up = health.get("uptime_ms")
            if (self._last_uptime_ms is not None and up is not None
                    and up + 2000 < self._last_uptime_ms):
                await self._alerts.on_hub_restart()
            if up is not None:
                self._last_uptime_ms = up
        except Exception:
            pass

        rows = await self._hub.events(limit=50)
        if not rows:
            return
        # Rows are newest-first; process oldest-first past our high-water mark.
        fresh = []
        for r in rows:
            rid = r.get("id")
            if self._last_event_id is not None and rid is not None and rid <= self._last_event_id:
                break
            fresh.append(r)
        if not fresh:
            return
        self._last_event_id = fresh[0].get("id")
        for r in reversed(fresh):
            await self._alerts.on_audit_event(r)
