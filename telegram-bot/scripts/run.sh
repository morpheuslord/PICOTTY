#!/usr/bin/env bash
# run.sh — start the Telegram sidecar in the foreground (dev / manual runs).
# Loads telegram-bot/.env, then runs the app from its own venv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$BOT_DIR/.env"

command -v uv >/dev/null 2>&1 || { echo "uv not found; run scripts/install.sh first"; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo ".env missing; copy .env.example to .env and fill it in"; exit 1; }

set -a; . "$ENV_FILE"; set +a

cd "$BOT_DIR"
exec uv run python -m app
