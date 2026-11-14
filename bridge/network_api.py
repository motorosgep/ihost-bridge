"""HTTP routes for the home network (TP-Link Deco).

GET    /api/network                     the mesh: internet, Deco units, clients, reservations
POST   /api/network/reservations        {"items": [{"mac", "ip"}, ...]}: add or change fixed IPs
DELETE /api/network/reservations/{mac}  drop one fixed IP
POST   /api/setup/deco                  {"host", "password"} from the setup page

The network routes show who is home and change the router, so they need the
caller's gateway access token (the one an app such as Butter got when the user
pressed Allow on the iHost), as `Authorization: Bearer <token>`. The bridge asks
the gateway whether the token is genuine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from bridge.app import BridgeApp

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Cache-Control": "no-store",
}


def network_routes() -> list[web.RouteDef]:
    return [
        web.get("/api/network", api_network),
        web.post("/api/network/reservations", api_reserve),
        web.delete("/api/network/reservations/{mac}", api_unreserve),
        web.options("/api/network", preflight),
        web.options("/api/network/reservations", preflight),
        web.options("/api/network/reservations/{mac}", preflight),
        web.post("/api/setup/deco", api_setup_deco),
    ]


def _json(body: object, status: int = 200) -> web.Response:
    return web.json_response(body, status=status, headers=CORS)


async def preflight(request: web.Request) -> web.Response:
    return web.Response(status=204, headers=CORS)


async def _authorised(request: web.Request) -> web.Response | None:
    """None when the caller showed a gateway token the gateway accepts, else the error response."""
    bridge: BridgeApp = request.app["bridge"]
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else None
    if not token:
        return _json({"error": "a gateway access token is needed (Authorization: Bearer <token>)"}, 401)
    if not await bridge.hub_token_ok(token):
        return _json({"error": "the gateway did not accept that access token"}, 403)
    return None


async def api_network(request: web.Request) -> web.Response:
    bridge: BridgeApp = request.app["bridge"]
    if (denied := await _authorised(request)) is not None:
        return denied
    if bridge.deco is None or not bridge.deco.configured:
        return _json({"configured": False})
    return _json(bridge.deco.network())


async def api_reserve(request: web.Request) -> web.Response:
    from bridge.deco.service import DecoUnavailable

    bridge: BridgeApp = request.app["bridge"]
    if (denied := await _authorised(request)) is not None:
        return denied
    if bridge.deco is None or not bridge.deco.configured:
        return _json({"ok": False, "error": "the Deco is not set up on the bridge"}, 409)
    try:
        body = await request.json()
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list) or not items or not all(isinstance(i, dict) for i in items):
            raise ValueError('expected {"items": [{"mac": "...", "ip": "..."}]}')
        if len(items) > 32:
            raise ValueError("at most 32 reservations at a time")
        result = await bridge.deco.reserve(items)
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)
    except DecoUnavailable as exc:
        return _json({"ok": False, "error": str(exc)}, 503)
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": f"the Deco refused the change: {exc}"}, 502)
    return _json({"ok": True, **result})


async def api_unreserve(request: web.Request) -> web.Response:
    from bridge.deco.service import DecoUnavailable

    bridge: BridgeApp = request.app["bridge"]
    if (denied := await _authorised(request)) is not None:
        return denied
    if bridge.deco is None or not bridge.deco.configured:
        return _json({"ok": False, "error": "the Deco is not set up on the bridge"}, 409)
    try:
        result = await bridge.deco.unreserve(request.match_info["mac"])
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)}, 400)
    except LookupError as exc:
        return _json({"ok": False, "error": str(exc)}, 404)
    except DecoUnavailable as exc:
        return _json({"ok": False, "error": str(exc)}, 503)
    except Exception as exc:  # noqa: BLE001
        return _json({"ok": False, "error": f"the Deco refused the change: {exc}"}, 502)
    return _json({"ok": True, **result})


async def api_setup_deco(request: web.Request) -> web.Response:
    """Save the Deco address and admin password from the setup page, then read the mesh right away."""
    bridge: BridgeApp = request.app["bridge"]
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        message = await bridge.save_deco(body)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response({"ok": True, "message": message})
