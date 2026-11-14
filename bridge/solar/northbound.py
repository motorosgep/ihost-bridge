"""Huawei's Northbound API ("thirdData"): the plant's history, with an API account.

The account is a user name and a system code, created in FusionSolar under
System > Company Management > Northbound Management. Huawei limits how often
each call may be made, so the caller keeps what it reads (see ledger.py) and
asks rarely.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=30)
HOST_RE = re.compile(r"^[a-z0-9-]+\.fusionsolar\.huawei\.com$")
PATHS = {"day": "getKpiStationDay", "month": "getKpiStationMonth", "year": "getKpiStationYear"}
MESSAGES = {
    20400: "Huawei did not accept that user name and system code",
    20056: "this API account is not allowed to use the API on this host",
    401: "this API account has no permission for that data; check what is ticked for it in Northbound Management",
    407: "Huawei limits how often this can be asked",
}


class NorthboundError(Exception):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def busy(self) -> bool:
        """Refused for asking too often; try again later."""
        return self.code == 407


def api_host_for(kiosk_host: str | None) -> str:
    """The API lives on the region's main host (eu5...), not the numbered one kiosk links use (uniNNNeu5...)."""
    match = re.match(r"^(?:uni\d+)?([a-z]+\d+\.fusionsolar\.huawei\.com)$", kiosk_host or "")
    return match.group(1) if match else "eu5.fusionsolar.huawei.com"


def normalise_host(text: str | None) -> str | None:
    host = re.sub(r"/.*$", "", re.sub(r"^https?://", "", (text or "").strip().lower()))
    return host if HOST_RE.match(host) else None


@dataclass
class Yield:
    """Energy produced in one day ("2026-11-14"), month ("2026-11") or year ("2026")."""

    span: str
    key: str
    kwh: float


def plant_date(stamp_ms: float) -> datetime:
    """Huawei stamps each figure with midnight on the plant's clock; whatever offset
    puts the stamp on a midnight gives the date back, with no timezone table."""
    day = 86_400_000
    offset = (day - int(stamp_ms) % day) % day
    if offset > 14 * 3_600_000:
        offset -= day
    return datetime.fromtimestamp(0, timezone.utc) + timedelta(milliseconds=int(stamp_ms) + offset)


def parse_yields(rows: Any, span: str) -> list[Yield]:
    out: list[Yield] = []
    for row in rows if isinstance(rows, list) else []:
        stamp = row.get("collectTime")
        items = row.get("dataItemMap") or {}
        # PVYield is the panels' output on newer systems; inverter_power is the same figure on older ones.
        raw = next((items[k] for k in ("PVYield", "inverter_power", "inverterYield") if items.get(k) is not None), None)
        try:
            kwh = float(raw)
            d = plant_date(float(stamp))
        except (TypeError, ValueError):
            continue
        key = d.strftime("%Y-%m-%d" if span == "day" else "%Y-%m" if span == "month" else "%Y")
        out.append(Yield(span, key, max(0.0, kwh)))
    return sorted(out, key=lambda y: y.key)


class Northbound:
    def __init__(self, session: aiohttp.ClientSession, host: str, user_name: str, system_code: str) -> None:
        self.session = session
        self.host = host
        self.user_name = user_name
        self._system_code = system_code
        self._token: str | None = None

    async def _post(self, path: str, body: dict[str, Any], token: str | None = None) -> tuple[dict[str, Any], str | None]:
        headers = {"accept": "application/json"}
        if token:
            headers["xsrf-token"] = token
        try:
            # No redirects: the numbered kiosk hosts answer API calls with a redirect to the sign-in page.
            async with self.session.post(f"https://{self.host}/thirdData/{path}", json=body, headers=headers, timeout=TIMEOUT, allow_redirects=False) as res:
                if res.status != 200:
                    raise NorthboundError(f"FusionSolar answered {res.status} on {self.host}; is that the API host for your region?")
                return await res.json(content_type=None), res.headers.get("xsrf-token")
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise NorthboundError(f"could not reach FusionSolar: {exc or type(exc).__name__}") from exc

    @staticmethod
    def _failure(body: dict[str, Any]) -> NorthboundError:
        code = body.get("failCode") if isinstance(body.get("failCode"), int) else None
        detail = f" (code {code})" if code is not None else ""
        message = MESSAGES.get(code or -1) or f"FusionSolar refused the request{detail}" + (f": {body['message']}" if body.get("message") else "")
        return NorthboundError(message, code)

    async def login(self) -> str:
        body, token = await self._post("login", {"userName": self.user_name, "systemCode": self._system_code})
        if not body.get("success") or not token:
            raise self._failure(body)
        self._token = token
        return token

    async def call(self, path: str, body: dict[str, Any]) -> Any:
        for attempt in (0, 1):
            token = self._token or await self.login()
            answer, _ = await self._post(path, body, token)
            if answer.get("success") and answer.get("data") is not None:
                return answer["data"]
            if answer.get("failCode") == 305 and attempt == 0:  # the token ran out: sign in again, once
                self._token = None
                continue
            raise self._failure(answer)
        raise NorthboundError("FusionSolar kept refusing the sign-in")

    async def plants(self) -> list[tuple[str, str]]:
        """(code, name) of every plant the account may read."""
        try:
            rows = await self.call("getStationList", {})
            return [(str(r["stationCode"]), str(r.get("stationName") or r["stationCode"]).strip()) for r in rows if r.get("stationCode")]
        except NorthboundError as exc:
            if exc.busy:
                raise
            # Newer accounts are only allowed the paged list.
            page = await self.call("stations", {"pageNo": 1})
            return [(str(r["plantCode"]), str(r.get("plantName") or r["plantCode"]).strip()) for r in (page.get("list") or []) if r.get("plantCode")]

    async def yields(self, station_code: str, span: str, year: int, month: int = 7) -> list[Yield]:
        """`day`: each day of that month; `month`: each month of that year; `year`: the years on record."""
        # The middle of the period, so it is the same period on the plant's clock wherever that is.
        moment = datetime(year, month if span == "day" else 7, 15, 12, tzinfo=timezone.utc)
        rows = await self.call(PATHS[span], {"stationCodes": station_code, "collectTime": int(moment.timestamp() * 1000)})
        return parse_yields(rows, span)
