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
    ) + render_uptime_hub(node)


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


def render_uptime_hub(node: dict) -> str:
    """The hub-label line for /uptime, appended when a node reports one."""
    hub = node.get("hub_label")
    return ("\nhub: %s" % esc(str(hub))) if hub else ""


def render_events(events: list[dict], limit: int = None) -> str:
    """A compact recent-events (audit) tail for /events."""
    if not events:
        return "<b>No events.</b>"
    rows = events[:limit] if limit else events
    lines = []
    for e in rows:
        nid = e.get("node_id") or "-"
        detail = (e.get("detail") or "").replace("\n", " ")
        lines.append("%5s %-8s %-10s %s" % (
            _age(e.get("ts")), (e.get("type") or "")[:8], str(nid)[:10], detail[:52]))
    return "<b>Recent events</b>\n<pre>%s</pre>" % esc("\n".join(lines))


def render_output_log(node_id: str, chunks: list[dict], max_chars: int = 3000) -> str:
    """Recent console scrollback for /log, ANSI-stripped and tail-trimmed."""
    if not chunks:
        return "No recent output for <b>%s</b>." % esc(node_id)
    text = strip_ansi("".join(c.get("text", "") for c in chunks))
    text = text[-max_chars:]
    return "<b>%s</b> — recent console\n%s" % (esc(node_id), code_block(text))


def render_search(q: str, matches: list[dict], max_rows: int = 20) -> str:
    """Where a string scrolled past, for /search."""
    if not matches:
        return "No matches for <b>%s</b>." % esc(q)
    lines = []
    for m in matches[:max_rows]:
        nid = m.get("node_id") or "-"
        ts = m.get("received_at") or m.get("ts")
        txt = strip_ansi(m.get("text", "")).replace("\n", " ").strip()
        lines.append("%5s %-10s %s" % (_age(ts), str(nid)[:10], txt[:52]))
    note = "" if len(matches) <= max_rows else "\n<i>+%d more</i>" % (len(matches) - max_rows)
    return "<b>%d match(es) for “%s”</b>\n<pre>%s</pre>%s" % (
        len(matches), esc(q), esc("\n".join(lines)), note)


def render_macros(macros: list[dict]) -> str:
    if not macros:
        return "<b>No macros.</b>  (create them from the dashboard)"
    lines = []
    for m in macros:
        warn = " ⚠️" if m.get("dangerous") else ""
        lines.append("/runmacro %s — %s%s" % (m.get("id"), esc(str(m.get("name", ""))), warn))
    return "<b>Macros</b>\n" + "\n".join(lines)


def render_runbooks(runbooks: list[dict]) -> str:
    if not runbooks:
        return "<b>No runbooks.</b>  (create them from the dashboard)"
    lines = ["/runbook %s — %s" % (r.get("id"), esc(str(r.get("name", "")))) for r in runbooks]
    return "<b>Runbooks</b>\n" + "\n".join(lines)


def render_dispatch(title: str, dispatched: list[dict]) -> str:
    """Summarize a bulk/macro fan-out result: how many sent/skipped/errored."""
    sent = sum(1 for d in dispatched if d.get("status") == "sent")
    skipped = sum(1 for d in dispatched if d.get("status") == "skipped")
    errored = sum(1 for d in dispatched if d.get("status") == "error")
    head = "✅ <b>%s</b>: %d sent" % (esc(title), sent)
    if skipped:
        head += " · %d skipped" % skipped
    if errored:
        head += " · %d error" % errored
    return head


def render_hubs(nodes: list[dict]) -> str:
    """Which hub each node is currently connected to, for /hubs (dual-hub)."""
    if not nodes:
        return "<b>No nodes registered.</b>"
    lines = []
    for n in sorted(nodes, key=lambda x: x.get("id", "")):
        lines.append("%s %-14s %s" % (
            _dot(n), (n.get("id") or "")[:14], n.get("hub_label") or "—"))
    body = "  %-14s %s\n" % ("node", "hub") + "\n".join(lines)
    return "<pre>%s</pre>\n<i>hub = which configured hub the board is on</i>" % esc(body)


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
