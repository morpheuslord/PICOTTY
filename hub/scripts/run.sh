#!/usr/bin/env bash
# run.sh — start the hub in the foreground (development / manual runs).
#
# Loads private/hub-token.txt (if present) so the hub adopts your shared node
# token on first start, then runs the app from its venv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HUB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJ_DIR="$(cd "$HUB_DIR/.." && pwd)"
TOKEN_FILE="$PROJ_DIR/private/hub-token.txt"

[[ -x "$HUB_DIR/.venv/bin/python" ]] || { echo "venv missing; run scripts/install.sh first"; exit 1; }

if [[ -f "$TOKEN_FILE" ]]; then
  set -a; . "$TOKEN_FILE"; set +a
fi

export HUB_DB_PATH="${HUB_DB_PATH:-$HUB_DIR/data/hub.db}"
export HUB_STATIC_DIR="${HUB_STATIC_DIR:-$HUB_DIR/static}"

cd "$HUB_DIR"
exec "$HUB_DIR/.venv/bin/python" -m app.main
