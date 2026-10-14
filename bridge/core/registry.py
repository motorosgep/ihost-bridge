"""The registry owns all endpoints and keeps the gateway in sync with them.

Responsibilities:
- DiscoveryRequest on start (and on demand) and persisting the serial numbers
  the gateway assigned
- change-only DeviceStatesChangeReport when an endpoint's state changes
- DeviceOnlineChangeReport on availability changes
- dispatching UpdateDeviceStates / QueryDeviceStates directives to endpoints
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from bridge.core.endpoint import ENERGY_RESOLUTION, Endpoint
from bridge.core.energy import parse_ts
from bridge.cube.client import CubeAuthError, CubeClient, CubeError

log = logging.getLogger(__name__)

DIRECTIVE_TIMEOUT = 2.5  # the gateway gives up after 3 s


class Registry:
    def __init__(self, client: CubeClient, data_dir: Path, service_address: str) -> None:
        self.client = client
        self.service_address = service_address
        self.endpoints: dict[str, Endpoint] = {}
        self._serial_file = data_dir / "registry.json"
        self.serials: dict[str, str] = self._load_serials()
        self._reported: dict[str, dict[str, Any]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self.synced = False
        self.last_sync_error: str | None = None

    # ----- persistence -------------------------------------------------------

    def _load_serials(self) -> dict[str, str]:
        try:
            return json.loads(self._serial_file.read_text())
        except FileNotFoundError:
            return {}
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s: %s", self._serial_file, exc)
            return {}

    def _save_serials(self) -> None:
        self._serial_file.write_text(json.dumps(self.serials, indent=2))

    # ----- endpoint management ---------------------------------------------

    def add(self, endpoint: Endpoint) -> None:
        if endpoint.third_serial in self.endpoints:
            raise ValueError(f"duplicate endpoint {endpoint.third_serial}")
        self.endpoints[endpoint.third_serial] = endpoint
        endpoint.add_state_listener(self._on_state)
        endpoint.add_online_listener(self._on_online)
        log.info("Endpoint added: %s (%s, %s)", endpoint.name, endpoint.third_serial, endpoint.display_category)

    def remove(self, third_serial: str) -> None:
        """Forget an endpoint (its gateway serial is kept so a re-added endpoint maps to the same device)."""
        self.endpoints.pop(third_serial, None)
        self._reported.pop(third_serial, None)

    def is_current(self, endpoint: Endpoint) -> bool:
        return self.endpoints.get(endpoint.third_serial) is endpoint

    def serial_of(self, endpoint: Endpoint) -> str | None:
        return self.serials.get(endpoint.third_serial)

    def endpoint_by_serial(self, serial: str) -> Endpoint | None:
        for third, ser in self.serials.items():
            if ser == serial:
                return self.endpoints.get(third)
        return None

    # ----- sync ---------------------------------------------------------------

    async def sync(self) -> dict[str, str]:
        """Send every endpoint to the gateway. Safe to repeat; the gateway updates in place."""
        self._loop = asyncio.get_running_loop()
        described = [ep.describe(self.service_address) for ep in self.endpoints.values()]
        if not described:
            log.warning("No endpoints to sync")
            return {}
        try:
            mapping = await self.client.discovery(described)
        except CubeError as exc:
            self.last_sync_error = str(exc)
            self.synced = False
            raise
        self.serials.update(mapping)
        self._save_serials()
        for ep in self.endpoints.values():
            self._reported[ep.third_serial] = ep.state()
        self.synced = True
        self.last_sync_error = None
        log.info("Synced %d endpoint(s) with the gateway", len(mapping))
        missing = [ep.name for ep in self.endpoints.values() if ep.third_serial not in self.serials]
        if missing:
            log.warning("Gateway did not return serials for: %s", ", ".join(missing))
        return mapping

    async def report_all(self) -> None:
        """Push the full state of every endpoint (used after reconnects)."""
        for ep in self.endpoints.values():
            self._reported.pop(ep.third_serial, None)
            await self.report(ep)

    # ----- change reporting --------------------------------------------------

    def _on_state(self, endpoint: Endpoint) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.report(endpoint)))

    def _on_online(self, endpoint: Endpoint, online: bool) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._report_online(endpoint, online)))

    def sample_energy(self, endpoint: Endpoint) -> None:
        if endpoint.energy_log is None:
            return
        value = endpoint.energy_counter_kwh()
        if value is not None:
            endpoint.energy_log.record(value)

    def sample_energy_all(self) -> None:
        for ep in self.endpoints.values():
            self.sample_energy(ep)

    async def report(self, endpoint: Endpoint) -> None:
        if not self.is_current(endpoint):
            return  # a replaced endpoint whose old device thread is still winding down
        self.sample_energy(endpoint)
        serial = self.serial_of(endpoint)
        if not serial or not self.client.token_valid:
            return
        state = endpoint.state()
        previous = self._reported.get(endpoint.third_serial, {})
        changed = {k: v for k, v in state.items() if previous.get(k) != v}
        if not changed:
            return
        try:
            await self.client.report_state(serial, endpoint.third_serial, changed)
            self._reported[endpoint.third_serial] = state
            log.debug("Reported %s: %s", endpoint.name, changed)
        except CubeAuthError as exc:
            log.error("State report rejected, token invalid: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("State report failed for %s: %s", endpoint.name, exc)

    async def _report_online(self, endpoint: Endpoint, online: bool) -> None:
        if not self.is_current(endpoint):
            return
        serial = self.serial_of(endpoint)
        if not serial or not self.client.token_valid:
            return
        try:
            await self.client.report_online(serial, endpoint.third_serial, online)
            log.info("%s is now %s", endpoint.name, "online" if online else "offline")
            if online:
                self._reported.pop(endpoint.third_serial, None)
                await self.report(endpoint)
        except Exception as exc:  # noqa: BLE001
            log.warning("Online report failed for %s: %s", endpoint.name, exc)

    # ----- directives from the gateway --------------------------------------

    async def handle_directive(self, directive: dict[str, Any]) -> dict[str, Any]:
        header = directive.get("header") or {}
        name = header.get("name")
        message_id = header.get("message_id", "")
        ref = directive.get("endpoint") or {}
        endpoint = self.endpoints.get(ref.get("third_serial_number") or "")
        if endpoint is None and ref.get("serial_number"):
            endpoint = self.endpoint_by_serial(ref["serial_number"])

        def respond(resp_name: str, payload: dict[str, Any]) -> dict[str, Any]:
            version = header.get("version") or "2"
            return {"event": {"header": {"name": resp_name, "message_id": message_id, "version": version}, "payload": payload}}

        def error(err_type: str, description: str) -> dict[str, Any]:
            log.warning("Directive %s failed: %s (%s)", name, description, err_type)
            return respond("ErrorResponse", {"type": err_type, "description": description})

        if endpoint is None:
            return error("NO_SUCH_ENDPOINT", f"unknown endpoint {ref}")
        if not endpoint.online:
            return error("ENDPOINT_UNREACHABLE", f"{endpoint.name} is offline")

        payload = directive.get("payload") or {}
        if name == "UpdateDeviceStates":
            state = dict(payload.get("state") or {})
            log.info("Command for %s: %s", endpoint.name, json.dumps(state))
            if "power-consumption" in state:
                self._energy_update(endpoint, state.pop("power-consumption"))
                if not state:
                    return respond("Response", {})
            try:
                await asyncio.wait_for(endpoint.apply(state), DIRECTIVE_TIMEOUT)
            except asyncio.TimeoutError:
                return error("INTERNAL_ERROR", "device did not answer in time")
            except ValueError as exc:
                return error("INVALID_DIRECTIVE", str(exc))
            except Exception as exc:  # noqa: BLE001
                log.exception("apply() failed for %s", endpoint.name)
                return error("INTERNAL_ERROR", str(exc))
            # Reflect what the appliance now reports; its own update will follow.
            asyncio.ensure_future(self.report(endpoint))
            return respond("Response", {})

        if name == "QueryDeviceStates":
            requested = payload.get("state") or {}
            if "power-consumption" in requested:
                if endpoint.energy_log is None:
                    return error("NOT_SUPPORTED_IN_CURRENT_MODE", f"{endpoint.name} has no energy meter")
                result = self._energy_query(endpoint, requested["power-consumption"])
                return respond("Response", {"state": {"power-consumption": result}})
            return respond("Response", {"state": endpoint.state()})

        return error("INVALID_DIRECTIVE", f"unsupported directive {name}")

    # ----- energy statistics (power-consumption capability) -------------------

    @staticmethod
    def _energy_update(endpoint: Endpoint, request: dict[str, Any]) -> None:
        """Start or stop the gateway's real-time summary window."""
        if endpoint.energy_log is None:
            return
        inner = request.get("powerConsumption") or request
        if "rlSummarize" not in inner:
            return
        if inner["rlSummarize"]:
            start = parse_ts((inner.get("timeRange") or {}).get("start")) or time.time()
            endpoint.energy_log.set_realtime_window(start)
            log.info("%s: real-time energy summary started", endpoint.name)
        else:
            endpoint.energy_log.set_realtime_window(None)
            log.info("%s: real-time energy summary stopped", endpoint.name)

    def _energy_query(self, endpoint: Endpoint, request: dict[str, Any]) -> dict[str, Any]:
        elog = endpoint.energy_log
        assert elog is not None
        self.sample_energy(endpoint)
        now = time.time()
        inner = request.get("powerConsumption") or request
        qtype = inner.get("type") or "summarize"
        if qtype == "rlSummarize":
            start = elog.rl_start if elog.rl_start is not None else now
            return {"type": "rlSummarize", "rlSummarize": round(elog.usage(start, now) * 100, 2)}
        time_range = inner.get("timeRange") or {}
        start = parse_ts(time_range.get("start"))
        end = parse_ts(time_range.get("end")) or now
        if start is None:
            start = end - 24 * 3600
        return {
            "type": "summarize",
            "rlSummarize": round(elog.usage(start, end) * 100, 2),
            "electricityIntervals": elog.intervals(start, end, ENERGY_RESOLUTION),
        }

    # ----- status for the web UI ---------------------------------------------

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "third_serial": ep.third_serial,
                "serial": self.serial_of(ep),
                "name": ep.name,
                "category": ep.display_category,
                "model": ep.model,
                "online": ep.online,
                "state": ep.state(),
            }
            for ep in self.endpoints.values()
        ]
