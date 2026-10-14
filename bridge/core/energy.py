"""Persistent log of a cumulative energy counter, serving the CUBE `power-consumption` capability.

Appliances only give us a lifetime kWh total. The gateway asks for usage per
interval, so we keep timestamped samples of the total and derive interval usage
by interpolating the counter at the interval boundaries.
"""

from __future__ import annotations

import json
import logging
import os
import time
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_SAMPLES = 50_000       # years of hourly samples
MIN_INTERVAL = 600.0       # seconds between samples when the counter did not change
MAX_BUCKETS = 5_000


def parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def fmt_ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_tz_offset_hours() -> int:
    offset = datetime.now().astimezone().utcoffset()
    return int(round(offset.total_seconds() / 3600)) if offset else 0


class EnergyLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.times: list[float] = []
        self.values: list[float] = []
        self.rl_start: float | None = None  # start of the gateway's "real-time summary" window
        self._load()

    # ----- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s: %s", self.path, exc)
            return
        for t, v in data.get("samples", []):
            self.times.append(float(t))
            self.values.append(float(v))
        self.rl_start = data.get("rl_start")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"samples": [[t, v] for t, v in zip(self.times, self.values)], "rl_start": self.rl_start}))
        os.replace(tmp, self.path)

    # ----- recording -----------------------------------------------------------

    def record(self, kwh: float, now: float | None = None) -> bool:
        """Store a counter reading. Returns True if a sample was added."""
        now = now if now is not None else time.time()
        if self.values:
            if kwh == self.values[-1] and now - self.times[-1] < MIN_INTERVAL:
                return False
            if kwh < self.values[-1] - 0.5:
                log.warning("%s: energy counter went backwards (%.2f -> %.2f), keeping both", self.path.stem, self.values[-1], kwh)
        self.times.append(now)
        self.values.append(float(kwh))
        if len(self.times) > MAX_SAMPLES:
            del self.times[: len(self.times) - MAX_SAMPLES]
            del self.values[: len(self.values) - MAX_SAMPLES]
        self._save()
        return True

    def set_realtime_window(self, start: float | None) -> None:
        self.rl_start = start
        self._save()

    # ----- queries ---------------------------------------------------------------

    def value_at(self, t: float) -> float | None:
        if not self.times:
            return None
        if t <= self.times[0]:
            return self.values[0]
        if t >= self.times[-1]:
            return self.values[-1]
        i = bisect_left(self.times, t)
        t0, t1 = self.times[i - 1], self.times[i]
        v0, v1 = self.values[i - 1], self.values[i]
        if t1 == t0:
            return v1
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0)

    def usage(self, start: float, end: float) -> float:
        a, b = self.value_at(start), self.value_at(end)
        if a is None or b is None:
            return 0.0
        return max(0.0, b - a)

    def intervals(self, start: float, end: float, resolution: float) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        t = start
        while t < end and len(out) < MAX_BUCKETS:
            t_next = min(t + resolution, end)
            out.append({"usage": round(self.usage(t, t_next) * 100, 2), "start": fmt_ts(t), "end": fmt_ts(t_next)})
            t = t_next
        return out

    def latest(self) -> float | None:
        return self.values[-1] if self.values else None
