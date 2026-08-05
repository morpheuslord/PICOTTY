"""PICOTTY Telegram sidecar.

A separate process (Option B in the design doc) that bridges the hub to Telegram
over outbound-only long polling. It talks to the hub solely through the REST API
and /ws event stream on :8080, holds no hub internals, and can be stopped without
touching the hub — the kill switch for the entire external surface.
"""

__version__ = "1.0.0"
