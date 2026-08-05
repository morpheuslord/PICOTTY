# PICOTTY documentation

Deep technical reference. For the gist and copy-paste commands, see the
[project README](../README.md).

| Doc | What's inside |
|---|---|
| [architecture.md](architecture.md) | Component roles, the single-loop hub, the one-cable USB design, the wire protocol, message journeys, data model, repository layout |
| [hardware.md](hardware.md) | Physical topology, bill of materials (Pico / Pico 2 + WIZnet HAT), component tree, sizing |
| [deployment.md](deployment.md) | The three build phases, the end-to-end workflow, the full script reference + common invocations |
| [firmware.md](firmware.md) | Node firmware lifecycle, LED status codes, keyboard layout, OTA capability, hardening, CircuitPython version rules |
| [operations.md](operations.md) | Observability, prompt-state badges, HID vs Serial input, console renderer, session recording, the serial bridge, alerting |
| [automation.md](automation.md) | Prompt-state detection, the expect (wait-for-output) engine, the offline command queue, YAML runbooks — with the REST API and worked examples |
| [ota.md](ota.md) | Over-the-wire firmware updates: the safety model (checksum, `.bak`, watchdog-revert, finalize-when-healthy), the push flow, canary rollout, and current integration status |
| [packaging.md](packaging.md) | The `picotty` uv package: import surfaces, extras, entry points, install (`uv tool install`), the client SDK, and building + publishing to PyPI |
| [telegram.md](telegram.md) | The Telegram bot sidecar: tiers (stats / alerts / gated terminal), the security model, and the dashboard credential setup flow |
| [considerations.md](considerations.md) | Security boundary, the serial-bridge boundary, target requirements, power, roadmap |
