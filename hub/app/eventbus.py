"""The WebSocket event bus.

Bridges what the TCP server ingests to what browsers see. Low-volume events
(node up/down, heartbeats, results-as-summary, audit) broadcast to everyone.
The one high-volume channel, serial output, is subscription-scoped: it reaches
only browsers that have that node's console open.

Backpressure: each client has a bounded queue. If a browser is slow, we drop
its oldest events and mark a gap rather than stalling the loop for everyone —
the database is the complete record, the live stream is best-effort.
"""

from __future__ import annotations

import asyncio


class WSClient:
    def __init__(self, queue_max: int):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_max)
        self.subscriptions: set = set()
        self.gap = False

    def enqueue(self, event: dict) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest to make room; note the gap for the client.
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.gap = True
            try:
                self.queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


class EventBus:
    def __init__(self, queue_max: int):
        self._clients: set = set()
        self._queue_max = queue_max

    def register(self) -> WSClient:
        client = WSClient(self._queue_max)
        self._clients.add(client)
        return client

    def unregister(self, client: WSClient) -> None:
        self._clients.discard(client)

    def client_count(self) -> int:
        return len(self._clients)

    def broadcast(self, event: dict) -> None:
        """Deliver a low-volume event to every connected browser."""
        for client in self._clients:
            client.enqueue(event)

    def send_to_subscribers(self, node_id: str, event: dict) -> None:
        """Deliver a node-scoped event (output/result) only to subscribers."""
        for client in self._clients:
            if node_id in client.subscriptions:
                client.enqueue(event)
