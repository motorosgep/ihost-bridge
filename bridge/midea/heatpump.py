"""Midea air-to-water heat pump (type 0xC3, "M-Thermal") exposed as thermostats and sensors.

Endpoints per unit:
- "<name> zone 1": thermostat. Power is the zone enable, setpoint is the zone
  target (water or room temperature depending on the unit's zone mode),
  temperature is the supply water temperature.
- "<name> hot water": thermostat. Power is the DHW enable, setpoint is the tank
  target, temperature is the tank temperature.
- "<name> outdoor": temperature sensor.
"""

from __future__ import annotations

import logging
from typing import Any

from midealocal.device import MideaDevice

from bridge.core.endpoint import Endpoint, cap, cap_setpoint, cap_temperature, on_off
from bridge.midea.common import MideaEndpoint, num

log = logging.getLogger(__name__)


def _thermostat_mode_cap() -> dict[str, Any]:
    return cap(
        "thermostat",
        "1100",
        "thermostat-mode",
        {"supportedModes": {"type": "enum", "permission": "01", "values": ["MANUAL"]}},
    )


def _first(value: Any, index: int = 0) -> Any:
    if isinstance(value, (list, tuple)):
        return value[index] if len(value) > index else None
    return value


class HeatPumpZoneEndpoint(MideaEndpoint):
    display_category = "thermostat"

    def __init__(self, device: MideaDevice, third_serial: str, name: str, zone: int = 0) -> None:
        super().__init__(device, third_serial, name)
        self.zone = zone

    def _limits(self) -> tuple[float, float]:
        a = self.attrs
        heating = a.get("mode") != 2  # C3 mode 2 is cooling
        # these limits are per-zone lists on the device
        lo = num(_first(a.get("zone_heating_temp_min" if heating else "zone_cooling_temp_min"), self.zone))
        hi = num(_first(a.get("zone_heating_temp_max" if heating else "zone_cooling_temp_max"), self.zone))
        if not _first(a.get("zone_temp_type"), self.zone):  # room-temperature mode uses the room limits
            lo, hi = num(a.get("room_temp_min")), num(a.get("room_temp_max"))
        return (lo if lo is not None else 5.0, hi if hi is not None else 60.0)

    def _target(self) -> float | None:
        a = self.attrs
        water_mode = _first(a.get("zone_temp_type"), self.zone)
        if water_mode:
            return num(_first(a.get("zone_target_temp"), self.zone))
        return num(a.get("room_target_temp"))

    def capabilities(self) -> list[dict[str, Any]]:
        lo, hi = self._limits()
        return [
            cap("power", "1100"),
            _thermostat_mode_cap(),
            cap_setpoint("manual-mode", lo, hi, 1.0),
            cap_temperature(-10, 90),
            cap("eco", "1100"),
        ]

    def energy_counter_kwh(self) -> float | None:
        if self.zone != 0:
            return None  # the counter is unit-wide; attach it to zone 1 only
        return num(self.attrs.get("total_energy_consumption"), 2)

    def state(self) -> dict[str, Any]:
        a = self.attrs
        power_attr = "zone1_power" if self.zone == 0 else "zone2_power"
        st: dict[str, Any] = {
            "power": {"powerState": on_off(a.get(power_attr))},
            "thermostat": {"thermostat-mode": {"thermostatMode": "MANUAL"}},
            "eco": {"powerState": on_off(a.get("eco_mode"))},
        }
        target = self._target()
        if target is not None:
            st["thermostat-target-setpoint"] = {"manual-mode": {"targetSetpoint": target}}
        supply = num(a.get("hp_temp_tw_out"))
        if supply is None:
            supply = num(a.get("temp_tw_out"))
        if supply is not None:
            st["temperature"] = {"temperature": supply}
        return st

    async def apply(self, state: dict[str, Any]) -> None:
        sets: list[tuple[str, Any]] = []
        if "power" in state:
            attr = "zone1_power" if self.zone == 0 else "zone2_power"
            sets.append((attr, state["power"].get("powerState") == "on"))
        if "eco" in state:
            sets.append(("eco_mode", state["eco"].get("powerState") == "on"))
        setpoint = state.get("thermostat-target-setpoint") or {}
        target = next(
            (float(v["targetSetpoint"]) for v in setpoint.values() if isinstance(v, dict) and "targetSetpoint" in v),
            None,
        )
        if not sets and target is None:
            if "thermostat" in state:
                return  # mode is always MANUAL; nothing to do
            raise ValueError("nothing applicable in directive")
        if sets:
            await self.set_attributes(sets)
        if target is not None:
            await self._loop.run_in_executor(
                None, lambda: self.device.set_target_temperature(target, None, zone=self.zone)  # type: ignore[attr-defined]
            )


class HeatPumpDhwEndpoint(MideaEndpoint):
    display_category = "thermostat"

    def capabilities(self) -> list[dict[str, Any]]:
        a = self.attrs
        lo = num(a.get("dhw_temp_min")) or 35.0
        hi = num(a.get("dhw_temp_max")) or 60.0
        return [
            cap("power", "1100"),
            _thermostat_mode_cap(),
            cap_setpoint("manual-mode", lo, hi, 1.0),
            cap_temperature(0, 90),
        ]

    def state(self) -> dict[str, Any]:
        a = self.attrs
        st: dict[str, Any] = {
            "power": {"powerState": on_off(a.get("dhw_power"))},
            "thermostat": {"thermostat-mode": {"thermostatMode": "MANUAL"}},
        }
        target = num(a.get("dhw_target_temp"))
        if target is not None:
            st["thermostat-target-setpoint"] = {"manual-mode": {"targetSetpoint": target}}
        tank = num(a.get("tank_actual_temperature"))
        if tank is not None:
            st["temperature"] = {"temperature": tank}
        return st

    async def apply(self, state: dict[str, Any]) -> None:
        sets: list[tuple[str, Any]] = []
        if "power" in state:
            sets.append(("dhw_power", state["power"].get("powerState") == "on"))
        setpoint = state.get("thermostat-target-setpoint") or {}
        for v in setpoint.values():
            if isinstance(v, dict) and "targetSetpoint" in v:
                sets.append(("dhw_target_temp", int(round(float(v["targetSetpoint"])))))
                break
        if not sets:
            if "thermostat" in state:
                return
            raise ValueError("nothing applicable in directive")
        await self.set_attributes(sets)


class HeatPumpTemperatureEndpoint(MideaEndpoint):
    """One temperature reading as its own iHost sensor; first attribute with a value wins."""

    display_category = "temperatureSensor"

    def __init__(self, device: MideaDevice, third_serial: str, name: str, attributes: tuple[str, ...]) -> None:
        super().__init__(device, third_serial, name)
        self.attributes = attributes

    def value(self) -> float | None:
        for attr in self.attributes:
            v = num(self.attrs.get(attr))
            if v is not None:
                return v
        return None

    def capabilities(self) -> list[dict[str, Any]]:
        return [cap_temperature(-40, 100)]

    def state(self) -> dict[str, Any]:
        v = self.value()
        return {"temperature": {"temperature": v}} if v is not None else {}


class HeatPumpPowerEndpoint(MideaEndpoint):
    """Electrical telemetry of the outdoor unit as a general sensor: power, voltage, compressor current."""

    display_category = "sensor"

    def capabilities(self) -> list[dict[str, Any]]:
        a = self.attrs
        caps = []
        if a.get("hp_instant_power") is not None or a.get("instant_power0") is not None:
            caps.append(cap("electric-power", "0100"))
        if a.get("hp_voltage") is not None:
            caps.append(cap("voltage", "0100"))
        if a.get("hp_compressor_current") is not None:
            caps.append(cap("electric-current", "0100"))
        return caps

    def state(self) -> dict[str, Any]:
        a = self.attrs
        st: dict[str, Any] = {}
        watts = a.get("hp_instant_power") if a.get("hp_instant_power") is not None else a.get("instant_power0")
        if watts is not None:
            st["electric-power"] = {"electric-power": int(round(float(watts) * 100))}  # 0.01 W units
        if a.get("hp_voltage") is not None:
            st["voltage"] = {"voltage": int(round(float(a["hp_voltage"]) * 100))}  # 0.01 V units
        if a.get("hp_compressor_current") is not None:
            st["electric-current"] = {"electric-current": int(round(float(a["hp_compressor_current"]) * 100))}  # 0.01 A
        return st


TEMPERATURE_SENSORS = (
    ("outdoor", "outdoor", ("hp_temp_t4", "outdoor_temperature")),
    ("water-in", "water in", ("hp_temp_tw_in", "temp_tw_in")),
    ("water-out", "water out", ("hp_temp_tw_out", "temp_tw_out")),
)


def make_heatpump_endpoints(device: MideaDevice, base_name: str, existing: set[str] | None = None) -> list[Endpoint]:
    """Endpoints the unit can back right now, skipping any whose third_serial is in `existing`.

    Called again on every device update so sensors whose data only arrives later
    (the heat pump pushes outdoor temperature and energy on its own schedule)
    get created once the first value is in.
    """
    existing = existing or set()
    did = device.device_id
    a = device.attributes
    prefix = f"midea-c3-{did}"
    candidates: list[tuple[str, Any]] = [
        (f"{prefix}-zone1", lambda s: HeatPumpZoneEndpoint(device, s, f"{base_name} zone 1", zone=0)),
    ]
    if a.get("dhw_target_temp") is not None or a.get("dhw_power") is not None:
        candidates.append((f"{prefix}-dhw", lambda s: HeatPumpDhwEndpoint(device, s, f"{base_name} hot water")))
    for suffix, label, attrs in TEMPERATURE_SENSORS:
        if any(a.get(attr) is not None for attr in attrs):
            candidates.append(
                (f"{prefix}-{suffix}", lambda s, label=label, attrs=attrs: HeatPumpTemperatureEndpoint(device, s, f"{base_name} {label}", attrs))
            )
    if any(a.get(k) is not None for k in ("hp_instant_power", "instant_power0", "hp_voltage", "hp_compressor_current")):
        candidates.append((f"{prefix}-power", lambda s: HeatPumpPowerEndpoint(device, s, f"{base_name} power")))
    return [build(serial) for serial, build in candidates if serial not in existing]
