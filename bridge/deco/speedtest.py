"""Internet speed test, run from the hub against Cloudflare's speed test endpoints.

The Deco's local API has no speed test (its app runs one through TP-Link's cloud),
so the bridge measures for itself: round-trip time, then a download and an upload
sized to the line (a small transfer first, a bigger one when the small one was
over too quickly to time). Every six hours, and when an app asks, at most once
every two minutes. Results are kept in <data dir>/speedtest.json, so the last one
survives a restart and a restart does not start a new test.

It measures the hub's own connection, so a hub on Wi-Fi or with a slow CPU reads
lower than the line can do; the figures are for "is it as usual?", not for
arguing with the provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

BASE = "https://speed.cloudflare.com"
INTERVAL = 6 * 3600
MIN_GAP = 120
KEEP = 28                     # a week of results at four a day
ENOUGH_SECONDS = 2.0          # a transfer shorter than this is too quick to time well
DOWN_SIZES = (10_000_000, 50_000_000)
UP_SIZES = (5_000_000, 20_000_000)
TIMEOUT = aiohttp.ClientTimeout(total=60)


def _mbps(size: int, seconds: float) -> float:
    return round(size * 8 / seconds / 1_000_000, 1) if seconds > 0 else 0.0


class SpeedTest:
    def __init__(self, data_dir: Path) -> None:
        self.file = data_dir / "speedtest.json"
        self.history: list[dict[str, Any]] = []
        self.running = False
        self._task: asyncio.Task[dict[str, Any]] | None = None
        self._load()

    @property
    def last(self) -> dict[str, Any] | None:
        return self.history[-1] if self.history else None

    def _load(self) -> None:
        try:
            data = json.loads(self.file.read_text())
            self.history = [r for r in data.get("history", []) if isinstance(r, dict)][-KEEP:]
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s: %s", self.file, exc)

    def _save(self) -> None:
        self.file.write_text(json.dumps({"history": self.history[-KEEP:]}))

    def due(self) -> bool:
        return not self.running and (self.last is None or time.time() - self.last.get("at", 0) >= INTERVAL)

    def wait_left(self) -> int:
        """Seconds until a test may be asked for again (0: now)."""
        if self.last is None:
            return 0
        return max(0, int(MIN_GAP - (time.time() - self.last.get("at", 0))))

    def start(self) -> asyncio.Task[dict[str, Any]]:
        """Start a test, or hand back the one already running."""
        if self._task is None or self._task.done():
            self.running = True
            self._task = asyncio.create_task(self._run())
        return self._task

    async def _run(self) -> dict[str, Any]:
        result: dict[str, Any] = {"at": time.time()}
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                result.update(await self.measure(session))
            log.info("Speed test: %.1f down, %.1f up Mbit/s, %.0f ms", result["down_mbps"], result["up_mbps"], result["ping_ms"])
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc) or type(exc).__name__
            log.warning("Speed test failed: %s", result["error"])
        finally:
            self.running = False
        result["at"] = round(result["at"])
        self.history.append(result)
        self.history = self.history[-KEEP:]
        try:
            self._save()
        except OSError as exc:
            log.warning("Could not save %s: %s", self.file, exc)
        return result

    async def measure(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        pings = []
        colo = None
        for _ in range(5):
            t0 = time.perf_counter()
            async with session.get(f"{BASE}/__down", params={"bytes": "0"}) as resp:
                resp.raise_for_status()
                await resp.read()
                colo = resp.headers.get("cf-meta-colo") or colo
            pings.append((time.perf_counter() - t0) * 1000)
        down = 0.0
        for size in DOWN_SIZES:
            seconds = await self._download(session, size)
            down = _mbps(size, seconds)
            if seconds >= ENOUGH_SECONDS:
                break
        up = 0.0
        for size in UP_SIZES:
            seconds = await self._upload(session, size)
            up = _mbps(size, seconds)
            if seconds >= ENOUGH_SECONDS:
                break
        return {"ping_ms": round(statistics.median(pings)), "down_mbps": down, "up_mbps": up, "server": f"Cloudflare {colo}" if colo else "Cloudflare"}

    @staticmethod
    async def _download(session: aiohttp.ClientSession, size: int) -> float:
        async with session.get(f"{BASE}/__down", params={"bytes": str(size)}) as resp:
            resp.raise_for_status()
            got = 0
            t0 = None
            async for chunk in resp.content.iter_chunked(1 << 16):
                if t0 is None:
                    t0 = time.perf_counter()  # from the first byte, so the round trip is not counted
                got += len(chunk)
            if t0 is None or got < size * 0.9:
                raise RuntimeError(f"download ended early ({got} of {size} bytes)")
            return time.perf_counter() - t0

    @staticmethod
    async def _upload(session: aiohttp.ClientSession, size: int) -> float:
        body = bytes(size)
        t0 = time.perf_counter()
        async with session.post(f"{BASE}/__up", data=body, headers={"Content-Type": "application/octet-stream"}) as resp:
            resp.raise_for_status()
            await resp.read()
        return time.perf_counter() - t0

    def status(self) -> dict[str, Any]:
        return {"last": self.last, "history": self.history, "running": self.running, "every_hours": INTERVAL // 3600}
