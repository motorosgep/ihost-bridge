"""Midea air conditioner (type 0xAC) as an iHost airConditioner, plus an outdoor temperature sensor."""

from __future__ import annotations

import logging
from typing import Any

from midealocal.device import MideaDevice

from bridge.core.endpoint import Endpoint, cap, cap_humidity, cap_mode, cap_setpoint, cap_temperature, on_off
from bridge.midea.common import MideaEndpoint, num

log = logging.getLogger(__name__)

# midea-local mode numbers <-> CUBE airConditionerMode values
MODE_TO_CUBE = {1: "auto", 2: "cool", 3: "dry", 4: "heat", 5: "fan"}
CUBE_TO_MODE = {v: k for k, v in MODE_TO_CUBE.items()}

# CUBE fanLevel <-> midea-local fan_speed set-points (20 silent, 40 low, 60 medium, 80 high, 100 full, 102 auto)
FAN_LEVEL_TO_SPEED = {"low": 40, "medium": 60, "high": 80, "auto": 102}
FAN_LEVELS = list(FAN_LEVEL_TO_SPEED)


def fan_level(speed: Any) -> str:
    try:
        s = int(speed)
    except (TypeError, ValueError):
        return "auto"
    if s > 100:
        return "auto"
    if s > 60:
        return "high"
    if s > 40:
        return "medium"
    return "low"


class MideaAcEndpoint(MideaEndpoint):
    display_category = "airConditioner"

    def __init__(self, device: MideaDevice, third_serial: str, name: str) -> None:
        super().__init__(device, third_serial, name)
        a = self.attrs
        self._has_humidity = a.get("indoor_humidity") is not None
        self._has_power_meter = a.get("realtime_power") is not None

    def capabilities(self) -> list[dict[str, Any]]:
        a = self.attrs
        tmin = num(a.get("min_temperature")) or 16.0
        tmax = num(a.get("max_temperature")) or 30.0
        caps = [
            cap("power", "1100"),
            cap_mode("airConditionerMode", ["cool", "heat", "auto", "fan", "dry"]),
            cap_mode("fanLevel", FAN_LEVELS),
            cap_setpoint("manual-mode", tmin, tmax, 0.5),
            cap_temperature(-20, 60),
            cap("vertical-swing", "1100"),
            cap("horizontal-swing", "1100"),
            cap("eco", "1100"),
        ]
        if self._has_humidity:
            caps.append(cap_humidity())
        if self._has_power_meter:
            caps.append(cap("electric-power", "0100"))
        return caps

    def state(self) -> dict[str, Any]:
        a = self.attrs
        st: dict[str, Any] = {
            "power": {"powerState": on_off(a.get("power"))},
            "mode": {
                "airConditionerMode": {"modeValue": MODE_TO_CUBE.get(a.get("mode"), "auto")},
                "fanLevel": {"modeValue": fan_level(a.get("fan_speed"))},
            },
            "thermostat-target-setpoint": {
                "manual-mode": {"targetSetpoint": num(a.get("target_temperature")) or 24.0}
            },
            "vertical-swing": {"powerState": on_off(a.get("swing_vertical"))},
            "horizontal-swing": {"powerState": on_off(a.get("swing_horizontal"))},
            "eco": {"powerState": on_off(a.get("eco_mode"))},
        }
        indoor = num(a.get("indoor_temperature"))
        if indoor is not None:
            st["temperature"] = {"temperature": indoor}
        if self._has_humidity and a.get("indoor_humidity") is not None:
            st["humidity"] = {"humidity": int(a["indoor_humidity"])}
        if self._has_power_meter:
            watts = num(a.get("realtime_power"), 2) or 0.0
            st["electric-power"] = {"electric-power": int(round(watts * 100))}  # unit is 0.01 W
        return st

    def energy_counter_kwh(self) -> float | None:
        total = num(self.attrs.get("total_energy_consumption"), 2)
        return total if total else None  # units without a meter report 0.0

    async def apply(self, state: dict[str, Any]) -> None:
        sets: list[tuple[str, Any]] = []
        if "power" in state:
            sets.append(("power", state["power"].get("powerState") == "on"))
        mode = state.get("mode") or {}
        if "airConditionerMode" in mode:
            value = mode["airConditionerMode"].get("modeValue")
            if value not in CUBE_TO_MODE:
                raise ValueError(f"unsupported airConditionerMode {value!r}")
            sets.append(("mode", CUBE_TO_MODE[value]))
        if "fanLevel" in mode:
            value = mode["fanLevel"].get("modeValue")
            if value not in FAN_LEVEL_TO_SPEED:
                raise ValueError(f"unsupported fanLevel {value!r}")
            sets.append(("fan_speed", FAN_LEVEL_TO_SPEED[value]))
        setpoint = state.get("thermostat-target-setpoint") or {}
        for key in ("manual-mode", "auto-mode", "eco-mode"):
            if key in setpoint and "targetSetpoint" in setpoint[key]:
                sets.append(("target_temperature", float(setpoint[key]["targetSetpoint"])))
        for cube_key, attr in (("vertical-swing", "swing_vertical"), ("horizontal-swing", "swing_horizontal"), ("eco", "eco_mode")):
            if cube_key in state:
                sets.append((attr, state[cube_key].get("powerState") == "on"))
        if not sets:
            raise ValueError("nothing applicable in directive")
        await self.set_attributes(sets)


class MideaAcOutdoorEndpoint(MideaEndpoint):
    display_category = "temperatureSensor"

    def capabilities(self) -> list[dict[str, Any]]:
        return [cap_temperature(-40, 60)]

    def state(self) -> dict[str, Any]:
        outdoor = num(self.attrs.get("outdoor_temperature"))
        return {"temperature": {"temperature": outdoor}} if outdoor is not None else {}


# Fresh-air (ventilation) module: midea-local fan_speed set-points 20 silent, 40 low, 60 medium, 80 high, 100 full
FRESH_LEVEL_TO_SPEED = {"low": 40, "medium": 60, "high": 80}


def fresh_level(speed: Any) -> str:
    try:
        s = int(speed)
    except (TypeError, ValueError):
        return "low"
    if s > 60:
        return "high"
    if s > 40:
        return "medium"
    return "low"


class MideaAcFreshAirEndpoint(MideaEndpoint):
    """The AC's fresh-air intake as an iHost fan: on/off plus low/medium/high."""

    display_category = "fan"

    def capabilities(self) -> list[dict[str, Any]]:
        return [cap("power", "1100"), cap_mode("fanLevel", list(FRESH_LEVEL_TO_SPEED))]

    def state(self) -> dict[str, Any]:
        a = self.attrs
        return {
            "power": {"powerState": on_off(a.get("fresh_air_power"))},
            "mode": {"fanLevel": {"modeValue": fresh_level(a.get("fresh_air_fan_speed"))}},
        }

    async def apply(self, state: dict[str, Any]) -> None:
        sets: list[tuple[str, Any]] = []
        if "power" in state:
            sets.append(("fresh_air_power", state["power"].get("powerState") == "on"))
        level = ((state.get("mode") or {}).get("fanLevel") or {}).get("modeValue")
        if level is not None:
            if level not in FRESH_LEVEL_TO_SPEED:
                raise ValueError(f"unsupported fanLevel {level!r}")
            sets.append(("fresh_air_fan_speed", FRESH_LEVEL_TO_SPEED[level]))
        if not sets:
            raise ValueError("nothing applicable in directive")
        await self.set_attributes(sets)


def make_ac_endpoints(device: MideaDevice, base_name: str, existing: set[str] | None = None) -> list[Endpoint]:
    """Endpoints the unit can back right now, skipping any whose third_serial is in `existing`."""
    existing = existing or set()
    did = device.device_id
    a = device.attributes
    candidates: list[tuple[str, Any]] = [(f"midea-ac-{did}", lambda s: MideaAcEndpoint(device, s, base_name))]
    if a.get("outdoor_temperature") is not None:
        candidates.append((f"midea-ac-{did}-outdoor", lambda s: MideaAcOutdoorEndpoint(device, s, f"{base_name} outdoor")))
    if a.get("fresh_air_mode") is not None:  # only reported by units that have the module
        candidates.append((f"midea-ac-{did}-fresh-air", lambda s: MideaAcFreshAirEndpoint(device, s, f"{base_name} fresh air")))
    return [build(serial) for serial, build in candidates if serial not in existing]
