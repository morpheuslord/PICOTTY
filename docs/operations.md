[← Docs index](README.md) · [← Project README](../README.md)

# Operations & debugging

## Observability

Three concentric views, from a headless node outward to the whole fleet:

```mermaid
graph LR
  LED["Onboard LED<br/>(glance: solid / blink codes)"] --> FILE["/error.txt<br/>(plug in, read the fault)"]
  FILE --> REPL["pico-debug.sh<br/>(live REPL over USB)"]
  REPL --> HUBV["Hub Events + results<br/>(dashboard / journalctl / API)"]
```

- **LED** — solid = connected & healthy; slow blink = up but not connected; medium
  blink = Ethernet problem; fast blink = fatal config/HID error.
- **`/error.txt`** — opt-in (`LOG_TO_FILE = "true"`): config/HID/Ethernet errors,
  watchdog-reset boots, and crashes, readable by plugging the Pico into a computer.
  Trade-off: the drive goes read-only over USB until you re-flash CircuitPython.
- **`pico-debug.sh`** — the node's REPL over its console USB channel: live
  `print()` output, tracebacks, and an interactive prompt (Ctrl-C / Ctrl-D).
- **The hub** — once connected, the node reports home: `node_up`/`node_down`,
  heartbeats + RTT, **error** events (including "recovered from watchdog reset"),
  and every command's `ok`/`failed` result with detail. Read it in the dashboard
  **Events** view, `journalctl -u swarm-hub -f`, or `GET /api/events`.

## HID input vs. Serial input

The console tab has two input modes, toggled per node.

- **HID mode** injects USB keystrokes (`type`/`keys`/macros); they land on the
  target's keyboard console — `tty1`, BIOS, GRUB — so it is the mode for firmware
  setup, bootloaders, and any target with no serial getty.
- **Serial mode** writes bytes straight into the target's serial line (the `send`
  command) and is the mode for an interactive **Linux serial login**: with a getty
  on the node's data port, keystrokes reach the login session and the getty echoes
  them back through the same channel, so passwords mask and typed characters appear
  with no local echo.

The two are distinct sessions and line up only at BIOS/GRUB; at Linux runtime, HID
drives `tty1` while Serial drives the getty. **A common surprise:** while watching
the Serial console, HID keystrokes appear to do nothing — they are landing on
`tty1`, not the serial getty you're viewing. Serial mode is offered only for nodes
whose firmware advertises the `serial_tx` capability; older firmware shows HID only.

## The console is not a terminal emulator

The console view is an append-only text log, not a VT. Terminal control sequences
(colors, cursor moves, bracketed-paste toggles) are **stripped** at render, and
full-screen TUIs like `top` or `vim` scroll as plain text rather than repainting
in place. Serial mode is for shell interaction — logging in, running commands,
reading output — not for curses applications. The raw bytes are still stored in
`output_log`; only the display is cleaned.
