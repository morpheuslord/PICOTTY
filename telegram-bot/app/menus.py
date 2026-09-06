"""Inline-keyboard builders for the button-driven bot UI.

Pure layout: each function returns an InlineKeyboardMarkup from plain data, so it's
testable without a live bot. The callback_data scheme is compact (Telegram caps it
at 64 bytes) and ':'-separated:

  m:<view>              top-level menu view (main/nodes/fleet/hubs/events/...)
  n:<id>                open a node's detail
  a:<act>:<id>[:extra]  a node action (ping/read/tel/shell/reboot/sysrq/mute/…)
  src:<label>           set the bot's source hub
  hub:<id>:<act>[:tgt]  steer a node between hubs
  mac:<id> / rb:<id>    run a macro / runbook (with a confirm step)
  arm / disarm          break-glass controls
  x                     no-op (dismiss)
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

BACK = "⬅ Back"


def _rows(buttons, per_row=2):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def _btn(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def main_menu(armed: bool, multi_hub: bool) -> InlineKeyboardMarkup:
    rows = [
        [_btn("📊 Status", "m:status"), _btn("🖥 Nodes", "m:nodes")],
        [_btn("📡 Telemetry", "m:tel"), _btn("📜 Events", "m:events")],
        [_btn("🧰 Fleet", "m:fleet"), _btn("🔔 Alerts", "m:alerts")],
    ]
    hub_row = []
    if multi_hub:
        hub_row.append(_btn("🛰 Source hub", "m:hubs"))
    hub_row.append(_btn("🔓 Armed" if armed else "🔒 Arm", "m:arm"))
    rows.append(hub_row)
    return InlineKeyboardMarkup(rows)


def home_row(extra=None):
    row = [_btn("🏠 Menu", "m:main")]
    if extra:
        row.append(extra)
    return row


def nodes_menu(nodes: list) -> InlineKeyboardMarkup:
    btns = []
    for n in sorted(nodes, key=lambda x: x.get("id", "")):
        nid = n.get("id", "")
        on = n.get("status") == "online"
        peer = n.get("peer_hub")
        dot = "🟢" if on else ("🟣" if peer else "⚪")
        btns.append(_btn("%s %s" % (dot, nid), "n:" + nid))
    rows = _rows(btns, 2)
    rows.append([_btn("🔄 Refresh", "m:nodes"), _btn("🏠 Menu", "m:main")])
    return InlineKeyboardMarkup(rows)


def node_menu(node: dict, armed: bool, muted: bool, multi_hub: bool) -> InlineKeyboardMarkup:
    nid = node.get("id", "")
    online = node.get("status") == "online"
    can_tx = "serial_tx" in (node.get("capabilities") or [])
    rows = [[_btn("📶 Ping", "a:ping:" + nid), _btn("📥 Read", "a:read:" + nid),
             _btn("📡 Telemetry", "a:tel:" + nid)]]
    if online and armed and can_tx:
        rows.append([_btn("💻 Shell", "a:shell:" + nid), _btn("🔁 Reboot", "a:reboot:" + nid),
                     _btn("⚡ SysRq", "a:sysrqm:" + nid)])
    elif online and not armed:
        rows.append([_btn("🔒 Arm for shell/reboot", "m:arm")])
    rows.append([_btn("🔔 Unmute" if muted else "🔕 Mute", "a:unmute:" + nid if muted else "a:mute:" + nid),
                 _btn("📄 Log", "a:log:" + nid)])
    if multi_hub:
        rows.append([_btn("🛰 Move hub", "a:hubm:" + nid)])
    rows.append([_btn("🔄 Refresh", "n:" + nid), _btn("⬅ Nodes", "m:nodes"), _btn("🏠 Menu", "m:main")])
    return InlineKeyboardMarkup(rows)


def confirm_menu(confirm_data: str, cancel_data: str, label: str = "Confirm") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("⚠ " + label, confirm_data), _btn("✖ Cancel", cancel_data)]])


def sysrq_menu(node_id: str) -> InlineKeyboardMarkup:
    keys = [("b reboot", "b"), ("o poweroff", "o"), ("s sync", "s"),
            ("e term", "e"), ("i kill", "i"), ("u remount-ro", "u")]
    btns = [_btn(lbl, "a:sysrq:%s:%s" % (node_id, k)) for lbl, k in keys]
    rows = _rows(btns, 2)
    rows.append([_btn("⬅ Node", "n:" + node_id)])
    return InlineKeyboardMarkup(rows)


def node_hub_menu(node_id: str, target: str) -> InlineKeyboardMarkup:
    """Steer one node: switch/prefer/pin to `target`, or unpin."""
    rows = [
        [_btn("➡ Move to %s" % target, "hub:%s:switch:%s" % (node_id, target))],
        [_btn("🏠 Set %s as home" % target, "hub:%s:prefer:%s" % (node_id, target))],
        [_btn("📌 Pin to %s" % target, "hub:%s:pin:%s" % (node_id, target)),
         _btn("📎 Unpin", "hub:%s:unpin:-" % node_id)],
        [_btn("⬅ Node", "n:" + node_id)],
    ]
    return InlineKeyboardMarkup(rows)


def hubs_menu(endpoints: list) -> InlineKeyboardMarkup:
    """Source-hub picker: endpoints = [{label, base, active}]."""
    rows = []
    for ep in endpoints:
        mark = "➡ " if ep.get("active") else ""
        rows.append([_btn("%s%s — %s" % (mark, ep["label"], ep["base"]), "src:" + ep["label"])])
    rows.append(home_row())
    return InlineKeyboardMarkup(rows)


def fleet_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🧩 Macros", "m:macros"), _btn("📕 Runbooks", "m:runbooks")],
        [_btn("📢 Bulk (type a line)", "m:bulkhelp")],
        home_row(),
    ])


def macros_menu(macros: list) -> InlineKeyboardMarkup:
    btns = [_btn(("⚠ " if m.get("dangerous") else "") + str(m.get("name", "")), "mac:%s" % m.get("id"))
            for m in macros]
    rows = _rows(btns, 1) if btns else []
    rows.append([_btn("⬅ Fleet", "m:fleet"), _btn("🏠 Menu", "m:main")])
    return InlineKeyboardMarkup(rows)


def runbooks_menu(runbooks: list) -> InlineKeyboardMarkup:
    btns = [_btn(str(r.get("name", "")), "rb:%s" % r.get("id")) for r in runbooks]
    rows = _rows(btns, 1) if btns else []
    rows.append([_btn("⬅ Fleet", "m:fleet"), _btn("🏠 Menu", "m:main")])
    return InlineKeyboardMarkup(rows)


def run_targets_menu(kind: str, item_id, node_ids: list) -> InlineKeyboardMarkup:
    """Choose where to run a macro/runbook: all online, or a specific node."""
    rows = [[_btn("▶ Run on ALL online (%d)" % len(node_ids), "%srun:%s:*" % (kind, item_id))]]
    btns = [_btn(nid, "%srun:%s:%s" % (kind, item_id, nid)) for nid in node_ids[:8]]
    rows += _rows(btns, 2)
    back = "m:macros" if kind == "mac" else "m:runbooks"
    rows.append([_btn("⬅ Back", back), _btn("🏠 Menu", "m:main")])
    return InlineKeyboardMarkup(rows)


def alerts_menu(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("🔕 Alerts ON" if enabled else "🔔 Alerts OFF", "x")],
        [_btn("🖥 Mute a node", "m:nodes")],
        home_row(),
    ])


def back_only(node_id: str = None) -> InlineKeyboardMarkup:
    row = home_row()
    if node_id:
        row.insert(0, _btn("⬅ Node", "n:" + node_id))
    return InlineKeyboardMarkup([row])
