#!/usr/bin/env bash
# install-service.sh — install, enable and start the hub as a systemd service.
#
# Renders swarm-hub.service with the real user and paths (handling paths with
# spaces), installs it to /etc/systemd/system, then enables + starts it. On its
# first start the hub adopts the shared token from private/hub-token.txt.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HUB_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJ_DIR="$(cd "$HUB_DIR/.." && pwd)"
TOKEN_FILE="$PROJ_DIR/private/hub-token.txt"
UNIT_SRC="$SCRIPT_DIR/swarm-hub.service"
UNIT_DST=/etc/systemd/system/swarm-hub.service
RUN_USER="${SUDO_USER:-$USER}"

[[ -x "$HUB_DIR/.venv/bin/python" ]] || { echo "venv missing; run scripts/install.sh first"; exit 1; }

echo "==> Rendering unit (user=$RUN_USER, dir=$HUB_DIR)"
tmp="$(mktemp)"
sed -e "s#__USER__#${RUN_USER}#g" \
    -e "s#__HUBDIR__#${HUB_DIR}#g" \
    -e "s#__TOKENFILE__#${TOKEN_FILE}#g" \
    "$UNIT_SRC" > "$tmp"

echo "==> Installing $UNIT_DST (needs sudo)"
sudo cp "$tmp" "$UNIT_DST"
rm -f "$tmp"
sudo systemctl daemon-reload
sudo systemctl enable --now swarm-hub.service

echo
echo "==> Status:"
sudo systemctl --no-pager --full status swarm-hub.service || true
echo
echo "Follow logs:  journalctl -u swarm-hub -f"
echo "Dashboard:    http://<hub-ip>:8080"
echo "Stop/disable: sudo systemctl disable --now swarm-hub"
