#!/usr/bin/env python3
"""Two-hub failover: cross-hub directive relay + peer visibility.

Boots TWO hubs (A = the one you drive, B = the peer that holds the node) on
private ports sharing one node token, connects a node to B, then:

  * POST /nodes/<id>/hub on A (which does NOT hold the node) relays the directive
    to B, and the node — connected to B — receives the hub_directive frame. This
    is the on-demand takeover working from a hub the board isn't connected to.
  * A's node view shows peer_hub = B's hub id (peer visibility), so a board live
    on the peer reads as active-elsewhere, not just offline.
  * A relayed call (X-Picotty-Relay: 1) is NOT relayed again (loop guard).

    hub/.venv/bin/python hub/tests/test_failover_relay.py

Exits non-zero on any failure. Loopback only.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUB_DIR = HERE.parent
sys.path.insert(0, str(HUB_DIR))
sys.path.insert(0, str(HUB_DIR / "src"))

from tests.driver import DriverNode  # noqa: E402

TOKEN = "relay-itest-token"
A_HTTP, A_TCP = 8096, 9096      # the hub you drive
B_HTTP, B_TCP = 8095, 9095      # the peer that holds the node
A = "http://127.0.0.1:%d/api" % A_HTTP
B = "http://127.0.0.1:%d/api" % B_HTTP

_results = []


def record(name, ok, note=""):
    _results.append((name, ok))
    print("  %s %-34s %s" % ("[PASS]" if ok else "[FAIL]", name, note))


def http(base, method, path, body=None, headers=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"}
    hdr.update(headers or {})
    req = urllib.request.Request(base + path, data=data, method=method, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _spawn(tmp, name, http_port, tcp_port, env_extra):
    env = dict(os.environ)
    env.update({
        "HUB_DB_PATH": os.path.join(tmp, "%s.db" % name),
        "HUB_HTTP_PORT": str(http_port), "HUB_TCP_PORT": str(tcp_port),
        "HUB_HTTP_HOST": "127.0.0.1", "HUB_TCP_HOST": "127.0.0.1",
        "SWARM_NODE_TOKEN": TOKEN,
        "PYTHONPATH": str(HUB_DIR / "src") + os.pathsep + env.get("PYTHONPATH", ""),
    })
    env.update(env_extra)
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "picotty.hub.main:app",
         "--host", "127.0.0.1", "--port", str(http_port), "--log-level", "warning"],
        cwd=str(HUB_DIR), env=env)


async def _wait(fn, timeout=8.0, interval=0.15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        await asyncio.sleep(interval)
    return fn()


async def checks():
    # node is connected to B (the peer). A is the hub we drive.
    node = DriverNode("127.0.0.1", B_TCP, "relaynode", TOKEN)
    await node.connect(hub="primary")
    # B holds it online.
    online_on_b = await _wait(lambda: http(B, "GET", "/nodes/relaynode")[1].get("node", {}).get("status") == "online")
    record("node online on peer B", bool(online_on_b))

    # A does not hold it: it is never "online" on A (it's on the peer).
    st, av = http(A, "GET", "/nodes/relaynode")
    record("node not online on driver hub A", av.get("node", {}).get("status") != "online",
           "status=%s" % av.get("node", {}).get("status"))

    # Peer visibility: A's peer_poller should mark it as held by B (hub id "hub-b").
    peer = await _wait(lambda: http(A, "GET", "/nodes/relaynode")[1].get("node", {}).get("peer_hub"), timeout=10)
    record("A shows node active on peer", peer == "hub-b", "peer_hub=%s" % peer)

    # THE FIX: steer the node from A (which doesn't hold it). A relays to B, and
    # the node — connected to B — receives the hub_directive frame.
    st, dr = http(A, "POST", "/nodes/relaynode/hub", {"action": "switch", "target": "backup"})
    frame = await node.expect_frame(lambda fr: fr.get("type") == "hub_directive", timeout=6)
    record("cross-hub relay delivers directive",
           st == 200 and dr.get("ok") is True and frame.get("action") == "switch" and frame.get("target") == "backup",
           "relayed_via=%s" % dr.get("relayed_via"))

    # Loop guard: a call already marked as a relay is NOT relayed again by A.
    st2, lg = http(A, "POST", "/nodes/relaynode/hub",
                   {"action": "switch", "target": "backup"}, headers={"X-Picotty-Relay": "1"})
    record("relayed call is not re-relayed", lg.get("ok") is False and lg.get("error") == "node_offline",
           "error=%s" % lg.get("error"))

    await node.close()


def main():
    tmp = tempfile.mkdtemp(prefix="relay-itest-")
    # A knows B as a peer; both share the token. Distinct hub ids.
    a = _spawn(tmp, "A", A_HTTP, A_TCP, {"HUB_ID": "hub-a", "HUB_PEERS": "http://127.0.0.1:%d" % B_HTTP})
    b = _spawn(tmp, "B", B_HTTP, B_TCP, {"HUB_ID": "hub-b"})
    try:
        for base in (A, B):
            for _ in range(80):
                try:
                    with urllib.request.urlopen(base + "/health", timeout=1) as r:
                        if r.status == 200:
                            break
                except Exception:
                    time.sleep(0.25)
        ok = asyncio.run(checks())
        passed = sum(1 for _, o in _results if o)
        total = len(_results)
        print("\n=== %d/%d relay checks passed ===" % (passed, total))
        rc = 0 if passed == total else 1
    finally:
        for p in (a, b):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
    sys.exit(rc)


if __name__ == "__main__":
    main()
