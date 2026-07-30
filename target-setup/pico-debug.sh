#!/usr/bin/env bash
# pico-debug.sh — open a bidirectional debug console to the Pico ITSELF from the
# host it's plugged into (e.g. a Proxmox node), over the same USB cable.
#
# A node built with console=True in boot.py exposes TWO USB serial ports:
#   - the DATA channel  — the target's serial back-channel (proxmox-serial.sh)
#   - the CONSOLE channel — the CircuitPython REPL: the node's print() output,
#     tracebacks, and an interactive prompt.
# This attaches a live terminal to that CONSOLE channel, so you can watch what the
# firmware is doing and drop into the REPL to debug it.
#
#   ./pico-debug.sh            # find the console port and open a terminal
#   ./pico-debug.sh --status   # show which ports are which, change nothing
#   ./pico-debug.sh --dev /dev/ttyACM0
#   ./pico-debug.sh --pin      # also pin a stable /dev/ttyPICO-console symlink
#   ./pico-debug.sh --undo      # remove the pinned symlink rule
#
# In the terminal:  Ctrl-C interrupts code.py into the REPL,  Ctrl-D reloads it.
# To QUIT the terminal without touching the node, use the tool's own exit key
# (printed on connect; the built-in fallback quits with Ctrl-]).
set -euo pipefail

SYMLINK="ttyPICO-console"
DEV=""
MODE="attach"
RULE="/etc/udev/rules.d/98-pico-console.rules"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dev) DEV="${2:?}"; shift 2;;
    --symlink) SYMLINK="${2:?}"; shift 2;;
    --status) MODE="status"; shift;;
    --pin) MODE="pin"; shift;;
    --undo) MODE="undo"; shift;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

prop() { udevadm info -q property -n "$1" 2>/dev/null | sed -n "s/^$2=//p"; }

if [[ "$MODE" == "undo" ]]; then
  [[ $EUID -eq 0 ]] || { echo "run as root for --undo"; exit 1; }
  rm -f "$RULE"; udevadm control --reload-rules && udevadm trigger --subsystem-match=tty || true
  echo "removed $RULE"; exit 0
fi

# --- detect the Pico serial ports -------------------------------------------
candidates=()
for d in /dev/ttyACM*; do
  [[ -e "$d" ]] || continue
  if echo "$(prop "$d" ID_VENDOR_ID) $(prop "$d" ID_MODEL) $(prop "$d" ID_VENDOR)" \
       | grep -qiE '239a|2e8a|cafe|pico|circuitpython|adafruit|raspberry'; then
    candidates+=("$d")
  fi
done

# The CONSOLE channel is the LOWER USB interface number; DATA is the higher one.
console=""; data=""; lo=999; hi=-1
for d in "${candidates[@]}"; do
  ifn="$(prop "$d" ID_USB_INTERFACE_NUM)"; ifn="${ifn:-0}"; ifn=$((10#$ifn))
  (( ifn < lo )) && { lo=$ifn; console="$d"; }
  (( ifn > hi )) && { hi=$ifn; data="$d"; }
done

if [[ "$MODE" == "status" ]]; then
  echo "Pico serial ports on this host:"
  if [[ ${#candidates[@]} -eq 0 ]]; then
    echo "  none found under /dev/ttyACM* — is the Pico plugged in and running?"
  else
    for d in "${candidates[@]}"; do
      role="?"; [[ "$d" == "$console" ]] && role="CONSOLE (REPL/debug)"; [[ "$d" == "$data" && "$data" != "$console" ]] && role="DATA (target back-channel)"
      echo "  $d  interface=$(prop "$d" ID_USB_INTERFACE_NUM)  -> $role"
    done
    [[ ${#candidates[@]} -eq 1 ]] && echo "  (only one port — boot.py likely has console=False, so there is no REPL to debug.)"
  fi
  exit 0
fi

target="${DEV:-$console}"
if [[ -z "$target" ]]; then
  echo "No Pico console port found. Plug in a node built with console=True in boot.py,"
  echo "or pass --dev /dev/ttyACMx. (Run --status to see the ports.)"
  exit 1
fi
if [[ ${#candidates[@]} -eq 1 && -z "$DEV" ]]; then
  echo "warning: only one Pico serial port is present. If boot.py has console=False"
  echo "there is no REPL here — this may be the DATA channel. Ctrl-C won't get a prompt."
fi

# --- optional: pin a stable symlink for the console port --------------------
if [[ "$MODE" == "pin" ]]; then
  [[ $EUID -eq 0 ]] || { echo "run as root for --pin"; exit 1; }
  vid="$(prop "$target" ID_VENDOR_ID)"; pid="$(prop "$target" ID_MODEL_ID)"
  ifnum="$(prop "$target" ID_USB_INTERFACE_NUM)"; ifnum="${ifnum:-00}"
  cat > "$RULE" <<EOF
# Managed by pico-debug.sh — stable name for the Pico's REPL/console port.
SUBSYSTEM=="tty", ATTRS{idVendor}=="$vid", ATTRS{idProduct}=="$pid", ENV{ID_USB_INTERFACE_NUM}=="$ifnum", SYMLINK+="$SYMLINK"
EOF
  udevadm control --reload-rules && udevadm trigger --subsystem-match=tty --action=add
  echo "pinned /dev/$SYMLINK -> the console port. Debug it any time with:"
  echo "  ./pico-debug.sh --dev /dev/$SYMLINK"
  exit 0
fi

# --- attach an interactive terminal -----------------------------------------
echo "==> Debug console: $target"
echo "    Ctrl-C = REPL,  Ctrl-D = reload code.py."
if command -v tio >/dev/null; then
  echo "    (tio: quit with Ctrl-T then Q)"; exec tio "$target"
elif command -v picocom >/dev/null; then
  echo "    (picocom: quit with Ctrl-A then Ctrl-Q)"; exec picocom -q -b 115200 "$target"
elif command -v screen >/dev/null; then
  echo "    (screen: quit with Ctrl-A then K)"; exec screen "$target" 115200
else
  echo "    (built-in terminal: quit with Ctrl-])"
  echo "    tip: 'apt install tio' for a nicer one."
  exec python3 - "$target" <<'PY'
import os, sys, select, termios, tty
port = sys.argv[1]
fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
try:  # put the serial line in raw mode (USB-CDC ignores baud)
    tty.setraw(fd)
except Exception:
    pass
old = termios.tcgetattr(0)
tty.setraw(0)
try:
    while True:
        r, _, _ = select.select([0, fd], [], [])
        if 0 in r:
            d = os.read(0, 1024)
            if b'\x1d' in d:      # Ctrl-] quits
                break
            os.write(fd, d)
        if fd in r:
            d = os.read(fd, 4096)
            if not d:
                break
            os.write(1, d)
finally:
    termios.tcsetattr(0, termios.TCSADRAIN, old)
    os.close(fd)
    print("\r")
PY
fi
