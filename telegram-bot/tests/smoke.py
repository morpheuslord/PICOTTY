"""Wiring smoke test: build the whole application with dummy credentials and
confirm every handler registers and the relay/session objects construct. Needs
the sidecar dependencies installed (python-telegram-bot etc.) but talks to no
network — run_polling is never called.

Run:  telegram-bot/.venv/bin/python telegram-bot/tests/smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Minimal viable env for build_application (token format must contain a colon).
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:AA-fake-token-for-smoke-only")
os.environ.setdefault("TELEGRAM_ALLOWED_CHAT_IDS", "42")
os.environ.setdefault("SHELL_ENABLED", "true")
try:
    import pyotp
    os.environ.setdefault("SHELL_TOTP_SECRET", pyotp.random_base32())
except ImportError:
    os.environ["SHELL_ENABLED"] = "false"

from app import config                    # noqa: E402
from app.bot import build_application     # noqa: E402

cfg = config.load()
app = build_application(cfg)

handlers = [h for group in app.handlers.values() for h in group]
names = set()
for h in handlers:
    cmds = getattr(h, "commands", None)
    if cmds:
        names.update(cmds)

expected = {"status", "nodes", "uptime", "arm", "disarm", "shell", "end",
            "reboot", "sysrq", "ctrlc", "esc", "mute"}
missing = expected - names
assert not missing, "missing handlers: %s" % missing
assert app.post_init is not None and app.post_shutdown is not None

print("smoke OK — %d handlers, commands cover: %s" % (len(handlers), sorted(names)))
