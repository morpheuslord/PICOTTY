"""Wire framing for the swarm link, hub side.

Same contract the node implements: a 4-byte big-endian length followed by that
many bytes of UTF-8 JSON. On asyncio we can lean on StreamReader.readexactly,
which blocks until exactly the requested bytes arrive and raises on EOF.
"""

from __future__ import annotations

import asyncio
import json
import struct

# The wire-protocol version. Advisory for now: the node may echo it in `hello`
# and the hub can warn on a mismatch, but feature-level negotiation still runs
# through the capability flags (hid / cdc / serial_tx / ota), which is what
# actually gates behavior. Bump only on an incompatible framing/semantics change.
PROTOCOL_VERSION = 1

# Hard cap on a single inbound frame. A node result or output chunk is small;
# anything enormous is a desync or a bad actor, and we drop the connection.
MAX_FRAME_BYTES = 1 << 20  # 1 MiB

# Per-command cap on a `send`'s UTF-8 `data`. A serial getty consumes bytes far
# slower than TCP delivers them, and the node buffers only a few KB, so we reject
# an oversized payload at the REST layer rather than letting it back up the node.
SEND_DATA_MAX = 4096


class ProtocolError(Exception):
    """A framing-level violation. Fatal for the connection."""


def _is_hex(s: str) -> bool:
    """True for an even-length string of hex digits (a valid `raw` payload)."""
    if not isinstance(s, str) or not s or len(s) % 2 != 0:
        return False
    try:
        bytes.fromhex(s)
    except ValueError:
        return False
    return True


def validate_send(command: dict):
    """Validate a `send` command body from the REST layer.

    Enforces `data` xor `raw`, hex-validates `raw`, and size-caps `data`.
    Returns (True, None, None) when valid, else (False, error_code, detail)
    where error_code is 'too_large' (map to 413) or 'bad_command' (map to 422).
    The node re-validates defensively, but rejecting here keeps a malformed or
    oversized frame from ever reaching it.
    """
    data = command.get("data")
    raw = command.get("raw")
    has_data = data is not None
    has_raw = raw is not None
    if has_data == has_raw:
        return False, "bad_command", "send requires exactly one of 'data' or 'raw'"
    if has_data:
        if not isinstance(data, str):
            return False, "bad_command", "'data' must be a string"
        if len(data.encode("utf-8")) > SEND_DATA_MAX:
            return False, "too_large", "'data' exceeds %d bytes" % SEND_DATA_MAX
    elif not _is_hex(raw):
        return False, "bad_command", "'raw' must be an even-length hex string"
    return True, None, None


def encode_frame(obj) -> bytes:
    """Serialize a message dict to a complete on-wire frame."""
    body = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> dict:
    """Read exactly one framed message. Raises IncompleteReadError at EOF and
    ProtocolError on an oversized or undecodable frame."""
    header = await reader.readexactly(4)
    (length,) = struct.unpack(">I", header)
    if length > MAX_FRAME_BYTES:
        raise ProtocolError("frame length %d exceeds cap %d" % (length, MAX_FRAME_BYTES))
    body = await reader.readexactly(length)
    try:
        msg = json.loads(body)
    except ValueError as e:
        raise ProtocolError("bad JSON body: %s" % e)
    if not isinstance(msg, dict):
        raise ProtocolError("frame body is not a JSON object")
    return msg
