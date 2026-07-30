#!/usr/bin/env bash
# proxmox-serial.sh — set up a Proxmox (or any systemd/Debian) host so the swarm
# Pico plugged into its USB gets a stable device name and a login shell on serial.
#
# What it does:
#   1. Finds the Pico's USB CDC serial device (/dev/ttyACM*).
#   2. Writes a udev rule pinning it to a STABLE symlink (default /dev/ttyPICO)
#      keyed on USB vendor/product + interface, so it survives reboots and the
#      ttyACM number changing.
#   3. Runs a serial login shell (serial-getty) on that stable name, started
#      automatically whenever the Pico is present. Your normal tty1 shell is
#      untouched.
#   4. Optionally (--boot-console) also sends kernel/boot messages to serial.
#
# Run it ON THE PROXMOX HOST (as root), with the Pico plugged in:
#   ./proxmox-serial.sh                 # detect + configure
#   ./proxmox-serial.sh --status        # show what it detects, change nothing
#   ./proxmox-serial.sh --dev /dev/ttyACM1   # force a specific device
#   ./proxmox-serial.sh --boot-console  # also add console=ttyACM0 to the kernel
#   ./proxmox-serial.sh --undo          # remove the rule + stop the getty
set -euo pipefail

SYMLINK="ttyPICO"
BAUD="115200"
DEV=""
MODE="install"
BOOT_CONSOLE=0
RULE="/etc/udev/rules.d/99-pico-serial.rules"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --symlink) SYMLINK="${2:?}"; shift 2;;
    --baud) BAUD="${2:?}"; shift 2;;
    --dev) DEV="${2:?}"; shift 2;;
    --boot-console) BOOT_CONSOLE=1; shift;;
    --status) MODE="status"; shift;;
    --undo) MODE="undo"; shift;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

need_root() { [[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }; }
prop() { udevadm info -q property -n "$1" 2>/dev/null | sed -n "s/^$2=//p"; }

# --- undo --------------------------------------------------------------------
if [[ "$MODE" == "undo" ]]; then
  need_root
  systemctl stop "serial-getty@${SYMLINK}.service" 2>/dev/null || true
  rm -f "$RULE"
  udevadm control --reload-rules && udevadm trigger --subsystem-match=tty || true
  echo "removed $RULE and stopped serial-getty@${SYMLINK}. tty1 is unaffected."
  echo "(if you used --boot-console, remove console=ttyACM* from the kernel cmdline manually.)"
  exit 0
fi

# --- detect the Pico ---------------------------------------------------------
echo "==> Looking for a Pico serial device…"
candidates=()
for d in /dev/ttyACM*; do
  [[ -e "$d" ]] || continue
  ven="$(prop "$d" ID_VENDOR_ID)"; mdl="$(prop "$d" ID_MODEL)"; vname="$(prop "$d" ID_VENDOR)"
  # Match on the usual CircuitPython/RP2040 signatures; --dev overrides all this.
  if echo "${ven} ${mdl} ${vname}" | grep -qiE '239a|2e8a|cafe|pico|circuitpython|adafruit|raspberry'; then
    candidates+=("$d")
  fi
done

if [[ -n "$DEV" ]]; then
  target="$DEV"
elif [[ ${#candidates[@]} -eq 1 ]]; then
  target="${candidates[0]}"
elif [[ ${#candidates[@]} -gt 1 ]]; then
  # Two CDC channels (console+data): the DATA one has the higher interface num.
  target=""; best=-1
  for d in "${candidates[@]}"; do
    ifn="$(prop "$d" ID_USB_INTERFACE_NUM)"; ifn="${ifn:-0}"
    if (( 10#$ifn > best )); then best=$((10#$ifn)); target="$d"; fi
  done
  echo "   multiple Pico ttys found; picked $target (data channel, interface $best)."
  echo "   tip: set console=False in the node's boot.py so only one appears."
else
  echo "   no Pico serial device found under /dev/ttyACM*."
  echo "   Is the Pico plugged into THIS host and running the firmware? Try --dev /dev/ttyACMx."
  [[ "$MODE" == "status" ]] && exit 0 || exit 1
fi

vid="$(prop "$target" ID_VENDOR_ID)"
pid="$(prop "$target" ID_MODEL_ID)"
ifnum="$(prop "$target" ID_USB_INTERFACE_NUM)"; ifnum="${ifnum:-00}"
echo "   device : $target"
echo "   usb    : vendor=$vid product=$pid interface=$ifnum ($(prop "$target" ID_VENDOR) $(prop "$target" ID_MODEL))"
echo "   by-id  : $(ls -1 /dev/serial/by-id/ 2>/dev/null | sed 's/^/            /' | tr '\n' ' ')"

if [[ "$MODE" == "status" ]]; then
  echo "==> status only; no changes."
  echo "   rule present : $([[ -f "$RULE" ]] && echo yes || echo no)"
  echo "   symlink      : $([[ -e "/dev/$SYMLINK" ]] && echo "/dev/$SYMLINK -> $(readlink -f /dev/$SYMLINK)" || echo absent)"
  echo "   getty        : $(systemctl is-active "serial-getty@${SYMLINK}.service" 2>/dev/null || echo inactive)"
  exit 0
fi

# --- write the udev rule -----------------------------------------------------
need_root
if [[ -z "$vid" || -z "$pid" ]]; then
  echo "could not read USB vendor/product for $target; pass --dev explicitly."; exit 1
fi

echo "==> Writing $RULE (stable name /dev/$SYMLINK + auto login shell)"
# The login shell is the stock serial-getty@.service template, which runs agetty
# and therefore, deliberately, PROMPTS FOR A LOGIN — no autologin. This matters
# now that the node can WRITE to this serial line (the hub's `send`/Serial-mode
# path): the console tab is an interactive login, so it must require credentials.
# Network isolation (the management VLAN) is a boundary, NOT an authenticator; do
# NOT add --autologin here or via a serial-getty override. agetty also sets ICRNL
# on the line, so a bare CR (0x0d) from the serial-write path is translated to a
# newline — Enter over serial behaves exactly like Enter on a local tty.
cat > "$RULE" <<EOF
# Managed by proxmox-serial.sh — swarm Pico serial console.
# Stable symlink + a serial login shell started whenever the Pico is present.
# serial-getty@ prompts for login (no autologin) — the serial path needs credentials.
SUBSYSTEM=="tty", ATTRS{idVendor}=="$vid", ATTRS{idProduct}=="$pid", ENV{ID_USB_INTERFACE_NUM}=="$ifnum", SYMLINK+="$SYMLINK", TAG+="systemd", ENV{SYSTEMD_WANTS}+="serial-getty@$SYMLINK.service"
EOF

udevadm control --reload-rules
udevadm trigger --subsystem-match=tty --action=add
systemctl daemon-reload
sleep 1

if [[ -e "/dev/$SYMLINK" ]]; then
  systemctl restart "serial-getty@${SYMLINK}.service" || true
  echo "   /dev/$SYMLINK -> $(readlink -f "/dev/$SYMLINK")"
  echo "   serial-getty@${SYMLINK}: $(systemctl is-active "serial-getty@${SYMLINK}.service" 2>/dev/null || echo starting)"
else
  echo "   symlink not present yet; it will appear on the next plug/boot and the"
  echo "   login shell will start automatically."
fi

# --- optional: kernel/boot console on serial ---------------------------------
if [[ "$BOOT_CONSOLE" == "1" ]]; then
  # Order matters: the kernel sends boot messages to EVERY console= entry, but the
  # LAST one is the "primary" — it becomes /dev/console and receives init's stdio
  # and the single-user prompt. We put the serial line last so a headless host is
  # fully drivable over serial, and keep console=tty0 FIRST so a physically
  # attached monitor still shows the boot messages (tty0 = the active virtual
  # console). Drop tty0 and a monitor goes dark at boot.
  CON="console=tty0 console=ttyACM0,${BAUD}"
  echo "==> Adding boot console ($CON). Note: uses ttyACM0 (numbered, not the symlink),"
  echo "    because the kernel needs a name before udev runs. Adjust if it isn't ttyACM0."
  echo "    console=tty0 is kept so a local monitor still gets boot messages; the serial"
  echo "    entry is last, making it the primary console for init and single-user mode."
  if command -v proxmox-boot-tool >/dev/null && [[ -f /etc/kernel/cmdline ]]; then
    if ! grep -q "console=ttyACM0" /etc/kernel/cmdline; then
      sed -i "s|\$| ${CON}|" /etc/kernel/cmdline
    fi
    proxmox-boot-tool refresh
    echo "   updated /etc/kernel/cmdline (systemd-boot); reboot to apply."
  elif [[ -f /etc/default/grub ]]; then
    if ! grep -q "console=ttyACM0" /etc/default/grub; then
      sed -i "s|^GRUB_CMDLINE_LINUX=\"\(.*\)\"|GRUB_CMDLINE_LINUX=\"\1 ${CON}\"|" /etc/default/grub
    fi
    update-grub
    echo "   updated /etc/default/grub; reboot to apply."
  else
    echo "   couldn't find /etc/kernel/cmdline or /etc/default/grub; skip."
  fi
fi

echo
echo "Done. tty1 keeps its normal shell; the Pico now reads a serial login on /dev/$SYMLINK."
echo "Verify from the host:  ls -l /dev/$SYMLINK ; systemctl status serial-getty@${SYMLINK}"
