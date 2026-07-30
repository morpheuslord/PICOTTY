#!/usr/bin/env python3
"""A fake node, for testing the hub without hardware.

It speaks the same wire protocol as the real CircuitPython firmware: dials the
hub, sends hello, heartbeats on interval, answers commands with results, and
emits periodic fake serial output. Run several with different --id to simulate a
fleet.

    python -m tools.node_sim --hub 127.0.0.1 --port 9000 --id node-01 --token <TOKEN>

The token must match the hub's current node token (printed once on first hub
start, or rotated via POST /api/settings/token/rotate).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import struct
import time


def encode(obj) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> dict:
    header = await reader.readexactly(4)
    (n,) = struct.unpack(">I", header)
    body = await reader.readexactly(n)
    return json.loads(body)


CHATTER = [
    "[  OK  ] Started Daily apt download activities.",
    "kernel: eth0: link up 1000Mbps full duplex",
    "pvestatd[1142]: status update time (5.012 seconds)",
    "systemd[1]: Reached target Multi-User System.",
    "cron[882]: (root) CMD (command -v debian-sa1 > /dev/null)",
]


class SimNode:
    def __init__(self, args):
        self.args = args
        self.writer = None
        self.lock = asyncio.Lock()

    async def send(self, obj):
        async with self.lock:
            self.writer.write(encode(obj))
            await self.writer.drain()

    async def run(self):
        while True:
            try:
                await self.session()
            except (ConnectionError, OSError, asyncio.IncompleteReadError) as e:
                print("[%s] link down: %s; retrying in 2s" % (self.args.id, e))
                await asyncio.sleep(2)

    async def session(self):
        reader, self.writer = await asyncio.open_connection(self.args.hub, self.args.port)
        caps = ["hid", "cdc"]
        if not self.args.no_serial_tx:
            caps.append("serial_tx")
        await self.send({
            "type": "hello", "id": self.args.id, "token": self.args.token,
            "fw": self.args.fw, "cap": caps,
        })
        print("[%s] connected to %s:%d" % (self.args.id, self.args.hub, self.args.port))
        hb = asyncio.create_task(self._heartbeat())
        chat = asyncio.create_task(self._chatter())
        try:
            while True:
                msg = await read_frame(reader)
                await self.handle(msg)
        finally:
            hb.cancel()
            chat.cancel()

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(self.args.heartbeat / 1000)
            await self.send({"type": "heartbeat", "id": self.args.id})

    async def _chatter(self):
        while True:
            await asyncio.sleep(random.uniform(3, 8))
            await self.send({
                "type": "output", "text": random.choice(CHATTER) + "\n",
                "ts": int(time.monotonic() * 1000),
            })

    async def handle(self, msg):
        t = msg.get("type")
        cmd_id = msg.get("cmd_id")
        if t == "type":
            text = msg.get("text", "")
            await asyncio.sleep(0.2)
            await self.send({"type": "output", "text": text, "ts": _ms()})
            await self.send({"type": "result", "cmd_id": cmd_id, "status": "ok"})
            await asyncio.sleep(0.1)
            await self.send({"type": "output", "text": self._reply(text), "ts": _ms()})
        elif t == "keys":
            await self.send({"type": "output", "text": "[%s]\n" % "+".join(msg.get("chord", [])), "ts": _ms()})
            await self.send({"type": "result", "cmd_id": cmd_id, "status": "ok"})
        elif t == "sequence":
            for step in msg.get("steps", []):
                if step.get("type") == "type":
                    await self.send({"type": "output", "text": step.get("text", ""), "ts": _ms()})
                elif step.get("type") == "keys":
                    await self.send({"type": "output", "text": "[%s]\n" % "+".join(step.get("chord", [])), "ts": _ms()})
                elif "delay_ms" in step:
                    await asyncio.sleep(min(step["delay_ms"], 1000) / 1000)
            await self.send({"type": "result", "cmd_id": cmd_id, "status": "ok"})
        elif t == "send":
            # Write to the target's serial getty. Exactly one of data/raw; raw is
            # hex. Log the decoded payload and, like a real getty, echo it back
            # through the output stream so the console shows the typed characters.
            data = msg.get("data")
            raw = msg.get("raw")
            if (data is None) == (raw is None):
                await self.send({"type": "error", "cmd_id": cmd_id,
                                 "detail": "send needs exactly one of data/raw"})
                return
            if raw is not None:
                try:
                    text = bytes.fromhex(raw).decode("utf-8", "replace")
                except ValueError:
                    await self.send({"type": "result", "cmd_id": cmd_id,
                                     "status": "failed", "payload": "bad raw hex"})
                    return
            else:
                text = data
            print("[%s] send -> %r" % (self.args.id, text))
            await self.send({"type": "output", "text": text, "ts": _ms()})  # getty echo
            await self.send({"type": "result", "cmd_id": cmd_id, "status": "ok"})
        elif t == "read":
            await self.send({"type": "result", "cmd_id": cmd_id, "status": "ok",
                             "payload": "sim buffer: %d bytes\n" % random.randint(12, 120)})
        elif t == "ping":
            await self.send({"type": "pong", "nonce": msg.get("nonce")})
        elif t == "reboot":
            await self.send({"type": "bye"})
            print("[%s] reboot requested; dropping" % self.args.id)
            raise ConnectionError("reboot")
        elif t == "config":
            hb = msg.get("heartbeat_ms")
            if hb:
                self.args.heartbeat = hb

    def _reply(self, text):
        t = text.strip()
        if t == "uptime":
            return " 09:41:22 up 5 days,  1 user,  load average: 0.31, 0.28, 0.22\n"
        if t == "help":
            return "commands: status  reboot  help\n"
        return "-bash: %s: command not found\n" % (t.split(" ")[0] if t else "")


def _ms():
    return int(time.monotonic() * 1000)


def main():
    ap = argparse.ArgumentParser(description="Fake swarm node for testing the hub")
    ap.add_argument("--hub", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--id", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--fw", default="1.0.0")
    ap.add_argument("--no-serial-tx", action="store_true",
                    help="drop the serial_tx capability (simulate old firmware that can't `send`)")
    ap.add_argument("--heartbeat", type=int, default=5000, help="ms")
    args = ap.parse_args()
    try:
        asyncio.run(SimNode(args).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
