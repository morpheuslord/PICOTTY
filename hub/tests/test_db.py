#!/usr/bin/env python3
"""Offline checks for the hub's SQLite layer — no server, no nodes, no network.

Run against a throwaway database:
    hub/.venv/bin/python hub/tests/test_db.py

These exercise list_commands() with a joined result row — the exact path that
raised `sqlite3.OperationalError: ambiguous column name: node_id`. That query
JOINs commands and results, and node_id / id / status exist in BOTH tables, so
every filter column must be qualified with the table alias. A "does the app
import" smoke test misses this entirely; you have to actually run the query with
a result row present, which is what these checks do.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile

# Make `import app.db` resolve no matter what directory this is run from.
HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HUB_DIR not in sys.path:
    sys.path.insert(0, HUB_DIR)

from app.db import Database  # noqa: E402


async def run_checks() -> bool:
    checks = []

    def ok(name, cond, note=""):
        checks.append((name, bool(cond)))
        print("  %s %-22s %s" % ("[PASS]" if cond else "[FAIL]", name, note))

    print("\n=== hub db checks (list_commands join) ===")

    tmp = tempfile.mkdtemp(prefix="hubtest-")
    db = Database(os.path.join(tmp, "test.db"))
    await db.connect()
    try:
        node = "Node-Test"
        # commands.node_id has a FK to nodes(id) and foreign_keys is ON, so the
        # node row must exist before any command references it.
        await db.upsert_node(node, "1.0.0", 1000)

        # cmd-1: has a result row (the JOIN case that used to throw).
        cid1 = await db.insert_command("cmd-1", node, "type", {"text": "hi"}, "tester", 1001)
        await db.insert_result(cid1, node, "cmd-1", "done", "typed ok", 1002)
        await db.complete_command("cmd-1", "ok", 1003)

        # cmd-2: no result yet — the LEFT JOIN must still return it.
        await db.insert_command("cmd-2", node, "read", {}, "tester", 1010)

        # cmd-3: different type + a failed status, for the filter checks.
        await db.insert_command("cmd-3", node, "send", {"data": "x"}, "tester", 1020)
        await db.complete_command("cmd-3", "failed", 1021)

        # 1) The bug repro: an unfiltered list must not raise and must carry the
        #    joined result payload through as result_payload.
        rows = await db.list_commands(node, limit=40)
        ok("list-no-filter", len(rows) == 3, "got %d rows" % len(rows))
        by_cmd = {r["cmd_id"]: r for r in rows}
        ok("join-result-payload", by_cmd["cmd-1"].get("result_payload") == "typed ok",
           "result_payload=%r" % by_cmd["cmd-1"].get("result_payload"))
        ok("left-join-null", by_cmd["cmd-2"].get("result_payload") is None,
           "no result -> NULL payload")

        # 2) Each filter path touches a column present in BOTH tables, so each is
        #    an independent chance to reintroduce the ambiguity.
        typed = await db.list_commands(node, type_="send")
        ok("filter-type", len(typed) == 1 and typed[0]["cmd_id"] == "cmd-3",
           "got %d" % len(typed))

        failed = await db.list_commands(node, status="failed")
        ok("filter-status", len(failed) == 1 and failed[0]["cmd_id"] == "cmd-3",
           "got %d" % len(failed))

        mid_id = by_cmd["cmd-2"]["id"]
        older = await db.list_commands(node, before=mid_id)
        ok("filter-before", len(older) == 1 and all(r["id"] < mid_id for r in older),
           "got %d before id=%s" % (len(older), mid_id))

        # 3) All filters at once: node_id + type + status qualified together.
        combo = await db.list_commands(node, type_="send", status="failed")
        ok("filter-combined", len(combo) == 1 and combo[0]["cmd_id"] == "cmd-3",
           "got %d" % len(combo))
    finally:
        await db.close()
        shutil.rmtree(tmp, ignore_errors=True)

    passed, total = sum(1 for _, c in checks if c), len(checks)
    print("=== %d/%d db checks passed ===\n" % (passed, total))
    return passed == total


def main():
    sys.exit(0 if asyncio.run(run_checks()) else 1)


if __name__ == "__main__":
    main()
