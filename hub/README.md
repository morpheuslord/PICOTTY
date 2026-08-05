# Hub — the `picotty` package

The management side, packaged as the **`picotty`** Python distribution (uv). One
process on one asyncio event loop runs two faces at once — a raw TCP server for
the node swarm and a FastAPI app for the browser — over a shared in-memory
registry and one SQLite database.

One distribution, three import surfaces:

| Import | What | Needs |
|---|---|---|
| `picotty.hub` | the server (registry + SQLite + :9000 TCP + FastAPI dashboard) | `[hub]` extra |
| `picotty.client` | the SDK: `HubClient` (REST) + `HubEvents` (WebSocket) | base install |
| `picotty.protocol` | wire framing, validation, `PROTOCOL_VERSION` | base install |

The lean base (httpx + websockets) is all a client needs; the server stack
(FastAPI, uvicorn, aiosqlite, pydantic, pyyaml) comes from the `[hub]` extra, so a
Pi Zero 2 W running only the Telegram sidecar stays small.

## Layout

```
hub/
  pyproject.toml        # the picotty distribution (uv_build backend)
  uv.lock               # pinned, reproducible installs
  .python-version       # 3.11 floor
  src/picotty/
    __init__.py         # version, single-sourced from distribution metadata
    protocol.py         # public wire-protocol surface (re-exports hub/protocol.py)
    client/             # the SDK: HubClient (REST) + HubEvents (WS async iterator)
    sim.py              # the node simulator (picotty-sim console script)
    static/             # the dashboard, shipped inside the wheel (served at /)
    hub/                # the server
      main.py           # wires TCP + FastAPI + background tasks on one loop
      config.py         # process config (env) + operator-tunable defaults
      protocol.py       # length-prefixed JSON framing (authoritative)
      registry.py       # in-memory NodeState registry
      db.py             # aiosqlite: schema, queries, batched output, retention
      eventbus.py       # WebSocket fan-out with subscription filtering
      core.py           # the shared Hub: dispatch, ping/pong, audit, view-merge
      tcp_server.py     # swarm face on :9000
      telegram_setup.py # writes the sidecar .env for the dashboard's Telegram page
      tasks.py          # sweep, output flush, retention, stats, loop-lag
      api/{rest,ws,models}.py
  tests/                # test_db.py, test_integration.py, driver.py
  scripts/              # install / run / systemd unit
```

## Run it (from source, with uv)

```bash
bash scripts/install.sh          # uv sync --extra hub (+ fetches terminal libs)
bash scripts/run.sh              # foreground (dev), loads private/hub-token.txt
# or install as a service that starts on boot:
bash scripts/install-service.sh  # renders + enables the systemd unit
journalctl -u swarm-hub -f       # follow logs
```

Manual equivalent:

```bash
cd hub
uv sync --extra hub
uv run --extra hub picotty-hub
```

Or install it as a tool (no repo checkout): `uv tool install picotty`, which puts
`picotty-hub` and `picotty-sim` on PATH. See **[../docs/packaging.md](../docs/packaging.md)**.

On first start the hub mints a **shared node token** and prints it once — put it
in each node's `settings.toml` as `NODE_TOKEN`. It listens on `:9000` (swarm) and
`:8080` (browser). Open http://localhost:8080 for the dashboard.

Configuration via environment (all optional): `HUB_TCP_PORT`, `HUB_HTTP_PORT`,
`HUB_DB_PATH`, `HUB_STATIC_DIR`, `HUB_TCP_HOST`, `HUB_HTTP_HOST`, `TELEGRAM_ENV_PATH`.
Runtime state (the SQLite DB) defaults to `~/.local/share/picotty/hub.db` (honors
`XDG_DATA_HOME`); the systemd unit uses `/var/lib/picotty`. Static assets ship
inside the wheel. Operator-tunable settings (heartbeat, stale timeout, retention,
confirm-dangerous, alerts) live in the DB and change via `PATCH /api/settings`.

Run under **one** uvicorn worker only. The single event loop is the design: a
second worker would get its own registry and node sockets and the two would
disagree about who is online.

## Test without hardware

Start the hub, grab the printed token, then run one or more fake nodes with the
packaged simulator:

```bash
uv run picotty-sim --id node-01 --token <TOKEN>
uv run picotty-sim --id node-02 --token <TOKEN>
```

Each simulator connects, heartbeats, answers commands, and streams fake serial
output — enough to exercise the full dashboard. The suites:

```bash
uv run python tests/test_db.py           # offline db checks
uv run python tests/test_integration.py  # end-to-end over real TCP + REST
```

## Using the client SDK

```python
from picotty.client import HubClient

async with HubClient("http://hub:8080") as hub:
    print(await hub.health())
    async with hub.events_stream() as stream:   # the /ws feed
        await stream.subscribe("node-01")
        async for ev in stream:
            ...
```

This is what the Telegram sidecar imports; it needs only the base install.

## Notes

- **Registry is disposable.** On restart it starts empty and refills as nodes
  reconnect; the SQLite record survives.
- **Output is batched** to SQLite (default every 500 ms) to protect SD-card write
  throughput; the live WebSocket stream is immediate and independent.
- **Auth is optional** and off by default — the design assumes an isolated
  management VLAN reached through a VPN/tunnel, not port exposure.
