"""Background tasks that run on the hub's single event loop.

- liveness sweep: flips stale nodes offline (catches half-open connections).
- output flush: writes batched serial output to SQLite on an interval.
- retention: prunes output_log and events past their configured age, daily.
- hub stats: broadcasts a periodic health pulse to browsers.
"""

from __future__ import annotations

import asyncio

from . import config
from .core import Hub
from .utils import now_ms


async def liveness_sweep(hub: Hub):
    """Mark any node whose last-seen is older than the stale threshold offline,
    even if its socket hasn't closed yet."""
    interval = config.PROCESS.sweep_interval_ms / 1000
    while True:
        await asyncio.sleep(interval)
        try:
            stale_ms = int(hub.settings.get("stale_timeout_ms", 15000))
            cutoff = now_ms() - stale_ms
            for state in hub.registry.all():
                if state.status != "offline" and state.last_seen < cutoff:
                    await hub.mark_offline(state, "stale: no frame within %dms" % stale_ms)
        except Exception as e:
            await hub.audit("error", None, "sweep error: %s" % e)


async def output_flusher(hub: Hub):
    interval = config.PROCESS.output_flush_interval_ms / 1000
    while True:
        await asyncio.sleep(interval)
        try:
            await hub.db.flush_output()
        except Exception as e:
            await hub.audit("error", None, "output flush error: %s" % e)


async def retention_pruner(hub: Hub):
    # Run shortly after startup, then once a day.
    await asyncio.sleep(30)
    while True:
        try:
            out_days = int(hub.settings.get("output_retention_days", 30))
            ev_days = int(hub.settings.get("event_retention_days", 90))
            pruned = await hub.db.prune(out_days, ev_days)
            if pruned[0] or pruned[1]:
                await hub.audit(
                    "settings", None, "retention pruned %d output rows, %d events" % pruned
                )
        except Exception as e:
            await hub.audit("error", None, "retention error: %s" % e)
        await asyncio.sleep(86400)


async def stats_broadcaster(hub: Hub):
    interval = config.PROCESS.stats_interval_ms / 1000
    while True:
        await asyncio.sleep(interval)
        try:
            hub.eventbus.broadcast(
                {
                    "event": "hub_stats",
                    "uptime_ms": hub.uptime_ms(),
                    "loop_lag_ms": hub.loop_lag_ms,
                    "nodes_online": hub.registry.online_count(),
                    "nodes_total": hub.registry.count(),
                }
            )
        except Exception:
            pass


async def loop_lag_monitor(hub: Hub):
    """Cheap event-loop lag estimate: measure oversleep on a fixed tick."""
    tick = 0.5
    while True:
        start = asyncio.get_event_loop().time()
        await asyncio.sleep(tick)
        hub.loop_lag_ms = max(0, int((asyncio.get_event_loop().time() - start - tick) * 1000))
