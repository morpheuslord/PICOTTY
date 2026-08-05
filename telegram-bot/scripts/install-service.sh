#!/usr/bin/env bash
# install-service.sh — install the sidecar as a systemd unit.
#
# Fills the placeholders in swarm-telegram.service and enables it. The unit is a
# clean kill switch: `systemctl stop swarm-telegram` removes the entire external
# surface with zero hub impact.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UNIT_SRC="$SCRIPT_DIR/swarm-telegram.service"
UNIT_DST="/etc/systemd/system/swarm-telegram.service"
RUN_USER="${SUDO_USER:-$USER}"

[[ -x "$BOT_DIR/.venv/bin/python" ]] || { echo "venv missing; run scripts/install.sh first"; exit 1; }
[[ -f "$BOT_DIR/.env" ]] || { echo ".env missing; run scripts/install.sh and fill it in first"; exit 1; }

echo "==> Writing $UNIT_DST (user=$RUN_USER)"
sed -e "s|__USER__|$RUN_USER|g" \
    -e "s|__BOTDIR__|$BOT_DIR|g" \
    "$UNIT_SRC" | sudo tee "$UNIT_DST" >/dev/null

echo "==> Enabling + starting"
sudo systemctl daemon-reload
sudo systemctl enable --now swarm-telegram.service

echo
echo "Done.  Logs:   journalctl -u swarm-telegram -f"
echo "       Stop:   sudo systemctl stop swarm-telegram   (kill switch)"
