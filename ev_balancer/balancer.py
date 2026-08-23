"""Per-phase load balancing logic.

DEFA only accepts a single max-current value that is applied identically
to all three phases, so the limiting factor is whichever phase currently
has the least headroom under the main fuse rating.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .shelly import PhaseCurrents


@dataclass
class BalancerConfig:
    fuse_limit_a: float
    min_charge_current_a: float
    safety_margin_a: float
    shelly_measures_whole_house: bool
    # Output is rounded down to a multiple of this many amps.
    step_a: float = 1.0
    # An increase only commits once the higher value has been the
    # continuously-computed target for this long; any change to the
    # proposed value (up, down, or a drop back to the old value) resets
    # the timer. Decreases always commit immediately - never delay a
    # safety-motivated cut in current.
    increase_hold_seconds: float = 30.0


def compute_max_charge_current_a(
    shelly: PhaseCurrents,
    ev_current_l1_a: float,
    ev_current_l2_a: float,
    ev_current_l3_a: float,
    config: BalancerConfig,
) -> float:
    """Computes the shared per-phase current ceiling from house-fuse headroom.

    ev_current_l*_a is the *combined* actual draw across all chargers - with
    multiple stations, each one may be offered up to this same ceiling, so
    it reflects headroom against total house load, not any one station's
    own limit (that's clamped separately per-station using its own
    installation max).
    """
    ev_currents_a = (ev_current_l1_a, ev_current_l2_a, ev_current_l3_a)
    shelly_currents_a = (shelly.l1_a, shelly.l2_a, shelly.l3_a)

    headrooms = []
    for total_a, ev_a in zip(shelly_currents_a, ev_currents_a):
        house_only_a = max(total_a - ev_a, 0.0) if config.shelly_measures_whole_house else total_a
        headrooms.append(config.fuse_limit_a - config.safety_margin_a - house_only_a)

    target_a = max(min(*headrooms), 0.0)

    if target_a < config.min_charge_current_a:
        return 0.0
    return target_a


class ChargeCurrentController:
    """Turns a raw per-cycle target into a stepped, debounced value to write.

    Rounds down to `step_a` and only commits an increase once it has been
    the sustained target for `increase_hold_seconds`, so the setpoint
    doesn't chase every small, momentary wobble in house load. Decreases
    are never delayed.
    """

    def __init__(self, config: BalancerConfig):
        self._config = config
        self.committed_a: Optional[float] = None
        self._pending_increase_a: Optional[float] = None
        self._pending_since: Optional[float] = None

    def update(self, raw_target_a: float, now: float) -> Optional[float]:
        """Returns the new value to write this cycle, or None to leave it be."""
        step = self._config.step_a
        stepped_a = math.floor(raw_target_a / step) * step

        if self.committed_a is None:
            self.committed_a = stepped_a
            return stepped_a

        if stepped_a <= self.committed_a:
            self._pending_increase_a = None
            self._pending_since = None
            if stepped_a == self.committed_a:
                return None
            self.committed_a = stepped_a
            return stepped_a

        # stepped_a > committed_a: a proposed increase, subject to debounce.
        if stepped_a != self._pending_increase_a:
            self._pending_increase_a = stepped_a
            self._pending_since = now
            return None

        if now - self._pending_since >= self._config.increase_hold_seconds:
            self.committed_a = stepped_a
            self._pending_increase_a = None
            self._pending_since = None
            return stepped_a

        return None
