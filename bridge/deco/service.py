"""Reads the Deco mesh with the admin password, presents the internet connection to the
gateway as a sensor, and adds or removes fixed-IP (DHCP) reservations on request.

The Deco allows one admin session at a time and locks the login after repeated
wrong passwords, so the service keeps one session open, logs in again only when a
call fails, never more often than once a minute, and stops trying altogether after
the Deco refuses the password, until the password is saved again.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bridge.config import DecoConfig
from bridge.core.endpoint import Endpoint, cap

log = logging.getLogger(__name__)

POLL_INTERVAL = 60            # clients, units and internet status
RESERVATION_INTERVAL = 300    # the reservation table changes only when someone edits it
MIN_LOGIN_GAP = 60            # never log in more often than this
LOGIN_BACKOFF = 1800          # after three logins in a row fail for network reasons
STALE_AFTER = 600             # no good answer for this long: the internet sensor goes offline
FORGET_AFTER = 7 * 86400      # offline clients stay listed this long after they were last seen

MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")

# Names the Deco shows for smart-home gear, so apps can suggest pinning their address.
SMART_HOME_NAMES = re.compile(
    r"yeelight|yeelink|midea|net_ac|switchbot|sonoff|ewelink|esp[_-]|espressif|tasmota|shelly|ihost|tuya|tado|hue|ledvance",
    re.I,
)


class DecoUnavailable(Exception):
    """The Deco cannot be asked right now (not set up, password refused, waiting before the next login)."""


def normalise_mac(value: str) -> str:
    mac = str(value or "").strip().upper().replace("-", ":")
    if not MAC_RE.match(mac):
        raise ValueError(f"not a MAC address: {value!r}")
    return mac


def _mac(value: str) -> str:
    """For display: the normalised MAC, or the raw value when the Deco sends something odd."""
    try:
        return normalise_mac(value)
    except ValueError:
        return str(value or "")


class InternetEndpoint(Endpoint):
    """The home's internet connection as a gateway contact sensor: closed while online, open when down."""

    display_category = "contactSensor"
    manufacturer = "TP-Link"
    model = "Deco"

    def __init__(self, third_serial: str, name: str) -> None:
        super().__init__(third_serial, name)
        self.connected = True
        self.online = False

    def capabilities(self) -> list[dict[str, Any]]:
        return [cap("contact", "0100")]

    def state(self) -> dict[str, Any]:
        return {"contact": {"contact": not self.connected}}

    async def apply(self, state: dict[str, Any]) -> None:
        raise ValueError("the internet sensor is read-only")

    def update(self, connected: bool) -> None:
        changed = connected != self.connected
        self.connected = connected
        self.set_online(True)
        if changed:
            self.notify_state()


@dataclass
class Snapshot:
    fetched_at: float = 0.0
    units: list[dict[str, Any]] = field(default_factory=list)
    internet: dict[str, Any] | None = None
    lan: dict[str, str] | None = None  # the Deco's own LAN address and mask
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)  # by MAC, includes recently seen ones
    reservations: list[dict[str, str]] = field(default_factory=list)
    reservation_max: int = 0
    reservations_at: float = 0.0


class DecoService:
    def __init__(self, data_dir: Path, cfg: DecoConfig, client_factory: Callable[[str, str, str], Any] | None = None) -> None:
        self.data_dir = data_dir
        self.cfg = cfg
        self.settings_file = data_dir / "deco.json"
        self._client_factory = client_factory or _sdk_client
        self.host: str | None = None
        self._password: str | None = None
        self._username = "admin"
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._last_login = 0.0
        self._login_failures = 0
        self._next_login = 0.0
        self.auth_refused: str | None = None
        self.error: str | None = None
        self.snap = Snapshot()
        self._last_good = 0.0
        self.endpoint: InternetEndpoint | None = None
        self.on_endpoint_change: Callable[[], Awaitable[None]] | None = None
        # Filled in by the app: IP -> label for devices it knows (the gateway, Midea units).
        self.known_ips: Callable[[], dict[str, str]] = dict
        self._load_settings()

    # ----- settings -------------------------------------------------------------

    def _saved(self) -> dict[str, Any]:
        try:
            data = json.loads(self.settings_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _load_settings(self) -> None:
        saved = self._saved()
        self.host = (saved.get("host") if "host" in saved else self.cfg.host) or None
        self._password = (saved.get("password") if "password" in saved else self.cfg.password) or None
        self._username = str(saved.get("username") or self.cfg.username or "admin")
        self._client = None
        self.auth_refused = None
        self.error = None
        self._login_failures = 0
        self._next_login = 0.0
        if not self.configured:
            self.snap = Snapshot()

    def save_settings(self, body: dict[str, Any]) -> None:
        """Host and password from the setup page. A blank password keeps the stored one; an empty host
        switches the Deco off and forgets the password."""
        saved = self._saved()
        host = str(body.get("host") or "").strip()
        password = str(body.get("password") or "")
        if host:
            if not re.match(r"^[A-Za-z0-9.\-]+(:\d+)?$", host):
                raise ValueError("the Deco address should look like 192.168.68.1")
            # A new address needs the password typed again: otherwise anyone who can reach the setup
            # page could point the bridge at their own machine and receive the stored password.
            if host != (saved.get("host") or self.cfg.host) and not password:
                raise ValueError("type the admin password again when changing the Deco address")
            saved["host"] = host
            if password:
                saved["password"] = password
            elif not saved.get("password") and not self.cfg.password:
                raise ValueError("the Deco admin password is needed")
        else:
            saved = {"host": None, "password": None}
        self.settings_file.write_text(json.dumps(saved, indent=2))
        try:
            self.settings_file.chmod(0o600)
        except OSError:
            pass
        self._load_settings()
        self._last_login = 0.0  # someone just pressed Save: try the new settings right away

    @property
    def configured(self) -> bool:
        return bool(self.host and self._password)

    # ----- talking to the Deco ------------------------------------------------------

    async def _call(self, fn: Callable[[Any], Any]) -> Any:
        """Run one SDK call in a worker thread (the SDK is synchronous), logging in first if needed.
        A failed call drops the session; the next call logs in again, within the login limits."""
        client = await self._session()
        try:
            return await asyncio.to_thread(fn, client)
        except Exception:
            self._client = None
            raise

    async def _session(self) -> Any:
        if not self.configured:
            raise DecoUnavailable("not set up")
        if self.auth_refused:
            raise DecoUnavailable(self.auth_refused)
        if self._client is not None:
            return self._client
        now = time.monotonic()
        earliest = max(self._next_login, self._last_login + MIN_LOGIN_GAP if self._last_login else 0.0)
        wait = earliest - now
        if wait > 0:
            raise DecoUnavailable(f"waiting {int(wait)} s before logging in again")
        from tplink_deco_api import ApiError, AuthenticationError

        self._last_login = now
        client = self._client_factory(str(self.host), self._username, str(self._password))
        try:
            await asyncio.to_thread(client.login)
        except (ApiError, AuthenticationError) as exc:
            # A refused password must not be retried: the Deco locks the login after a few attempts.
            code = getattr(exc, "error_code", None)
            self.auth_refused = f"the Deco refused the admin password{f' (code {code})' if code is not None else ''}; save it again on the setup page"
            log.error("Deco: %s", self.auth_refused)
            raise DecoUnavailable(self.auth_refused) from exc
        except Exception as exc:
            self._login_failures += 1
            if self._login_failures >= 3:
                self._next_login = now + LOGIN_BACKOFF
            raise DecoUnavailable(f"could not reach the Deco at {self.host}: {exc}") from exc
        self._login_failures = 0
        self._client = client
        log.info("Deco: logged in to %s", self.host)
        return client

    # ----- the loop ---------------------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(stop.wait(), 15)
            except asyncio.TimeoutError:
                pass

    async def tick(self, force: bool = False) -> None:
        if not self.configured:
            return
        async with self._lock:
            now = time.time()
            if force or now - self.snap.fetched_at >= POLL_INTERVAL:
                await self._poll()
            if self.error is None and (force or now - self.snap.reservations_at >= RESERVATION_INTERVAL):
                await self._read_reservations()
            if self.endpoint is not None and time.time() - self._last_good > STALE_AFTER:
                self.endpoint.set_online(False)

    async def _poll(self) -> None:
        snap = self.snap
        snap.fetched_at = time.time()
        try:
            units = await self._call(lambda c: c.get_device_list())
            internet = await self._call(lambda c: c.get_internet_status())
            if snap.lan is None:
                wan = await self._call(lambda c: c.get_wan_info())
                if wan.lan.ip_info.ip and wan.lan.ip_info.mask:
                    snap.lan = {"ip": wan.lan.ip_info.ip, "mask": wan.lan.ip_info.mask}
            # One list per Deco unit, so every client is tied to the Deco it is connected to.
            per_unit: list[tuple[Any, list[Any]]] = []
            for unit in units:
                per_unit.append((unit, await self._call(lambda c, mac=unit.mac: c.get_client_list(mac.replace(":", "-")))))
        except DecoUnavailable as exc:
            self.error = str(exc)
            return
        except Exception as exc:  # noqa: BLE001
            self.error = f"the Deco did not answer: {exc}"
            log.warning("Deco: %s", self.error)
            return
        self.error = None
        self._last_good = snap.fetched_at
        snap.units = [_unit(u) for u in units]
        snap.internet = {
            "online": internet.ipv4.inet_status == "online" or internet.ipv6.inet_status == "online",
            "ipv4": internet.ipv4.inet_status,
            "ipv6": internet.ipv6.inet_status,
            "link": internet.link_status,
            "connect_type": internet.ipv4.connect_type,
        }
        seen_now: set[str] = set()
        for unit, clients in per_unit:
            unit_name = _unit_name(unit)
            for client in clients:
                if client.client_mesh:
                    continue  # another Deco unit, not a client
                entry = _client(client, unit.mac, unit_name)
                seen_now.add(entry["mac"])
                snap.clients[entry["mac"]] = entry
        for mac, entry in list(snap.clients.items()):
            if mac not in seen_now:
                entry["online"] = False
                if snap.fetched_at - entry.get("last_seen", 0) > FORGET_AFTER:
                    del snap.clients[mac]
        await self._present(snap.internet["online"])

    async def _read_reservations(self) -> None:
        try:
            table = await self._call(lambda c: c.address_reservations.get_table())
        except DecoUnavailable as exc:
            self.error = str(exc)
            return
        except Exception as exc:  # noqa: BLE001
            self.error = f"could not read the reservations: {exc}"
            log.warning("Deco: %s", self.error)
            return
        self.snap.reservations = [{"mac": _mac(r.mac), "ip": r.ip} for r in table.reservations]
        self.snap.reservation_max = int(table.max_count or 0)
        self.snap.reservations_at = time.time()

    async def _present(self, connected: bool) -> None:
        changed = False
        if self.endpoint is None:
            self.endpoint = InternetEndpoint("deco-internet", self.cfg.name or "Internet")
            changed = True
        self.endpoint.update(connected)
        if changed and self.on_endpoint_change is not None:
            await self.on_endpoint_change()

    # ----- reservations ---------------------------------------------------------------------

    def _lan_network(self) -> ipaddress.IPv4Network | None:
        lan = self.snap.lan
        if not lan:
            return None
        try:
            return ipaddress.IPv4Network(f"{lan['ip']}/{lan['mask']}", strict=False)
        except ValueError:
            return None

    def check_reservation(self, mac: str, ip: str) -> tuple[str, str]:
        """Validate one reservation against what the Deco has now. Returns (mac, ip) normalised."""
        mac = normalise_mac(mac)
        try:
            addr = ipaddress.IPv4Address(str(ip).strip())
        except ValueError:
            raise ValueError(f"not an IPv4 address: {ip!r}") from None
        net = self._lan_network()
        if net is None:
            raise ValueError("the Deco's LAN range is not known yet; try again in a minute")
        if addr not in net or addr in (net.network_address, net.broadcast_address):
            raise ValueError(f"{addr} is outside the home network {net}")
        if self.snap.lan and str(addr) == self.snap.lan["ip"]:
            raise ValueError(f"{addr} is the Deco's own address")
        for r in self.snap.reservations:
            if r["ip"] == str(addr) and r["mac"] != mac:
                raise ValueError(f"{addr} is already reserved for another device ({r['mac']})")
        for c in self.snap.clients.values():
            if c["online"] and c["ip"] == str(addr) and c["mac"] != mac:
                raise ValueError(f"{addr} is in use by {c['name'] or c['mac']}")
        return mac, str(addr)

    async def reserve(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Add or change reservations. Every item is checked before anything is written."""
        from tplink_deco_api import AddressReservation

        async with self._lock:
            await self._read_reservations()
            if self.error:
                raise DecoUnavailable(self.error)
            wanted: list[tuple[str, str]] = []
            ips_taken: dict[str, str] = {}
            for item in items:
                mac, ip = self.check_reservation(str(item.get("mac") or ""), str(item.get("ip") or ""))
                if ips_taken.get(ip, mac) != mac:
                    raise ValueError(f"{ip} is asked for twice")
                ips_taken[ip] = mac
                wanted.append((mac, ip))
            current = {r["mac"]: r["ip"] for r in self.snap.reservations}
            adds = [(m, i) for m, i in wanted if m not in current]
            changes = [(m, i) for m, i in wanted if m in current and current[m] != i]
            unchanged = [m for m, i in wanted if current.get(m) == i]
            limit = self.snap.reservation_max
            if limit and len(current) + len(adds) > limit:
                raise ValueError(f"the Deco holds at most {limit} reservations; {len(current)} are in use")
            done: list[str] = []
            try:
                for mac, ip in changes:
                    await self._call(lambda c, m=mac, i=ip: c.address_reservations.modify(AddressReservation(m, current[m]), AddressReservation(m, i)))
                    done.append(mac)
                if adds:
                    for mac, ip in adds:
                        await self._call(lambda c, m=mac, i=ip: c.address_reservations.add(AddressReservation(m, i)))
                        done.append(mac)
            finally:
                await self._read_reservations()
            log.info("Deco: reserved %d address(es), changed %d, %d already in place", len(adds), len(changes), len(unchanged))
            return {"added": len(adds), "changed": len(changes), "unchanged": len(unchanged), "reservations": self.snap.reservations}

    async def unreserve(self, mac: str) -> dict[str, Any]:
        mac = normalise_mac(mac)
        async with self._lock:
            await self._read_reservations()
            if self.error:
                raise DecoUnavailable(self.error)
            if not any(r["mac"] == mac for r in self.snap.reservations):
                raise LookupError(f"no reservation for {mac}")
            try:
                await self._call(lambda c: c.address_reservations.remove(mac))
            finally:
                await self._read_reservations()
            log.info("Deco: removed the reservation for %s", mac)
            return {"reservations": self.snap.reservations}

    # ----- what the HTTP API serves ---------------------------------------------------------------

    def network(self) -> dict[str, Any]:
        snap = self.snap
        reserved = {r["mac"]: r["ip"] for r in snap.reservations}
        known = self.known_ips()
        clients = []
        for c in snap.clients.values():
            label = known.get(c["ip"])
            clients.append(
                {
                    **c,
                    "reserved_ip": reserved.get(c["mac"]),
                    "known_as": label,
                    "smart_home": bool(label) or bool(SMART_HOME_NAMES.search(c["name"] or "")),
                }
            )
        listed = {c["mac"] for c in clients}
        for mac, ip in reserved.items():
            if mac not in listed:  # reserved, but not seen since the bridge started
                clients.append({"mac": mac, "name": "", "ip": ip, "online": False, "reserved_ip": ip, "known_as": known.get(ip), "smart_home": bool(known.get(ip))})
        clients.sort(key=lambda c: (not c["online"], (c.get("known_as") or c["name"] or c["mac"]).casefold()))
        net = self._lan_network()
        return {
            "configured": self.configured,
            "host": self.host,
            "error": self.auth_refused or self.error,
            "fetched_at": snap.fetched_at or None,
            "internet": snap.internet,
            "lan": {"address": snap.lan["ip"], "network": str(net)} if snap.lan and net else None,
            "units": snap.units,
            "clients": clients,
            "reservations": {"items": snap.reservations, "max": snap.reservation_max or None},
            "serial": self.endpoint.third_serial if self.endpoint else None,
        }

    def status(self) -> dict[str, Any]:
        """For the setup page. Never includes the password."""
        return {
            "host": self.host,
            "configured": self.configured,
            "error": self.auth_refused or self.error,
            "units": len(self.snap.units),
            "clients_online": sum(1 for c in self.snap.clients.values() if c["online"]),
            "reservations": len(self.snap.reservations),
            "internet": (self.snap.internet or {}).get("online"),
            "fetched_at": self.snap.fetched_at or None,
        }


# Deco firmware (seen on an X20, 1.1.8) offers only TLS 1.2 with RSA key exchange (AES128-GCM-SHA256),
# which Python's default cipher list leaves out, so the handshake fails. Allow those ciphers for the Deco
# only. The certificate is self-signed and not verified anyway; the payloads are also encrypted by the
# Deco's own RSA+AES scheme.
DECO_CIPHERS = "DEFAULT:AES128-GCM-SHA256:AES256-GCM-SHA384"


def _sdk_client(host: str, username: str, password: str) -> Any:
    from tplink_deco_api import DecoClient

    client = DecoClient(host, username, password, timeout=10.0)
    ctx = getattr(getattr(client, "_transport", None), "_ssl_ctx", None)
    if ctx is not None:
        try:
            ctx.set_ciphers(DECO_CIPHERS)
        except Exception as exc:  # noqa: BLE001
            log.warning("Deco: could not widen the TLS cipher list (%s); the login may fail", exc)
    else:
        log.warning("Deco: the SDK no longer exposes its TLS settings; the login may fail on older firmware")
    return client


def _unit_name(unit: Any) -> str:
    return unit.custom_nickname or unit.nickname or unit.device_model or unit.mac


def _unit(unit: Any) -> dict[str, Any]:
    return {
        "mac": _mac(unit.mac),
        "name": _unit_name(unit),
        "model": unit.device_model,
        "role": unit.role,  # "master" is the main Deco, the one wired to the modem
        "ip": unit.device_ip,
        "firmware": unit.software_ver,
        "hardware": unit.hardware_ver,
        "online": unit.group_status != "disconnected",
        "internet": unit.inet_status,
    }


def _client(client: Any, unit_mac: str, unit_name: str) -> dict[str, Any]:
    wired = str(client.wire_type).lower() == "wired"
    return {
        "mac": _mac(client.mac),
        "name": client.name,
        "ip": client.ip,
        "online": bool(client.online),
        "wired": wired,
        "band": None if wired else client.connection_type or None,  # band2_4, band5, band6
        "deco_mac": _mac(unit_mac),
        "deco": unit_name,
        "client_type": client.client_type or None,
        # As the Deco reports them; the unit is not documented (the existing integrations treat it as bytes/s).
        "up_speed": client.up_speed,
        "down_speed": client.down_speed,
        "last_seen": time.time(),
    }
