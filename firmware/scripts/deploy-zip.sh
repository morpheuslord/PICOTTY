#!/usr/bin/env bash
# deploy-zip.sh — extract a staged firmware zip onto a mounted CIRCUITPY drive.
#
# Use this on a machine that only has the zip (not the repo): flash CircuitPython
# first so the board mounts as CIRCUITPY, then:
#
#   ./deploy-zip.sh node-01.zip                       # auto-detect the drive
#   ./deploy-zip.sh node-01.zip /run/media/me/CIRCUITPY
#
# The zip's CONTENTS (boot.py, code.py, modules, lib/, settings.toml) are written
# to the ROOT of the drive — which is exactly where CircuitPython expects them.
set -euo pipefail

ZIP="${1:-}"
DRIVE="${2:-}"
[[ -n "$ZIP" && -f "$ZIP" ]] || { echo "usage: ./deploy-zip.sh <firmware.zip> [CIRCUITPY_path]"; exit 1; }

if [[ -z "$DRIVE" ]]; then
  for d in "/media/$USER/CIRCUITPY" "/run/media/$USER/CIRCUITPY" "/Volumes/CIRCUITPY" /media/*/CIRCUITPY; do
    [[ -d "$d" ]] && { DRIVE="$d"; break; }
  done
fi
[[ -n "$DRIVE" && -d "$DRIVE" ]] || { echo "No CIRCUITPY drive found; pass the path as the 2nd argument."; exit 1; }
[[ -f "$DRIVE/boot_out.txt" ]] || echo "warning: $DRIVE has no boot_out.txt — is it a CircuitPython board?"

echo "==> Extracting $ZIP -> $DRIVE"
# Prefer unzip; fall back to Python so this works without the unzip package.
if command -v unzip >/dev/null; then
  unzip -o "$ZIP" -d "$DRIVE"
else
  python3 - "$ZIP" "$DRIVE" <<'PY'
import sys, zipfile
zip_path, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zip_path) as z:
    z.extractall(dest)
    print("  extracted %d entries" % len(z.namelist()))
PY
fi
sync
echo "==> Done. Power-cycle the board once so boot.py re-runs."
