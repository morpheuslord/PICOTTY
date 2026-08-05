#!/usr/bin/env bash
# install.sh — set up the Telegram sidecar with uv, sharing ONE credentials file
# with the hub.
#
# The hub dashboard's "Settings -> Telegram" page writes ~/.config/picotty/
# telegram.env (its default TELEGRAM_ENV_PATH). This script points the sidecar at
# that same file, so the dashboard writes it and the sidecar reads + hot-reloads
# it — no path juggling, no hub restart. An older in-repo telegram-bot/.env is
# migrated automatically.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${TELEGRAM_ENV_FILE:-$HOME/.config/picotty/telegram.env}"
LEGACY_ENV="$BOT_DIR/.env"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install it: https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

echo "==> uv sync (creates telegram-bot/.venv with picotty[telegram])"
cd "$BOT_DIR"
uv sync

mkdir -p "$BOT_DIR/data" "$(dirname "$ENV_FILE")"

# One credentials file, shared with the dashboard. Prefer whatever already
# exists; else migrate a legacy in-repo .env; else lay down the template.
if [[ -f "$ENV_FILE" ]]; then
  echo "==> Using existing credentials at $ENV_FILE"
elif [[ -f "$LEGACY_ENV" ]]; then
  echo "==> Migrating $LEGACY_ENV -> $ENV_FILE"
  install -m 600 "$LEGACY_ENV" "$ENV_FILE"
else
  install -m 600 "$BOT_DIR/.env.example" "$ENV_FILE"
  echo "==> Created $ENV_FILE (chmod 600). Fill it in — by hand, or from the hub"
  echo "    dashboard's Telegram settings (which writes this same file):"
  echo "      - TELEGRAM_BOT_TOKEN         (from @BotFather)"
  echo "      - TELEGRAM_ALLOWED_CHAT_IDS  (your numeric chat id)"
  echo "      - SHELL_TOTP_SECRET          (dashboard 'Generate', or pyotp.random_base32())"
fi
chmod 600 "$ENV_FILE" 2>/dev/null || true

echo
echo "Done. The hub and this sidecar now share $ENV_FILE."
echo "  Run in the foreground:   $BOT_DIR/scripts/run.sh"
echo "  Install as a service:    $BOT_DIR/scripts/install-service.sh"
