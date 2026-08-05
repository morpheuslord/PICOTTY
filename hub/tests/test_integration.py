#!/usr/bin/env python3
"""End-to-end hub checks against a throwaway DB, over the real TCP + REST faces.

Boots the hub as a subprocess (one uvicorn worker, as in production) on private
ports with a known token, then drives it with tests/driver.py (a frame-level
node) and plain HTTP. Covers the improvement-plan phases that are server-side:
prompt-state, offline queue, expect engine, serial bridge, session recording.

    hub/.venv/bin/python hub/tests/test_integration.py

Exits non-zero if any check fails. No hardware, no network beyond loopback.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUB_DIR = HERE.parent
sys.path.insert(0, str(HUB_DIR))         # for `tests.driver`
sys.path.insert(0, str(HUB_DIR / "src"))  # for `picotty` from the src/ layout

from tests.driver import DriverNode  # noqa: E402

TOKEN = "itest-token-abc"
HTTP_PORT = 8097
TCP_PORT = 9097
BASE = "http://127.0.0.1:%d/api" % HTTP_PORT

_results = []


def record(name, ok, note=""):
    _results.append((name, ok))
    mark = "[PASS]" if ok else "[FAIL]"
    print("  %s %-26s %s" % (mark, name, note))


def http(method, path, body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def get_node(node_id):
    _, body = http("GET", "/nodes/%s" % node_id)
    return body.get("node", {})


async def wait_for(fn, timeout=5.0, interval=0.1):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        await asyncio.sleep(interval)
    return fn()


async def checks():
    # --- Phase 4: prompt-state detection ---------------------------------
    n = DriverNode("127.0.0.1", TCP_PORT, "drv-1", TOKEN)
    await n.connect()
    await wait_for(lambda: get_node("drv-1").get("status") == "online")
    await n.output("\nDebian GNU/Linux 12 tty1\n\ndebian login: ")
    got = await wait_for(lambda: get_node("drv-1").get("prompt_state") == "login")
    record("phase4 login-state", get_node("drv-1").get("prompt_state") == "login",
           "prompt_state=%s" % get_node("drv-1").get("prompt_state"))
    await n.output("root@debian:~# ")
    await wait_for(lambda: get_node("drv-1").get("prompt_state") == "shell")
    record("phase4 shell-state", get_node("drv-1").get("prompt_state") == "shell",
           "prompt_state=%s" % get_node("drv-1").get("prompt_state"))
    record("phase4 layout-passthrough", get_node("drv-1").get("layout") == "us")
    await n.close()

    # --- Target-machine liveness (node vs. attached machine) -------------
    tn = DriverNode("127.0.0.1", TCP_PORT, "drv-host", TOKEN)
    await tn.connect()
    await wait_for(lambda: get_node("drv-host").get("status") == "online")
    # Machine up: node reports the USB host is present.
    await tn.heartbeat(host=True)
    await wait_for(lambda: get_node("drv-host").get("target") == "up")
    record("target up (host present)", get_node("drv-host").get("target") == "up",
           "target=%s" % get_node("drv-host").get("target"))
    # Machine dead: node still alive but the target's USB host is gone.
    await tn.heartbeat(host=False)
    await wait_for(lambda: get_node("drv-host").get("target") == "down")
    record("target down (host gone, node alive)", get_node("drv-host").get("target") == "down",
           "target=%s" % get_node("drv-host").get("target"))
    # Fresh serial output proves the machine is alive regardless of host flag.
    await tn.output("root@box:~# uptime\n")
    await wait_for(lambda: get_node("drv-host").get("target") == "up")
    record("target up (recent output)", get_node("drv-host").get("target") == "up")
    await tn.close()
    # Node offline -> target unknown (can't tell; likely no power).
    await wait_for(lambda: get_node("drv-host").get("status") == "offline")
    record("target unknown when node offline", get_node("drv-host").get("target") == "unknown",
           "target=%s" % get_node("drv-host").get("target"))

    # --- Phase 5: expect engine ------------------------------------------
    n = DriverNode("127.0.0.1", TCP_PORT, "drv-exp", TOKEN)
    await n.connect()
    await wait_for(lambda: get_node("drv-exp").get("status") == "online")

    steps = [
        {"wait_for": {"regex": r"login:\s*$", "timeout_ms": 4000}},
        {"type": "send", "data": "root\n"},
        {"wait_for": {"regex": r"[Pp]assword:\s*$", "timeout_ms": 4000}},
        {"type": "send", "data": "secret\n"},
        {"wait_for": {"regex": r"#\s*$", "timeout_ms": 4000}},
    ]
    st, body = http("POST", "/nodes/drv-exp/expect", {"steps": steps})
    job_id = body.get("job_id")
    record("phase5 start", st == 200 and body.get("ok") and job_id, "job=%s" % job_id)

    # Busy: a second job on the same node must be rejected.
    st2, body2 = http("POST", "/nodes/drv-exp/expect", {"steps": steps})
    record("phase5 busy-reject", st2 == 409 and body2.get("error") == "busy")

    async def responder():
        # Drive the target side of the login conversation.
        await asyncio.sleep(0.2)
        await n.output("\ndebian login: ")
        # first send (root)
        f = await n.expect_frame(lambda fr: fr.get("type") == "send")
        await n.send({"type": "result", "cmd_id": f["cmd_id"], "status": "ok"})
        await n.output("Password: ")
        f = await n.expect_frame(lambda fr: fr.get("type") == "send")
        await n.send({"type": "result", "cmd_id": f["cmd_id"], "status": "ok"})
        await n.output("root@debian:~# ")

    await responder()
    done = await wait_for(lambda: http("GET", "/nodes/drv-exp/expect/%s" % job_id)[1]["job"]["status"] == "done", timeout=6)
    final = http("GET", "/nodes/drv-exp/expect/%s" % job_id)[1]["job"]
    record("phase5 completes", final["status"] == "done", "status=%s step=%s/%s" % (final["status"], final["step"], final["total"]))

    # A wrong-expectation job must fail cleanly on timeout, not hang.
    st, body = http("POST", "/nodes/drv-exp/expect",
                    {"steps": [{"wait_for": {"regex": "NEVER_APPEARS", "timeout_ms": 800}}]})
    jid = body.get("job_id")
    timed = await wait_for(lambda: http("GET", "/nodes/drv-exp/expect/%s" % jid)[1]["job"]["status"] == "timeout", timeout=4)
    record("phase5 timeout-clean", http("GET", "/nodes/drv-exp/expect/%s" % jid)[1]["job"]["status"] == "timeout")

    # Bad numeric field must be a clean 422, not a 500 (auditor fix).
    stbad, _ = http("POST", "/nodes/drv-exp/expect",
                    {"steps": [{"wait_for": {"regex": "x", "timeout_ms": "abc"}}]})
    record("phase5 bad-input-422", stbad == 422, "status=%s" % stbad)

    # A leading wait_for should match a prompt already on screen (seed from tail).
    await n.output("\nalready-at login: ")
    await asyncio.sleep(0.3)  # let the classifier tail absorb it
    st, body = http("POST", "/nodes/drv-exp/expect",
                    {"steps": [{"wait_for": {"regex": "login:", "timeout_ms": 1500}}]})
    seed_job = body.get("job_id")
    await wait_for(lambda: http("GET", "/nodes/drv-exp/expect/%s" % seed_job)[1]["job"]["status"] == "done", timeout=3)
    record("phase5 seed-existing-prompt",
           http("GET", "/nodes/drv-exp/expect/%s" % seed_job)[1]["job"]["status"] == "done")
    await n.close()

    # --- Phase 6: offline command queue ----------------------------------
    # Queue a command for a node that has never connected.
    st, body = http("POST", "/nodes/drv-queue/queue",
                    {"command": {"type": "keys", "chord": ["ENTER"]}, "ttl_ms": 60000})
    qid = body.get("id")
    record("phase6 enqueue", st == 200 and body.get("ok") and qid, "q=%s" % qid)
    _, ql = http("GET", "/nodes/drv-queue/queue")
    record("phase6 pending-listed", len(ql.get("queued", [])) == 1)

    # Also queue one that is already expired; it must be dropped, not delivered.
    http("POST", "/nodes/drv-queue/queue",
         {"command": {"type": "keys", "chord": ["ESCAPE"]}, "ttl_ms": 1})
    await asyncio.sleep(0.05)

    # Now the node connects: the ENTER should be delivered, the expired ESC dropped.
    nq = DriverNode("127.0.0.1", TCP_PORT, "drv-queue", TOKEN)
    await nq.connect()
    frame = await nq.expect_frame(lambda fr: fr.get("type") == "keys", timeout=5)
    record("phase6 delivered-on-connect", frame.get("chord") == ["ENTER"], "chord=%s" % frame.get("chord"))
    # No second (expired) command should arrive.
    got_expired = True
    try:
        await nq.expect_frame(lambda fr: fr.get("type") == "keys" and fr.get("chord") == ["ESCAPE"], timeout=1)
    except asyncio.TimeoutError:
        got_expired = False
    record("phase6 expired-dropped", not got_expired)
    _, ql2 = http("GET", "/nodes/drv-queue/queue")
    record("phase6 queue-drained", len(ql2.get("queued", [])) == 0)
    await nq.close()

    # --- Phase 7: session recording (asciicast v2) -----------------------
    nc = DriverNode("127.0.0.1", TCP_PORT, "drv-cast", TOKEN)
    await nc.connect()
    await wait_for(lambda: get_node("drv-cast").get("status") == "online")
    await nc.output("hello from the console\n")
    await asyncio.sleep(0.1)
    await nc.output("second line\n")
    await asyncio.sleep(0.6)  # let the output flusher persist
    cast_url = BASE + "/nodes/drv-cast/session.cast"
    with urllib.request.urlopen(cast_url, timeout=5) as r:
        lines = r.read().decode().splitlines()
    header_ok = False
    events = []
    try:
        header = json.loads(lines[0])
        header_ok = header.get("version") == 2 and "width" in header
        events = [json.loads(x) for x in lines[1:] if x.strip()]
    except Exception:
        pass
    record("phase7 cast-header", header_ok, "v=%s" % (json.loads(lines[0]).get("version") if lines else "—"))
    body_joined = "".join(e[2] for e in events if len(e) == 3 and e[1] == "o")
    record("phase7 cast-events", "hello from the console" in body_joined and "second line" in body_joined,
           "%d events" % len(events))
    record("phase7 cast-offsets", all(len(e) == 3 and e[1] == "o" and e[0] >= 0 for e in events))
    await nc.close()

    # --- Phase 8: raw serial bridge --------------------------------------
    BRIDGE_PORT = 10099
    http("PATCH", "/settings", {"serial_bridge_enabled": True})
    nb = DriverNode("127.0.0.1", TCP_PORT, "drv-bridge", TOKEN)
    await nb.connect()
    await wait_for(lambda: get_node("drv-bridge").get("status") == "online")
    st, body = http("POST", "/nodes/drv-bridge/bridge?port=%d" % BRIDGE_PORT)
    record("phase8 assign-port", st == 200 and body.get("ok") and body.get("port") == BRIDGE_PORT)
    await asyncio.sleep(0.3)  # listener binds via reconcile

    # A raw client (stand-in for minicom) connects to the node's bridge port.
    breader, bwriter = await asyncio.open_connection("127.0.0.1", BRIDGE_PORT)
    # client -> node: bytes should arrive as a `send` frame with the hex payload.
    bwriter.write(b"ls\n")
    await bwriter.drain()
    frame = await nb.expect_frame(lambda fr: fr.get("type") == "send" and fr.get("raw"), timeout=4)
    got_bytes = bytes.fromhex(frame["raw"]) if frame.get("raw") else b""
    record("phase8 client-to-node", got_bytes == b"ls\n", "raw=%s" % frame.get("raw"))
    # bridge writes carry a 'b_' cmd_id (swallowed, no DB row).
    record("phase8 bridge-cmdid", str(frame.get("cmd_id", "")).startswith("b_"))
    # node -> client: node output should be delivered to the socket.
    await nb.output("file1 file2\n")
    data = await asyncio.wait_for(breader.read(64), timeout=4)
    record("phase8 node-to-client", b"file1 file2" in data, "recv=%r" % data)
    bwriter.close()

    # Disabling the bridge must unbind the listener.
    http("PATCH", "/settings", {"serial_bridge_enabled": False})
    await asyncio.sleep(0.3)
    unbound = False
    try:
        _, w2 = await asyncio.open_connection("127.0.0.1", BRIDGE_PORT)
        w2.close()
    except OSError:
        unbound = True
    record("phase8 disable-unbinds", unbound)
    await nb.close()

    # --- Phase 11: alerting hooks ----------------------------------------
    captured = []

    async def capture(reader, writer):
        try:
            data = await reader.read(65536)
            # split headers/body on the blank line
            body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
            captured.append(body)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    cap_server = await asyncio.start_server(capture, "127.0.0.1", 0)
    cap_port = cap_server.sockets[0].getsockname()[1]
    http("PATCH", "/settings", {"alerts_enabled": True,
                                "alerts_webhook_url": "http://127.0.0.1:%d/hook" % cap_port})
    na = DriverNode("127.0.0.1", TCP_PORT, "drv-alert", TOKEN)
    await na.connect()
    await wait_for(lambda: get_node("drv-alert").get("status") == "online")
    await na.close()  # drops the socket -> node_down -> alert
    got = await wait_for(lambda: len(captured) > 0, timeout=6)
    alert_ok = False
    if captured:
        try:
            payload = json.loads(captured[0].decode())
            alert_ok = payload.get("kind") == "down" and payload.get("node_id") == "drv-alert"
        except Exception:
            pass
    record("phase11 webhook-fires", alert_ok, "%d captured" % len(captured))
    cap_server.close()

    # --- Phase 9: runbooks -----------------------------------------------
    rb_yaml = (
        "name: itest-login\n"
        "steps:\n"
        "  - wait_for: 'login:'\n"
        "    timeout_ms: 4000\n"
        "  - send: \"root\\n\"\n"
        "  - wait_for: '[Pp]assword:'\n"
        "    timeout_ms: 4000\n"
        "  - send: \"secret\\n\"\n"
        "  - wait_for: '# '\n"
        "    timeout_ms: 4000\n"
    )
    st, body = http("POST", "/runbooks", {"name": "itest-login", "yaml": rb_yaml})
    rb_id = body.get("id")
    record("phase9 create", st == 200 and rb_id, "id=%s" % rb_id)
    # Bad YAML is rejected at create time.
    stb, _ = http("POST", "/runbooks", {"name": "bad", "yaml": "steps: not-a-list"})
    record("phase9 reject-bad", stb == 422)

    nr = DriverNode("127.0.0.1", TCP_PORT, "drv-rb", TOKEN)
    await nr.connect()
    await wait_for(lambda: get_node("drv-rb").get("status") == "online")
    st, body = http("POST", "/runbooks/%d/run" % rb_id, {"node_ids": ["drv-rb"]})
    run_id = body.get("run_id")
    record("phase9 run-start", st == 200 and run_id, "run=%s" % run_id)

    async def rb_responder():
        await asyncio.sleep(0.3)
        await nr.output("\ndebian login: ")
        f = await nr.expect_frame(lambda fr: fr.get("type") == "send")
        await nr.send({"type": "result", "cmd_id": f["cmd_id"], "status": "ok"})
        await nr.output("Password: ")
        f = await nr.expect_frame(lambda fr: fr.get("type") == "send")
        await nr.send({"type": "result", "cmd_id": f["cmd_id"], "status": "ok"})
        await nr.output("root@debian:~# ")

    await rb_responder()

    def rb_node_status():
        _, b = http("GET", "/runbooks/%d/runs/%s" % (rb_id, run_id))
        return b.get("run", {}).get("nodes", {}).get("drv-rb", {}).get("status")

    await wait_for(lambda: rb_node_status() == "done", timeout=6)
    record("phase9 run-completes", rb_node_status() == "done", "node status=%s" % rb_node_status())
    await nr.close()

    # --- Phase 10: OTA firmware updates ----------------------------------
    import base64 as _b64
    files = [
        {"path": "code.py", "content_b64": _b64.b64encode(b"print('new firmware')\n").decode()},
        {"path": "lib/mod.mpy", "content_b64": _b64.b64encode(b"\x00\x01" * 400).decode()},
    ]
    st, body = http("POST", "/ota/bundles", {"name": "test-bundle", "files": files})
    record("phase10 bundle-create", st == 200 and body.get("ok"),
           "sha=%s" % (body.get("manifest", {}).get("total_sha256", "")[:8]))
    _, bl = http("GET", "/ota/bundles")
    record("phase10 bundle-listed", any(b["name"] == "test-bundle" for b in bl.get("bundles", [])))

    no = DriverNode("127.0.0.1", TCP_PORT, "drv-ota", TOKEN, caps=["hid", "cdc", "serial_tx", "ota"])
    await no.connect()
    await wait_for(lambda: get_node("drv-ota").get("status") == "online")

    # A node WITHOUT ota must be rejected.
    npo = DriverNode("127.0.0.1", TCP_PORT, "drv-noota", TOKEN, caps=["hid", "cdc"])
    await npo.connect()
    await wait_for(lambda: get_node("drv-noota").get("status") == "online")
    stno, _ = http("POST", "/nodes/drv-noota/ota", {"bundle": "test-bundle"})
    record("phase10 gate-unsupported", stno == 422)
    await npo.close()

    st, body = http("POST", "/nodes/drv-ota/ota", {"bundle": "test-bundle"})
    ota_job = body.get("job_id")
    record("phase10 push-start", st == 200 and ota_job, "job=%s total=%s" % (ota_job, body.get("total_bytes")))

    async def ota_responder(node):
        f = await node.expect_frame(lambda fr: fr.get("type") == "ota_begin", timeout=6)
        await node.send({"type": "result", "cmd_id": f["cmd_id"], "status": "ok"})
        chunks = 0
        while True:
            f = await node.expect_frame(lambda fr: fr.get("type") in ("ota_chunk", "ota_commit"), timeout=6)
            await node.send({"type": "result", "cmd_id": f["cmd_id"], "status": "ok"})
            if f["type"] == "ota_chunk":
                chunks += 1
            else:
                break
        return chunks

    chunks = await ota_responder(no)
    record("phase10 chunks-streamed", chunks >= 2, "%d chunks" % chunks)
    await no.close()  # simulate the soft-reload dropping the link
    await asyncio.sleep(0.3)
    # New firmware boots and reconnects (fresh connection).
    no2 = DriverNode("127.0.0.1", TCP_PORT, "drv-ota", TOKEN, caps=["hid", "cdc", "serial_tx", "ota"])
    await no2.connect()

    def ota_status():
        _, b = http("GET", "/nodes/drv-ota/ota/%s" % ota_job)
        return b.get("job", {}).get("status")

    await wait_for(lambda: ota_status() == "healthy", timeout=10)
    record("phase10 reconnect-healthy", ota_status() == "healthy", "status=%s" % ota_status())
    # Provenance: the node should now record which bundle it was flashed with.
    lota = get_node("drv-ota").get("last_ota") or ""
    record("phase10 last-ota-recorded", "test-bundle" in lota, "last_ota=%s" % lota)
    await no2.close()

    # ZIP upload: hub decompresses a .zip into a bundle (with a stripped top dir).
    import io as _io, zipfile as _zip
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w") as z:
        z.writestr("Node-X/boot.py", b"# boot\n")
        z.writestr("Node-X/code.py", b"print('zip fw')\n")
        z.writestr("Node-X/__MACOSX/junk", b"junk")
    zb64 = _b64.b64encode(buf.getvalue()).decode()
    stz, bz = http("POST", "/ota/bundles/zip", {"name": "ziptest", "zip_b64": zb64})
    paths = [f["path"] for f in bz.get("manifest", {}).get("files", [])] if bz.get("ok") else []
    record("phase10 zip-upload", stz == 200 and bz.get("ok"), "files=%s" % paths)
    record("phase10 zip-strips-topdir-and-junk",
           set(paths) == {"boot.py", "code.py"}, "paths=%s" % paths)

    # --- SysRq reboot + custom chords ------------------------------------
    sn = DriverNode("127.0.0.1", TCP_PORT, "drv-sysrq", TOKEN)
    await sn.connect()
    await wait_for(lambda: get_node("drv-sysrq").get("status") == "online")
    st, _ = http("POST", "/nodes/drv-sysrq/sysrq", {"key": "b"})
    frame = await sn.expect_frame(lambda fr: fr.get("type") == "sysrq", timeout=4)
    record("sysrq dispatched", st == 200 and frame.get("key") == "b", "key=%s" % frame.get("key"))
    stbad, _ = http("POST", "/nodes/drv-sysrq/sysrq", {"key": "bb"})
    record("sysrq rejects multi-char", stbad != 200 or _.get("ok") is False)
    await sn.close()

    st, cb = http("POST", "/chords", {"label": "VT2", "chord": ["ctrl", "alt", "f2"]})
    cid = cb.get("id")
    _, cl = http("GET", "/chords")
    made = next((c for c in cl.get("chords", []) if c["id"] == cid), None)
    record("chord create+normalize", made is not None and made["chord"] == ["CTRL", "ALT", "F2"],
           "chord=%s" % (made and made["chord"]))
    http("DELETE", "/chords/%d" % cid)
    _, cl2 = http("GET", "/chords")
    record("chord delete", not any(c["id"] == cid for c in cl2.get("chords", [])))

    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print("=== %d/%d integration checks passed ===" % (passed, total))
    return passed == total


def main():
    tmp = tempfile.mkdtemp()
    env = dict(os.environ)
    src = str(HUB_DIR / "src")
    env.update({
        "HUB_DB_PATH": os.path.join(tmp, "hub.db"),
        "HUB_HTTP_PORT": str(HTTP_PORT),
        "HUB_TCP_PORT": str(TCP_PORT),
        "HUB_HTTP_HOST": "127.0.0.1",
        "HUB_TCP_HOST": "127.0.0.1",
        "SWARM_NODE_TOKEN": TOKEN,
        # Ensure the uvicorn subprocess can import picotty from the src/ layout
        # even when the package isn't installed into this interpreter.
        "PYTHONPATH": src + os.pathsep + env.get("PYTHONPATH", ""),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "picotty.hub.main:app",
         "--host", "127.0.0.1", "--port", str(HTTP_PORT), "--log-level", "warning"],
        cwd=str(HUB_DIR), env=env,
    )
    try:
        # wait for health
        for _ in range(80):
            try:
                with urllib.request.urlopen(BASE + "/health", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.25)
        ok = asyncio.run(checks())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
