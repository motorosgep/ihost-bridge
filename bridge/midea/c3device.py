"""Heat pump (C3) device subclass that keeps the unit telemetry midea-local parses but drops.

midea-local decodes the "unit parameters" reply (compressor frequency, water flow,
pressures, sensor temperatures, power) but only copies the fields that have a
DeviceAttributes entry. This subclass re-reads the parsed message and stores the
rest under `hp_*` attribute names so endpoints can expose them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from midealocal.devices.c3 import MideaC3Device
from midealocal.devices.c3.message import MessageC3Response

log = logging.getLogger(__name__)


def _temp(value: Any) -> float | None:
    """Single-byte temperature, two's complement for sub-zero values."""
    if value is None:
        return None
    v = int(value)
    return float(v - 256) if v > 127 else float(v)


def _int(value: Any) -> int | None:
    return None if value is None else int(value)


# parsed message field -> (attribute name, converter)
TELEMETRY: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "comp_run_freq": ("hp_compressor_frequency", _int),        # Hz
    "odu_target_fre": ("hp_compressor_target_frequency", _int),
    "fan_speed": ("hp_outdoor_fan_speed", _int),               # rpm
    "unit_mode_run": ("hp_unit_mode_run", _int),
    "temp_t1": ("hp_temp_t1", _temp),                          # water outlet after backup heater
    "temp_t2": ("hp_temp_t2", _temp),                          # refrigerant liquid
    "temp_t2b": ("hp_temp_t2b", _temp),                        # refrigerant gas
    "temp_t3": ("hp_temp_t3", _temp),                          # outdoor coil
    "temp_t4": ("hp_temp_t4", _temp),                          # outdoor ambient
    "temp_t5": ("hp_temp_t5", _temp),                          # tank
    "temp_tp": ("hp_temp_tp", _temp),                          # compressor discharge
    "temp_th": ("hp_temp_th", _temp),                          # compressor suction
    "temp_tw_in": ("hp_temp_tw_in", _temp),                    # water inlet
    "temp_tw_out": ("hp_temp_tw_out", _temp),                  # water outlet
    "temp_ta": ("hp_temp_ta", _temp),                          # room (wired controller)
    "odu_voltage": ("hp_voltage", _int),                       # V
    "dc_current": ("hp_compressor_current", _int),             # A
    "pressure_high": ("hp_pressure_high", _int),
    "pressure_low": ("hp_pressure_low", _int),
    "water_flower": ("hp_water_flow", _int),
    "water_pressure": ("hp_water_pressure", _int),
    "pwm_pump_out": ("hp_pump_pwm", _int),
    "current_unit_capacity": ("hp_unit_capacity", _int),
    "instant_power0": ("hp_instant_power", _int),
    "total_electricity0": ("hp_total_electricity", _int),
    "total_thermal0": ("hp_total_thermal", _int),
}


class BridgeC3Device(MideaC3Device):
    """MideaC3Device plus `hp_*` telemetry attributes."""

    def process_message(self, msg: bytes) -> dict[str, Any]:
        status = super().process_message(msg)
        try:
            message = MessageC3Response(bytearray(msg))
        except Exception as exc:  # noqa: BLE001
            log.debug("[%s] telemetry re-parse failed: %s", self.device_id, exc)
            return status
        for field, (attr, convert) in TELEMETRY.items():
            try:
                raw = getattr(message, field)
            except AttributeError:
                continue
            try:
                value = convert(raw)
            except (TypeError, ValueError):
                continue
            if self._attributes.get(attr) != value:
                self._attributes[attr] = value
                status[attr] = value
        return status
