#!/usr/bin/env bash
# run.sh — start the Telegram sidecar in the foreground (dev / manual runs).
# Loads the shared credentials file (the same one the dashboard writes), then
# runs the app from its own venv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# The dashboard's default target; a legacy in-repo .env still works as a fallback.
ENV_FILE="${TELEGRAM_ENV_FILE:-$HOME/.config/picotty/telegram.env}"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$BOT_DIR/.env"

command -v uv >/dev/null 2>&1 || { echo "uv not found; run scripts/install.sh first"; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "no credentials file — configure via the dashboard (Settings -> Telegram) or run scripts/install.sh"; exit 1; }

# Export so the sidecar's hot-reload watches the same file it was loaded from.
export TELEGRAM_ENV_FILE="$ENV_FILE"
set -a; . "$ENV_FILE"; set +a

cd "$BOT_DIR"
exec uv run python -m app
