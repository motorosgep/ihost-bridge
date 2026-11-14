"""HTTP routes for solar: GET /api/solar (live figures), GET /api/solar/history, POST /api/setup/solar."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from bridge.app import BridgeApp

CORS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}


def solar_routes() -> list[web.RouteDef]:
    return [
        web.get("/api/solar", api_solar),
        web.get("/api/solar/history", api_solar_history),
        web.post("/api/setup/solar", api_setup_solar),
    ]


async def api_solar(request: web.Request) -> web.Response:
    bridge: BridgeApp = request.app["bridge"]
    if bridge.solar is None:
        return web.json_response({"configured": False}, headers=CORS)
    return web.json_response(bridge.solar.live(), headers=CORS)


async def api_solar_history(request: web.Request) -> web.Response:
    """span=day&year=2026&month=11: that month's days; span=month&year=2026: that year's months; span=year: every year."""
    bridge: BridgeApp = request.app["bridge"]
    span = request.query.get("span", "day")
    if span not in ("day", "month", "year"):
        return web.json_response({"error": "span must be day, month or year"}, status=400, headers=CORS)
    now = datetime.now()
    try:
        year = int(request.query.get("year") or now.year)
        month = int(request.query.get("month") or now.month)
        if not (1 <= month <= 12 and 1970 <= year <= 9999):
            raise ValueError
    except ValueError:
        return web.json_response({"error": "year and month must be numbers"}, status=400, headers=CORS)
    if bridge.solar is None:
        return web.json_response({"span": span, "items": [], "api": False, "api_error": None}, headers=CORS)
    return web.json_response(bridge.solar.history(span, year, month), headers=CORS)


async def api_setup_solar(request: web.Request) -> web.Response:
    """Save the kiosk link and/or the API account from the setup page, then read the plant right away."""
    bridge: BridgeApp = request.app["bridge"]
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        message = await bridge.save_solar(body)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response({"ok": True, "message": message})
