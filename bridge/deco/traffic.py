"""Home internet traffic, from the per-device rates the Deco reports.

The Deco gives each client's current down and up rate in kB/s (it refreshes them
every minute or two, so a burst shows up late and short ones may be missed).
Each mesh read is recorded here:

- the home's total rate, one sample per read, for the last 24 hours;
- hourly averages and peaks for the last 30 days;
- per device, the data moved today and yesterday, estimated from the rates
  (rate times the time until the next read).

Kept in <data dir>/traffic.json, written at most every five minutes.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DAY = 24 * 3600
KEEP_HOURS = 30 * 24
SAVE_EVERY = 300
MAX_GAP = 180          # a read further from the last than this counts for this long only (bridge was down)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


class TrafficLog:
    def __init__(self, data_dir: Path) -> None:
        self.file = data_dir / "traffic.json"
        # [epoch s, down kB/s, up kB/s]
        self.samples: deque[list[float]] = deque()
        # [hour start, down avg, up avg, down peak, up peak, samples]
        self.hours: list[list[float]] = []
        # day -> mac -> {"name", "deco", "down", "up"} in kB
        self.days: dict[str, dict[str, dict[str, Any]]] = {}
        self.now: dict[str, Any] = {"at": None, "down": 0, "up": 0, "decos": {}}
        self._last_at = 0.0
        self._saved_at = 0.0
        self._load()

    # ----- persistence ----------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.file.read_text())
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s: %s", self.file, exc)
            return
        cutoff = time.time() - DAY
        self.samples = deque(s for s in data.get("samples", []) if s[0] >= cutoff)
        self.hours = data.get("hours", [])[-KEEP_HOURS:]
        self.days = {k: v for k, v in data.get("days", {}).items() if isinstance(v, dict)}
        if self.samples:
            self._last_at = self.samples[-1][0]

    def save(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._saved_at < SAVE_EVERY:
            return
        self._saved_at = now
        try:
            self.file.write_text(json.dumps({"samples": list(self.samples), "hours": self.hours, "days": self.days}))
        except OSError as exc:
            log.warning("Could not save %s: %s", self.file, exc)

    # ----- recording ------------------------------------------------------------------------

    def record(self, clients: list[dict[str, Any]], at: float | None = None) -> None:
        """One mesh read: `clients` are the online ones, with mac, name, deco, down_speed and up_speed (kB/s)."""
        at = at or time.time()
        down = sum(max(0, int(c.get("down_speed") or 0)) for c in clients)
        up = sum(max(0, int(c.get("up_speed") or 0)) for c in clients)
        decos: dict[str, dict[str, int]] = {}
        for c in clients:
            d = decos.setdefault(c.get("deco") or "Other", {"down": 0, "up": 0, "devices": 0})
            d["down"] += max(0, int(c.get("down_speed") or 0))
            d["up"] += max(0, int(c.get("up_speed") or 0))
            d["devices"] += 1
        self.now = {"at": round(at), "down": down, "up": up, "decos": decos}

        # Volume: each device's rate holds until this read, at most MAX_GAP.
        span = min(MAX_GAP, at - self._last_at) if self._last_at else 60.0
        if span > 0:
            today = self.days.setdefault(_day(at), {})
            for c in clients:
                rd, ru = max(0, int(c.get("down_speed") or 0)), max(0, int(c.get("up_speed") or 0))
                if not rd and not ru:
                    continue
                entry = today.setdefault(c["mac"], {"name": "", "deco": None, "down": 0.0, "up": 0.0})
                entry["name"] = c.get("known_as") or c.get("name") or entry["name"]
                entry["deco"] = c.get("deco") or entry["deco"]
                entry["down"] = round(entry["down"] + rd * span, 1)
                entry["up"] = round(entry["up"] + ru * span, 1)
        for day in sorted(self.days)[:-2]:  # today and yesterday only
            del self.days[day]
        self._last_at = at

        self.samples.append([round(at), down, up])
        while self.samples and self.samples[0][0] < at - DAY:
            self.samples.popleft()

        hour = int(at // 3600 * 3600)
        if not self.hours or self.hours[-1][0] != hour:
            self.hours.append([hour, 0.0, 0.0, 0, 0, 0])
            self.hours = self.hours[-KEEP_HOURS:]
        h = self.hours[-1]
        n = h[5]
        h[1] = round((h[1] * n + down) / (n + 1), 1)
        h[2] = round((h[2] * n + up) / (n + 1), 1)
        h[3] = max(h[3], down)
        h[4] = max(h[4], up)
        h[5] = n + 1
        self.save()

    # ----- reading --------------------------------------------------------------------------

    def _day_view(self, day: str) -> dict[str, Any]:
        entries = self.days.get(day, {})
        devices = sorted(
            ({"mac": mac, **e} for mac, e in entries.items()),
            key=lambda e: -(e["down"] + e["up"]),
        )
        return {
            "day": day,
            "down_kb": round(sum(e["down"] for e in devices)),
            "up_kb": round(sum(e["up"] for e in devices)),
            "devices": [{**e, "down": round(e["down"]), "up": round(e["up"])} for e in devices],
        }

    def view(self) -> dict[str, Any]:
        now = time.time()
        return {
            "unit": "kB/s",
            "now": self.now,
            "samples": list(self.samples),
            "hours": [h[:5] for h in self.hours],
            "today": self._day_view(_day(now)),
            "yesterday": self._day_view(_day(now - DAY)),
        }
