"""PICOTTY — networked serial console + HID keyboard injection for a fleet of
headless machines.

One distribution, three import surfaces (see Dev-Docs/improvement-docs/picotty-packaging.md):

- ``picotty.hub``      — the server (registry + SQLite + :9000 TCP + FastAPI).
                         Needs the ``[hub]`` extra.
- ``picotty.client``   — the SDK: :class:`~picotty.client.HubClient` (REST) and
                         :class:`~picotty.client.HubEvents` (WebSocket feed).
                         Lean; base install (httpx + websockets) only.
- ``picotty.protocol`` — the wire protocol: frame pack/unpack, validation,
                         ``PROTOCOL_VERSION`` and command constants. The single
                         authoritative definition shared by hub, client, sim.

The version is single-sourced from the installed distribution metadata (the
``version`` field in pyproject.toml).
"""

from __future__ import annotations

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("picotty")
except Exception:  # running from a source tree with no metadata yet
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
