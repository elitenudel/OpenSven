from __future__ import annotations

import argparse
import logging
import time
from typing import Optional

import yaml

from .balancer import BalancerConfig, ChargeCurrentController, compute_max_charge_current_a
from .defa_modbus import StationReading
from .history import HistoryRecorder, amps_to_watts
from .shelly import ShellyPhaseCurrentReader
from .state import BalancerState, ChargerStatus
from .stations import StationManager
from .web import start_in_background as start_web_in_background

logger = logging.getLogger(__name__)

_STATUS_CHANGE_THRESHOLD_A = 1.0
_COLOR_HOUSE = "\033[36m"  # cyan
_COLOR_EV = "\033[33m"  # yellow
_COLOR_ALLOCATED = "\033[32m"  # green
_COLOR_ON = "\033[32m"  # green
_COLOR_OFF = "\033[90m"  # grey
_COLOR_RESET = "\033[0m"


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run(config: dict, shelly_only: bool = False) -> None:
    shelly_cfg = config["shelly"]
    defa_cfg = config["defa"]
    balancer_cfg = BalancerConfig(**config["balancer"])

    shelly_poll_interval = shelly_cfg.get("poll_interval_seconds", 2)
    shelly = ShellyPhaseCurrentReader(
        host=shelly_cfg["host"],
        poll_interval_seconds=shelly_poll_interval,
    )
    shelly.start()

    # A reading older than this is treated as "no data" rather than acted
    # on - stale house-load numbers are worse than none, since a real spike
    # since the last good read would go completely unaccounted for.
    shelly_max_age_seconds = shelly_cfg.get("max_age_seconds", max(5.0, shelly_poll_interval * 5))

    # Seed list used only the first time this process ever runs - once
    # chargers_file exists (either from a prior run or from the web
    # dashboard's settings menu), it's the source of truth and this is
    # ignored, so config.yaml's comments/formatting never get rewritten.
    stations_cfg = defa_cfg.get("stations", [])

    fallback_current_a = defa_cfg.get("fallback_current_a", 0)
    alive_timeout_seconds = defa_cfg.get("alive_timeout_seconds", 100)
    # Below this actual current, a charger counts as idle - not using its
    # allocation - which makes room for the next-priority charger to start.
    idle_threshold_a = defa_cfg.get("idle_threshold_a", 1.0)
    # Minimum time between forced shutdowns when combined actual draw across
    # enabled chargers exceeds the allocated ceiling, so each shutdown gets
    # a chance to take effect before the next one is considered.
    shutdown_stagger_seconds = defa_cfg.get("shutdown_stagger_seconds", 5)

    station_manager = StationManager(
        store_path=defa_cfg.get("chargers_file", "state/chargers.yaml"),
        shelly_only=shelly_only,
        fallback_current_a=fallback_current_a,
        alive_timeout_seconds=alive_timeout_seconds,
    )
    station_manager.load_initial(stations_cfg)

    if shelly_only:
        logger.warning(
            "Running in --shelly-only mode: not connected to any DEFA station, EV current is assumed to "
            "be 0A, nothing will be written. This only shows what the balancer would do."
        )

    poll_interval = defa_cfg.get("poll_interval_seconds", 5)
    alive_interval = defa_cfg.get("alive_interval_seconds", 20)

    history_cfg = config.get("history", {})
    history_recorder: Optional[HistoryRecorder] = None
    history_sample_interval = history_cfg.get("sample_interval_seconds", 60)
    history_voltage = history_cfg.get("voltage", 230)
    if history_cfg.get("enabled", True):
        history_recorder = HistoryRecorder(
            directory=history_cfg.get("directory", "state/history"),
            max_size_bytes=int(history_cfg.get("max_size_mb", 1024) * 1024 * 1024),
        )

    web_cfg = config.get("web", {})
    state: Optional[BalancerState] = None
    if web_cfg.get("enabled", False):
        state = BalancerState()
        web_host = web_cfg.get("host", "0.0.0.0")
        web_port = web_cfg.get("port", 8080)
        settings_token = web_cfg.get("settings_token", "")
        start_web_in_background(state, station_manager, history_recorder, settings_token, web_host, web_port)
        logger.info("Web dashboard listening on http://%s:%s", web_host, web_port)
        if not settings_token:
            logger.info("Dashboard settings menu (add/remove chargers) is disabled - set web.settings_token to enable it")

    controller = ChargeCurrentController(balancer_cfg)
    last_alive_sent = 0.0
    last_status_values: tuple[float, ...] | None = None
    last_forced_shutdown_at = 0.0
    last_main_fuse_a = (0.0, 0.0, 0.0)
    last_history_sample_at = 0.0

    initial_stations = station_manager.snapshot()
    priority_order = ", ".join(f"{i + 1}={st.name}" for i, st in enumerate(initial_stations))
    logger.info(
        "Load balancer running (poll every %ss, alive every %ss). Charger priority: %s",
        poll_interval,
        alive_interval,
        priority_order or "(none yet - add one via the dashboard settings menu)",
    )

    while True:
        loop_start = time.monotonic()

        # Chargers can be added/removed at runtime via the dashboard, so
        # take a fresh snapshot every cycle rather than a list fixed at
        # startup.
        stations = station_manager.snapshot()

        currents = shelly.latest(max_age_seconds=shelly_max_age_seconds)
        have_fresh_data = currents is not None
        if not have_fresh_data:
            logger.warning(
                "No Shelly reading in the last %.0fs - withholding writes and the alive "
                "heartbeat so DEFA's own alive-timeout fallback can take over",
                shelly_max_age_seconds,
            )
        else:
            # Shelly's own reading is independent of every station, so this
            # is safe to record even if a station below turns out to be
            # unreachable - it must not freeze just because one charger did.
            last_main_fuse_a = (currents.l1_a, currents.l2_a, currents.l3_a)
            try:
                # Read every station individually - a station whose read
                # fails contributes a conservative 0A placeholder (never
                # understates house-only load, so it can only make the
                # computed allocation *more* cautious, not less) instead of
                # aborting the whole cycle - one unreachable charger must
                # not freeze the allocation and the other, healthy stations
                # along with it. Its own actual_a/installation_max_a display
                # is left untouched (last known value) rather than zeroed,
                # and it's skipped below when writing, since a station that
                # just failed to read is in no state to accept a write.
                readings: list[StationReading] = []
                failed_indices: set[int] = set()
                now = time.time()
                for i, st in enumerate(stations):
                    if st.client is None:
                        # No charger connected to read actual EV draw from,
                        # so this reflects headroom against house load alone.
                        reading = StationReading(
                            installation_max_current_ma=int(balancer_cfg.fuse_limit_a * 1000),
                            actual_current_l1_ma=0,
                            actual_current_l2_ma=0,
                            actual_current_l3_ma=0,
                        )
                    else:
                        try:
                            reading = st.client.read_station()
                        except IOError as exc:
                            logger.error(
                                "Modbus error reading %s, assuming 0A from it this cycle: %s", st.name, exc
                            )
                            st.online = False
                            failed_indices.add(i)
                            readings.append(
                                StationReading(
                                    installation_max_current_ma=int(st.installation_max_a * 1000)
                                    or int(balancer_cfg.fuse_limit_a * 1000),
                                    actual_current_l1_ma=0,
                                    actual_current_l2_ma=0,
                                    actual_current_l3_ma=0,
                                )
                            )
                            continue
                        st.online = True
                        st.last_seen = now

                    st.actual_a = (
                        reading.actual_current_l1_ma / 1000.0,
                        reading.actual_current_l2_ma / 1000.0,
                        reading.actual_current_l3_ma / 1000.0,
                    )
                    st.installation_max_a = reading.installation_max_current_ma / 1000.0
                    readings.append(reading)

                ev_l1_a = sum(r.actual_current_l1_ma for r in readings) / 1000.0
                ev_l2_a = sum(r.actual_current_l2_ma for r in readings) / 1000.0
                ev_l3_a = sum(r.actual_current_l3_ma for r in readings) / 1000.0

                raw_target_a = compute_max_charge_current_a(currents, ev_l1_a, ev_l2_a, ev_l3_a, balancer_cfg)
                new_allocated_a = controller.update(raw_target_a, loop_start)
                allocated_a = controller.committed_a

                # --- priority cascade: station 0 always tries; station i
                # only joins in if the one ahead of it is enabled but idle
                # (not actually using its allocation). ---
                desired_enabled = [False] * len(stations)
                if stations and allocated_a >= balancer_cfg.min_charge_current_a:
                    desired_enabled[0] = True
                    for i in range(1, len(stations)):
                        prev_actual_max_a = (
                            max(
                                readings[i - 1].actual_current_l1_ma,
                                readings[i - 1].actual_current_l2_ma,
                                readings[i - 1].actual_current_l3_ma,
                            )
                            / 1000.0
                        )
                        desired_enabled[i] = desired_enabled[i - 1] and prev_actual_max_a < idle_threshold_a

                # --- safety net: if actual combined draw among the stations
                # we intend to keep enabled exceeds the allocated ceiling
                # (e.g. two stations ramped up at once), force the lowest-
                # priority one of them off, one at a time. ---
                enabled_indices = [i for i, en in enumerate(desired_enabled) if en]
                if enabled_indices:
                    total_l1_a = sum(readings[i].actual_current_l1_ma for i in enabled_indices) / 1000.0
                    total_l2_a = sum(readings[i].actual_current_l2_ma for i in enabled_indices) / 1000.0
                    total_l3_a = sum(readings[i].actual_current_l3_ma for i in enabled_indices) / 1000.0
                    over_limit = max(total_l1_a, total_l2_a, total_l3_a) > allocated_a

                    if over_limit and (loop_start - last_forced_shutdown_at) >= shutdown_stagger_seconds:
                        victim = enabled_indices[-1]
                        desired_enabled[victim] = False
                        last_forced_shutdown_at = loop_start
                        logger.warning(
                            "Combined actual draw (L1=%.1f L2=%.1f L3=%.1f) exceeds allocated %.0fA - "
                            "forcing %s off",
                            total_l1_a,
                            total_l2_a,
                            total_l3_a,
                            allocated_a,
                            stations[victim].name,
                        )

                # --- write to each station: always on a state change,
                # otherwise only when the allocated ceiling itself changed.
                # A station that just failed to read above is skipped - its
                # connection is presumed broken this cycle, so there's
                # nothing reliable to write to it. ---
                for i, st in enumerate(stations):
                    if i in failed_indices:
                        continue
                    want_on = desired_enabled[i]
                    target_ma = min(int(allocated_a * 1000), readings[i].installation_max_current_ma) if want_on else 0
                    state_changed = want_on != st.enabled
                    if state_changed or (want_on and new_allocated_a is not None):
                        if st.client is not None:
                            try:
                                st.client.set_ems_max_current_ma(target_ma)
                            except IOError as exc:
                                logger.error("Failed to write to %s, will retry next cycle: %s", st.name, exc)
                                continue
                        if state_changed:
                            logger.info(
                                "%s%s -> %s (%dA)",
                                "[shelly-only, not written] " if shelly_only else "",
                                st.name,
                                f"{_COLOR_ON}ON{_COLOR_RESET}" if want_on else f"{_COLOR_OFF}OFF{_COLOR_RESET}",
                                target_ma // 1000,
                            )
                        st.enabled = want_on

                status_values = (
                    currents.l1_a,
                    currents.l2_a,
                    currents.l3_a,
                    ev_l1_a,
                    ev_l2_a,
                    ev_l3_a,
                    allocated_a,
                )
                if last_status_values is None or any(
                    abs(new - old) > _STATUS_CHANGE_THRESHOLD_A for new, old in zip(status_values, last_status_values)
                ):
                    chargers_str = "  ".join(
                        f"{st.name}={_COLOR_ON if st.enabled else _COLOR_OFF}{'ON' if st.enabled else 'OFF'}{_COLOR_RESET}"
                        for st in stations
                    )
                    logger.info(
                        "%sMain Fuse%s L1=%5.1f L2=%5.1f L3=%5.1f  "
                        "%sEV actual%s L1=%5.1f L2=%5.1f L3=%5.1f  "
                        "%sAllocated Charging Power%s=%3.0fA  %s",
                        _COLOR_HOUSE, _COLOR_RESET, currents.l1_a, currents.l2_a, currents.l3_a,
                        _COLOR_EV, _COLOR_RESET, ev_l1_a, ev_l2_a, ev_l3_a,
                        _COLOR_ALLOCATED, _COLOR_RESET, allocated_a,
                        chargers_str,
                    )
                    last_status_values = status_values
            except IOError as exc:
                logger.error("Modbus error, will retry next cycle: %s", exc)

        if have_fresh_data and not shelly_only and loop_start - last_alive_sent >= alive_interval:
            for st in stations:
                try:
                    if st.client is not None:
                        st.client.send_alive()
                except IOError as exc:
                    logger.error("Failed to send alive to %s: %s", st.name, exc)
            last_alive_sent = loop_start

        if state is not None:
            state.update(
                have_fresh_data=have_fresh_data,
                main_fuse_l1_a=last_main_fuse_a[0],
                main_fuse_l2_a=last_main_fuse_a[1],
                main_fuse_l3_a=last_main_fuse_a[2],
                ev_total_l1_a=sum(st.actual_a[0] for st in stations),
                ev_total_l2_a=sum(st.actual_a[1] for st in stations),
                ev_total_l3_a=sum(st.actual_a[2] for st in stations),
                allocated_a=controller.committed_a or 0.0,
                chargers=[
                    ChargerStatus(
                        name=st.name,
                        host=st.host,
                        port=st.port,
                        unit_id=st.unit_id,
                        enabled=st.enabled,
                        actual_l1_a=st.actual_a[0],
                        actual_l2_a=st.actual_a[1],
                        actual_l3_a=st.actual_a[2],
                        installation_max_a=st.installation_max_a,
                        online=st.online,
                        last_seen=st.last_seen,
                    )
                    for st in stations
                ],
            )

        if history_recorder is not None and loop_start - last_history_sample_at >= history_sample_interval:
            history_recorder.record(
                timestamp=time.time(),
                main_fuse_w=amps_to_watts(*last_main_fuse_a, voltage=history_voltage),
                chargers={
                    st.name: (amps_to_watts(*st.actual_a, voltage=history_voltage), st.enabled, st.online)
                    for st in stations
                },
            )
            last_history_sample_at = loop_start

        elapsed = time.monotonic() - loop_start
        time.sleep(max(0.0, poll_interval - elapsed))


def main() -> None:
    parser = argparse.ArgumentParser(description="EV load balancer: Shelly Pro 3EM -> DEFA Power")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--shelly-only",
        action="store_true",
        help="Don't connect to any DEFA station; just log what the balancer would set, based on Shelly readings alone.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    run(config, shelly_only=args.shelly_only)


if __name__ == "__main__":
    main()
