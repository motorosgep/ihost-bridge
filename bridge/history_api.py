"""HTTP routes serving the history recorder: GET /api/history, /metrics, /samples, /energy."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from aiohttp import web

from bridge.core.recorder import parse_time

if TYPE_CHECKING:
    from bridge.core.recorder import Recorder

CORS = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"}


def history_routes() -> list[web.RouteDef]:
    return [
        web.get("/api/history", api_info),
        web.get("/api/history/metrics", api_metrics),
        web.get("/api/history/samples", api_samples),
        web.get("/api/history/energy", api_energy),
    ]


def _recorder(request: web.Request) -> Recorder | None:
    return request.app.get("recorder")


def _window(request: web.Request) -> tuple[float, float, float]:
    now = time.time()
    end = parse_time(request.query.get("to"), now)
    start = parse_time(request.query.get("from"), end - 24 * 3600)
    try:
        resolution = float(request.query.get("resolution") or 0)
    except ValueError:
        resolution = 0.0
    return start, end, resolution


async def api_info(request: web.Request) -> web.Response:
    rec = _recorder(request)
    if rec is None:
        return web.json_response({"recorder": False}, headers=CORS)
    return web.json_response(rec.info(), headers=CORS)


async def api_metrics(request: web.Request) -> web.Response:
    rec = _recorder(request)
    serial = request.query.get("serial")
    if rec is None:
        return web.json_response({"error": "recorder disabled"}, status=404, headers=CORS)
    if not serial:
        return web.json_response({"error": "serial required"}, status=400, headers=CORS)
    return web.json_response({"serial": serial, "metrics": rec.metrics(serial)}, headers=CORS)


async def api_samples(request: web.Request) -> web.Response:
    rec = _recorder(request)
    serial, metric = request.query.get("serial"), request.query.get("metric")
    if rec is None:
        return web.json_response({"error": "recorder disabled"}, status=404, headers=CORS)
    if not serial or not metric:
        return web.json_response({"error": "serial and metric required"}, status=400, headers=CORS)
    start, end, resolution = _window(request)
    return web.json_response(rec.samples(serial, metric, start, end, resolution), headers=CORS)


async def api_energy(request: web.Request) -> web.Response:
    rec = _recorder(request)
    serial = request.query.get("serial")
    if rec is None:
        return web.json_response({"error": "recorder disabled"}, status=404, headers=CORS)
    if not serial:
        return web.json_response({"error": "serial required"}, status=400, headers=CORS)
    start, end, resolution = _window(request)
    return web.json_response(rec.energy(serial, start, end, resolution or 3600), headers=CORS)
