"""Data and registration for the heat pump card."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from bridge.cube.client import CubeClient, CubeError
from bridge.midea.common import num
from bridge.midea.heatpump import HeatPumpDhwEndpoint, HeatPumpZoneEndpoint

log = logging.getLogger(__name__)

CARD_LABEL = "Heat pump"
C3_MODES = {1: "auto", 2: "cooling", 3: "heating"}


def _start_of_local_day() -> float:
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def card_data(endpoints: dict[str, Any]) -> dict[str, Any]:
    """Snapshot for the card and drawer, built from the registered heat pump endpoints."""
    zone = next((ep for ep in endpoints.values() if isinstance(ep, HeatPumpZoneEndpoint)), None)
    if zone is None:
        return {"found": False}
    dhw = next((ep for ep in endpoints.values() if isinstance(ep, HeatPumpDhwEndpoint) and ep.device is zone.device), None)
    a = zone.attrs
    lo, hi = zone._limits()  # noqa: SLF001

    energy: dict[str, Any] = {}
    if zone.energy_log is not None:
        now = time.time()
        energy = {
            "today_kwh": round(zone.energy_log.usage(_start_of_local_day(), now), 2),
            "total_consumed_kwh": zone.energy_log.latest(),
            "total_produced_kwh": num(a.get("total_produced_energy"), 0),
        }

    data: dict[str, Any] = {
        "found": True,
        "name": zone.name.removesuffix(" zone 1").strip() or zone.name,
        "online": zone.online,
        "mode": C3_MODES.get(a.get("mode"), "heating"),
        "zone": {
            "third_serial": zone.third_serial,
            "power": bool(a.get("zone1_power")),
            "target": zone._target(),  # noqa: SLF001
            "supply": num(a.get("hp_temp_tw_out")) if a.get("hp_temp_tw_out") is not None else num(a.get("temp_tw_out")),
            "min": lo,
            "max": hi,
            "water_mode": bool((a.get("zone_temp_type") or [True])[0]),
        },
        "outdoor": num(a.get("hp_temp_t4")) if a.get("hp_temp_t4") is not None else num(a.get("outdoor_temperature")),
        "status": {
            "heating": bool(a.get("status_heating")),
            "dhw": bool(a.get("status_dhw")),
            "eco": bool(a.get("eco_mode")),
            "silent": bool(a.get("silent_mode")),
            "backup_heater": bool(a.get("status_ibh") or a.get("status_tbh")),
            "error_code": a.get("error_code") or 0,
        },
        "energy": energy,
        "updated": datetime.now().strftime("%H:%M"),
    }
    if dhw is not None:
        da = dhw.attrs
        data["dhw"] = {
            "third_serial": dhw.third_serial,
            "power": bool(da.get("dhw_power")),
            "target": num(da.get("dhw_target_temp")),
            "tank": num(da.get("tank_actual_temperature")),
            "min": num(da.get("dhw_temp_min")) or 20.0,
            "max": num(da.get("dhw_temp_max")) or 60.0,
        }
    return data


class CardRegistrar:
    """Keeps one web card per label registered on the gateway, re-creating it when URLs change."""

    def __init__(self, client: CubeClient, data_dir: Path) -> None:
        self.client = client
        self._file = data_dir / "cards.json"
        self._state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self._file.read_text())
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s: %s", self._file, exc)
            return {}

    def _save(self) -> None:
        self._file.write_text(json.dumps(self._state, indent=2))

    async def ensure(self, label: str, base_url: str, path: str, drawer_path: str) -> str | None:
        """Create the card if missing or stale. Returns the card id."""
        wanted = {
            "dimensions": [
                {"src": f"{base_url}{path}?size=2x1", "size": "2×1"},
                {"src": f"{base_url}{path}?size=1x1", "size": "1×1"},
            ],
            "drawer_component": {"src": f"{base_url}{drawer_path}"},
            "default": "2×1",
        }
        known = self._state.get(label) or {}
        try:
            existing = await self.client.list_cards()
        except CubeError as exc:
            log.warning("Could not list UI cards: %s", exc)
            return known.get("id")

        mine = [c for c in existing if c.get("label") == label and (c.get("appName") or c.get("app_name")) in (None, self.client.app_name)]
        current = next((c for c in mine if c.get("id") == known.get("id")), mine[0] if mine else None)
        if current is not None and known.get("web_settings") == wanted:
            return str(current["id"])

        for card in mine:  # stale URL or duplicate: replace
            try:
                await self.client.delete_card(str(card["id"]))
            except CubeError as exc:
                log.warning("Could not delete old card %s: %s", card.get("id"), exc)

        try:
            card_id = await self.client.create_card(label, wanted)
        except CubeError as exc:
            if "×" in json.dumps(wanted):  # some firmwares may want the ASCII spelling
                ascii_settings = json.loads(json.dumps(wanted).replace("×", "x"))
                card_id = await self.client.create_card(label, ascii_settings)
                wanted = ascii_settings
            else:
                raise exc
        self._state[label] = {"id": card_id, "web_settings": wanted}
        self._save()
        log.info("Registered UI card '%s' (%s) at %s", label, card_id, base_url)
        return card_id
