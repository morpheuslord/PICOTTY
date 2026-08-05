[← Project README](../README.md)

# picotty_node — CircuitPython library (packaging target)

> **This is the DEFERRED Part-2 target, not the running firmware.** The
> authoritative node firmware **today** is [`firmware/circuitpython/`](../firmware/circuitpython/)
> — flat modules deployed by `build.sh`, fully working, OTA-capable. This folder
> holds the *packaging target* for that firmware (`picotty_node` as a `/lib`
> library) plus the drive-file [examples](examples/). The `.mpy` bundles, the
> per-CircuitPython-major CI, and circup registration described below are a
> **follow-up** — nothing here ships yet. Do not deploy from `node/`; deploy from
> `firmware/`.

## Goal

A node's `CIRCUITPY` drive should hold two files: a three-line `code.py` and a
`settings.toml`. Everything else — the wire layer, the netlink transport, the
HID injector, the serial backchannel, the command dispatch, OTA — moves into a
library installed once at `/lib/picotty_node/`.

```python
# code.py on the CIRCUITPY drive — the whole thing
import picotty_node
picotty_node.run()
```

`run()` reads `settings.toml`, builds the cooperative loop, and never returns.
Config stays out of the library on purpose: `settings.toml` is the one file
edited per node, `code.py` never changes, and a library update is "replace
`/lib/picotty_node/`" — touching neither. See [examples/](examples/) for both
template files.

## Package shape

The current flat firmware modules fold into the package one-to-one, with the
`code.py` dispatch loop becoming the package entry point:

| Current firmware module | `picotty_node` submodule |
|---|---|
| `code.py` (dispatch loop) | `picotty_node/__init__.py` (`run()`) + `picotty_node/loop.py` |
| `boot.py` | stays a drive file (runs before `/lib` is importable); OTA boot-recovery it calls moves to `picotty_node.otaflash` |
| `nodeconfig.py` | `picotty_node/config.py` |
| `messages.py` | `picotty_node/messages.py` |
| `wire.py` | `picotty_node/wire.py` |
| `netlink.py` | `picotty_node/netlink.py` |
| `injector.py` | `picotty_node/injector.py` |
| `backchannel.py` | `picotty_node/backchannel.py` |
| `otaflash.py` | `picotty_node/otaflash.py` |

`boot.py` is the one file that cannot move: it runs before `/lib` is on the
import path, so it stays on the drive (the example ships a thin one that imports
`picotty_node.otaflash` for boot-time OTA recovery). Third-party Adafruit
libraries (`adafruit_wiznet5k`, HID, `adafruit_hashlib`, layout libs) stay in
`/lib` alongside `picotty_node/` exactly as `build.sh` stages them today.

## Why `.mpy`

Shipping compiled `.mpy` instead of `.py` buys two things on an RP2040:

- **Import-time RAM.** No on-device compile step at import — faster boot and
  lower peak RAM during import, which is the tight resource at 264 KB.
- **Single-folder install.** `/lib/picotty_node/` is one directory of bytecode,
  no loose source files to manage on the drive.

The cost is a build step. **`.mpy` bytecode is tied to the CircuitPython MAJOR
version** — a 10.x board must load 10.x `.mpy`, and a 9.x `.mpy` fails to import
and crash-loops the node (the same rule that governs the Adafruit bundle, see
[firmware.md](../docs/firmware.md#circuitpython-version-rules)). So bundles are
compiled **per supported major** with the matching `mpy-cross`.

## Release layout (CI, per tag)

CI downloads the pinned `mpy-cross` binaries, compiles, zips, and attaches to the
GitHub release. Two artifacts per tag:

- **`picotty-node-<version>-cp<major>.zip`** — the `/lib/picotty_node/` folder as
  `.mpy` for that CircuitPython major, plus an example `code.py` and
  `settings.toml`. One zip per supported major.
- **`picotty-node-<version>-src.zip`** — plain `.py` source, for running from
  source or on a major without a prebuilt bundle.

Adafruit's cookiecutter library template and build actions do exactly this
per-major compile-and-attach and are worth cribbing from rather than writing
fresh.

## Install

### Manual unzip (the offline path — lead with this)

This must always work and needs no network on the node host.

1. Check the board's CircuitPython major (in `boot_out.txt` on the drive, or the
   REPL).
2. Download `picotty-node-<version>-cp<major>.zip` matching that major.
3. Unzip its `picotty_node/` folder into the board's `/lib`.
4. Copy the example `code.py` and `settings.toml` to the drive root; edit
   `settings.toml` for this node.

Getting the major wrong is the one failure mode — a mismatched `.mpy` crash-loops
the node — so match the zip's `cp<major>` to the board before copying.

### circup (follow-up)

Once `picotty_node` is registered in the CircuitPython **Community Bundle**,
`circup install picotty_node` fetches the bundle matching the board's real
version automatically. Registration means adopting the community bundle's repo
layout, CI conventions, and tagged releases — worth doing for discoverability,
but a follow-up, not a blocker. **In the meantime circup can install straight
from a GitHub URL**, so the tagged release is usable via circup before formal
registration lands.

## Memory checklist

The current firmware already fits, and `.mpy` only *improves* the import-time
footprint — but the restructure is the moment to bank a measurement. Add to the
node test checklist:

- **Measure free RAM after `run()` reaches steady state, per supported
  CircuitPython major**, and record it. Regressions across a major bump then show
  up as a number, not a mystery crash-loop.

**Escape hatch (mention, don't build):** if a future CircuitPython major gets
tight, `picotty_node` can be built as **frozen modules in flash**, which drops
its RAM cost further still. Note it as an option; there is no need to build it
now.

## Protocol compatibility

The node and hub version independently and will drift — a hub bugfix should not
force reflashing four nodes — so they negotiate rather than assume:

- The hub's `picotty.protocol` carries `PROTOCOL_VERSION = 1`. The node **echoes
  it in `hello`**. The hub logs a warning on mismatch and **refuses only on an
  incompatible major**; a compatible drift is allowed.
- **Capability flags** (`hid` / `cdc` / `serial_tx` / `ota`) still do
  feature-level negotiation on top of the version handshake — a node only gets
  offered what it advertises it can do.
- Each side's release notes state the protocol version it speaks and the minimum
  counterpart version.

### Constants-diff CI check

`picotty.protocol` is the single authoritative source; the node's wire code
mirrors it. A CI check **diffs the command names and the protocol version**
between the hub's protocol module and the node's wire code, failing the build if
they diverge — so the two sides cannot silently drift out of sync. Direct code
sharing is not practical (the node is written against CircuitPython's stdlib
subset), which is exactly why the diff exists.
