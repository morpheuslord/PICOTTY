# boot.py — runs once, before USB enumerates. USB descriptor shape is decided
# here and nowhere else; changing it in code.py is too late.
#
# This node presents itself to the TARGET machine as a composite USB device:
#   - an HID keyboard, for injecting keystrokes, and
#   - a CDC "data" serial channel, the clean back-channel we read the target's
#     console output from (separate from the REPL console).
#
# Verified against current CircuitPython (8.x/9.x):
#   usb_cdc.enable(console=, data=)  -> docs.circuitpython.org/en/latest/shared-bindings/usb_cdc
#   usb_hid.enable((Device.KEYBOARD,)) MUST be in boot.py, keyboard-only keeps
#   the USB endpoint budget small so the composite fits on the RP2040.

import usb_cdc
import usb_hid

# console=True keeps the REPL serial so you can debug the node itself.
# data=True adds the SECOND serial port: usb_cdc.data, the target back-channel.
#
# PRODUCTION TIP: set console=False so the target sees exactly ONE serial port
# (the clean data channel) instead of also seeing the node's Python REPL. Keep
# console=True while developing the firmware.
usb_cdc.enable(console=True, data=True)

# Present only a keyboard. Trimming the default HID set (keyboard+mouse+consumer)
# frees USB endpoints, which matters because we also run two CDC channels.
usb_hid.enable((usb_hid.Device.KEYBOARD,))

# PRODUCTION TIP: uncomment the two lines below to hide the CIRCUITPY drive from
# the target machine (so it does not mount a stray USB drive) and to free two
# more USB endpoints. While developing, leave storage enabled so you can edit
# these files by mounting the drive.
#
# import storage
# storage.disable_usb_drive()

# Optional error logging to /error.txt. When LOG_TO_FILE is true in settings.toml
# we remount the filesystem writable to CircuitPython so code.py can append
# errors you can read by plugging the Pico into any computer.
#
# TRADE-OFF: this makes the CIRCUITPY drive READ-ONLY over USB, so build.sh /
# deploy-zip.sh drag-drop updates stop working until you re-flash CircuitPython
# (hold BOOTSEL) and redeploy with LOG_TO_FILE off. Leave it off unless you are
# actively chasing a fault on a node you can't attach a console to.
import os

if (os.getenv("LOG_TO_FILE") or "").strip().lower() in ("1", "true", "yes", "on"):
    import storage
    storage.remount("/", readonly=False)
