<!--
Thanks for contributing to PICOTTY! Please fill this out.
For security fixes, coordinate privately first — see SECURITY.md.
-->

## What & why

<!-- What does this change do, and what problem does it solve? Link any issue: Closes #123 -->

## Component(s)

<!-- Tick all that apply -->

- [ ] Node firmware (CircuitPython)
- [ ] Hub — TCP / protocol / registry / DB
- [ ] Hub — REST / WebSocket API
- [ ] Dashboard (Swarm Control UI)
- [ ] Scripts / deployment / target-setup
- [ ] Docs

## How it was tested

<!-- Describe what you ran. Delete lines that don't apply. -->

- [ ] `python3 -m py_compile` on changed firmware modules
- [ ] Firmware verified against `firmware/tools/testhub.py --selftest`
- [ ] Hub exercised with `uv run picotty-sim` + the dashboard
- [ ] Manual test on hardware (describe below)

<!-- details / output -->

## Checklist

- [ ] No secrets or real identifiers are committed (ran the leak-check grep from CONTRIBUTING.md; `private/`, `hub/data/`, `firmware/build/` untouched).
- [ ] Code matches the style/idiom of the surrounding files.
- [ ] The wire protocol (if touched) stays small and backward-compatible.
- [ ] Docs updated where behavior or usage changed.
- [ ] Change is in scope (managing hardware you own, not attacking third-party systems).
