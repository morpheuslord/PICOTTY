"""The durable store: SQLite via aiosqlite.

The registry holds live status; this holds identity and history. The TCP server
writes what nodes report (registrations, results, output, connection events);
the REST layer writes what operators change (labels, notes, groups, macros,
settings, the initial command row). Both read.

WAL is on so reads don't block the ingest writer. Output appends are batched
because that table is the highest-volume one and this may live on an SD card.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from . import config
from .utils import now_ms

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  id          TEXT PRIMARY KEY,
  label       TEXT NOT NULL DEFAULT '',
  group_name  TEXT NOT NULL DEFAULT '',
  notes       TEXT NOT NULL DEFAULT '',
  fw_version  TEXT,
  first_seen  INTEGER NOT NULL,
  last_seen   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  cmd_id       TEXT NOT NULL UNIQUE,
  node_id      TEXT NOT NULL,
  type         TEXT NOT NULL,
  payload      TEXT NOT NULL,
  issued_by    TEXT,
  issued_at    INTEGER NOT NULL,
  status       TEXT NOT NULL,
  completed_at INTEGER,
  FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_commands_node_time ON commands(node_id, issued_at DESC);

CREATE TABLE IF NOT EXISTS results (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  command_id  INTEGER NOT NULL,
  node_id     TEXT NOT NULL,
  cmd_id      TEXT NOT NULL,
  status      TEXT NOT NULL,
  payload     TEXT,
  received_at INTEGER NOT NULL,
  FOREIGN KEY (command_id) REFERENCES commands(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS output_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id     TEXT NOT NULL,
  cmd_id      TEXT,
  text        TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_output_node_time ON output_log(node_id, received_at DESC);

CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT,
  type    TEXT NOT NULL,
  detail  TEXT,
  ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(ts DESC);

CREATE TABLE IF NOT EXISTS macros (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  group_name TEXT NOT NULL DEFAULT '',
  steps      TEXT NOT NULL,
  dangerous  INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Commands staged for an offline node, delivered on its next hello. No FK to
-- nodes: you may queue for a node the hub has never seen (pre-staged before it
-- first powers on). status: pending|delivered|expired|cancelled.
CREATE TABLE IF NOT EXISTS queued_commands (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id    TEXT NOT NULL,
  type       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  issued_by  TEXT,
  issued_at  INTEGER NOT NULL,
  expires_at INTEGER,
  status     TEXT NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_queued_node ON queued_commands(node_id, id);

-- Runbooks: YAML jobs combining expect steps across a node group (phase 9).
CREATE TABLE IF NOT EXISTS runbooks (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  yaml       TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

-- Per-node raw serial bridge port assignments (phase 8). One TCP port maps to
-- one node's serial RX/TX so minicom/PuTTY/etc. can attach.
CREATE TABLE IF NOT EXISTS serial_bridge (
  node_id TEXT PRIMARY KEY,
  port    INTEGER NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  username      TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  created_at    INTEGER NOT NULL
);
"""


def _coerce_setting(key: str, value: str):
    """Turn a stored TEXT setting back into a typed Python value."""
    if key in config.BOOL_SETTINGS:
        return value == "true"
    if key in config.INT_SETTINGS:
        try:
            return int(value)
        except (TypeError, ValueError):
            return config.DEFAULT_SETTINGS.get(key)
    return value


def _encode_setting(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class Database:
    def __init__(self, path: Path):
        self._path = Path(path)
        self._db: aiosqlite.Connection = None
        # Buffered output rows: (node_id, cmd_id, text, received_at)
        self._output_buf: list = []

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._path))
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=3000")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Add columns to existing tables without altering the ones already there.
        Guarded ALTERs (checked against PRAGMA table_info) keep an already-populated
        deployment forward-safe."""
        async with self._db.execute("PRAGMA table_info(nodes)") as cur:
            cols = {r["name"] for r in await cur.fetchall()}
        if "last_ota" not in cols:
            # Which OTA bundle this node was last flashed with (provenance), since
            # the node's fw_version reflects code.py's FW_VERSION, not the bundle name.
            await self._db.execute("ALTER TABLE nodes ADD COLUMN last_ota TEXT")

    async def close(self) -> None:
        if self._db is not None:
            await self.flush_output()
            await self._db.close()
            self._db = None

    # -- nodes ----------------------------------------------------------------

    async def upsert_node(self, node_id: str, fw_version: str, ts: int) -> None:
        """First-seen if new; refresh last-seen and firmware. Label/group/notes
        are preserved for a node we've seen before."""
        await self._db.execute(
            """
            INSERT INTO nodes (id, fw_version, first_seen, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                fw_version = excluded.fw_version,
                last_seen  = excluded.last_seen
            """,
            (node_id, fw_version, ts, ts),
        )
        await self._db.commit()

    async def touch_node(self, node_id: str, ts: int) -> None:
        await self._db.execute("UPDATE nodes SET last_seen=? WHERE id=?", (ts, node_id))
        await self._db.commit()

    async def set_last_ota(self, node_id: str, label: str) -> None:
        """Record the OTA bundle a node was last flashed with (provenance)."""
        await self._db.execute("UPDATE nodes SET last_ota=? WHERE id=?", (label, node_id))
        await self._db.commit()

    async def get_node(self, node_id: str) -> dict:
        async with self._db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_nodes(self) -> list:
        async with self._db.execute("SELECT * FROM nodes ORDER BY id") as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def update_node_meta(self, node_id: str, label=None, group=None, notes=None) -> None:
        sets, args = [], []
        if label is not None:
            sets.append("label=?"); args.append(label)
        if group is not None:
            sets.append("group_name=?"); args.append(group)
        if notes is not None:
            sets.append("notes=?"); args.append(notes)
        if not sets:
            return
        args.append(node_id)
        await self._db.execute("UPDATE nodes SET %s WHERE id=?" % ", ".join(sets), args)
        await self._db.commit()

    async def delete_node(self, node_id: str) -> None:
        await self._db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        await self._db.commit()

    # -- commands / results ---------------------------------------------------

    async def insert_command(self, cmd_id, node_id, cmd_type, payload: dict, issued_by, issued_at) -> int:
        cur = await self._db.execute(
            """
            INSERT INTO commands (cmd_id, node_id, type, payload, issued_by, issued_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'sent')
            """,
            (cmd_id, node_id, cmd_type, json.dumps(payload), issued_by, issued_at),
        )
        await self._db.commit()
        return cur.lastrowid

    async def get_command_by_cmd_id(self, cmd_id: str) -> dict:
        async with self._db.execute("SELECT * FROM commands WHERE cmd_id=?", (cmd_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def complete_command(self, cmd_id: str, status: str, completed_at: int) -> None:
        db_status = "done" if status == "ok" else status  # ok->done, failed/timeout as-is
        await self._db.execute(
            "UPDATE commands SET status=?, completed_at=? WHERE cmd_id=?",
            (db_status, completed_at, cmd_id),
        )
        await self._db.commit()

    async def insert_result(self, command_id, node_id, cmd_id, status, payload, received_at) -> None:
        await self._db.execute(
            """
            INSERT INTO results (command_id, node_id, cmd_id, status, payload, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (command_id, node_id, cmd_id, status, payload, received_at),
        )
        await self._db.commit()

    async def list_commands(self, node_id: str, limit=50, before=None, type_=None, status=None) -> list:
        # Qualify every filter column with the commands alias `c`: this query
        # joins results `r`, and node_id/id/status exist in BOTH tables, so an
        # unqualified name is "ambiguous column name" to SQLite.
        where = ["c.node_id=?"]
        args = [node_id]
        if before is not None:
            where.append("c.id < ?"); args.append(before)
        if type_:
            where.append("c.type=?"); args.append(type_)
        if status:
            where.append("c.status=?"); args.append(status)
        args.append(limit)
        sql = (
            "SELECT c.*, r.payload AS result_payload FROM commands c "
            "LEFT JOIN results r ON r.cmd_id = c.cmd_id "
            "WHERE " + " AND ".join(where) + " ORDER BY c.id DESC LIMIT ?"
        )
        async with self._db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- output (batched) -----------------------------------------------------

    def append_output(self, node_id: str, text: str, received_at: int, cmd_id=None) -> None:
        """Queue an output chunk. Flushed in batches by flush_output()."""
        self._output_buf.append((node_id, cmd_id, text, received_at))

    async def flush_output(self) -> int:
        if not self._output_buf or self._db is None:
            return 0
        rows = self._output_buf
        self._output_buf = []
        await self._db.executemany(
            "INSERT INTO output_log (node_id, cmd_id, text, received_at) VALUES (?, ?, ?, ?)",
            rows,
        )
        await self._db.commit()
        return len(rows)

    async def list_output(self, node_id: str, since=None, before=None, limit=500) -> list:
        where = ["node_id=?"]
        args = [node_id]
        if since is not None:
            where.append("received_at >= ?"); args.append(since)
        if before is not None:
            where.append("id < ?"); args.append(before)
        args.append(limit)
        sql = (
            "SELECT id, cmd_id, text, received_at FROM output_log WHERE "
            + " AND ".join(where) + " ORDER BY id DESC LIMIT ?"
        )
        async with self._db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        # Return chronological (oldest first) for console backfill.
        return [dict(r) for r in reversed(rows)]

    async def iter_output_text(self, node_id: str):
        """Yield the full output log for a node as text lines, for download."""
        async with self._db.execute(
            "SELECT text FROM output_log WHERE node_id=? ORDER BY id ASC", (node_id,)
        ) as cur:
            async for row in cur:
                yield row["text"]

    def _output_window_where(self, node_id, since, before):
        where = ["node_id=?"]
        args = [node_id]
        if since is not None:
            where.append("received_at >= ?"); args.append(since)
        if before is not None:
            where.append("received_at < ?"); args.append(before)
        return " AND ".join(where), args

    async def output_window_start(self, node_id: str, since=None, before=None):
        """The received_at (ms) of the first output row in the window, or None.
        Used to anchor asciicast time offsets and the recording's timestamp."""
        clause, args = self._output_window_where(node_id, since, before)
        async with self._db.execute(
            "SELECT MIN(received_at) AS t FROM output_log WHERE " + clause, args
        ) as cur:
            row = await cur.fetchone()
        return row["t"] if row and row["t"] is not None else None

    async def iter_output_window(self, node_id: str, since=None, before=None):
        """Yield (received_at_ms, text) over a time window, oldest first. This is
        the source for the asciicast session export."""
        clause, args = self._output_window_where(node_id, since, before)
        async with self._db.execute(
            "SELECT received_at, text FROM output_log WHERE " + clause + " ORDER BY id ASC", args
        ) as cur:
            async for row in cur:
                yield row["received_at"], row["text"]

    async def count_output(self) -> int:
        async with self._db.execute("SELECT COUNT(*) AS n FROM output_log") as cur:
            row = await cur.fetchone()
        return row["n"]

    async def search_output(self, query: str, node_id=None, limit=200) -> list:
        """Find console output containing a substring, newest first. A simple
        LIKE scan (indexed on node_id/time); good enough to answer "where did that
        error scroll past" without an FTS table."""
        where = ["text LIKE ?"]
        args = ["%" + query + "%"]
        if node_id:
            where.append("node_id=?"); args.append(node_id)
        args.append(limit)
        sql = ("SELECT id, node_id, cmd_id, text, received_at FROM output_log WHERE "
               + " AND ".join(where) + " ORDER BY id DESC LIMIT ?")
        async with self._db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def backup_to(self, dest_path) -> None:
        """Write a consistent snapshot of the DB via VACUUM INTO. Safe to run on a
        live WAL database; produces a single compacted file."""
        await self._db.execute("VACUUM INTO ?", (str(dest_path),))
        await self._db.commit()

    # -- events ---------------------------------------------------------------

    async def insert_event(self, type_: str, node_id, detail: str, ts: int) -> None:
        await self._db.execute(
            "INSERT INTO events (node_id, type, detail, ts) VALUES (?, ?, ?, ?)",
            (node_id, type_, detail, ts),
        )
        await self._db.commit()

    async def list_events(self, node_id=None, type_=None, since=None, before=None, limit=100) -> list:
        where, args = [], []
        if node_id:
            where.append("node_id=?"); args.append(node_id)
        if type_:
            where.append("type=?"); args.append(type_)
        if since is not None:
            where.append("ts >= ?"); args.append(since)
        if before is not None:
            where.append("id < ?"); args.append(before)
        args.append(limit)
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        async with self._db.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- macros ---------------------------------------------------------------

    async def list_macros(self) -> list:
        async with self._db.execute("SELECT * FROM macros ORDER BY name") as cur:
            rows = await cur.fetchall()
        return [self._macro_row(r) for r in rows]

    async def get_macro(self, macro_id: int) -> dict:
        async with self._db.execute("SELECT * FROM macros WHERE id=?", (macro_id,)) as cur:
            row = await cur.fetchone()
        return self._macro_row(row) if row else None

    async def create_macro(self, name, steps: list, group="", dangerous=False) -> int:
        cur = await self._db.execute(
            "INSERT INTO macros (name, group_name, steps, dangerous, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, group, json.dumps(steps), 1 if dangerous else 0, now_ms()),
        )
        await self._db.commit()
        return cur.lastrowid

    async def update_macro(self, macro_id, name=None, steps=None, group=None, dangerous=None) -> None:
        sets, args = [], []
        if name is not None:
            sets.append("name=?"); args.append(name)
        if steps is not None:
            sets.append("steps=?"); args.append(json.dumps(steps))
        if group is not None:
            sets.append("group_name=?"); args.append(group)
        if dangerous is not None:
            sets.append("dangerous=?"); args.append(1 if dangerous else 0)
        if not sets:
            return
        args.append(macro_id)
        await self._db.execute("UPDATE macros SET %s WHERE id=?" % ", ".join(sets), args)
        await self._db.commit()

    async def delete_macro(self, macro_id: int) -> None:
        await self._db.execute("DELETE FROM macros WHERE id=?", (macro_id,))
        await self._db.commit()

    @staticmethod
    def _macro_row(row) -> dict:
        d = dict(row)
        d["steps"] = json.loads(d["steps"])
        d["dangerous"] = bool(d["dangerous"])
        d["group"] = d.pop("group_name", "")
        return d

    # -- queued commands (offline queue) --------------------------------------

    async def enqueue_command(self, node_id, type_, payload: dict, issued_by, issued_at, expires_at) -> int:
        cur = await self._db.execute(
            """
            INSERT INTO queued_commands (node_id, type, payload, issued_by, issued_at, expires_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (node_id, type_, json.dumps(payload), issued_by, issued_at, expires_at),
        )
        await self._db.commit()
        return cur.lastrowid

    async def list_queued(self, node_id: str, status="pending") -> list:
        async with self._db.execute(
            "SELECT * FROM queued_commands WHERE node_id=? AND status=? ORDER BY id",
            (node_id, status),
        ) as cur:
            rows = await cur.fetchall()
        return [self._queued_row(r) for r in rows]

    async def take_pending_queued(self, node_id: str, now: int) -> list:
        """Return non-expired pending rows for a node in issue order, expiring
        stale ones in place. The caller dispatches these, then marks each
        delivered. Expiry is applied here so a late drain never delivers a
        command past its TTL."""
        async with self._db.execute(
            "SELECT * FROM queued_commands WHERE node_id=? AND status='pending' ORDER BY id",
            (node_id,),
        ) as cur:
            rows = await cur.fetchall()
        deliver, expired = [], []
        for r in rows:
            if r["expires_at"] is not None and r["expires_at"] < now:
                expired.append(r["id"])
            else:
                deliver.append(self._queued_row(r))
        if expired:
            await self._db.executemany(
                "UPDATE queued_commands SET status='expired' WHERE id=?",
                [(i,) for i in expired],
            )
            await self._db.commit()
        return deliver

    async def mark_queued(self, qid: int, status: str) -> None:
        await self._db.execute("UPDATE queued_commands SET status=? WHERE id=?", (status, qid))
        await self._db.commit()

    async def cancel_queued(self, qid: int) -> int:
        cur = await self._db.execute(
            "UPDATE queued_commands SET status='cancelled' WHERE id=? AND status='pending'", (qid,)
        )
        await self._db.commit()
        return cur.rowcount

    @staticmethod
    def _queued_row(row) -> dict:
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d

    # -- runbooks -------------------------------------------------------------

    async def list_runbooks(self) -> list:
        async with self._db.execute("SELECT * FROM runbooks ORDER BY name") as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_runbook(self, rid: int) -> dict:
        async with self._db.execute("SELECT * FROM runbooks WHERE id=?", (rid,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def create_runbook(self, name: str, yaml_text: str) -> int:
        cur = await self._db.execute(
            "INSERT INTO runbooks (name, yaml, created_at) VALUES (?, ?, ?)",
            (name, yaml_text, now_ms()),
        )
        await self._db.commit()
        return cur.lastrowid

    async def update_runbook(self, rid: int, name=None, yaml_text=None) -> None:
        sets, args = [], []
        if name is not None:
            sets.append("name=?"); args.append(name)
        if yaml_text is not None:
            sets.append("yaml=?"); args.append(yaml_text)
        if not sets:
            return
        args.append(rid)
        await self._db.execute("UPDATE runbooks SET %s WHERE id=?" % ", ".join(sets), args)
        await self._db.commit()

    async def delete_runbook(self, rid: int) -> None:
        await self._db.execute("DELETE FROM runbooks WHERE id=?", (rid,))
        await self._db.commit()

    # -- serial bridge port map -----------------------------------------------

    async def assign_bridge_port(self, node_id: str, port: int) -> None:
        await self._db.execute(
            "INSERT INTO serial_bridge (node_id, port) VALUES (?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET port=excluded.port",
            (node_id, port),
        )
        await self._db.commit()

    async def remove_bridge_port(self, node_id: str) -> None:
        await self._db.execute("DELETE FROM serial_bridge WHERE node_id=?", (node_id,))
        await self._db.commit()

    async def list_bridge_ports(self) -> list:
        async with self._db.execute("SELECT node_id, port FROM serial_bridge ORDER BY port") as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # -- settings -------------------------------------------------------------

    async def get_settings(self) -> dict:
        """All settings, defaults overlaid with stored values, typed."""
        out = dict(config.DEFAULT_SETTINGS)
        async with self._db.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
        for r in rows:
            if r["key"] in config.DEFAULT_SETTINGS:
                out[r["key"]] = _coerce_setting(r["key"], r["value"])
        return out

    async def get_setting_raw(self, key: str) -> str:
        async with self._db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_settings(self, values: dict) -> None:
        for key, value in values.items():
            await self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, _encode_setting(value)),
            )
        await self._db.commit()

    # -- retention ------------------------------------------------------------

    async def prune(self, output_retention_days: int, event_retention_days: int) -> tuple:
        now = now_ms()
        out_cut = now - output_retention_days * 86_400_000
        ev_cut = now - event_retention_days * 86_400_000
        c1 = await self._db.execute("DELETE FROM output_log WHERE received_at < ?", (out_cut,))
        c2 = await self._db.execute("DELETE FROM events WHERE ts < ?", (ev_cut,))
        await self._db.commit()
        return (c1.rowcount, c2.rowcount)

    async def db_size_bytes(self) -> int:
        try:
            return self._path.stat().st_size
        except OSError:
            return 0
