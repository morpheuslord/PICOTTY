"""Rendering helpers: HTML escaping for Telegram, node/status tables, ANSI
stripping, and chunking terminal output into Telegram-sized code blocks.

We use Telegram's HTML parse mode (not MarkdownV2) because escaping is simpler
and less error-prone: only &, <, > need escaping, and everything else — including
the shell metacharacters that pepper terminal output — passes through untouched.
"""

from __future__ import annotations

import re
import time

# Strip ANSI CSI / OSC escape sequences the getty emits (colors, cursor moves,
# title sets). Matches the dashboard's console-cleaning intent: show text, not
# terminal control bytes, in a chat window that cannot render them.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# Other C0 control bytes except tab/newline — bell, carriage-return noise, etc.
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def esc(text: str) -> str:
    """Escape the three HTML-significant characters for Telegram HTML mode."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def node_caps(node: dict) -> list[str]:
    """The hub's merge_node exposes capabilities as a list under 'capabilities'.
    Tolerate a comma-string too, so a shape change can't silently break gating."""
    caps = node.get("capabilities")
    if isinstance(caps, str):
        return [c.strip() for c in caps.split(",") if c.strip()]
    return list(caps or [])


def has_cap(node: dict, cap: str) -> bool:
    return cap in node_caps(node)


def strip_ansi(text: str) -> str:
    text = _ANSI.sub("", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CTRL.sub("", text)


def code_block(text: str) -> str:
    return "<pre>%s</pre>" % esc(text)


def _age(ms: int | None) -> str:
    if not ms:
        return "—"
    secs = max(0, int((time.time() * 1000 - ms) / 1000))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm" % (secs // 60)
    if secs < 86400:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


def _dot(node: dict) -> str:
    """A single-glyph status marker: node link, then target-machine liveness."""
    if node.get("status") != "online":
        return "○"          # node offline
    target = node.get("target") or node.get("host_up")
    if target in ("up", True):
        return "●"          # node online, target alive
    if target in ("down", False):
        return "◐"          # node online, target dead
    return "◍"              # node online, target unknown


def render_nodes(nodes: list[dict]) -> str:
    """A compact monospace roster for /nodes and /status."""
    if not nodes:
        return "<b>No nodes registered.</b>"
    lines = []
    for n in sorted(nodes, key=lambda x: x.get("id", "")):
        tx = "tx" if has_cap(n, "serial_tx") else "  "
        lines.append("%s %-14s %-7s %-4s %5s" % (
            _dot(n),
            (n.get("id") or "")[:14],
            (n.get("status") or "")[:7],
            tx,
            _age(n.get("last_seen")),
        ))
    header = "  %-14s %-7s %-4s %5s" % ("node", "link", "cap", "seen")
    body = header + "\n" + "\n".join(lines)
    legend = "● target up  ◐ target down  ◍ unknown  ○ node offline"
    return "<pre>%s</pre>\n<i>%s</i>" % (esc(body), esc(legend))


def render_status(health: dict, stats: dict, nodes: list[dict]) -> str:
    up_ms = health.get("uptime_ms", 0)
    up = _age(int(time.time() * 1000) - up_ms) if up_ms else "—"
    online = health.get("nodes_online", 0)
    total = health.get("nodes_total", 0)
    head = (
        "<b>PICOTTY hub</b>  v%s\n"
        "uptime %s · nodes %d/%d online · loop lag %sms · ws %d"
        % (
            esc(str(health.get("version", "?"))),
            up, online, total,
            esc(str(stats.get("loop_lag_ms", "?"))),
            stats.get("ws_clients", 0),
        )
    )
    return head + "\n\n" + render_nodes(nodes)


def render_uptime(node: dict) -> str:
    dot = _dot(node)
    target = node.get("target") or node.get("host_up")
    tstr = {True: "up", "up": "up", False: "down", "down": "down"}.get(target, "unknown")
    return (
        "%s <b>%s</b>\n"
        "link: %s\n"
        "target machine: %s\n"
        "last seen: %s ago\n"
        "fw: %s · caps: %s\n"
        "ip: %s"
        % (
            dot, esc(str(node.get("id", "?"))),
            esc(str(node.get("status", "?"))),
            esc(tstr),
            _age(node.get("last_seen")),
            esc(str(node.get("fw_version") or "?")),
            esc(", ".join(node_caps(node)) or "-"),
            esc(str(node.get("ip", "?"))),
        )
    )


def _dur(ms: int | None) -> str:
    """Human duration from a millisecond SPAN (not a wall-clock timestamp like
    _age takes). Mirrors the dashboard's durShort: largest sensible unit."""
    if ms is None:
        return "—"
    s = int(ms // 1000)
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm" % (s // 60)
    if s < 86400:
        return "%dh %dm" % (s // 3600, (s % 3600) // 60)
    return "%dd %dh" % (s // 86400, (s % 86400) // 3600)


def _ms(v) -> str:
    return "%dms" % v if isinstance(v, (int, float)) else "—"


def _pct(v) -> str:
    return "%d%%" % v if isinstance(v, (int, float)) else "—"


def link_quality(node: dict):
    """Classify link health from jitter + loss so a flaky node is visible before
    it drops. Mirrors the dashboard's netQuality thresholds exactly. Returns
    (label, emoji) or None when there is no telemetry yet (node offline / never
    pinged)."""
    j = node.get("jitter_ms")
    if j is None:
        return None
    loss = node.get("loss_pct") or 0
    if loss >= 10 or j >= 100:
        return ("poor", "🔴")
    if loss >= 2 or j >= 40:
        return ("fair", "🟡")
    return ("good", "🟢")


def render_telemetry(nodes: list[dict]) -> str:
    """A link-quality roster for /telemetry: per-node rtt / jitter / loss /
    quality / reconnects. Offline nodes show dashes (their telemetry is null)."""
    if not nodes:
        return "<b>No nodes registered.</b>"
    lines = []
    for n in sorted(nodes, key=lambda x: x.get("id", "")):
        q = link_quality(n)
        lines.append("%s %-13s %6s %6s %5s %-4s %3d" % (
            _dot(n),
            (n.get("id") or "")[:13],
            _ms(n.get("rtt_avg_ms")),
            _ms(n.get("jitter_ms")),
            _pct(n.get("loss_pct")),
            (q[0] if q else "—"),
            int(n.get("reconnects") or 0),
        ))
    header = "  %-13s %6s %6s %5s %-4s %3s" % ("node", "rtt", "jit", "loss", "qual", "rc")
    body = header + "\n" + "\n".join(lines)
    legend = "rtt/jit = avg over recent pings · rc = reconnects this hub run"
    return "<pre>%s</pre>\n<i>%s</i>" % (esc(body), esc(legend))


def render_telemetry_node(node: dict) -> str:
    """Per-node link telemetry detail for /telemetry <node>."""
    q = link_quality(node)
    qstr = "%s %s" % (q[1], q[0]) if q else "— (no telemetry)"
    return (
        "%s <b>%s</b> — link telemetry\n"
        "quality: %s\n"
        "rtt: %s now · %s avg (%s–%s)\n"
        "jitter: %s · loss: %s\n"
        "node uptime: %s\n"
        "reconnects: %d (this hub run)"
        % (
            _dot(node), esc(str(node.get("id", "?"))),
            qstr,
            _ms(node.get("rtt_ms")), _ms(node.get("rtt_avg_ms")),
            _ms(node.get("rtt_min_ms")), _ms(node.get("rtt_max_ms")),
            _ms(node.get("jitter_ms")), _pct(node.get("loss_pct")),
            _dur(node.get("node_uptime_ms")),
            int(node.get("reconnects") or 0),
        )
    )


def chunk_output(text: str, max_chunk: int) -> list[str]:
    """Split relayed terminal text into <=max_chunk pieces, preferring line
    boundaries so a code block never tears mid-line. Over-long single lines are
    hard-split."""
    text = text.rstrip("\n")
    if not text:
        return []
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > max_chunk:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:max_chunk])
            line = line[max_chunk:]
        add = line if not cur else cur + "\n" + line
        if len(add) > max_chunk:
            chunks.append(cur)
            cur = line
        else:
            cur = add
    if cur:
        chunks.append(cur)
    return chunks
