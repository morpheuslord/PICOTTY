# otaflash.py — receive a firmware bundle over the wire and swap it in, with a
# watchdog-revert safety net so a bad update self-reverts on the next reset.
#
# This is the highest-risk firmware surface: a bad node deploys to hardware you
# may not be able to reach. The safety rails, in order:
#
#   1. It only works when the filesystem is WRITABLE to CircuitPython. That needs
#      boot.py to have remounted "/" read-only-to-USB (OTA_ENABLED), which is the
#      production posture anyway (USB drive hidden). A node that can't write does
#      not advertise the `ota` capability, so the hub never tries to push to it.
#   2. Integrity is verified before anything is swapped: each file's SHA-256 and
#      size must match the manifest. SHA-256 comes from adafruit_hashlib; without
#      it the node reports `ota` unavailable rather than trusting an unchecked
#      transfer.
#   3. The swap keeps the previous file as <file>.bak and writes an /ota_pending
#      marker. On the NEXT boot, boot.py runs recover_if_pending(): if that boot
#      followed a WATCHDOG reset (the new firmware crash-looped), the .bak set is
#      restored automatically. Only once the new firmware has connected and run a
#      full heartbeat does it finalize() — dropping the marker and the .bak files —
#      so a later unrelated reset can never revert a firmware that already works.
#
# Chunks are small and written straight to a staging file, so a large bundle
# never grows memory; the cooperative loop keeps feeding the watchdog between
# chunk frames exactly as it does for serial output.

import os
import json

_STAGING = "/ota_staging"
_PENDING = "/ota_pending.json"
_PROBE = "/.ota_probe"


class OTAError(Exception):
    """A recoverable OTA-command failure: reported as a `failed` result, never a
    crash. The half-written staging area is discarded; the live firmware is
    untouched until a verified commit."""


def _sha_factory():
    try:
        from adafruit_hashlib import sha256
        return sha256
    except Exception:
        return None


def _fs_writable():
    try:
        with open(_PROBE, "w") as f:
            f.write("x")
        os.remove(_PROBE)
        return True
    except OSError:
        return False


def _safe_rel(path):
    """A bundle path must be a plain relative path under the drive root: no
    absolute path, no '..' traversal. Guards against a hostile manifest writing
    outside CIRCUITPY."""
    if not isinstance(path, str) or not path:
        return False
    if path[0] == "/" or ".." in path.split("/"):
        return False
    return True


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _replace(src, dst):
    """os.rename that overwrites: CircuitPython's rename fails if dst exists."""
    if _exists(dst):
        os.remove(dst)
    os.rename(src, dst)


def _mkdir(path):
    if not _exists(path):
        os.mkdir(path)


def _rmdir_contents(path):
    try:
        entries = os.listdir(path)
    except OSError:
        return
    for name in entries:
        full = path + "/" + name
        try:
            os.remove(full)
        except OSError:
            try:
                _rmdir_contents(full)
                os.rmdir(full)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


class OTA:
    def __init__(self):
        self._sha = _sha_factory()
        self.available = self._sha is not None and _fs_writable()
        self._done = False
        self._reset()

    def _reset(self):
        self._files = {}     # rel_path -> {size, sha, written, hasher, staged}
        self._order = []
        self._active = None  # (rel_path, open file handle) currently being written
        self._total_sha = None

    def _close_active(self):
        if self._active is not None:
            try:
                self._active[1].close()
            except OSError:
                pass
            self._active = None

    # -- command handlers (called from code.py dispatch) ---------------------

    def begin(self, msg):
        if not self.available:
            raise OTAError("ota unavailable (needs writable fs + adafruit_hashlib)")
        files = msg.get("files") or []
        if not files:
            raise OTAError("no files in manifest")
        self._close_active()
        self._reset()
        _rmdir_contents(_STAGING)
        _mkdir(_STAGING)
        for i, fdesc in enumerate(files):
            path = fdesc.get("path")
            if not _safe_rel(path):
                raise OTAError("unsafe path in manifest: %r" % path)
            staged = _STAGING + "/f%d.part" % i
            # start each staging file empty
            with open(staged, "wb") as f:
                pass
            self._files[path] = {
                "size": int(fdesc.get("size", 0)),
                "sha": fdesc.get("sha256", ""),
                "written": 0,
                "hasher": self._sha(),
                "staged": staged,
            }
            self._order.append(path)
        self._total_sha = msg.get("total_sha256")
        return "staged %d file(s)" % len(self._order)

    def chunk(self, msg, decode_hex):
        path = msg.get("path")
        rec = self._files.get(path)
        if rec is None:
            raise OTAError("chunk for unknown path %r" % path)
        data = decode_hex(msg.get("data", ""))
        if not data:
            return None
        if self._active is None or self._active[0] != path:
            self._close_active()
            self._active = (path, open(rec["staged"], "ab"))
        self._active[1].write(data)
        rec["written"] += len(data)
        rec["hasher"].update(data)
        return None

    def commit(self, msg):
        self._close_active()
        if not self._order:
            raise OTAError("commit with no begun bundle")
        # 1) verify every staged file before touching the live firmware.
        for path in self._order:
            rec = self._files[path]
            if rec["size"] and rec["written"] != rec["size"]:
                raise OTAError("size mismatch %s: got %d want %d" % (path, rec["written"], rec["size"]))
            if rec["sha"]:
                digest = rec["hasher"].hexdigest()
                if digest != rec["sha"]:
                    raise OTAError("sha256 mismatch for %s" % path)
        # 2) swap in, backing up each replaced file as <file>.bak.
        swapped = []
        for path in self._order:
            rec = self._files[path]
            dst = "/" + path
            if _exists(dst):
                _replace(dst, dst + ".bak")
            _replace(rec["staged"], dst)
            swapped.append(path)
        # 3) drop a pending marker so a crash-looping update self-reverts on the
        #    next watchdog boot (see recover_if_pending).
        with open(_PENDING, "w") as f:
            json.dump({"files": swapped}, f)
        _rmdir_contents(_STAGING)
        self._reset()
        return "committed %d file(s)" % len(swapped)

    def finalize(self):
        """Called by healthy running firmware: the update booted and connected,
        so it is good. Drop the pending marker and the .bak files so a later,
        unrelated watchdog reset does NOT revert working firmware. Idempotent and
        cheap when nothing is pending."""
        if self._done:
            return False
        self._done = True
        try:
            with open(_PENDING) as f:
                info = json.load(f)
        except OSError:
            return False
        for path in info.get("files", []):
            try:
                os.remove("/" + path + ".bak")
            except OSError:
                pass
        try:
            os.remove(_PENDING)
        except OSError:
            pass
        return True


def recover_if_pending(was_watchdog):
    """Run early in boot (from boot.py, after the writable remount).

    Returns None if no update is pending, "pending" if one is pending but this is
    a clean boot (leave it for the running firmware to finalize when healthy), or
    "reverted" if this boot followed a watchdog reset and the .bak set was
    restored. All filesystem errors are swallowed: recovery must never brick a
    node by raising in boot.py."""
    try:
        with open(_PENDING) as f:
            info = json.load(f)
    except OSError:
        return None
    if not was_watchdog:
        return "pending"
    for path in info.get("files", []):
        dst = "/" + path
        bak = dst + ".bak"
        if not _exists(bak):
            continue
        try:
            if _exists(dst):
                os.remove(dst)
            os.rename(bak, dst)
        except OSError:
            pass
    try:
        os.remove(_PENDING)
    except OSError:
        pass
    return "reverted"
