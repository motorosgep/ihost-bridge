"""aiohttp application: the gateway's callback endpoint plus a small status page."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from bridge.history_api import history_routes
from bridge.climate_api import climate_routes
from bridge.network_api import network_routes
from bridge.solar_api import solar_routes

if TYPE_CHECKING:
    from bridge.app import BridgeApp

log = logging.getLogger(__name__)


# Changes that need no credentials: asking the gateway for a token (someone still has to press Allow on
# the iHost), and the PIN routes themselves. The network routes check the caller's gateway token themselves.
OPEN_WRITES = {"/api/token", "/api/auth/setup", "/api/auth/unlock", "/api/auth/reset"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def caller_allowed(request: web.Request) -> bool:
    """A setup-page session or an app's gateway access token."""
    bridge: BridgeApp = request.app["bridge"]
    if bridge.auth.session_ok(request.headers.get("X-Bridge-Session")):
        return True
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else None
    return bool(token) and await bridge.hub_token_ok(token)


@web.middleware
async def guard(request: web.Request, handler):  # noqa: ANN001
    """Everything that changes a device, the router or the settings needs a PIN session or a gateway token."""
    if request.method in SAFE_METHODS:
        return await handler(request)
    bridge: BridgeApp = request.app["bridge"]
    path = request.path
    if path == "/webhook":
        if not bridge.webhook_source_ok(request.remote):
            return web.json_response({"error": "directives are accepted from the gateway only"}, status=403)
    elif path not in OPEN_WRITES and not path.startswith("/api/network/") and path != "/api/climate":  # these check the token themselves
        if not await caller_allowed(request):
            return web.json_response(
                {"ok": False, "error": "locked: enter the setup PIN on the bridge's web page (apps send their iHost access token)", "locked": True},
                status=401,
            )
    return await handler(request)


def make_app(bridge: BridgeApp) -> web.Application:
    app = web.Application(middlewares=[guard])
    app["bridge"] = bridge
    app["recorder"] = bridge.recorder
    app.add_routes(history_routes())
    app.add_routes(solar_routes())
    app.add_routes(network_routes())
    app.add_routes(climate_routes())
    app.add_routes(
        [
            web.post("/webhook", webhook),
            web.get("/", index),
            web.get("/api/status", api_status),
            web.post("/api/token", api_token),
            web.get("/api/auth", api_auth),
            web.post("/api/auth/setup", api_auth_setup),
            web.post("/api/auth/unlock", api_auth_unlock),
            web.post("/api/auth/reset", api_auth_reset),
            web.post("/api/sync", api_sync),
            web.get("/api/raw", api_raw),
            web.post("/api/debug/query", api_debug_query),
            web.get("/api/debug/cards", api_debug_cards),
            web.post("/api/control", api_control),
            web.post("/api/setup/keys", api_setup_keys),
            web.post("/api/setup/config", api_setup_config),
            web.post("/api/reload", api_reload),
            web.get("/api/cards/heatpump", api_card_heatpump),
            web.get("/cards/heatpump", card_heatpump),
            web.get("/cards/heatpump/drawer", card_heatpump_drawer),
        ]
    )
    return app


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def card_heatpump(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "heatpump_card.html", headers={"Cache-Control": "no-store"})


async def card_heatpump_drawer(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC_DIR / "heatpump_drawer.html", headers={"Cache-Control": "no-store"})


async def api_card_heatpump(request: web.Request) -> web.Response:
    from bridge.cards.heatpump import card_data

    bridge: BridgeApp = request.app["bridge"]
    return web.json_response(card_data(bridge.registry.endpoints))


async def api_setup_keys(request: web.Request) -> web.Response:
    """Receive the Midea token/key file as text, store it, and reconnect the appliances."""
    bridge: BridgeApp = request.app["bridge"]
    try:
        count = bridge.save_keys(await request.text())
    except (ValueError, TypeError) as exc:
        return web.json_response({"ok": False, "error": f"not a valid key file: {exc}"}, status=400)
    ok, message = await bridge.reload_midea()
    return web.json_response({"ok": ok, "message": f"saved keys for {count} device(s); {message}"})


async def api_setup_config(request: web.Request) -> web.Response:
    """Receive config.yaml as text, store it, and reconnect the appliances with the new settings."""
    bridge: BridgeApp = request.app["bridge"]
    try:
        bridge.save_config(await request.text())
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"ok": False, "error": f"not a valid config: {exc}"}, status=400)
    ok, message = await bridge.reload_midea()
    return web.json_response({"ok": ok, "message": f"config saved; {message}"})


async def api_reload(request: web.Request) -> web.Response:
    bridge: BridgeApp = request.app["bridge"]
    ok, message = await bridge.reload_midea()
    return web.json_response({"ok": ok, "message": message})


async def api_control(request: web.Request) -> web.Response:
    """Apply a CUBE-format state fragment to one endpoint (used by the card drawers)."""
    bridge: BridgeApp = request.app["bridge"]
    try:
        body = await request.json()
        third_serial = str(body["third_serial"])
        state = dict(body["state"])
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "expected {third_serial, state}"}, status=400)
    endpoint = bridge.registry.endpoints.get(third_serial)
    if endpoint is None:
        return web.json_response({"ok": False, "error": f"unknown endpoint {third_serial}"}, status=404)
    log.info("Card control for %s: %s", endpoint.name, state)
    try:
        await endpoint.apply(state)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        log.exception("card control failed")
        return web.json_response({"ok": False, "error": str(exc)}, status=500)
    asyncio.ensure_future(bridge.registry.report(endpoint))
    return web.json_response({"ok": True})


async def api_debug_cards(request: web.Request) -> web.Response:
    """The gateway's custom UI card list, as the gateway reports it."""
    bridge: BridgeApp = request.app["bridge"]
    try:
        cards = await bridge.client.list_cards()
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(exc)}, status=500)
    return web.json_response({"ok": True, "cards": cards})


async def api_debug_query(request: web.Request) -> web.Response:
    """Protocol exploration: {"device_id": ..., "body_type": 16} sends a raw read-only query."""
    bridge: BridgeApp = request.app["bridge"]
    body = await request.json()
    try:
        message = await bridge.debug_query(int(body["device_id"]), int(body["body_type"]))
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response({"ok": True, "message": message})


async def api_raw(request: web.Request) -> web.Response:
    """Raw attribute dump of every connected appliance (debugging aid)."""
    bridge: BridgeApp = request.app["bridge"]
    return web.json_response(bridge.raw_attributes(), dumps=lambda o: __import__("json").dumps(o, default=str, indent=1))


async def webhook(request: web.Request) -> web.Response:
    """Directives from the gateway (UpdateDeviceStates, QueryDeviceStates)."""
    bridge: BridgeApp = request.app["bridge"]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid json"}, status=400)
    directive = body.get("directive") if isinstance(body, dict) else None
    if not directive:
        return web.json_response({"error": "missing directive"}, status=400)
    log.debug("Directive: %s", directive)
    response = await bridge.registry.handle_directive(directive)
    return web.json_response(response)


async def api_auth(request: web.Request) -> web.Response:
    bridge: BridgeApp = request.app["bridge"]
    return web.json_response({"pin_set": bridge.auth.pin_set, "unlocked": bridge.auth.session_ok(request.headers.get("X-Bridge-Session"))})


async def _pin_from(request: web.Request) -> str:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return ""
    return str(body.get("pin") or "") if isinstance(body, dict) else ""


async def api_auth_setup(request: web.Request) -> web.Response:
    """Choose the setup PIN; only while none is set."""
    bridge: BridgeApp = request.app["bridge"]
    try:
        session = bridge.auth.set_first_pin(await _pin_from(request))
    except PermissionError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=409)
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response({"ok": True, "session": session})


async def api_auth_unlock(request: web.Request) -> web.Response:
    from bridge.auth import PinLocked

    bridge: BridgeApp = request.app["bridge"]
    try:
        session = bridge.auth.unlock(await _pin_from(request))
    except PinLocked as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=429)
    if session is None:
        return web.json_response({"ok": False, "error": "wrong PIN" if bridge.auth.pin_set else "no PIN set yet"}, status=401)
    return web.json_response({"ok": True, "session": session})


async def api_auth_reset(request: web.Request) -> web.Response:
    """Forgot the PIN: it is cleared once someone presses Allow on the iHost."""
    bridge: BridgeApp = request.app["bridge"]
    bridge.request_pin_reset()
    return web.json_response({"ok": True, "message": "Open the iHost web console and press Allow within 5 minutes; the PIN is then cleared."})


async def api_status(request: web.Request) -> web.Response:
    bridge: BridgeApp = request.app["bridge"]
    return web.json_response(bridge.status())


async def api_token(request: web.Request) -> web.Response:
    bridge: BridgeApp = request.app["bridge"]
    started = bridge.request_token()
    return web.json_response({"waiting": True, "started": started})


async def api_sync(request: web.Request) -> web.Response:
    bridge: BridgeApp = request.app["bridge"]
    ok, message = await bridge.sync()
    return web.json_response({"ok": ok, "message": message})


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ihost-bridge</title>
<style>
  :root { color-scheme: light dark; font-family: -apple-system, system-ui, sans-serif; }
  body { margin: 0; padding: 1.5rem; max-width: 60rem; }
  h1 { font-size: 1.4rem; margin: 0 0 .5rem; }
  .muted { opacity: .7; }
  .card { border: 1px solid rgba(128,128,128,.35); border-radius: .75rem; padding: 1rem; margin: 1rem 0; }
  .row { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }
  button { font: inherit; padding: .5rem 1rem; border-radius: .5rem; border: 1px solid rgba(128,128,128,.5); background: transparent; cursor: pointer; }
  button.primary { background: #2f6fed; color: #fff; border-color: #2f6fed; }
  .pill { display: inline-block; padding: .1rem .55rem; border-radius: 999px; font-size: .8rem; border: 1px solid rgba(128,128,128,.5); }
  .ok { color: #1a9a4a; } .bad { color: #d23b3b; }
  table { border-collapse: collapse; width: 100%; }
  td, th { text-align: left; padding: .35rem .5rem; border-bottom: 1px solid rgba(128,128,128,.25); vertical-align: top; font-size: .95rem; }
  code { font-size: .85rem; white-space: pre-wrap; word-break: break-word; }
</style>
</head>
<body>
<h1>ihost-bridge</h1>
<div class="muted" id="gateway">Loading…</div>

<div class="card" id="lock" hidden>
  <div class="row"><strong id="lock-title">Setup PIN</strong><span id="lock-state" class="pill">…</span></div>
  <p class="muted" id="lock-hint"></p>
  <div class="row">
    <input id="lock-pin" type="password" inputmode="numeric" style="font:inherit;padding:.4rem;flex:1" placeholder="PIN (at least 6 characters)" autocomplete="off">
    <button class="primary" id="lock-btn" onclick="lockAction()">Unlock</button>
    <button id="lock-reset" onclick="resetPin()">Forgot PIN</button>
  </div>
  <div id="lock-msg" class="muted" style="margin-top:.5rem"></div>
</div>

<div class="card">
  <div class="row">
    <strong>Gateway access</strong>
    <span id="token" class="pill">…</span>
    <button class="primary" id="btn-token" onclick="getToken()">Get Token</button>
    <button id="btn-sync" onclick="sync()">Re-sync devices</button>
  </div>
  <p class="muted" id="hint">
    Press <em>Get Token</em>, then open the iHost web console: a dialog
    "Get eWeLink CUBE access token" appears. Press <em>Allow</em> within 5 minutes.
  </p>
  <div id="msg" class="muted"></div>
</div>

<div class="card">
  <strong>Devices</strong>
  <table id="devices"><thead><tr><th>Name</th><th>Category</th><th>Serial</th><th>Online</th><th>State</th></tr></thead><tbody></tbody></table>
</div>

<div class="card">
  <div class="row"><strong>Setup</strong><span id="setup-state" class="pill">…</span></div>
  <p class="muted">
    Paste the <code>midea-devices.json</code> produced by <code>midealocal discover</code> (the Midea token/key
    cache) and save it; the appliances connect right away. Optionally paste a <code>config.yaml</code> to name
    devices (see <code>config.example.yaml</code>).
  </p>
  <textarea id="keys" rows="5" style="width:100%;font-family:monospace;font-size:.8rem" placeholder='{"devices": [{"device_id": "...", "token": "...", "key": "..."}]}'></textarea>
  <div class="row" style="margin:.4rem 0 1rem"><button class="primary" onclick="saveKeys()">Save keys and connect</button></div>
  <textarea id="config" rows="6" style="width:100%;font-family:monospace;font-size:.8rem" placeholder="midea:&#10;  devices:&#10;    - device_id: 153931628306230&#10;      name: Living room AC"></textarea>
  <div class="row" style="margin:.4rem 0"><button onclick="saveConfig()">Save config and reconnect</button><button onclick="reloadDevices()">Reconnect appliances</button></div>
  <div id="setup-msg" class="muted"></div>
</div>

<div class="card">
  <div class="row"><strong>Solar</strong><span id="solar-state" class="pill">…</span></div>
  <p class="muted">
    Huawei FusionSolar. Paste the plant's <em>kiosk link</em> (FusionSolar: your plant, Kiosk view, switch on, copy link)
    for live output; the plant then appears on the iHost as a sensor. Optionally add a <em>Northbound API</em> account
    (System, Company Management, Northbound Management) for the full history by day, month and year. Without it the
    bridge keeps its own history from today on.
  </p>
  <input id="solar-kiosk" style="width:100%;font:inherit;padding:.4rem" placeholder="https://…fusionsolar.huawei.com/…kiosk?kk=…">
  <div class="row" style="margin:.5rem 0">
    <input id="solar-user" style="font:inherit;padding:.4rem;flex:1" placeholder="API user name (optional)" autocomplete="off">
    <input id="solar-code" type="password" style="font:inherit;padding:.4rem;flex:1" placeholder="System code (blank keeps the saved one)" autocomplete="new-password">
  </div>
  <div class="row" style="margin:.5rem 0">
    <input id="solar-host" style="font:inherit;padding:.4rem;flex:1" placeholder="API host, e.g. eu5.fusionsolar.huawei.com">
    <input id="solar-station" style="font:inherit;padding:.4rem;flex:1" placeholder="Plant code (only if the account reads several)">
  </div>
  <div class="row"><button class="primary" onclick="saveSolar()">Save solar</button></div>
  <div id="solar-msg" class="muted" style="margin-top:.5rem"></div>
</div>

<div class="card">
  <div class="row"><strong>Network</strong><span id="deco-state" class="pill">…</span></div>
  <p class="muted">
    TP-Link Deco. Enter the main Deco's address and the admin password (the one the Deco app asks for). The bridge
    keeps one session open and reads the mesh once a minute; the internet connection appears on the iHost as a
    sensor (closed while online). Apps paired with the iHost, such as Butter, then list the clients and can pin
    fixed addresses. Signing in to the Deco's own web page signs the bridge out for a minute.
  </p>
  <div class="row" style="margin:.5rem 0">
    <input id="deco-host" style="font:inherit;padding:.4rem;flex:1" placeholder="Deco address, e.g. 192.168.68.1" autocomplete="off">
    <input id="deco-password" type="password" style="font:inherit;padding:.4rem;flex:1" placeholder="Admin password (blank keeps the saved one)" autocomplete="new-password">
  </div>
  <div class="row"><button class="primary" onclick="saveDeco()">Save and connect</button><button onclick="saveDeco(true)">Switch off</button></div>
  <div id="deco-msg" class="muted" style="margin-top:.5rem"></div>
</div>

<script>
// Changes need a session for the setup PIN; reading does not.
let pinSet = true;
function session() { try { return localStorage.getItem('bridge-session') || ''; } catch (e) { return ''; } }
async function bfetch(url, opts = {}) {
  const r = await fetch(url, {...opts, headers: {...(opts.headers || {}), 'X-Bridge-Session': session()}});
  if (r.status === 401) { checkLock(); }
  return r;
}
async function checkLock() {
  const a = await (await fetch('/api/auth', {headers: {'X-Bridge-Session': session()}})).json();
  pinSet = a.pin_set;
  const card = document.getElementById('lock');
  card.hidden = a.unlocked;
  document.getElementById('lock-title').textContent = a.pin_set ? 'Locked' : 'Choose a setup PIN';
  document.getElementById('lock-state').textContent = a.pin_set ? 'enter the PIN to make changes' : 'no PIN yet';
  document.getElementById('lock-state').className = 'pill bad';
  document.getElementById('lock-btn').textContent = a.pin_set ? 'Unlock' : 'Set PIN';
  document.getElementById('lock-reset').hidden = !a.pin_set;
  document.getElementById('lock-hint').textContent = a.pin_set
    ? 'Changes (devices, keys, solar, router) need the setup PIN. Reading works without it.'
    : 'Anyone on the home network can open this page, so changes are protected by a PIN. Choose one now (at least 6 characters); this browser stays unlocked.';
}
async function lockAction() {
  const pin = document.getElementById('lock-pin').value;
  const r = await fetch(pinSet ? '/api/auth/unlock' : '/api/auth/setup', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({pin})});
  const j = await r.json();
  document.getElementById('lock-pin').value = '';
  if (j.ok) { try { localStorage.setItem('bridge-session', j.session); } catch (e) {} document.getElementById('lock-msg').textContent = ''; }
  else document.getElementById('lock-msg').textContent = 'Error: ' + j.error;
  checkLock();
}
async function resetPin() {
  const j = await (await fetch('/api/auth/reset', {method: 'POST'})).json();
  document.getElementById('lock-msg').textContent = j.message;
}
checkLock();
</script>

<script>
let decoFilled = false;
async function saveDeco(off) {
  const v = id => document.getElementById(id).value;
  document.getElementById('deco-msg').textContent = off ? 'Switching off…' : 'Connecting…';
  const r = await bfetch('/api/setup/deco', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(off ? {host: ''} : {host: v('deco-host').trim(), password: v('deco-password')})});
  const j = await r.json();
  document.getElementById('deco-msg').textContent = j.ok ? j.message : ('Error: ' + j.error);
  document.getElementById('deco-password').value = '';
  if (off) document.getElementById('deco-host').value = '';
  refresh();
}
function showDeco(d) {
  const st = document.getElementById('deco-state');
  if (!d || !d.configured) { st.textContent = 'not set up'; st.className = 'pill'; }
  else if (d.error) { st.textContent = d.error; st.className = 'pill bad'; }
  else if (!d.fetched_at) { st.textContent = 'connecting…'; st.className = 'pill'; }
  else {
    st.textContent = `${d.units} Deco unit(s) · ${d.clients_online} client(s) online · ${d.reservations} fixed address(es) · internet ${d.internet ? 'up' : 'down'}`;
    st.className = 'pill ' + (d.internet ? 'ok' : 'bad');
  }
  if (d && !decoFilled) { decoFilled = true; document.getElementById('deco-host').value = d.host || ''; }
}
</script>

<script>
let solarFilled = false;
async function saveSolar() {
  const v = id => document.getElementById(id).value.trim();
  document.getElementById('solar-msg').textContent = 'Saving…';
  const r = await bfetch('/api/setup/solar', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
    kiosk_url: v('solar-kiosk'),
    api: {user_name: v('solar-user'), system_code: v('solar-code'), host: v('solar-host'), station_code: v('solar-station')},
  })});
  const j = await r.json();
  document.getElementById('solar-msg').textContent = j.ok ? j.message : ('Error: ' + j.error);
  document.getElementById('solar-code').value = '';
  refresh();
}
function showSolar(so) {
  const st = document.getElementById('solar-state');
  if (!so || (!so.kiosk_url && !so.api_user)) { st.textContent = 'not set up'; st.className = 'pill'; }
  else {
    const bad = so.kiosk_error || so.api_error;
    st.textContent = (so.plant ? `${so.plant}: ${(so.power_kw || 0).toFixed(2)} kW, ${(so.today_kwh || 0).toFixed(1)} kWh today` : 'no reading yet') +
      (so.api_user ? (so.api_error ? ' · API: ' + so.api_error : ` · API ok, ${so.days_on_record} days on record`) : ` · ${so.days_on_record} days on record`) +
      (so.kiosk_error ? ' · kiosk: ' + so.kiosk_error : '');
    st.className = 'pill ' + (bad ? 'bad' : 'ok');
  }
  if (so && !solarFilled) {
    solarFilled = true;
    document.getElementById('solar-kiosk').value = so.kiosk_url || '';
    document.getElementById('solar-user').value = so.api_user || '';
    document.getElementById('solar-host').value = so.api_user ? (so.api_host || '') : '';
    document.getElementById('solar-host').placeholder = 'API host, default ' + (so.api_host || 'eu5.fusionsolar.huawei.com');
    document.getElementById('solar-station').value = so.api_plant ? so.api_plant.code : '';
  }
}
async function postText(url, text) {
  const r = await bfetch(url, {method: 'POST', headers: {'Content-Type': 'text/plain'}, body: text});
  const j = await r.json(); return j.ok ? j.message : ('Error: ' + (j.error || j.message));
}
async function saveKeys() { document.getElementById('setup-msg').textContent = 'Saving…'; document.getElementById('setup-msg').textContent = await postText('/api/setup/keys', document.getElementById('keys').value); refresh(); }
async function saveConfig() { document.getElementById('setup-msg').textContent = 'Saving…'; document.getElementById('setup-msg').textContent = await postText('/api/setup/config', document.getElementById('config').value); refresh(); }
async function reloadDevices() { document.getElementById('setup-msg').textContent = 'Reconnecting…'; const j = await (await bfetch('/api/reload', {method: 'POST'})).json(); document.getElementById('setup-msg').textContent = j.message || ('Error: ' + j.error); refresh(); }
</script>

<script>
async function refresh() {
  try {
    const s = await (await fetch('/api/status')).json();
    document.getElementById('gateway').textContent =
      `Gateway ${s.cube.host} · callback ${s.cube.service_address} · ${s.message || ''}`;
    const t = document.getElementById('token');
    t.textContent = s.cube.token_valid ? 'token OK' : (s.cube.waiting_for_token ? 'waiting for Allow on the iHost…' : 'no valid token');
    t.className = 'pill ' + (s.cube.token_valid ? 'ok' : 'bad');
    document.getElementById('msg').textContent = s.cube.last_error ? ('Last error: ' + s.cube.last_error) : '';
    const st = document.getElementById('setup-state');
    st.textContent = s.setup.keys_present ? `${s.setup.appliances} appliance(s) configured` : 'no Midea key file yet';
    st.className = 'pill ' + (s.setup.keys_present ? 'ok' : 'bad');
    showSolar(s.solar);
    showDeco(s.deco);
    const tb = document.querySelector('#devices tbody'); tb.innerHTML = '';
    for (const d of s.endpoints) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${d.name}<br><span class="muted">${d.model || ''}</span></td><td>${d.category}</td>` +
        `<td><code>${d.serial || '— not synced —'}</code></td>` +
        `<td class="${d.online ? 'ok' : 'bad'}">${d.online ? 'online' : 'offline'}</td>` +
        `<td><code>${JSON.stringify(d.state)}</code></td>`;
      tb.appendChild(tr);
    }
  } catch (e) { document.getElementById('gateway').textContent = 'Bridge not reachable: ' + e; }
}
async function getToken() { await fetch('/api/token', {method: 'POST'}); refresh(); }
async function sync() {
  const r = await (await bfetch('/api/sync', {method: 'POST'})).json();
  document.getElementById('msg').textContent = r.message || r.error; refresh();
}
refresh(); setInterval(refresh, 3000);
</script>
</body>
</html>
"""


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")
