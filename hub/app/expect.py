"""The expect engine: hub-side wait-for-output automation.

`sequence` steps are fire-and-forget. An expect job is the missing half: it
alternates *action* steps (send/type/keys, dispatched through the ordinary
command path) with *wait_for* steps that block until a regex appears in the
node's serial output within a timeout. That is what turns PICOTTY from a
keyboard into automation — "wait for `login:`, send the user, wait for
`Password:`, send the password, wait for a shell prompt."

All the waiting is hub-side; the firmware stays dumb. The node just keeps
emitting output and executing discrete commands. tcp_server feeds every output
chunk to the running job via hub.feed_expect -> ExpectManager.feed.

Design invariants:
  * One running job per node. A second start is rejected with `busy`, because
    two jobs racing on one output stream is undefined.
  * Everything is bounded: max steps, max wall-time, a compiled+size-limited
    regex per wait, and every wait ends on its timeout rather than hanging.
  * A wait step clears the match buffer first, so it matches output that arrives
    *after* the preceding action, not a stale prompt from before it.
"""

from __future__ import annotations

import asyncio
import re

from .classifier import update_tail
from .utils import gen_cmd_id, now_ms

# Bounds. A job that exceeds any of these is rejected or aborted rather than
# being allowed to run unbounded on the shared loop.
MAX_STEPS = 64
MAX_REGEX_LEN = 512
MAX_WAIT_MS = 120_000
MAX_JOB_WALL_MS = 600_000
DEFAULT_WAIT_MS = 15_000
MATCH_WINDOW = 4096  # only the freshest bytes of the buffer are searched
FINISHED_TTL_MS = 300_000  # keep a finished job's status queryable this long


class ExpectError(Exception):
    """A job could not be built (bad step shape, oversized regex, too many steps)."""


def _int_field(value, label):
    """Coerce a numeric step field, raising ExpectError (a clean 422) rather than
    letting a ValueError/TypeError escape as a 500 for input like timeout_ms='x'."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ExpectError("%s must be an integer" % label)


def _build_command(step: dict):
    """Translate an action step into a dispatch_command body, or return None if
    the step is not an action (i.e. it is a wait_for or delay)."""
    if "wait_for" in step or "delay_ms" in step:
        return None
    stype = step.get("type")
    if stype == "send":
        cmd = {"type": "send"}
        if step.get("raw") is not None:
            cmd["raw"] = step["raw"]
        else:
            cmd["data"] = step.get("data", "")
        return cmd
    if stype == "type":
        return {"type": "type", "text": step.get("text", ""), "char_delay_ms": step.get("char_delay_ms")}
    if stype == "keys":
        return {"type": "keys", "chord": step.get("chord", [])}
    raise ExpectError("unknown action step type: %r" % stype)


class ExpectJob:
    def __init__(self, manager, job_id, node_id, steps, initial_tail=""):
        self.manager = manager
        self.job_id = job_id
        self.node_id = node_id
        self.status = "pending"  # pending|running|done|failed|timeout|cancelled
        self.step_index = -1
        self.total = len(steps)
        self.detail = ""
        self.started_at = now_ms()
        self.finished_at = None
        self._steps = self._validate(steps)
        # Seed with the node's recent output so a LEADING wait_for can match a
        # prompt the target is already showing (e.g. a box idling at `login:`
        # before the job started). Once an action runs, the buffer is cleared so
        # later waits only see post-action output.
        self._buf = initial_tail or ""
        self._event = asyncio.Event()
        self._task = None

    def _validate(self, steps):
        if not isinstance(steps, list) or not steps:
            raise ExpectError("steps must be a non-empty list")
        if len(steps) > MAX_STEPS:
            raise ExpectError("too many steps (max %d)" % MAX_STEPS)
        out = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                raise ExpectError("step %d is not an object" % i)
            if "wait_for" in step:
                w = step["wait_for"]
                if isinstance(w, str):
                    w = {"regex": w}
                regex = w.get("regex", "")
                if not isinstance(regex, str) or not regex:
                    raise ExpectError("step %d wait_for needs a regex" % i)
                if len(regex) > MAX_REGEX_LEN:
                    raise ExpectError("step %d regex too long" % i)
                try:
                    rx = re.compile(regex, re.M)
                except re.error as e:
                    raise ExpectError("step %d bad regex: %s" % (i, e))
                timeout = min(_int_field(w.get("timeout_ms", DEFAULT_WAIT_MS),
                                         "step %d timeout_ms" % i), MAX_WAIT_MS)
                on_timeout = w.get("on_timeout", "fail")
                if on_timeout not in ("fail", "continue"):
                    raise ExpectError("step %d on_timeout must be fail|continue" % i)
                out.append(("wait", rx, timeout, on_timeout, regex))
            elif "delay_ms" in step:
                out.append(("delay", min(_int_field(step["delay_ms"], "step %d delay_ms" % i), MAX_WAIT_MS)))
            else:
                cmd = _build_command(step)  # raises on unknown type
                out.append(("action", cmd))
        return out

    def feed(self, text):
        """Called from the output path with fresh serial output."""
        self._buf = update_tail(self._buf, text)
        self._event.set()

    def snapshot(self):
        return {
            "job_id": self.job_id, "node_id": self.node_id, "status": self.status,
            "step": self.step_index, "total": self.total, "detail": self.detail,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }

    def _progress(self, hub, phase, detail=""):
        self.detail = detail
        hub.eventbus.broadcast({
            "event": "expect_progress", "id": self.node_id, "job_id": self.job_id,
            "step": self.step_index, "total": self.total, "phase": phase, "detail": detail,
        })

    async def _wait_for(self, rx, timeout_ms):
        # The buffer is cleared just before each action dispatch, so a wait here
        # already sees only output produced since the preceding action — no
        # mid-wait clear (which would race the very output we want to catch).
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_ms / 1000
        while True:
            if rx.search(self._buf[-MATCH_WINDOW:]):
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._event.clear()
            try:
                await asyncio.wait_for(self._event.wait(), remaining)
            except asyncio.TimeoutError:
                return False

    async def run(self, hub):
        self.status = "running"
        wall_deadline = now_ms() + MAX_JOB_WALL_MS
        try:
            for i, step in enumerate(self._steps):
                self.step_index = i
                if now_ms() > wall_deadline:
                    self.status = "timeout"
                    self._progress(hub, "timeout", "job exceeded max wall-time")
                    return
                kind = step[0]
                if kind == "action":
                    cmd = step[1]
                    body = {k: v for k, v in cmd.items() if v is not None}
                    # Drop any output that preceded this action so the *next*
                    # wait_for only sees what this action produces.
                    self._buf = ""
                    res = await hub.dispatch_command(self.node_id, body, issued_by="expect")
                    if not res.get("ok"):
                        self.status = "failed"
                        self._progress(hub, "failed", "action failed: %s" % res.get("error"))
                        return
                    self._progress(hub, "action", "%s" % cmd.get("type"))
                elif kind == "delay":
                    self._progress(hub, "delay", "%dms" % step[1])
                    await asyncio.sleep(step[1] / 1000)
                elif kind == "wait":
                    _, rx, timeout_ms, on_timeout, regex = step
                    self._progress(hub, "wait", "for /%s/" % regex)
                    matched = await self._wait_for(rx, timeout_ms)
                    if not matched:
                        if on_timeout == "continue":
                            self._progress(hub, "wait_timeout_continue", "/%s/ not seen; continuing" % regex)
                            continue
                        self.status = "timeout"
                        self._progress(hub, "timeout", "/%s/ not seen in %dms" % (regex, timeout_ms))
                        return
                    self._progress(hub, "matched", "/%s/" % regex)
            self.status = "done"
            self.step_index = self.total
            self._progress(hub, "done", "all steps completed")
        except asyncio.CancelledError:
            self.status = "cancelled"
            self._progress(hub, "cancelled", "job cancelled")
            raise
        except Exception as e:  # never take down the loop
            self.status = "failed"
            self._progress(hub, "failed", "error: %s" % e)
        finally:
            self.finished_at = now_ms()
            self.manager._on_finish(self)


class ExpectManager:
    """Owns running/finished expect jobs. One running job per node."""

    def __init__(self, hub):
        self.hub = hub
        self._by_job = {}      # job_id -> ExpectJob
        self._active = {}      # node_id -> job_id (running only)

    def is_busy(self, node_id):
        return node_id in self._active

    def start(self, node_id, steps):
        if node_id in self._active:
            return {"ok": False, "error": "busy", "detail": "an expect job is already running on %s" % node_id}
        state = self.hub.registry.get(node_id)
        initial_tail = state.tail if state else ""
        try:
            job = ExpectJob(self, gen_cmd_id(), node_id, steps, initial_tail=initial_tail)
        except ExpectError as e:
            return {"ok": False, "error": "bad_expect", "detail": str(e)}
        self._by_job[job.job_id] = job
        self._active[node_id] = job.job_id
        job._task = asyncio.get_event_loop().create_task(job.run(self.hub))
        return {"ok": True, "job_id": job.job_id, "status": job.status}

    def feed(self, node_id, text):
        job_id = self._active.get(node_id)
        if job_id is not None:
            job = self._by_job.get(job_id)
            if job is not None:
                job.feed(text)

    def get(self, job_id):
        job = self._by_job.get(job_id)
        return job.snapshot() if job else None

    def cancel(self, job_id):
        job = self._by_job.get(job_id)
        if job is None:
            return {"ok": False, "error": "not_found"}
        if job._task and not job._task.done():
            job._task.cancel()
        return {"ok": True}

    def _on_finish(self, job):
        # Free the node for a new job; keep the snapshot queryable for a while,
        # then let it be pruned lazily.
        if self._active.get(job.node_id) == job.job_id:
            self._active.pop(job.node_id, None)
        self._prune()

    def _prune(self):
        cut = now_ms() - FINISHED_TTL_MS
        stale = [jid for jid, j in self._by_job.items()
                 if j.finished_at is not None and j.finished_at < cut]
        for jid in stale:
            self._by_job.pop(jid, None)
