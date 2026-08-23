"""Polls a Shelly Pro 3EM's local HTTP RPC API for live per-phase current.

MQTT status updates from the device are push-based on its own internal
schedule (a mix of periodic heartbeat and change-triggered publishes) and
aren't user-configurable - observed gaps were on the order of a minute,
too slow to react to a load spike before the main fuse trips. The local
RPC endpoint ("/rpc/EM.GetStatus") returns a live reading synchronously
in ~25ms, so polling it directly gives full control over the read
interval instead.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PhaseCurrents:
    l1_a: float
    l2_a: float
    l3_a: float


class ShellyPhaseCurrentReader:
    def __init__(self, host: str, poll_interval_seconds: float = 2.0, em_id: int = 0, timeout_seconds: float = 3.0):
        self._url = f"http://{host}/rpc/EM.GetStatus?id={em_id}"
        self._poll_interval = poll_interval_seconds
        self._timeout = timeout_seconds
        self._lock = threading.Lock()
        self._latest: Optional[PhaseCurrents] = None
        self._latest_at: Optional[float] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._timeout + 1)

    def _run(self) -> None:
        while not self._stop.is_set():
            loop_start = time.monotonic()
            try:
                with urllib.request.urlopen(self._url, timeout=self._timeout) as resp:
                    payload = json.loads(resp.read())
                currents = PhaseCurrents(
                    l1_a=float(payload["a_current"]),
                    l2_a=float(payload["b_current"]),
                    l3_a=float(payload["c_current"]),
                )
                with self._lock:
                    self._latest = currents
                    self._latest_at = time.monotonic()
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                logger.warning("Failed to poll Shelly at %s: %s", self._url, exc)

            elapsed = time.monotonic() - loop_start
            self._stop.wait(max(0.0, self._poll_interval - elapsed))

    def latest(self, max_age_seconds: Optional[float] = None) -> Optional[PhaseCurrents]:
        """Returns the latest reading, or None if there isn't one yet or it's
        older than max_age_seconds - callers use that to detect a dead poller
        rather than silently acting on a stale value forever."""
        with self._lock:
            if self._latest is None:
                return None
            if max_age_seconds is not None and time.monotonic() - self._latest_at > max_age_seconds:
                return None
            return self._latest
