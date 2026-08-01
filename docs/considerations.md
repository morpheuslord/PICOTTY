[← Docs index](README.md) · [← Project README](../README.md)

# Considerations

- **Network isolation is the real security boundary.** Both hub and nodes belong
  on an isolated management VLAN; reach the dashboard through a tunnel, never by
  exposing `:8080`. `:9000` must never be reachable from outside the segment. The
  `hello` token is a second line, not a substitute.
- **The raw serial bridge widens that boundary — keep it inside.** When enabled,
  each assigned node's serial line is a plain, unauthenticated TCP port: anything
  that can reach it gets an interactive serial session to that node. It is off by
  default and per-node opt-in. Bind it on the management interface only
  (`HUB_BRIDGE_HOST`) and never expose a bridge port outside the isolated segment.
  See [operations.md](operations.md#the-raw-serial-bridge).
- **Keyboard layout is a per-node firmware choice.** The character-to-keycode
  mapping lives on the node, so a target set to a non-US layout needs the node's
  `KEYBOARD_LAYOUT` set to match or symbols mistype. See
  [firmware.md](firmware.md#keyboard-layout).
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

Much of the original roadmap has shipped. Now available:

- **Automation** — hub-side prompt-state detection, a wait-for-output expect
  engine, an offline command queue delivered on reconnect, and YAML runbooks over
  node groups. See [automation.md](automation.md).
- **Interactive serial** — the `send` write path with HID⇄Serial input modes and
  control-byte shortcuts, plus a raw TCP serial bridge for `minicom`/PuTTY/etc.
  See [operations.md](operations.md#hid-input-vs-serial-input).
- **Session recording** — asciicast v2 export/replay over any time window.
- **Alerting** — outbound webhook/ntfy on node-down, watchdog recovery, and failed
  results, deduped. See [operations.md](operations.md#alerting-hooks).
- **Keyboard layout maps** — per-node `KEYBOARD_LAYOUT`. See
  [firmware.md](firmware.md#keyboard-layout).
- **OTA firmware updates** — a chunked, checksummed push with `.bak` +
  watchdog-revert and finalize-when-healthy on the node, plus hub-side bundle
  storage and a canary rollout. Fully wired and driven from the dashboard behind
  the `ota` capability gate. See [ota.md](ota.md) for the safety model and push flow.

Still scaffolded / off by default:

- **Auth** is scaffolded but off by default (network-gated); enable it if you ever
  place the hub outside an isolated segment.
