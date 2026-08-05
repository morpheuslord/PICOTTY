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
from .. import telegram_setup as tg
from ..core import Hub
from ..protocol import validate_send
from ..utils import gen_token, hash_token, now_ms, verify_password
from .models import (
    BulkCmd, ChordCreate, ChordPatch, CmdBody, ExpectBody, KeysBody, LoginBody,
    MacroCreate, MacroPatch, MacroRun, NodePatch, OTABundleCreate, OTABundleZip,
    OTAPush, OTARollout, QueueBody, RunbookCreate, RunbookPatch, RunbookRun,
    SequenceBody, SysrqBody, SettingsPatch, TelegramConfig,
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


@router.post("/nodes/{node_id}/sysrq")
async def post_sysrq(request: Request, node_id: str, body: SysrqBody):
    """Magic SysRq over HID (Alt+SysRq+<key>) — reboot a hung target machine.
    Default key 'b' = immediate reboot. Requires kernel.sysrq on the target."""
    hub = hub_of(request)
    key = (body.key or "b").strip().lower()
    if len(key) != 1:
        return err("bad_key", "sysrq key must be a single character")
    return await hub.dispatch_command(node_id, {"type": "sysrq", "key": key})


# -- custom chords ------------------------------------------------------------

@router.get("/chords")
async def list_chords(request: Request):
    hub = hub_of(request)
    return {"ok": True, "chords": await hub.db.list_chords()}


@router.post("/chords")
async def create_chord(request: Request, body: ChordCreate):
    hub = hub_of(request)
    if not body.label.strip() or not body.chord:
        return err("bad_chord", "label and a non-empty chord are required")
    cid = await hub.db.create_chord(body.label.strip(), [k.strip().upper() for k in body.chord if k.strip()])
    return {"ok": True, "id": cid}


@router.patch("/chords/{chord_id}")
async def patch_chord(request: Request, chord_id: int, body: ChordPatch):
    hub = hub_of(request)
    chord = [k.strip().upper() for k in body.chord if k.strip()] if body.chord is not None else None
    await hub.db.update_chord(chord_id, label=body.label, chord=chord)
    return {"ok": True}


@router.delete("/chords/{chord_id}")
async def delete_chord(request: Request, chord_id: int):
    hub = hub_of(request)
    await hub.db.delete_chord(chord_id)
    return {"ok": True}


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


@router.get("/nodes/{node_id}/session.cast")
async def session_cast(request: Request, node_id: str, since: int = None,
                       before: int = None, width: int = 100, height: int = 30):
    """Stream the node's console over a time window as an asciicast v2 recording.

    `since`/`before` are received_at bounds in ms (the same clock as output
    timestamps). Built from output_log, so no capture path is needed: an install
    or a crash can be replayed at real speed. Node `ts` is deliberately ignored —
    the Picos have no RTC, so we use the hub's received_at as the authoritative
    clock (see operations.md)."""
    import json as _json
    hub = hub_of(request)
    await hub.db.flush_output()
    start = await hub.db.output_window_start(node_id, since, before)

    async def gen():
        header = {"version": 2, "width": width, "height": height}
        if start is not None:
            header["timestamp"] = start // 1000
        yield _json.dumps(header) + "\n"
        if start is None:
            return
        async for ts, text in hub.db.iter_output_window(node_id, since, before):
            offset = max(0.0, (ts - start) / 1000.0)
            yield _json.dumps([round(offset, 3), "o", text]) + "\n"

    headers = {"Content-Disposition": 'attachment; filename="%s.cast"' % node_id}
    return StreamingResponse(gen(), media_type="application/x-asciicast", headers=headers)


@router.get("/output/search")
async def output_search(request: Request, q: str, node_id: str = None, limit: int = 200):
    """Find where a string scrolled past in the console history."""
    hub = hub_of(request)
    if not q:
        return err("bad_query", "q is required")
    await hub.db.flush_output()
    rows = await hub.db.search_output(q, node_id=node_id, limit=min(limit, 1000))
    return {"ok": True, "matches": rows}


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


# -- OTA firmware updates -----------------------------------------------------

@router.get("/ota/bundles")
async def ota_bundles(request: Request):
    hub = hub_of(request)
    return {"ok": True, "bundles": hub.ota.list_bundles()}


@router.post("/ota/bundles")
async def ota_create_bundle(request: Request, body: OTABundleCreate):
    hub = hub_of(request)
    from ..ota import OTAError
    try:
        manifest = hub.ota.create_bundle(body.name, [f.model_dump() for f in body.files])
    except OTAError as e:
        # Log the specific reason to the audit trail (operator-visible), but don't
        # echo the exception text back in the HTTP body (py/stack-trace-exposure).
        await hub.audit("error", None, "OTA bundle rejected: %s" % e)
        return JSONResponse(status_code=422,
                            content=err("bad_bundle", "the bundle could not be created; see the events log for the reason"))
    await hub.audit("settings", None, "OTA bundle %s uploaded (%d files)" % (body.name, len(body.files)))
    return {"ok": True, "manifest": manifest}


@router.post("/ota/bundles/zip")
async def ota_create_bundle_zip(request: Request, body: OTABundleZip):
    """Upload a whole firmware as a .zip (e.g. firmware/build/<node>.zip); the hub
    decompresses it and stages every file — no picking files one by one."""
    import base64 as _b64
    from ..ota import OTAError
    hub = hub_of(request)
    try:
        raw = _b64.b64decode(body.zip_b64, validate=True)
    except Exception:
        return JSONResponse(status_code=422, content=err("bad_zip", "zip_b64 is not valid base64"))
    try:
        manifest = hub.ota.create_bundle_from_zip(body.name, raw)
    except OTAError as e:
        # Reason to the audit trail, not the HTTP response (py/stack-trace-exposure).
        await hub.audit("error", None, "OTA zip bundle rejected: %s" % e)
        return JSONResponse(status_code=422,
                            content=err("bad_bundle", "the .zip could not be processed as a firmware bundle; see the events log"))
    await hub.audit("settings", None, "OTA bundle %s uploaded from zip (%d files)" % (body.name, len(manifest["files"])))
    return {"ok": True, "manifest": manifest}


@router.post("/nodes/{node_id}/ota")
async def ota_push(request: Request, node_id: str, body: OTAPush):
    hub = hub_of(request)
    if not hub.registry.is_online(node_id):
        return err("node_offline", "node %s is not connected" % node_id)
    res = hub.ota.start_push(node_id, body.bundle)
    if not res.get("ok"):
        status = 404 if res.get("error") == "no_bundle" else 422
        return JSONResponse(status_code=status, content=res)
    return res


@router.get("/nodes/{node_id}/ota/{job_id}")
async def ota_status(request: Request, node_id: str, job_id: str):
    hub = hub_of(request)
    job = hub.ota.get_job(job_id)
    if not job:
        return err("not_found", "no such ota job")
    return {"ok": True, "job": job}


@router.post("/bulk/ota")
async def ota_rollout(request: Request, body: OTARollout):
    hub = hub_of(request)
    import asyncio
    if hub.ota.get_manifest(body.bundle) is None:
        return err("no_bundle", "no such bundle %s" % body.bundle)
    # A canary rollout gates on the first node coming back healthy, which can take
    # up to a minute, so run it as a background task and let the UI follow
    # ota_progress events rather than blocking the request.
    asyncio.get_event_loop().create_task(
        hub.ota.rollout(body.node_ids, body.bundle, body.stagger_ms or 0))
    await hub.audit("settings", None, "OTA rollout of %s to %d node(s) started" % (body.bundle, len(body.node_ids)))
    return {"ok": True, "detail": "rollout started; watch ota_progress events", "nodes": body.node_ids}


# -- raw serial bridge --------------------------------------------------------

@router.get("/bridge")
async def bridge_list(request: Request):
    hub = hub_of(request)
    return {"ok": True, "enabled": bool(hub.settings.get("serial_bridge_enabled")),
            "bind": config.PROCESS.bridge_host, "ports": await hub.db.list_bridge_ports()}


@router.post("/nodes/{node_id}/bridge")
async def bridge_assign(request: Request, node_id: str, port: int):
    hub = hub_of(request)
    if port < 1024 or port > 65535:
        return err("bad_port", "port must be 1024-65535")
    existing = {r["port"]: r["node_id"] for r in await hub.db.list_bridge_ports()}
    if port in existing and existing[port] != node_id:
        return err("port_taken", "port %d is already assigned to %s" % (port, existing[port]))
    if port in (config.PROCESS.tcp_port, config.PROCESS.http_port):
        return err("bad_port", "port collides with a hub face")
    await hub.db.assign_bridge_port(node_id, port)
    await hub.bridge.reconcile()
    await hub.audit("settings", node_id, "serial bridge assigned port %d" % port)
    return {"ok": True, "node_id": node_id, "port": port,
            "enabled": bool(hub.settings.get("serial_bridge_enabled"))}


@router.delete("/nodes/{node_id}/bridge")
async def bridge_unassign(request: Request, node_id: str):
    hub = hub_of(request)
    await hub.db.remove_bridge_port(node_id)
    await hub.bridge.reconcile()
    await hub.audit("settings", node_id, "serial bridge port removed")
    return {"ok": True}


# -- offline command queue ----------------------------------------------------

@router.post("/nodes/{node_id}/queue")
async def enqueue(request: Request, node_id: str, body: QueueBody):
    hub = hub_of(request)
    command = body.command.model_dump(exclude_none=True)
    ctype = command.get("type")
    if ctype == "send":
        ok, code, detail = validate_send(command)
        if not ok:
            status = 413 if code == "too_large" else 422
            return JSONResponse(status_code=status, content=err(code, detail))
    ts = now_ms()
    expires_at = (ts + body.ttl_ms) if body.ttl_ms else None
    qid = await hub.db.enqueue_command(node_id, ctype, command, None, ts, expires_at)
    await hub.audit("cmd", node_id, "queued %s for delivery on connect (q%d)" % (ctype, qid))
    # If the node happens to be online right now, drain immediately.
    if hub.registry.is_online(node_id):
        await hub.drain_queue(node_id)
    return {"ok": True, "id": qid, "expires_at": expires_at}


@router.get("/nodes/{node_id}/queue")
async def list_queue(request: Request, node_id: str):
    hub = hub_of(request)
    return {"ok": True, "queued": await hub.db.list_queued(node_id)}


@router.delete("/nodes/{node_id}/queue/{qid}")
async def cancel_queue(request: Request, node_id: str, qid: int):
    hub = hub_of(request)
    n = await hub.db.cancel_queued(qid)
    if not n:
        return err("not_found", "no pending queued command %d" % qid)
    return {"ok": True}


# -- expect (wait-for-output automation) --------------------------------------

@router.post("/nodes/{node_id}/expect")
async def post_expect(request: Request, node_id: str, body: ExpectBody):
    hub = hub_of(request)
    if not hub.registry.is_online(node_id):
        return err("node_offline", "node %s is not connected" % node_id)
    res = hub.expect.start(node_id, body.steps)
    if not res.get("ok"):
        status = 409 if res.get("error") == "busy" else 422
        return JSONResponse(status_code=status, content=res)
    await hub.audit("cmd", node_id, "expect job %s started (%d steps)" % (res["job_id"], len(body.steps)))
    return res


@router.get("/nodes/{node_id}/expect/{job_id}")
async def get_expect(request: Request, node_id: str, job_id: str):
    hub = hub_of(request)
    snap = hub.expect.get(job_id)
    if not snap:
        return err("not_found", "no such expect job")
    return {"ok": True, "job": snap}


@router.post("/nodes/{node_id}/expect/{job_id}/cancel")
async def cancel_expect(request: Request, node_id: str, job_id: str):
    hub = hub_of(request)
    res = hub.expect.cancel(job_id)
    if not res.get("ok"):
        return err(res.get("error", "error"))
    return {"ok": True}


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


# -- runbooks -----------------------------------------------------------------

@router.get("/runbooks")
async def list_runbooks(request: Request):
    hub = hub_of(request)
    return {"ok": True, "runbooks": await hub.db.list_runbooks()}


@router.post("/runbooks")
async def create_runbook(request: Request, body: RunbookCreate):
    hub = hub_of(request)
    ok, detail = hub.runbooks.validate(body.yaml)
    if not ok:
        return JSONResponse(status_code=422, content=err("bad_runbook", detail))
    rid = await hub.db.create_runbook(body.name, body.yaml)
    return {"ok": True, "id": rid}


@router.get("/runbooks/{rid}")
async def get_runbook(request: Request, rid: int):
    hub = hub_of(request)
    rb = await hub.db.get_runbook(rid)
    if not rb:
        return err("not_found", "no such runbook")
    return {"ok": True, "runbook": rb}


@router.patch("/runbooks/{rid}")
async def patch_runbook(request: Request, rid: int, body: RunbookPatch):
    hub = hub_of(request)
    if body.yaml is not None:
        ok, detail = hub.runbooks.validate(body.yaml)
        if not ok:
            return JSONResponse(status_code=422, content=err("bad_runbook", detail))
    await hub.db.update_runbook(rid, name=body.name, yaml_text=body.yaml)
    return {"ok": True}


@router.delete("/runbooks/{rid}")
async def delete_runbook(request: Request, rid: int):
    hub = hub_of(request)
    await hub.db.delete_runbook(rid)
    return {"ok": True}


@router.post("/runbooks/{rid}/run")
async def run_runbook(request: Request, rid: int, body: RunbookRun):
    hub = hub_of(request)
    rb = await hub.db.get_runbook(rid)
    if not rb:
        return err("not_found", "no such runbook")
    node_ids = list(body.node_ids or [])
    if body.group:
        rows = await hub.db.list_nodes()
        node_ids += [r["id"] for r in rows if r.get("group_name") == body.group]
    if not node_ids:
        return err("no_targets", "no target nodes (pass node_ids or a group)")
    res = await hub.runbooks.start(rb, node_ids, body.stagger_ms or 0)
    if not res.get("ok"):
        return JSONResponse(status_code=422, content=res)
    await hub.audit("cmd", None, "runbook %s run %s on %d node(s)" % (rb["name"], res["run_id"], len(res["nodes"])))
    return res


@router.get("/runbooks/{rid}/runs/{run_id}")
async def get_runbook_run(request: Request, rid: int, run_id: str):
    hub = hub_of(request)
    run = hub.runbooks.get(run_id)
    if not run:
        return err("not_found", "no such run")
    return {"ok": True, "run": run}


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
    # A toggled serial_bridge_enabled must bind/unbind listeners immediately.
    if "serial_bridge_enabled" in values and hub.bridge is not None:
        await hub.bridge.reconcile()
    await hub.audit("settings", None, "updated: %s" % ", ".join(values.keys()))
    return {"ok": True, "settings": await hub.db.get_settings()}


# -- telegram sidecar setup ---------------------------------------------------

@router.get("/telegram")
async def telegram_status(request: Request):
    hub = hub_of(request)
    path = config.PROCESS.telegram_env_path
    st = tg.status(path)
    # Resolve the bot username for display if a token is stored — best-effort,
    # and the token itself is never returned.
    if st["token_present"]:
        env = tg.parse_env_file(path)
        ok, uname = await tg.validate_token(env.get("TELEGRAM_BOT_TOKEN", ""))
        st["valid"] = ok
        st["bot_username"] = uname if ok else None
    return {"ok": True, "telegram": st}


@router.post("/telegram/totp")
async def telegram_totp(request: Request):
    """Generate a fresh base32 TOTP secret + otpauth URI for the shell tier. The
    operator adds the secret to an authenticator app, then saves it below."""
    secret = tg.gen_totp_secret()
    return {"ok": True, "secret": secret, "uri": tg.otpauth_uri(secret)}


@router.post("/telegram")
async def telegram_save(request: Request, body: TelegramConfig):
    hub = hub_of(request)
    path = config.PROCESS.telegram_env_path
    env = tg.parse_env_file(path)  # merge onto the existing .env

    token = (body.bot_token or env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        return err("bad_request", "a bot token is required")
    ok, uname = await tg.validate_token(token)
    if not ok:
        await hub.audit("settings", None, "telegram token validation failed")
        # uname here is a static reason from validate_token, not an exception.
        return err("invalid_token", uname)

    raw_ids = body.chat_ids if body.chat_ids is not None else env.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    ids = [c.strip() for c in str(raw_ids).replace(" ", "").split(",") if c.strip()]
    for c in ids:
        if not c.lstrip("-").isdigit():
            return err("bad_request", "chat id is not numeric: %s" % c)
    if not ids:
        return err("bad_request", "at least one chat id is required")

    env["TELEGRAM_BOT_TOKEN"] = token
    env["TELEGRAM_ALLOWED_CHAT_IDS"] = ",".join(ids)
    env.setdefault("HUB_BASE_URL", "http://127.0.0.1:%d" % config.PROCESS.http_port)
    if body.hub_base_url:
        env["HUB_BASE_URL"] = body.hub_base_url.strip()
    if body.alerts_enabled is not None:
        env["ALERTS_ENABLED"] = "true" if body.alerts_enabled else "false"
    if body.shell_enabled is not None:
        env["SHELL_ENABLED"] = "true" if body.shell_enabled else "false"
    if body.totp_secret:
        env["SHELL_TOTP_SECRET"] = body.totp_secret.strip()
    if body.arm_window_s:
        env["SHELL_ARM_WINDOW_S"] = str(int(body.arm_window_s))

    shell_on = env.get("SHELL_ENABLED", "").lower() in ("1", "true", "yes", "on")
    if shell_on and not env.get("SHELL_TOTP_SECRET"):
        return err("bad_request", "the shell tier requires a TOTP secret; generate one first")

    try:
        tg.write_env_file(path, env)
    except OSError:
        await hub.audit("settings", None, "telegram .env write failed at %s" % path)
        return err("write_failed",
                   "could not write the sidecar .env — check TELEGRAM_ENV_PATH and permissions")
    await hub.audit("settings", None, "telegram sidecar configured (@%s)" % uname)
    return {"ok": True, "bot_username": uname, "env_path": str(path),
            "chat_count": len(ids), "shell_enabled": shell_on}


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
    # A human password: verify with the memory-hard KDF + constant-time compare,
    # never the fast token hash. (auth_password_hash is written by hash_password.)
    if verify_password(body.password, stored):
        return {"ok": True}
    return err("unauthorized", "bad password")


@router.post("/auth/logout")
async def logout(request: Request):
    return {"ok": True}
