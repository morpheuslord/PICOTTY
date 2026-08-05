#!/usr/bin/env bash
# run.sh — start the hub in the foreground (development / manual runs).
#
# Loads private/hub-token.txt (if present) so the hub adopts your shared node
# token on first start, then runs the packaged picotty-hub console script inside
# uv's managed environment. Runtime state (the SQLite DB) defaults to a platform
# data dir (~/.local/share/picotty); override with HUB_DB_PATH.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HUB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJ_DIR="$(cd "$HUB_DIR/.." && pwd)"
TOKEN_FILE="$PROJ_DIR/private/hub-token.txt"

command -v uv >/dev/null 2>&1 || { echo "uv not found; run scripts/install.sh first"; exit 1; }

if [[ -f "$TOKEN_FILE" ]]; then
  set -a; . "$TOKEN_FILE"; set +a
fi

cd "$HUB_DIR"
exec uv run --extra hub picotty-hub
