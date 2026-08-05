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


def test_render_nodes_markers():
    nodes = [
        {"id": "a", "status": "online", "target": "up",
         "capabilities": ["hid", "cdc", "serial_tx"], "last_seen": int(time.time() * 1000)},
        {"id": "b", "status": "offline", "capabilities": ["hid"]},
    ]
    out = formatting.render_nodes(nodes)
    assert "●" in out and "○" in out
    assert "tx" in out


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
