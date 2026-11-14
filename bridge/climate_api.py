"""HTTP routes for room control (heating from indoor sensors).

GET  /api/climate   status, settings and the hub's temperature sensors to choose from
POST /api/climate   change settings: {"enabled", "target", "sensors", "below", "above", "min_run", "min_rest"}

Both need the caller's gateway access token, like the network routes: they read
the rooms and switch the heating.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from bridge.network_api import CORS, _authorised, _json, preflight

if TYPE_CHECKING:
    from bridge.app import BridgeApp


def climate_routes() -> list[web.RouteDef]:
    return [
        web.get("/api/climate", api_climate),
        web.post("/api/climate", api_climate_set),
        web.options("/api/climate", preflight),
    ]


async def _view(bridge: BridgeApp) -> dict:
    room = bridge.room
    try:
        devices = await bridge.client.list_devices()
    except Exception:  # noqa: BLE001
        devices = []
    return {**room.view(), "candidates": room.candidates(devices)}


async def api_climate(request: web.Request) -> web.Response:
    bridge: BridgeApp = request.app["bridge"]
    if (denied := await _authorised(request)) is not None:
        return denied
    if bridge.room is None:
        return _json({"error": "room control is not running"}, 503)
    return _json(await _view(bridge))


async def api_climate_set(request: web.Request) -> web.Response:
    bridge: BridgeApp = request.app["bridge"]
    if (denied := await _authorised(request)) is not None:
        return denied
    if bridge.room is None:
        return _json({"ok": False, "error": "room control is not running"}, 503)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        await bridge.room.change(body)
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)
    return _json({"ok": True, **(await _view(bridge))})


__all__ = ["climate_routes", "CORS"]
