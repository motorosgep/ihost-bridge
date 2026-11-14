# ihost-bridge

Expose appliances the SONOFF iHost cannot pair natively as first-class devices in
eWeLink CUBE, so they show up in the iHost device list and can be used in scenes.

Today it bridges **Midea** units over the local LAN protocol (no cloud at runtime):

| Appliance | Shown in iHost as |
|---|---|
| Midea air conditioner (type `0xAC`) | `airConditioner`: power, mode (cool/heat/auto/fan/dry), fan level, target temperature, indoor temperature and humidity, swing, eco, live power draw. Units with an outdoor sensor get a separate `temperatureSensor`; units with a fresh-air module get a separate `fan` (on/off, low/medium/high) |
| Midea air-to-water heat pump (type `0xC3`) | `thermostat` for zone 1 (zone enable, target, supply temperature when reported), `thermostat` for hot water (DHW enable, tank target and temperature). Outdoor, water-in/out temperature sensors and a power/voltage/current sensor are created when the unit answers the "unit parameters" query; some units (e.g. 171H120F) do not |

Appliances that report a lifetime kWh counter (the heat pump, and ACs with a
meter) also get the gateway's `power-consumption` capability: the bridge keeps a
small sample log of the counter under `data/energy/` and answers the gateway's
hourly usage queries from it, so the iHost shows consumption statistics for them.

Optionally (`cards.heatpump: true`) the bridge also registers a **custom UI card**
for the heat pump under the iHost's Custom Tiles: a tile served by the bridge
(`/cards/heatpump`) with its own icon, target, outdoor and tank temperatures,
compressor status and today's consumption, and a drawer (`/cards/heatpump/drawer`)
with on/off and setpoint controls. The gateway loads these pages in an iframe, so
the bridge must be reachable from the browser; see `cube.public_host`. Off by
default; the normal device cards cover everyday use.

The bridge talks to the gateway through the [eWeLink CUBE Open API v2](https://cube.ewelink.cc/resources/developer-and-api-guide)
and to the appliances through [midea-local](https://github.com/midea-lan/midea-local).

## How it works

1. On start the bridge broadcasts on the LAN to find Midea units and matches them
   with the token/key pairs in `midea-devices.json`.
2. It asks the gateway for an access token once. You approve the request on the
   iHost web console ("Get eWeLink CUBE access token" dialog).
3. It registers every endpoint with a `DiscoveryRequest`, then pushes
   `DeviceStatesChangeReport` events whenever an appliance changes, and answers the
   gateway's `UpdateDeviceStates` / `QueryDeviceStates` directives on `/webhook`.

A small status page on port 9900 shows the token state, each endpoint, its iHost
serial number and its current state.

## One-time: Midea token and key

Midea "V3" units only talk to clients that present a per-device token and key.
Fetch them once with your MSmartHome account (the account is not needed afterwards):

```bash
python3 -m venv ~/midea && ~/midea/bin/pip install midea-local
cd ~/midea && ~/midea/bin/midealocal discover --username 'EMAIL' --password 'PASSWORD' --cloud-name SmartHome
```

This writes `midea-devices.json` into the current directory. Copy it into the
bridge's data directory (or point `midea.keys_file` at it). Never commit it.

## Run on your computer (development)

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
mkdir -p data && cp config.example.yaml data/config.yaml   # set cube.host to the iHost IP
cp /path/to/midea-devices.json data/
.venv/bin/python -m bridge
```

Open http://localhost:9900, press **Get Token**, then press **Allow** on the iHost
web console. The devices appear in the iHost within a few seconds.

## Run on the iHost as an add-on

The GitHub workflow builds the image for `linux/arm/v7` (the iHost), `arm64` and
`amd64` on every push to `main` and publishes it to GitHub's container registry as
`ghcr.io/<owner>/ihost-bridge:latest` (make the package public in the repository's
package settings so the iHost can pull it without credentials). A Docker Hub mirror
is pushed too when `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` secrets are set.

1. iHost web console: Docker, **Add**, **Add Repository**, enter the full image
   path `ghcr.io/<owner>/ihost-bridge:latest`, then **Install**.
2. **Run** it. Set Network to `host`; create a volume (Docker, Volume, Create volume)
   and map it to `/data` so the token, keys and energy history survive re-installs.
   Optionally add an environment variable `TZ` (e.g. `Europe/Budapest`) so daily
   energy totals use your local midnight.
3. Open `http://<ihost-ip>:9900` (the link next to **Port** on the add-on's page).
   In **Setup**, paste the contents of your `midea-devices.json` and save; the
   appliances connect within seconds. Optionally paste a `config.yaml` to give
   devices friendly names.
4. Press **Get Token**, then **Allow** on the iHost's "Get eWeLink CUBE access
   token" dialog. The devices appear in the iHost device list.

Inside the add-on the gateway is reachable as `http://ihost`, which is the default.

## Who may change things

Reading (status, history, solar) stays open on the home network. Everything that
changes a device, the router or the bridge's settings needs one of:

- the **setup PIN**: the first time the web page is opened after updating, it
  asks you to choose one (at least 6 characters), and that browser stays
  unlocked. Five wrong PINs lock the check for 5 minutes, doubling each time.
  Forgot it? Press **Forgot PIN**, then **Allow** on the iHost (the same dialog as
  Get Token); the PIN is cleared and the next visitor chooses a new one. It is
  stored only as a salted hash in `setup_pin.json`.
- an app's **iHost access token** (`Authorization: Bearer <token>`), the one the
  app got when you pressed Allow on the iHost. Butter sends it; the bridge asks
  the gateway whether it is genuine.

Gateway directives (`/webhook`) carry no credentials, so the bridge accepts them
only from the gateway's own addresses (loopback, the iHost's IP). If your setup
routes them differently and devices stop responding, the log says
"Refused a directive from …"; set `cube.webhook_any_source: true` (or
`WEBHOOK_ANY_SOURCE=1`) to go back to accepting any address.

## When the router hands out new addresses

Inside the add-on the gateway is always `ihost`, so the bridge itself is
unaffected. Appliances do get new addresses; when one stays offline for a few
minutes the bridge rediscovers the LAN and reconnects, at most once every ten
minutes. A DHCP reservation for each unit avoids the gap altogether; with a
TP-Link Deco set up (below), Butter's Network screen pins them in one go.

## History for Butter

The gateway keeps sensor history for its own console but does not expose it
through the Open API. The bridge therefore records every numeric reading it
sees (temperature, humidity, power draw, battery, setpoints, on/off states) from
the gateway's event stream into `history.sqlite` on its data volume, takes a full
snapshot every 15 minutes, and serves it on the same port as the web UI:

```
GET /api/history                                   recorder status
GET /api/history/metrics?serial=<hub serial>       metrics recorded for a device
GET /api/history/samples?serial=&metric=temperature.temperature&from=&to=&resolution=3600
GET /api/history/energy?serial=&from=&to=&resolution=3600
```

`from`/`to` take epoch seconds or ISO-8601; `resolution` buckets samples
(avg/min/max) or returns raw points when omitted. Energy comes from the bridge's
own counter log for its Midea appliances and is integrated from power readings
for everything else. The [Butter](https://github.com/motorosgep/butter) app finds
the recorder next to the hub automatically. Disable it with `recorder.enabled:
false`; without Midea appliances you can run only the recorder with
`python -m bridge.recorder_cli --host <gateway ip>`.

## Solar (Huawei FusionSolar)

The bridge can read a Huawei solar plant and show it on the iHost as a sensor
(output now, and the lifetime counter as energy statistics), usable in scenes
like any other device. Fill it in under **Solar** on the bridge's web page:

- **Kiosk link** (no account needed): in FusionSolar open your plant, choose
  Kiosk view, switch it on and copy the link. The bridge reads it every five
  minutes: output, today's curve, and the totals for the day, month, year and
  lifetime.
- **Northbound API account** (optional): a user name and system code created
  under System > Company Management > Northbound Management, usually by the
  installer. It adds the plant's history by day, month and year. Huawei allows
  only a few history calls a day, so the bridge fetches one period every ten
  minutes, newest first, and keeps what it has read in `solar.sqlite`; a plant
  with a few years behind it takes some hours to fill in.

Without the API account the bridge still builds a history, by writing down the
kiosk's totals from the day it is set up. The settings are stored in
`solar.json` in the data directory (the system code is never shown again), or
can be given in `config.yaml` / the environment, see `config.example.yaml`.

Apps read it from the bridge:

```
GET /api/solar                                     live figures, today's curve, which sources are set up
GET /api/solar/history?span=day&year=2026&month=11 each day of a month, kWh
GET /api/solar/history?span=month&year=2026        each month of a year
GET /api/solar/history?span=year                   every year on record
```

## Network (TP-Link Deco)

The bridge can read a TP-Link Deco mesh with the Deco's admin password (the one
the Deco app asks for). Fill in the main Deco's address and the password under
**Network** on the bridge's web page. It then:

- keeps one admin session open and reads the Deco units, the clients (name,
  address, wired or which band, which Deco) and the internet status once a
  minute, and the fixed-address (DHCP reservation) table every five minutes;
- shows the internet connection on the iHost as a contact sensor named
  "Internet", closed while online and open when it is down, for scenes;
- adds, changes and removes fixed addresses when an app asks, after checking
  each one: inside the home network, not the Deco's own address, not held by
  another device, and within the Deco's limit.

The Deco allows one admin session at a time, so signing in to its web page signs
the bridge out; it logs in again after a minute. If the Deco refuses the
password the bridge stops trying (the Deco locks the login after a few wrong
attempts) until the password is saved again. Changing the Deco address on the
web page needs the password typed again. The settings are stored in `deco.json`
in the data directory, readable only by the bridge, or can be given in
`config.yaml` / `DECO_HOST` and `DECO_PASSWORD`.

The network pages show who is home and change the router, so unlike the other
pages they answer only apps paired with the iHost: send the gateway access token
the app got when you pressed Allow, as `Authorization: Bearer <token>`. The
bridge asks the gateway whether the token is genuine.

```
GET    /api/network                     internet, Deco units, clients, fixed addresses
POST   /api/network/reservations        {"items": [{"mac": "...", "ip": "..."}]}
DELETE /api/network/reservations/<mac>
```

Uses [tplink-deco-api](https://github.com/roquerodrigo/tplink-deco-api), which
needs Python 3.14. Tested against a fake Deco; the login handshake was checked
against a Deco X20 on firmware 1.1.8.

## Configuration

See [config.example.yaml](config.example.yaml). Everything can also be set with
environment variables: `CUBE_HOST`, `CUBE_SERVICE_HOST`, `BRIDGE_PORT`,
`BRIDGE_DATA_DIR`, `MIDEA_KEYS_FILE`, `LOG_LEVEL`, `SOLAR_KIOSK_URL`,
`SOLAR_API_USER`, `SOLAR_API_CODE`, `DECO_HOST`, `DECO_PASSWORD`, `WEBHOOK_ANY_SOURCE`.

## Layout

```
bridge/
  cube/client.py     Open API v2 client (token, discovery, state and online reports)
  cube/server.py     aiohttp app: /webhook for gateway directives + status page
  core/endpoint.py   Endpoint base class and capability helpers
  core/registry.py   sync, change-only reporting, directive dispatch
  midea/             Midea adapters (ac.py, heatpump.py) on top of midea-local
  solar/             Huawei FusionSolar: kiosk reader, Northbound API client, yield ledger, service
  deco/              TP-Link Deco: mesh reader, internet sensor, fixed-address reservations
  network_api.py     /api/network routes (need the caller's gateway token)
  auth.py            setup PIN, sessions; with cube/server.py's guard, who may change things
  app.py             wiring and lifecycle
```

Adding another vendor means one module that subclasses `Endpoint` and a few
lines in `app.py`; the gateway side does not change.

## Status

Early but functional. Tested against an iHost on eWeLink CUBE 2.9.0 with two
Midea ACs and one M-Thermal heat pump.
