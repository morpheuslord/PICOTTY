[← Docs index](README.md) · [← Project README](../README.md)

# Packaging & release

- [Overview: one distribution, three import surfaces](#overview-one-distribution-three-import-surfaces)
- [Install (users)](#install-users)
- [Develop (from source)](#develop-from-source)
- [Using the SDK](#using-the-sdk)
- [Build & publish](#build--publish)
- [The node library](#the-node-library)

## Overview: one distribution, three import surfaces

PICOTTY ships as a **single** distribution — `picotty` (version single-sourced from
`hub/pyproject.toml`, license GPL-3.0-or-later) built with the `uv_build` backend
from the `src/` layout under `hub/`. One wheel, three things you can import:

| Import surface | What it is | Needs |
|---|---|---|
| `picotty.protocol` | The wire protocol — `PROTOCOL_VERSION`, `validate_send`, `encode_frame`, `read_frame`, `ProtocolError` | base install |
| `picotty.client` | The SDK — `HubClient` (async REST) + `HubEvents` (WebSocket async-iterator) | base install |
| `picotty.hub` | The server (FastAPI app, `picotty-hub`) | `[hub]` extra |

The base install is deliberately **lean** — only `httpx` and `websockets`. Everything
that only a *client* needs (the SDK, the Telegram sidecar, a cron health check, CI)
runs on that alone. The server's heavier stack lives behind extras so it is never
dragged in by accident:

- **`[hub]`** — `fastapi`, `uvicorn[standard]`, `aiosqlite`, `pydantic`, `pyyaml`.
- **`[telegram]`** — `python-telegram-bot[rate-limiter]`, `pyotp`.

The point of the split is size and blast radius: a Pi Zero 2 W running only the
Telegram sidecar installs `picotty[telegram]` and never pulls FastAPI or uvicorn, and
a machine that just talks to a remote hub installs bare `picotty`. Dev tooling
(`pytest`) lives in a uv dependency **group** (`dev`), not an extra, so it never leaks
into a user install.

## Install (users)

Install the tool in its own isolated environment with uv:

```bash
uv tool install picotty                 # base: SDK + protocol + both CLIs
uv tool install 'picotty[hub]'          # add the server stack (to run the hub)
```

That puts two console entry points on `PATH`:

| Command | Entry point | Does |
|---|---|---|
| `picotty-hub` | `picotty.hub.main:main` | Runs the hub server (needs `[hub]`). |
| `picotty-sim` | `picotty.sim:main` | The node simulator — a fake node for demos/tests. |

```bash
picotty-hub                                       # serve the dashboard + REST/WS
picotty-sim --id demo --token <TOK>               # dial a fake node into it
```

Upgrade in place with `uv tool upgrade picotty`.

**Runtime state lives out of the install tree.** The hub's SQLite database defaults to
`~/.local/share/picotty/hub.db` (honoring `XDG_DATA_HOME`); a systemd unit that sets
`StateDirectory=picotty` gets `/var/lib/picotty`. The dashboard's static assets ship
inside the wheel and are loaded via `importlib.resources`. Two env overrides:

| Variable | Overrides | Default |
|---|---|---|
| `HUB_DB_PATH` | SQLite database path | `$XDG_DATA_HOME/picotty/hub.db` → `~/.local/share/picotty/hub.db` |
| `HUB_STATIC_DIR` | Dashboard static-asset directory | the assets bundled in the wheel |

## Develop (from source)

```bash
git clone https://github.com/morpheuslord/PICOTTY
cd PICOTTY/hub
uv sync --extra hub                     # creates hub/.venv with the server stack + dev group
```

Run the CLIs and the tests through `uv run` (no manual venv activation):

```bash
uv run --extra hub picotty-hub
uv run picotty-sim --id demo --token <TOK>

uv run python tests/test_db.py
uv run python tests/test_integration.py
```

**Fetch the vendored terminal libraries first for anything that serves the
dashboard.** xterm and the asciinema player are vendored (gitignored) and pulled by a
script; run it once before serving or building so the assets exist:

```bash
bash src/picotty/static/vendor/fetch-vendor.sh
```

## Using the SDK

`picotty.client` is the lean way for a program to drive a running hub — REST plus the
same `/ws` event feed the dashboard uses. It needs **only the base install** (no
`[hub]`, no FastAPI):

```python
import asyncio
from picotty.client import HubClient

async def main():
    async with HubClient("http://hub:8080") as hub:
        print(await hub.health())
        print(await hub.nodes())

        # HubEvents: async-iterate the live event stream, per-node subscribe
        async with hub.events_stream() as stream:
            await stream.subscribe("node-01")
            async for ev in stream:
                print(ev["event"], ev)

asyncio.run(main())
```

The Telegram sidecar (`telegram-bot/`) is built exactly this way: it depends on
`picotty[telegram]` and imports `picotty.client` — never the server.

## Build & publish

The wheel bundles the dashboard assets, so **fetch the vendored libraries before you
build** or the wheel ships without a working terminal:

```bash
cd hub
bash src/picotty/static/vendor/fetch-vendor.sh   # MUST run before uv build
uv build                                          # -> dist/*.whl + dist/*.tar.gz
```

Inspect what landed before publishing:

```bash
ls -l dist/
python -m zipfile -l dist/picotty-1.0.0-*.whl | grep -E 'static/vendor'   # vendored assets present?
```

Publish with uv:

```bash
uv publish                              # dist/*.whl + *.tar.gz -> PyPI
```

The version is single-sourced in `hub/pyproject.toml` (`__init__.py` reads it back
from installed metadata) — bump it in one place, tag, and CI does the rest.

### Trusted Publishing (preferred)

Publish from CI with **no long-lived token** — PyPI Trusted Publishing (OIDC). The
account/owner is `morpheus_lord`; before the first CI publish, configure a PyPI
**pending publisher** for project `picotty` pointing at this GitHub repo and the
`publish.yml` workflow (PyPI → *Your projects* → *Publishing*, or *Pending publishers*
for a project that does not yet exist). Once that trust is registered, the tagged
release workflow authenticates over OIDC — see
[`.github/workflows/publish.yml`](../.github/workflows/publish.yml).

**Manual fallback** (from a laptop, or if OIDC is not yet set up): pass an API token —

```bash
UV_PUBLISH_TOKEN=pypi-... uv publish
```

Never commit a token; the CI workflow embeds none.

## The node library

The CircuitPython node library is a documented follow-up — the on-device firmware
packaged as installable `.mpy` bundles is deferred. See
[`node/README.md`](../node/README.md) for the current status.
