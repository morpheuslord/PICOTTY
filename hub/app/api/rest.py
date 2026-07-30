"""Every REST endpoint. Base path is mounted at /api by main.py.

Live fields (status, rtt, inflight) come from the registry; durable fields
(label, notes, group, history) come from the database; node responses merge both.
"""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import __version__, config
from ..core import Hub
from ..protocol import validate_send
from ..utils import gen_token, hash_token, now_ms
from .models import (
    BulkCmd, CmdBody, KeysBody, LoginBody, MacroCreate, MacroPatch, MacroRun,
    NodePatch, SequenceBody, SettingsPatch,
)

router = APIRouter()


def hub_of(request: Request) -> Hub:
    return request.app.state.hub


def err(error: str, detail: str = "", code: str = None):
    return {"ok": False, "error": error, "detail": detail}


# -- health / stats -----------------------------------------------------------

@router.get("/health")
async def health(request: Request):
    hub = hub_of(request)
    return {
        "ok": True,
        "uptime_ms": hub.uptime_ms(),
        "loop_lag_ms": hub.loop_lag_ms,
        "nodes_online": hub.registry.online_count(),
        "nodes_total": hub.registry.count(),
        "bind": config.PROCESS.tcp_host,
        "swarm_port": config.PROCESS.tcp_port,
        "web_port": config.PROCESS.http_port,
        "version": __version__,
    }


@router.get("/stats")
async def stats(request: Request):
    hub = hub_of(request)
    per_node = [
        {"id": s.node_id, "rtt_ms": s.rtt_ms, "inflight": len(s.inflight), "status": s.status}
        for s in hub.registry.all()
    ]
    return {
        "ok": True,
        "uptime_ms": hub.uptime_ms(),
        "loop_lag_ms": hub.loop_lag_ms,
        "db_size_bytes": await hub.db.db_size_bytes(),
        "output_rows": await hub.db.count_output(),
        "ws_clients": hub.eventbus.client_count(),
        "nodes_online": hub.registry.online_count(),
        "nodes_total": hub.registry.count(),
        "per_node": per_node,
    }


# -- nodes --------------------------------------------------------------------

@router.get("/nodes")
async def list_nodes(request: Request, status: str = None, group: str = None,
                     q: str = None, sort: str = "id", order: str = "asc"):
    hub = hub_of(request)
    rows = await hub.db.list_nodes()
    nodes = [hub.merge_node(r) for r in rows]

    if status in ("online", "offline"):
        nodes = [n for n in nodes if n["status"] == status]
    if group:
        nodes = [n for n in nodes if n["group"] == group]
    if q:
        ql = q.lower()
        nodes = [n for n in nodes
                 if ql in (n["id"] + " " + n["label"] + " " + n["notes"] + " " + n["group"]).lower()]

    reverse = order == "desc"
    if sort in ("id", "label", "group", "status", "last_seen"):
        nodes.sort(key=lambda n: (n.get(sort) is None, n.get(sort)), reverse=reverse)
    return {"ok": True, "nodes": nodes}


@router.get("/nodes/{node_id}")
async def get_node(request: Request, node_id: str):
    hub = hub_of(request)
    row = await hub.db.get_node(node_id)
    if not row:
        return err("not_found", "no such node %s" % node_id)
    return {"ok": True, "node": hub.merge_node(row)}


@router.patch("/nodes/{node_id}")
async def patch_node(request: Request, node_id: str, body: NodePatch):
    hub = hub_of(request)
    row = await hub.db.get_node(node_id)
    if not row:
        return err("not_found", "no such node %s" % node_id)
    await hub.db.update_node_meta(node_id, label=body.label, group=body.group, notes=body.notes)
    fresh = await hub.db.get_node(node_id)
    merged = hub.merge_node(fresh)
    hub.eventbus.broadcast({
        "event": "node_updated", "id": node_id, "status": merged["status"],
        "last_seen": merged["last_seen"], "label": merged["label"], "group": merged["group"],
    })
    return {"ok": True, "node": merged}


@router.delete("/nodes/{node_id}")
async def delete_node(request: Request, node_id: str):
    hub = hub_of(request)
    if hub.registry.is_online(node_id):
        return err("node_online", "cannot forget an online node; it would re-register")
    row = await hub.db.get_node(node_id)
    if not row:
        return err("not_found", "no such node %s" % node_id)
    await hub.db.delete_node(node_id)
    await hub.audit("settings", node_id, "node forgotten")
    return {"ok": True}


# -- commands -----------------------------------------------------------------

@router.post("/nodes/{node_id}/cmd")
async def post_cmd(request: Request, node_id: str, body: CmdBody):
    hub = hub_of(request)
    command = body.model_dump(exclude_none=True)
    if command.get("type") == "send":
        # Reject a malformed/oversized send before it reaches the node, and gate
        # it on the node advertising serial_tx so no command row is even created
        # for firmware that cannot honor it.
        ok, code, detail = validate_send(command)
        if not ok:
            status = 413 if code == "too_large" else 422
            return JSONResponse(status_code=status, content=err(code, detail))
        # Only gate a connected node; an offline one is reported as node_offline
        # by dispatch_command below (the existing online check covers it).
        if hub.registry.is_online(node_id) and not hub.node_supports(node_id, "serial_tx"):
            return err("unsupported", "node firmware does not support serial write")
    return await hub.dispatch_command(node_id, command)


@router.post("/nodes/{node_id}/keys")
async def post_keys(request: Request, node_id: str, body: KeysBody):
    hub = hub_of(request)
    return await hub.dispatch_command(node_id, {"type": "keys", "chord": body.chord})


@router.post("/nodes/{node_id}/sequence")
async def post_sequence(request: Request, node_id: str, body: SequenceBody):
    hub = hub_of(request)
    return await hub.dispatch_command(
        node_id, {"type": "sequence", "steps": body.steps, "stop_on_error": body.stop_on_error}
    )


@router.post("/nodes/{node_id}/read")
async def post_read(request: Request, node_id: str):
    hub = hub_of(request)
    return await hub.dispatch_command(node_id, {"type": "read"})


@router.post("/nodes/{node_id}/ping")
async def post_ping(request: Request, node_id: str):
    hub = hub_of(request)
    rtt = await hub.ping_node(node_id)
    if rtt is None:
        return err("ping_failed", "no pong from %s" % node_id)
    return {"ok": True, "rtt_ms": rtt}


@router.post("/nodes/{node_id}/reboot")
async def post_reboot(request: Request, node_id: str):
    hub = hub_of(request)
    return await hub.send_control(node_id, {"type": "reboot"}, "node reboot requested")


@router.get("/nodes/{node_id}/commands")
async def node_commands(request: Request, node_id: str, limit: int = 50,
                        before: int = None, type: str = None, status: str = None):
    hub = hub_of(request)
    rows = await hub.db.list_commands(node_id, limit=limit, before=before, type_=type, status=status)
    return {"ok": True, "commands": rows}


@router.get("/nodes/{node_id}/output")
async def node_output(request: Request, node_id: str, since: int = None,
                      before: int = None, limit: int = 500):
    hub = hub_of(request)
    await hub.db.flush_output()  # ensure the newest lines are visible on backfill
    rows = await hub.db.list_output(node_id, since=since, before=before, limit=limit)
    chunks = [{"id": r["id"], "ts": r["received_at"], "text": r["text"]} for r in rows]
    return {"ok": True, "chunks": chunks, "has_more": len(rows) == limit}


@router.get("/nodes/{node_id}/output/download")
async def node_output_download(request: Request, node_id: str):
    hub = hub_of(request)
    await hub.db.flush_output()

    async def gen():
        async for text in hub.db.iter_output_text(node_id):
            yield text

    headers = {"Content-Disposition": 'attachment; filename="%s-console.log"' % node_id}
    return StreamingResponse(gen(), media_type="text/plain", headers=headers)


# -- bulk ---------------------------------------------------------------------

@router.post("/bulk/cmd")
async def bulk_cmd(request: Request, body: BulkCmd):
    hub = hub_of(request)
    import asyncio
    command = body.command.model_dump(exclude_none=True)
    dispatched = []
    for i, node_id in enumerate(body.node_ids):
        if not hub.registry.is_online(node_id):
            if body.skip_offline:
                dispatched.append({"id": node_id, "status": "skipped", "reason": "offline"})
                continue
        res = await hub.dispatch_command(node_id, command)
        if res.get("ok"):
            dispatched.append({"id": node_id, "cmd_id": res["cmd_id"], "status": "sent"})
        else:
            dispatched.append({"id": node_id, "status": "error", "reason": res.get("error")})
        if body.stagger_ms and i < len(body.node_ids) - 1:
            await asyncio.sleep(body.stagger_ms / 1000)
    return {"ok": True, "dispatched": dispatched}


# -- macros -------------------------------------------------------------------

@router.get("/macros")
async def list_macros(request: Request):
    hub = hub_of(request)
    return {"ok": True, "macros": await hub.db.list_macros()}


@router.post("/macros")
async def create_macro(request: Request, body: MacroCreate):
    hub = hub_of(request)
    macro_id = await hub.db.create_macro(body.name, body.steps, group=body.group, dangerous=body.dangerous)
    return {"ok": True, "id": macro_id}


@router.patch("/macros/{macro_id}")
async def patch_macro(request: Request, macro_id: int, body: MacroPatch):
    hub = hub_of(request)
    await hub.db.update_macro(macro_id, name=body.name, steps=body.steps,
                              group=body.group, dangerous=body.dangerous)
    return {"ok": True}


@router.delete("/macros/{macro_id}")
async def delete_macro(request: Request, macro_id: int):
    hub = hub_of(request)
    await hub.db.delete_macro(macro_id)
    return {"ok": True}


@router.post("/macros/{macro_id}/run")
async def run_macro(request: Request, macro_id: int, body: MacroRun):
    hub = hub_of(request)
    import asyncio
    macro = await hub.db.get_macro(macro_id)
    if not macro:
        return err("not_found", "no such macro")
    command = {"type": "sequence", "steps": macro["steps"], "stop_on_error": True}
    dispatched = []
    for i, node_id in enumerate(body.node_ids):
        if not hub.registry.is_online(node_id):
            dispatched.append({"id": node_id, "status": "skipped", "reason": "offline"})
            continue
        res = await hub.dispatch_command(node_id, command)
        dispatched.append({"id": node_id, "cmd_id": res.get("cmd_id"), "status": "sent" if res.get("ok") else "error"})
        if body.stagger_ms and i < len(body.node_ids) - 1:
            await asyncio.sleep(body.stagger_ms / 1000)
    return {"ok": True, "dispatched": dispatched}


# -- events -------------------------------------------------------------------

@router.get("/events")
async def list_events(request: Request, node_id: str = None, type: str = None,
                      since: int = None, before: int = None, limit: int = 100):
    hub = hub_of(request)
    rows = await hub.db.list_events(node_id=node_id, type_=type, since=since, before=before, limit=limit)
    return {"ok": True, "events": rows}


@router.get("/events/export")
async def export_events(request: Request, node_id: str = None, type: str = None,
                        since: int = None, before: int = None, limit: int = 10000):
    hub = hub_of(request)
    rows = await hub.db.list_events(node_id=node_id, type_=type, since=since, before=before, limit=limit)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "ts", "type", "node_id", "detail"])
    for r in rows:
        w.writerow([r["id"], r["ts"], r["type"], r.get("node_id") or "", r.get("detail") or ""])
    headers = {"Content-Disposition": 'attachment; filename="events.csv"'}
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers=headers)


# -- settings -----------------------------------------------------------------

@router.get("/settings")
async def get_settings(request: Request):
    hub = hub_of(request)
    return {"ok": True, "settings": await hub.db.get_settings()}


@router.patch("/settings")
async def patch_settings(request: Request, body: SettingsPatch):
    hub = hub_of(request)
    values = {k: v for k, v in body.model_dump(exclude_none=True).items()}
    if not values:
        return {"ok": True, "settings": await hub.db.get_settings()}
    await hub.db.set_settings(values)
    await hub.load_settings()  # apply live (the sweep reads hub.settings)
    await hub.audit("settings", None, "updated: %s" % ", ".join(values.keys()))
    return {"ok": True, "settings": await hub.db.get_settings()}


@router.post("/settings/token/rotate")
async def rotate_token(request: Request):
    hub = hub_of(request)
    token = gen_token()
    await hub.db.set_settings({"node_token_hash": hash_token(token)})
    await hub.audit("settings", None, "node token rotated")
    return {"ok": True, "token": token}


# -- auth (optional; network isolation is the real gate) ----------------------

@router.post("/auth/login")
async def login(request: Request, body: LoginBody):
    hub = hub_of(request)
    if not hub.settings.get("auth_enabled"):
        return {"ok": True, "detail": "auth disabled; access gated at the network layer"}
    stored = await hub.db.get_setting_raw("auth_password_hash")
    if stored and hash_token(body.password) == stored:
        return {"ok": True}
    return err("unauthorized", "bad password")


@router.post("/auth/logout")
async def logout(request: Request):
    return {"ok": True}
