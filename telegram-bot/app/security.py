"""Access control for the sidecar.

Two independent gates:

1. Chat-ID allowlist — checked on EVERY update, not just at session start. An
   unknown chat gets silence (logged), never an error reply that would confirm
   the bot exists.

2. Break-glass arming for the terminal tier — the allowlist alone is not enough,
   because a compromised phone or Telegram account would otherwise hold shell
   access. /shell, /reboot, /sysrq require the shell to be ARMED, which needs a
   valid TOTP via /arm. Arming lasts a bounded window then auto-disarms. Stats
   and alerts are always on; state-changing actions are opt-in per window.
"""

from __future__ import annotations

import time

try:
    import pyotp
except ImportError:  # surfaced clearly at startup by __main__
    pyotp = None


class Security:
    def __init__(self, allowed_chat_ids: frozenset[int], totp_secret: str,
                 arm_window_s: int):
        self._allowed = allowed_chat_ids
        self._totp_secret = totp_secret
        self._arm_window_s = arm_window_s
        self._armed_until = 0.0       # monotonic-ish wall clock (time.time)
        self._armed_by = None         # chat id that armed
        self._last_totp = None        # last accepted code, for replay rejection

    # -- allowlist ------------------------------------------------------------

    def is_allowed(self, chat_id: int | None) -> bool:
        return chat_id is not None and chat_id in self._allowed

    def update(self, allowed_chat_ids: frozenset[int], totp_secret: str,
               arm_window_s: int) -> None:
        """Apply a hot-reloaded configuration. The allowlist, TOTP secret and arm
        window can change live; an armed session is left intact."""
        self._allowed = allowed_chat_ids
        self._totp_secret = totp_secret
        self._arm_window_s = arm_window_s

    # -- break-glass arming ---------------------------------------------------

    def arm(self, chat_id: int, code: str) -> tuple[bool, str]:
        """Verify a TOTP and arm the shell tier. Returns (ok, message)."""
        if pyotp is None:
            return False, "TOTP library unavailable on the sidecar host."
        if not self._totp_secret:
            return False, "Shell tier has no TOTP secret configured; it is disabled."
        code = (code or "").strip().replace(" ", "")
        if not code.isdigit():
            return False, "Provide the 6-digit code: /arm 123456"
        totp = pyotp.TOTP(self._totp_secret)
        # valid_window=1 tolerates one 30s step of clock skew either way.
        if not totp.verify(code, valid_window=1):
            return False, "Invalid or expired code."
        # Reject an immediate replay of the same code within its validity window.
        if code == self._last_totp and self.is_armed():
            return False, "That code was just used; wait for the next one."
        self._last_totp = code
        self._armed_until = time.time() + self._arm_window_s
        self._armed_by = chat_id
        mins = self._arm_window_s // 60
        return True, "Shell armed for %d minutes. /disarm to end early." % mins

    def disarm(self) -> None:
        self._armed_until = 0.0
        self._armed_by = None

    def is_armed(self) -> bool:
        return time.time() < self._armed_until

    def armed_remaining_s(self) -> int:
        return max(0, int(self._armed_until - time.time()))
