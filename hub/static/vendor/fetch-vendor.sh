#!/usr/bin/env sh
# fetch-vendor.sh — download the pinned front-end libraries used by the dashboard
# as OPTIONAL progressive enhancements (xterm.js console + asciinema replay).
#
# The hub runs on an isolated VLAN with no internet, so we do NOT fetch these at
# runtime and we do NOT commit them to the repo. Run this ONCE on a networked
# machine, then copy the whole hub/static/vendor/ directory onto the hub.
#
# app.js feature-detects window.Terminal / window.AsciinemaPlayer; if these files
# are absent the UI silently falls back to the built-in DOM log renderer and a
# plain .cast download link. So skipping this script is safe — you just lose the
# xterm terminal and the inline replay player.
#
# Pinned versions (bump deliberately; keep the filenames below stable — index.html
# references these exact names):
#   xterm                 5.3.0
#   @xterm/addon-fit      0.8.0   (a.k.a. xterm-addon-fit)
#   asciinema-player      3.8.0
#
# Usage:  cd hub/static/vendor && sh fetch-vendor.sh
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# jsDelivr is used only as a download mirror here, on a networked box. Nothing
# fetches these URLs at hub runtime.
fetch() {
  url="$1"; out="$2"
  echo "  -> $out"
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$url" -o "$out"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$out" "$url"
  else echo "need curl or wget" >&2; exit 1; fi
}

echo "Fetching xterm.js 5.3.0 ..."
fetch "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"                       xterm.js
fetch "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css"                      xterm.css

echo "Fetching xterm addon-fit 0.8.0 ..."
fetch "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"   xterm-addon-fit.js

echo "Fetching asciinema-player 3.8.0 ..."
fetch "https://cdn.jsdelivr.net/npm/asciinema-player@3.8.0/dist/bundle/asciinema-player.min.js"   asciinema-player.min.js
fetch "https://cdn.jsdelivr.net/npm/asciinema-player@3.8.0/dist/bundle/asciinema-player.css"      asciinema-player.css

echo "Done. Files in $DIR:"
ls -1 "$DIR"
echo
echo "Now copy hub/static/vendor/ onto the hub. index.html already references"
echo "these filenames; no further wiring is needed."
