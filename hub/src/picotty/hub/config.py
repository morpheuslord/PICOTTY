"""Hub configuration.

Two kinds of config live here. Process-level settings (ports, paths) come from
the environment and are fixed for a run. Operator-tunable settings (heartbeat
interval, retention, confirm-dangerous) have defaults here but are overlaid from
the ``settings`` table at startup and can change live via the settings API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# The installed package root (…/site-packages/picotty, or src/picotty from a
# source tree). Static assets ship inside the package alongside the code.
BASE_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _packaged_static() -> Path:
    """The dashboard's static assets, shipped inside the wheel. Resolved via
    importlib.resources so it works from an installed package, not just a source
    checkout. Overridable with HUB_STATIC_DIR for dashboard development."""
    try:
        from importlib.resources import files
        return Path(str(files("picotty") / "static"))
    except Exception:
        return BASE_DIR / "static"


def _default_db_path() -> Path:
    """Runtime state lives OUT of the source tree (an installed package must not
    write into site-packages). Precedence: HUB_DB_PATH > $XDG_DATA_HOME/picotty >
    ~/.local/share/picotty. A systemd unit can point HUB_DB_PATH at its
    StateDirectory (/var/lib/picotty)."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "picotty" / "hub.db"


@dataclass(frozen=True)
class ProcessConfig:
    """Fixed for the lifetime of the process. Sourced from the environment."""

    tcp_host: str = os.environ.get("HUB_TCP_HOST", "0.0.0.0")
    tcp_port: int = _env_int("HUB_TCP_PORT", 9000)
    http_host: str = os.environ.get("HUB_HTTP_HOST", "0.0.0.0")
    http_port: int = _env_int("HUB_HTTP_PORT", 8080)
    db_path: Path = _env_path("HUB_DB_PATH", _default_db_path())
    static_dir: Path = _env_path("HUB_STATIC_DIR", _packaged_static())

    # Bind host for the raw serial bridge listeners. Keep it on the management
    # interface; default binds all interfaces like the other faces, fine on an
    # isolated VLAN. Set HUB_BRIDGE_HOST to a specific IP on a multi-homed hub.
    bridge_host: str = os.environ.get("HUB_BRIDGE_HOST", "0.0.0.0")

    # Where the dashboard's Telegram settings page writes the sidecar's .env. It
    # is the sidecar's single source of truth; point both here and the sidecar's
    # TELEGRAM_ENV_FILE at the same path (co-located hub + sidecar).
    telegram_env_path: Path = _env_path(
        "TELEGRAM_ENV_PATH", Path.home() / ".config" / "picotty" / "telegram.env")

    # Where the sidecar's install scripts live, for the dashboard's one-click
    # "Install sidecar" button. Defaults to ../telegram-bot relative to the hub's
    # working directory (the repo checkout); override for other layouts.
    telegram_bot_dir: Path = _env_path("TELEGRAM_BOT_DIR", Path.cwd().parent / "telegram-bot")

    # How often the liveness sweep runs and how often batched output is flushed.
    sweep_interval_ms: int = _env_int("HUB_SWEEP_INTERVAL_MS", 3000)
    output_flush_interval_ms: int = _env_int("HUB_OUTPUT_FLUSH_INTERVAL_MS", 500)
    output_flush_max_rows: int = _env_int("HUB_OUTPUT_FLUSH_MAX_ROWS", 200)
    # Periodic hub_stats broadcast interval.
    stats_interval_ms: int = _env_int("HUB_STATS_INTERVAL_MS", 5000)
    # Per-WebSocket outbound queue depth before drop-oldest kicks in.
    ws_queue_max: int = _env_int("HUB_WS_QUEUE_MAX", 512)


PROCESS = ProcessConfig()


# Operator-tunable defaults. Persisted in and re-read from the settings table.
DEFAULT_SETTINGS: dict[str, object] = {
    "heartbeat_interval_ms": 5000,
    "stale_timeout_ms": 15000,
    # A node is flagged "stale" in the UI before it fully flips offline.
    "warn_timeout_ms": 8000,
    "output_retention_days": 30,
    "event_retention_days": 90,
    "require_confirm_dangerous": True,
    "auth_enabled": False,
    # Raw serial bridge (phase 8): expose each assigned node's serial as a TCP
    # port for minicom/PuTTY/etc. Off by default; the port map lives in the
    # serial_bridge table. Bind host is process-level (HUB_BRIDGE_HOST).
    "serial_bridge_enabled": False,
    # Alerting (phase 11): outbound webhook/ntfy on notable events. Off by default.
    "alerts_enabled": False,
    "alerts_webhook_url": "",
    "alerts_ntfy_url": "",
}

# Settings whose values are booleans, so the settings API can coerce correctly.
BOOL_SETTINGS = {"require_confirm_dangerous", "auth_enabled", "serial_bridge_enabled",
                 "alerts_enabled"}
INT_SETTINGS = {
    "heartbeat_interval_ms",
    "stale_timeout_ms",
    "warn_timeout_ms",
    "output_retention_days",
    "event_retention_days",
}
