#!/usr/bin/env bash
# install.sh — set up the Telegram sidecar with uv.
#
# The sidecar depends on `picotty` (the lean client SDK) plus the Telegram extra;
# uv resolves them into telegram-bot/.venv. Its own venv keeps python-telegram-bot
# out of the hub. Run once, then use run.sh (dev) or install-service.sh (systemd).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

echo "==> uv sync (creates telegram-bot/.venv with picotty[telegram])"
cd "$BOT_DIR"
uv sync

mkdir -p "$BOT_DIR/data"

if [[ ! -f "$BOT_DIR/.env" ]]; then
  cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
  chmod 600 "$BOT_DIR/.env"
  echo
  echo "Created $BOT_DIR/.env (chmod 600). Fill it in — by hand, or from the hub"
  echo "dashboard's Telegram settings (which writes this same file):"
  echo "  - TELEGRAM_BOT_TOKEN         (from @BotFather)"
  echo "  - TELEGRAM_ALLOWED_CHAT_IDS  (your numeric chat id)"
  echo "  - SHELL_TOTP_SECRET          (uv run python -c 'import pyotp;print(pyotp.random_base32())')"
fi

echo
echo "Done."
echo "  Run in the foreground:   $BOT_DIR/scripts/run.sh   (or: uv run python -m app)"
echo "  Install as a service:    $BOT_DIR/scripts/install-service.sh"
