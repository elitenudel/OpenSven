"""Runtime-mutable registry of DEFA charging stations.

Chargers can be added or removed at runtime via the web dashboard's
settings menu. This registry is the single source of truth for both the
balancer loop (which only ever reads a snapshot of it once per cycle) and
the web layer (which adds/removes under a lock and persists the change to
its own store file - kept separate from config.yaml so a runtime change
never touches that file's comments/formatting).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

import yaml

from .defa_modbus import DefaModbusClient

logger = logging.getLogger(__name__)


@dataclass
class ChargerStation:
    name: str
    host: str
    port: int
    unit_id: int
    client: Optional[DefaModbusClient]
    enabled: bool = False
    actual_a: tuple[float, float, float] = (0.0, 0.0, 0.0)
    installation_max_a: float = 0.0
    online: bool = False
    last_seen: Optional[float] = None


class DuplicateStationError(ValueError):
    pass


class StationNotFoundError(KeyError):
    pass


class StationManager:
    def __init__(
        self,
        store_path: str,
        *,
        shelly_only: bool,
        fallback_current_a: float,
        alive_timeout_seconds: float,
    ) -> None:
        self._store_path = store_path
        self._shelly_only = shelly_only
        self._fallback_current_a = fallback_current_a
        self._alive_timeout_seconds = alive_timeout_seconds
        self._lock = threading.Lock()
        self._stations: list[ChargerStation] = []

    def load_initial(self, seed_stations_cfg: list[dict]) -> None:
        """Loads the starting station list from the store file if it
        already exists (a prior run persisted it), otherwise seeds it once
        from config.yaml's defa.stations."""
        cfg = self._read_store()
        if cfg is None:
            cfg = seed_stations_cfg
        stations = []
        for i, sc in enumerate(cfg):
            name = sc.get("name") or f"charger{i + 1}"
            host, port, unit_id = sc["host"], sc.get("port", 502), sc.get("unit_id", 255)
            client = self._connect_client(name, host, port, unit_id)
            stations.append(ChargerStation(name=name, host=host, port=port, unit_id=unit_id, client=client))
        with self._lock:
            self._stations = stations
            self._save()

    def snapshot(self) -> list[ChargerStation]:
        with self._lock:
            return list(self._stations)

    def add(self, name: str, host: str, port: int, unit_id: int) -> ChargerStation:
        with self._lock:
            if any(st.name == name for st in self._stations):
                raise DuplicateStationError(f"Charger '{name}' already exists")
        # Connecting is slow I/O (socket timeout on an unreachable host) -
        # do it outside the lock so it doesn't stall the balancer loop's
        # own snapshot() call or other concurrent settings requests.
        client = self._connect_client(name, host, port, unit_id)
        with self._lock:
            if any(st.name == name for st in self._stations):
                if client is not None:
                    client.close()
                raise DuplicateStationError(f"Charger '{name}' already exists")
            station = ChargerStation(name=name, host=host, port=port, unit_id=unit_id, client=client)
            self._stations.append(station)
            priority = len(self._stations)
            self._save()
        logger.info("Added charger %s (%s:%s), priority %d", name, host, port, priority)
        return station

    def update(self, old_name: str, name: str, host: str, port: int, unit_id: int) -> ChargerStation:
        with self._lock:
            station = next((st for st in self._stations if st.name == old_name), None)
            if station is None:
                raise StationNotFoundError(old_name)
            if name != old_name and any(st.name == name for st in self._stations):
                raise DuplicateStationError(f"Charger '{name}' already exists")
            connection_changed = (host, port, unit_id) != (station.host, station.port, station.unit_id)

        # Reconnecting is slow I/O on an unreachable host - do it outside the
        # lock, same reasoning as add().
        new_client = self._connect_client(name, host, port, unit_id) if connection_changed else None

        with self._lock:
            old_client = station.client if connection_changed else None
            station.name = name
            station.host = host
            station.port = port
            station.unit_id = unit_id
            if connection_changed:
                station.client = new_client
                station.online = False
                station.last_seen = None
            self._save()

        if old_client is not None:
            try:
                old_client.set_ems_max_current_ma(0)
            except IOError as exc:
                logger.warning("Could not turn off %s's old connection before editing it: %s", old_name, exc)
            old_client.close()

        logger.info("Updated charger %s -> %s (%s:%s)", old_name, name, host, port)
        return station

    def remove(self, name: str) -> None:
        with self._lock:
            idx = next((i for i, st in enumerate(self._stations) if st.name == name), None)
            if idx is None:
                raise StationNotFoundError(name)
            station = self._stations.pop(idx)
            self._save()
        if station.client is not None:
            try:
                station.client.set_ems_max_current_ma(0)
            except IOError as exc:
                logger.warning("Could not turn off %s before removing it: %s", name, exc)
            station.client.close()
        logger.info("Removed charger %s (%s:%s)", name, station.host, station.port)

    def _connect_client(self, name: str, host: str, port: int, unit_id: int) -> Optional[DefaModbusClient]:
        if self._shelly_only:
            return None
        client = DefaModbusClient(host=host, port=port, unit_id=unit_id)
        try:
            client.connect()
            # Safe fallback if this process dies: station drops to this
            # current instead of continuing to charge unmanaged once
            # `alive` goes stale.
            client.set_timeout_max_charge_current_ma(int(self._fallback_current_a * 1000))
            client.set_alive_timeout_ms(int(self._alive_timeout_seconds * 1000))
        except IOError as exc:
            # Don't block on one unreachable station - keep the client
            # around so the balancer loop's own IOError handling retries
            # the connection on every subsequent poll.
            logger.error("Communication error setting up %s, will retry: %s", name, exc)
        return client

    def _read_store(self) -> Optional[list[dict]]:
        if not os.path.exists(self._store_path):
            return None
        with open(self._store_path) as f:
            data = yaml.safe_load(f) or {}
        return data.get("stations") or []

    def _save(self) -> None:
        data = {
            "stations": [
                {"name": st.name, "host": st.host, "port": st.port, "unit_id": st.unit_id}
                for st in self._stations
            ]
        }
        directory = os.path.dirname(self._store_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self._store_path}.tmp"
        with open(tmp_path, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.replace(tmp_path, self._store_path)
