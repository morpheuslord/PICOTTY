"""Host-runnable tests for the node's hub-failover selector.

`hubselect.py` is pure logic (no hardware imports), so it runs on plain CPython.

Run:  python firmware/tests/test_hubselect.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "circuitpython"))

from hubselect import HubSelector  # noqa: E402


class FakeCfg:
    """Stand-in for NodeConfig with just the fields HubSelector reads/writes."""

    def __init__(self, hubs, failback="sticky", tries=2):
        self.hubs = hubs
        self.hub_failback = failback
        self.hub_failover_tries = tries
        self.hub_host = hubs[0]["host"]
        self.hub_port = hubs[0]["port"]


PRIMARY = {"label": "primary", "host": "10.0.0.1", "port": 9000}
BACKUP = {"label": "backup", "host": "10.0.0.2", "port": 9000}


def _sel(failback="sticky", tries=2, pref_path=None):
    cfg = FakeCfg([dict(PRIMARY), dict(BACKUP)], failback=failback, tries=tries)
    if pref_path is None:
        # a path that does not exist -> no persisted override loaded
        pref_path = os.path.join(tempfile.gettempdir(), "picotty_test_nopenofile.json")
        try:
            os.remove(pref_path)
        except OSError:
            pass
    return cfg, HubSelector(cfg, pref_path=pref_path)


# -- single hub: exactly today's behavior -------------------------------------

def test_single_hub_never_rotates():
    cfg = FakeCfg([dict(PRIMARY)])
    s = HubSelector(cfg, pref_path="/nonexistent/x.json")
    assert not s.has_backup
    for _ in range(10):
        assert s.next_target() == "primary"
        s.on_failed()          # even repeated failures never move (only one hub)
    assert s.next_target() == "primary"
    assert cfg.hub_host == "10.0.0.1"


# -- sticky failover ----------------------------------------------------------

def test_sticky_fails_over_after_tries_then_stays():
    cfg, s = _sel(failback="sticky", tries=2)
    # boot: dial primary, primary is down
    assert s.next_target() == "primary"
    s.on_failed()                          # fail 1 (< tries) -> still primary
    assert s.next_target() == "primary"
    s.on_failed()                          # fail 2 (== tries) -> rotate to backup
    assert s.next_target() == "backup"
    assert cfg.hub_host == "10.0.0.2"
    s.on_connected()                       # backup accepts us
    # primary recovers, but sticky keeps us on backup across benign drops
    for _ in range(5):
        assert s.next_target() == "backup"
    assert cfg.hub_host == "10.0.0.2"


def test_sticky_session_drop_retries_same_hub():
    cfg, s = _sel(failback="sticky", tries=2)
    assert s.next_target() == "primary"
    s.on_connected()
    # a session drop (reached_hub was True) does NOT call on_failed -> same hub
    assert s.next_target() == "primary"
    s.on_connected()
    assert s.next_target() == "primary"


def test_sticky_round_trips_backup_to_primary():
    cfg, s = _sel(failback="sticky", tries=1)
    assert s.next_target() == "primary"
    s.on_failed()                          # tries=1 -> rotate immediately
    assert s.next_target() == "backup"
    s.on_failed()                          # backup also down -> rotate back
    assert s.next_target() == "primary"


# -- preemptive failback ------------------------------------------------------

def test_preemptive_returns_to_primary_on_reconnect():
    cfg, s = _sel(failback="preemptive", tries=2)
    assert s.next_target() == "primary"
    s.on_failed(); s.on_failed()           # primary down -> backup
    assert s.next_target() == "backup"
    s.on_connected()                       # on backup, but preemptive resets cursor
    # next reconnect (e.g. after a session drop) tries primary first again
    assert s.next_target() == "primary"


# -- runtime directives -------------------------------------------------------

def test_switch_moves_now_then_reverts_preference():
    cfg, s = _sel(failback="sticky", tries=2)
    assert s.next_target() == "primary"
    s.on_connected()
    assert s.apply_directive("switch", "backup") is True
    assert s.next_target() == "backup"     # pending switch honored immediately
    s.on_connected()
    # switch is one-shot: sticky now holds backup, but a later failover still has
    # primary at the front of preference order
    s.on_failed(); s.on_failed()
    assert s.next_target() == "primary"


def test_prefer_promotes_backup_to_primary_and_moves():
    cfg, s = _sel(failback="sticky", tries=1)
    assert s.next_target() == "primary"
    s.on_connected()
    assert s.apply_directive("prefer", "backup") is True
    assert s.next_target() == "backup"     # moved now
    s.on_connected()
    # backup is now the preferred hub: if it dies, we fall over to primary...
    s.on_failed()
    assert s.next_target() == "primary"
    # ...and when preference-primary(=backup) is reachable again, it's front of order
    s.on_failed()
    assert s.next_target() == "backup"


def test_pin_locks_and_unpin_releases():
    cfg, s = _sel(failback="sticky", tries=1)
    s.next_target(); s.on_connected()
    assert s.apply_directive("pin", "backup") is True
    assert s.next_target() == "backup"
    s.on_connected()
    # pinned: failover is ignored entirely
    for _ in range(5):
        s.on_failed()
        assert s.next_target() == "backup"
    assert s.apply_directive("unpin", None) is True
    # released: normal failover resumes
    s.on_failed()
    assert s.next_target() in ("primary", "backup")


def test_directive_rejects_unknown_target_or_action():
    cfg, s = _sel()
    assert s.apply_directive("switch", "nope") is False
    assert s.apply_directive("bogus", "backup") is False
    assert s.apply_directive("prefer", "primary") is True   # valid no-op-ish


def test_failed_switch_falls_back_to_previous():
    cfg, s = _sel(failback="sticky", tries=2)
    s.next_target(); s.on_connected()      # on primary
    s.apply_directive("switch", "backup")
    assert s.next_target() == "backup"
    s.on_failed()                          # backup unreachable -> abandon switch
    assert s.next_target() == "primary"    # back to where we were


# -- persistence --------------------------------------------------------------

def test_prefer_persists_across_reload():
    path = os.path.join(tempfile.gettempdir(), "picotty_test_pref.json")
    try:
        os.remove(path)
    except OSError:
        pass
    cfg = FakeCfg([dict(PRIMARY), dict(BACKUP)])
    s = HubSelector(cfg, pref_path=path)
    s.apply_directive("prefer", "backup")   # persisted
    # simulate a soft reload: a fresh selector loads the override
    cfg2 = FakeCfg([dict(PRIMARY), dict(BACKUP)])
    s2 = HubSelector(cfg2, pref_path=path)
    assert s2.next_target() == "backup"     # backup is preferred after reload
    os.remove(path)


def test_note_hub_id():
    cfg, s = _sel()
    s.note_hub_id("hub-main")
    assert s._connected_id == "hub-main"


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        passed += 1
        print("ok  %s" % fn.__name__)
    print("\n%d/%d passed" % (passed, len(fns)))


if __name__ == "__main__":
    _run_all()
