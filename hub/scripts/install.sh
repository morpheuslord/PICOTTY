#!/usr/bin/env bash
# install.sh — set up the hub with uv (tested target: Pi Zero 2 W, 64-bit Pi OS).
#
# uv manages the interpreter, the virtualenv (hub/.venv), the lockfile, and the
# dependency install. This creates the environment with the `hub` extra (the
# server stack) plus the dev group, and installs the picotty-hub / picotty-sim
# console scripts into .venv/bin. Then use run.sh (dev) or install-service.sh.
#
# Prefer `uv tool install picotty` for a plain deployment (no repo checkout); this
# script is for working from the source tree.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HUB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

echo "==> Fetching the dashboard's terminal libraries (xterm, asciinema)"
bash "$HUB_DIR/src/picotty/static/vendor/fetch-vendor.sh" || {
  echo "   (vendor fetch failed — the console tab needs these; re-run when online)"; }

echo "==> uv sync --extra hub (creates hub/.venv, installs server + dev tools)"
cd "$HUB_DIR"
uv sync --extra hub

echo
echo "Done."
echo "  Run in the foreground:   $HUB_DIR/scripts/run.sh   (or: uv run --extra hub picotty-hub)"
echo "  Install as a service:    $HUB_DIR/scripts/install-service.sh"
echo "  Test with a fake node:   uv run picotty-sim --id demo --token <TOKEN>"
