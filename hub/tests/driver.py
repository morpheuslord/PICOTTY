"""A frame-level node driver for integration tests.

Unlike tools/node_sim.py (which chatters randomly), this gives a test exact
control: connect, hello, then send/await individual frames. Used by the
integration checks so prompt-state, expect, queue, and bridge behavior is
deterministic. Not shipped to nodes — a test aid only.
"""

from __future__ import annotations

import asyncio
import json
import struct


def encode(obj) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(body)) + body


async def read_frame(reader) -> dict:
    header = await reader.readexactly(4)
    (n,) = struct.unpack(">I", header)
    body = await reader.readexactly(n)
    return json.loads(body)


class DriverNode:
    def __init__(self, host, port, node_id, token, caps=None, layout="us"):
        self.host, self.port = host, port
        self.node_id, self.token = node_id, token
        self.caps = caps if caps is not None else ["hid", "cdc", "serial_tx"]
        self.layout = layout
        self.reader = self.writer = None
        self.inbox = asyncio.Queue()
        self._pump = None

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        await self.send({"type": "hello", "id": self.node_id, "token": self.token,
                         "fw": "test", "cap": self.caps, "layout": self.layout})
        self._pump = asyncio.create_task(self._reader_pump())

    async def _reader_pump(self):
        try:
            while True:
                self.inbox.put_nowait(await read_frame(self.reader))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass

    async def send(self, obj):
        self.writer.write(encode(obj))
        await self.writer.drain()

    async def output(self, text):
        await self.send({"type": "output", "text": text, "ts": 0})

    async def heartbeat(self, host=None):
        msg = {"type": "heartbeat", "id": self.node_id}
        if host is not None:
            msg["host"] = bool(host)
        await self.send(msg)

    async def expect_frame(self, pred, timeout=5.0):
        """Wait for the next inbound frame matching pred(frame) -> bool."""
        async def _wait():
            while True:
                frame = await self.inbox.get()
                if pred(frame):
                    return frame
        return await asyncio.wait_for(_wait(), timeout)

    async def close(self):
        if self._pump:
            self._pump.cancel()
        if self.writer:
            self.writer.close()
