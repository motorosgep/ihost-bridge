"""Room control: switch the heat pump's space heating on and off from indoor sensors.

For a well-insulated house on radiators, where even the lowest water temperature
overheats the rooms, the heat pump runs at its fixed water temperature and this
switches its heating zone (zone 1; hot water is left alone) on and off like a room
thermostat:

- the room temperature is the average of the chosen hub sensors that reported in
  the last half hour (a sensor that went quiet is left out);
- heating goes on at or below `target - below` and off at or above `target + above`
  (a gap, so it does not flick on and off around the target);
- once switched, the heat pump keeps that state for at least `min_run` (on) or
  `min_rest` (off), to spare the compressor;
- with no fresh reading at all it switches heating on and says so: the house may
  get too warm, as before, but never cold.

Off by default; Butter turns it on and sets the target and sensors. Settings live
in <data dir>/room_control.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TICK = 60
STALE_AFTER = 30 * 60
KEEP_LOG = 50

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "target": 22.0,
    "below": 0.3,        # heating on at target - below
    "above": 0.2,        # heating off at target + above
    "min_run": 30 * 60,
    "min_rest": 20 * 60,
    "sensors": [],       # hub serial numbers
}
LIMITS = {"target": (10.0, 28.0), "below": (0.1, 2.0), "above": (0.0, 2.0), "min_run": (0, 3 * 3600), "min_rest": (0, 3 * 3600)}


def reading(device: dict[str, Any]) -> tuple[float | None, float | None]:
    """A hub device's temperature and when it was last reported (epoch s), if it says."""
    t = (device.get("state") or {}).get("temperature") or {}
    value = t.get("temperature")
    at = t.get("updated_at")
    if isinstance(at, (int, float)) and at > 0:
        at = at / 1000 if at > 1e12 else float(at)
    else:
        at = None
    return (float(value) if isinstance(value, (int, float)) else None), at


class RoomControl:
    def __init__(
        self,
        data_dir: Path,
        list_devices: Callable[[], Awaitable[list[dict[str, Any]]]],
        zone: Callable[[], Any],
    ) -> None:
        """`list_devices` reads the hub's devices; `zone` returns the heat pump zone 1 endpoint, or None."""
        self.file = data_dir / "room_control.json"
        self._list_devices = list_devices
        self._zone = zone
        self.settings: dict[str, Any] = dict(DEFAULTS)
        self.last_switch = 0.0          # when this switched the zone last (epoch s)
        self.status: dict[str, Any] = {"room": None, "sensors": [], "heating": None, "reason": "not started"}
        self.log: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._load()

    # ----- settings -------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.file.read_text())
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s: %s", self.file, exc)
            return
        for key in DEFAULTS:
            if key in data:
                self.settings[key] = data[key]
        self.last_switch = float(data.get("last_switch") or 0)

    def _save(self) -> None:
        self.file.write_text(json.dumps({**self.settings, "last_switch": self.last_switch}, indent=2))

    def update(self, body: dict[str, Any]) -> dict[str, Any]:
        """Change settings from an app. Unknown keys are refused; numbers are checked against LIMITS."""
        new = dict(self.settings)
        for key, value in body.items():
            if key not in DEFAULTS:
                raise ValueError(f"unknown setting: {key}")
            if key == "enabled":
                if not isinstance(value, bool):
                    raise ValueError("enabled must be true or false")
            elif key == "sensors":
                if not isinstance(value, list) or not all(isinstance(s, str) and s for s in value) or len(value) > 8:
                    raise ValueError("sensors must be a list of up to 8 device serial numbers")
            else:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{key} must be a number")
                lo, hi = LIMITS[key]
                if not lo <= value <= hi:
                    raise ValueError(f"{key} must be between {lo} and {hi}")
            new[key] = value
        if new["enabled"] and not new["sensors"]:
            raise ValueError("choose at least one sensor before switching room control on")
        self.settings = new
        self._save()
        log.info("Room control settings: %s", {k: v for k, v in new.items() if k != "sensors"} | {"sensors": len(new["sensors"])})
        return self.settings

    async def change(self, body: dict[str, Any]) -> dict[str, Any]:
        """Apply new settings and act on them now. Switching room control off hands heating back on,
        so the heat pump is never left off by a control that no longer watches the rooms."""
        async with self._lock:
            was_on = self.settings["enabled"]
            self.update(body)
            if was_on and not self.settings["enabled"]:
                zone = self._zone()
                if zone is not None and self._zone_on(zone) is False:
                    try:
                        await zone.apply({"power": {"powerState": "on"}})
                        log.info("Room control off: heating handed back on")
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Room control off: could not switch heating back on: %s", exc)
            await self._tick(time.time())
        return self.view()

    # ----- control loop ---------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001
                log.exception("room control tick failed")
            try:
                await asyncio.wait_for(stop.wait(), TICK)
            except asyncio.TimeoutError:
                pass

    async def tick(self, now: float | None = None) -> None:
        async with self._lock:
            await self._tick(now or time.time())

    async def _tick(self, now: float) -> None:
        s = self.settings
        zone = self._zone()
        heating = self._zone_on(zone)
        sensors, room = await self._room(now)
        self.status = {"room": room, "sensors": sensors, "heating": heating, "reason": "", "checked_at": round(now)}
        if not s["enabled"]:
            self.status["reason"] = "Room control is off; the heat pump follows its own settings."
            return
        if zone is None or not getattr(zone, "online", False):
            self.status["reason"] = "The heat pump is not reachable."
            return

        on_at, off_at = s["target"] - s["below"], s["target"] + s["above"]
        if room is None:
            want, why = True, "No indoor sensor has reported in the last half hour, so heating stays on (the heat pump's own setting)."
        elif room <= on_at:
            want, why = True, f"{room:.1f}° is at or below {on_at:.1f}°."
        elif room >= off_at:
            want, why = False, f"{room:.1f}° is at or above {off_at:.1f}°."
        else:
            want, why = heating, f"{room:.1f}° is within {on_at:.1f}–{off_at:.1f}°, so heating stays {'on' if heating else 'off'}."

        if want == heating:
            self.status["reason"] = why
            return
        held = now - self.last_switch
        hold = s["min_run"] if heating else s["min_rest"]
        if self.last_switch and held < hold:
            wait = int((hold - held) // 60) + 1
            self.status["reason"] = f"{why} Switching {'off' if heating else 'on'} in {wait} min (heating {'runs' if heating else 'rests'} at least {hold // 60} min)."
            self.status["next_switch_at"] = round(self.last_switch + hold)
            return
        try:
            await zone.apply({"power": {"powerState": "on" if want else "off"}})
        except Exception as exc:  # noqa: BLE001
            self.status["reason"] = f"Could not switch the heat pump: {exc}"
            log.warning("Room control: could not switch heating %s: %s", "on" if want else "off", exc)
            return
        self.last_switch = now
        self._save()
        self.status["heating"] = want
        self.status["reason"] = f"Heating switched {'on' if want else 'off'}: {why}"
        self.log.append({"at": round(now), "on": want, "room": room})
        self.log = self.log[-KEEP_LOG:]
        log.info("Room control: heating %s (%s)", "on" if want else "off", why)

    @staticmethod
    def _zone_on(zone: Any) -> bool | None:
        if zone is None:
            return None
        try:
            return zone.state()["power"]["powerState"] == "on"
        except Exception:  # noqa: BLE001
            return None

    async def _room(self, now: float) -> tuple[list[dict[str, Any]], float | None]:
        chosen = self.settings["sensors"]
        try:
            devices = await self._list_devices()
        except Exception as exc:  # noqa: BLE001
            log.warning("Room control: could not read the hub's devices: %s", exc)
            devices = []
        by_serial = {d.get("serial_number"): d for d in devices}
        out = []
        for serial in chosen:
            d = by_serial.get(serial)
            value, at = reading(d) if d else (None, None)
            fresh = bool(d and d.get("online", True) and value is not None and (at is None or now - at <= STALE_AFTER))
            out.append({"serial": serial, "name": (d or {}).get("name") or serial, "value": value, "at": round(at) if at else None, "used": fresh})
        used = [o["value"] for o in out if o["used"]]
        return out, (round(sum(used) / len(used), 2) if used else None)

    def candidates(self, devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Every hub device with a temperature that is not one of the bridge's own (for the sensor picker)."""
        out = []
        for d in devices:
            value, at = reading(d)
            if value is None or d.get("third_serial_number"):
                continue
            out.append({"serial": d.get("serial_number"), "name": d.get("name"), "value": value, "at": round(at) if at else None, "online": d.get("online", True)})
        return sorted(out, key=lambda o: str(o["name"]))

    def view(self) -> dict[str, Any]:
        s = self.settings
        return {
            "settings": s,
            "on_at": round(s["target"] - s["below"], 2),
            "off_at": round(s["target"] + s["above"], 2),
            "last_switch": round(self.last_switch) or None,
            **self.status,
            "log": self.log,
        }
