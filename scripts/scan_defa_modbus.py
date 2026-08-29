#!/usr/bin/env python3
"""Brute-force scan of a DEFA Power station's Modbus registers.

DEFA's register map PDF only documents a handful of addresses (see
ev_balancer/defa_modbus.py) - notably nothing for the vehicle's state of
charge. This reads every register in a range, one at a time (so a gap
between implemented registers can't make a whole block read fail), and
prints whatever comes back.

If --soc is given (the car's actual state of charge right now, read off
its own dashboard/app), each readable register - and each adjacent pair
combined into a uint32 the way DEFA's documented registers are, see the
module docstring in defa_modbus.py - is also checked against that value at
three plausible fixed-point scales (raw %, x10, x100) and near-matches are
reported. Absence of a match is itself informative: it likely means this
station just doesn't expose vehicle SOC over local Modbus at all (that
data usually only reaches a backend via OCPP/ISO 15118, not this
interface).

Usage:
    .venv/bin/python scripts/scan_defa_modbus.py --host 10.30.0.200 --soc 62
"""

from __future__ import annotations

import argparse
import time

import yaml
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

READ_FUNCS = {
    "input": lambda client, addr, unit: client.read_input_registers(addr, count=1, device_id=unit),
    "holding": lambda client, addr, unit: client.read_holding_registers(addr, count=1, device_id=unit),
}


def scan(client: ModbusTcpClient, unit_id: int, start: int, end: int, functions: list[str], delay: float):
    """Yield (function_name, address, value) for every readable register in [start, end]."""
    total = (end - start + 1) * len(functions)
    attempted = 0
    found = 0
    for name in functions:
        read = READ_FUNCS[name]
        for addr in range(start, end + 1):
            attempted += 1
            try:
                result = read(client, addr, unit_id)
            except ModbusException:
                result = None
            if result is not None and not result.isError():
                found += 1
                yield name, addr, result.registers[0]
            if delay:
                time.sleep(delay)
            if attempted % 500 == 0:
                print(f"  ... scanned {attempted}/{total}, {found} readable so far")


def find_soc_candidates(readings: list[tuple[str, int, int]], soc: float, tolerance: float):
    """Check every register (alone, and paired with the next same-function one as a
    big-endian uint32) against `soc` at scales 1/10/100, and return near-matches
    sorted closest-first."""
    by_key = {(name, addr): value for name, addr, value in readings}
    candidates = []
    for name, addr, value in readings:
        for scale, label in ((1, "uint16"), (10, "uint16/10"), (100, "uint16/100")):
            diff = abs(value / scale - soc)
            if diff <= tolerance:
                candidates.append((diff, name, addr, value, label))
        nxt = by_key.get((name, addr + 1))
        if nxt is None:
            continue
        combined = (value << 16) | nxt
        for scale, label in ((1, "uint32"), (10, "uint32/10"), (100, "uint32/100")):
            diff = abs(combined / scale - soc)
            if diff <= tolerance:
                candidates.append((diff, name, addr, combined, f"{label} (regs {addr}-{addr + 1})"))
    candidates.sort(key=lambda c: c[0])
    return candidates


def defaults_from_config(path: str) -> dict:
    try:
        with open(path) as f:
            config = yaml.safe_load(f)
        station = config["defa"]["stations"][0]
        return {"host": station["host"], "port": station.get("port", 502), "unit_id": station.get("unit_id", 255)}
    except (OSError, KeyError, IndexError, TypeError):
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml", help="Used to default --host/--port/--unit-id from defa.stations[0]")
    parser.add_argument("--host", help="DEFA station IP (default: first station in --config)")
    parser.add_argument("--port", type=int, help="default: from --config, else 502")
    parser.add_argument("--unit-id", type=int, help="default: from --config, else 255")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--end", type=int, default=3000)
    parser.add_argument("--function", choices=["input", "holding", "both"], default="both")
    parser.add_argument("--soc", type=float, help="Car's actual state of charge in %% right now, to match against")
    parser.add_argument("--tolerance", type=float, default=1.0, help="Allowed +/- %% difference when matching --soc")
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds to sleep between reads, to throttle the scan")
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.2,
        help="Seconds to wait for a reply before treating a register as unimplemented (default: 0.2, "
        "generous for a LAN device but small enough to get through a wide range in reasonable time - "
        "the station doesn't send a Modbus exception for unimplemented registers, it just stays silent, "
        "so every one of those costs a full timeout)",
    )
    args = parser.parse_args()

    defaults = defaults_from_config(args.config)
    host = args.host or defaults.get("host")
    port = args.port or defaults.get("port", 502)
    unit_id = args.unit_id if args.unit_id is not None else defaults.get("unit_id", 255)
    if not host:
        parser.error("--host is required (no station found in --config either)")

    functions = ["input", "holding"] if args.function == "both" else [args.function]

    # retries=0: one attempt per address, no automatic retry - a non-response is expected
    # for most addresses here, not a transient error worth retrying.
    client = ModbusTcpClient(host, port=port, timeout=args.timeout, retries=0)
    # pymodbus's sync client force-disconnects after a handful of consecutive non-responses
    # (it's meant for a client that expects to stay connected to one always-present device,
    # not for probing thousands of addresses most of which won't answer) - it does
    # auto-reconnect on the next request, but that adds a needless TCP handshake per burst
    # of misses. Effectively disable that circuit breaker for the scan.
    client.set_max_no_responses(1_000_000)
    if not client.connect():
        raise SystemExit(f"Could not connect to {host}:{port}")

    total = (args.end - args.start + 1) * len(functions)
    est_seconds = total * args.timeout
    print(f"Worst case (every address unimplemented): ~{est_seconds / 60:.1f} minutes. Ctrl-C to stop early.")

    readings: list[tuple[str, int, int]] = []
    print(f"Scanning registers {args.start}-{args.end} ({functions}) on {host}:{port} unit {unit_id}...")
    try:
        for name, addr, value in scan(client, unit_id, args.start, args.end, functions, args.delay):
            readings.append((name, addr, value))
            print(f"  {name:7s} {addr:5d} = {value:6d} (0x{value:04X})")
    finally:
        client.close()

    print(f"\n{len(readings)} readable registers total.")

    if args.soc is not None:
        candidates = find_soc_candidates(readings, args.soc, args.tolerance)
        if not candidates:
            print(f"\nNo register matched SOC={args.soc}% within +/-{args.tolerance}.")
            print("This likely means the station doesn't expose vehicle SOC over local Modbus at all.")
        else:
            print(f"\nCandidates for SOC={args.soc}% (closest first):")
            for diff, name, addr, value, form in candidates:
                print(f"  {name:7s} reg {addr:5d}: raw={value} -> {form}, off by {diff:.2f}%")


if __name__ == "__main__":
    main()
