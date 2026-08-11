"""Background tasks that run on the hub's single event loop.

- liveness sweep: flips stale nodes offline AND closes their socket (so a
  half-open connection is actually torn down, forcing the node to reconnect).
- node pinger: hub->node keepalive so an idle node keeps receiving traffic and
  a silent (dead) node is caught quickly.
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


async def node_pinger(hub: Hub):
    """Ping every online node on an interval — the hub->node keepalive.

    Purpose is liveness, not RTT (though it refreshes rtt_ms as a bonus): the
    inbound ping gives an idle node the periodic traffic its own dead-hub timeout
    needs, and the pong refreshes last_seen so a node that has gone silent is
    swept offline (and its socket closed) promptly instead of lingering half-open.
    Pings fan out concurrently so one slow node never delays the others."""
    interval = config.PROCESS.node_ping_interval_ms / 1000
    while True:
        await asyncio.sleep(interval)
        try:
            online = [s.node_id for s in hub.registry.all() if s.status == "online"]
            if online:
                await asyncio.gather(
                    *(hub.ping_node(nid) for nid in online),
                    return_exceptions=True,
                )
        except Exception as e:
            await hub.audit("error", None, "node ping error: %s" % e)


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


async def nightly_backup(hub: Hub):
    """Write a consistent DB snapshot to data/backups/ once a day, keeping the
    last few. Pairs with the retention pruner: retention bounds size, this bounds
    the blast radius of a corrupt SD card."""
    import time
    from . import config
    keep = 7
    backups_dir = config.PROCESS.db_path.parent / "backups"
    await asyncio.sleep(120)  # let startup settle
    while True:
        try:
            backups_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d")
            dest = backups_dir / ("hub-%s.db" % stamp)
            if dest.exists():
                dest.unlink()
            await hub.db.backup_to(dest)
            # Prune all but the newest `keep` snapshots.
            snaps = sorted(backups_dir.glob("hub-*.db"))
            for old in snaps[:-keep]:
                try:
                    old.unlink()
                except OSError:
                    pass
            await hub.audit("settings", None, "db snapshot written: %s" % dest.name)
        except Exception as e:
            await hub.audit("error", None, "backup error: %s" % e)
        await asyncio.sleep(86400)


async def loop_lag_monitor(hub: Hub):
    """Cheap event-loop lag estimate: measure oversleep on a fixed tick."""
    tick = 0.5
    while True:
        start = asyncio.get_event_loop().time()
        await asyncio.sleep(tick)
        hub.loop_lag_ms = max(0, int((asyncio.get_event_loop().time() - start - tick) * 1000))
