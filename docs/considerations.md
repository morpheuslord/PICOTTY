[← Docs index](README.md) · [← Project README](../README.md)

# Considerations

- **Network isolation is the real security boundary.** Both hub and nodes belong
  on an isolated management VLAN; reach the dashboard through a tunnel, never by
  exposing `:8080`. `:9000` must never be reachable from outside the segment. The
  `hello` token is a second line, not a substitute.
- **The target must talk.** A node can only read what the target *emits* to its
  serial port. Configure a serial console on the target (a getty and/or a
  bootloader/BIOS serial line) — `proxmox-serial.sh` does this for Proxmox-style
  Debian hosts. Where a target can't be configured, the node is a blind keyboard:
  you can type, you can't read.
- **HID input vs. serial input.** The console tab has two per-node input modes;
  HID keystrokes land on `tty1`/BIOS/GRUB, serial `send` bytes land on the serial
  getty. See [operations.md](operations.md#hid-input-vs-serial-input) for the full
  distinction.
- **The console is not a terminal emulator.** ANSI escapes are stripped and
  full-screen TUIs won't repaint — it's an append-only log for shell interaction,
  not curses apps. See [operations.md](operations.md#the-console-is-not-a-terminal-emulator).
- **CircuitPython version match.** `.mpy` libraries are per-major-version; keep the
  board's firmware and the staged libs on the same major. See
  [firmware.md](firmware.md#circuitpython-version-rules).
- **One uvicorn worker.** The single event loop is the design — a second worker
  would get its own registry and node sockets and the two would disagree.
- **Power.** A node is powered by its target's USB; when the target is off, the
  node is off. That's usually fine.

## Roadmap

- **Auth** is scaffolded but off by default (network-gated); enable it if you ever
  place the hub outside an isolated segment.
