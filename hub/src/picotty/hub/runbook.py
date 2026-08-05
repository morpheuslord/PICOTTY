"""Runbooks: YAML jobs that run expect steps across a group of nodes.

A macro is a single sequence. A runbook is the fleet-scale version: a named,
durable list of expect steps (wait-for-output automation) executed per node
across a target group, with per-node staggering and a live progress view. It
sits directly on top of the expect engine (expect.py) — each target node gets
its own ExpectJob driven by the runbook's steps — and reuses the bulk-dispatch
staggering idea from rest.bulk_cmd.

YAML shape (v1):

    name: log-in-and-check
    steps:
      - wait_for: "login:"
        timeout_ms: 30000
      - send: "root\n"
      - wait_for: "[Pp]assword:"
      - send: "toor\n"
      - wait_for: "[#$] $"
      - type: "uptime\n"

Each step is one of: wait_for (regex, optional timeout_ms/on_timeout), send
(text or {raw}), type (text), keys (chord string like "CTRL+C"), delay_ms.
These translate 1:1 onto expect steps. Runs are tracked in memory (bounded);
the runbook definitions themselves are durable in the runbooks table.
"""

from __future__ import annotations

import asyncio

import yaml

from .utils import gen_cmd_id, now_ms

MAX_NODES = 128
RUN_TTL_MS = 3_600_000  # keep a finished run queryable this long


class RunbookError(Exception):
    """The YAML could not be parsed or does not match the runbook schema."""


def _int_field(value, label):
    """Coerce a numeric YAML field, raising RunbookError (a clean 422) rather than
    letting a ValueError/TypeError escape as a 500 for input like timeout_ms: abc."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RunbookError("%s must be an integer" % label)


def _translate_step(step: dict) -> dict:
    """Map a runbook YAML step onto an expect-engine step."""
    if not isinstance(step, dict):
        raise RunbookError("each step must be a mapping")
    if "wait_for" in step:
        w = {"regex": str(step["wait_for"])}
        if "timeout_ms" in step:
            w["timeout_ms"] = _int_field(step["timeout_ms"], "timeout_ms")
        if "on_timeout" in step:
            w["on_timeout"] = step["on_timeout"]
        return {"wait_for": w}
    if "send" in step:
        val = step["send"]
        if isinstance(val, dict) and "raw" in val:
            return {"type": "send", "raw": str(val["raw"])}
        return {"type": "send", "data": str(val)}
    if "type" in step and "wait_for" not in step:
        return {"type": "type", "text": str(step["type"])}
    if "keys" in step:
        chord = step["keys"]
        if isinstance(chord, str):
            chord = [p.strip().upper() for p in chord.split("+") if p.strip()]
        return {"type": "keys", "chord": chord}
    if "delay_ms" in step:
        return {"delay_ms": _int_field(step["delay_ms"], "delay_ms")}
    raise RunbookError("unrecognized step: %r" % step)


def parse_runbook(yaml_text: str) -> dict:
    """Parse + validate a runbook YAML into {name, steps:[expect steps]}."""
    try:
        doc = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise RunbookError("bad YAML: %s" % e)
    if not isinstance(doc, dict):
        raise RunbookError("runbook must be a mapping with 'steps'")
    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RunbookError("runbook needs a non-empty 'steps' list")
    return {"name": doc.get("name", ""), "steps": [_translate_step(s) for s in steps]}


class RunbookRunner:
    def __init__(self, hub):
        self.hub = hub
        self._runs = {}  # run_id -> run dict

    def validate(self, yaml_text: str):
        """Return (ok, error). Used by the REST layer before persisting."""
        try:
            parse_runbook(yaml_text)
            return True, None
        except RunbookError as e:
            return False, str(e)

    async def start(self, runbook: dict, node_ids, stagger_ms: int) -> dict:
        try:
            parsed = parse_runbook(runbook["yaml"])
        except RunbookError as e:
            return {"ok": False, "error": "bad_runbook", "detail": str(e)}
        node_ids = list(dict.fromkeys(node_ids))  # de-dup, preserve order
        if not node_ids:
            return {"ok": False, "error": "no_targets", "detail": "no target nodes"}
        if len(node_ids) > MAX_NODES:
            return {"ok": False, "error": "too_many", "detail": "max %d nodes" % MAX_NODES}

        run_id = "rb_" + gen_cmd_id()[2:]
        run = {
            "run_id": run_id, "runbook_id": runbook.get("id"), "name": runbook.get("name", ""),
            "started_at": now_ms(), "finished_at": None,
            "nodes": {nid: {"status": "queued", "job_id": None} for nid in node_ids},
        }
        self._runs[run_id] = run
        asyncio.get_event_loop().create_task(
            self._drive(run, parsed["steps"], node_ids, stagger_ms))
        return {"ok": True, "run_id": run_id, "nodes": node_ids}

    async def _drive(self, run, steps, node_ids, stagger_ms):
        for i, nid in enumerate(node_ids):
            if not self.hub.registry.is_online(nid):
                run["nodes"][nid] = {"status": "skipped", "job_id": None, "reason": "offline"}
            elif self.hub.expect.is_busy(nid):
                run["nodes"][nid] = {"status": "rejected", "job_id": None, "reason": "busy"}
            else:
                res = self.hub.expect.start(nid, steps)
                if res.get("ok"):
                    run["nodes"][nid] = {"status": "running", "job_id": res["job_id"]}
                else:
                    run["nodes"][nid] = {"status": "rejected", "job_id": None, "reason": res.get("error")}
            self._broadcast(run)
            if stagger_ms and i < len(node_ids) - 1:
                await asyncio.sleep(stagger_ms / 1000)

    def _refresh(self, run):
        """Fold live expect-job status into the run's per-node view."""
        done = True
        for nid, entry in run["nodes"].items():
            job_id = entry.get("job_id")
            if job_id:
                snap = self.hub.expect.get(job_id)
                if snap:
                    entry["status"] = snap["status"]
                    entry["step"] = snap["step"]
                    entry["total"] = snap["total"]
            # "pending" is the window between create_task and the job's first
            # line setting "running" — the run is not finished during it.
            if entry["status"] in ("queued", "pending", "running"):
                done = False
        if done and run["finished_at"] is None:
            run["finished_at"] = now_ms()
        return run

    def _broadcast(self, run):
        self.hub.eventbus.broadcast({
            "event": "runbook_progress", "run_id": run["run_id"],
            "name": run["name"], "nodes": self._summary(run),
        })

    @staticmethod
    def _summary(run):
        return {nid: e.get("status") for nid, e in run["nodes"].items()}

    def get(self, run_id):
        run = self._runs.get(run_id)
        if not run:
            return None
        self._prune()
        return self._refresh(run)

    def _prune(self):
        cut = now_ms() - RUN_TTL_MS
        for rid in [r for r, v in self._runs.items()
                    if v["finished_at"] is not None and v["finished_at"] < cut]:
            self._runs.pop(rid, None)
