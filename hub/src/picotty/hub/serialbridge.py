"""Raw TCP serial bridge: expose each node's serial as a socket.

The serial channel is otherwise reachable only through the dashboard. This binds
one TCP port per assigned node (port map in the serial_bridge table) so any tool
that speaks a raw serial socket — minicom, PuTTY, conserver, esptool — attaches
to a node unchanged:

    minicom -D tcp:HUB:PORT      # -> interactive serial to that node

Data flow on a client connection:
  socket bytes  --> hub.bridge_send(node, bytes)   (a lightweight `send`, no DB row)
  node `output` --> classifier/tcp_server --> hub.feed_bridge --> socket write

The bridge is a new *consumer* of the existing send/output paths; the firmware is
untouched. Off by default (serial_bridge_enabled); a node with no serial_tx gets
a read-only bridge (writes are dropped). Runs on the hub's single event loop as
its own asyncio listeners — no threads, no second process.
"""

from __future__ import annotations

import asyncio

from . import config

# If a client can't keep up, drop it rather than stalling the loop for everyone.
MAX_CLIENT_BACKLOG = 1 << 20  # 1 MiB buffered toward one slow reader


class SerialBridge:
    def __init__(self, hub):
        self.hub = hub
        self._servers = {}   # node_id -> asyncio.Server
        self._clients = {}   # node_id -> set[asyncio.StreamWriter]

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Bind listeners for every assigned node, if the bridge is enabled."""
        if not self.hub.settings.get("serial_bridge_enabled"):
            return
        for row in await self.hub.db.list_bridge_ports():
            await self._bind(row["node_id"], row["port"])

    async def stop(self) -> None:
        for node_id in list(self._servers):
            await self._unbind(node_id)

    async def reconcile(self) -> None:
        """Re-apply the enabled flag + port map after a settings/assignment change."""
        enabled = bool(self.hub.settings.get("serial_bridge_enabled"))
        want = {}
        if enabled:
            want = {r["node_id"]: r["port"] for r in await self.hub.db.list_bridge_ports()}
        # Drop listeners no longer wanted (or a changed port).
        for node_id in list(self._servers):
            if want.get(node_id) != self._port_of(node_id):
                await self._unbind(node_id)
        # Bind newly wanted ones.
        for node_id, port in want.items():
            if node_id not in self._servers:
                await self._bind(node_id, port)

    def _port_of(self, node_id):
        srv = self._servers.get(node_id)
        if not srv or not srv.sockets:
            return None
        try:
            return srv.sockets[0].getsockname()[1]
        except Exception:
            return None

    async def _bind(self, node_id: str, port: int) -> None:
        if node_id in self._servers:
            return
        try:
            server = await asyncio.start_server(
                self._make_handler(node_id), config.PROCESS.bridge_host, port
            )
        except OSError as e:
            await self.hub.audit("error", node_id, "serial bridge bind :%d failed: %s" % (port, e))
            return
        self._servers[node_id] = server
        self._clients.setdefault(node_id, set())
        await self.hub.audit("settings", node_id, "serial bridge listening on :%d" % port)

    async def _unbind(self, node_id: str) -> None:
        server = self._servers.pop(node_id, None)
        for w in list(self._clients.get(node_id, ())):
            try:
                w.close()
            except Exception:
                pass
        self._clients.pop(node_id, None)
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass

    # -- per-connection handler ----------------------------------------------

    def _make_handler(self, node_id: str):
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            if not self.hub.registry.is_online(node_id):
                writer.close()
                return
            self._clients.setdefault(node_id, set()).add(writer)
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    # socket -> node. Bounded exactly like `send`: bridge_send
                    # rejects if the node is gone; a write error ends the client.
                    ok = await self.hub.bridge_send(node_id, data)
                    if not ok:
                        break
            except (ConnectionError, OSError):
                pass
            finally:
                self._clients.get(node_id, set()).discard(writer)
                try:
                    writer.close()
                except Exception:
                    pass
        return handle

    def close_clients(self, node_id: str) -> None:
        """Drop every bridge client for a node (called when it goes offline).
        The listener stays bound so a client can reconnect once the node is back."""
        for w in list(self._clients.get(node_id, ())):
            try:
                w.close()
            except Exception:
                pass
        if node_id in self._clients:
            self._clients[node_id] = set()

    # -- node output -> connected clients ------------------------------------

    def feed(self, node_id: str, text: str) -> None:
        """Mirror a node's output chunk to every attached bridge client. Called
        from the output hot path (sync); StreamWriter.write buffers, so we never
        await here. A client whose buffer blows past the cap is dropped."""
        clients = self._clients.get(node_id)
        if not clients:
            return
        data = text.encode("utf-8", "replace")
        for w in list(clients):
            try:
                transport = w.transport
                if transport is not None and transport.get_write_buffer_size() > MAX_CLIENT_BACKLOG:
                    clients.discard(w)
                    w.close()
                    continue
                w.write(data)
            except Exception:
                clients.discard(w)
                try:
                    w.close()
                except Exception:
                    pass
