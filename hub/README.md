# Hub

The management side: one Python process on one asyncio event loop, running two
faces at once — a raw TCP server for the node swarm and a FastAPI app for the
browser — over a shared in-memory registry and one SQLite database. The REST +
WebSocket surface is implemented in `app/api/`.

## Layout

```
hub/
  app/
    main.py         # entrypoint: wires TCP + FastAPI + background tasks on one loop
    config.py       # process config (env) + operator-tunable setting defaults
    protocol.py     # length-prefixed JSON framing (hub side)
    registry.py     # in-memory NodeState registry (live status + socket writers)
    db.py           # aiosqlite: schema, queries, batched output, retention
    eventbus.py     # WebSocket fan-out with subscription filtering + backpressure
    core.py         # the shared Hub: dispatch, ping/pong, audit, offline, view-merge
    tcp_server.py   # swarm face on :9000 (hello/heartbeat/result/output/pong/error/bye)
    tasks.py        # sweep, output flush, retention, stats, loop-lag
    api/
      rest.py       # every REST endpoint
      ws.py         # the /ws WebSocket
      models.py     # request bodies
  tools/node_sim.py # fake node for testing without hardware
  static/           # built UI goes here (served at /); see ../frontend
  data/hub.db       # created on first run (gitignored)
  requirements.txt
```

## Run it

On a Raspberry Pi (Zero 2 W), use the scripts:

```bash
bash scripts/install.sh          # apt deps + venv + pip install
bash scripts/run.sh              # foreground (dev), loads private/hub-token.txt
# or install as a service that starts on boot:
bash scripts/install-service.sh  # renders + enables the systemd unit
journalctl -u swarm-hub -f       # follow logs
```

Manual equivalent:

```bash
cd hub
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

On first start the hub mints a **shared node token** and prints it once — put it
in each node's `settings.toml` as `NODE_TOKEN`. It listens on `:9000` (swarm) and
`:8080` (browser). Open http://localhost:8080 for the dashboard once the UI is
built into `static/`.

Configuration via environment (all optional): `HUB_TCP_PORT`, `HUB_HTTP_PORT`,
`HUB_DB_PATH`, `HUB_STATIC_DIR`, `HUB_TCP_HOST`, `HUB_HTTP_HOST`. Operator-tunable
settings (heartbeat, stale timeout, retention, confirm-dangerous) live in the DB
and are changed via `PATCH /api/settings`.

Run under **one** uvicorn worker only. The single event loop is the design: a
second worker would get its own registry and node sockets and the two would
disagree about who is online.

## Test without hardware

Start the hub, grab the printed token, then run one or more fake nodes:

```bash
python -m tools.node_sim --id node-01 --token <TOKEN>
python -m tools.node_sim --id node-02 --token <TOKEN>
```

Each simulator connects, heartbeats, answers commands, and streams fake serial
output — enough to exercise the full dashboard. To pre-label and group your nodes,
use the seed in `../private/` (see that folder's README).

## Notes

- **Registry is disposable.** On restart it starts empty and refills as nodes
  reconnect; the SQLite record survives.
- **Output is batched** to SQLite (default every 500 ms) to protect SD-card write
  throughput; the live WebSocket stream is immediate and independent.
- **Auth is optional** and off by default — the design assumes an isolated
  management VLAN reached through a VPN/tunnel, not port exposure.
