#!/usr/bin/env bash
# update.sh — pull the latest PICOTTY and bring this host up to it.
#
# One command to update a source-tree deployment: refresh the git checkout, sync
# each component's uv environment, and restart its systemd service so the new
# code is live. Safe to re-run.
#
# It only touches what is actually deployed here. A component (the hub, the
# Telegram sidecar) counts as deployed if its uv venv exists OR its systemd unit
# is installed; anything not deployed is skipped. So the same script works on a
# hub-only box, a sidecar-only box, or one running both.
#
# NOT handled here: node firmware. Push firmware over-the-air from the dashboard
# (Settings -> Firmware / OTA) — it never comes down a shell on the hub.
#
# Usage:
#   ./update.sh              pull, sync dependencies, restart services
#   ./update.sh --no-pull    skip the git pull; sync + restart the current tree
#   ./update.sh --no-restart update code + dependencies but leave services running
#   ./update.sh --help       show this header
set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HUB_DIR="$PROJ_DIR/hub"
BOT_DIR="$PROJ_DIR/telegram-bot"

DO_PULL=1
DO_RESTART=1
for arg in "$@"; do
  case "$arg" in
    --no-pull)    DO_PULL=0 ;;
    --no-restart) DO_RESTART=0 ;;
    -h|--help)    sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//p}' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '    %s\n' "$*" >&2; }

command -v uv >/dev/null 2>&1 || {
  echo "uv not found — install it first: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
}

# ---- helpers ----------------------------------------------------------------

have_systemctl() { command -v systemctl >/dev/null 2>&1; }

unit_installed() {  # $1 = unit name; true only if the unit file is actually present
  have_systemctl && systemctl cat "$1" >/dev/null 2>&1
}

deployed() {  # $1 = component dir, $2 = unit name
  [[ -x "$1/.venv/bin/python" ]] && return 0
  unit_installed "$2" && return 0
  return 1
}

restart_unit() {  # $1 = unit name; restart only if installed
  local unit="$1"
  unit_installed "$unit" || { warn "$unit not installed — not restarting"; return 0; }
  say "Restarting $unit"
  sudo systemctl restart "$unit"
  sudo systemctl --no-pager --lines=0 status "$unit" || true
}

pyproject_version() {  # $1 = component dir
  sed -n 's/^version = "\(.*\)"/\1/p' "$1/pyproject.toml" 2>/dev/null | head -1
}

# ---- 1. update the source tree ----------------------------------------------

if [[ "$DO_PULL" == 1 ]]; then
  say "Updating source (git)"
  cd "$PROJ_DIR"
  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "Not a git checkout — cannot pull. Use --no-pull to sync the current tree." >&2
    exit 1
  fi
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "Working tree has uncommitted changes to tracked files. Commit or stash them," >&2
    echo "or re-run with --no-pull to update dependencies against the current tree." >&2
    git status --short >&2
    exit 1
  fi
  branch="$(git rev-parse --abbrev-ref HEAD)"
  git fetch --prune --tags origin
  git pull --ff-only origin "$branch"
  echo "    now at $(git describe --tags --always) on ${branch}"
else
  say "Skipping git pull (--no-pull)"
fi

# ---- 2. hub -----------------------------------------------------------------

if deployed "$HUB_DIR" swarm-hub.service; then
  say "Updating hub (uv sync --extra hub)"
  # The dashboard's terminal libs (xterm, asciinema) are gitignored, so refresh
  # them from the tree's version pin before syncing — else the console tab breaks.
  bash "$HUB_DIR/src/picotty/static/vendor/fetch-vendor.sh" \
    || warn "vendor fetch failed — console libs left as-is; re-run when online"
  ( cd "$HUB_DIR" && uv sync --extra hub )
  [[ "$DO_RESTART" == 1 ]] && restart_unit swarm-hub.service
else
  echo "hub not deployed on this host — skipping"
fi

# ---- 3. telegram sidecar ----------------------------------------------------

if deployed "$BOT_DIR" swarm-telegram.service; then
  say "Updating Telegram sidecar (uv sync)"
  ( cd "$BOT_DIR" && uv sync )
  [[ "$DO_RESTART" == 1 ]] && restart_unit swarm-telegram.service
else
  echo "Telegram sidecar not deployed on this host — skipping"
fi

# ---- done -------------------------------------------------------------------

say "Done."
echo "    hub      $(pyproject_version "$HUB_DIR" || echo '?')"
echo "    sidecar  $(pyproject_version "$BOT_DIR" || echo '?')"
echo "    Node firmware is updated separately — push it OTA from the dashboard."
