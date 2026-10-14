"""Midea adapters built on the midea-local library (LAN protocol, no cloud at runtime)."""

from __future__ import annotations

from midealocal.const import DeviceType
from midealocal.device import MideaDevice

from bridge.core.endpoint import Endpoint
from bridge.midea.ac import make_ac_endpoints
from bridge.midea.heatpump import make_heatpump_endpoints


def make_endpoints(device: MideaDevice, base_name: str, existing: set[str] | None = None) -> list[Endpoint]:
    """Create the gateway endpoints for one connected Midea appliance.

    With `existing` (third serials already registered) only the endpoints that
    became possible since are returned, so this can be called on every update.
    """
    if device.device_type == DeviceType.AC:
        return make_ac_endpoints(device, base_name, existing)
    if device.device_type == DeviceType.C3:
        return make_heatpump_endpoints(device, base_name, existing)
    return []
