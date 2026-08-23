"""Thread-safe snapshot of the balancer's live state, for the web dashboard.

The balancer loop publishes a fresh snapshot every cycle; the Flask app
(running in a background thread in the same process) only ever reads it -
no separate Modbus/Shelly polling happens on the web side.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ChargerStatus:
    name: str
    enabled: bool
    actual_l1_a: float
    actual_l2_a: float
    actual_l3_a: float
    installation_max_a: float


@dataclass
class BalancerSnapshot:
    updated_at: float
    have_fresh_data: bool
    main_fuse_l1_a: float
    main_fuse_l2_a: float
    main_fuse_l3_a: float
    ev_total_l1_a: float
    ev_total_l2_a: float
    ev_total_l3_a: float
    allocated_a: float
    chargers: list[ChargerStatus] = field(default_factory=list)


class BalancerState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: Optional[BalancerSnapshot] = None

    def update(self, **kwargs) -> None:
        snapshot = BalancerSnapshot(updated_at=time.time(), **kwargs)
        with self._lock:
            self._snapshot = snapshot

    def get(self) -> Optional[BalancerSnapshot]:
        with self._lock:
            return self._snapshot
