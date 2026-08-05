"""Sidecar configuration, sourced entirely from the environment (.env file).

This process is a remote-access surface bridged to a third-party network, so the
whole configuration is a handful of explicit environment variables — no defaults
that silently open the shell tier, no credentials in the repo. The token and the
chat allowlist are required; everything else has a safe default.

The .env lives outside the repo working tree (or is gitignored) at chmod 600,
owned by the sidecar's user. It is the single source of truth for credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Raised on a missing or malformed required setting so the sidecar refuses
    to start half-configured rather than run with a broken surface."""


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _chat_ids(name: str) -> frozenset[int]:
    """Parse a comma-separated allowlist of numeric chat IDs. Non-numeric
    entries are rejected loudly — a typo here must not silently widen access."""
    raw = _str(name)
    if not raw:
        return frozenset()
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            raise ConfigError(
                "TELEGRAM_ALLOWED_CHAT_IDS contains a non-numeric entry: %r" % part)
    return frozenset(out)


@dataclass(frozen=True)
class Config:
    # -- Telegram -------------------------------------------------------------
    bot_token: str
    allowed_chat_ids: frozenset[int]

    # -- Hub REST + WebSocket -------------------------------------------------
    hub_base_url: str = "http://127.0.0.1:8080"
    hub_ws_url: str = ""            # derived from hub_base_url if blank
    hub_auth_password: str = ""     # only if the hub has auth_enabled
    hub_timeout_s: float = 10.0

    # -- Tier 2: alerts -------------------------------------------------------
    alerts_enabled: bool = True
    alert_debounce_s: int = 180     # per (kind, node) flap suppression
    events_poll_interval_s: int = 15

    # -- Tier 3: terminal bridge / break-glass --------------------------------
    shell_enabled: bool = True
    shell_totp_secret: str = ""     # base32; required to arm the shell
    shell_arm_window_s: int = 3600  # armed duration after a good /arm
    shell_idle_timeout_s: int = 300  # auto-close an idle session
    output_flush_interval_s: float = 1.6   # coalesce window for relayed output
    output_max_chunk: int = 3500    # < Telegram's 4096, leaves room for wrappers
    output_summarize_bytes: int = 24_000   # per-window ceiling before we summarize

    # -- Audit ----------------------------------------------------------------
    audit_log_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "telegram-audit.jsonl")

    # -- Hot reload -----------------------------------------------------------
    # The .env this sidecar was configured from. The hub's Telegram settings page
    # writes this same file; the sidecar watches it and hot-reloads the allowlist,
    # TOTP secret, and shell/alert settings on change (a token change still needs
    # a restart, and is logged as such).
    env_file: Path = field(default_factory=lambda: BASE_DIR / ".env")

    @property
    def ws_url(self) -> str:
        if self.hub_ws_url:
            return self.hub_ws_url
        base = self.hub_base_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):] + "/ws"
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):] + "/ws"
        return "ws://" + base + "/ws"


def load() -> Config:
    """Build the Config from the environment, or raise ConfigError."""
    token = _str("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is required")
    chat_ids = _chat_ids("TELEGRAM_ALLOWED_CHAT_IDS")
    if not chat_ids:
        raise ConfigError(
            "TELEGRAM_ALLOWED_CHAT_IDS is required (at least one numeric chat id)")

    shell_enabled = _bool("SHELL_ENABLED", True)
    totp = _str("SHELL_TOTP_SECRET")
    if shell_enabled and not totp:
        # The shell tier is state-changing remote access; it must not run without
        # its second factor. Better to disable it explicitly than arm blindly.
        raise ConfigError(
            "SHELL_ENABLED is on but SHELL_TOTP_SECRET is empty — set a base32 "
            "TOTP secret, or set SHELL_ENABLED=false to run stats+alerts only")

    audit_path = _str("AUDIT_LOG_PATH")
    cfg = Config(
        bot_token=token,
        allowed_chat_ids=chat_ids,
        hub_base_url=_str("HUB_BASE_URL", "http://127.0.0.1:8080"),
        hub_ws_url=_str("HUB_WS_URL"),
        hub_auth_password=_str("HUB_AUTH_PASSWORD"),
        hub_timeout_s=float(_int("HUB_TIMEOUT_S", 10)),
        alerts_enabled=_bool("ALERTS_ENABLED", True),
        alert_debounce_s=_int("ALERT_DEBOUNCE_S", 180),
        events_poll_interval_s=_int("EVENTS_POLL_INTERVAL_S", 15),
        shell_enabled=shell_enabled,
        shell_totp_secret=totp,
        shell_arm_window_s=_int("SHELL_ARM_WINDOW_S", 3600),
        shell_idle_timeout_s=_int("SHELL_IDLE_TIMEOUT_S", 300),
        output_flush_interval_s=float(_int("OUTPUT_FLUSH_INTERVAL_MS", 1600)) / 1000.0,
        output_max_chunk=_int("OUTPUT_MAX_CHUNK", 3500),
        output_summarize_bytes=_int("OUTPUT_SUMMARIZE_BYTES", 24_000),
        audit_log_path=Path(audit_path).expanduser() if audit_path else BASE_DIR / "data" / "telegram-audit.jsonl",
        env_file=Path(_str("TELEGRAM_ENV_FILE")).expanduser() if _str("TELEGRAM_ENV_FILE") else BASE_DIR / ".env",
    )
    return cfg

