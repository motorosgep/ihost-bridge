"""History recorder: keeps numeric device readings from the gateway in SQLite.

The gateway stores sensor history for its own console but does not expose it
through the Open API, so this module records every numeric reading it sees on
the gateway's event stream (plus a periodic snapshot of all devices) and serves
time-bucketed queries for apps such as Butter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from bridge.cube.client import CubeClient

log = logging.getLogger(__name__)

STATE_EVENT = "device#v2#updateDeviceState"
MIN_INTERVAL = 600          # seconds; identical consecutive values are stored at most this often
RAW_LIMIT = 5000            # most samples returned by a raw (unbucketed) query
MAX_BUCKETS = 2000
SKIP_KEYS = {"updated_at"}
ON_OFF = {"on": 1.0, "off": 0.0}

EnergyLookup = Callable[[str], Any]  # hub serial -> EnergyLog | None


def parse_time(value: str | None, default: float) -> float:
    """Epoch seconds from an ISO-8601 string or a number; `default` when empty."""
    if value in (None, ""):
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return default
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def flatten(state: dict[str, Any]) -> list[tuple[str, float, float | None]]:
    """Numeric leaves of a CUBE state fragment as (metric, value, updated_at seconds).

    `temperature.temperature`, `electric-power.electricPower`,
    `thermostat-target-setpoint.manual-mode.targetSetpoint`; on/off states become 1/0.
    """
    out: list[tuple[str, float, float | None]] = []

    def leaf(prefix: str, key: str, value: Any, updated: float | None) -> None:
        if key in SKIP_KEYS:
            return
        if isinstance(value, bool):
            out.append((f"{prefix}.{key}", 1.0 if value else 0.0, updated))
        elif isinstance(value, (int, float)):
            out.append((f"{prefix}.{key}", float(value), updated))
        elif isinstance(value, str) and key.endswith("State") and value in ON_OFF:
            out.append((f"{prefix}.{key}", ON_OFF[value], updated))

    for cap, block in state.items():
        if not isinstance(block, dict):
            continue
        updated = block.get("updated_at")
        updated_s = float(updated) / 1000 if isinstance(updated, (int, float)) and updated > 1e11 else None
        for key, value in block.items():
            if isinstance(value, dict):
                inner_updated = value.get("updated_at")
                inner_s = float(inner_updated) / 1000 if isinstance(inner_updated, (int, float)) and inner_updated > 1e11 else updated_s
                for k2, v2 in value.items():
                    leaf(f"{cap}.{key}", k2, v2, inner_s)
            else:
                leaf(cap, key, value, updated_s)
    return out


class Recorder:
    def __init__(
        self,
        client: CubeClient,
        session: aiohttp.ClientSession,
        db_path: Path,
        snapshot_interval: float = 900.0,
        retention_days: int = 400,
        energy_lookup: EnergyLookup | None = None,
    ) -> None:
        self._client = client
        self._session = session
        self._db_path = db_path
        self.snapshot_interval = snapshot_interval
        self.retention_days = retention_days
        self._energy_lookup = energy_lookup
        self._last: dict[tuple[str, str], tuple[float, float]] = {}
        self.stream_state = "starting"
        self.last_event: float | None = None
        self.last_snapshot: float | None = None
        self.started = time.time()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS samples (serial TEXT NOT NULL, metric TEXT NOT NULL, ts INTEGER NOT NULL, value REAL NOT NULL)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS samples_idx ON samples (serial, metric, ts)")
        self._db.commit()

    # ----- recording ----------------------------------------------------------

    def record_state(self, serial: str, state: dict[str, Any], now: float | None = None) -> int:
        now = now or time.time()
        stored = 0
        for metric, value, updated in flatten(state):
            stored += self.record_value(serial, metric, value, updated or now)
        if stored:
            self._db.commit()
        return stored

    def record_value(self, serial: str, metric: str, value: float, ts: float) -> int:
        key = (serial, metric)
        last = self._last.get(key)
        if last is None:
            row = self._db.execute(
                "SELECT ts, value FROM samples WHERE serial=? AND metric=? ORDER BY ts DESC LIMIT 1", key
            ).fetchone()
            last = (float(row[0]), float(row[1])) if row else None
        if last is not None:
            last_ts, last_value = last
            if ts <= last_ts and value == last_value:
                return 0
            if value == last_value and ts - last_ts < MIN_INTERVAL:
                return 0
        self._db.execute("INSERT INTO samples (serial, metric, ts, value) VALUES (?, ?, ?, ?)", (serial, metric, int(ts), value))
        self._last[key] = (ts, value)
        return 1

    def prune(self) -> int:
        cutoff = int(time.time() - self.retention_days * 86400)
        cur = self._db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self._db.commit()
        return cur.rowcount

    # ----- queries ---------------------------------------------------------------

    def info(self) -> dict[str, Any]:
        from bridge import __version__

        row = self._db.execute("SELECT COUNT(*), COUNT(DISTINCT serial), MIN(ts), MAX(ts) FROM samples").fetchone()
        return {
            "recorder": True,
            "version": __version__,
            "samples": row[0],
            "devices": row[1],
            "oldest": row[2],
            "newest": row[3],
            "stream": self.stream_state,
            "last_event": self.last_event,
            "last_snapshot": self.last_snapshot,
            "snapshot_interval": self.snapshot_interval,
            "retention_days": self.retention_days,
        }

    def metrics(self, serial: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT metric, COUNT(*), MIN(ts), MAX(ts) FROM samples WHERE serial=? GROUP BY metric ORDER BY metric", (serial,)
        ).fetchall()
        out = []
        for metric, count, first, last in rows:
            latest = self._db.execute(
                "SELECT value FROM samples WHERE serial=? AND metric=? ORDER BY ts DESC LIMIT 1", (serial, metric)
            ).fetchone()
            out.append({"metric": metric, "count": count, "first": first, "last": last, "last_value": latest[0] if latest else None})
        return out

    def samples(self, serial: str, metric: str, start: float, end: float, resolution: float) -> dict[str, Any]:
        base = {"serial": serial, "metric": metric, "from": int(start), "to": int(end), "resolution": int(resolution)}
        prev = self._db.execute(
            "SELECT ts, value FROM samples WHERE serial=? AND metric=? AND ts<? ORDER BY ts DESC LIMIT 1", (serial, metric, int(start))
        ).fetchone()
        base["before"] = {"t": prev[0], "v": prev[1]} if prev else None
        if resolution <= 0:
            rows = self._db.execute(
                "SELECT ts, value FROM samples WHERE serial=? AND metric=? AND ts>=? AND ts<=? ORDER BY ts LIMIT ?",
                (serial, metric, int(start), int(end), RAW_LIMIT),
            ).fetchall()
            base["points"] = [{"t": t, "v": v} for t, v in rows]
            return base
        resolution = max(resolution, (end - start) / MAX_BUCKETS)
        rows = self._db.execute(
            "SELECT (ts / ?) * ? AS bucket, AVG(value), MIN(value), MAX(value), COUNT(*) FROM samples "
            "WHERE serial=? AND metric=? AND ts>=? AND ts<=? GROUP BY bucket ORDER BY bucket",
            (int(resolution), int(resolution), serial, metric, int(start), int(end)),
        ).fetchall()
        base["resolution"] = int(resolution)
        base["points"] = [{"t": b, "avg": round(avg, 3), "min": mn, "max": mx, "n": n} for b, avg, mn, mx, n in rows]
        return base

    def energy(self, serial: str, start: float, end: float, resolution: float) -> dict[str, Any]:
        """kWh per interval: from the bridge's own counter log when it has one, else integrated from power samples."""
        resolution = max(resolution, 60.0, (end - start) / MAX_BUCKETS)
        elog = self._energy_lookup(serial) if self._energy_lookup else None
        intervals: list[dict[str, Any]] = []
        if elog is not None:
            t = start
            while t < end:
                t_next = min(t + resolution, end)
                intervals.append({"start": int(t), "end": int(t_next), "kwh": round(elog.usage(t, t_next), 4)})
                t = t_next
            source = "counter"
        else:
            intervals = self._integrate_power(serial, start, end, resolution)
            source = "power"
        return {
            "serial": serial,
            "from": int(start),
            "to": int(end),
            "resolution": int(resolution),
            "source": source,
            "intervals": intervals,
            "total_kwh": round(sum(i["kwh"] for i in intervals), 4),
        }

    def _integrate_power(self, serial: str, start: float, end: float, resolution: float) -> list[dict[str, Any]]:
        # gateway unit: 0.01 W; the attribute is `electricPower` on gateway devices and may be
        # named differently by third-party bridges, so take whatever the capability reports
        row = self._db.execute(
            "SELECT metric FROM samples WHERE serial=? AND metric LIKE 'electric-power.%' ORDER BY ts DESC LIMIT 1", (serial,)
        ).fetchone()
        if not row:
            return []
        metric = row[0]
        prev = self._db.execute(
            "SELECT ts, value FROM samples WHERE serial=? AND metric=? AND ts<? ORDER BY ts DESC LIMIT 1", (serial, metric, int(start))
        ).fetchone()
        rows = self._db.execute(
            "SELECT ts, value FROM samples WHERE serial=? AND metric=? AND ts>=? AND ts<=? ORDER BY ts", (serial, metric, int(start), int(end))
        ).fetchall()
        points = ([(float(prev[0]), float(prev[1]))] if prev else []) + [(float(t), float(v)) for t, v in rows]
        if not points:
            return []
        out: list[dict[str, Any]] = []
        t = start
        while t < end:
            t_next = min(t + resolution, end)
            wh = 0.0
            # hold each sample's value until the next sample (step integration)
            for i, (ts, value) in enumerate(points):
                seg_start = max(ts, t)
                seg_end = min(points[i + 1][0] if i + 1 < len(points) else end, t_next)
                if seg_end > seg_start:
                    wh += (value / 100.0) * (seg_end - seg_start) / 3600.0
            out.append({"start": int(t), "end": int(t_next), "kwh": round(wh / 1000.0, 4)})
            t = t_next
        return out

    # ----- event stream ------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 2.0
        next_prune = 0.0
        while not stop.is_set():
            if not (self._client.token and self._client.token_valid):
                self.stream_state = "waiting for token"
                await _sleep(stop, 5)
                continue
            try:
                await self._snapshot()
                self.stream_state = "connecting"
                await self._stream(stop)
                backoff = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.stream_state = f"reconnecting ({exc})"
                log.warning("History stream lost: %s; retrying in %.0fs", exc, backoff)
                await _sleep(stop, backoff)
                backoff = min(backoff * 2, 120)
            if time.time() > next_prune:
                removed = self.prune()
                if removed:
                    log.info("Pruned %d history samples older than %d days", removed, self.retention_days)
                next_prune = time.time() + 86400

    async def _snapshot(self) -> None:
        devices = await self._client.list_devices()
        now = time.time()
        stored = 0
        for dev in devices:
            serial = dev.get("serial_number")
            state = dev.get("state") or {}
            if not serial or not isinstance(state, dict):
                continue
            stored += self.record_state(serial, state, now)
            # keep flat lines alive: a point "now" when the last stored one is older than the snapshot interval
            for metric, value, _ in flatten(state):
                last = self._last.get((serial, metric))
                if last and now - last[0] >= self.snapshot_interval:
                    stored += self.record_value(serial, metric, value, now)
        self._db.commit()
        self.last_snapshot = now
        log.debug("History snapshot: %d device(s), %d new sample(s)", len(devices), stored)

    async def _stream(self, stop: asyncio.Event) -> None:
        url = f"http://{self._client.host}/open-api/v2/sse/bridge"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=max(180.0, self.snapshot_interval + 60))
        snapshot_due = time.time() + self.snapshot_interval
        async with self._session.get(url, params={"access_token": self._client.token}, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"event stream answered HTTP {resp.status}")
            self.stream_state = "connected"
            log.info("History recorder listening to the gateway event stream")
            event: str | None = None
            data: list[str] = []
            async for raw in resp.content:
                if stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line == "":
                    if data:
                        self._dispatch(event, "\n".join(data))
                    event, data = None, []
                elif line.startswith(":"):
                    pass
                elif line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
                if time.time() >= snapshot_due:
                    snapshot_due = time.time() + self.snapshot_interval
                    try:
                        await self._snapshot()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("History snapshot failed: %s", exc)
        raise RuntimeError("event stream ended")

    def _dispatch(self, event: str | None, payload: str) -> None:
        self.last_event = time.time()
        if event != STATE_EVENT:
            return
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            return
        endpoint = body.get("endpoint") or {}
        serial = endpoint.get("serial_number")
        state = body.get("payload")
        if serial and isinstance(state, dict):
            self.record_state(serial, state)


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except asyncio.TimeoutError:
        pass
