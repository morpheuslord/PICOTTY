"""``picotty.hub`` — the server.

A single asyncio process that runs two faces on one event loop:

- a raw TCP server on :9000 facing the node swarm (the wire protocol), and
- a FastAPI app on :8080 facing the browser (REST + WebSocket + static dashboard).

They share one in-memory registry and one SQLite database. Import this only with
the ``[hub]`` extra installed (FastAPI, uvicorn, aiosqlite, pydantic, pyyaml); a
plain client needs :mod:`picotty.client` instead.

Public surface:

- :class:`Hub`  — the shared owner of the registry, SQLite, protocol and events.
- :func:`serve` — bring the hub up (the ``picotty-hub`` console entry point).
"""

from __future__ import annotations

from .. import __version__  # single-sourced from the distribution metadata
from .core import Hub

__all__ = ["Hub", "serve", "build_app", "__version__"]


def build_app():
    """Return the FastAPI application (lazy: imports the server stack on demand)."""
    from .main import build_app as _build_app
    return _build_app()


def serve() -> None:
    """Run the hub (uvicorn). Backs the ``picotty-hub`` console script."""
    from .main import main as _main
    _main()
