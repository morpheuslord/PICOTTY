"""The in-memory registry: the live source of truth for who is online.

Deliberately not persisted. On a hub restart it starts empty and refills as
nodes reconnect. It holds each node's socket writer, which is what lets a REST
call push a command straight down the node's own connection.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Inflight:
    """A command dispatched to a node, awaiting its result."""

    cmd_id: str
    type: str
    sent_at: int
    db_command_id: int


@dataclass
class NodeState:
    node_id: str
    writer: asyncio.StreamWriter
    addr: tuple
    connected_at: int
    last_seen: int
    status: str = "online"  # "online" | "busy" | "offline"
    fw_version: str = ""
    capabilities: list = field(default_factory=list)
    layout: str = "us"  # keyboard layout the node reported in hello (read-only)
    rtt_ms: Optional[int] = None
    inflight: dict = field(default_factory=dict)  # cmd_id -> Inflight
    # Live prompt-state classification (registry-only, like rtt_ms). Set by the
    # output classifier; None until enough output has been seen.
    prompt_state: Optional[str] = None
    tail: str = ""  # rolling tail of recent output the classifier reads
    # Target-machine liveness, distinct from node liveness. host_up comes from the
    # node's heartbeat (USB host enumerated = machine running); None if the
    # firmware doesn't report it. last_output_at corroborates: fresh serial output
    # means the machine is definitely alive.
    host_up: Optional[bool] = None
    last_output_at: int = 0

    # Network telemetry (registry-only, like rtt_ms). A rolling window of ping
    # RTT samples (None = a probe that got no pong) feeds the derived quality
    # metrics the dashboard shows so a flaky/jittery link is visible before it
    # drops. Populated by the hub's node pinger; see utils.net_stats.
    rtt_window: list = field(default_factory=list)
    rtt_avg_ms: Optional[int] = None
    rtt_min_ms: Optional[int] = None
    rtt_max_ms: Optional[int] = None
    jitter_ms: Optional[int] = None
    loss_pct: int = 0
    # Activity: firmware uptime the node reports in its heartbeat (ms since its
    # boot), and the hub time we sampled it. A drop in this value is an
    # unannounced node reboot. reconnects counts how many times this id has
    # re-registered during THIS hub run — a flapping link even while "online".
    node_uptime_ms: Optional[int] = None
    node_uptime_at: int = 0
    reconnects: int = 0

    # Not part of the persisted model; live socket bookkeeping.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_pongs: dict = field(default_factory=dict)  # nonce -> asyncio.Future
    pending_results: dict = field(default_factory=dict)  # 'r_' cmd_id -> Future (OTA request/reply)

    @property
    def ip(self) -> str:
        try:
            return self.addr[0]
        except (TypeError, IndexError):
            return ""


class Registry:
    """A dict of node_id -> NodeState with small helpers. Single-threaded under
    the asyncio loop, so no locking is needed for the dict itself."""

    def __init__(self):
        self._nodes: dict[str, NodeState] = {}
        # Lifetime connect count per node id, surviving the NodeState churn of
        # reconnects (each reconnect builds a fresh NodeState). reconnects =
        # connects - 1, so the first connection reports 0.
        self._connects: dict[str, int] = {}

    def add(self, state: NodeState) -> None:
        self._nodes[state.node_id] = state

    def record_connect(self, node_id: str) -> int:
        """Count a (re)connection for this id and return its reconnect count
        (0 on the first connect this hub run, 1 on the first reconnect, ...)."""
        n = self._connects.get(node_id, 0) + 1
        self._connects[node_id] = n
        return n - 1

    def get(self, node_id: str) -> Optional[NodeState]:
        return self._nodes.get(node_id)

    def remove(self, node_id: str) -> Optional[NodeState]:
        return self._nodes.pop(node_id, None)

    def all(self) -> list:
        return list(self._nodes.values())

    def is_online(self, node_id: str) -> bool:
        st = self._nodes.get(node_id)
        return st is not None and st.status != "offline"

    def online_count(self) -> int:
        return sum(1 for st in self._nodes.values() if st.status == "online")

    def count(self) -> int:
        return len(self._nodes)
