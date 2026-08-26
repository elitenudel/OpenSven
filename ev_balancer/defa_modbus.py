"""Modbus TCP client for the DEFA Power charging station.

Register map from DEFA's documentation. All values are uint32 spread over
2 registers, big-endian (high word first). Read-only values live in input
registers (function code 4); read/write values live in holding registers
(function code 3 to read, 16 to write).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

logger = logging.getLogger(__name__)

# (start address, register count)
INSTALLATION_MAX_CURRENT = 290  # R, mA
ACTUAL_CURRENT_L1 = 293  # R, mA
ACTUAL_CURRENT_L2 = 296  # R, mA
ACTUAL_CURRENT_L3 = 299  # R, mA
STATION_STATUS = 302  # R
STATION_POWER = 305  # R, mW
EMS_MAX_CURRENT = 2000  # R/W, mA
TIMEOUT_MAX_CHARGE_CURRENT = 2004  # R/W, mA
ALIVE = 2008  # W
ALIVE_TIMEOUT = 2012  # R/W, ms


def _encode_uint32(value: int) -> list[int]:
    value &= 0xFFFFFFFF
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]


def _decode_uint32(registers: list[int]) -> int:
    return (registers[0] << 16) | registers[1]


@dataclass
class StationReading:
    installation_max_current_ma: int
    actual_current_l1_ma: int
    actual_current_l2_ma: int
    actual_current_l3_ma: int


class DefaModbusClient:
    def __init__(self, host: str, port: int = 502, unit_id: int = 255):
        self._client = ModbusTcpClient(host, port=port)
        self._unit_id = unit_id

    def connect(self) -> None:
        if not self._client.connect():
            raise ConnectionError(f"Could not connect to DEFA Power at {self._client.comm_params.host}")

    def close(self) -> None:
        self._client.close()

    def _read_input_uint32(self, address: int) -> int:
        try:
            result = self._client.read_input_registers(address, count=2, device_id=self._unit_id)
        except ModbusException as exc:
            raise IOError(f"Modbus communication error reading input register {address}: {exc}") from exc
        if result.isError():
            raise IOError(f"Modbus read error at input register {address}: {result}")
        return _decode_uint32(result.registers)

    def _read_holding_uint32(self, address: int) -> int:
        try:
            result = self._client.read_holding_registers(address, count=2, device_id=self._unit_id)
        except ModbusException as exc:
            raise IOError(f"Modbus communication error reading holding register {address}: {exc}") from exc
        if result.isError():
            raise IOError(f"Modbus read error at holding register {address}: {result}")
        return _decode_uint32(result.registers)

    def _write_uint32(self, address: int, value: int) -> None:
        try:
            result = self._client.write_registers(address, _encode_uint32(value), device_id=self._unit_id)
        except ModbusException as exc:
            raise IOError(f"Modbus communication error writing register {address}: {exc}") from exc
        if result.isError():
            raise IOError(f"Modbus write error at register {address}: {result}")

    def read_station(self) -> StationReading:
        return StationReading(
            installation_max_current_ma=self._read_input_uint32(INSTALLATION_MAX_CURRENT),
            actual_current_l1_ma=self._read_input_uint32(ACTUAL_CURRENT_L1),
            actual_current_l2_ma=self._read_input_uint32(ACTUAL_CURRENT_L2),
            actual_current_l3_ma=self._read_input_uint32(ACTUAL_CURRENT_L3),
        )

    def read_ems_max_current_ma(self) -> int:
        return self._read_holding_uint32(EMS_MAX_CURRENT)

    def set_ems_max_current_ma(self, value_ma: int) -> None:
        self._write_uint32(EMS_MAX_CURRENT, value_ma)

    def set_timeout_max_charge_current_ma(self, value_ma: int) -> None:
        """Fallback current applied by the station if `alive` isn't kept fresh."""
        self._write_uint32(TIMEOUT_MAX_CHARGE_CURRENT, value_ma)

    def set_alive_timeout_ms(self, value_ms: int) -> None:
        self._write_uint32(ALIVE_TIMEOUT, value_ms)

    def send_alive(self) -> None:
        self._write_uint32(ALIVE, 1)
