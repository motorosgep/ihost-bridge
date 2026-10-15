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

## When the router hands out new addresses

Inside the add-on the gateway is always `ihost`, so the bridge itself is
unaffected. Appliances do get new addresses; when one stays offline for a few
minutes the bridge rediscovers the LAN and reconnects, at most once every ten
minutes. A DHCP reservation for each unit avoids the gap altogether.

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

## Configuration

See [config.example.yaml](config.example.yaml). Everything can also be set with
environment variables: `CUBE_HOST`, `CUBE_SERVICE_HOST`, `BRIDGE_PORT`,
`BRIDGE_DATA_DIR`, `MIDEA_KEYS_FILE`, `LOG_LEVEL`.

## Layout

```
bridge/
  cube/client.py     Open API v2 client (token, discovery, state and online reports)
  cube/server.py     aiohttp app: /webhook for gateway directives + status page
  core/endpoint.py   Endpoint base class and capability helpers
  core/registry.py   sync, change-only reporting, directive dispatch
  midea/             Midea adapters (ac.py, heatpump.py) on top of midea-local
  app.py             wiring and lifecycle
```

Adding another vendor means one module that subclasses `Endpoint` and a few
lines in `app.py`; the gateway side does not change.

## Status

Early but functional. Tested against an iHost on eWeLink CUBE 2.9.0 with two
Midea ACs and one M-Thermal heat pump.
