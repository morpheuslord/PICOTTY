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


def _truthy(name):
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


# OTA needs the filesystem writable to CircuitPython too. Enabling it here has the
# same trade-off as LOG_TO_FILE: the CIRCUITPY drive goes READ-ONLY over USB. For
# an OTA node that is fine — you update it over the wire, not by drag-drop — and
# it is the production posture anyway. Hiding the USB drive (below) is recommended
# alongside OTA so the target never mounts a stray drive.
_OTA = _truthy("OTA_ENABLED")

if _truthy("LOG_TO_FILE") or _OTA:
    import storage
    storage.remount("/", readonly=False)

# OTA watchdog-revert: if a firmware update is pending and the LAST boot hung
# (watchdog reset), the update crash-looped — restore the previous files before
# the (broken) new code.py runs. Done here, in boot.py, so recovery happens
# before the suspect firmware is imported. Everything is defensively wrapped: a
# failure here must never brick the node.
if _OTA:
    try:
        import microcontroller
        _was_wdt = microcontroller.cpu.reset_reason == microcontroller.ResetReason.WATCHDOG
    except Exception:
        _was_wdt = False
    try:
        import otaflash
        _r = otaflash.recover_if_pending(_was_wdt)
        if _r == "reverted":
            print("OTA: update crash-looped; reverted to previous firmware")
    except Exception as _e:
        print("OTA recovery skipped:", _e)
