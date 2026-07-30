# Contributing to PICOTTY

Thanks for your interest in improving PICOTTY — a Pico-based serial/HID KVM swarm
with a central hub and dashboard. This guide covers how the project is laid out,
how to set up a dev loop, and the one rule that matters most: **never commit
secrets or machine-specific identifiers.**

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Ground rule: keep private data out of the repo

PICOTTY is deployed against real machines, so the repo must stay free of
deployment specifics. **Never commit:**

- the shared node token, or `private/` in any form,
- real node ids, hub IP/gateway, or target hostnames,
- the hub database (`hub/data/`) or staged firmware (`firmware/build/`).

These are already covered by `.gitignore`. Before you push, run the leak check —
it must print nothing:

```bash
grep -rniE 'node-(main|ic|pbs)|192\.168\.|10\.20\.0\.|your-token-prefix' \
  --exclude-dir=private --exclude-dir=.git . || echo "clean"
```

Use generic placeholders in examples: `node-01`, `<hub-ip>`, `<TOKEN>`.

## Project layout

| Path | What it is |
|---|---|
| `firmware/circuitpython/` | The node firmware (CircuitPython for Pico + W5100S). |
| `firmware/scripts/` | `install-deps.sh`, `build.sh`, `deploy-zip.sh`. |
| `firmware/tools/testhub.py` | A mock hub to test a real node in isolation. |
| `hub/app/` | The hub: asyncio TCP server + FastAPI + SQLite + registry. |
| `hub/static/` | The Swarm Control dashboard (buildless SPA). |
| `hub/tools/node_sim.py` | A fake node to test the hub with no hardware. |
| `target-setup/` | Scripts that run on a managed target machine. |

See the top-level [README](README.md) for the architecture and diagrams.

## Dev setup & test loops

You do **not** need hardware to develop most of this.

**Hub + dashboard:**

```bash
cd hub
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m app.main                                   # serves :8080, listens :9000
# in another shell, simulate nodes (grab the token the hub printed):
python -m tools.node_sim --id node-01 --token <TOKEN>
```

Open `http://localhost:8080` and you should see the simulated node, drive it, and
watch the Events feed.

**Firmware:** syntax-check without a board, and exercise a real node against the
mock hub:

```bash
cd firmware/circuitpython
for f in *.py; do python3 -m py_compile "$f"; done   # syntax check
python3 ../tools/testhub.py --selftest               # with a node dialed in
```

## Coding style

- **Match the surrounding code.** Comment density, naming, and idiom should look
  like the file you're editing.
- Firmware runs on CircuitPython — stick to what the RP2040 build supports (no
  CPython-only stdlib), keep the main loop non-blocking, and don't allocate on the
  hot path.
- The hub is a single asyncio process; keep it that way (one uvicorn worker) and
  don't block the event loop.
- Keep the wire protocol small and backward-compatible; if a message shape must
  change, add a field rather than repurposing one.

## Submitting changes

1. Fork and branch off `main` (`git switch -c fix/short-description`).
2. Make focused commits with clear messages.
3. Run the syntax check, the leak check, and (where relevant) the hub + `node_sim`
   or `testhub` loop.
4. Update docs (README / component READMEs) if behavior or usage changed.
5. Open a PR using the template; describe what and why, the component touched, and
   what you tested.

## Scope & responsible use

PICOTTY injects keystrokes into and reads consoles from attached machines. Please
keep contributions aligned with its purpose — **managing hardware you own on a
network you control.** Features whose primary purpose is attacking systems the
operator doesn't administer are out of scope.

## Reporting bugs & vulnerabilities

- Functional bugs and feature ideas: open an issue with the matching template.
- Security vulnerabilities: **do not** open a public issue — see
  [SECURITY.md](SECURITY.md).
