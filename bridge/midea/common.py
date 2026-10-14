"""Shared Midea helpers: token cache, LAN discovery, device construction, update plumbing."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from midealocal.const import DeviceType, ProtocolVersion
from midealocal.device import MideaDevice
from midealocal.devices.ac import MideaACDevice
from midealocal.discover import discover

from bridge.core.endpoint import Endpoint
from bridge.midea.c3device import BridgeC3Device

log = logging.getLogger(__name__)

DEVICE_CLASSES: dict[DeviceType, type[MideaDevice]] = {
    DeviceType.AC: MideaACDevice,
    DeviceType.C3: BridgeC3Device,
}

TYPE_NAMES = {DeviceType.AC: "AC", DeviceType.C3: "heat pump"}


def load_keys(path: Path) -> dict[int, tuple[str, str]]:
    """Read the midea-local cache file: {"devices": [{"device_id", "token", "key"}, ...]}."""
    data = json.loads(path.read_text())
    keys: dict[int, tuple[str, str]] = {}
    for entry in data.get("devices", []):
        try:
            keys[int(entry["device_id"])] = (str(entry["token"]), str(entry["key"]))
        except (KeyError, TypeError, ValueError):
            log.warning("Skipping malformed key entry: %s", {k: v for k, v in entry.items() if k == "device_id"})
    return keys


def discover_devices(ip_addresses: list[str] | None = None) -> dict[int, dict[str, Any]]:
    """Blocking UDP discovery; run in an executor."""
    return discover(ip_address=ip_addresses or None)


def type_name(info: dict[str, Any]) -> str:
    try:
        return TYPE_NAMES.get(DeviceType(int(info["type"])), f"type 0x{int(info['type']):02X}")
    except (KeyError, ValueError):
        return "unknown"


def build_device(info: dict[str, Any], token: str, key: str, name: str) -> MideaDevice | None:
    """Instantiate the midea-local device class for a discovery record, or None if unsupported."""
    try:
        dtype = DeviceType(int(info["type"]))
    except (KeyError, ValueError):
        return None
    cls = DEVICE_CLASSES.get(dtype)
    if cls is None:
        return None
    return cls(  # type: ignore[call-arg]
        name=name,
        device_id=int(info["device_id"]),
        ip_address=str(info["ip_address"]),
        port=int(info.get("port") or 6444),
        token=token,
        key=key,
        device_protocol=ProtocolVersion(int(info.get("protocol") or 3)),
        model=str(info.get("model") or ""),
        subtype=0,
        mac=info.get("mac"),
        serial_number=info.get("sn"),
        manufacturer_code=info.get("manufacturer_code"),
        customize="",
    )


def connect_device(device: MideaDevice) -> bool:
    """Blocking first connection + status refresh; run in an executor."""
    try:
        ok = device.connect(check_protocol=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] connect failed: %s", device.name, exc)
        return False
    if not ok:
        return False
    try:
        device.refresh_status(check_protocol=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] initial refresh failed: %s", device.name, exc)
    return True


def num(value: Any, digits: int = 1) -> float | None:
    """Round a numeric attribute, passing None through."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


class MideaEndpoint(Endpoint):
    """Endpoint bound to a midea-local device; forwards the device thread's updates to the loop."""

    manufacturer = "Midea"

    def __init__(self, device: MideaDevice, third_serial: str, name: str) -> None:
        super().__init__(third_serial, name)
        self.device = device
        self.model = device.model or ""
        self._loop = asyncio.get_running_loop()
        device.register_update(self._device_update)

    @property
    def attrs(self) -> dict[str, Any]:
        return self.device.attributes

    def _device_update(self, status: dict[str, Any]) -> None:
        # Called from the midea-local device thread; the loop may already be gone during shutdown.
        if self._loop.is_closed():
            return
        try:
            if "available" in status:
                self._loop.call_soon_threadsafe(self.set_online, bool(status["available"]))
            self._loop.call_soon_threadsafe(self.notify_state)
        except RuntimeError:  # loop closed between the check and the call
            pass

    async def set_attributes(self, sets: list[tuple[str, Any]]) -> None:
        def _apply() -> None:
            for attr, value in sets:
                log.debug("[%s] set %s = %r", self.device.name, attr, value)
                self.device.set_attribute(attr, value)

        await self._loop.run_in_executor(None, _apply)

    async def apply(self, state: dict[str, Any]) -> None:  # read-only by default
        raise ValueError(f"{self.name} is read-only")
