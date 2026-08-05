"""The wire protocol — public surface (``picotty.protocol``).

The authoritative implementation lives in :mod:`picotty.hub.protocol` (co-located
with the server that first defined it). This module re-exports it as a top-level
import surface so the client SDK, the simulator, tests, and any external script
share the exact same frame framing, validation, and constants instead of each
carrying a private copy — the compatibility guarantee the packaging design calls
for.
"""

from __future__ import annotations

from .hub.protocol import (  # noqa: F401
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    SEND_DATA_MAX,
    ProtocolError,
    encode_frame,
    read_frame,
    validate_send,
)

__all__ = [
    "PROTOCOL_VERSION",
    "MAX_FRAME_BYTES",
    "SEND_DATA_MAX",
    "ProtocolError",
    "encode_frame",
    "read_frame",
    "validate_send",
]
