"""Client for the eWeLink CUBE Open API v2 as served by a SONOFF iHost.

Only the parts needed by a third-party device bridge are implemented:
token acquisition, DiscoveryRequest, DeviceStatesChangeReport,
DeviceOnlineChangeReport and the device list.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=10)


class CubeError(Exception):
    """The gateway answered with an error."""


class CubeAuthError(CubeError):
    """No token, or the gateway rejected the token."""


class CubeClient:
    def __init__(self, session: aiohttp.ClientSession, host: str, app_name: str, data_dir: Path):
        self._session = session
        self.host = host
        self.base = f"http://{host}/open-api/v2/rest"
        self.app_name = app_name
        self._token_file = data_dir / "cube_token.json"
        self.token: str | None = None
        self.token_valid = False
        self.last_error: str | None = None
        self._load_token()

    # ----- token -----------------------------------------------------------

    def _load_token(self) -> None:
        try:
            data = json.loads(self._token_file.read_text())
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s: %s", self._token_file, exc)
            return
        self.token = data.get("token") or None
        self.token_valid = bool(self.token)
        if self.token:
            log.info("Loaded CUBE access token from %s", self._token_file)

    def _save_token(self) -> None:
        self._token_file.write_text(json.dumps({"token": self.token}))
        try:
            self._token_file.chmod(0o600)
        except OSError:
            pass

    async def try_get_token(self) -> bool:
        """One attempt at the access-token endpoint.

        Succeeds only within the window after the user pressed Allow on the
        gateway's "Get eWeLink CUBE access token" dialog.
        """
        url = f"{self.base}/bridge/access_token"
        async with self._session.get(url, params={"app_name": self.app_name}, timeout=TIMEOUT) as resp:
            data = await resp.json(content_type=None)
        if data.get("error") == 0 and (data.get("data") or {}).get("token"):
            self.token = data["data"]["token"]
            self.token_valid = True
            self.last_error = None
            self._save_token()
            log.info("Received CUBE access token")
            return True
        return False

    async def wait_for_token(self, timeout: float = 300.0, interval: float = 2.0) -> bool:
        """Poll until the user approves the request on the gateway, or time out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                if await self.try_get_token():
                    return True
            except Exception as exc:  # noqa: BLE001
                log.debug("Token poll failed: %s", exc)
            await asyncio.sleep(interval)
        return False

    def _headers(self) -> dict[str, str]:
        if not self.token:
            raise CubeAuthError("no access token yet")
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    # ----- plain REST ------------------------------------------------------

    async def bridge_info(self) -> dict[str, Any]:
        """Unauthenticated gateway info (ip, mac, firmware version, name)."""
        async with self._session.get(f"{self.base}/bridge", timeout=TIMEOUT) as resp:
            data = await resp.json(content_type=None)
        if data.get("error") != 0:
            raise CubeError(data)
        return data["data"]

    async def list_devices(self) -> list[dict[str, Any]]:
        async with self._session.get(f"{self.base}/devices", headers=self._headers(), timeout=TIMEOUT) as resp:
            data = await resp.json(content_type=None)
        self._check_rest(data)
        return data["data"]["device_list"]

    async def delete_device(self, serial_number: str) -> None:
        async with self._session.delete(
            f"{self.base}/devices/{serial_number}", headers=self._headers(), timeout=TIMEOUT
        ) as resp:
            data = await resp.json(content_type=None)
        self._check_rest(data)

    # ----- custom UI cards ---------------------------------------------------

    async def list_cards(self) -> list[dict[str, Any]]:
        async with self._session.get(f"{self.base}/ui/cards", headers=self._headers(), timeout=TIMEOUT) as resp:
            data = await resp.json(content_type=None)
        self._check_rest(data)
        return data.get("data") or []

    async def create_card(self, label: str, web_settings: dict[str, Any]) -> str:
        body = {"label": label, "web_settings": web_settings}
        async with self._session.post(f"{self.base}/ui/cards", json=body, headers=self._headers(), timeout=TIMEOUT) as resp:
            data = await resp.json(content_type=None)
        self._check_rest(data)
        return str(data["data"]["id"])

    async def delete_card(self, card_id: str) -> None:
        async with self._session.delete(f"{self.base}/ui/cards/{card_id}", headers=self._headers(), timeout=TIMEOUT) as resp:
            data = await resp.json(content_type=None)
        self._check_rest(data)

    def _check_rest(self, data: dict[str, Any]) -> None:
        err = data.get("error", 0)
        if err == 401:
            self.token_valid = False
            self.last_error = data.get("message", "unauthorized")
            raise CubeAuthError(self.last_error)
        if err != 0:
            self.last_error = f"{err}: {data.get('message')}"
            raise CubeError(self.last_error)
        self.token_valid = True

    # ----- third-party events ---------------------------------------------

    async def _event(self, name: str, payload: dict[str, Any], endpoint: dict[str, Any] | None = None) -> dict:
        body: dict[str, Any] = {
            "event": {
                "header": {"name": name, "message_id": str(uuid.uuid4()), "version": "2"},
                "payload": payload,
            }
        }
        if endpoint:
            body["event"]["endpoint"] = endpoint
        # The gateway drops idle connections without warning; one retry covers a stale socket.
        for attempt in (1, 2):
            try:
                async with self._session.post(
                    f"{self.base}/thirdparty/event", json=body, headers=self._headers(), timeout=TIMEOUT
                ) as resp:
                    if resp.status == 401:
                        self.token_valid = False
                        self.last_error = "token rejected"
                        raise CubeAuthError(self.last_error)
                    data = await resp.json(content_type=None)
                break
            except (aiohttp.ClientConnectionError, ConnectionResetError, asyncio.TimeoutError) as exc:
                if attempt == 2:
                    raise
                log.debug("%s to gateway failed (%s); retrying once", name, exc)
                await asyncio.sleep(0.5)

        # The gateway may answer with the generic {error,data,message} wrapper
        # or with a bare {header,payload} event; accept both.
        if "error" in data:
            self._check_rest(data)
            data = data.get("data") or {}
        header = data.get("header") or {}
        if header.get("name") == "ErrorResponse":
            self.last_error = json.dumps(data.get("payload"))
            if (data.get("payload") or {}).get("type") == "AUTH_FAILURE":
                self.token_valid = False
                raise CubeAuthError(self.last_error)
            raise CubeError(self.last_error)
        self.token_valid = True
        self.last_error = None
        return data.get("payload") or {}

    async def discovery(self, endpoints: list[dict[str, Any]]) -> dict[str, str]:
        """Register or update third-party devices. Returns third_serial -> serial_number."""
        payload = await self._event("DiscoveryRequest", {"endpoints": endpoints})
        return {e["third_serial_number"]: e["serial_number"] for e in payload.get("endpoints", [])}

    async def report_state(self, serial_number: str, third_serial: str, state: dict[str, Any]) -> None:
        await self._event(
            "DeviceStatesChangeReport",
            {"state": state},
            {"serial_number": serial_number, "third_serial_number": third_serial},
        )

    async def report_online(self, serial_number: str, third_serial: str, online: bool) -> None:
        await self._event(
            "DeviceOnlineChangeReport",
            {"online": online},
            {"serial_number": serial_number, "third_serial_number": third_serial},
        )
