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

# Directory of the hub package's parent (the `hub/` folder).
BASE_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


@dataclass(frozen=True)
class ProcessConfig:
    """Fixed for the lifetime of the process. Sourced from the environment."""

    tcp_host: str = os.environ.get("HUB_TCP_HOST", "0.0.0.0")
    tcp_port: int = _env_int("HUB_TCP_PORT", 9000)
    http_host: str = os.environ.get("HUB_HTTP_HOST", "0.0.0.0")
    http_port: int = _env_int("HUB_HTTP_PORT", 8080)
    db_path: Path = _env_path("HUB_DB_PATH", BASE_DIR / "data" / "hub.db")
    static_dir: Path = _env_path("HUB_STATIC_DIR", BASE_DIR / "static")

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
}

# Settings whose values are booleans, so the settings API can coerce correctly.
BOOL_SETTINGS = {"require_confirm_dangerous", "auth_enabled"}
INT_SETTINGS = {
    "heartbeat_interval_ms",
    "stale_timeout_ms",
    "warn_timeout_ms",
    "output_retention_days",
    "event_retention_days",
}
