"""Prompt-state detection: where is a target right now?

The serial output stream already flows through tcp_server on the `output`
branch. We keep a bounded rolling tail per node and run a small ordered set of
regexes over it to tag the node with a coarse `prompt_state` — login, password,
shell, grub, panic, booting — so the dashboard can show at a glance where every
box in the fleet sits.

This is live status, like rtt: it lives on NodeState (registry-only, never
persisted) and is recomputed on every output chunk. The tail is capped so memory
stays flat no matter how chatty a target is. The same tail is what the expect
engine (expect.py) waits on, so both features read one buffer.
"""

from __future__ import annotations

import re

# The tail we keep per node. Big enough to hold a prompt line and a little
# context, small enough that thousands of nodes stay cheap.
TAIL_MAX = 4096

# Ordered (state, regex) pairs. First match on the recent tail wins, so put the
# most specific/most urgent states first. Patterns are matched against the tail
# with DOTALL off and MULTILINE on, so `$` means end-of-line.
_PATTERNS = [
    # A kernel panic or oops is the loudest thing a box can say — check first.
    ("panic", re.compile(r"Kernel panic|BUG: unable to handle|Oops:|Call Trace:", re.M)),
    # Bootloaders, before any login can appear.
    ("grub", re.compile(r"GRUB|Minimal BASH-like line editing|Press ENTER to boot|GNU GRUB", re.M)),
    # A password prompt sits just under a login prompt; check it before shell so
    # a trailing ':' doesn't read as a prompt.
    ("password", re.compile(r"[Pp]assword:\s*$", re.M)),
    ("login", re.compile(r"\blogin:\s*$", re.M)),
    # An interactive shell prompt: a line ending in $, #, or > (optionally with a
    # trailing space). The `(?:^|\S)` alternative also matches a bare "# "/"$ "
    # root prompt at column 0. Kept last of the "settled" states.
    ("shell", re.compile(r"(?:^|\S) *[#$>] *$", re.M)),
    # Boot chatter: systemd units and kernel ring-buffer lines.
    ("booting", re.compile(r"\[\s*OK\s*\]|systemd\[1\]|Reached target|\[\s*\d+\.\d+\]", re.M)),
]


def classify(tail: str):
    """Return a prompt-state name for the given output tail, or None if nothing
    recognizable is near the end. Only the last portion of the tail is examined
    so a stale prompt far above doesn't outvote fresh output."""
    if not tail:
        return None
    # Look mostly at the freshest output; a prompt is on or near the last line.
    window = tail[-512:]
    for name, rx in _PATTERNS:
        if rx.search(window):
            return name
    return None


def update_tail(prev: str, chunk: str) -> str:
    """Append a new output chunk to a node's rolling tail, keeping it bounded."""
    if not chunk:
        return prev
    combined = prev + chunk
    if len(combined) > TAIL_MAX:
        combined = combined[-TAIL_MAX:]
    return combined
