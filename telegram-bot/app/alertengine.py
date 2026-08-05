"""Tier 2: push alerts.

Turns hub events into chat notifications: node offline/online, watchdog recovery,
command failures, hub restart. A per-(kind,node) debounce window stops a flapping
node (bad cable) from flooding the chat, and everything stays well inside the
~1 msg/s per-chat limit because alerts are inherently low-rate.

Two sources feed this:
  - the WS broadcast stream (node_up / node_down) — immediate.
  - a /events poll — for notable audit rows (watchdog, failed) that are not on
    the WS broadcast channel.
The relay calls on_ws_event() and on_audit_event(); both funnel through emit().
"""

from __future__ import annotations

import time
from typing import Awaitable, Callable

Broadcast = Callable[[str], Awaitable[None]]


class AlertEngine:
    def __init__(self, broadcast: Broadcast, debounce_s: int, enabled: bool):
        self._broadcast = broadcast     # send an HTML message to all allowed chats
        self._debounce_s = debounce_s
        self._enabled = enabled
        self._last: dict[tuple, float] = {}
        self._muted: set = set()        # node ids whose alerts are suppressed

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def set_debounce(self, debounce_s: int) -> None:
        self._debounce_s = debounce_s

    def mute(self, node_id: str) -> None:
        self._muted.add(node_id)

    def unmute(self, node_id: str) -> None:
        self._muted.discard(node_id)

    async def _emit(self, kind: str, node, title: str, detail: str = "") -> None:
        if not self._enabled:
            return
        if node is not None and node in self._muted:
            return
        key = (kind, node)
        now = time.time()
        if now - self._last.get(key, 0.0) < self._debounce_s:
            return
        self._last[key] = now
        node_s = (" <b>%s</b>" % node) if node else ""
        body = "🔔 %s%s" % (title, node_s)
        if detail:
            body += "\n<i>%s</i>" % detail
        await self._broadcast(body)

    async def on_ws_event(self, event: dict) -> None:
        ev = event.get("event")
        node = event.get("id")
        if ev == "node_down":
            await self._emit("down", node, "Node offline", event.get("reason") or "")
        elif ev == "node_up":
            await self._emit("up", node, "Node reconnected")

    async def on_audit_event(self, row: dict) -> None:
        """A row from /events. Only a couple of types are notable."""
        type_ = row.get("type")
        node = row.get("node_id")
        detail = (row.get("detail") or "")
        low = detail.lower()
        if type_ == "error" and "watchdog" in low:
            await self._emit("watchdog", node, "Recovered from watchdog reset", detail)
        elif type_ == "result" and low.rstrip().endswith("failed"):
            await self._emit("failed", node, "Command failed", detail)

    async def on_hub_restart(self) -> None:
        # Not node-scoped; bypass the per-node debounce with a synthetic key.
        await self._emit("hub", None, "Hub restarted")
