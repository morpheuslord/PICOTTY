# messages.py — builders for every node-to-hub message type.
#
# Keeping these in one place makes the node's half of the wire protocol easy to
# read. Each returns a plain dict; wire.encode turns it into a frame.


def hello(node_id, token, fw, cap, layout=None):
    """Sent once on connect. The hub validates `token` before anything else.

    `layout` is the active keyboard layout code (e.g. "us", "de"); the hub keeps
    it as read-only node detail. Omitted by old firmware, which the hub treats as
    unknown/US."""
    msg = {"type": "hello", "id": node_id, "token": token, "fw": fw, "cap": cap}
    if layout is not None:
        msg["layout"] = layout
    return msg


def heartbeat(node_id, host=None):
    """Liveness pulse on the heartbeat interval. Not persisted per beat.

    `host` reports whether the TARGET machine's USB host has us enumerated — a
    proxy for 'the machine is powered and running'. True = machine up, False =
    node still powered (e.g. USB standby) but the target is off/hung, None =
    firmware too old to report it. Lets the hub show target liveness distinctly
    from node liveness."""
    msg = {"type": "heartbeat", "id": node_id}
    if host is not None:
        msg["host"] = bool(host)
    return msg


def result(cmd_id, status, payload=None):
    """A command finished. status is 'ok', 'failed', or 'timeout'."""
    msg = {"type": "result", "cmd_id": cmd_id, "status": status}
    if payload is not None:
        msg["payload"] = payload
    return msg


def output(text, ts):
    """A chunk of target serial output not tied to a specific command."""
    return {"type": "output", "text": text, "ts": ts}


def pong(nonce):
    """Reply to a ping; the hub matches the nonce to compute RTT."""
    return {"type": "pong", "nonce": nonce}


def error(detail, cmd_id=None):
    """Something the node wants recorded as an audit event."""
    msg = {"type": "error", "detail": detail}
    if cmd_id is not None:
        msg["cmd_id"] = cmd_id
    return msg


def bye():
    """Graceful shutdown notice so the hub marks us offline without waiting."""
    return {"type": "bye"}
