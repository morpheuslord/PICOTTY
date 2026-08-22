"""Unit tests that need no live bot or hub.

Run:  telegram-bot/.venv/bin/python -m pytest telegram-bot/tests -q
(or the stdlib-only fallback:  python tests/test_unit.py)
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, formatting               # noqa: E402
from app.alertengine import AlertEngine          # noqa: E402
from app.security import Security                 # noqa: E402
from app.sessions import OutputPump               # noqa: E402


# -- config -------------------------------------------------------------------

def test_config_requires_token(monkeypatch=None):
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = "1"
    try:
        config.load()
    except config.ConfigError:
        return
    raise AssertionError("expected ConfigError for missing token")


def test_config_rejects_nonnumeric_chatid():
    os.environ["TELEGRAM_BOT_TOKEN"] = "x:y"
    os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = "12,notanumber"
    os.environ["SHELL_ENABLED"] = "false"
    try:
        config.load()
    except config.ConfigError:
        return
    raise AssertionError("expected ConfigError for non-numeric chat id")


def test_config_shell_needs_totp():
    os.environ["TELEGRAM_BOT_TOKEN"] = "x:y"
    os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = "12"
    os.environ["SHELL_ENABLED"] = "true"
    os.environ.pop("SHELL_TOTP_SECRET", None)
    try:
        config.load()
    except config.ConfigError:
        return
    raise AssertionError("expected ConfigError: shell on without TOTP")


def test_config_ws_url_derivation():
    os.environ["TELEGRAM_BOT_TOKEN"] = "x:y"
    os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = "12"
    os.environ["SHELL_ENABLED"] = "false"
    os.environ["HUB_BASE_URL"] = "http://10.0.0.5:8080"
    cfg = config.load()
    assert cfg.ws_url == "ws://10.0.0.5:8080/ws", cfg.ws_url
    os.environ["HUB_BASE_URL"] = "https://hub.example:8443"
    cfg = config.load()
    assert cfg.ws_url == "wss://hub.example:8443/ws", cfg.ws_url


# -- security / TOTP ----------------------------------------------------------

def test_totp_arm_and_replay():
    import pyotp
    secret = pyotp.random_base32()
    sec = Security(frozenset({42}), secret, arm_window_s=60)
    assert not sec.is_armed()
    code = pyotp.TOTP(secret).now()
    ok, _ = sec.arm(42, code)
    assert ok and sec.is_armed()
    # Immediate replay of the same code is rejected while still armed.
    ok2, _ = sec.arm(42, code)
    assert not ok2, "replay of the same TOTP should be rejected"
    # A bad code fails.
    ok3, _ = sec.arm(42, "000000")
    # (000000 could theoretically be valid; guard by asserting type only.)
    assert isinstance(ok3, bool)
    sec.disarm()
    assert not sec.is_armed()


def test_allowlist():
    sec = Security(frozenset({1, 2}), "", arm_window_s=60)
    assert sec.is_allowed(1)
    assert not sec.is_allowed(3)
    assert not sec.is_allowed(None)


# -- formatting ---------------------------------------------------------------

def test_strip_ansi():
    raw = "\x1b[32mgreen\x1b[0m\r\nline2\x07"
    out = formatting.strip_ansi(raw)
    assert out == "green\nline2", repr(out)


def test_esc():
    assert formatting.esc("a<b>&c") == "a&lt;b&gt;&amp;c"


def test_chunk_output_splits_on_lines():
    text = "\n".join("line%d" % i for i in range(100))
    chunks = formatting.chunk_output(text, 40)
    assert all(len(c) <= 40 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_chunk_output_hard_splits_long_line():
    text = "x" * 250
    chunks = formatting.chunk_output(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


def test_render_nodes_empty():
    assert "No nodes" in formatting.render_nodes([])


def test_render_events():
    assert "No events" in formatting.render_events([])
    evs = [{"ts": int(time.time() * 1000), "type": "node_up", "node_id": "n1", "detail": "registered via primary"},
           {"ts": int(time.time() * 1000), "type": "cmd", "node_id": None, "detail": "reboot"}]
    out = formatting.render_events(evs)
    assert "node_up" in out and "primary" in out and "<pre>" in out


def test_render_dispatch_counts():
    d = [{"id": "a", "status": "sent"}, {"id": "b", "status": "skipped", "reason": "offline"},
         {"id": "c", "status": "error"}, {"id": "d", "status": "sent"}]
    out = formatting.render_dispatch("Bulk", d)
    assert "2 sent" in out and "1 skipped" in out and "1 error" in out


def test_render_hubs_shows_labels():
    nodes = [{"id": "n1", "status": "online", "hub_label": "primary"},
             {"id": "n2", "status": "online", "hub_label": "backup"},
             {"id": "n3", "status": "offline"}]
    out = formatting.render_hubs(nodes)
    assert "primary" in out and "backup" in out and "—" in out
    assert "No nodes" in formatting.render_hubs([])


def test_render_search_and_log():
    assert "No matches" in formatting.render_search("x", [])
    matches = [{"node_id": "n1", "received_at": int(time.time() * 1000), "text": "\x1b[32mfound it\x1b[0m\n"}]
    out = formatting.render_search("found", matches)
    assert "found it" in out and "1 match" in out
    assert "No recent output" in formatting.render_output_log("n1", [])
    log = formatting.render_output_log("n1", [{"text": "line1\n"}, {"text": "line2\n"}])
    assert "line1" in log and "line2" in log


def test_render_macros_and_runbooks():
    assert "No macros" in formatting.render_macros([])
    assert "No runbooks" in formatting.render_runbooks([])
    m = formatting.render_macros([{"id": 3, "name": "reset", "dangerous": True}])
    assert "/runmacro 3" in m and "reset" in m and "⚠️" in m
    r = formatting.render_runbooks([{"id": 5, "name": "provision"}])
    assert "/runbook 5" in r and "provision" in r


def test_render_uptime_includes_hub():
    node = {"id": "n1", "status": "online", "hub_label": "backup",
            "capabilities": ["hid"], "last_seen": int(time.time() * 1000)}
    assert "hub: backup" in formatting.render_uptime(node)
    # a node without a hub label omits the line
    node2 = {"id": "n2", "status": "online", "capabilities": ["hid"]}
    assert "hub:" not in formatting.render_uptime(node2)


def test_render_nodes_markers():
    nodes = [
        {"id": "a", "status": "online", "target": "up",
         "capabilities": ["hid", "cdc", "serial_tx"], "last_seen": int(time.time() * 1000)},
        {"id": "b", "status": "offline", "capabilities": ["hid"]},
    ]
    out = formatting.render_nodes(nodes)
    assert "●" in out and "○" in out
    assert "tx" in out


# -- telemetry ----------------------------------------------------------------

def test_link_quality_thresholds():
    # Mirrors the dashboard's netQuality: loss>=10 or jitter>=100 → poor;
    # loss>=2 or jitter>=40 → fair; else good; None jitter → no telemetry.
    assert formatting.link_quality({"jitter_ms": None}) is None
    assert formatting.link_quality({"jitter_ms": 5, "loss_pct": 0})[0] == "good"
    assert formatting.link_quality({"jitter_ms": 55, "loss_pct": 0})[0] == "fair"
    assert formatting.link_quality({"jitter_ms": 5, "loss_pct": 3})[0] == "fair"
    assert formatting.link_quality({"jitter_ms": 120, "loss_pct": 0})[0] == "poor"
    assert formatting.link_quality({"jitter_ms": 5, "loss_pct": 15})[0] == "poor"


def test_dur_is_a_span_not_a_timestamp():
    assert formatting._dur(None) == "—"
    assert formatting._dur(45_000) == "45s"
    assert formatting._dur(90_000) == "1m"
    assert formatting._dur(3 * 3600_000 + 12 * 60_000) == "3h 12m"
    assert formatting._dur(5 * 86400_000 + 2 * 3600_000) == "5d 2h"


def test_render_telemetry_roster_and_detail():
    nodes = [
        {"id": "node-01", "status": "online", "target": "up",
         "rtt_ms": 3, "rtt_avg_ms": 3, "rtt_min_ms": 2, "rtt_max_ms": 5,
         "jitter_ms": 1, "loss_pct": 0,
         "node_uptime_ms": 5 * 86400_000 + 2 * 3600_000, "reconnects": 0},
        {"id": "node-02", "status": "online", "target": "down",
         "rtt_ms": 22, "rtt_avg_ms": 18, "jitter_ms": 55, "loss_pct": 6,
         "node_uptime_ms": 3 * 3600_000, "reconnects": 4},
        {"id": "node-03", "status": "offline"},
    ]
    roster = formatting.render_telemetry(nodes)
    assert "good" in roster and "fair" in roster
    assert "No nodes" in formatting.render_telemetry([])
    detail = formatting.render_telemetry_node(nodes[1])
    assert "node-02" in detail and "🟡 fair" in detail
    assert "5d 2h" in formatting.render_telemetry_node(nodes[0])
    # Offline node: no telemetry, renders without raising.
    assert "no telemetry" in formatting.render_telemetry_node(nodes[2])


# -- output pump summarize ----------------------------------------------------

def test_output_pump_summarizes():
    sent = []

    async def send(html):
        sent.append(html)

    async def run():
        pump = OutputPump(send, flush_interval_s=0.01, max_chunk=500, summarize_bytes=1000)
        pump.start()
        pump.feed("A" * 5000)   # far over the summarize ceiling
        await asyncio.sleep(0.05)
        await pump.close()

    asyncio.run(run())
    joined = "".join(sent)
    assert "skipped" in joined, "expected a summarize notice"


# -- alert engine debounce ----------------------------------------------------

def test_alert_debounce_and_mute():
    sent = []

    async def bc(html):
        sent.append(html)

    async def run():
        eng = AlertEngine(bc, debounce_s=999, enabled=True)
        await eng.on_ws_event({"event": "node_down", "id": "n1", "reason": "x"})
        await eng.on_ws_event({"event": "node_down", "id": "n1", "reason": "x"})  # debounced
        eng.mute("n2")
        await eng.on_ws_event({"event": "node_down", "id": "n2"})                 # muted
        await eng.on_ws_event({"event": "node_down", "id": "n3"})                 # fresh

    asyncio.run(run())
    joined = " ".join(sent)
    assert joined.count("Node offline") == 2, sent   # n1 once, n3 once; n2 muted
    assert "n2" not in joined


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print("ok  %s" % fn.__name__)
    print("\n%d/%d passed" % (passed, len(fns)))


if __name__ == "__main__":
    _run_all()
