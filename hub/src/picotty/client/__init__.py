"""``picotty.client`` — the hub SDK.

Everything a program needs to talk to a running hub, over the same REST API and
``/ws`` event stream the dashboard uses:

- :class:`HubClient`  — async REST wrapper (health, stats, nodes, commands, send).
- :class:`HubEvents`  — async iterator over the WebSocket event feed, with
                        per-node subscribe/unsubscribe.

Lean by design: only ``httpx`` and ``websockets``, so the base ``picotty`` install
(no ``[hub]`` extra, no FastAPI) is all a client — the Telegram sidecar, a cron
health check, CI — needs.

    from picotty.client import HubClient

    async with HubClient("http://hub:8080") as hub:
        print(await hub.health())
        async with hub.events_stream() as stream:
            await stream.subscribe("node-01")
            async for ev in stream:
                ...
"""

from __future__ import annotations

import contextlib
import json
from typing import AsyncIterator, Optional

import httpx
import websockets


class HubError(Exception):
    """A hub REST call returned a non-JSON body or a server (5xx) error."""


def _derive_ws(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/ws"
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + "/ws"
    return "ws://" + base + "/ws"


class HubClient:
    """Async REST client for a running hub. The hub mounts REST under ``/api`` and
    the WebSocket at ``/ws``; pass the server root as ``base_url``."""

    def __init__(self, base_url: str, *, timeout: float = 10.0,
                 ws_url: Optional[str] = None):
        self._base = base_url.rstrip("/")
        self._ws_url = ws_url or _derive_ws(self._base)
        self._http = httpx.AsyncClient(base_url=self._base + "/api", timeout=timeout)

    async def __aenter__(self) -> "HubClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- generic REST ---------------------------------------------------------

    async def get(self, path: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        r = await self._http.get(path, params=params)
        r.raise_for_status()
        return r.json()

    async def post(self, path: str, body: Optional[dict] = None) -> dict:
        r = await self._http.post(path, json=body or {})
        if r.status_code >= 500:
            r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            raise HubError("non-JSON reply from hub (%s)" % r.status_code)

    # -- typed convenience ----------------------------------------------------

    async def health(self) -> dict:
        return await self.get("/health")

    async def stats(self) -> dict:
        return await self.get("/stats")

    async def nodes(self, **filters) -> list[dict]:
        return (await self.get("/nodes", **filters)).get("nodes", [])

    async def node(self, node_id: str) -> Optional[dict]:
        data = await self.get("/nodes/%s" % node_id)
        return data.get("node") if data.get("ok") else None

    async def events(self, since: Optional[int] = None, limit: int = 100,
                     type_: Optional[str] = None) -> list[dict]:
        data = await self.get("/events", since=since, limit=limit, type=type_)
        return data.get("events", [])

    async def cmd(self, node_id: str, command: dict) -> dict:
        return await self.post("/nodes/%s/cmd" % node_id, command)

    async def send_serial(self, node_id: str, *, data: Optional[str] = None,
                          raw: Optional[str] = None) -> dict:
        """Write into a node's serial getty (the hub `send` command). Exactly one
        of ``data`` (UTF-8 text) or ``raw`` (hex bytes), mirroring firmware."""
        body: dict = {"type": "send"}
        if data is not None:
            body["data"] = data
        if raw is not None:
            body["raw"] = raw
        return await self.cmd(node_id, body)

    async def reboot(self, node_id: str) -> dict:
        return await self.post("/nodes/%s/reboot" % node_id)

    async def sysrq(self, node_id: str, command: str) -> dict:
        return await self.post("/nodes/%s/sysrq" % node_id, {"command": command})

    # -- live event stream ----------------------------------------------------

    def events_stream(self, **connect_kwargs) -> "HubEventsCM":
        """Return an async context manager yielding a :class:`HubEvents`."""
        return HubEventsCM(self._ws_url, connect_kwargs)

    # Short alias used in the docstring / common case.
    def events_ws(self, **connect_kwargs) -> "HubEventsCM":
        return self.events_stream(**connect_kwargs)


class HubEventsCM:
    def __init__(self, ws_url: str, connect_kwargs: dict):
        self._ws_url = ws_url
        self._connect_kwargs = connect_kwargs
        self._sock = None

    async def __aenter__(self) -> "HubEvents":
        kwargs = {"ping_interval": 20, "ping_timeout": 20, "max_size": 2 ** 20}
        kwargs.update(self._connect_kwargs)
        self._sock = await websockets.connect(self._ws_url, **kwargs)
        return HubEvents(self._sock)

    async def __aexit__(self, *exc) -> None:
        if self._sock is not None:
            await self._sock.close()


class HubEvents:
    """An async iterator over the hub's WebSocket event feed.

    Low-volume events (node up/down, heartbeat, node_state) arrive to everyone;
    high-volume per-node ``output``/``result`` events arrive only after
    :meth:`subscribe`."""

    def __init__(self, sock):
        self._sock = sock

    async def subscribe(self, node_id: str) -> None:
        await self._sock.send(json.dumps({"type": "subscribe", "node_id": node_id}))

    async def unsubscribe(self, node_id: str) -> None:
        await self._sock.send(json.dumps({"type": "unsubscribe", "node_id": node_id}))

    async def __aiter__(self) -> AsyncIterator[dict]:
        async for raw in self._sock:
            try:
                yield json.loads(raw)
            except (ValueError, TypeError):
                continue


__all__ = ["HubClient", "HubEvents", "HubEventsCM", "HubError"]
