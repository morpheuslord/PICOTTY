#!/usr/bin/env bash
# install.sh — install hub dependencies on Raspberry Pi OS (tested target: Pi Zero 2 W).
#
# Installs system packages, creates a Python venv in hub/.venv, and installs the
# Python requirements. Run this once; then use run.sh (dev) or install-service.sh
# (systemd) to start the hub.
#
# Tip: use the 64-bit Raspberry Pi OS on the Zero 2 W — the dependency wheels
# (uvicorn/uvloop/httptools, pydantic-core) install far more smoothly there.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HUB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "==> Installing system packages (needs sudo)"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip python3-dev build-essential libffi-dev

echo "==> Creating virtualenv at $HUB_DIR/.venv"
python3 -m venv "$HUB_DIR/.venv"
"$HUB_DIR/.venv/bin/pip" install --upgrade pip wheel

echo "==> Installing Python requirements"
"$HUB_DIR/.venv/bin/pip" install -r "$HUB_DIR/requirements.txt"

mkdir -p "$HUB_DIR/data"

echo
echo "Done."
echo "  Run in the foreground:   $HUB_DIR/scripts/run.sh"
echo "  Install as a service:    $HUB_DIR/scripts/install-service.sh"
