# hubselect.py — choose which hub a node dials, and fail over between them.
#
# Pure logic, no hardware imports, so it runs on plain CPython too (see
# firmware/tests/test_hubselect.py). The node holds an ordered list of hubs
# (primary first) and points cfg.hub_host/hub_port at the chosen one before each
# connect, so the transport (netlink) never changes.
#
# Failback policy:
#   * "sticky" (default): once a hub accepts us we keep dialing it across benign
#     drops; only after `failover_tries` connect failures to reach it do we
#     advance to the next candidate. A recovered primary does NOT pull us back —
#     no interruption to a live session.
#   * "preemptive": every reconnect restarts from the most-preferred hub, so a
#     recovered primary is used again on the next reconnect.
#
# A hub (dashboard today, an app tomorrow) can steer the node at runtime via a
# `hub_directive` frame handled in code.py:
#   * switch  — move to the target hub now (one-shot; preference unchanged)
#   * prefer  — make the target the preferred hub AND move now (persisted)
#   * pin     — lock to the target (ignore failover) and move now (persisted)
#   * unpin   — release a pin
# A directive's target must be one of the node's configured hubs, so a hub can
# never redirect a board to an arbitrary address.

# Where a runtime override is persisted so a promotion survives a soft reload.
# Best-effort: on a read-only filesystem (the default production posture, no OTA)
# the override simply holds for the life of the process.
HUB_PREF_PATH = "/hub_pref.json"


class HubSelector:
    def __init__(self, cfg, pref_path=HUB_PREF_PATH):
        self._cfg = cfg
        self._hubs = cfg.hubs                                  # [{label,host,port}]
        self._pref_path = pref_path
        self._by_label = {}
        for i in range(len(self._hubs)):
            self._by_label[self._hubs[i]["label"]] = i
        self._order = list(range(len(self._hubs)))            # preference (indices)
        self._cursor = 0                                       # position in _order
        self._sticky = None                                   # idx currently holding us
        self._pin = None                                      # locked idx or None
        self._pending = None                                  # idx to switch to now
        self._active = self._order[0]                         # idx last handed out
        self._fails = 0                                       # consecutive connect fails
        self._connected_id = None                             # hub_id from welcome
        self._tries = cfg.hub_failover_tries if cfg.hub_failover_tries > 0 else 1
        self._preemptive = cfg.hub_failback == "preemptive"
        self._load()

    # -- selection ------------------------------------------------------------

    def _current_idx(self):
        if self._pending is not None:
            return self._pending
        if self._pin is not None:
            return self._pin
        if not self._preemptive and self._sticky is not None:
            return self._sticky
        return self._order[self._cursor % len(self._order)]

    def next_target(self):
        """Point cfg at the hub to dial next; returns its label."""
        idx = self._current_idx()
        self._active = idx
        h = self._hubs[idx]
        self._cfg.hub_host = h["host"]
        self._cfg.hub_port = h["port"]
        return h["label"]

    @property
    def active_label(self):
        return self._hubs[self._active]["label"]

    @property
    def has_backup(self):
        return len(self._hubs) > 1

    def on_connected(self):
        """A hello just succeeded on the active hub."""
        idx = self._active
        self._fails = 0
        if self._pending == idx:
            self._pending = None
        if self._preemptive:
            self._cursor = 0            # next reconnect starts from the top again
        else:
            self._sticky = idx          # stay here until it fails
            self._cursor = self._order.index(idx)

    def on_failed(self):
        """A connect attempt to the active hub never reached it."""
        self._fails += 1
        if self._pending is not None:
            self._pending = None        # a requested switch couldn't connect; drop it
            self._fails = 0
            return
        if self._fails >= self._tries:
            self._fails = 0
            self._sticky = None
            if len(self._order) > 1:
                self._cursor = (self._cursor + 1) % len(self._order)

    # -- runtime directives ---------------------------------------------------

    def note_hub_id(self, hub_id):
        self._connected_id = hub_id

    def apply_directive(self, action, target):
        """Return True if the directive was valid/applied. Moving actions
        (switch/prefer/pin) set a pending target; the caller drops the session so
        the loop dials it."""
        if action == "unpin":
            self._pin = None
            self._persist()
            return True
        idx = self._by_label.get(target)
        if idx is None or action not in ("switch", "prefer", "pin"):
            return False
        if action == "prefer":
            self._order.remove(idx)
            self._order.insert(0, idx)
            self._cursor = 0
        elif action == "pin":
            self._pin = idx
        self._pending = idx
        if action in ("prefer", "pin"):
            self._persist()
        return True

    # -- persistence ----------------------------------------------------------

    def _persist(self):
        try:
            import json
            primary = self._hubs[self._order[0]]["label"]
            pin = self._hubs[self._pin]["label"] if self._pin is not None else None
            with open(self._pref_path, "w") as f:
                json.dump({"primary": primary, "pin": pin}, f)
        except Exception:
            pass  # read-only FS: the override just holds for this process

    def _load(self):
        try:
            import json
            with open(self._pref_path) as f:
                data = json.load(f)
        except Exception:
            return
        primary = data.get("primary")
        if primary in self._by_label:
            idx = self._by_label[primary]
            self._order.remove(idx)
            self._order.insert(0, idx)
        pin = data.get("pin")
        if pin in self._by_label:
            self._pin = self._by_label[pin]
