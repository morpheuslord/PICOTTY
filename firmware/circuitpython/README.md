# Node firmware (CircuitPython)

Firmware for one swarm node: a Raspberry Pi Pico (RP2040) with a WIZnet W5100S
Ethernet HAT. The node plugs into a target machine's USB port and presents itself
as a **USB keyboard** plus a **USB serial data channel**, while reaching the hub
over Ethernet (SPI). It dials the hub, obeys its commands (type text, send key
chords, run sequences, read the target's serial output, and **write** into the
target's serial line), and reports back.

This implements the node half of the wire protocol: growing reconnect backoff, a
bounded receive buffer, a per-loop cap on forwarded output, non-blocking I/O
throughout, a hardware watchdog, and full coverage of every message type.

## Files

| File | Role |
|---|---|
| `boot.py` | Shapes the USB device (keyboard HID + CDC data channel). Runs once, before USB enumerates. |
| `code.py` | Main program: bring-up, connect/backoff supervisor, cooperative session loop, command dispatch. |
| `netlink.py` | Transport over the W5100S: brings the interface up once, manages one TCP socket with correct non-blocking semantics. |
| `injector.py` | The keystroke path: type literal text and map named chords to HID keycodes. |
| `backchannel.py` | Reads the target's serial output from the CDC data channel, and writes into it for the `send` command (chunked, non-blocking, drained across loop passes). |
| `wire.py` | Length-prefixed JSON framing and a bounded frame reader. |
| `messages.py` | Builders for every node→hub message. |
| `nodeconfig.py` | Loads and validates `settings.toml`. |
| `otaflash.py` | Over-the-wire firmware update machinery (checksummed push, `.bak` + watchdog-revert). Wired into `code.py`/`boot.py` behind the `ota` capability — see the OTA note below. |
| `settings.toml.example` | Copy to `settings.toml` and edit per node. |

## Hardware and wiring

W5100S HAT on the Pico's SPI0 (confirm against your HAT's silkscreen — revisions
vary):

| Signal | Pico GPIO |
|---|---|
| MISO | GP16 |
| CS   | GP17 |
| SCK  | GP18 |
| MOSI | GP19 |
| RST  | GP20 |
| INT  | GP21 (unused by the polling driver) |

The HAT is powered from the Pico's 3V3; the Pico is powered from the target's USB.
When the target is off, the node is off. If you need the node to survive a target
power cycle, give it a second power feed and avoid backfeeding the target (a
hardware choice, not a firmware one).

To change the pins, edit the constants at the top of `netlink.py`.

## Flashing

1. **Install CircuitPython** on the Pico. Flash the generic *Raspberry Pi Pico*
   build (8.x or 9.x), **not** the W5100S-EVB build — your Ethernet is a separate
   HAT, not integrated. Hold BOOTSEL, drag the `.uf2` onto the `RPI-RP2` drive.
   The board reboots as a `CIRCUITPY` drive.

2. **Install the libraries** into `CIRCUITPY/lib/`. The easiest way is
   [`circup`](https://github.com/adafruit/circup), which pulls dependencies too:

   ```bash
   pip install circup
   circup install adafruit_wiznet5k adafruit_hid
   ```

   That brings in `adafruit_wiznet5k`, `adafruit_hid`, and their dependencies
   (`adafruit_bus_device`, `adafruit_ticks`, `adafruit_connection_manager`).
   Requires **adafruit_wiznet5k ≥ 7.0.0** (the SocketPool API this firmware uses).
   Alternatively, copy those folders from the matching
   [CircuitPython library bundle](https://circuitpython.org/libraries) by hand.

3. **Copy the firmware.** Copy every file in this directory to the root of the
   `CIRCUITPY` drive: `boot.py`, `code.py`, `netlink.py`, `injector.py`,
   `backchannel.py`, `wire.py`, `messages.py`, `nodeconfig.py`.

4. **Configure the node.** Copy `settings.toml.example` to `settings.toml` on the
   drive and edit at least `NODE_ID`, `NODE_TOKEN`, and `HUB_IP`. Because `boot.py`
   changes the USB descriptors, **fully power-cycle the board once** after the
   first copy (a plain reset re-runs `code.py` but not `boot.py`).

5. **Watch it come up.** Open the node's REPL console serial port; you should see
   `node <id> ip <addr> cap ['hid', 'cdc', 'serial_tx']` then `connected; hello sent`.

### Build scripts (steps 2–4 automated)

`../scripts/install-deps.sh` installs the tooling (circup, mpy-cross). Then
`../scripts/build.sh` either deploys to a mounted board or builds a drop-in
artifact:

```bash
../scripts/build.sh --node node-01              # copy onto a mounted CIRCUITPY
../scripts/build.sh --node node-01 --stage      # -> ../build/node-01/ + a .zip
```

A CircuitPython app is a *folder* of files (`boot.py`, `code.py`, the modules,
`lib/`, `settings.toml`), not a single compiled binary — the only one-file
drop-in is the CircuitPython `.uf2` in step 1. `--stage` assembles that folder
(and a zip) offline, with the Adafruit libraries fetched into `lib/`, so you can
drag its contents onto any board later without this machine present. Add `--mpy`
to ship the modules precompiled.

## `settings.toml`

`NODE_ID`, `NODE_TOKEN`, and `HUB_IP` are required; the node refuses to run without
them and prints a clear error. Everything else has a sensible default — see the
comments in `settings.toml.example`. Notes:

- Use the hub's **IP**, not a hostname, so `connect()` stays fast.
- Leave `STATIC_IP` unset to use DHCP. A static node needs no lease upkeep.
- Each node auto-derives a **unique MAC** from its `NODE_ID` (nodes must not share
  a MAC on the VLAN). Override with `NODE_MAC` only if you manage MACs centrally.
- Numbers must have no leading zeros, underscores, or `0x`/`0b` prefixes
  (CircuitPython parses `settings.toml` numbers with C `strtol` rules).

## The real limitation: the target must talk

The node presents a serial port to the target, but it can only read what the
target actually *sends* to it. By default a running OS sends nothing there. To get
output back, configure the target to use that serial port as a console:

- **Linux:** enable a `getty` on the serial device (e.g. `systemctl enable
  --now serial-getty@ttyACMx.service`) and/or add `console=ttyACMx,115200` to the
  kernel command line.
- **BIOS/UEFI:** enable serial console redirection to the port.

Where a target can't be configured this way, the node is a **blind keyboard**: you
can type, you can't read. That's inherent to the hardware. The node still advertises
`cdc`; it just won't see output until the target emits some. Decide per target
which machines are worth wiring a console on.

Also note the node presents **two** CDC ports (the node's own REPL console and the
data channel). Point the target's serial console at the **data** port. For a
production node you can set `console=False` in `boot.py` so the target sees only the
one clean data port.

## Writing to the serial line — the `send` command

Reading the console is only half of an interactive session. The `send` command
lets the hub **write** bytes into the target's serial line, so with a getty
running there the console becomes a real serial login (type a command, the getty
echoes it back, you read the reply — all over the one CDC data channel).

- Wire shape: `{type: "send", cmd_id, data}` for UTF-8 text, or
  `{type: "send", cmd_id, raw}` where `raw` is a hex string (e.g. `"03"` for
  Ctrl+C, `"0d"` for CR, `"7f"` for Backspace). Exactly one of `data`/`raw`.
  Binary bytes travel as hex so the wire stays UTF-8 JSON.
- **Cooperative, non-blocking.** Writes are chunked and drained across loop
  passes with a per-loop byte budget (`SERIAL_TX_BUDGET`, default 1024),
  interleaved with the console read path so neither starves the other. A large
  paste can never block the loop or trip the ~8 s watchdog.
- **Bounded buffer.** At most `SERIAL_TX_BOUND` bytes (default 4096) may be queued
  but unsent at once. A `send` that would overflow it is failed with a
  `serial tx backlog` detail rather than growing memory. The `ok` result is sent
  only once the whole payload has been handed to the CDC port.
- **Capability-gated.** A node advertises `serial_tx` in its `hello` capabilities
  when `usb_cdc.data` is present, so the hub and dashboard can tell new firmware
  from old. If the data channel is not enabled in `boot.py`, `send` fails cleanly
  with a clear detail — it never crashes the loop.

## Keyboard layout — `KEYBOARD_LAYOUT`

The node types as a USB HID keyboard, and the character-to-keycode mapping is
**layout-specific** and lives on the node. A US-configured node typing into a
target set to a German/UK/French layout mistypes symbols (the classic "the
password has a `/` and the node sent `-`" bug), so the layout is a per-node
setting the hub cannot fix for you.

- Set `KEYBOARD_LAYOUT` in `settings.toml` to the target's layout code. Default
  `us`; an unset value keeps a node typing exactly as before.
- `us` (or `en`/`en_us`) uses the built-in `KeyboardLayoutUS`. Any other code
  lazily imports the community library `keyboard_layout_win_<code>` (which needs
  its paired `keycode_win_<code>` in `lib/`). If that library isn't on the board,
  the node **logs a warning and falls back to US** — a bad layout degrades to a
  working keyboard, never a fatal error. Stage the layout your node needs.
- The node reports its resolved layout in `hello`; the hub shows it read-only in
  node detail so you can see what each node is set to.
- Only literal text (`type`, and `send` as text) is layout-sensitive; the
  named-chord path (`keys`) maps keycodes directly and is unaffected.

## Firmware updates over the wire

`otaflash.py` implements receiving a new firmware bundle over the network and
swapping it in, with SHA-256 verification, a `.bak` backup of every replaced file,
and an automatic revert if the new firmware crash-loops (a watchdog reset on the
next boot restores the `.bak` set). It is **fully wired into `code.py` and
`boot.py`**: `code.py` imports it, advertises the `ota` capability when
`OTA_ENABLED` is set and the filesystem is writable with `adafruit_hashlib`
present, and handles `ota_begin`/`ota_chunk`/`ota_commit` plus finalize-on-healthy;
`boot.py` runs its boot-time recovery. So an OTA-posture node can be flashed over
the wire from the dashboard. The safety model and push flow are documented in
[../../docs/ota.md](../../docs/ota.md).

## Reboot, reconnect, and liveness

- `reboot` from the hub calls `supervisor.reload()` — it restarts the firmware
  logic (fresh sockets and buffers) **without** re-enumerating USB, so the target
  keeps seeing the keyboard and serial port. It is not a target reboot.
- If the hub goes away, the node retries with growing backoff (1 s → 30 s) and
  resumes when it returns. It does nothing to the target in the meantime.
- If the main loop ever hangs, the hardware watchdog resets the node (this *does*
  re-enumerate USB). Disable it with `WATCHDOG_ENABLED = "false"` if you prefer.

## Testing a node in isolation (no hub)

Use `../tools/testhub.py` — a mock hub that a node connects to, so you can verify
the firmware before the real hub exists. Point the node at the machine running it
(set `HUB_IP`/`HUB_PORT` in the node's `settings.toml`) and either match the token
or let the tool accept any.

```bash
# on the test machine:
python3 ../tools/testhub.py                 # interactive
python3 ../tools/testhub.py --selftest      # automated checks, then exit
python3 ../tools/testhub.py --selftest --hid # also test keystroke injection
```

When the node connects you'll see its `hello`, live heartbeats, and any serial
output. Interactive commands: `run <cmd>`, `type <text>`, `key ENTER`,
`keys CTRL+C`, `read`, `send <text>`, `sendraw <hex>`, `ping`, `config <ms>`,
`seq`, `reboot`, `selftest`.

`--selftest` checks heartbeat, ping/pong RTT, `read`, `send` (serial write, on
nodes advertising `serial_tx`), `config`, and graceful handling of an unknown
command, printing PASS/FAIL. Add `--hid` to also test `type`/`keys`. Like `read`,
`send` has no HID side effect — its bytes go to the serial channel, not the
keyboard — so it is in the default safe subset.

> **Watch where the keystrokes go.** `type`/`keys`/`seq` inject over USB into
> **whatever machine the Pico is plugged into**. For a clean test, plug the Pico
> into a spare machine or VM with a text field focused, and run `testhub.py` on a
> different box. The ping/read/config/heartbeat checks have no HID side effects.

## Observing a deployed node (no console)

Once deployed — USB to the target, Ethernet to the switch — there's no port left
for a serial console. Two channels replace it.

### 1. The onboard LED — problems *before* the hub is reachable

The firmware drives the Pico's onboard LED so you can read the node at a glance:

| LED | Meaning |
|---|---|
| **solid on** | connected to the hub, healthy |
| **slow blink** (~1 Hz) | powered + networked, but not connected to the hub (retrying) |
| **medium blink** (~2.5 Hz) | Ethernet/link problem — can't bring the network up |
| **fast blink** (~5 Hz) | fatal config or HID error (bad `settings.toml`, HID not enabled) |

The W5100S HAT's own RJ45 link/activity LEDs separately confirm the physical
Ethernet link.

### 2. The hub — everything *after* it connects

The node reports home, so the hub is your log. In the dashboard's **Events** feed
(or `GET /api/events`, or `journalctl -u swarm-hub -f` on the hub) you see:

- **node_up / node_down** — registration, disconnect, and stale-sweep offline.
- **heartbeat + RTT** — live liveness; a node that dies stops pulsing.
- **error events** — the node sends an `error` frame for anything it wants on the
  record: an unknown command, a protocol desync, and **"recovered from watchdog
  reset"** after the loop hung and the watchdog restarted it.
- **command results** — every command's `ok`/`failed`, and on failure the detail
  (e.g. `unknown keycode: 'FOO'`, or an unmappable character). Filter the Events
  feed or command history by the node's id to see exactly what failed.

So: a misbehaving node shows up as failed results + error events against its id; a
dead one shows as node_down and silence. Repeated "recovered from watchdog reset"
means it's hanging — check power and the attached target.

### 3. Optional: `/error.txt` on the filesystem

For detail from a node that can't reach the hub — config errors, HID failures,
Ethernet-init failures, watchdog reset loops, crashes — the firmware can append
them to `/error.txt`, which you read by unplugging the Pico and plugging it into
any computer.

Turn it on with `LOG_TO_FILE = "true"` in `settings.toml`. `boot.py` then remounts
the filesystem writable to CircuitPython and `code.py` logs faults there (the file
self-truncates past ~32 KB).

**The trade-off:** a filesystem writable to CircuitPython is **read-only over USB**,
so `build.sh` / `deploy-zip.sh` drag-drop updates no longer work while it's on. You
can still *read* the drive (that's the point), just not write it. To update or turn
logging back off, **re-flash CircuitPython** (hold BOOTSEL, drag the `.uf2`) — that
resets the filesystem to host-writable — then redeploy with `LOG_TO_FILE = "false"`.

So: leave it off normally (the hub + LED cover the usual cases); switch it on only
when you're actively chasing a fault on a node you can't attach a console to.

### Before you deploy

Bench-test each node headless first: point its `HUB_IP` at your laptop and run
`python3 ../tools/testhub.py --selftest`. If that passes, the node is good before
it ever goes dark.

## Troubleshooting

- **Board boots to safe mode, or the keyboard/serial port never appears on the
  target.** You've likely exhausted USB endpoints (keyboard HID + two CDC channels
  + the mass-storage drive is a lot for the RP2040). Uncomment
  `storage.disable_usb_drive()` in `boot.py` to drop the drive and free endpoints —
  this also hides the `CIRCUITPY` drive from the target, which you usually want on a
  node plugged into a managed machine. Edit files via a dev machine or the REPL
  afterward.
- **`HID ERROR: no USB HID devices`.** `boot.py` didn't enable the keyboard, or you
  edited it and only did a soft reset. Power-cycle so `boot.py` re-runs.
- **`CONFIG ERROR: missing required setting`.** Add the named key to `settings.toml`
  and reset.
- **`ethernet init failed` on a loop.** Check the HAT seating, the SPI pins in
  `netlink.py`, and (for DHCP) that a lease is available. The node retries every
  3 s.
- **Connected, but no serial output in the console.** The target isn't emitting to
  the serial port — see "The real limitation" above. Typing still works.
- **Symbols type wrong.** The character-to-keycode mapping is layout-specific and
  defaults to US. If the target is set to a non-US layout, set `KEYBOARD_LAYOUT`
  (e.g. `"de"`, `"uk"`, `"fr"`) in `settings.toml` and make sure the matching
  `keyboard_layout_win_<code>` library is in `lib/`; otherwise the node logs a
  warning and falls back to US. The named-chord path (`keys`) is unaffected. See
  "Keyboard layout" below.

## A note on MicroPython

This firmware is CircuitPython specifically because of the composite USB device:
CircuitPython exposes an HID keyboard **and** a dedicated CDC *data* channel
(separate from the REPL) natively. On MicroPython the second data CDC requires a
custom composite descriptor and a firmware rebuild; its built-in CDC is the REPL,
so the target's serial port would be the node's Python prompt rather than a clean
data channel.
