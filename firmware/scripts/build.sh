#!/usr/bin/env bash
# build.sh — assemble or deploy node firmware.
#
# CircuitPython apps are a FOLDER of files on the CIRCUITPY drive, not a single
# compiled binary. This script has two modes:
#   Deploy (default): copy the firmware straight onto a mounted CIRCUITPY board.
#   Stage (--stage):  build a drop-in folder + zip WITHOUT a board attached; you
#                     later drag its contents onto any CIRCUITPY drive.
#
# (The CircuitPython interpreter itself is a separate one-time .uf2 you flash
#  from circuitpython.org — that is the only true single-file drop-in.)
#
# Usage:
#   ./build.sh --node node-01                    # deploy to an auto-detected board
#   ./build.sh --node node-01 --drive /path/CIRCUITPY
#   ./build.sh --node node-01 --stage            # -> ../build/node-01/ + .zip
#   ./build.sh --node node-01 --stage --mpy      # staged, modules precompiled
#   ./build.sh --stage --no-libs                 # firmware only, skip libraries
#
# Flags:
#   --node <id>       include private/nodes/<id>/settings.toml
#   --settings <path> include a specific settings.toml (overrides --node's)
#   --drive <path>    CIRCUITPY mount (deploy mode; auto-detected if omitted)
#   --stage [dir]     build a drop-in artifact instead of writing to a board
#   --cp-version <v>  CircuitPython version for the staged library fetch (default 10.2.1)
#   --mpy             compile modules to .mpy (needs mpy-cross)
#   --no-libs         skip the Adafruit library install
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FW_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_DIR="$FW_DIR/circuitpython"
PROJ_DIR="$(cd "$FW_DIR/.." && pwd)"
PRIV_NODES="$PROJ_DIR/private/nodes"

NODE=""; DRIVE=""; MPY=0; LIBS=1; STAGE=0; STAGE_DIR=""; CP_VERSION="10.2.1"; SETTINGS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --node)  NODE="${2:-}"; shift 2;;
    --settings) SETTINGS="${2:-}"; shift 2;;
    --drive) DRIVE="${2:-}"; shift 2;;
    --stage) STAGE=1; if [[ -n "${2:-}" && "${2:0:2}" != "--" ]]; then STAGE_DIR="$2"; shift 2; else shift; fi;;
    --cp-version) CP_VERSION="${2:-}"; shift 2;;
    --mpy)   MPY=1; shift;;
    --no-libs) LIBS=0; shift;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

ENTRY=(boot.py code.py)
MODULES=(wire.py netlink.py injector.py backchannel.py nodeconfig.py messages.py otaflash.py)

if [[ "$STAGE" == "1" ]]; then
  DEST="${STAGE_DIR:-$FW_DIR/build/${NODE:-node}}"
  rm -rf "$DEST"; mkdir -p "$DEST/lib"
  echo "==> Staging into: $DEST"
  # A stub boot_out.txt lets circup fetch libraries for the right CircuitPython
  # version with no board attached. The board writes its own on boot, and we
  # drop this one from the final artifact.
  echo "Adafruit CircuitPython $CP_VERSION on 2024-01-01; Raspberry Pi Pico with rp2040" > "$DEST/boot_out.txt"
else
  GIVEN_DRIVE="$DRIVE"
  if [[ -z "$DRIVE" ]]; then
    for d in "/media/$USER/CIRCUITPY" "/run/media/$USER/CIRCUITPY" "/Volumes/CIRCUITPY" /media/*/CIRCUITPY; do
      [[ -d "$d" ]] && { DRIVE="$d"; break; }
    done
  fi
  if [[ -z "$DRIVE" || ! -d "$DRIVE" ]]; then
    if [[ -n "$GIVEN_DRIVE" ]]; then echo "The --drive path does not exist: $GIVEN_DRIVE"
    else echo "No CIRCUITPY drive found (looked under /media and /run/media)."; fi
    for b in "/media/$USER/RPI-RP2" "/run/media/$USER/RPI-RP2" "/Volumes/RPI-RP2" /media/*/RPI-RP2; do
      if [[ -d "$b" ]]; then
        echo
        echo "Found $b — the board is in BOOTLOADER mode, not running CircuitPython."
        echo "Flash the Raspberry Pi Pico CircuitPython .uf2 onto it; it remounts as"
        echo "CIRCUITPY, then re-run this. (Or build an offline artifact with --stage.)"
        exit 1
      fi
    done
    echo
    echo "No board attached. Build a drop-in artifact instead:"
    echo "  ./build.sh --node ${NODE:-node-01} --stage"
    exit 1
  fi
  [[ -f "$DRIVE/boot_out.txt" ]] || echo "warning: $DRIVE has no boot_out.txt — is it really a CircuitPython board?"
  DEST="$DRIVE"
  echo "==> Target board: $DEST"
fi

# Entry files always ship as source .py.
for f in "${ENTRY[@]}"; do cp "$SRC_DIR/$f" "$DEST/$f"; echo "  + $f"; done

if [[ "$MPY" == "1" ]]; then
  python3 -m mpy_cross --version >/dev/null 2>&1 || { echo "mpy-cross unavailable; run install-deps.sh or drop --mpy"; exit 1; }
  tmp="$(mktemp -d)"
  for f in "${MODULES[@]}"; do
    python3 -m mpy_cross "$SRC_DIR/$f" -o "$tmp/${f%.py}.mpy"
    cp "$tmp/${f%.py}.mpy" "$DEST/"; rm -f "$DEST/$f"; echo "  + ${f%.py}.mpy"
  done
  rm -rf "$tmp"
else
  for f in "${MODULES[@]}"; do cp "$SRC_DIR/$f" "$DEST/$f"; rm -f "$DEST/${f%.py}.mpy"; echo "  + $f"; done
fi

# Libraries via circup (needs network; works against a board or a staged dir).
if [[ "$LIBS" == "1" ]]; then
  if command -v circup >/dev/null; then
    echo "==> Installing Adafruit libraries with circup"
    if ! circup --path "$DEST" install adafruit_hid adafruit_wiznet5k; then
      echo "warning: circup could not install libraries here."
      [[ "$STAGE" == "1" ]] && echo "  (staged builds need network + a matching --cp-version; or install libs on the board later.)"
    fi
  else
    echo "warning: circup not found — skipping libraries. Run install-deps.sh (and add ~/.local/bin to PATH)."
  fi
fi

# The node's config: an explicit --settings path wins, else --node's file.
SRC_TOML=""
SETTINGS_LABEL=""
if [[ -n "$SETTINGS" ]]; then
  SRC_TOML="$SETTINGS"; SETTINGS_LABEL="$SETTINGS"
elif [[ -n "$NODE" ]]; then
  SRC_TOML="$PRIV_NODES/$NODE/settings.toml"; SETTINGS_LABEL="$NODE"
fi
if [[ -n "$SRC_TOML" ]]; then
  if [[ -f "$SRC_TOML" ]]; then
    cp "$SRC_TOML" "$DEST/settings.toml"; echo "  + settings.toml ($SETTINGS_LABEL)"
    if grep -q 'PUT-HUB-TOKEN-HERE' "$DEST/settings.toml"; then
      echo "  ! settings.toml has a placeholder token — run private/provision.py --token <TOKEN>"
    fi
  else
    echo "warning: settings file not found: $SRC_TOML"
  fi
else
  echo "note: no --node/--settings given; settings.toml not included."
fi

# Keyboard layout library. "us" is built into adafruit_hid; any other layout
# needs a community library pair (keyboard_layout_win_<code> + keycode_win_<code>)
# from Neradoc's CircuitPython_Keyboard_Layouts bundle. Read the layout from the
# settings we just staged and fetch just that one, so a de/uk/fr node types its
# symbols correctly. A missing library is non-fatal: the firmware falls back to US.
if [[ "$LIBS" == "1" && -f "$DEST/settings.toml" ]]; then
  LAYOUT="$(sed -n 's/^[[:space:]]*KEYBOARD_LAYOUT[[:space:]]*=[[:space:]]*"\{0,1\}\([A-Za-z_]*\).*/\1/p' "$DEST/settings.toml" | head -n1 | tr 'A-Z' 'a-z')"
  if [[ -n "$LAYOUT" && "$LAYOUT" != "us" && "$LAYOUT" != "en" && "$LAYOUT" != "en_us" ]]; then
    if command -v circup >/dev/null; then
      echo "==> Installing keyboard layout '$LAYOUT' (community bundle)"
      circup bundle-add Neradoc/CircuitPython_Keyboard_Layouts >/dev/null 2>&1 || true
      if ! circup --path "$DEST" install "keyboard_layout_win_$LAYOUT" "keycode_win_$LAYOUT"; then
        echo "  warning: could not stage layout '$LAYOUT'. The node will fall back to US."
        echo "  Fetch it manually into $DEST/lib/ from CircuitPython_Keyboard_Layouts,"
        echo "  or set KEYBOARD_LAYOUT = \"us\"."
      fi
    else
      echo "warning: KEYBOARD_LAYOUT='$LAYOUT' but circup not found; node will fall back to US."
    fi
  fi
fi

# OTA needs a SHA-256 implementation on the node (CircuitPython has no hashlib),
# so stage adafruit_hashlib when the node has OTA_ENABLED. Without it the node
# simply won't advertise the `ota` capability and the hub won't push to it.
if [[ "$LIBS" == "1" && -f "$DEST/settings.toml" ]]; then
  if grep -qiE '^[[:space:]]*OTA_ENABLED[[:space:]]*=[[:space:]]*"?(1|true|yes|on)"?' "$DEST/settings.toml"; then
    if command -v circup >/dev/null; then
      echo "==> Installing adafruit_hashlib (OTA sha256 verify)"
      circup --path "$DEST" install adafruit_hashlib || \
        echo "  warning: could not stage adafruit_hashlib; node will not offer OTA."
    else
      echo "warning: OTA_ENABLED but circup not found; node will not offer OTA."
    fi
  fi
fi

if [[ "$STAGE" == "1" ]]; then
  rm -f "$DEST/boot_out.txt"          # keep the artifact clean; the board makes its own
  mkdir -p "$FW_DIR/build"
  ZIP="$FW_DIR/build/${NODE:-node}.zip"
  python3 - "$DEST" "$ZIP" <<'PY'
import shutil, sys
dest, zip_path = sys.argv[1], sys.argv[2]
base = zip_path[:-4] if zip_path.endswith(".zip") else zip_path
shutil.make_archive(base, "zip", dest)
print("  + " + zip_path)
PY
  echo
  echo "==> Staged. To install on a Pico already running CircuitPython, either:"
  echo "    • drag the CONTENTS of  $DEST  onto the CIRCUITPY drive, or"
  echo "    • unzip  $ZIP  onto CIRCUITPY."
  echo "  Then power-cycle the board once so boot.py re-runs."
else
  sync
  echo "==> Done. Power-cycle the board once so boot.py re-runs and the USB shape takes effect."
fi
