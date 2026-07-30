# target-setup

Scripts that run **on a target machine** (the box a Pico is plugged into), not on
the hub. They configure the target to talk to the Pico over its USB serial port.

## proxmox-serial.sh

Run once on each Proxmox host with the Pico plugged in (as root):

```bash
./proxmox-serial.sh            # detect + configure
./proxmox-serial.sh --status   # show what it detects, change nothing
./proxmox-serial.sh --undo      # remove the rule + stop the getty
```

It:
- finds the Pico's `/dev/ttyACM*`,
- pins it to a stable **`/dev/ttyPICO`** via a udev rule (survives reboots and
  the ACM number changing),
- runs a serial **login shell** on it, started automatically whenever the Pico is
  present. Your normal `tty1` shell is untouched.

Add `--boot-console` to also send kernel/boot messages to serial (uses
`ttyACM0` since the kernel needs a name before udev runs; edit if yours differs).

## pico-debug.sh

A bidirectional debug view of the **Pico itself** from the host, over the same USB
cable. A node with `console=True` in `boot.py` (the default) exposes a second
serial port — the CircuitPython REPL — carrying the firmware's `print()` output,
tracebacks, and an interactive prompt.

```bash
./pico-debug.sh            # find the console port and open a terminal
./pico-debug.sh --status   # show which port is CONSOLE vs DATA
./pico-debug.sh --pin       # pin a stable /dev/ttyPICO-console symlink
```

In the terminal: **Ctrl-C** interrupts `code.py` into the REPL, **Ctrl-D** reloads
it. It uses `tio`/`picocom`/`screen` if installed, else a built-in Python terminal
(quit with Ctrl-]).

## Console mode: keep it on if you want the debug view

`pico-debug.sh` needs the REPL port, which only exists with `console=True` in
`boot.py` — **the shipped default**. Both scripts here auto-detect which port is
which by USB interface number (CONSOLE = lower, DATA = higher), so keeping console
on is not ambiguous. Set `console=False` only if you never want to debug the Pico
and prefer a single clean serial port (and rebuild/redeploy after changing it).

### Reading vs. driving the serial shell

This makes Proxmox **emit** its serial shell to the Pico (the Pico *reads* it).
To also *type into* that serial shell — including logging in — the Pico must
**write** to the serial channel. The current firmware types via USB HID, which
goes to the host's `tty1`, not the serial getty. Driving the serial console
requires the "send over serial" firmware command (see the project TODO / ask the
maintainer). HID remains the right tool for BIOS/UEFI and the GRUB menu, where a
USB keyboard drives the same console that mirrors to serial.
