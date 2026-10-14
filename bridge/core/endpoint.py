"""An Endpoint is one device as the gateway sees it.

A physical appliance may map to several endpoints (an air conditioner plus
an outdoor temperature sensor, a heat pump zone plus a hot-water tank).
Vendor adapters subclass Endpoint and translate between the CUBE state
format and the appliance's own API.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bridge.core.energy import EnergyLog

log = logging.getLogger(__name__)

ENERGY_RESOLUTION = 3600  # seconds per usage bucket reported to the gateway

StateListener = Callable[["Endpoint"], None]
OnlineListener = Callable[["Endpoint", bool], None]


def on_off(value: Any) -> str:
    return "on" if value else "off"


class Endpoint(ABC):
    display_category: str = "switch"
    manufacturer: str = "Unknown"
    model: str = ""
    firmware_version: str = "1.0.0"

    def __init__(self, third_serial: str, name: str) -> None:
        self.third_serial = third_serial
        self.name = name
        self.online = True
        self.energy_log: EnergyLog | None = None  # attached by the app when energy_counter_kwh() has data
        self._state_listeners: list[StateListener] = []
        self._online_listeners: list[OnlineListener] = []

    # ----- optional energy metering ---------------------------------------

    def energy_counter_kwh(self) -> float | None:
        """Lifetime energy counter in kWh, or None if the appliance has none."""
        return None

    def energy_capability(self) -> dict[str, Any]:
        from bridge.core.energy import local_tz_offset_hours

        return cap(
            "power-consumption",
            "1101",
            settings={
                "resolution": {"permission": "01", "type": "numeric", "value": ENERGY_RESOLUTION},
                "timeZoneOffset": {"permission": "01", "type": "numeric", "min": -12, "max": 14, "value": local_tz_offset_hours()},
            },
        )

    # ----- to be implemented by adapters ----------------------------------

    @abstractmethod
    def capabilities(self) -> list[dict[str, Any]]:
        """Capability declarations in CUBE format."""

    @abstractmethod
    def state(self) -> dict[str, Any]:
        """Full current state in CUBE format (only capabilities declared above)."""

    @abstractmethod
    async def apply(self, state: dict[str, Any]) -> None:
        """Apply a CUBE state fragment coming from an UpdateDeviceStates directive."""

    # ----- plumbing ---------------------------------------------------------

    def add_state_listener(self, cb: StateListener) -> None:
        self._state_listeners.append(cb)

    def add_online_listener(self, cb: OnlineListener) -> None:
        self._online_listeners.append(cb)

    def notify_state(self) -> None:
        for cb in self._state_listeners:
            cb(self)

    def set_online(self, online: bool) -> None:
        if online == self.online:
            return
        self.online = online
        for cb in self._online_listeners:
            cb(self, online)

    def describe(self, service_address: str) -> dict[str, Any]:
        """Endpoint object for a DiscoveryRequest."""
        capabilities = self.capabilities()
        if self.energy_log is not None:
            capabilities.append(self.energy_capability())
        return {
            "third_serial_number": self.third_serial,
            "name": self.name,
            "display_category": self.display_category,
            "capabilities": capabilities,
            "state": self.state(),
            "tags": {"bridge": "ihost-bridge"},
            "manufacturer": self.manufacturer,
            "model": self.model,
            "firmware_version": self.firmware_version,
            "service_address": service_address,
        }


# ----- capability declaration helpers -------------------------------------

def cap(capability: str, permission: str, name: str | None = None, settings: dict | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"capability": capability, "permission": permission}
    if name:
        d["name"] = name
    if settings:
        d["settings"] = settings
    return d


def cap_mode(name: str, values: list[str]) -> dict[str, Any]:
    return cap(
        "mode",
        "1100",
        name,
        {"supportedValues": {"type": "enum", "permission": "01", "values": values}},
    )


def cap_setpoint(name: str, minimum: float, maximum: float, step: float = 0.5, writable: bool = True) -> dict[str, Any]:
    return cap(
        "thermostat-target-setpoint",
        "1110" if writable else "0110",
        name,
        {
            "temperatureUnit": {"type": "enum", "permission": "01", "value": "c", "values": ["c", "f"]},
            "temperatureRange": {"type": "numeric", "permission": "01", "min": minimum, "max": maximum, "step": step},
        },
    )


def cap_temperature(minimum: float = -40, maximum: float = 100) -> dict[str, Any]:
    return cap(
        "temperature",
        "0100",
        settings={"temperatureRange": {"type": "numeric", "permission": "01", "min": minimum, "max": maximum}},
    )


def cap_humidity() -> dict[str, Any]:
    return cap("humidity", "0100", settings={"humidityRange": {"type": "numeric", "permission": "01", "min": 0, "max": 100}})
