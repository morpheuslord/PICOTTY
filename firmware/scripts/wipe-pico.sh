#!/usr/bin/env bash
# wipe-pico.sh — erase a node's files when the CIRCUITPY drive is WRITE-PROTECTED.
#
# THE PROBLEM this solves: once a node boots with OTA_ENABLED (or LOG_TO_FILE),
# boot.py remounts the filesystem writable TO CIRCUITPYTHON, which makes the drive
# READ-ONLY OVER USB. From the host you can no longer drag-drop or delete files —
# so you can't reflash it the normal way.
#
# THE WAY IN is the CircuitPython REPL. The firmware can write its own filesystem
# even when the host can't, so we drop into the REPL over the node's CONSOLE
# serial port and tell CircuitPython to erase itself:
#
#     import storage; storage.erase_filesystem()
#
# That reformats CIRCUITPY to an empty, HOST-WRITABLE drive and reboots — after
# which you can drag a fresh firmware/build/<node>/ onto it again.
#
# Usage:
#   ./wipe-pico.sh                 # find the console port, confirm, full erase
#   ./wipe-pico.sh --dev /dev/ttyACM0
#   ./wipe-pico.sh --status        # just show which port is the REPL, change nothing
#   ./wipe-pico.sh --files         # softer: delete only the app files, keep lib/
#   ./wipe-pico.sh --yes           # skip the confirmation prompt (scripted use)
#
# This is DESTRUCTIVE: --files removes the node code; the default fully reformats
# the drive. It is exactly what you want on a bricked/write-locked node, but there
# is no undo. The node's settings live in private/nodes/<id>/ so nothing unique is
# lost — re-run build.sh afterwards.
#
# If the REPL can't be reached at all (no console port, watchdog resetting too
# fast), use the hardware path instead: hold BOOTSEL, drop the CircuitPython .uf2
# (or Adafruit's flash_nuke.uf2) onto RPI-RP2. That always works. See the end.
set -euo pipefail

DEV=""
MODE="erase"      # erase | files
ASSUME_YES=0
STATUS_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) DEV="${2:?}"; shift 2;;
    --status) STATUS_ONLY=1; shift;;
    --files) MODE="files"; shift;;
    --yes|-y) ASSUME_YES=1; shift;;
    -h|--help) awk 'NR>1 && /^#/{sub(/^# ?/,"");print;next} NR>1{exit}' "$0"; exit 0;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

prop() { udevadm info -q property -n "$1" 2>/dev/null | sed -n "s/^$2=//p"; }

# --- find the CircuitPython CONSOLE (REPL) serial port ----------------------
# A node exposes two ACM ports; the REPL is the LOWER USB interface number (the
# DATA back-channel is the higher one). Same detection as pico-debug.sh.
detect_console() {
  local candidates=() d console="" lo=999 ifn
  for d in /dev/ttyACM*; do
    [[ -e "$d" ]] || continue
    if echo "$(prop "$d" ID_VENDOR_ID) $(prop "$d" ID_MODEL) $(prop "$d" ID_VENDOR)" \
         | grep -qiE '239a|2e8a|cafe|pico|circuitpython|adafruit|raspberry'; then
      candidates+=("$d")
    fi
  done
  for d in "${candidates[@]}"; do
    ifn="$(prop "$d" ID_USB_INTERFACE_NUM)"; ifn="${ifn:-0}"; ifn=$((10#$ifn))
    if (( ifn < lo )); then lo=$ifn; console="$d"; fi
  done
  # Fall back to the sole/first candidate if interface numbers weren't available.
  [[ -z "$console" && ${#candidates[@]} -gt 0 ]] && console="${candidates[0]}"
  echo "$console"
}

if [[ -z "$DEV" ]]; then
  DEV="$(detect_console || true)"
fi

if [[ "$STATUS_ONLY" == "1" ]]; then
  echo "CircuitPython console (REPL) port: ${DEV:-<none found>}"
  [[ -n "$DEV" ]] && { echo "  vendor : $(prop "$DEV" ID_VENDOR) $(prop "$DEV" ID_MODEL)"; \
                       echo "  iface  : $(prop "$DEV" ID_USB_INTERFACE_NUM)"; }
  echo "Other Pico serial ports:"; ls /dev/ttyACM* 2>/dev/null | sed 's/^/  /' || echo "  (none)"
  exit 0
fi

if [[ -z "$DEV" || ! -e "$DEV" ]]; then
  echo "No CircuitPython console port found."
  echo "  • Plug the Pico into THIS machine (it must be running CircuitPython, not in BOOTSEL)."
  echo "  • Pass it explicitly:  ./wipe-pico.sh --dev /dev/ttyACM0"
  echo "  • Or use the hardware path: hold BOOTSEL and drop the CircuitPython .uf2 (or flash_nuke.uf2)."
  exit 1
fi

if [[ ! -w "$DEV" ]]; then
  echo "Cannot write to $DEV (permission). Try:  sudo ./wipe-pico.sh --dev $DEV"
  echo "  (or add yourself to the 'dialout'/'uucp' group and re-login)."
  exit 1
fi

# --- confirm (destructive) --------------------------------------------------
if [[ "$MODE" == "erase" ]]; then
  ACTION="REFORMAT the CIRCUITPY filesystem (erase EVERYTHING, incl. lib/)"
else
  ACTION="DELETE the node app files (code.py, boot.py, settings.toml, modules; keep lib/)"
fi
echo "About to $ACTION"
echo "  on the node at: $DEV"
if [[ "$ASSUME_YES" != "1" ]]; then
  read -r -p "Type 'wipe' to proceed: " ans
  [[ "$ans" == "wipe" ]] || { echo "aborted."; exit 1; }
fi

# --- build the REPL program -------------------------------------------------
# We disable the watchdog FIRST (a node in its steady loop has it armed; at the
# REPL nothing feeds it, so it would reset us in ~8s mid-wipe), then act.
if [[ "$MODE" == "erase" ]]; then
  read -r -d '' PROG <<'PY' || true
import microcontroller, storage
try:
    microcontroller.watchdog.mode = None
except Exception:
    pass
print("WIPE: erasing filesystem...")
storage.erase_filesystem()
PY
else
  read -r -d '' PROG <<'PY' || true
import os, microcontroller
try:
    microcontroller.watchdog.mode = None
except Exception:
    pass
_targets = ["code.py","boot.py","settings.toml","wire.py","netlink.py","injector.py",
            "backchannel.py","nodeconfig.py","messages.py","otaflash.py",
            "error.txt","ota_pending.json"]
for _f in _targets:
    try:
        os.remove("/"+_f)
        print("WIPE: removed", _f)
    except OSError:
        pass
print("WIPE: done. Reset the board; the drive will be host-writable again.")
import supervisor; supervisor.reload()
PY
fi

# --- drive the REPL over the serial port ------------------------------------
# Pure bash + stty (no pyserial). We use the RAW REPL (Ctrl-A ... Ctrl-D) so the
# whole program runs atomically regardless of indentation echo.
echo "==> Configuring $DEV and entering the REPL"
stty -F "$DEV" 115200 raw -echo -hupcl 2>/dev/null || stty -F "$DEV" 115200 raw -echo 2>/dev/null || true

exec 3<>"$DEV"
send() { printf '%b' "$1" >&3; }

# Break out of the running firmware into the friendly REPL, then into raw REPL.
send '\r'
send '\x03'; sleep 0.4      # Ctrl-C -> KeyboardInterrupt out of code.py
send '\x03'; sleep 0.4      # again, in case the first landed mid-reconnect
send '\x01'; sleep 0.3      # Ctrl-A -> raw REPL

# Paste the program, then Ctrl-D to execute.
while IFS= read -r line; do
  send "$line"; send '\r'
done <<< "$PROG"
send '\x04'                  # Ctrl-D -> run the pasted block
sleep 4                      # erase_filesystem() reformats + reboots

exec 3>&- || true

echo
echo "==> Sent. What to expect:"
if [[ "$MODE" == "erase" ]]; then
  echo "   The board reformats and reboots. CIRCUITPY reappears EMPTY and host-writable."
  echo "   Now re-deploy:  bash build.sh --node <id> --drive /run/media/\$USER/CIRCUITPY"
  echo "   (or drag the contents of firmware/build/<id>/ onto the drive)."
else
  echo "   App files removed; on the next boot the drive is host-writable again."
fi
echo
echo "If nothing happened (watchdog reset the board first, or Ctrl-C didn't land),"
echo "just run this again — or use the guaranteed hardware path:"
echo "   hold BOOTSEL while plugging in  ->  RPI-RP2 appears  ->  drop the"
echo "   CircuitPython .uf2 (fresh install) or Adafruit flash_nuke.uf2 (full erase)."
