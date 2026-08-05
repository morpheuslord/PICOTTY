"""OTA orchestration: push a firmware bundle to nodes from the dashboard.

Updating firmware by hand means touching every Pico; for a rack that is the real
operational cost. A node that advertises the `ota` capability can receive new
files over the wire (ota_begin -> ota_chunk* -> ota_commit) and soft-reload onto
them, with a watchdog-revert safety net on the node side (see otaflash.py).

This module is the hub half:
  * Bundle storage — a directory per bundle under hub/data/firmware/<name>/ with
    a manifest (each file's path, size, SHA-256) plus the file blobs.
  * Per-node push — begin, stream small checksummed chunks, commit, then WATCH
    for the node to reconnect (proof the new firmware booted). Progress streams
    as `ota_progress` events.
  * Canary rollout — update one node, confirm it comes back healthy, THEN proceed
    to the rest staggered, so a bad bundle can never brick the whole fleet at once.

Every request goes through hub.request (an `r_` request/reply with no per-chunk
command row), so a large bundle doesn't flood the DB.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re

from . import config
from .utils import gen_cmd_id, now_ms

CHUNK = 512            # raw bytes per ota_chunk (1024 hex chars << 16 KB frame cap)
BEGIN_TIMEOUT = 20.0
CHUNK_TIMEOUT = 15.0
COMMIT_TIMEOUT = 30.0
RECONNECT_TIMEOUT_MS = 45_000  # how long to wait for the node to come back healthy
JOB_TTL_MS = 3_600_000
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MAX_FILES = 400              # a full firmware (with lib/) is many files
_MAX_FILE_BYTES = 1 << 20     # 1 MiB/file — Pico modules are tiny; this is a sanity cap
_MAX_BUNDLE_BYTES = 8 << 20   # 8 MiB total across a bundle
# Junk that shows up in zips and must never be staged.
_ZIP_SKIP = ("__MACOSX/", ".DS_Store", "boot_out.txt", "Thumbs.db")


class OTAError(Exception):
    pass


def _safe_rel(path):
    return isinstance(path, str) and path and path[0] != "/" and ".." not in path.split("/")


class OTAManager:
    def __init__(self, hub):
        self.hub = hub
        self.dir = config.PROCESS.db_path.parent / "firmware"
        self._jobs = {}   # job_id -> snapshot dict

    # -- bundle storage -------------------------------------------------------

    def _ensure_dir(self):
        self.dir.mkdir(parents=True, exist_ok=True)

    def create_bundle(self, name: str, files: list) -> dict:
        """files: [{"path": str, "content_b64": str}]. Writes the blobs + a
        manifest and returns the manifest."""
        if not _NAME_RE.match(name or ""):
            raise OTAError("bundle name must match [A-Za-z0-9._-]{1,64}")
        if not files or len(files) > _MAX_FILES:
            raise OTAError("bundle must have 1..%d files" % _MAX_FILES)
        self._ensure_dir()
        bdir = self.dir / name
        blobs = bdir / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        manifest_files = []
        hasher_total = hashlib.sha256()
        total = 0
        for i, f in enumerate(files):
            path = f.get("path")
            if not _safe_rel(path):
                raise OTAError("unsafe path: %r" % path)
            try:
                content = base64.b64decode(f.get("content_b64", ""), validate=True)
            except Exception:
                raise OTAError("bad base64 for %s" % path)
            if len(content) > _MAX_FILE_BYTES:
                raise OTAError("%s exceeds %d bytes" % (path, _MAX_FILE_BYTES))
            total += len(content)
            if total > _MAX_BUNDLE_BYTES:
                raise OTAError("bundle exceeds %d bytes total" % _MAX_BUNDLE_BYTES)
            blob = blobs / ("f%d.bin" % i)
            blob.write_bytes(content)
            hasher_total.update(content)
            manifest_files.append({
                "path": path, "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(), "blob": blob.name,
            })
        manifest = {
            "name": name, "files": manifest_files,
            "total_sha256": hasher_total.hexdigest(), "created_at": now_ms(),
        }
        (bdir / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    def create_bundle_from_zip(self, name: str, zip_bytes: bytes) -> dict:
        """Build a bundle from an uploaded .zip — the hub decompresses it and
        stages every file, so an operator can upload firmware/build/<node>.zip
        (or any zip) instead of picking files one by one.

        A single common top-level directory is stripped (so zipping the FOLDER
        `Node-Main/` still yields `code.py`, not `Node-Main/code.py`). Junk
        entries (__MACOSX, .DS_Store, boot_out.txt) are skipped, and every path
        is validated the same way as a hand-built bundle."""
        import io
        import zipfile
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile:
            raise OTAError("not a valid zip file")
        names = [i.filename for i in zf.infolist()
                 if not i.is_dir() and not any(s in i.filename for s in _ZIP_SKIP)]
        if not names:
            raise OTAError("zip contains no usable files")
        # Strip a single shared leading directory if every entry has one.
        prefix = ""
        first = names[0].split("/")
        if len(first) > 1 and all(n.startswith(first[0] + "/") for n in names):
            prefix = first[0] + "/"
        files = []
        for info in zf.infolist():
            if info.is_dir() or any(s in info.filename for s in _ZIP_SKIP):
                continue
            rel = info.filename[len(prefix):] if prefix else info.filename
            if not rel:
                continue
            if not _safe_rel(rel):
                raise OTAError("unsafe path in zip: %r" % info.filename)
            files.append({"path": rel, "content_b64": base64.b64encode(zf.read(info)).decode()})
        return self.create_bundle(name, files)

    def list_bundles(self) -> list:
        if not self.dir.exists():
            return []
        out = []
        for child in sorted(self.dir.iterdir()):
            mf = child / "manifest.json"
            if mf.is_file():
                try:
                    m = json.loads(mf.read_text())
                    out.append({"name": m["name"], "files": [f["path"] for f in m["files"]],
                                "total_sha256": m.get("total_sha256"), "created_at": m.get("created_at")})
                except Exception:
                    pass
        return out

    def get_manifest(self, name: str) -> dict:
        mf = self.dir / name / "manifest.json"
        if not mf.is_file():
            return None
        return json.loads(mf.read_text())

    def _blob_bytes(self, name, blob):
        return (self.dir / name / "blobs" / blob).read_bytes()

    # -- per-node push --------------------------------------------------------

    def _job_snapshot(self, job):
        return {k: job[k] for k in
                ("job_id", "node_id", "bundle", "status", "phase", "sent_bytes",
                 "total_bytes", "detail", "started_at", "finished_at")}

    def get_job(self, job_id):
        job = self._jobs.get(job_id)
        return self._job_snapshot(job) if job else None

    def _progress(self, job, phase, detail=""):
        job["phase"] = phase
        job["detail"] = detail
        self.hub.eventbus.broadcast({
            "event": "ota_progress", "job_id": job["job_id"], "id": job["node_id"],
            "status": job["status"], "phase": phase, "detail": detail,
            "sent_bytes": job["sent_bytes"], "total_bytes": job["total_bytes"],
        })

    def start_push(self, node_id: str, bundle: str) -> dict:
        manifest = self.get_manifest(bundle)
        if manifest is None:
            return {"ok": False, "error": "no_bundle", "detail": "no such bundle %s" % bundle}
        if not self.hub.node_supports(node_id, "ota"):
            return {"ok": False, "error": "unsupported", "detail": "node does not advertise ota"}
        job_id = "ota_" + gen_cmd_id()[2:]
        total = sum(f["size"] for f in manifest["files"])
        job = {"job_id": job_id, "node_id": node_id, "bundle": bundle, "status": "running",
               "phase": "begin", "sent_bytes": 0, "total_bytes": total, "detail": "",
               "started_at": now_ms(), "finished_at": None}
        self._jobs[job_id] = job
        asyncio.get_event_loop().create_task(self._run_push(job, manifest))
        self._prune()
        return {"ok": True, "job_id": job_id, "total_bytes": total}

    async def _run_push(self, job, manifest):
        node_id = job["node_id"]
        state = self.hub.registry.get(node_id)
        pre_connected = state.connected_at if state else 0
        try:
            begin = await self.hub.request(node_id, {
                "type": "ota_begin",
                "files": [{"path": f["path"], "size": f["size"], "sha256": f["sha256"]} for f in manifest["files"]],
                "total_sha256": manifest.get("total_sha256"),
            }, timeout=BEGIN_TIMEOUT)
            if not begin.get("ok") or begin.get("status") != "ok":
                return self._fail(job, "begin rejected: %s" % (begin.get("payload") or begin.get("error")))
            self._progress(job, "begin", "manifest accepted")

            for f in manifest["files"]:
                data = self._blob_bytes(manifest["name"], f["blob"])
                seq = 0
                for off in range(0, len(data), CHUNK):
                    chunk = data[off:off + CHUNK]
                    r = await self.hub.request(node_id, {
                        "type": "ota_chunk", "path": f["path"], "seq": seq, "data": chunk.hex(),
                    }, timeout=CHUNK_TIMEOUT)
                    if not r.get("ok") or r.get("status") != "ok":
                        return self._fail(job, "chunk failed on %s: %s" % (f["path"], r.get("payload") or r.get("error")))
                    seq += 1
                    job["sent_bytes"] += len(chunk)
                    self._progress(job, "chunk", "%s %d/%d B" % (f["path"], job["sent_bytes"], job["total_bytes"]))

            job["status"] = "committing"
            self._progress(job, "commit", "verifying + swapping on node")
            commit = await self.hub.request(node_id, {"type": "ota_commit"}, timeout=COMMIT_TIMEOUT)
            # The node acks ok then reloads and drops the link; a send_failed /
            # timeout right after a swap is expected, so only an explicit failed
            # result is a hard failure.
            if commit.get("ok") and commit.get("status") == "failed":
                return self._fail(job, "commit failed: %s" % commit.get("payload"))
            job["status"] = "committed"
            self._progress(job, "committed", "waiting for node to reboot on new firmware")
            # Provenance: the node's fw_version comes from code.py's FW_VERSION, not
            # the bundle name, so record which bundle was pushed for the UI to show.
            try:
                await self.hub.db.set_last_ota(node_id, "%s @ %d" % (job["bundle"], now_ms()))
            except Exception:
                pass
            await self.hub.audit("cmd", node_id, "OTA committed bundle %s (%s)" % (job["bundle"], job["job_id"]))

            # Canary: confirm the node comes back (proof the new firmware booted).
            if await self._await_reconnect(node_id, pre_connected):
                job["status"] = "healthy"
                self._progress(job, "healthy", "node reconnected on new firmware")
            else:
                job["status"] = "unconfirmed"
                self._progress(job, "unconfirmed", "node did not reconnect in time (may still recover/revert)")
        except Exception as e:
            self._fail(job, "error: %s" % e)
        finally:
            job["finished_at"] = now_ms()

    async def _await_reconnect(self, node_id, pre_connected):
        deadline = now_ms() + RECONNECT_TIMEOUT_MS
        while now_ms() < deadline:
            st = self.hub.registry.get(node_id)
            if st is not None and st.status == "online" and st.connected_at > pre_connected:
                return True
            await asyncio.sleep(0.5)
        return False

    def _fail(self, job, detail):
        job["status"] = "failed"
        job["finished_at"] = now_ms()
        self._progress(job, "failed", detail)
        return None

    def _prune(self):
        cut = now_ms() - JOB_TTL_MS
        for jid in [j for j, v in self._jobs.items()
                    if v.get("finished_at") and v["finished_at"] < cut]:
            self._jobs.pop(jid, None)

    # -- canary bulk rollout --------------------------------------------------

    async def rollout(self, node_ids: list, bundle: str, stagger_ms: int = 0) -> dict:
        """Update the first node, wait until it is healthy, then the rest one at a
        time (staggered). Aborts the rollout if the canary does not come back."""
        if self.get_manifest(bundle) is None:
            return {"ok": False, "error": "no_bundle"}
        node_ids = list(dict.fromkeys(node_ids))
        started, skipped = [], []
        for i, nid in enumerate(node_ids):
            res = self.start_push(nid, bundle)
            if not res.get("ok"):
                skipped.append({"id": nid, "reason": res.get("error")})
                continue
            started.append({"id": nid, "job_id": res["job_id"]})
            if i == 0:
                # Canary gate: only proceed past the first node once it is healthy.
                job = self._jobs[res["job_id"]]
                deadline = now_ms() + RECONNECT_TIMEOUT_MS + COMMIT_TIMEOUT * 1000
                while now_ms() < deadline and job["status"] not in ("healthy", "failed", "unconfirmed"):
                    await asyncio.sleep(0.5)
                if job["status"] != "healthy":
                    return {"ok": True, "canary": job["status"], "aborted": True,
                            "started": started, "skipped": skipped,
                            "detail": "canary %s did not confirm healthy; rollout stopped" % nid}
            elif stagger_ms:
                await asyncio.sleep(stagger_ms / 1000)
        return {"ok": True, "aborted": False, "started": started, "skipped": skipped}
