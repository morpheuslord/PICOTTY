#!/usr/bin/env python3
"""package_tester.py — a live smoke test for a running PICOTTY hub.

Pull the `picotty` package onto the hub host (e.g. a Pi Zero 2 W), start it, then
run this against the live REST + WebSocket API to confirm the system works end to
end before you publish. It performs READ-ONLY checks by default; add --write to
also type a single harmless Enter into one online node's serial console.

    python3 tools/package_tester.py --hub http://127.0.0.1:8080
    python3 tools/package_tester.py --hub http://hub:8080 --node Node-Main --write

It prefers the packaged `picotty` SDK — importing it verifies the install, and its
`websockets` dependency drives the live-event check. Where `picotty` is not
installed it falls back to a stdlib-only REST smoke test (no third-party imports)
and skips the WebSocket step with a note, so the script also runs from a bare
laptop.

Exit code 0 if every check passed (warnings don't fail the run), 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# status buckets
_PASS, _FAIL, _WARN = "PASS", "FAIL", "WARN"
_results: list[tuple[str, str]] = []


def record(name: str, status: str, note: str = "") -> None:
    _results.append((name, status))
    print("  [%s] %-30s %s" % (status, name, note))


def _api(base: str, method: str, path: str, body=None, timeout: float = 8.0):
    """One REST call. Returns (status_code, parsed_json_or_None). Raises only on
    a transport error (hub unreachable), which the caller treats as fatal."""
    url = base.rstrip("/") + "/api" + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "null")
        except Exception:
            return e.code, None


def _ws_url(base: str) -> str:
    b = base.rstrip("/")
    if b.startswith("https://"):
        return "wss://" + b[len("https://"):] + "/ws"
    if b.startswith("http://"):
        return "ws://" + b[len("http://"):] + "/ws"
    return "ws://" + b + "/ws"


def check_package() -> None:
    """Verify the installed picotty package imports and reports its versions."""
    try:
        import picotty
        from picotty import protocol
        record("package import", _PASS,
               "picotty %s · protocol v%s" % (picotty.__version__, protocol.PROTOCOL_VERSION))
    except Exception as e:
        record("package import", _WARN,
               "picotty not importable — REST-only fallback (%s)" % type(e).__name__)


def check_rest(base: str, timeout: float) -> dict:
    """Core REST smoke test. Returns a small context dict for later steps."""
    ctx: dict = {"online_node": None, "node_caps": []}

    st, body = _api(base, "GET", "/health", timeout=timeout)
    ok = st == 200 and isinstance(body, dict) and body.get("ok") is True
    record("GET /health", _PASS if ok else _FAIL,
           "v%s · %s/%s nodes online · web:%s swarm:%s" % (
               body.get("version"), body.get("nodes_online"), body.get("nodes_total"),
               body.get("web_port"), body.get("swarm_port")) if ok else "unexpected: %s" % body)

    st, body = _api(base, "GET", "/stats", timeout=timeout)
    ok = st == 200 and isinstance(body, dict) and "loop_lag_ms" in body
    record("GET /stats", _PASS if ok else _FAIL,
           "loop lag %sms · ws clients %s" % (body.get("loop_lag_ms"), body.get("ws_clients")) if ok else str(body))

    st, body = _api(base, "GET", "/nodes", timeout=timeout)
    nodes = body.get("nodes", []) if isinstance(body, dict) else []
    ok = st == 200 and isinstance(body, dict) and body.get("ok") is True
    online = [n for n in nodes if n.get("status") == "online"]
    record("GET /nodes", _PASS if ok else _FAIL, "%d node(s), %d online" % (len(nodes), len(online)))
    if online:
        ctx["online_node"] = online[0]["id"]
        ctx["node_caps"] = online[0].get("capabilities") or []

    st, body = _api(base, "GET", "/events?limit=5", timeout=timeout)
    ok = st == 200 and isinstance(body, dict) and isinstance(body.get("events"), list)
    record("GET /events", _PASS if ok else _FAIL,
           "%d recent event(s)" % len(body.get("events", [])) if ok else str(body))

    st, body = _api(base, "GET", "/settings", timeout=timeout)
    ok = st == 200 and isinstance(body, dict) and isinstance(body.get("settings"), dict)
    record("GET /settings", _PASS if ok else _FAIL,
           "%d setting(s)" % len(body.get("settings", {})) if ok else str(body))

    st, body = _api(base, "GET", "/telegram", timeout=timeout)
    tg = body.get("telegram", {}) if isinstance(body, dict) else {}
    ok = st == 200 and isinstance(body, dict) and "configured" in tg
    record("GET /telegram", _PASS if ok else _FAIL,
           "sidecar %s" % ("configured" if tg.get("configured") else "not configured") if ok else str(body))

    return ctx


def check_node_ops(base: str, node: str, caps, do_write: bool, timeout: float) -> None:
    st, body = _api(base, "POST", "/nodes/%s/ping" % node, timeout=timeout)
    ok = st == 200 and isinstance(body, dict) and body.get("ok") is True
    record("POST ping (%s)" % node, _PASS if ok else _FAIL,
           "rtt %sms" % body.get("rtt_ms") if ok else str(body))

    st, body = _api(base, "POST", "/nodes/%s/read" % node, timeout=timeout)
    ok = st == 200 and isinstance(body, dict) and body.get("ok") is True
    record("POST read serial (%s)" % node, _PASS if ok else _FAIL, "" if ok else str(body))

    if not do_write:
        record("serial write", _WARN, "skipped — pass --write to type an Enter into %s" % node)
        return
    if "serial_tx" not in caps:
        record("serial write", _WARN, "%s has no serial_tx capability; skipped" % node)
        return
    # A single carriage return: submits an empty line at the getty (a fresh
    # prompt) — the least intrusive thing that still proves the send path works.
    st, body = _api(base, "POST", "/nodes/%s/cmd" % node,
                    {"type": "send", "data": "\r"}, timeout=timeout)
    ok = st == 200 and isinstance(body, dict) and body.get("ok", True) is not False
    record("POST serial write (%s)" % node, _PASS if ok else _FAIL,
           "sent Enter" if ok else str(body))


def check_ws(base: str, timeout: float) -> None:
    try:
        import asyncio
        import websockets
    except Exception:
        record("WebSocket /ws", _WARN, "websockets not installed (install picotty); skipped")
        return

    async def _run() -> tuple[bool, str]:
        url = _ws_url(base)
        try:
            async with websockets.connect(url, ping_interval=None, open_timeout=timeout) as s:
                await s.send(json.dumps({"type": "ping"}))
                raw = await asyncio.wait_for(s.recv(), timeout=timeout)
                msg = json.loads(raw)
                return True, msg.get("event") or "message"
        except Exception as e:
            return False, type(e).__name__

    try:
        ok, note = asyncio.run(_run())
    except Exception as e:
        ok, note = False, type(e).__name__
    record("WebSocket /ws", _PASS if ok else _FAIL,
           "live feed replied: %s" % note if ok else "no reply (%s)" % note)


def main() -> int:
    ap = argparse.ArgumentParser(description="Live smoke test for a running PICOTTY hub.")
    ap.add_argument("--hub", default=os.environ.get("PICOTTY_HUB", "http://127.0.0.1:8080"),
                    help="hub base URL (default: $PICOTTY_HUB or http://127.0.0.1:8080)")
    ap.add_argument("--node", help="node id for the node-op checks (default: first online node)")
    ap.add_argument("--write", action="store_true",
                    help="also type a single Enter into the node's serial console (needs serial_tx)")
    ap.add_argument("--no-ws", action="store_true", help="skip the WebSocket check")
    ap.add_argument("--timeout", type=float, default=8.0, help="per-request timeout seconds")
    args = ap.parse_args()

    print("PICOTTY package tester → %s" % args.hub)
    print("-" * 60)

    check_package()

    try:
        ctx = check_rest(args.hub, args.timeout)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        record("hub reachable", _FAIL, "cannot reach %s (%s)" % (args.hub, type(e).__name__))
        _summary()
        return 1

    if not args.no_ws:
        check_ws(args.hub, args.timeout)

    node = args.node or ctx.get("online_node")
    caps = ctx.get("node_caps", [])
    if args.node and args.node != ctx.get("online_node"):
        caps = []  # unknown for an explicitly-named node; write-gate stays conservative
    if node:
        check_node_ops(args.hub, node, caps, args.write, args.timeout)
    else:
        record("node ops", _WARN, "no online node — connect one to exercise node commands")

    return _summary()


def _summary() -> int:
    print("-" * 60)
    passed = sum(1 for _, s in _results if s == _PASS)
    failed = sum(1 for _, s in _results if s == _FAIL)
    warned = sum(1 for _, s in _results if s == _WARN)
    print("%d passed · %d failed · %d warning(s)" % (passed, failed, warned))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
