"""The FusionSolar kiosk link: live figures without an account."""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

HOST_RE = re.compile(r"^[a-z0-9-]+\.fusionsolar\.huawei\.com$")
TIMEOUT = aiohttp.ClientTimeout(total=20)


class KioskError(Exception):
    pass


@dataclass
class KioskLink:
    host: str
    kk: str

    @property
    def url(self) -> str:
        return f"https://{self.host}/pvmswebsite/nologin/assets/build/cloud.html#/kiosk?kk={self.kk}"


def parse_link(text: str | None) -> KioskLink | None:
    """Host and key out of a pasted kiosk link, or None when it is not one."""
    text = (text or "").strip()
    host = re.match(r"^(?:https?://)?([^/\s]+)", text, re.I)
    kk = re.search(r"[?&]kk=([A-Za-z0-9_-]+)", text)
    if not host or not kk or not HOST_RE.match(host.group(1).lower()):
        return None
    return KioskLink(host.group(1).lower(), kk.group(1))


@dataclass
class Snapshot:
    """What the plant reports right now."""

    fetched_at: float
    name: str = "Solar"
    power_kw: float | None = None
    today_kwh: float | None = None
    month_kwh: float | None = None
    year_kwh: float | None = None
    lifetime_kwh: float | None = None
    co2_kg: float | None = None
    # Today's output as [minutes after midnight on the plant's clock, kW].
    curve: list[tuple[int, float]] = field(default_factory=list)

    @property
    def updated(self) -> int | None:
        return self.curve[-1][0] if self.curve else None

    @property
    def peak_kw(self) -> float:
        return max((v for _, v in self.curve), default=0.0)


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def parse_kiosk(body: dict[str, Any], fetched_at: float | None = None) -> Snapshot:
    if not body.get("success") or not isinstance(body.get("data"), str):
        raise KioskError("the kiosk link was not accepted; it may have expired or been switched off in FusionSolar")
    try:
        raw = json.loads(html.unescape(body["data"]))  # the kiosk wraps its JSON in HTML escaping
    except ValueError as exc:
        raise KioskError(f"the kiosk answered something unreadable: {exc}") from exc
    kpi = raw.get("realKpi") or {}
    chart = raw.get("powerCurve") or {}
    curve: list[tuple[int, float]] = []
    for label, value in zip(chart.get("xAxis") or [], chart.get("activePower") or []):
        kw = _num(value)  # slots the plant has not reported come as "-"
        try:
            hour, minute = (int(part) for part in str(label).split(":"))
        except ValueError:
            continue
        if kw is not None:
            curve.append((hour * 60 + minute, max(0.0, kw)))
    power = _num(kpi.get("realTimePower"))
    co2 = _num((raw.get("socialContribution") or {}).get("co2Reduction"))
    return Snapshot(
        fetched_at=fetched_at if fetched_at is not None else time.time(),
        name=str((raw.get("stationOverview") or {}).get("stationName") or "").strip() or "Solar",
        power_kw=max(0.0, power) if power is not None else None,
        today_kwh=_num(kpi.get("dailyEnergy")),
        month_kwh=_num(kpi.get("monthEnergy")),
        year_kwh=_num(kpi.get("yearEnergy")),
        lifetime_kwh=_num(kpi.get("cumulativeEnergy")),
        co2_kg=co2 if co2 and co2 > 0 else None,
        curve=curve,
    )


async def fetch(session: aiohttp.ClientSession, link: KioskLink) -> Snapshot:
    url = f"https://{link.host}/rest/pvms/web/kiosk/v1/station-kiosk-file"
    try:
        async with session.get(url, params={"kk": link.kk}, headers={"accept": "application/json"}, timeout=TIMEOUT) as res:
            if res.status != 200:
                raise KioskError(f"FusionSolar answered {res.status}")
            body = await res.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise KioskError(f"could not reach FusionSolar: {exc or type(exc).__name__}") from exc
    return parse_kiosk(body)
