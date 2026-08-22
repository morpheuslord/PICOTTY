"""Hub failover for the sidecar: talk to a primary hub, fall over to a backup.

The boards fail over between two hubs; without this the phone control plane would
still hang off ONE hub, so a primary-hub outage takes the bot down even though the
backup hub is happily driving the fleet. FailoverHub holds a HubClient per hub and,
on a connection error, advances to the next hub and retries — so every REST call
and the WS relay follow whichever hub is actually up.

It duck-types HubClient (same method names, via __getattr__ proxying) plus
`advance()` / `current_base`, so the EventRelay can nudge the WebSocket onto the
live hub after a drop. With a single hub configured it is a thin pass-through.
"""

from __future__ import annotations

import httpx

from picotty.client import HubClient

# Errors that mean "this hub is unreachable" (vs. a 4xx the hub actually answered,
# which HubClient already surfaces as a normal result). On any of these we fail
# over to the next hub.
_CONN_ERRORS = (
    httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
    httpx.PoolTimeout, httpx.RemoteProtocolError, httpx.WriteError,
    ConnectionError, OSError,
)


# Position labels so the user can pick a source hub by name ("/source backup").
_LABELS = ("primary", "backup")


class FailoverHub:
    def __init__(self, endpoints, *, timeout: float = 10.0):
        # endpoints: [(base_url, ws_url), ...], primary first.
        self._endpoints = list(endpoints)
        self._clients = [HubClient(b, ws_url=w, timeout=timeout) for b, w in self._endpoints]
        self._i = 0

    # -- current hub ----------------------------------------------------------

    @property
    def current_base(self) -> str:
        return self._endpoints[self._i][0]

    @property
    def multi(self) -> bool:
        return len(self._clients) > 1

    def label_for(self, i: int) -> str:
        return _LABELS[i] if i < len(_LABELS) else "hub%d" % (i + 1)

    @property
    def active_label(self) -> str:
        return self.label_for(self._i)

    def advance(self) -> str:
        """Move to the next hub (after a REST/WS failure). Returns its base URL."""
        if len(self._clients) > 1:
            self._i = (self._i + 1) % len(self._clients)
        return self.current_base

    # -- user source selection ("which hub the bot acts on") ------------------

    def select(self, which: str):
        """Pin the active hub by label (primary/backup), 1-based index, or a
        substring of the base URL. Returns the new base URL, or None if unknown.
        Automatic failover still applies afterwards if the chosen hub goes down."""
        w = (which or "").strip().lower()
        idx = None
        for i in range(len(self._clients)):
            if self.label_for(i).lower() == w:
                idx = i
                break
        if idx is None and w.isdigit():
            n = int(w) - 1
            if 0 <= n < len(self._clients):
                idx = n
        if idx is None:
            for i, (base, _ws) in enumerate(self._endpoints):
                if w and w in base.lower():
                    idx = i
                    break
        if idx is None:
            return None
        self._i = idx
        return self.current_base

    def endpoints_status(self) -> list:
        return [{"label": self.label_for(i), "base": self._endpoints[i][0], "active": i == self._i}
                for i in range(len(self._clients))]

    async def hub_ids(self) -> dict:
        """Best-effort {label: hub_id} by probing each hub's /health."""
        out = {}
        for i, c in enumerate(self._clients):
            try:
                h = await c.health()
                out[self.label_for(i)] = h.get("hub_id")
            except Exception:
                out[self.label_for(i)] = None
        return out

    # -- WebSocket (event stream) --------------------------------------------

    def events_stream(self, **kw):
        return self._clients[self._i].events_stream(**kw)

    def events_ws(self, **kw):
        return self.events_stream(**kw)

    # -- lifecycle ------------------------------------------------------------

    async def aclose(self) -> None:
        for c in self._clients:
            try:
                await c.aclose()
            except Exception:
                pass

    # -- REST, with failover --------------------------------------------------

    async def _call(self, name, *a, **k):
        last = None
        for _ in range(len(self._clients)):
            try:
                return await getattr(self._clients[self._i], name)(*a, **k)
            except _CONN_ERRORS as e:
                last = e
                self.advance()   # this hub is down — try the next one
        raise last

    def __getattr__(self, name):
        # Proxy any HubClient coroutine method (health, nodes, send_serial, …) with
        # failover. Private/dunder names are NOT proxied, so attribute access during
        # __init__ can't recurse into a half-built object.
        if name.startswith("_"):
            raise AttributeError(name)

        async def _proxy(*a, **k):
            return await self._call(name, *a, **k)

        return _proxy
