"""Sidecar-local audit log.

Every action over this remote surface is recorded: the chat id, the command, the
node, the outcome, and session open/close. The design doc weighs central hub-side
audit against sidecar isolation; for v1 we keep the record inside the sidecar
(Option B — the bot holds no hub internals and needs no extra hub write endpoint,
so its blast radius stays exactly the read/command REST surface). The log is an
append-only JSONL file at chmod 600, owned by the sidecar user.

Never log the bot token or the TOTP secret. Typed shell input is recorded as a
command event so there is a trail of what was sent to a target.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Create with restrictive perms up front so the record is never briefly
        # world-readable between creation and a later chmod.
        if not self._path.exists():
            fd = os.open(self._path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            os.close(fd)
        else:
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

    async def record(self, action: str, *, chat_id=None, node=None, detail=None,
                     ok: bool | None = None) -> None:
        entry = {
            "ts": int(time.time() * 1000),
            "action": action,
            "chat_id": chat_id,
            "node": node,
            "detail": detail,
            "ok": ok,
        }
        line = json.dumps({k: v for k, v in entry.items() if v is not None}) + "\n"
        async with self._lock:
            # Blocking append under the lock; entries are tiny and infrequent
            # relative to the event loop's other work.
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)
