"""Application wiring: gateway client, registry, vendor adapters and the web server."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import socket
import time
from typing import Any

import aiohttp
from aiohttp import web

from bridge import __version__
from bridge.config import Config
from bridge.core.registry import Registry
from bridge.cube.client import CubeAuthError, CubeClient, CubeError
from bridge.cube.server import make_app

log = logging.getLogger(__name__)

LOCAL_GATEWAY_NAMES = {"ihost", "ihost.local", "localhost", "127.0.0.1"}


class BridgeApp:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client: CubeClient | None = None
        self.registry: Registry | None = None
        self.message = "starting"
        self._token_task: asyncio.Task | None = None
        self._devices: list[Any] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._late_sync_running = False
        self.recorder: Any = None
        self._offline_since: float | None = None
        self._last_recovery = 0.0

    # ----- lifecycle ---------------------------------------------------------

    async def run(self) -> None:
        cfg = self.cfg
        self._loop = asyncio.get_running_loop()
        # Fresh connection per request: the gateway resets pooled keep-alive sockets after a short idle time.
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True)) as session:
            self.client = CubeClient(session, cfg.cube.host, cfg.cube.app_name, cfg.data_dir)
            service_address = self._service_address()
            self.registry = Registry(self.client, cfg.data_dir, service_address)

            if cfg.midea.enabled:
                await self._setup_midea()

            stop = asyncio.Event()
            recorder_task: asyncio.Task[None] | None = None
            if cfg.recorder.enabled:
                from bridge.core.recorder import Recorder

                self.recorder = Recorder(
                    self.client,
                    session,
                    cfg.data_dir / "history.sqlite",
                    snapshot_interval=cfg.recorder.snapshot_interval,
                    retention_days=cfg.recorder.retention_days,
                    energy_lookup=self._energy_log_for,
                )

            runner = web.AppRunner(make_app(self))
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", cfg.cube.port).start()
            log.info("ihost-bridge %s: web UI on port %d, gateway callback %s", __version__, cfg.cube.port, service_address)

            await self._initial_sync()
            if self.recorder is not None:
                recorder_task = asyncio.create_task(self.recorder.run(stop))

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, stop.set)
                except NotImplementedError:  # not on this platform
                    pass
            try:
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), 60)
                    except asyncio.TimeoutError:
                        await self._housekeeping()
            finally:
                log.info("Shutting down")
                if recorder_task is not None:
                    recorder_task.cancel()
                for dev in self._devices:
                    try:
                        dev.close()
                    except Exception:  # noqa: BLE001
                        pass
                await runner.cleanup()

    def _energy_log_for(self, serial: str) -> Any:
        """The bridge's own energy counter log for a gateway serial, if that device is one of ours."""
        if not self.registry:
            return None
        endpoint = self.registry.endpoint_by_serial(serial)
        return endpoint.energy_log if endpoint is not None else None

    def _service_address(self) -> str:
        cfg = self.cfg.cube
        host = cfg.service_host
        if not host:
            if cfg.host in LOCAL_GATEWAY_NAMES:
                host = "ihost"  # running as an add-on on the gateway itself (host network)
            else:
                host = _local_ip_towards(cfg.host)
        return f"http://{host}:{cfg.port}/webhook"

    async def _initial_sync(self) -> None:
        assert self.client and self.registry
        if not self.client.token:
            self.message = "No gateway token yet: open the web UI and press Get Token"
            log.warning(self.message)
            return
        ok, message = await self.sync()
        self.message = message
        if not ok:
            log.warning(message)

    async def _housekeeping(self) -> None:
        assert self.client and self.registry
        self.registry.sample_energy_all()
        self._remember_pushed_attributes()
        if self.client.token and self.client.token_valid and not self.registry.synced:
            await self.sync()
        await self._recover_moved_appliances()

    OFFLINE_GRACE = 240.0  # seconds an appliance may stay offline before we go looking for it
    RECOVERY_INTERVAL = 600.0  # at most one LAN rediscovery per ten minutes

    async def _recover_moved_appliances(self) -> None:
        """Routers re-lease addresses; when appliances stay offline, look for them on the LAN again."""
        assert self.registry
        if not self.cfg.midea.enabled or not self._devices:
            return
        offline = [ep for ep in self.registry.endpoints.values() if getattr(ep, "device", None) is not None and not ep.online]
        now = time.monotonic()
        if not offline:
            self._offline_since = None
            return
        if self._offline_since is None:
            self._offline_since = now
            return
        if now - self._offline_since < self.OFFLINE_GRACE or now - self._last_recovery < self.RECOVERY_INTERVAL:
            return
        self._last_recovery = now
        from bridge.midea.common import discover_devices

        loop = asyncio.get_running_loop()
        infos = await loop.run_in_executor(None, discover_devices)
        current = {int(dev.device_id): getattr(dev, "ip_address", None) or getattr(dev, "_ip_address", None) for dev in self._devices}
        moved = [did for did, info in infos.items() if int(did) in current and info.get("ip_address") and info["ip_address"] != current[int(did)]]
        if not moved:
            log.info("%d appliance(s) offline; LAN discovery found no address change", len(offline))
            return
        log.warning("Appliance(s) %s answer from a new address; reconnecting", moved)
        await self.reload_midea()
        self._offline_since = None

    PUSHED_ATTRIBUTES = ("outdoor_temperature", "total_energy_consumption", "total_produced_energy", "status_heating", "status_dhw")

    def _remember_pushed_attributes(self) -> None:
        """Persist the attributes that only arrive by push, so a restart can pre-seed them."""
        seen_file = self.cfg.data_dir / "midea-seen.json"
        try:
            seen = json.loads(seen_file.read_text())
        except Exception:  # noqa: BLE001
            return
        changed = False
        for dev in self._devices:
            entry = seen.get(str(dev.device_id))
            if entry is None:
                continue
            latest = {k: dev.attributes.get(k) for k in self.PUSHED_ATTRIBUTES if dev.attributes.get(k) is not None}
            if latest and latest != entry.get("last_attributes"):
                entry["last_attributes"] = latest
                changed = True
        if changed:
            try:
                seen_file.write_text(json.dumps(seen, indent=2))
            except OSError as exc:
                log.warning("Could not write %s: %s", seen_file, exc)

    def _attach_energy(self, endpoint: Any) -> bool:
        """Give the endpoint an energy log if its appliance has a counter. Returns True if newly attached."""
        from bridge.core.energy import EnergyLog

        if endpoint.energy_log is not None:
            return False
        value = endpoint.energy_counter_kwh()
        if value is None:
            return False
        endpoint.energy_log = EnergyLog(self.cfg.data_dir / "energy" / f"{endpoint.third_serial}.json")
        endpoint.energy_log.record(value)
        log.info("Energy metering enabled for %s (counter at %.2f kWh)", endpoint.name, value)
        return True

    # ----- gateway actions -----------------------------------------------------

    async def sync(self) -> tuple[bool, str]:
        assert self.client and self.registry
        if not self.client.token:
            return False, "no gateway token"
        try:
            mapping = await self.registry.sync()
        except CubeAuthError as exc:
            self.message = f"Gateway rejected the token ({exc}); press Get Token"
            return False, self.message
        except CubeError as exc:
            self.message = f"Sync failed: {exc}"
            return False, self.message
        except Exception as exc:  # noqa: BLE001
            self.message = f"Sync failed: {exc}"
            log.exception("sync failed")
            return False, self.message
        # Endpoints that are offline right now must be reported as such; the
        # gateway assumes online after a DiscoveryRequest.
        for ep in self.registry.endpoints.values():
            if not ep.online:
                try:
                    await self.client.report_online(mapping.get(ep.third_serial) or self.registry.serial_of(ep) or "", ep.third_serial, False)
                except Exception as exc:  # noqa: BLE001
                    log.debug("offline report failed: %s", exc)
        self.message = f"Synced {len(mapping)} device(s) with the gateway"
        await self._ensure_cards()
        return True, self.message

    async def _ensure_cards(self) -> None:
        """Register the bridge's custom UI cards on the gateway (idempotent)."""
        from bridge.cards.heatpump import CARD_LABEL, card_data
        from bridge.cards.heatpump import CardRegistrar

        assert self.client and self.registry
        if not self.cfg.cards.heatpump or not card_data(self.registry.endpoints).get("found"):
            return
        base = await self._public_base_url()
        try:
            await CardRegistrar(self.client, self.cfg.data_dir).ensure(CARD_LABEL, base, "/cards/heatpump", "/cards/heatpump/drawer")
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not register the heat pump card: %s", exc)

    async def _public_base_url(self) -> str:
        """Address a browser on the LAN can use to reach this bridge (for card iframes)."""
        cfg = self.cfg.cube
        host = cfg.public_host
        if not host:
            if cfg.host in LOCAL_GATEWAY_NAMES:
                try:
                    host = (await self.client.bridge_info()).get("ip") if self.client else None  # add-on: the gateway's own IP
                except Exception:  # noqa: BLE001
                    host = None
            host = host or _local_ip_towards(cfg.host if cfg.host not in LOCAL_GATEWAY_NAMES else "8.8.8.8")
        return f"http://{host}:{cfg.port}"

    def request_token(self) -> bool:
        """Start polling the gateway for a token; returns False if already polling."""
        if self._token_task and not self._token_task.done():
            return False
        self._token_task = asyncio.ensure_future(self._token_flow())
        return True

    async def _token_flow(self) -> None:
        assert self.client
        self.message = "Waiting for you to press Allow on the iHost (5 minutes)"
        log.info(self.message)
        if await self.client.wait_for_token():
            await self.sync()
        else:
            self.message = "Timed out waiting for the token; press Get Token again"
            log.warning(self.message)

    # ----- setup from the web UI (the add-on has no other way to receive files) -----

    def save_keys(self, text: str) -> int:
        """Store a midea-devices.json (midea-local cache format, or a bare list of entries). Returns entry count."""
        data = json.loads(text)
        entries = data.get("devices") if isinstance(data, dict) else data
        if not isinstance(entries, list) or not entries:
            raise ValueError('expected {"devices": [{"device_id", "token", "key"}, ...]}')
        for entry in entries:
            if not all(k in entry for k in ("device_id", "token", "key")):
                raise ValueError("every entry needs device_id, token and key")
        path = self.cfg.midea.keys_file or (self.cfg.data_dir / "midea-devices.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"devices": entries}, indent=2))
        try:
            path.chmod(0o600)
        except OSError:
            pass
        self.cfg.midea.keys_file = path
        log.info("Saved Midea keys for %d device(s) to %s", len(entries), path)
        return len(entries)

    def save_config(self, text: str) -> None:
        """Store config.yaml and re-read the sections that can change without a restart."""
        import yaml

        from bridge.config import load

        parsed = yaml.safe_load(text) if text.strip() else {}
        if parsed is not None and not isinstance(parsed, dict):
            raise ValueError("config must be a YAML mapping")
        path = self.cfg.data_dir / "config.yaml"
        path.write_text(text)
        fresh = load(str(path))
        self.cfg.midea = fresh.midea
        self.cfg.cards = fresh.cards
        log.info("Saved config to %s", path)

    async def reload_midea(self) -> tuple[bool, str]:
        """Drop every Midea device and set them up again (after keys or config changed)."""
        assert self.registry
        for dev in self._devices:
            try:
                dev.close()
            except Exception:  # noqa: BLE001
                pass
        for ep in [ep for ep in self.registry.endpoints.values() if getattr(ep, "device", None) is not None]:
            self.registry.remove(ep.third_serial)
        self._devices = []
        if self.cfg.midea.enabled:
            await self._setup_midea()
        count = len(self.registry.endpoints)
        if self.client and self.client.token:
            ok, message = await self.sync()
            return ok, f"{count} endpoint(s) set up; {message}"
        self.message = f"{count} endpoint(s) set up; press Get Token to register them with the gateway"
        return True, self.message

    def status(self) -> dict[str, Any]:
        assert self.client and self.registry
        keys_file = self.cfg.midea.keys_file
        return {
            "version": __version__,
            "message": self.message,
            "setup": {
                "keys_file": str(keys_file) if keys_file else None,
                "keys_present": bool(keys_file and keys_file.is_file()),
                "config_file": str(self.cfg.data_dir / "config.yaml"),
                "config_present": (self.cfg.data_dir / "config.yaml").is_file(),
                "appliances": len(self._devices),
            },
            "cube": {
                "host": self.client.host,
                "service_address": self.registry.service_address,
                "token_valid": self.client.token_valid,
                "has_token": bool(self.client.token),
                "waiting_for_token": bool(self._token_task and not self._token_task.done()),
                "last_error": self.client.last_error or self.registry.last_sync_error,
                "synced": self.registry.synced,
            },
            "endpoints": self.registry.status(),
            "recorder": self.recorder.info() if self.recorder is not None else {"recorder": False},
        }

    async def debug_query(self, device_id: int, body_type: int) -> str:
        """Send a raw read-only query with the given body type to a Midea device (protocol exploration).

        The reply, if any, shows up in the log as a `process message <hex>` line for that device.
        """
        from midealocal.devices.c3.message import MessageQuery
        from midealocal.message import ListTypes

        device = next((d for d in self._devices if d.device_id == device_id), None)
        if device is None:
            raise ValueError(f"no device {device_id}")
        cmd = MessageQuery(device._message_protocol_version, ListTypes(body_type))  # noqa: SLF001
        await asyncio.get_running_loop().run_in_executor(None, device.build_send, cmd)
        return f"sent query body_type=0x{body_type:02X} to {device.name}"

    def raw_attributes(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for dev in self._devices:
            out[f"{dev.name} ({dev.device_type.name} {dev.device_id})"] = {
                "model": dev.model,
                "available": dev.available,
                "attributes": dict(dev.attributes),
            }
        return out

    # ----- Midea -----------------------------------------------------------------

    async def _setup_midea(self) -> None:
        from bridge.midea import make_endpoints
        from bridge.midea.common import build_device, connect_device, discover_devices, load_keys, type_name

        assert self.registry
        cfg = self.cfg.midea
        loop = asyncio.get_running_loop()

        keys: dict[int, tuple[str, str]] = {}
        if cfg.keys_file and cfg.keys_file.is_file():
            keys = load_keys(cfg.keys_file)
            log.info("Loaded Midea keys for %d device(s) from %s", len(keys), cfg.keys_file)
        else:
            log.error("Midea key file not found at %s; run `midealocal discover` with your account once and copy midea-devices.json there", cfg.keys_file)

        infos: dict[int, dict[str, Any]] = {}
        if cfg.discover:
            log.info("Discovering Midea devices on the LAN...")
            infos = await loop.run_in_executor(None, discover_devices)
        for static in cfg.devices:
            if static.device_id not in infos and static.ip:
                infos.update(await loop.run_in_executor(None, discover_devices, [static.ip]))
        log.info("Midea discovery found %d device(s)", len(infos))

        # A unit that missed one discovery broadcast must not vanish from the gateway: fall back to
        # what we saw last time and let the device thread keep retrying the connection.
        seen_file = self.cfg.data_dir / "midea-seen.json"
        seen: dict[str, dict[str, Any]] = {}
        try:
            seen = json.loads(seen_file.read_text())
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s: %s", seen_file, exc)
        for device_id, info in list(seen.items()):
            did = int(device_id)
            if did in infos or did not in keys:
                continue
            retry = await loop.run_in_executor(None, discover_devices, [info.get("ip_address")]) if info.get("ip_address") else {}
            if did in retry:
                infos[did] = retry[did]
                log.info("Midea %s answered a targeted probe at its last address", device_id)
            else:
                infos[did] = dict(info)
                log.warning("Midea %s did not answer discovery; using its last known address %s", device_id, info.get("ip_address"))
        for did, info in infos.items():
            entry = {k: v for k, v in info.items() if isinstance(v, (str, int, float, bool)) or v is None}
            previous = seen.get(str(did)) or {}
            if previous.get("last_attributes"):
                entry["last_attributes"] = previous["last_attributes"]  # keep the pushed values across runs
            seen[str(did)] = entry
        try:
            seen_file.write_text(json.dumps(seen, indent=2))
        except OSError as exc:
            log.warning("Could not write %s: %s", seen_file, exc)

        for device_id, info in infos.items():
            tk = keys.get(device_id)
            label = f"{type_name(info)} {device_id} at {info.get('ip_address')}"
            if not tk:
                log.warning("No token/key for Midea %s; skipping", label)
                continue
            override = cfg.device_config(device_id)
            name = (override.name if override and override.name else None) or f"Midea {type_name(info)} {info.get('model') or device_id}"
            device = build_device(info, tk[0], tk[1], name)
            if device is None:
                log.info("Unsupported Midea device type for %s; skipping", label)
                continue
            online = await loop.run_in_executor(None, connect_device, device)
            # Values the unit only pushes on its own schedule (hourly energy report): start from the
            # last ones we saw so the dependent endpoints exist right away instead of an hour later.
            for key, value in (seen.get(str(device_id), {}).get("last_attributes") or {}).items():
                if device.attributes.get(key) is None:
                    device._attributes[key] = value  # noqa: SLF001
            endpoints = make_endpoints(device, name)
            if not endpoints:
                log.info("No endpoints produced for %s", label)
                continue
            for ep in endpoints:
                ep.online = online
                self._attach_energy(ep)
                self.registry.add(ep)
            # Some data only arrives later (pushed energy reports, late sensors): grow the endpoint set as it does.
            device.register_update(lambda status, dev=device, nm=name: self._schedule_late_endpoints(dev, nm))
            device.open()  # background thread: keeps the socket alive and polls status
            self._devices.append(device)
            log.info("Midea %s ready as '%s' (%s)", label, name, "online" if online else "offline, will retry")

    def _schedule_late_endpoints(self, device: Any, name: str) -> None:
        # Called from the device thread.
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._add_late_endpoints(device, name)))
        except RuntimeError:
            pass

    async def _add_late_endpoints(self, device: Any, name: str) -> None:
        from bridge.midea import make_endpoints

        assert self.registry
        if self._late_sync_running:
            return
        new = make_endpoints(device, name, existing=set(self.registry.endpoints))
        # Existing endpoints whose energy counter has just started reporting need the capability added too.
        gained_energy = [
            ep for ep in self.registry.endpoints.values()
            if getattr(ep, "device", None) is device and self._attach_energy(ep)
        ]
        if not new and not gained_energy:
            return
        self._late_sync_running = True
        try:
            for ep in new:
                ep.online = bool(device.available)
                self._attach_energy(ep)
                self.registry.add(ep)
                log.info("Endpoint created from data that arrived after startup: %s", ep.name)
            if self.client and self.client.token:
                await self.sync()
        finally:
            self._late_sync_running = False


def _local_ip_towards(host: str) -> str:
    """The local interface address used to reach `host` (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 80))
        return s.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        s.close()
