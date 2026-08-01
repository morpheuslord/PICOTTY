# Dashboard UI changes

All work is confined to `hub/static/`. Additive and non-breaking: every existing
flow (HID/serial composer, macros, events, settings, DEMO mode) is unchanged.
Verified with `node --check hub/static/app.js`.

Files touched: `app.js`, `index.html`, `styles`/`app.css` (no CSS change needed),
plus new `vendor/fetch-vendor.sh`, `vendor/README.md`, this file.

## Phase 1 — Layout display (read-only)
- `renderHeaderInto`: node detail header now shows `kbd: <layout>` from the node's
  `layout` field.
- Carried into the client node record in `apiNodeToRec` and `mergeMeta`
  (`layout`, default `"us"`).

## Phase 2 — Serial composer polish (verify)
- Verified `buildSerialComposer` already provides Enter (CR `\r`=0d), Backspace
  (`7f`), Tab (`\t`=09), Esc (`1b`), Ctrl-C (`03`), Ctrl-D (`04`), Ctrl-Z (`1a`),
  the HID⇄Serial `buildModeRow` toggle gated on `serial_tx`, debounced key
  streaming and paste. No gaps found; left unchanged.

## Phase 3 — xterm.js console (progressive enhancement)
- New helpers: `xtermAvailable`, `xtermActive`, `setupXterm`, `disposeXterm`,
  `xtermReplay`, `xtermMeta`, `mountConsole`, `refreshConsole`.
- `buildCenter` mounts an xterm `Terminal` + fit addon into `ui.xtermHost` **only
  when `window.Terminal` exists**; otherwise the original DOM-log `ui.term`
  surface is used unchanged.
- `pushOutput` writes raw bytes to `term.write(...)` when active (DOM-log path
  otherwise); `pushLine` mirrors sys/in/err lines into xterm with ANSI prefixes.
- Serial mode wires `term.onData(d => postSend(id,{data:d}))`; HID mode leaves the
  terminal read-only.
- `renderView` disposes the terminal before clearing; a window `resize` listener
  refits. `selectNode`/`backfillNode`/`clearConsole` now call `refreshConsole`.
- `index.html` adds guarded `vendor/xterm.css`, `vendor/xterm.js`,
  `vendor/xterm-addon-fit.js` (404 harmless until vendored).

## Phase 4 — Prompt-state badge
- New `promptBadge()` + `PROMPT_BADGE` palette (panic=red, shell=green,
  login/password=amber, grub/booting=blue, null=none).
- Rendered per row in `renderNodeList` and (larger) in the detail header.
- `onEvent` handles `node_state` → updates `n.promptState` and re-renders.
- Carried in `apiNodeToRec`/`mergeMeta`; demo nodes seeded with states.

## Phase 5 — Expect builder + live progress
- `openExpectBuilder(nodeId)` modal: ordered steps mixing **send** actions,
  **wait-for** rows (regex + timeout_ms + on_timeout fail|continue) and **delay**
  rows; reorder/remove; `POST /nodes/{id}/expect {steps:[...]}`.
- `ui.expectBar` + `renderExpectBar` shows a live progress strip (step/total,
  phase, detail) under the console with Cancel/Dismiss.
- `onEvent` handles `expect_progress`; `cancelExpect` → `POST …/expect/{job}/cancel`.
- Opened from a new **Expect** button in the header actions.

## Phase 6 — Offline queue
- `openQueueSheet(nodeId)`: lists pending (`GET /nodes/{id}/queue`), adds a
  type-command with TTL (`POST …/queue {command,ttl_ms}`), cancels
  (`DELETE …/queue/{qid}`). New **Queue** header button.

## Phase 7 — Session replay
- `openReplay(nodeId)`: if `window.AsciinemaPlayer` is vendored, plays
  `/api/nodes/{id}/session.cast` inline; otherwise offers a `.cast` download link
  + `asciinema play` hint. New **Replay** header button; guarded
  `vendor/asciinema-player.*` in `index.html`.

## Phase 8 — Serial bridge info
- Header meta shows `serial bridge: <bind>:<port>` when assigned.
- `openBridgeSheet(nodeId)`: assign/unassign (`POST/DELETE /nodes/{id}/bridge?port=N`)
  with a `minicom -D tcp:<hub>:<port>` hint. New **Bridge** header button.
- `refreshBridge()` + `state.bridge` loaded in `loadLive`.
- Settings gains a `serial_bridge_enabled` toggle.

## Phase 9 — Runbooks view
- New nav entry **Runbooks** (between Macros and Events) + router case.
- `buildRunbooksView`: list, YAML `<textarea>` editor (`openRunbookEditor`,
  create/patch/delete), run modal (`openRunbookRun` → `POST /runbooks/{id}/run`
  with node_ids or group + stagger_ms), and a live runs pane.
- `onEvent` handles `runbook_progress`; `refreshRunbooks` + `state.runbooks`
  loaded in `loadLive`. Placeholder YAML matches the spec.

## Phase 11 — Alerts settings
- Settings adds `alerts_enabled` toggle and `alerts_webhook_url` /
  `alerts_ntfy_url` text inputs → `PATCH /settings`.

## Phase 12 — OTA firmware updates
- `nodeHasOta(n)` mirrors `nodeHasSerialTx` (checks the node's `caps` for `ota`).
  A **Firmware** button appears in the node header only for ota-capable nodes.
- **Per-node update** (`openOtaSheet`): pick a bundle (radio list from
  `state.bundles`), `POST /nodes/{id}/ota`, and watch live progress. Progress is
  driven by the `ota_progress` WS event (handled in `onEvent`, mirrored into
  `state.otaJobs[nodeId]`) with `pollOta` polling `GET /nodes/{id}/ota/{job_id}`
  as a backup. Terminal states are shown clearly: **healthy** (green success),
  **failed** and **unconfirmed** (red).
- A compact live **OTA bar** (`renderOtaBar` → `ui.otaBar`) sits beside the expect
  bar in the console view: bundle name, byte progress bar (`sent/total`), phase/
  status text, and a Dismiss button on terminal states.
- **Bundle manager + rollout** (`openBundleManager`, reached via the sheet's
  "Manage bundles…"): create a bundle from a name + `<input type=file multiple>`
  (files read with `FileReader` as base64, prefix stripped, `POST /ota/bundles`);
  list existing bundles with their files + short sha; and a **canary rollout**
  control (pick ota-capable nodes or a group + stagger → `POST /bulk/ota`) with a
  note to follow each node's progress bar.
- `state.bundles` / `state.otaJobs` added; bundles loaded in `loadLive`
  (`GET /ota/bundles`) and `applyDemo`. `refreshBundles` re-fetches after create.
- DEMO: demo nodes gain the `ota` cap, a demo `fw-1.1.0` bundle is seeded, and
  `runOtaDemo` simulates a byte-streamed flash → committing → reboot → healthy so
  the whole OTA UI (including rollout) is exercisable offline.

## DEMO mode
- Demo nodes carry `layout`/`promptState`; a demo runbook and empty bridge added.
- Expect / queue / bridge / runbook actions have demo simulations so the UI is
  never blank and every affordance is exercisable offline.

## Operator step (vendoring — optional)
The xterm and asciinema libs are **not** committed and **not** fetched at runtime
(hub is on an isolated VLAN). To enable the enhanced console + inline replay:

```sh
cd hub/static/vendor && sh fetch-vendor.sh   # on a networked machine
# then copy hub/static/vendor/ onto the hub
```

Until then the tags 404 harmlessly and the UI falls back to the existing DOM-log
renderer and a `.cast` download link. See `vendor/README.md`.

## Help system — in-app docs + tooltips + `?` deep-links
Additive and non-breaking; DEMO mode still works. `node --check hub/static/app.js`
passes and `help.html` is balanced HTML.

Files touched: `app.js`, `index.html` (unchanged — help is reachable directly at
`/help.html`), and a NEW self-contained `help.html`.

### New page — `hub/static/help.html`
- A single, self-contained docs page. Links `styles.css` for the shared palette
  (CSS custom properties), then an inline `<style>` for a sticky left TOC/sidebar
  + scrollable content column. Responsive (sidebar collapses under 820px). No CDN
  / no external assets — everything inline or same-origin (isolated VLAN).
- Top "← Back to dashboard" link (`/`) in the sidebar and again at the foot of the
  Security section; a grouped table of contents jumps to every section.
- One `<section id="…">` per feature, each with prose adapted from `docs/*.md`
  (operations, automation, ota, firmware, considerations) plus the real REST
  endpoint(s) from `hub/app/api/rest.py`, gotchas and safety notes. Addresses and
  secrets are placeholders (`<hub-host>`, `<password>`, `<your-endpoint>`).
- **Stable anchor ids:** `overview`, `nodes`, `console`, `input-modes`,
  `control-bytes`, `keyboard-layout`, `ping-read-reboot`, `prompt-state`,
  `macros`, `sequences`, `expect`, `offline-queue`, `runbooks`, `bulk`,
  `session-recording`, `serial-bridge`, `ota`, `alerts`, `settings`, `events`,
  `security` (21 sections).

### `app.js` — shared helper
- New `helpLink(anchor, label, opts)` returns a small circular muted "?" `<a>`
  (`href="help.html#<anchor>"`, `target="_blank"`, `rel="noopener"`,
  `var(--color-neutral-600)`, ~15px, `cursor:help`). It `stopPropagation()`s so it
  never triggers the control it sits beside, and seeds its own hover tooltip.
  Reused everywhere (27 call sites).

### `app.js` — where the `?` links land (group- + key-button-level)
- **Top nav:** a new **Help** entry (after Settings) opens `help.html` in a new
  tab, with a trailing "?" glyph.
- **Panel/section headers:** Nodes rail, Serial console toolbar, right-rail
  history/events tabs, Macros header, Runbooks header, Events header, Settings
  header + Safety + Alert-endpoints sub-headers, and the composer's Input row.
- **Feature buttons in the node detail header:** Ping/Read/Reboot, Expect, Queue,
  Replay, Bridge, and Firmware (OTA) each get an adjacent anchor-specific "?".
- **Composer rows:** HID Keys/Chords and the Serial control-byte row link to
  `#control-bytes`; the Macros row links to `#macros`.
- **Modal sheets:** Expect, Queue, Bridge, OTA update + rollout bodies carry an
  inline "?" to their section.

### `app.js` — tooltips (`title=`) on every interactive control
- ~60 controls now carry a concise `title`: nav links + rail toggles + WS pill;
  node-list filter/status radios and each node row; console toolbar
  (autoscroll/wrap/clear/download); all detail-header actions (with caveats, e.g.
  "Reboot the Pico node (NOT the target machine)"); HID keys (per-key intent),
  chords (incl. ⚠ for CTRL+ALT+DEL and ALT+SysRq+B), append-⏎ and char-delay,
  macro chips; Serial field + each control byte (with hex); macro/runbook
  Run/Edit/Delete/New/Save; Events Export CSV; Settings Save; and the sheet
  primary actions (Run expect, Queue, Assign/Reassign/Unassign bridge, Update
  firmware, Create bundle, Start rollout, Run runbook).
