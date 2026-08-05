"""Dashboard-side setup for the Telegram sidecar.

The sidecar owns its credentials in a ``.env`` file; this module lets the hub's
settings page write that same file (the doc's convergence: the UI is a
convenience over the one ``.env`` that stays the single source of truth). The hub
never stores the token itself and never echoes it back — it validates the token
against Telegram's ``getMe`` and writes the ``.env`` at 0600.

Requires only httpx (a base ``picotty`` dependency); the TOTP secret is generated
with the stdlib so the hub needs no ``[telegram]`` extra.
"""

from __future__ import annotations

import base64
import os
import secrets
import tempfile
from pathlib import Path

import httpx

# The keys the hub manages in the sidecar .env. Order is the written order.
_MANAGED = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "HUB_BASE_URL",
    "ALERTS_ENABLED",
    "SHELL_ENABLED",
    "SHELL_TOTP_SECRET",
    "SHELL_ARM_WINDOW_S",
]
_SECRET_KEYS = {"TELEGRAM_BOT_TOKEN", "SHELL_TOTP_SECRET"}


def parse_env_file(path: Path) -> dict:
    """Parse a KEY=VALUE .env into a dict. Missing file → empty dict."""
    out: dict = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def write_env_file(path: Path, values: dict) -> None:
    """Atomically write the .env at 0600 (owner-only). Managed keys first in a
    fixed order, then any pre-existing extra keys preserved as-is."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Managed by the PICOTTY hub Telegram settings page. Hand edits to the",
             "# managed keys below may be overwritten on the next save.", ""]
    for key in _MANAGED:
        if key in values and values[key] is not None and values[key] != "":
            lines.append("%s=%s" % (key, values[key]))
    extras = {k: v for k, v in values.items() if k not in _MANAGED}
    if extras:
        lines.append("")
        for key, val in extras.items():
            lines.append("%s=%s" % (key, val))
    body = "\n".join(lines) + "\n"
    # Write to a temp file in the same dir at 0600, then rename over the target so
    # a reader never sees a half-written credential file.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        with _suppress():
            os.unlink(tmp)
        raise


class _suppress:
    def __enter__(self): return self
    def __exit__(self, *exc): return True


async def validate_token(token: str, timeout: float = 8.0) -> tuple[bool, str]:
    """Call Telegram getMe. Returns (ok, bot_username_or_error). Never logs the
    token."""
    if not token:
        return False, "no token"
    url = "https://api.telegram.org/bot%s/getMe" % token
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(url)
        data = r.json()
    except Exception as e:
        return False, "getMe request failed: %s" % type(e).__name__
    if not data.get("ok"):
        return False, "Telegram rejected the token"
    return True, data.get("result", {}).get("username", "?")


def gen_totp_secret() -> str:
    """A fresh base32 TOTP secret (compatible with any authenticator app),
    generated from the stdlib so the hub needs no pyotp."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def otpauth_uri(secret: str, label: str = "PICOTTY shell", issuer: str = "PICOTTY") -> str:
    from urllib.parse import quote
    return "otpauth://totp/%s?secret=%s&issuer=%s" % (quote(label), secret, quote(issuer))


def status(path: Path) -> dict:
    """Non-secret status of the sidecar .env for the settings page."""
    p = Path(path)
    env = parse_env_file(p)
    chat_ids = [c for c in (env.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")) if c.strip()]
    parent_writable = os.access(str(p.parent), os.W_OK) if p.parent.exists() else os.access(
        str(p.parent.parent), os.W_OK)
    return {
        "configured": bool(env.get("TELEGRAM_BOT_TOKEN")),
        "token_present": bool(env.get("TELEGRAM_BOT_TOKEN")),
        "chat_count": len(chat_ids),
        "shell_enabled": env.get("SHELL_ENABLED", "").lower() in ("1", "true", "yes", "on"),
        "totp_present": bool(env.get("SHELL_TOTP_SECRET")),
        "env_path": str(p),
        "exists": p.exists(),
        "writable": parent_writable,
    }
