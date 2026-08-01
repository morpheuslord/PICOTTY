"""Outbound alerting: webhook / ntfy on notable events.

For a fleet you stop watching the dashboard, so the hub tells you when something
matters. The audit path (hub.audit) is the single place every notable thing
already passes through — node down, watchdog recovery, a failed command — so we
hang outbound notifications off it.

Fire-and-forget: each notification is its own asyncio task with a timeout and a
single retry, so a slow or dead endpoint never blocks the event loop. A simple
per-key dedup window stops a flapping node from spamming you. Off by default
(alerts_enabled); the target URL(s) are settings.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request

from .utils import now_ms

# How long to suppress a repeat of the same (kind, node) alert. A node that
# flaps down/up every few seconds sends one alert, not a storm.
DEDUP_WINDOW_MS = 60_000
POST_TIMEOUT_S = 5
MAX_RETRIES = 1


def _classify(type_: str, detail: str):
    """Return a stable dedup key + human title for an event worth alerting on,
    or None if this event is not notable."""
    d = (detail or "")
    if type_ == "node_down":
        return "down", "node down"
    if type_ == "error" and "watchdog" in d.lower():
        return "watchdog", "recovered from watchdog reset"
    if type_ == "result" and d.rstrip().endswith("failed"):
        return "failed", "command failed"
    return None


class AlertDispatcher:
    def __init__(self, hub):
        self.hub = hub
        self._last = {}  # (kind, node_id) -> last_sent_ms

    def on_event(self, type_: str, node_id, detail: str) -> None:
        """Called from hub.audit for every event. Cheap no-op unless enabled and
        the event is notable. Schedules delivery as its own task."""
        if not self.hub.settings.get("alerts_enabled"):
            return
        hit = _classify(type_, detail)
        if hit is None:
            return
        kind, title = hit
        key = (kind, node_id)
        now = now_ms()
        last = self._last.get(key, 0)
        if now - last < DEDUP_WINDOW_MS:
            return  # within the dedup window; suppress the repeat
        self._last[key] = now
        payload = {
            "event": type_, "kind": kind, "title": title,
            "node_id": node_id, "detail": detail, "ts": now,
        }
        try:
            asyncio.get_event_loop().create_task(self._deliver(payload))
        except RuntimeError:
            pass  # no running loop (e.g. during teardown)

    async def _deliver(self, payload: dict) -> None:
        webhook = (self.hub.settings.get("alerts_webhook_url") or "").strip()
        ntfy = (self.hub.settings.get("alerts_ntfy_url") or "").strip()
        loop = asyncio.get_event_loop()
        targets = []
        if webhook:
            targets.append(("webhook", webhook))
        if ntfy:
            targets.append(("ntfy", ntfy))
        for kind, url in targets:
            body_text = "PICOTTY: %s — %s (%s)" % (
                payload["title"], payload.get("node_id") or "-", payload.get("detail") or "")
            for attempt in range(MAX_RETRIES + 1):
                try:
                    await loop.run_in_executor(None, self._post, kind, url, payload, body_text)
                    break
                except Exception:
                    if attempt >= MAX_RETRIES:
                        # Best-effort: record the miss but never raise into the loop.
                        try:
                            await self.hub.db.insert_event(
                                "error", payload.get("node_id"),
                                "alert delivery to %s failed" % kind, now_ms())
                        except Exception:
                            pass
                    else:
                        await asyncio.sleep(1)

    @staticmethod
    def _post(kind: str, url: str, payload: dict, body_text: str) -> None:
        """Blocking POST, run in a thread executor. ntfy takes a plain-text body;
        a generic webhook takes the JSON payload."""
        if kind == "ntfy":
            data = body_text.encode("utf-8")
            headers = {"Title": "PICOTTY alert", "Content-Type": "text/plain"}
        else:
            data = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT_S) as r:
            r.read()
