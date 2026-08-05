"""The hub process. One Python program, one asyncio event loop, two faces.

Startup wiring:
  - open the database, load settings, ensure a node token exists
  - build the shared Hub (registry + db + eventbus + settings)
  - start the raw TCP server on :9000 (swarm face)
  - start background tasks (sweep, output flush, retention, stats, lag monitor)
  - serve the FastAPI app on :8080 (browser face) with the built UI as static

Run under ONE uvicorn worker. The single event loop is the point: a second
worker would get its own registry and its own node sockets, and the two would
disagree about who is online.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import config
from .core import Hub
from .db import Database
from .eventbus import EventBus
from .registry import Registry
from .tcp_server import start_tcp_server
from .utils import gen_token, hash_token
from .api.rest import router as rest_router
from .api.ws import router as ws_router
from . import tasks as bg


async def _ensure_token(hub: Hub) -> None:
    """Establish the shared node token on first run.

    If SWARM_NODE_TOKEN is set in the environment (e.g. via the systemd
    EnvironmentFile that points at private/hub-token.txt), the hub adopts it, so
    the hub and the nodes share the same secret with no copy-paste. Otherwise a
    fresh token is generated and printed once.
    """
    existing = await hub.db.get_setting_raw("node_token_hash")
    if existing:
        return
    provided = os.environ.get("SWARM_NODE_TOKEN")
    token = provided or gen_token()
    await hub.db.set_settings({"node_token_hash": hash_token(token)})
    print("=" * 68)
    if provided:
        print("  Adopted the shared NODE TOKEN from SWARM_NODE_TOKEN.")
    else:
        print("  A new shared NODE TOKEN was generated (shown once):")
        print("      %s" % token)
        print("  Put this in each node's settings.toml as NODE_TOKEN.")
    print("  Rotate later via POST /api/settings/token/rotate.")
    print("=" * 68)


def build_app() -> FastAPI:
    app = FastAPI(title="Swarm controller hub")

    @app.on_event("startup")
    async def _startup():
        db = Database(config.PROCESS.db_path)
        await db.connect()
        registry = Registry()
        eventbus = EventBus(config.PROCESS.ws_queue_max)
        hub = Hub(db, registry, eventbus)
        await hub.load_settings()
        await _ensure_token(hub)

        # Optional subsystems from the improvement plan. Each is independent and
        # feeds off the existing output/dispatch paths; the hot path guards them
        # with a None-check so a disabled feature costs nothing.
        from .expect import ExpectManager
        hub.expect = ExpectManager(hub)
        from .alerts import AlertDispatcher
        hub.alerts = AlertDispatcher(hub)
        from .serialbridge import SerialBridge
        hub.bridge = SerialBridge(hub)
        from .runbook import RunbookRunner
        hub.runbooks = RunbookRunner(hub)
        from .ota import OTAManager
        hub.ota = OTAManager(hub)

        app.state.hub = hub

        app.state.tcp_server = await start_tcp_server(hub)
        await hub.bridge.start()  # binds per-node listeners only if enabled
        app.state.bg_tasks = [
            asyncio.create_task(bg.liveness_sweep(hub)),
            asyncio.create_task(bg.output_flusher(hub)),
            asyncio.create_task(bg.retention_pruner(hub)),
            asyncio.create_task(bg.stats_broadcaster(hub)),
            asyncio.create_task(bg.loop_lag_monitor(hub)),
            asyncio.create_task(bg.nightly_backup(hub)),
        ]
        print("hub: swarm TCP on %s:%d, web on %s:%d" % (
            config.PROCESS.tcp_host, config.PROCESS.tcp_port,
            config.PROCESS.http_host, config.PROCESS.http_port,
        ))

    @app.on_event("shutdown")
    async def _shutdown():
        for t in getattr(app.state, "bg_tasks", []):
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
        server = getattr(app.state, "tcp_server", None)
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        hub = getattr(app.state, "hub", None)
        if hub is not None:
            if hub.bridge is not None:
                with contextlib.suppress(Exception):
                    await hub.bridge.stop()
            await hub.db.close()

    app.include_router(rest_router, prefix="/api")
    app.include_router(ws_router)

    # Serve the built React/SPA UI if present. html=True makes it serve
    # index.html at / and fall back to it for client-side routes.
    static_dir = config.PROCESS.static_dir
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


app = build_app()


def main():
    uvicorn.run(
        app,
        host=config.PROCESS.http_host,
        port=config.PROCESS.http_port,
        log_level="info",
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )


if __name__ == "__main__":
    main()
