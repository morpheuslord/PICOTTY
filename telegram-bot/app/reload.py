"""Hot-reload the sidecar's .env.

The hub's Telegram settings page writes the same .env this sidecar started from.
Rather than force a restart on every change, we watch the file's mtime and apply
what can safely change at runtime: the chat allowlist, the TOTP secret, the arm
window, and the alert enable/debounce. A bot-token change cannot be hot-swapped
(python-telegram-bot binds the token at build time), so we log that a restart is
required instead of silently ignoring it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("picotty-telegram.reload")

_POLL_S = 5.0


def _parse_env(path: Path) -> dict:
    out: dict = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _chat_ids(raw: str) -> frozenset[int]:
    out = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part:
            try:
                out.add(int(part))
            except ValueError:
                pass
    return frozenset(out)


def _truthy(v: str) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


class Reloader:
    def __init__(self, *, env_file: Path, security, alerts, current_token: str):
        self._path = Path(env_file)
        self._security = security
        self._alerts = alerts
        self._token = current_token
        self._mtime: Optional[float] = self._current_mtime()
        self._task: Optional[asyncio.Task] = None

    def _current_mtime(self) -> Optional[float]:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(_POLL_S)
                mtime = self._current_mtime()
                if mtime is None or mtime == self._mtime:
                    continue
                self._mtime = mtime
                self._apply(_parse_env(self._path))
        except asyncio.CancelledError:
            pass

    def _apply(self, env: dict) -> None:
        allowed = _chat_ids(env.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
        totp = env.get("SHELL_TOTP_SECRET", "")
        try:
            arm_window = int(env.get("SHELL_ARM_WINDOW_S", "3600"))
        except ValueError:
            arm_window = 3600
        if allowed:
            self._security.update(allowed, totp, arm_window)
        self._alerts.set_enabled(_truthy(env.get("ALERTS_ENABLED", "true")))
        try:
            self._alerts.set_debounce(int(env.get("ALERT_DEBOUNCE_S", "180")))
        except ValueError:
            pass
        new_token = env.get("TELEGRAM_BOT_TOKEN", "")
        if new_token and new_token != self._token:
            log.warning("bot token changed in %s — restart the sidecar to apply "
                        "(allowlist and shell settings were reloaded live)", self._path)
        log.info("reloaded config from %s: %d allowed chat(s), shell_totp=%s",
                 self._path, len(allowed), "set" if totp else "unset")
