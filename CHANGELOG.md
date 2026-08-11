# Changelog

All notable changes to PICOTTY. This project adheres to [Semantic Versioning](https://semver.org).

## Unreleased

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
