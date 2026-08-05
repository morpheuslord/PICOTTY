"""Tier 3: line-oriented shell sessions bridged over chat.

This is not a PTY and does not pretend to be one. A session binds one chat to one
node's serial getty (built on the hub `send` command). Plain messages are written
with a trailing CR; control keys arrive as /ctrlc, /ctrld, /esc mapped to the
`send` command's hex `raw` field — the exact mechanism the dashboard serial mode
uses. Output is coalesced in a short window and flushed as monospace code blocks;
under sustained output the pump drops to summarized delivery so a boot log cannot
blow past Telegram's rate limit.

Constraints honored: one session per chat, one chat per node. Password entry is
safe for free — the getty does not echo, so nothing sensitive is relayed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

from . import formatting

# Control keys exposed as commands, mapped to hex for the `send` raw field.
CONTROL_KEYS = {
    "ctrlc": "03",   # SIGINT
    "ctrld": "04",   # EOF
    "ctrlz": "1a",   # SIGTSTP
    "esc": "1b",
    "tab": "09",
    "enter": "0d",
    "up": "1b5b41",
    "down": "1b5b42",
    "right": "1b5b43",
    "left": "1b5b44",
}

SendText = Callable[[str], Awaitable[None]]


class OutputPump:
    """Buffers a node's relayed output and flushes it to one chat on an interval.

    Coalescing is what keeps us inside Telegram's ~1 msg/s per-chat limit: a
    getty at 115200 baud can emit ~11.5 KB/s, far more than chat can carry, so
    beyond a per-window byte ceiling we summarize (keep the tail, note the skip).
    """

    def __init__(self, send: SendText, flush_interval_s: float, max_chunk: int,
                 summarize_bytes: int):
        self._send = send
        self._interval = flush_interval_s
        self._max_chunk = max_chunk
        self._summarize_bytes = summarize_bytes
        self._buf: list[str] = []
        self._pending = 0
        self._task: Optional[asyncio.Task] = None
        self._closed = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def feed(self, text: str) -> None:
        if self._closed or not text:
            return
        self._buf.append(text)
        self._pending += len(text)

    async def _run(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._interval)
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self) -> None:
        if not self._buf:
            return
        raw = "".join(self._buf)
        self._buf.clear()
        self._pending = 0
        text = formatting.strip_ansi(raw)
        if not text.strip():
            return
        skipped = 0
        if len(text) > self._summarize_bytes:
            skipped = len(text) - self._summarize_bytes
            text = text[-self._summarize_bytes:]
        for chunk in formatting.chunk_output(text, self._max_chunk):
            await self._send(formatting.code_block(chunk))
        if skipped:
            await self._send("<i>… %d bytes of output skipped (too fast to relay) …</i>" % skipped)

    async def close(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._flush()


class Session:
    def __init__(self, chat_id: int, node_id: str, pump: OutputPump):
        self.chat_id = chat_id
        self.node_id = node_id
        self.pump = pump
        self.opened_at = time.time()
        self.last_activity = time.time()

    def touch(self) -> None:
        self.last_activity = time.time()

    def idle_s(self) -> float:
        return time.time() - self.last_activity


class SessionManager:
    """Owns the live shell sessions and their subscription lifecycle.

    subscribe/unsubscribe are injected async callbacks into the WS relay so the
    manager can attach a node's output stream without owning the socket. The
    relay routes each subscribed node's output back in via feed_output().
    """

    def __init__(self, *, subscribe: Callable[[str], Awaitable[None]],
                 unsubscribe: Callable[[str], Awaitable[None]],
                 flush_interval_s: float, max_chunk: int, summarize_bytes: int,
                 idle_timeout_s: int):
        self._subscribe = subscribe
        self._unsubscribe = unsubscribe
        self._flush_interval_s = flush_interval_s
        self._max_chunk = max_chunk
        self._summarize_bytes = summarize_bytes
        self._idle_timeout_s = idle_timeout_s
        self._by_chat: dict[int, Session] = {}
        self._by_node: dict[str, Session] = {}
        self._reaper: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap())

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            self._reaper = None
        for chat_id in list(self._by_chat):
            await self.close(chat_id, reason="sidecar shutting down")

    def session_for_chat(self, chat_id: int) -> Optional[Session]:
        return self._by_chat.get(chat_id)

    def active_nodes(self) -> list[str]:
        return list(self._by_node)

    async def open(self, chat_id: int, node_id: str, send: SendText) -> tuple[bool, str]:
        if chat_id in self._by_chat:
            existing = self._by_chat[chat_id]
            return False, "You already have a session on %s. /end it first." % existing.node_id
        if node_id in self._by_node:
            return False, "%s is already bound to another chat." % node_id
        pump = OutputPump(send, self._flush_interval_s, self._max_chunk, self._summarize_bytes)
        session = Session(chat_id, node_id, pump)
        self._by_chat[chat_id] = session
        self._by_node[node_id] = session
        pump.start()
        await self._subscribe(node_id)
        return True, "Session open on <b>%s</b>. Type to send; /end to close." % node_id

    async def close(self, chat_id: int, reason: str = "closed") -> Optional[str]:
        session = self._by_chat.pop(chat_id, None)
        if not session:
            return None
        self._by_node.pop(session.node_id, None)
        await self._unsubscribe(session.node_id)
        await session.pump.close()
        return session.node_id

    def feed_output(self, node_id: str, text: str) -> None:
        session = self._by_node.get(node_id)
        if session:
            session.pump.feed(text)
            session.touch()

    async def _reap(self) -> None:
        try:
            while True:
                await asyncio.sleep(5)
                for chat_id, session in list(self._by_chat.items()):
                    if session.idle_s() > self._idle_timeout_s:
                        node = await self.close(chat_id, reason="idle timeout")
                        # The caller wires a notifier via the closed-callback path;
                        # here we just rely on the pump's final flush. The bot layer
                        # sends the idle notice (see _on_idle_close).
                        if self.on_idle_close and node:
                            await self.on_idle_close(chat_id, node)
        except asyncio.CancelledError:
            pass

    # Optional async hook (chat_id, node_id) -> None, set by the bot layer to
    # notify a chat that its session auto-closed.
    on_idle_close: Optional[Callable[[int, str], Awaitable[None]]] = None
