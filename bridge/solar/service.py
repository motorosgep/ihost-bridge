"""Runs the solar sources: polls the kiosk, backfills history from the API, and
presents the plant to the gateway as a sensor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from bridge.config import SolarConfig
from bridge.core.endpoint import Endpoint, cap
from bridge.solar import kiosk
from bridge.solar.kiosk import KioskError, KioskLink, Snapshot
from bridge.solar.ledger import Ledger
from bridge.solar.northbound import Northbound, NorthboundError, api_host_for, normalise_host

log = logging.getLogger(__name__)

KIOSK_INTERVAL = 300        # the plant reports every five minutes
STALE_AFTER = 1800          # no good kiosk answer for this long: the device goes offline
# Huawei allows only a few history calls a day, so they are spread out.
API_STEP = 600              # one history call at most this often
RUNNING_TTL = 6 * 3600      # periods still running are asked for again this often
SETTLE = 36 * 3600          # a finished period may be corrected for a while; after that it is final
BUSY_BACKOFF = 2 * 3600
ERROR_BACKOFF = 1800


class SolarEndpoint(Endpoint):
    """The plant as a gateway sensor: output now, plus the lifetime counter as energy statistics."""

    display_category = "sensor"
    manufacturer = "Huawei"
    model = "FusionSolar"

    def __init__(self, third_serial: str, name: str) -> None:
        super().__init__(third_serial, name)
        self.snapshot: Snapshot | None = None
        self.online = False

    def capabilities(self) -> list[dict[str, Any]]:
        return [cap("electric-power", "0100")]

    def state(self) -> dict[str, Any]:
        kw = self.snapshot.power_kw if self.snapshot and self.snapshot.power_kw is not None else 0.0
        return {"electric-power": {"electric-power": int(round(kw * 1000 * 100))}}  # 0.01 W units

    def energy_counter_kwh(self) -> float | None:
        return self.snapshot.lifetime_kwh if self.snapshot else None

    async def apply(self, state: dict[str, Any]) -> None:
        raise ValueError("the solar plant is read-only")

    def update(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.set_online(True)
        self.notify_state()


class SolarService:
    def __init__(self, session: aiohttp.ClientSession, data_dir: Path, cfg: SolarConfig) -> None:
        self.session = session
        self.data_dir = data_dir
        self.cfg = cfg
        self.settings_file = data_dir / "solar.json"
        self.ledger = Ledger(data_dir / "solar.sqlite")
        self.endpoint: SolarEndpoint | None = None
        self.snapshot: Snapshot | None = None
        self.kiosk_error: str | None = None
        self.api_error: str | None = None
        self.api_plant: tuple[str, str] | None = None  # (code, name) the history is read for
        self._link: KioskLink | None = None
        self._api: Northbound | None = None
        self._station_code: str | None = None
        self._last_kiosk = 0.0
        self._last_good_kiosk = 0.0
        self._next_api = 0.0
        self._lock = asyncio.Lock()
        # Called when the endpoint appears, changes or first gets its energy counter, so the app can register it.
        self.on_endpoint_change: Callable[[], Awaitable[None]] | None = None
        self._load_settings()

    # ----- settings -----------------------------------------------------------

    def _saved(self) -> dict[str, Any]:
        try:
            data = json.loads(self.settings_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _load_settings(self) -> None:
        """Settings saved from the web UI win over config.yaml and the environment."""
        saved = self._saved()
        api = saved.get("api") or {}
        self._link = kiosk.parse_link(saved.get("kiosk_url") if "kiosk_url" in saved else self.cfg.kiosk_url)
        user = api.get("user_name") if "api" in saved else self.cfg.api_user
        code = api.get("system_code") if "api" in saved else self.cfg.api_code
        host = normalise_host(api.get("host") if "api" in saved else self.cfg.api_host) or api_host_for(self._link.host if self._link else None)
        self._station_code = (api.get("station_code") if "api" in saved else self.cfg.station_code) or None
        self._api = Northbound(self.session, host, str(user), str(code)) if user and code else None
        self.api_plant = None
        self.api_error = None
        self.kiosk_error = None
        self._last_kiosk = self._next_api = 0.0
        if self._link is None:
            self.snapshot = None

    def save_settings(self, body: dict[str, Any]) -> None:
        """Store what the setup page sent. An empty kiosk link or user name clears that source; a
        blank system code keeps the one already stored, so the page never has to show it."""
        saved = self._saved()
        if "kiosk_url" in body:
            text = str(body.get("kiosk_url") or "").strip()
            if text and kiosk.parse_link(text) is None:
                raise ValueError("that is not a FusionSolar kiosk link (…fusionsolar.huawei.com/…kiosk?kk=…)")
            saved["kiosk_url"] = text or None
        if "api" in body:
            api = body.get("api") or {}
            user = str(api.get("user_name") or "").strip()
            code = str(api.get("system_code") or "").strip() or ((saved.get("api") or {}).get("system_code") if user else None) or self.cfg.api_code
            host = str(api.get("host") or "").strip()
            if host and normalise_host(host) is None:
                raise ValueError("the API host should look like eu5.fusionsolar.huawei.com")
            if user and not code:
                raise ValueError("the system code is needed with the API user name")
            previous = saved.get("api") or {}
            saved["api"] = {"host": normalise_host(host), "user_name": user, "system_code": code, "station_code": str(api.get("station_code") or "").strip() or None} if user else {}
            # A different account or plant means a different history. Naming the plant that was
            # already chosen automatically does not.
            current = saved["api"]
            known = previous.get("station_code") or (self.api_plant[0] if self.api_plant else None)
            if previous.get("user_name") != current.get("user_name") or (known and current.get("station_code") and known != current["station_code"]):
                self.ledger.forget_api()
        self.settings_file.write_text(json.dumps(saved, indent=2))
        try:
            self.settings_file.chmod(0o600)
        except OSError:
            pass
        self._load_settings()

    @property
    def sources(self) -> dict[str, bool]:
        return {"kiosk": self._link is not None, "api": self._api is not None}

    @property
    def configured(self) -> bool:
        return self._link is not None or self._api is not None

    # ----- the loop -------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), 30)
            except asyncio.TimeoutError:
                pass

    async def tick(self, force: bool = False) -> None:
        async with self._lock:
            now = time.time()
            if self._link is not None and (force or now - self._last_kiosk >= KIOSK_INTERVAL):
                self._last_kiosk = now
                await self._poll_kiosk()
            if self._api is not None and (force or now >= self._next_api):
                await self._api_step()

    async def _poll_kiosk(self) -> None:
        assert self._link is not None
        try:
            snap = await kiosk.fetch(self.session, self._link)
        except KioskError as exc:
            self.kiosk_error = str(exc)
            log.warning("Solar kiosk: %s", exc)
            if self.endpoint is not None and time.time() - self._last_good_kiosk > STALE_AFTER:
                self.endpoint.set_online(False)
            return
        self.kiosk_error = None
        self._last_good_kiosk = snap.fetched_at
        self.snapshot = snap
        self._record(snap)
        await self._present(snap)

    def _record(self, snap: Snapshot) -> None:
        """Write the kiosk's running totals down, so a history builds up even without the API."""
        now = datetime.now()
        # Just after midnight the kiosk can still show yesterday's totals.
        if now.hour == 0 and now.minute < 15:
            return
        for span, key, value in (("day", now.strftime("%Y-%m-%d"), snap.today_kwh), ("month", now.strftime("%Y-%m"), snap.month_kwh), ("year", now.strftime("%Y"), snap.year_kwh)):
            if value is not None:
                self.ledger.put(span, key, value, "kiosk", snap.fetched_at, running=True)

    async def _present(self, snap: Snapshot) -> None:
        assert self._link is not None
        serial = "fusionsolar-" + hashlib.sha1(self._link.kk.encode()).hexdigest()[:10]
        name = self.cfg.name or (f"Solar {snap.name}" if snap.name != "Solar" else "Solar")
        changed = False
        if self.endpoint is None or self.endpoint.third_serial != serial:
            self.endpoint = SolarEndpoint(serial, name)
            changed = True
            log.info("Solar plant '%s' ready: %.2f kW now, %.1f kWh today", snap.name, snap.power_kw or 0, snap.today_kwh or 0)
        had_counter = self.endpoint.energy_counter_kwh() is not None
        self.endpoint.update(snap)
        if (changed or not had_counter) and self.on_endpoint_change is not None:
            await self.on_endpoint_change()

    # ----- history from the API ---------------------------------------------------

    async def _api_step(self) -> None:
        """Make at most one history call: the running periods first, then the past, newest first."""
        assert self._api is not None
        now = time.time()
        try:
            if self.api_plant is None:
                self.api_plant = await self._choose_plant()
            job = self._next_job(now)
            if job is None:
                self._next_api = now + API_STEP
                self.api_error = None
                return
            span, year, month, key = job
            today = datetime.fromtimestamp(now)
            running = {today.strftime("%Y-%m-%d"), today.strftime("%Y-%m"), today.strftime("%Y")}
            for item in await self._api.yields(self.api_plant[0], span, year, month):
                self.ledger.put(item.span, item.key, item.kwh, "api", now, running=item.key in running)
            self.ledger.mark_fetched(span, key, now)
            self.api_error = None
            self._next_api = now + API_STEP
            log.info("Solar history: read %s %s from Huawei", span, key)
        except NorthboundError as exc:
            self.api_error = str(exc)
            self._next_api = now + (BUSY_BACKOFF if exc.busy else ERROR_BACKOFF)
            log.warning("Solar API: %s; next try in %d min", exc, (self._next_api - now) // 60)

    async def _choose_plant(self) -> tuple[str, str]:
        assert self._api is not None
        plants = await self._api.plants()
        if not plants:
            raise NorthboundError("the API account can read no plant; tick the plant for it in Northbound Management")
        if self._station_code:
            match = [p for p in plants if p[0] == self._station_code]
        elif self.snapshot is not None and len(plants) > 1:
            match = [p for p in plants if p[1].casefold() == self.snapshot.name.casefold()]
        else:
            match = plants[:1] if len(plants) == 1 else []
        if not match:
            names = ", ".join(f"{name} ({code})" for code, name in plants)
            raise NorthboundError(f"the API account reads several plants; set the plant code. Plants: {names}")
        return match[0]

    def _next_job(self, now: float) -> tuple[str, int, int, str] | None:
        today = datetime.fromtimestamp(now)

        def due(span: str, key: str, end: float | None) -> bool:
            at = self.ledger.fetched_at(span, key)
            if at is None:
                return True
            if end is not None and now > end:
                return at < end + SETTLE  # finished: one last read after it has settled
            return now - at > RUNNING_TTL

        def month_end(year: int, month: int) -> float:
            return datetime(year + (month == 12), month % 12 + 1, 1).timestamp()

        if due("year", "all", None):
            return ("year", today.year, 7, "all")
        if due("month", str(today.year), None):
            return ("month", today.year, 7, str(today.year))
        this_month = today.strftime("%Y-%m")
        if due("day", this_month, None):
            return ("day", today.year, today.month, this_month)
        years = [int(item["key"]) for item in self.ledger.items("year") if item["source"] == "api" and item["kwh"] > 0]
        first_year = min(years, default=today.year)
        for year in range(today.year - 1, first_year - 1, -1):
            if due("month", str(year), datetime(year + 1, 1, 1).timestamp()):
                return ("month", year, 7, str(year))
        # Days: only the months that produced something, newest first.
        producing = {item["key"] for item in self.ledger.items("month") if item["kwh"] > 0}
        year, month = today.year, today.month
        while (year, month) >= (first_year, 1):
            month -= 1
            if month == 0:
                year, month = year - 1, 12
            key = f"{year}-{month:02d}"
            if year >= first_year and key in producing and due("day", key, month_end(year, month)):
                return ("day", year, month, key)
        return None

    # ----- what the HTTP API serves -------------------------------------------------

    def live(self) -> dict[str, Any]:
        snap = self.snapshot
        out: dict[str, Any] = {
            "configured": self.configured,
            "sources": self.sources,
            "kiosk_error": self.kiosk_error,
            "api_error": self.api_error,
            "kiosk_url": self._link.url if self._link else None,
            "serial": self.endpoint.third_serial if self.endpoint else None,
        }
        if snap is not None:
            out.update(
                {
                    "name": snap.name,
                    "fetched_at": snap.fetched_at,
                    "power_kw": snap.power_kw,
                    "today_kwh": snap.today_kwh,
                    "month_kwh": snap.month_kwh,
                    "year_kwh": snap.year_kwh,
                    "lifetime_kwh": snap.lifetime_kwh,
                    "co2_kg": snap.co2_kg,
                    "peak_kw": snap.peak_kw,
                    "updated": snap.updated,
                    "curve": [[t, v] for t, v in snap.curve],
                }
            )
        return out

    def history(self, span: str, year: int | None = None, month: int | None = None) -> dict[str, Any]:
        prefix = "" if span == "year" else f"{year:04d}-" if span == "month" else f"{year:04d}-{month:02d}-"
        return {"span": span, "items": self.ledger.items(span, prefix), "api": self._api is not None, "api_error": self.api_error}

    def status(self) -> dict[str, Any]:
        """For the setup page. Never includes the system code."""
        return {
            "kiosk_url": self._link.url if self._link else None,
            "kiosk_error": self.kiosk_error,
            "plant": self.snapshot.name if self.snapshot else None,
            "power_kw": self.snapshot.power_kw if self.snapshot else None,
            "today_kwh": self.snapshot.today_kwh if self.snapshot else None,
            "api_user": self._api.user_name if self._api else None,
            "api_host": self._api.host if self._api else api_host_for(self._link.host if self._link else None),
            "api_plant": {"code": self.api_plant[0], "name": self.api_plant[1]} if self.api_plant else None,
            "api_error": self.api_error,
            "days_on_record": len(self.ledger.items("day")),
        }
