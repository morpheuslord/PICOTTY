# Changelog

All notable changes to PICOTTY. This project adheres to [Semantic Versioning](https://semver.org).

## v1.2.0 — 2026-09-06

Building on 1.1.0's dual-hub work: **OTA** gains firmware-only / settings-only
pushes plus a big speed-up, and the **Telegram bot** becomes button-driven.

### Added — OTA firmware-only / settings-only + faster, more resilient pushes

- **Scoped pushes.** An OTA push now takes a **scope**: `all` (default),
  `firmware` (everything *except* `settings.toml` — update the code, keep each
  node's existing config) or `settings` (only `settings.toml` — change config,
  keep the code). Exposed in the dashboard's Update-firmware sheet, on
  `POST /api/nodes/{id}/ota` and `/api/bulk/ota` (`scope`), and in the OTA manager.
  Works with existing nodes — the node writes exactly the files the hub sends, so
  no firmware change is needed. (Tip: a `settings`-only push is how you retune a
  board's `HUB_FAILOVER_TRIES`/timings without reflashing code.)
- **~8× faster.** The OTA chunk size went from 512 B to 4096 B (still well under
  the node's 16 KB frame cap), cutting round-trips.
- **Resilient transfer.** A transient link drop mid-transfer now retries the whole
  transfer (safe — `ota_begin` re-wipes staging and nothing is swapped until
  commit) instead of failing the push; commit is still one-shot, checksum-verified,
  with `.bak`/watchdog auto-revert.

### Added — Telegram button UI (picotty-telegram 1.3.0)

The bot is now **tap-driven**, not type-driven. **`/menu`** (and `/start`) opens an
inline-keyboard home screen — Status, Nodes, Telemetry, Events, Fleet, Alerts,
Source hub, Arm — and everything drills down by tapping:

- **Nodes → node detail** with action buttons: Ping, Read, Telemetry, Log, Mute,
  and (when armed) Shell, Reboot (confirm step), SysRq (key picker), Move hub.
- **Fleet** → Macros / Runbooks as buttons, then a "run on ALL online / this node"
  target picker.
- **Source hub** switch and per-node hub steering are button flows too.
- **Arm** by tapping 🔒 then sending your TOTP code (no `/arm` needed).
- **Context memory**: the bot remembers your selected node and the pending prompt,
  so buttons act without re-typing ids; the home screen shows live hub/online/armed
  status. Every typed command still works.

### Changed — clearer hub-switch controls

The dashboard's dual-hub control is now a prominent **⤓ Take over** button when a
board is live on a peer hub, alongside a clearly-labelled **⇄ Switch hub** control
(it was previously just an unlabelled "Hub" button).

### Fixed — Magic SysRq chord

The **Alt+SysRq+B** quick-chord (and any custom chord naming SysRq) was sent as a
plain HID chord, which failed on the node with `unknown keycode: 'SYSRQ'` (SysRq is
the PrintScreen key, and it must be held while the command key is tapped). Such
chords now route to the dedicated `/sysrq` command, which does the correct timing.
Also fixed `HubClient.sysrq()` sending the key under the wrong field (`command`
instead of `key`), so a Telegram `/sysrq <node> <key>` no longer silently defaulted
to `b` (reboot).

### Verified

Hub 7/7 db + **61/61** integration (incl. OTA firmware/settings scope + empty→422);
sidecar **28/28** unit (incl. the button-menu callback scheme) + wiring smoke; no
regressions.

## v1.1.0 — 2026-08-22

A board can now be served by **two hubs** with automatic failover and runtime
source-selection, and the **Telegram bot** gains a much larger command surface.

### Added — dual-hub failover

Give a node a **primary** and a **backup** hub. It prefers the primary, fails over
to the backup when the primary is unreachable, and **either hub can drive the
board** — so a dead main hub no longer means losing the fleet. See
[docs/dual-hub.md](docs/dual-hub.md).

- **Independent peers.** The two hubs run independently, each with its own SQLite
  DB; they share only the **node token**. Failover moves the live control
  connection, not the data — a board holds exactly one connection at a time, so no
  split-brain and no cross-hub coordination. Per-hub data (queue, recordings, OTA
  jobs, history) stays on the hub that recorded it.
- **Node config** ([settings.toml](firmware/circuitpython/settings.toml.example)):
  `HUB_IP_BACKUP`/`HUB_PORT_BACKUP`, display labels `HUB_LABEL`/`HUB_LABEL_BACKUP`,
  `HUB_FAILBACK` (`sticky` default / `preemptive`), `HUB_FAILOVER_TRIES`. Absent
  backup = today's single-hub behavior, fully backward compatible. The failover
  state machine lives in a new, host-testable
  [`hubselect.py`](firmware/circuitpython/hubselect.py).
- **Runtime steering.** Each hub advertises its identity to a board on connect (a
  `welcome` frame carrying the new `HUB_ID`), so the board knows **which hub it's
  talking to**. An authenticated hub can then send a directive — **switch** (move
  now), **prefer** (promote to home hub, persisted — "use my backup as my main"),
  **pin**/**unpin** — restricted to the board's configured hubs. Drive it from the
  dashboard's per-node **Hub** control, `POST /api/nodes/{id}/hub`, or Telegram.
- **Hub:** new `HUB_ID` env (default hostname), shown on the dashboard and sent in
  the welcome frame; a node's reported hub label is surfaced in the node API and on
  the dashboard (a "via &lt;label&gt;" badge). Node firmware bumped to **1.3.0**.

### Added — richer Telegram bot (picotty-telegram 1.2.0)

Surfaced hub capabilities that were REST-ready but had no chat command:

- **Read-only** (allowlist tier): `/events [n]` (recent audit), `/log <node>
  [lines]` (recent console output), `/search <text>` (search console history),
  `/ping <node>` (active RTT).
- **Fleet** (break-glass **armed** tier): `/bulk <text>` (type a line into every
  online node), `/macros` + `/runmacro <id> [node ...]`, `/runbooks` + `/runbook
  <id> <node ...|all|group:NAME>`.
- **Dual-hub:** `/hubs` (which hub each board is on) and `/hub <node>
  <switch|prefer|pin|unpin> [target]` (armed).
- New `picotty.client` SDK methods back these (`node_output`, `output_search`,
  `ping`, `bulk_cmd`, `macros`/`run_macro`, `runbooks`/`run_runbook`,
  `hub_directive`), so any client — not just the bot — can use them.

### Added — cross-hub takeover & peer visibility

A board holds one hub connection at a time, so only the hub that holds it can
steer it. New **`HUB_PEERS`** (comma-separated peer hub URLs) closes the gap:

- **Peer visibility.** Each hub polls its peers' rosters and shows a board that's
  live on the other hub as **"active on `<peer>`"** (a badge) instead of offline —
  even a board it has never seen.
- **On-demand takeover from either hub.** Steering a board the hub doesn't hold
  **relays** the directive to the peer that does, which delivers it — so you can
  pull a board to the backup from the backup's own dashboard while it's still on
  the primary. Relayed calls are never relayed again (loop-safe). Peer calls
  assume a trusted management VLAN (no auth), matching the node-token posture.

### Added — Telegram sidecar dual-hub

- **The bot fails over between hubs.** Set `HUB_BASE_URL_BACKUP` and the sidecar
  prefers the primary hub, failing over to the backup (REST + the live event
  stream) so the phone control plane survives a hub outage.
- **`/source [primary|backup]`** picks which hub the bot acts on (distinct from
  `/hub`, which steers an individual board). Telegram permits only one active
  receiver per token, so the intended HA is: run the same bot on both hosts with
  one enabled and the other a hot spare — see [docs/dual-hub.md](docs/dual-hub.md).

### Fixed — responsive dashboard

The dashboard (a fixed-width desktop layout) now adapts to phones, tablets, and
any window aspect ratio: the side rails are fluid on desktop and the three columns
stack with the console kept usably tall on narrow screens, with the page scrolling
instead of clipping. The node header is a vertical stack (info above a wrapping
action toolbar) so the info no longer collapses under the buttons.

### Verified

Firmware selector **12/12** host unit; hub 7/7 db + **58/58** integration (welcome
frame, hub-label, directive endpoint) + **5/5** two-hub relay (cross-hub takeover,
peer visibility, loop guard); sidecar **27/27** unit (hub-failover + source select)
+ wiring smoke + **18/18** end-to-end through `picotty.client`. No regressions.

## v1.0.3 — 2026-08-12

A reliability pass on node presence: nodes no longer get stuck showing offline
while alive, both ends recover from a dropped link on their own, and the
dashboard now surfaces link quality and node activity so a degrading node is
visible before it drops.

### Added — link telemetry & activity monitoring

Precautionary observability so a degrading node is visible before it drops:

- **Network telemetry.** The hub's per-node ping now feeds a rolling window that
  yields **RTT avg/min/max, jitter** (mean variation between consecutive pings),
  and **packet loss %**. Surfaced per node in the API and dashboard, with a
  live `node_net` event and a colour-coded **link quality** badge (good/fair/poor).
- **Activity / uptime.** Nodes report firmware **uptime** in the heartbeat; the
  hub shows it and raises a `node_down` event when it jumps backwards (an
  unannounced reboot). The hub also counts **reconnects** per node this run, so a
  flapping link is obvious even while the node reads "online".

### Fixed — nodes falsely shown offline (stuck-offline / reconnection)

A node that was alive and networked could show **offline** in the dashboard
indefinitely, only recovering when the hub was restarted (no node restart
needed). Root cause: when the hub's liveness sweep flipped a stale node offline
it removed it from the registry **without closing the socket**, so the still-open
connection kept delivering the node's frames onto a detached state — the node
never re-registered. Hardened both sides of the link:

- **Hub — sweep now tears down the socket.** `mark_offline` closes the node's
  writer, forcing a reconnect instead of a lingering half-open connection.
- **Hub — TCP keepalive** on accepted node sockets, a kernel-level backstop for a
  peer that vanished without a FIN.
- **Hub — node keepalive pinger** (`HUB_NODE_PING_INTERVAL_MS`, default 5s): the
  hub→node half of the heartbeat, so an idle node keeps receiving traffic and a
  silent node is swept promptly.
- **Node — dead-hub detection** (`HUB_TIMEOUT_MS`, default 20s): reconnect if no
  frame arrives from the hub within the window, catching a half-open socket where
  the node's own sends still buffer locally.
- **Node — bounded send** (`SEND_MAX_WAIT_MS`, default 2s): a wedged TX buffer now
  reconnects instead of spinning until the watchdog resets the whole node.
- **Node — DHCP re-acquire** (`REBIND_AFTER_FAILURES`, default 5): after repeated
  connects that never reached the hub, re-init the interface to pull a fresh
  lease — recovers a node that powered up before its DHCP server (site power cut)
  or whose lease went bad.

Node firmware bumped to **1.2.0**. The node pinger and dead-hub timeout are a
matched pair: run the updated hub alongside 1.2.0 firmware (or set
`HUB_TIMEOUT_MS = 0` to pair 1.2.0 nodes with an older, non-pinging hub).

### Telegram sidecar — `/telemetry` (picotty-telegram 1.1.0)

- **`/telemetry`** surfaces the new link telemetry over chat: bare, it prints a
  per-node roster (rtt avg, jitter, loss %, a good/fair/poor quality rating, and
  reconnect count); `/telemetry <node>` gives that node's detail plus firmware
  uptime. Requires a hub on **1.0.3+** (the fields it reads); the sidecar's
  `picotty` floor is bumped accordingly.

## v1.0.2 — 2026-08-05

First packaged release. (Versions 1.0.0 and 1.0.1 were burned on PyPI by the
file-name-reuse policy after deleted test uploads; 1.0.2 is the first published
version.) PICOTTY is a star-topology **networked serial console with
USB-HID keyboard injection** for a fleet of headless machines: each Raspberry Pi
Pico node plugs into a target's USB port and becomes a keyboard **and** a serial
console reader, and one Pi Zero 2 W hub coordinates the swarm through a single
browser dashboard over your management network. Built for homelab mini-PCs that
ship with **no BMC/IPMI and no accessible serial port** — this is the lights-out
management they never came with.

This release turns the repo-clone deployment into an installable, versioned
distribution and adds a phone control plane.

### Highlights

- **`picotty` Python package (uv)** — `uv tool install picotty` brings up the hub;
  three import surfaces (`picotty.hub`, `picotty.client`, `picotty.protocol`).
- **Interactive serial write** — a real terminal (xterm.js) in the browser, typing
  straight into the target's serial getty, alongside HID keyboard injection.
- **Telegram bot sidecar** — stats, push alerts, and a break-glass terminal on your
  phone, configured entirely from the dashboard.
- **OTA firmware updates** — chunked, checksummed, `.zip`-upload bundles with canary
  rollout and watchdog-revert.
- **Automation** — a wait-for-output expect engine, YAML runbooks, and an offline
  command queue.

### The hub — the `picotty` package

- Repackaged as the **`picotty`** distribution built with the `uv_build` backend
  from a `src/` layout, with a committed `uv.lock` for reproducible installs.
- **Three import surfaces:**
  - `picotty.hub` — the server (registry + SQLite + `:9000` TCP + FastAPI dashboard); needs the `[hub]` extra.
  - `picotty.client` — the SDK: `HubClient` (async REST) + `HubEvents` (WebSocket async-iterator); lean base install (httpx + websockets).
  - `picotty.protocol` — the wire protocol: framing, validation, `PROTOCOL_VERSION`.
- **Console entry points:** `picotty-hub` (the server) and `picotty-sim` (the node
  simulator — a fake node for demos/tests, no hardware).
- **Lean-by-default install** with extras: `[hub]` pulls FastAPI/uvicorn/aiosqlite/
  pydantic/pyyaml; `[telegram]` pulls the sidecar deps. A Pi running only the
  sidecar never installs FastAPI.
- **Runtime state out of the tree** — SQLite defaults to `~/.local/share/picotty/`
  (a systemd `StateDirectory` gets `/var/lib/picotty`); the dashboard's static
  assets ship inside the wheel (`importlib.resources`).

### Node firmware (CircuitPython, RP2040)

- One node per target: reads the target's serial console back over the network and
  types keystrokes (BIOS, GRUB, initramfs, the OS) as a USB HID keyboard.
- LED status codes, opt-in `/error.txt`, and a REPL debug path keep a monitorless
  node diagnosable; a hardware watchdog recovers a hung loop.
- Per-node keyboard layouts, target-machine liveness reporting, and a `serial_tx`
  capability that gates the interactive write path.

### Dashboard (Swarm Control)

- Rebuilt around a real terminal renderer with an **HID ⇄ Serial** input toggle,
  per-node **prompt-state** badges, and a **machine up/dead** liveness badge (is the
  *target* alive, not just the node).
- **Reboot-machine menu** with three methods (serial `reboot`, Ctrl+Alt+Del, Magic
  SysRq `Alt+SysRq+B`), **custom quick chords**, macros, and bulk/fleet actions.
- Author, view, **and edit** YAML runbooks in the browser; event history and audit;
  hover hints on every control with a docs "?" deep-link, plus an in-app help page.

### Automation

- **Expect engine** — wait-for-output flows with per-step regex + timeouts.
- **YAML runbooks** — expect flows run across a node group.
- **Offline command queue** — commands queued for an offline node deliver on
  reconnect (once, guarded against double-delivery).

### OTA firmware updates

- Push firmware over the wire: chunked transfer, **SHA-256 verify**, staged writes
  with a `.bak` backup and a `/ota_pending.json` marker, **watchdog-revert** in
  `boot.py`, and finalize-when-healthy.
- Upload a firmware **`.zip`** the hub decompresses into a bundle; **canary rollout**
  with per-node provenance (`last_ota`).

### Operations

- **Raw serial bridge** — expose an assigned node's serial as a TCP port for
  `minicom`/PuTTY.
- **Alerting** — outbound webhook / **ntfy** on node down, watchdog recovery, and
  command failures, with dedup.
- **Session recording** — asciicast capture and in-browser replay.

### Telegram bot sidecar (new)

- A **separate process** that reaches the hub only via REST + `/ws` and bridges it to
  Telegram over **outbound-only** long polling (no inbound port) — usable from an
  isolated management VLAN. Depends on `picotty[telegram]`, imports `picotty.client`.
- **Three tiers:** stats (`/status` `/nodes` `/uptime`), push alerts (node down/up,
  watchdog, failed, hub-restart; `/mute`), and a terminal bridge (`/shell` + control
  keys, `/reboot`, `/sysrq`) gated behind a chat-ID allowlist **+ break-glass TOTP
  arming** with auto-disarm and idle-close.
- **Dashboard setup** — Settings → Telegram card validates the bot token via
  Telegram `getMe`, generates a TOTP secret, and one-click **Install / start
  sidecar**. The hub and sidecar share one credentials file
  (`~/.config/picotty/telegram.env`, chmod 600) that the sidecar **hot-reloads**.

### Packaging & release

- `uv sync` / `uv run` dev workflow; `uv build` produces the wheel + sdist.
- **GitHub-Release-triggered publish** to PyPI via Trusted Publishing (OIDC, no
  token) — publishing a release builds and uploads the distributions.
- CI runs the hub (`test_db`, `test_integration`) and sidecar (unit, smoke) suites;
  workflows use least-privilege permissions.

### Tooling

- **`tools/package_tester.py`** — a live-hub smoke test to run on the hub host after
  install: exercises REST + WebSocket + a node round-trip end to end.

### Docs

New/updated: [packaging.md](docs/packaging.md), [telegram.md](docs/telegram.md),
plus architecture, hardware, deployment, firmware, operations, automation, and ota.

### Install

```bash
uv tool install picotty        # picotty-hub + picotty-sim on PATH
picotty-hub                    # dashboard at http://<hub-ip>:8080
```

From a source checkout: `bash hub/scripts/install.sh` (uv) then
`bash hub/scripts/install-service.sh`.

### Notes & known limitations

- **Serial console, not a KVM** — no video capture; reading output requires the
  target to have a serial console configured.
- Run the hub under **one** uvicorn worker (the single event loop is the design).
- The **CircuitPython node library** (installable `.mpy` bundles + circup) is
  scaffolded and documented ([node/README.md](node/README.md)) but **deferred**;
  the authoritative firmware today is `firmware/circuitpython/`.
- The management network is **not** an authenticator — the node token applies on
  every connection; auth on the dashboard is optional and off by default (assumes an
  isolated VLAN reached over VPN/tunnel).

### Verified

Hub wheel builds clean; 7/7 db + 44/44 integration checks; sidecar 14/14 unit +
wiring smoke + 9/9 end-to-end against a real hub through `picotty.client`; live
`tools/package_tester.py` green on a Pi Zero 2 W.
