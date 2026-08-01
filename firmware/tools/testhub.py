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
    python3 testhub.py --ota <dir>     # push a firmware bundle to the node, then exit
    python3 testhub.py --framecheck    # offline framing unit checks; no node/hardware

IMPORTANT — HID side effects: when the node runs a `type`/`keys`/`sequence`
command it injects those keystrokes over USB into WHATEVER MACHINE THE PICO IS
PLUGGED INTO. For a clean test, plug the Pico's USB into a spare machine or VM
(with a text field focused) and run this tool on a different box. The ping/read/
config/heartbeat checks have no HID side effects and are always safe.

IMPORTANT — OTA rewrites the node: the `ota <dir>` command and `--ota <dir>` mode
push a firmware bundle to the connected node, which OVERWRITES its files and soft-
reloads it onto the new firmware. Only aim it at a node you intend to reflash.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
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
        self.layout = None
        self.connected = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.pending_results = {}   # cmd_id -> Future(dict)
        self.quiet_cmd_ids = set()  # cmd_ids whose result frame should not be echoed
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
        self.layout = hello.get("layout")
        self.connected.set()
        tok = GREEN("token ok") if ok_token else YELLOW("token NOT checked")
        print(GREEN("\n== node connected =="))
        print("  id=%s fw=%s caps=%s layout=%s from %s (%s)" % (
            node_id, hello.get("fw"), ",".join(self.caps), self.layout, addr, tok))
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
            quiet = cmd_id in self.quiet_cmd_ids
            self.quiet_cmd_ids.discard(cmd_id)
            # Quiet results (OTA chunk acks) are handled by their awaiter, not echoed;
            # a FAILED quiet result is always surfaced so a stuck stream is visible.
            if not quiet or status != "ok":
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

    async def send_command(self, obj, timeout=5.0, quiet=False):
        """Send a command carrying a cmd_id and await its result.

        `quiet` suppresses the per-send echo — used for OTA chunk streams, which
        would otherwise print thousands of lines."""
        cmd_id = obj["cmd_id"]
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self.pending_results[cmd_id] = fut
        if quiet:
            self.quiet_cmd_ids.add(cmd_id)
        else:
            print("%s -> %s %s" % (hhmmss(), CYAN("send:"), {k: v for k, v in obj.items() if k != "cmd_id"}))
        if not await self._send(obj):
            self.pending_results.pop(cmd_id, None)
            self.quiet_cmd_ids.discard(cmd_id)
            return None
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending_results.pop(cmd_id, None)
            self.quiet_cmd_ids.discard(cmd_id)
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

    # -- OTA firmware push ---------------------------------------------------

    @staticmethod
    def _collect_bundle(directory):
        """Walk <directory> and return a sorted list of (rel_path, bytes) for every
        file under it. Relative paths use forward slashes so they match the node's
        drive layout regardless of the host OS."""
        bundle = []
        for root, _dirs, names in os.walk(directory):
            for name in names:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, directory).replace(os.sep, "/")
                with open(full, "rb") as f:
                    bundle.append((rel, f.read()))
        bundle.sort(key=lambda item: item[0])
        return bundle

    async def push_ota(self, directory, warn=True):
        """Read every file under <directory>, stage it on the connected node with
        ota_begin/ota_chunk, commit, and report the result. Returns True only if the
        commit result was ok. The node soft-reloads after a good commit, so a
        disconnect right after the ok is expected, not a failure."""
        directory = os.path.abspath(os.path.expanduser(directory))
        if not os.path.isdir(directory):
            print(RED("not a directory: %s" % directory))
            return False
        if self.writer is None:
            print(RED("no node connected"))
            return False
        bundle = self._collect_bundle(directory)
        if not bundle:
            print(RED("no files found under %s" % directory))
            return False
        if warn:
            print(RED("\n!! OTA REWRITES THE NODE'S FIRMWARE FILES AND REBOOTS IT !!"))
            print(YELLOW("   pushing %d file(s) from %s to node %s" % (
                len(bundle), directory, self.node_id)))
            if "ota" not in self.caps:
                print(YELLOW("   note: node did not advertise the 'ota' capability; "
                             "it will likely reject this"))

        # Build the manifest: per-file size + sha256, plus a total sha over the
        # concatenated contents (bundle order) for the whole-bundle check.
        total = hashlib.sha256()
        manifest = []
        for rel, blob in bundle:
            total.update(blob)
            manifest.append({
                "path": rel, "size": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(),
            })

        res = await self.send_command(
            {"type": "ota_begin", "cmd_id": self._cmd_id(),
             "files": manifest, "total_sha256": total.hexdigest()}, timeout=15)
        if not (res and res.get("status") == "ok"):
            print(RED("  ota_begin failed: %r" % (res.get("payload") if res else None)))
            return False
        print(GREEN("  staged %d file(s)" % len(manifest)))

        CHUNK = 512  # bytes per ota_chunk -> 1024 hex chars, well under the frame cap
        for rel, blob in bundle:
            seq = 0
            sent = 0
            for off in range(0, len(blob), CHUNK):
                piece = blob[off:off + CHUNK]
                res = await self.send_command(
                    {"type": "ota_chunk", "cmd_id": self._cmd_id(),
                     "path": rel, "seq": seq, "data": piece.hex()},
                    timeout=15, quiet=True)
                if not (res and res.get("status") == "ok"):
                    print(RED("\n  chunk %d of %s failed: %r" % (
                        seq, rel, res.get("payload") if res else None)))
                    return False
                seq += 1
                sent += len(piece)
                print("\r  %-24s %6d/%-6d bytes  (%d chunk%s)" % (
                    rel, sent, len(blob), seq, "" if seq == 1 else "s"),
                    end="", flush=True)
            print()  # end the progress line for this file

        print("  committing…")
        # After a good commit the node writes the pending marker, replies ok, and
        # soft-reloads — so the ok arrives, then the link drops. A vanished result
        # (node reloaded before the reply flushed) is inconclusive, not a pass.
        res = await self.send_command(
            {"type": "ota_commit", "cmd_id": self._cmd_id()}, timeout=20)
        if res and res.get("status") == "ok":
            print(GREEN("  commit ok: %r" % res.get("payload")))
            print(DIM("  node is soft-reloading; expect a disconnect then a reconnect"))
            return True
        if res is None:
            print(YELLOW("  no commit result (node may have reloaded before replying)"))
        else:
            print(RED("  commit failed: %r" % res.get("payload")))
        return False

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
                elif cmd == "ota":
                    if not arg.strip():
                        print(RED("usage: ota <dir>  (pushes a firmware bundle to the node)"))
                    else:
                        await self.push_ota(arg.strip())
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

        # layout: the node must advertise a keyboard layout in its hello frame.
        record("layout", bool(self.layout), "layout=%s" % (self.layout or "—"))
        if "ota" in self.caps:
            print(DIM("  note: node advertises 'ota' — firmware flashing supported "
                      "(push a bundle with the 'ota <dir>' command or --ota <dir>)"))

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
  ota <dir>         push every file under <dir> as a firmware bundle, then commit
  selftest [--hid]  run automated checks (add --hid to test keystrokes)
  help              this list
  quit              exit
note: type/keys/seq inject keystrokes into the machine the Pico is plugged into.
note: ota REWRITES the node's firmware files and reboots it onto the new build."""


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
        elif args.ota:
            # Wait for a node, push the bundle, confirm the commit, exit.
            print(RED("!! --ota REWRITES THE NODE'S FIRMWARE FILES AND REBOOTS IT !!"))
            try:
                await asyncio.wait_for(hub.connected.wait(), timeout=args.wait)
            except asyncio.TimeoutError:
                print(RED("no node connected within %ds" % args.wait))
                return 2
            ok = await hub.push_ota(args.ota)
            return 0 if ok else 1
        else:
            await hub.repl()
            return 0


# ---- offline framing checks (no node, no hardware) -------------------------

def _import_firmware_wire():
    """Import the REAL firmware wire.py (../circuitpython/wire.py) so the checks
    run against the code that ships to nodes, not a copy."""
    here = os.path.dirname(os.path.abspath(__file__))
    cp_dir = os.path.abspath(os.path.join(here, os.pardir, "circuitpython"))
    if cp_dir not in sys.path:
        sys.path.insert(0, cp_dir)
    import wire
    return wire


def frame_check() -> bool:
    """Unit-check the length-prefixed framing in firmware/circuitpython/wire.py.

    Runs with no node attached:  python3 testhub.py --framecheck

    This guards the exact regression that took a node offline in the field: the
    frame accumulator consumed a parsed frame with `del bytearray[:n]`, which
    works under CPython but raises TypeError on CircuitPython (its bytearray has
    no __delitem__). A plain round-trip on your dev machine would pass anyway, so
    the last check FORCES CircuitPython's restriction to make the bug reproduce
    here instead of on hardware.
    """
    wire = _import_firmware_wire()
    checks = []

    def ok(name, cond, note=""):
        checks.append((name, bool(cond)))
        print(("  " + (GREEN("[PASS]") if cond else RED("[FAIL]")) + " %-22s %s") % (name, note))

    print(CYAN("\n=== framing checks (wire.py) ==="))

    # Two whole frames delivered in one chunk: both pop, buffer ends empty.
    r = wire.FrameReader()
    r.feed(wire.encode({"type": "a", "n": 1}) + wire.encode({"type": "b", "n": 2}))
    m1, m2, m3 = r.pop(), r.pop(), r.pop()
    ok("two-frames", m1 == {"type": "a", "n": 1} and m2 == {"type": "b", "n": 2} and m3 is None)
    ok("buffer-drained", len(r._buf) == 0, "%d byte(s) left" % len(r._buf))

    # A single frame dribbled in across several feeds: no premature pop.
    r = wire.FrameReader()
    full = wire.encode({"k": "split"})
    r.feed(full[:2]); a = r.pop()
    r.feed(full[2:6]); b = r.pop()
    r.feed(full[6:]); cframe = r.pop()
    ok("partial-frame", a is None and b is None and cframe == {"k": "split"})

    # A frame followed by the START of the next: popping the first must RETAIN
    # the trailing bytes. This is the consume path that crashed on hardware.
    r = wire.FrameReader()
    f1, f2 = wire.encode({"i": 1}), wire.encode({"i": 2})
    r.feed(f1 + f2[:3])
    first = r.pop()
    leftover = len(r._buf)
    r.feed(f2[3:])
    second = r.pop()
    ok("leftover-retained", first == {"i": 1} and second == {"i": 2} and leftover == 3,
       "kept %d byte(s) between pops" % leftover)

    # Oversized length prefix -> ProtocolError (no unbounded memory growth).
    r = wire.FrameReader(max_frame_bytes=16)
    r.feed(struct.pack(">I", 9999))
    try:
        r.pop(); raised = False
    except wire.ProtocolError:
        raised = True
    ok("oversized-rejected", raised)

    # Undecodable JSON body -> ProtocolError.
    r = wire.FrameReader()
    bad = b"not-json"
    r.feed(struct.pack(">I", len(bad)) + bad)
    try:
        r.pop(); raised = False
    except wire.ProtocolError:
        raised = True
    ok("bad-json-rejected", raised)

    # CircuitPython parity: force a bytearray with no __delitem__ onto the
    # accumulator and confirm a frame with trailing bytes still parses. The
    # fixed consume rebinds a slice; the old `del buf[:n]` raises here exactly
    # as it did on the Pico.
    class _NoDelByteArray(bytearray):
        def __delitem__(self, _key):
            raise TypeError("'bytearray' object doesn't support item deletion")

    r = wire.FrameReader()
    r._buf = _NoDelByteArray()
    r.feed(wire.encode({"cp": "ok"}) + b"\x00\x00")  # trailing bytes force a real consume
    try:
        m = r.pop()
        cp_ok, note = (m == {"cp": "ok"}), "consumed without del"
    except TypeError as e:
        cp_ok, note = False, RED("del on bytearray: %s" % e)
    ok("circuitpython-consume", cp_ok, note)

    passed, total = sum(1 for _, c in checks if c), len(checks)
    line = "%d/%d framing checks passed" % (passed, total)
    print(CYAN("=== ") + (GREEN(line) if passed == total else RED(line)) + CYAN(" ===\n"))
    return passed == total


def main():
    ap = argparse.ArgumentParser(description="Mock hub for testing a Pico node")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--token", help="enforce this node token (default: accept any)")
    ap.add_argument("--selftest", action="store_true", help="run automated checks then exit")
    ap.add_argument("--hid", action="store_true", help="include keystroke-injection checks in --selftest")
    ap.add_argument("--ota", metavar="DIR", help="push the firmware bundle under DIR to the node, then exit (REWRITES the node)")
    ap.add_argument("--wait", type=int, default=60, help="seconds to wait for a node in --selftest/--ota")
    ap.add_argument("--framecheck", action="store_true",
                    help="run offline framing unit checks (no node/hardware) then exit")
    args = ap.parse_args()
    if args.framecheck:
        sys.exit(0 if frame_check() else 1)
    try:
        rc = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
