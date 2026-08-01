[← Docs index](README.md) · [← Project README](../README.md)

# Automation: expect, queue, runbooks

- [Why automation lives on the hub](#why-automation-lives-on-the-hub)
- [Prompt-state detection](#prompt-state-detection)
- [The expect engine](#the-expect-engine)
- [Offline command queue](#offline-command-queue)
- [Runbooks](#runbooks)

## Why automation lives on the hub

A node is deliberately dumb. It types, it reads the serial line, it echoes a
`cmd_id`, and that is all — every byte of *decision* is made on the hub. That
split is what makes the automation here safe to add: none of it changes the wire
protocol or the firmware, so it works against any node already in the rack,
including one running old firmware, as long as that node can emit serial output.

All three features below build on one shared piece: the **rolling output tail**
the hub keeps per node. Every `output` chunk a node sends is appended to a
bounded (~4 KB) per-node buffer in the registry. Prompt-state detection
classifies that buffer; the expect engine waits on it; runbooks drive the expect
engine. One buffer, three consumers.

## Prompt-state detection

The dashboard shows online/offline, but for a fleet you also want to know *where*
each target is sitting: at GRUB, at a login prompt, mid-boot, panicked, or booted
to a shell. The hub computes that from the output stream — no firmware change.

**How it works.** `hub/app/classifier.py` holds an ordered list of
`(state, regex)` pairs. On every output chunk, `tcp_server` appends to the node's
tail and runs the classifier over the **freshest ~512 bytes** of it (so a stale
prompt far above doesn't outvote fresh output). First match wins, so the list is
ordered most-urgent/most-specific first:

| State | Recognizes | Regex intent |
|---|---|---|
| `panic` | Kernel panic / oops / call trace | `Kernel panic`, `BUG: unable to handle`, `Oops:`, `Call Trace:` |
| `grub` | Bootloader menu | `GRUB`, `Minimal BASH-like line editing`, `Press ENTER to boot`, `GNU GRUB` |
| `password` | Password prompt | `[Pp]assword:` at end of line |
| `login` | Login prompt | `login:` at end of line |
| `shell` | Interactive prompt | a line ending in `$`, `#`, or `>` |
| `booting` | Boot chatter | `[ OK ]`, `systemd[1]`, `Reached target`, kernel `[  n.nn]` timestamps |

`password` is checked before `login`, and both before `shell`, so a trailing `:`
on a prompt line doesn't get read as a shell prompt.

The state lives on `NodeState.prompt_state` — **registry only, never persisted**,
exactly like `rtt_ms`. When it changes, the hub broadcasts a small event to
*every* browser (not just console subscribers) so node badges update at a glance:

```json
{"event": "node_state", "id": "node-01", "prompt_state": "login", "ts": 1234567890}
```

REST reflects it too: `GET /api/nodes/{id}` includes `prompt_state` (null when the
node is offline). The classifier is the same matcher the expect engine reuses to
decide when a `wait_for` has matched.

## The expect engine

A `sequence` is fire-and-forget — type, delay, type. An **expect job** is the
missing half: it alternates *action* steps with *wait_for* steps that block until
a regex appears in the node's serial output within a timeout. That is what turns
PICOTTY from a keyboard into automation: "wait for `login:`, send the user, wait
for `Password:`, send the password, wait for a shell prompt."

All the waiting is hub-side (`hub/app/expect.py`); the firmware just keeps
emitting output and running the discrete `send`/`type`/`keys` commands the job
dispatches.

### The endpoints

| Method + path | Does |
|---|---|
| `POST /api/nodes/{id}/expect` | Start a job. Body `{steps: [...]}`. Returns `{ok, job_id, status}`. |
| `GET /api/nodes/{id}/expect/{job_id}` | Poll status (also streamed live — see below). |
| `POST /api/nodes/{id}/expect/{job_id}/cancel` | Cancel a running job. |

Start rejects a node that is offline (`node_offline`), a bad step list
(`bad_expect`, HTTP 422), or a node that already has a job running
(`busy`, HTTP 409). **One running job per node** is a hard rule — two jobs racing
on one output stream is undefined.

### Step shapes

Each entry in `steps` is exactly one of:

```jsonc
// action — dispatched through the ordinary command path
{"type": "send", "data": "root\n"}          // UTF-8 text into the serial line
{"type": "send", "raw": "03"}               // raw bytes as hex (03 = Ctrl-C)
{"type": "type", "text": "uptime\n"}        // HID keystrokes (optional char_delay_ms)
{"type": "keys", "chord": ["CTRL", "C"]}    // an HID chord

// a fixed pause
{"delay_ms": 500}

// wait for output — the automation primitive
{"wait_for": {"regex": "login:\\s*$", "timeout_ms": 30000, "on_timeout": "fail"}}
{"wait_for": "login:"}                        // shorthand: just a regex string
```

- `on_timeout` is `fail` (default — the job stops with status `timeout`) or
  `continue` (log it and move to the next step). Use `continue` for a prompt that
  may or may not appear, e.g. an optional "[Y/n]".
- A `wait_for` searches only output that arrived **after the preceding action**.
  The job clears its match buffer right before dispatching each action, so a wait
  can never match a stale prompt printed before the action ran.

### Bounds

Everything is bounded so a job can never run away on the shared event loop:

| Limit | Value |
|---|---|
| Steps per job | 64 |
| Regex length | 512 chars |
| Default `wait_for` timeout | 15 s |
| Max `wait_for` timeout | 120 s |
| Max total job wall-time | 600 s (10 min) |
| Search window per wait | freshest 4 KB of the tail |
| Finished-job status retained | 5 min, then pruned |

### Live progress

A running job streams `expect_progress` events over the WebSocket:

```json
{"event": "expect_progress", "id": "node-01", "job_id": "…",
 "step": 2, "total": 6, "phase": "matched", "detail": "/login:/"}
```

`phase` walks through `action` → `wait` → `matched` (or `wait_timeout_continue`),
ending in `done`, `failed`, `timeout`, or `cancelled`. The status snapshot from
`GET …/expect/{job_id}` carries the same `status`, `step`, `total`, and `detail`.

### Worked example: unattended login

Log into a fresh Linux target, then run a command:

```json
POST /api/nodes/node-01/expect
{
  "steps": [
    {"wait_for": {"regex": "login:\\s*$", "timeout_ms": 30000}},
    {"type": "send", "data": "root\n"},
    {"wait_for": {"regex": "[Pp]assword:\\s*$", "timeout_ms": 10000}},
    {"type": "send", "data": "<password>\n"},
    {"wait_for": {"regex": "[#$]\\s*$", "timeout_ms": 10000}},
    {"type": "send", "data": "uptime\n"}
  ]
}
```

A correct password lands on the shell prompt and the job reports `done`. A wrong
password never reaches `[#$]`, so the final `wait_for` times out and the job fails
cleanly on that step — no keystrokes are lost, nothing hangs. This needs the
target running a serial getty on the node's data port
(see [operations.md](operations.md#hid-input-vs-serial-input)); on a node without
`serial_tx`, use `type`/`keys` actions instead of `send` and drive `tty1`.

## Offline command queue

Dispatching to an offline node normally returns `node_offline`. But a node is
powered by its target, so it is *off* exactly when you often most want to act:
"press ENTER at GRUB" is something you want staged *before* you power the box on,
delivered the instant it dials in.

The queue is an **explicit opt-in** — the default dispatch path is unchanged, so
nothing starts silently buffering. You reach it through its own endpoints:

| Method + path | Does |
|---|---|
| `POST /api/nodes/{id}/queue` | Enqueue `{command: {...}, ttl_ms}`. Returns `{ok, id, expires_at}`. |
| `GET /api/nodes/{id}/queue` | List this node's pending commands. |
| `DELETE /api/nodes/{id}/queue/{qid}` | Cancel a pending command. |

- `command` is the same shape as a normal command body (`type`, `text`, `chord`,
  `data`/`raw`, …). A `send` command is validated at enqueue time, so a malformed
  or oversized write is rejected up front, not on delivery.
- `ttl_ms` defaults to **1 hour**; pass `0`/`null` for no expiry. `expires_at` is
  returned so you can see the deadline.
- **Delivery.** When the node next sends its `hello`, the hub drains the queue in
  issue order — *after* announcing the node online, so the dashboard shows it up
  before commands flow. Expired rows are dropped, not delivered. If the node drops
  again mid-drain, the remaining commands stay pending for next time.
- If the node happens to already be online when you enqueue, the hub drains
  immediately, so the endpoint is also a fine "fire it now, or as soon as it's
  back" primitive.

Queued commands live in the durable `queued_commands` table, so a hub restart
does not lose them.

## Runbooks

A macro is a single sequence on one node. A **runbook** is the fleet-scale
version: a named, durable list of expect steps run across a whole node group, with
per-node staggering and a live progress view. It sits directly on top of the
expect engine — each target node gets its own `ExpectJob` driven by the runbook's
steps (`hub/app/runbook.py`).

### YAML schema

A runbook is a YAML mapping with a `name` and a non-empty `steps` list. Each step
is one of the following keys, and they translate 1:1 onto expect-engine steps:

| Key | Shape | Maps to |
|---|---|---|
| `wait_for` | a regex string, plus optional `timeout_ms` / `on_timeout` | expect `wait_for` |
| `send` | text, or `{raw: "0d"}` for hex bytes | expect `send` action |
| `type` | text (HID) | expect `type` action |
| `keys` | `"CTRL+C"` (or a list) | expect `keys` action |
| `delay_ms` | integer | expect `delay` |

```yaml
name: log-in-and-check
steps:
  - wait_for: "login:"
    timeout_ms: 30000
  - send: "root\n"
  - wait_for: "[Pp]assword:"
  - send: "<password>\n"
  - wait_for: "[#$] $"
  - type: "uptime\n"
```

### The endpoints

| Method + path | Does |
|---|---|
| `GET /api/runbooks` | List runbooks. |
| `POST /api/runbooks` | Create `{name, yaml}` — YAML is validated, `bad_runbook` on failure. |
| `GET /api/runbooks/{rid}` | Fetch one. |
| `PATCH /api/runbooks/{rid}` | Update `{name?, yaml?}` (re-validated). |
| `DELETE /api/runbooks/{rid}` | Delete. |
| `POST /api/runbooks/{rid}/run` | Run it — `{node_ids?, group?, stagger_ms?}`. |
| `GET /api/runbooks/{rid}/runs/{run_id}` | Live per-node run status. |

The runbook definitions are durable (the `runbooks` table); runs are tracked in
memory and their status is kept queryable for **1 hour** after they finish.

### Running one

`POST /runbooks/{rid}/run` targets either an explicit `node_ids` list, a `group`
name (all nodes in that group), or both. Up to **128 nodes** per run. For each
target, staggered by `stagger_ms`:

- **offline** → `skipped`,
- already running an expect job → `rejected` (busy — the one-job-per-node rule
  from the expect engine is respected),
- otherwise an expect job starts and the node goes `running`.

A single node failing does **not** abort the fleet — every other target keeps
going. Progress streams as `runbook_progress` events (a per-node status summary),
and `GET …/runs/{run_id}` folds live expect-job state into the same view. The run
is marked finished once no node is still `queued` or `running`.
