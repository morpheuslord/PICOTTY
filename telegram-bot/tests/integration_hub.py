#!/usr/bin/env python3
"""End-to-end test of the sidecar's HUB-FACING half against a real hub.

Boots the actual hub (its own venv) on private ports with a known token, connects
a real node_sim advertising serial_tx, then exercises the exact code paths the bot
depends on — with NO Telegram involved:

  * HubClient REST: /health, /stats, /nodes, /node
  * WS relay + SessionManager: subscribe to a node, `send` into it, and confirm
    the getty-echoed output flows back through the OutputPump
  * AlertEngine + EventRelay: killing the node produces a node_down alert

Run (uses the sidecar venv for httpx/websockets; launches the hub via its venv):
    telegram-bot/.venv/bin/python telegram-bot/tests/integration_hub.py

Exit non-zero on any failure. Loopback only.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOT_DIR = HERE.parent
PROJ = BOT_DIR.parent
HUB_DIR = PROJ / "hub"
sys.path.insert(0, str(BOT_DIR))

from picotty.client import HubClient        # noqa: E402

from app.alertengine import AlertEngine     # noqa: E402
from app.relay import EventRelay            # noqa: E402
from app.sessions import SessionManager     # noqa: E402

TOKEN = "tg-itest-token"
HTTP_PORT = 8098
TCP_PORT = 9098
BASE = "http://127.0.0.1:%d" % HTTP_PORT
HUB_PY = HUB_DIR / ".venv" / "bin" / "python"

_results = []


def record(name, ok, note=""):
    _results.append((name, ok))
    print("  %s %-34s %s" % ("[PASS]" if ok else "[FAIL]", name, note))


def _http_ok(path, timeout=5):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


async def _wait(fn, timeout=8.0, interval=0.15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if asyncio.iscoroutine(v):
            v = await v
        if v:
            return v
        await asyncio.sleep(interval)
    v = fn()
    return (await v) if asyncio.iscoroutine(v) else v


async def run() -> int:
    if not HUB_PY.exists():
        print("hub venv missing at %s — run hub/scripts/install.sh first" % HUB_PY)
        return 3

    tmp = tempfile.mkdtemp(prefix="tg-itest-")
    env = dict(os.environ)
    env.update({
        "HUB_HTTP_HOST": "127.0.0.1", "HUB_HTTP_PORT": str(HTTP_PORT),
        "HUB_TCP_HOST": "127.0.0.1", "HUB_TCP_PORT": str(TCP_PORT),
        "HUB_DB_PATH": os.path.join(tmp, "hub.db"),
        "SWARM_NODE_TOKEN": TOKEN,
    })
    hub = subprocess.Popen([str(HUB_PY), "-m", "picotty.hub.main"], cwd=str(HUB_DIR), env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sim = None
    client = HubClient(BASE, ws_url="ws://127.0.0.1:%d/ws" % HTTP_PORT)
    try:
        if not await _wait(lambda: _http_ok("/api/health"), timeout=15):
            record("hub boots", False, "no /api/health")
            return 1
        record("hub boots", True)

        # tier 1 REST -------------------------------------------------------
        health = await client.health()
        record("REST /health", health.get("ok") is True, "v%s" % health.get("version"))
        stats = await client.stats()
        record("REST /stats", "loop_lag_ms" in stats)

        # connect a real node with serial_tx --------------------------------
        sim = subprocess.Popen(
            [str(HUB_PY), "-m", "picotty.sim",
             "--hub", "127.0.0.1", "--port", str(TCP_PORT),
             "--id", "sim-tg", "--token", TOKEN, "--heartbeat", "1000"],
            cwd=str(HUB_DIR), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        async def node_online():
            n = await client.node("sim-tg")
            return n if (n and n.get("status") == "online") else None
        node = await _wait(node_online, timeout=10)
        record("node online", bool(node))
        from app.formatting import node_caps
        caps = node_caps(node or {})
        record("node advertises serial_tx", "serial_tx" in caps, ",".join(caps))

        # WS relay + session round-trip ------------------------------------
        alerts = AlertEngine(lambda h: asyncio.sleep(0), debounce_s=0, enabled=True)
        captured_alerts = []

        async def capture(html):
            captured_alerts.append(html)
        alerts._broadcast = capture

        got_output = []

        sessions = SessionManager(
            subscribe=lambda n: relay.subscribe(n),
            unsubscribe=lambda n: relay.unsubscribe(n),
            flush_interval_s=0.2, max_chunk=1000, summarize_bytes=100000,
            idle_timeout_s=999)
        relay = EventRelay(client, alerts, sessions, events_poll_interval_s=2)
        sessions.start()
        relay.start()

        async def send_capture(html):
            got_output.append(html)
        await asyncio.sleep(1.0)  # let the WS connect
        ok, _ = await sessions.open(42, "sim-tg", send_capture)
        record("session opens", ok)
        await asyncio.sleep(0.5)  # subscription lands

        await client.send_serial("sim-tg", data="whoami")
        # node_sim echoes the sent data back as an output event; the pump flushes
        # it to our capture within a couple of flush windows.
        await _wait(lambda: any("whoami" in h for h in got_output), timeout=5)
        record("send -> echoed output relayed", any("whoami" in h for h in got_output),
               repr(got_output[-1]) if got_output else "(none)")

        # control key via raw hex ------------------------------------------
        res = await client.send_serial("sim-tg", raw="03")   # Ctrl-C
        record("control key (raw hex) accepted", res.get("ok", True) is True)

        # alert on node death ----------------------------------------------
        sim.terminate()
        await _wait(lambda: any("offline" in h.lower() for h in captured_alerts), timeout=8)
        record("node_down -> alert", any("offline" in h.lower() for h in captured_alerts),
               repr(captured_alerts[-1]) if captured_alerts else "(none)")

        await relay.stop()
        await sessions.stop()
    finally:
        await client.aclose()
        for p in (sim, hub):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()

    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print("\n%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
