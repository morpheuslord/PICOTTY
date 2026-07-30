# wire.py — the length-prefixed JSON framing both hub and node speak.
#
# Each frame is a 4-byte big-endian unsigned length followed by that many bytes
# of UTF-8 JSON. Length-prefixing (not newline-delimiting) means a JSON body
# containing newlines or control characters needs no escaping, which matters
# because serial output and typed text carry arbitrary bytes.

import json
import struct


class ProtocolError(Exception):
    """A framing-level violation, e.g. an oversized length prefix. Fatal for
    the connection: the byte stream is desynced and cannot be trusted."""


def encode(obj):
    """Serialize a message dict to a complete on-wire frame (bytes)."""
    body = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(body)) + body


class FrameReader:
    """Accumulates received bytes and yields decoded message dicts.

    One preallocated bytearray is reused as the accumulator; complete frames are
    sliced off the front. A hard cap on frame size stops a malformed or hostile
    length prefix from growing memory without bound.
    """

    def __init__(self, max_frame_bytes=16384):
        self._buf = bytearray()
        self._max = max_frame_bytes

    def feed(self, data):
        """Append received bytes (accepts bytes or a memoryview slice)."""
        self._buf.extend(data)

    def pop(self):
        """Return the next complete message dict, or None if one isn't ready.

        Raises ProtocolError on an oversized frame or undecodable JSON, either
        of which means the stream is no longer trustworthy.
        """
        if len(self._buf) < 4:
            return None
        n = struct.unpack(">I", self._buf[:4])[0]
        if n > self._max:
            raise ProtocolError("frame length %d exceeds cap %d" % (n, self._max))
        if len(self._buf) < 4 + n:
            return None
        body = bytes(self._buf[4:4 + n])
        # Shift the remaining bytes down in place; keep the accumulator small.
        del self._buf[:4 + n]
        try:
            return json.loads(body)
        except ValueError as e:
            raise ProtocolError("bad JSON body: %s" % e)

    def reset(self):
        """Drop any buffered bytes. Called when a connection is torn down."""
        self._buf = bytearray()
