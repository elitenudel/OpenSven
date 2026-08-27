"""Charging history recorder for the dashboard's graph button.

Samples are stored as compact JSON lines, one file per hour (named by
hour-index so plain filename sort is chronological order), under
`history.directory`. Storage is capped at `history.max_size_mb` - whenever
a write pushes the directory over that size, whole files are deleted
oldest-first until it's back under the cap. Deleting entire hour-chunks
instead of trimming a single ever-growing file keeps this cheap enough for
a Raspberry Pi's SD card - no rewriting, no database, no VACUUM.

Amps are converted to watts before being stored (see `amps_to_watts`), so
only one number per charger and the main fuse needs to be kept per sample.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_CHUNK_SECONDS = 3600  # one file per hour


def amps_to_watts(l1_a: float, l2_a: float, l3_a: float, voltage: float) -> float:
    """Total real power across all three phases, treated as independent
    single-phase circuits (matches how the rest of this app already treats
    per-phase current, rather than assuming a balanced three-phase load)."""
    return voltage * (l1_a + l2_a + l3_a)


class HistoryRecorder:
    def __init__(self, directory: str, max_size_bytes: int) -> None:
        self._directory = Path(directory)
        self._max_size_bytes = max_size_bytes
        self._lock = threading.Lock()
        self._directory.mkdir(parents=True, exist_ok=True)

    def record(self, timestamp: float, main_fuse_w: float, chargers: dict[str, tuple[float, bool, bool, bool]]) -> None:
        """chargers maps charger name -> (watts, enabled, online, limited)."""
        record = {
            "t": round(timestamp),
            "fuse_w": round(main_fuse_w),
            "c": {
                name: [round(w), bool(enabled), bool(online), bool(limited)]
                for name, (w, enabled, online, limited) in chargers.items()
            },
        }
        chunk_path = self._chunk_path(timestamp)
        with self._lock:
            with open(chunk_path, "a") as f:
                f.write(json.dumps(record) + "\n")
            self._enforce_limit()

    def purge_charger(self, name: str) -> None:
        """Permanently deletes every recorded sample for `name` from every
        chunk file - called when a charger is *removed*, never when it's
        only edited (including a rename), since editing must never lose
        history. Rewrites each chunk in place (only the ones that actually
        mention `name`) rather than a full rebuild, since files can span
        many other chargers plus the main fuse reading, none of which this
        should touch."""
        with self._lock:
            for path, _ in self._chunk_files():
                self._purge_charger_from_file(path, name)

    def _purge_charger_from_file(self, path: Path, name: str) -> None:
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError as exc:
            logger.warning("Could not read history file %s while purging %s: %s", path, name, exc)
            return

        changed = False
        kept_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rec = json.loads(stripped)
            except ValueError:
                # Same torn-write possibility read_since() tolerates -
                # left untouched rather than dropped.
                kept_lines.append(line if line.endswith("\n") else line + "\n")
                continue
            if name in rec.get("c", {}):
                del rec["c"][name]
                changed = True
            kept_lines.append(json.dumps(rec) + "\n")

        if not changed:
            return

        tmp_path = path.with_name(path.name + ".tmp")
        try:
            with open(tmp_path, "w") as f:
                f.writelines(kept_lines)
            os.replace(tmp_path, path)
        except OSError as exc:
            logger.warning("Could not rewrite history file %s while purging %s: %s", path, name, exc)

    def read_since(self, since_timestamp: float) -> list[dict]:
        since_chunk = int(since_timestamp // _CHUNK_SECONDS)
        results: list[dict] = []
        with self._lock:
            chunk_files = self._chunk_files()
        for path, chunk_index in chunk_files:
            if chunk_index < since_chunk:
                continue
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            # Last line of the current chunk can be a torn
                            # write if the process was killed mid-append -
                            # skip it rather than losing the whole file.
                            continue
                        if rec.get("t", 0) >= since_timestamp:
                            results.append(rec)
            except OSError as exc:
                logger.warning("Could not read history file %s: %s", path, exc)
        results.sort(key=lambda r: r["t"])
        return results

    def _chunk_path(self, timestamp: float) -> Path:
        return self._directory / f"{int(timestamp // _CHUNK_SECONDS)}.jsonl"

    def _chunk_files(self) -> list[tuple[Path, int]]:
        chunks = []
        for entry in self._directory.glob("*.jsonl"):
            try:
                chunks.append((entry, int(entry.stem)))
            except ValueError:
                continue
        chunks.sort(key=lambda pair: pair[1])
        return chunks

    def _enforce_limit(self) -> None:
        chunk_files = self._chunk_files()
        sizes = [(path, os.path.getsize(path)) for path, _ in chunk_files]
        total = sum(size for _, size in sizes)
        i = 0
        while total > self._max_size_bytes and i < len(sizes):
            path, size = sizes[i]
            try:
                path.unlink()
                total -= size
            except OSError as exc:
                logger.warning("Could not delete old history file %s: %s", path, exc)
            i += 1
