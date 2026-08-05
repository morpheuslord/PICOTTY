"""Bot wiring: handlers, the two security gates, and the background relay.

Everything is built inside build_application() so handlers close over the shared
objects (hub client, security, sessions, alerts, audit) instead of reaching
through global state. Two gates protect the surface:

  * allowlist gate (group -1): runs on EVERY update; an unknown chat gets silence
    and an audit line, never a reply.
  * break-glass gate: /shell, /reboot, /sysrq require the shell to be ARMED via a
    valid TOTP (/arm). Stats and alerts need neither.
"""

from __future__ import annotations

import html as _html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (AIORateLimiter, ApplicationBuilder,
                          ApplicationHandlerStop, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler,
                          TypeHandler, filters)

from picotty.client import HubClient

from . import formatting
from .alertengine import AlertEngine
from .audit import AuditLog
from .config import Config
from .relay import EventRelay
from .reload import Reloader
from .security import Security
from .sessions import CONTROL_KEYS, SessionManager

HELP = """<b>PICOTTY hub bot</b>

<b>Stats</b>
/status — hub + node roster
/nodes — compact node list
/uptime [node] — per-node detail

<b>Shell (armed)</b>
/arm &lt;code&gt; — arm with your TOTP
/disarm — end the armed window
/armstatus — armed? for how long
/shell [node] — open a serial session
/end — close your session
control keys: /ctrlc /ctrld /ctrlz /esc /tab /enter /up /down /left /right
/reboot &lt;node&gt; — reboot the target machine
/sysrq &lt;node&gt; &lt;key&gt; — Magic SysRq (e.g. b)

<b>Alerts</b>
/mute &lt;node&gt; · /unmute &lt;node&gt;

In an open session, plain text is typed into the target (with a trailing CR).
Anything you type is visible in this chat's history — the getty does not echo
passwords, but your typed commands are recorded here."""


def build_application(cfg: Config):
    hub = HubClient(cfg.hub_base_url, ws_url=cfg.ws_url, timeout=cfg.hub_timeout_s)
    security = Security(cfg.allowed_chat_ids, cfg.shell_totp_secret, cfg.shell_arm_window_s)
    audit = AuditLog(cfg.audit_log_path)

    app = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    # ---- outbound helpers ---------------------------------------------------

    async def send(chat_id: int, html: str) -> None:
        try:
            await app.bot.send_message(chat_id, html, parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
        except Exception:
            pass

    async def reply(update: Update, html: str) -> None:
        msg = update.effective_message
        if msg is not None:
            await send(msg.chat_id, html)

    async def broadcast(html: str) -> None:
        for cid in cfg.allowed_chat_ids:
            await send(cid, html)

    def make_send(chat_id: int):
        async def _s(html: str) -> None:
            await send(chat_id, html)
        return _s

    # ---- shared services ----------------------------------------------------

    alerts = AlertEngine(broadcast, cfg.alert_debounce_s, cfg.alerts_enabled)
    sessions = SessionManager(
        subscribe=lambda n: relay.subscribe(n),
        unsubscribe=lambda n: relay.unsubscribe(n),
        flush_interval_s=cfg.output_flush_interval_s,
        max_chunk=cfg.output_max_chunk,
        summarize_bytes=cfg.output_summarize_bytes,
        idle_timeout_s=cfg.shell_idle_timeout_s,
    )
    relay = EventRelay(hub, alerts, sessions,
                       events_poll_interval_s=cfg.events_poll_interval_s)
    reloader = Reloader(env_file=cfg.env_file, security=security, alerts=alerts,
                        current_token=cfg.bot_token)

    async def on_idle_close(chat_id: int, node: str) -> None:
        await send(chat_id, "⏱️ Session on <b>%s</b> closed (idle timeout)." % node)
    sessions.on_idle_close = on_idle_close

    # ---- gate 1: allowlist, on every update ---------------------------------

    async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        cid = chat.id if chat else None
        if not security.is_allowed(cid):
            text = None
            if update.effective_message:
                text = update.effective_message.text
            await audit.record("denied", chat_id=cid, detail=text, ok=False)
            raise ApplicationHandlerStop   # silence: no reply to unknown chats

    # ---- gate 2: break-glass, for state-changing actions --------------------

    async def ensure_armed(update: Update) -> bool:
        if not cfg.shell_enabled:
            await reply(update, "🚫 Shell tier is disabled on this sidecar.")
            return False
        if not security.is_armed():
            await reply(update, "🔒 Shell is disarmed. <b>/arm &lt;code&gt;</b> first.")
            return False
        return True

    # ---- tier 1: stats ------------------------------------------------------

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await reply(update, HELP)

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            health = await hub.health()
            stats = await hub.stats()
            nodes = await hub.nodes()
        except Exception as e:
            await reply(update, "⚠️ Hub unreachable: %s" % formatting.esc(str(e)))
            return
        await reply(update, formatting.render_status(health, stats, nodes))

    async def cmd_nodes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            nodes = await hub.nodes()
        except Exception as e:
            await reply(update, "⚠️ Hub unreachable: %s" % formatting.esc(str(e)))
            return
        await reply(update, formatting.render_nodes(nodes))

    async def cmd_uptime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await _node_picker(update, "uptime", "Pick a node:")
            return
        node_id = context.args[0]
        node = await _fetch_node(update, node_id)
        if node is not None:
            await reply(update, formatting.render_uptime(node))

    # ---- tier 3: arming -----------------------------------------------------

    async def cmd_arm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not cfg.shell_enabled:
            await reply(update, "🚫 Shell tier is disabled on this sidecar.")
            return
        cid = update.effective_chat.id
        ok, msg = security.arm(cid, context.args[0] if context.args else "")
        await audit.record("arm", chat_id=cid, ok=ok)
        await reply(update, ("✅ " if ok else "❌ ") + formatting.esc(msg))

    async def cmd_disarm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        security.disarm()
        await audit.record("disarm", chat_id=update.effective_chat.id, ok=True)
        await reply(update, "🔒 Shell disarmed.")

    async def cmd_armstatus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if security.is_armed():
            await reply(update, "🔓 Armed — %d min left." % (security.armed_remaining_s() // 60 + 1))
        else:
            await reply(update, "🔒 Disarmed.")

    # ---- tier 3: shell sessions --------------------------------------------

    async def cmd_shell(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_armed(update):
            return
        if not context.args:
            await _node_picker(update, "shell", "Open a session on:")
            return
        await _open_shell(update, context.args[0])

    async def _open_shell(update: Update, node_id: str) -> None:
        node = await _fetch_node(update, node_id)
        if node is None:
            return
        if not formatting.has_cap(node, "serial_tx"):
            await reply(update, "🚫 <b>%s</b> firmware has no serial write (serial_tx)." % formatting.esc(node_id))
            return
        if node.get("status") != "online":
            await reply(update, "🚫 <b>%s</b> is offline." % formatting.esc(node_id))
            return
        cid = update.effective_chat.id
        ok, msg = await sessions.open(cid, node_id, make_send(cid))
        await audit.record("shell_open", chat_id=cid, node=node_id, ok=ok)
        await reply(update, ("💻 " if ok else "❌ ") + msg)

    async def cmd_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cid = update.effective_chat.id
        node = await sessions.close(cid, reason="user /end")
        if node:
            await audit.record("shell_close", chat_id=cid, node=node, ok=True)
            await reply(update, "👋 Session on <b>%s</b> closed." % formatting.esc(node))
        else:
            await reply(update, "No open session.")

    async def cmd_control(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cid = update.effective_chat.id
        session = sessions.session_for_chat(cid)
        if not session:
            await reply(update, "No open session. /shell &lt;node&gt; first.")
            return
        text = (update.effective_message.text or "").lstrip("/").split("@")[0].split()[0].lower()
        hexcode = CONTROL_KEYS.get(text)
        if hexcode is None:
            return
        res = await hub.send_serial(session.node_id, raw=hexcode)
        session.touch()
        await audit.record("control", chat_id=cid, node=session.node_id, detail=text,
                           ok=bool(res.get("ok", True)))
        if not res.get("ok", True):
            await reply(update, "⚠️ %s" % formatting.esc(str(res.get("detail") or res.get("error") or "send failed")))

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        cid = update.effective_chat.id
        session = sessions.session_for_chat(cid)
        if not session:
            return   # not in a session; ignore chatter
        if not security.is_armed():
            await sessions.close(cid, reason="disarmed")
            await reply(update, "🔒 Shell disarmed mid-session; closed.")
            return
        line = update.effective_message.text or ""
        res = await hub.send_serial(session.node_id, data=line + "\r")
        session.touch()
        await audit.record("input", chat_id=cid, node=session.node_id, detail=line, ok=bool(res.get("ok", True)))
        if not res.get("ok", True):
            await reply(update, "⚠️ %s" % formatting.esc(str(res.get("detail") or res.get("error") or "send failed")))

    # ---- tier 3: reboot / sysrq --------------------------------------------

    async def cmd_reboot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_armed(update):
            return
        if not context.args:
            await reply(update, "Usage: /reboot &lt;node&gt;")
            return
        node_id = context.args[0]
        cid = update.effective_chat.id
        res = await hub.reboot(node_id)
        await audit.record("reboot", chat_id=cid, node=node_id, ok=bool(res.get("ok", True)))
        if res.get("ok", True):
            await reply(update, "🔁 Reboot sent to <b>%s</b>." % formatting.esc(node_id))
        else:
            await reply(update, "❌ %s" % formatting.esc(str(res.get("detail") or res.get("error") or "failed")))

    async def cmd_sysrq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await ensure_armed(update):
            return
        if len(context.args) < 2:
            await reply(update, "Usage: /sysrq &lt;node&gt; &lt;key&gt;  (e.g. /sysrq node-01 b)")
            return
        node_id, key = context.args[0], context.args[1][:1]
        cid = update.effective_chat.id
        res = await hub.sysrq(node_id, key)
        await audit.record("sysrq", chat_id=cid, node=node_id, detail=key, ok=bool(res.get("ok", True)))
        if res.get("ok", True):
            await reply(update, "⚡ SysRq <b>%s</b> sent to <b>%s</b>." % (formatting.esc(key), formatting.esc(node_id)))
        else:
            await reply(update, "❌ %s" % formatting.esc(str(res.get("detail") or res.get("error") or "failed")))

    # ---- alerts: mute -------------------------------------------------------

    async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await reply(update, "Usage: /mute &lt;node&gt;")
            return
        alerts.mute(context.args[0])
        await reply(update, "🔕 Muted alerts for <b>%s</b>." % formatting.esc(context.args[0]))

    async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await reply(update, "Usage: /unmute &lt;node&gt;")
            return
        alerts.unmute(context.args[0])
        await reply(update, "🔔 Unmuted <b>%s</b>." % formatting.esc(context.args[0]))

    # ---- node picker (inline keyboard) --------------------------------------

    async def _node_picker(update: Update, action: str, prompt: str) -> None:
        try:
            nodes = await hub.nodes()
        except Exception as e:
            await reply(update, "⚠️ Hub unreachable: %s" % formatting.esc(str(e)))
            return
        if not nodes:
            await reply(update, "No nodes registered.")
            return
        rows, row = [], []
        for n in sorted(nodes, key=lambda x: x.get("id", "")):
            nid = n.get("id", "")
            row.append(InlineKeyboardButton(nid, callback_data="%s:%s" % (action, nid)))
            if len(row) == 2:
                rows.append(row); row = []
        if row:
            rows.append(row)
        await update.effective_message.reply_text(
            prompt, reply_markup=InlineKeyboardMarkup(rows))

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        action, _, node_id = data.partition(":")
        if action == "uptime":
            node = await _fetch_node(update, node_id)
            if node is not None:
                await send(query.message.chat_id, formatting.render_uptime(node))
        elif action == "shell":
            if not cfg.shell_enabled or not security.is_armed():
                await send(query.message.chat_id, "🔒 Shell is disarmed. /arm &lt;code&gt; first.")
                return
            await _open_shell(update, node_id)

    async def _fetch_node(update: Update, node_id: str):
        try:
            node = await hub.node(node_id)
        except Exception as e:
            await reply(update, "⚠️ Hub unreachable: %s" % formatting.esc(str(e)))
            return None
        if node is None:
            await reply(update, "No such node: <b>%s</b>" % formatting.esc(node_id))
        return node

    # ---- registration -------------------------------------------------------

    app.add_handler(TypeHandler(Update, gate), group=-1)

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("nodes", cmd_nodes))
    app.add_handler(CommandHandler("uptime", cmd_uptime))

    app.add_handler(CommandHandler("arm", cmd_arm))
    app.add_handler(CommandHandler("disarm", cmd_disarm))
    app.add_handler(CommandHandler("armstatus", cmd_armstatus))
    app.add_handler(CommandHandler("shell", cmd_shell))
    app.add_handler(CommandHandler("end", cmd_end))
    app.add_handler(CommandHandler(list(CONTROL_KEYS.keys()), cmd_control))
    app.add_handler(CommandHandler("reboot", cmd_reboot))
    app.add_handler(CommandHandler("sysrq", cmd_sysrq))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # ---- lifecycle: start/stop the relay with the application ---------------

    async def _post_init(application) -> None:
        sessions.start()
        relay.start()
        reloader.start()

    async def _post_shutdown(application) -> None:
        await reloader.stop()
        await relay.stop()
        await sessions.stop()
        await hub.aclose()

    app.post_init = _post_init
    app.post_shutdown = _post_shutdown
    return app
