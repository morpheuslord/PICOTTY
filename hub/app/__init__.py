"""Swarm controller hub.

A single asyncio process that runs two faces on one event loop:

- a raw TCP server on :9000 facing the node swarm (the wire protocol), and
- a FastAPI app on :8080 facing the browser (REST + WebSocket + static React).

They share one in-memory registry and one SQLite database.
"""

__version__ = "1.0.0"
