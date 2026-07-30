#!/usr/bin/env python3
"""testhub — a mock hub for testing a real Pico node in isolation.

Point a node at this machine (set HUB_IP/HUB_PORT in its settings.toml to this
host, or just run this on the same box the node already dials) and this tool
acts as the hub: it accepts the connection, prints everything the node sends,
and lets you drive every command the protocol supports, interactively or as an
automated self-test.

    python3 testhub.py                 # listen on 0.0.0.0:9000, interactive
    python3 testhub.py --selftest      # run the automated checks, then exit
    python3 testhub.py --selftest --hid # also test keystroke injection (see below)
    python3 testhub.py --token <TOK>   # enforce the node's token (default: accept any)

IMPORTANT — HID side effects: when the node runs a `type`/`keys`/`sequence`
command it injects those keystrokes over USB into WHATEVER MACHINE THE PICO IS
PLUGGED INTO. For a clean test, plug the Pico's USB into a spare machine or VM
(with a text field focused) and run this tool on a different box. The ping/read/
config/heartbeat checks have no HID side effects and are always safe.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import struct
import sys
import threading
import time

# ---- framing ---------------------------------------------------------------

def encode(obj) -> bytes:
    body = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> dict:
    header = await reader.readexactly(4)
    (n,) = struct.unpack(">I", header)
    body = await reader.readexactly(n)
    return json.loads(body)


def hhmmss() -> str:
    t = time.localtime()
    return "%02d:%02d:%02d" % (t.tm_hour, t.tm_min, t.tm_sec)


def unescape(s: str) -> str:
    return s.encode("utf-8").decode("unicode_escape")


# ---- ANSI (only if a tty) --------------------------------------------------

_COLOR = sys.stdout.isatty()
def c(code, s):
    return ("\033[%sm%s\033[0m" % (code, s)) if _COLOR else s
GREEN = lambda s: c("32", s)
RED = lambda s: c("31", s)
DIM = lambda s: c("2", s)
CYAN = lambda s: c("36", s)
YELLOW = lambda s: c("33", s)


class TestHub:
    def __init__(self, args):
        self.args = args
        self.writer = None
        self.node_id = None
        self.caps = []
        self.connected = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.pending_results = {}   # cmd_id -> Future(dict)
        self.pending_pongs = {}     # nonce -> Future(rtt_ms)
        self.hb_count = 0
        self.last_hb = None
        self.counter = 1000

    # -- connection ----------------------------------------------------------

    async def handle(self, reader, writer):
        addr = writer.get_extra_info("peername")
        try:
            hello = await read_frame(reader)
        except Exception as e:
            print(RED("connection from %s closed before hello: %s" % (addr, e)))
            writer.close()
            return
        if hello.get("type") != "hello":
            print(RED("first frame from %s was not hello: %r" % (addr, hello)))
            writer.close()
            return

        node_id = hello.get("id")
        token = hello.get("token", "")
        ok_token = self._check_token(token)
        if self.args.token and not ok_token:
            print(RED("REJECT %s: bad token" % node_id))
            writer.close()
            return

        if self.writer is not None:
            print(YELLOW("replacing previous node connection"))
            try:
                self.writer.close()
            except Exception:
                pass

        self.writer, self.node_id, self.caps = writer, node_id, hello.get("cap", [])
        self.connected.set()
        tok = GREEN("token ok") if ok_token else YELLOW("token NOT checked")
        print(GREEN("\n== node connected =="))
        print("  id=%s fw=%s caps=%s from %s (%s)" % (
            node_id, hello.get("fw"), ",".join(self.caps), addr, tok))
        print(DIM("  type 'help' for commands\n"))

        try:
            while True:
                msg = await read_frame(reader)
                self._on_frame(msg)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            print(RED("\n== node %s disconnected ==\n" % node_id))
            if self.writer is writer:
                self.writer = None
                self.node_id = None
                self.connected.clear()
            writer.close()

    def _check_token(self, token: str) -> bool:
        if not self.args.token:
            return False
        want = hashlib.sha256(self.args.token.encode()).hexdigest()
        got = hashlib.sha256(token.encode()).hexdigest()
        return hmac.compare_digest(want, got)

    def _on_frame(self, msg: dict):
        t = msg.get("type")
        if t == "heartbeat":
            self.hb_count += 1
            self.last_hb = time.monotonic()
            print(DIM("%s <- heartbeat  (#%d)" % (hhmmss(), self.hb_count)))
        elif t == "output":
            for line in msg.get("text", "").splitlines() or [""]:
                print("%s <- %s %s" % (hhmmss(), CYAN("output:"), line))
        elif t == "result":
            cmd_id = msg.get("cmd_id")
            status = msg.get("status")
            payload = msg.get("payload")
            col = GREEN if status == "ok" else RED
            extra = "" if payload is None else "  payload=%r" % payload
            print("%s <- %s %s (%s)%s" % (hhmmss(), CYAN("result:"), cmd_id, col(status), extra))
            fut = self.pending_results.pop(cmd_id, None)
            if fut and not fut.done():
                fut.set_result(msg)
        elif t == "pong":
            nonce = msg.get("nonce")
            fut = self.pending_pongs.pop(nonce, None)
            if fut and not fut.done():
                fut.set_result(time.monotonic())
            else:
                print("%s <- %s %s" % (hhmmss(), CYAN("pong:"), nonce))
        elif t == "error":
            print("%s <- %s %s" % (hhmmss(), RED("error:"), msg.get("detail")))
        elif t == "bye":
            print("%s <- %s" % (hhmmss(), YELLOW("bye (node dropping)")))
        else:
            print("%s <- %s" % (hhmmss(), msg))

    # -- sending -------------------------------------------------------------

    def _cmd_id(self):
        self.counter += 1
        return "t%d" % self.counter

    async def _send(self, obj):
        if self.writer is None:
            print(RED("no node connected"))
            return False
        async with self.send_lock:
            self.writer.write(encode(obj))
            await self.writer.drain()
        return True

    async def send_command(self, obj, timeout=5.0):
        """Send a command carrying a cmd_id and await its result."""
        cmd_id = obj["cmd_id"]
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self.pending_results[cmd_id] = fut
        print("%s -> %s %s" % (hhmmss(), CYAN("send:"), {k: v for k, v in obj.items() if k != "cmd_id"}))
        if not await self._send(obj):
            self.pending_results.pop(cmd_id, None)
            return None
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending_results.pop(cmd_id, None)
            print(RED("  timeout waiting for result of %s" % cmd_id))
            return None

    async def do_ping(self, timeout=2.0):
        nonce = "n%d" % (self.counter + 1)
        self.counter += 1
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self.pending_pongs[nonce] = fut
        sent = time.monotonic()
        print("%s -> %s nonce=%s" % (hhmmss(), CYAN("ping:"), nonce))
        if not await self._send({"type": "ping", "nonce": nonce}):
            self.pending_pongs.pop(nonce, None)
            return None
        try:
            recv = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending_pongs.pop(nonce, None)
            return None
        return int((recv - sent) * 1000)

    # -- interactive REPL ----------------------------------------------------

    async def repl(self):
        await self.connected.wait()
        char_delay = 0
        async for line in stdin_lines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""
            try:
                if cmd in ("quit", "exit", "q"):
                    break
                elif cmd == "help":
                    print(HELP)
                elif cmd == "delay":
                    char_delay = int(arg or "0")
                    print("char_delay = %d ms" % char_delay)
                elif cmd == "type":
                    body = {"type": "type", "cmd_id": self._cmd_id(), "text": unescape(arg)}
                    if char_delay:
                        body["char_delay_ms"] = char_delay
                    await self.send_command(body)
                elif cmd == "run":  # like type, but appends a newline
                    body = {"type": "type", "cmd_id": self._cmd_id(), "text": unescape(arg) + "\n"}
                    if char_delay:
                        body["char_delay_ms"] = char_delay
                    await self.send_command(body)
                elif cmd == "key":
                    await self.send_command({"type": "keys", "cmd_id": self._cmd_id(), "chord": [arg.strip().upper()]})
                elif cmd == "keys":
                    chord = [p.strip().upper() for p in arg.split("+") if p.strip()]
                    await self.send_command({"type": "keys", "cmd_id": self._cmd_id(), "chord": chord})
                elif cmd == "seq":
                    steps = [{"type": "type", "text": "seq-test"}, {"delay_ms": 300}, {"type": "keys", "chord": ["ENTER"]}]
                    await self.send_command({"type": "sequence", "cmd_id": self._cmd_id(), "steps": steps, "stop_on_error": True})
                elif cmd == "read":
                    await self.send_command({"type": "read", "cmd_id": self._cmd_id()})
                elif cmd == "send":  # write text into the target's serial getty
                    body = {"type": "send", "cmd_id": self._cmd_id(), "data": unescape(arg)}
                    await self.send_command(body)
                elif cmd == "sendraw":  # write raw bytes as hex (e.g. sendraw 03 = Ctrl+C)
                    await self.send_command({"type": "send", "cmd_id": self._cmd_id(), "raw": arg.strip()})
                elif cmd == "ping":
                    rtt = await self.do_ping()
                    print(GREEN("  rtt = %dms" % rtt) if rtt is not None else RED("  no pong"))
                elif cmd == "config":
                    await self._send({"type": "config", "heartbeat_ms": int(arg or "5000")})
                    print("  config sent (no result expected)")
                elif cmd == "reboot":
                    await self._send({"type": "reboot"})
                    print("  reboot sent; node should drop and reconnect")
                elif cmd == "selftest":
                    await self.selftest(hid="--hid" in arg)
                else:
                    print(RED("unknown command: %s (try 'help')" % cmd))
            except Exception as e:
                print(RED("error: %s" % e))
        print("bye")

    # -- automated self-test -------------------------------------------------

    async def selftest(self, hid=False):
        print(CYAN("\n=== self-test ===") + ("  (with HID)" if hid else "  (safe subset; add --hid for keystrokes)"))
        results = []

        def record(name, ok, note=""):
            results.append((name, ok))
            print(("  " + (GREEN("[PASS]") if ok else RED("[FAIL]")) + " %-14s %s") % (name, note))

        # heartbeat
        start = self.hb_count
        print("  waiting up to 12s for a heartbeat…")
        for _ in range(24):
            if self.hb_count > start:
                break
            await asyncio.sleep(0.5)
        record("heartbeat", self.hb_count > start, "got %d" % (self.hb_count - start))

        # ping
        rtt = await self.do_ping()
        record("ping/pong", rtt is not None, "rtt=%sms" % (rtt if rtt is not None else "—"))

        # read
        if "cdc" in self.caps:
            res = await self.send_command({"type": "read", "cmd_id": self._cmd_id()}, timeout=3)
            record("read", bool(res) and res.get("status") == "ok",
                   "payload len=%s" % (len(res.get("payload") or "") if res else "—"))
        else:
            record("read", True, "skipped (node has no cdc capability)")

        # send (serial write). No HID side effect — bytes go to usb_cdc.data, not
        # the keyboard — so it is safe in the default subset. If a loopback is
        # wired on the bench the echo returns as output; result-only is enough here.
        if "serial_tx" in self.caps:
            res = await self.send_command(
                {"type": "send", "cmd_id": self._cmd_id(), "data": "swarm-selftest\r"}, timeout=3)
            record("send", bool(res) and res.get("status") == "ok", "serial write acked")
        else:
            record("send", True, "skipped (node has no serial_tx capability)")

        # config (no result expected; just confirm no error/disconnect)
        await self._send({"type": "config", "heartbeat_ms": 4000})
        await asyncio.sleep(1)
        record("config", self.writer is not None, "node still connected")

        # unknown command handling: node should reply failed, not crash
        res = await self.send_command({"type": "bogus", "cmd_id": self._cmd_id()}, timeout=3)
        record("unknown-cmd", bool(res) and res.get("status") == "failed", "node rejected gracefully")

        if hid:
            res = await self.send_command({"type": "type", "cmd_id": self._cmd_id(), "text": "swarm-selftest"}, timeout=4)
            record("type (HID)", bool(res) and res.get("status") == "ok", "typed into attached machine")
            res = await self.send_command({"type": "keys", "cmd_id": self._cmd_id(), "chord": ["F13"]}, timeout=4)
            record("keys (HID)", bool(res) and res.get("status") == "ok", "sent F13 (usually harmless)")

        passed = sum(1 for _, ok in results if ok)
        total = len(results)
        line = "%d/%d passed" % (passed, total)
        print(CYAN("=== ") + (GREEN(line) if passed == total else RED(line)) + CYAN(" ===\n"))
        return passed == total


HELP = """commands:
  run <text>        type <text> then Enter (the common case)
  type <text>       type literal text (supports \\n, \\t); no auto-newline
  key <NAME>        single key, e.g.  key ENTER   key F2   key DEL
  keys <A+B+C>      a chord, e.g.  keys CTRL+C     keys CTRL+ALT+DELETE
  seq               send a demo sequence (type, delay, Enter)
  send <text>       write text into the target's serial getty (supports \\n, \\r)
  sendraw <hex>     write raw bytes as hex, e.g.  sendraw 03  (Ctrl+C)   sendraw 0d  (CR)
  read              flush + return the node's serial buffer
  ping              measure round-trip time
  config <ms>       set heartbeat interval
  delay <ms>        per-character delay applied to type/run
  reboot            soft-reboot the node firmware
  selftest [--hid]  run automated checks (add --hid to test keystrokes)
  help              this list
  quit              exit
note: type/keys/seq inject keystrokes into the machine the Pico is plugged into."""


# ---- stdin as an async generator (thread-fed) ------------------------------

async def stdin_lines():
    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()

    def pump():
        for line in sys.stdin:
            loop.call_soon_threadsafe(q.put_nowait, line)
        loop.call_soon_threadsafe(q.put_nowait, None)

    threading.Thread(target=pump, daemon=True).start()
    while True:
        line = await q.get()
        if line is None:
            return
        yield line


async def main_async(args):
    hub = TestHub(args)
    server = await asyncio.start_server(hub.handle, args.host, args.port)
    print("testhub listening on %s:%d  (waiting for a node to connect)" % (args.host, args.port))
    if not args.token:
        print(YELLOW("no --token given: any token is accepted (fine for local testing)"))

    async with server:
        if args.selftest:
            # Wait for a node, run the checks once, exit with a status code.
            try:
                await asyncio.wait_for(hub.connected.wait(), timeout=args.wait)
            except asyncio.TimeoutError:
                print(RED("no node connected within %ds" % args.wait))
                return 2
            ok = await hub.selftest(hid=args.hid)
            return 0 if ok else 1
        else:
            await hub.repl()
            return 0


def main():
    ap = argparse.ArgumentParser(description="Mock hub for testing a Pico node")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--token", help="enforce this node token (default: accept any)")
    ap.add_argument("--selftest", action="store_true", help="run automated checks then exit")
    ap.add_argument("--hid", action="store_true", help="include keystroke-injection checks in --selftest")
    ap.add_argument("--wait", type=int, default=60, help="seconds to wait for a node in --selftest")
    args = ap.parse_args()
    try:
        rc = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
