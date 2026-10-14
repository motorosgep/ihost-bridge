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

The image is built for `linux/arm/v7` (the iHost), `arm64` and `amd64` by the
GitHub workflow and pushed to Docker Hub.

1. iHost web console: Docker, **Add**, **Add Add-on**, search for the image name,
   **Add to list**, **Install**.
2. **Run** with network mode `host` and a volume mapped to `/data`.
3. Put `midea-devices.json` (and optionally `config.yaml`) into that volume.
4. Open the add-on's **Web UI** tab, press **Get Token**, approve on the iHost.

Inside the add-on the gateway is reachable as `http://ihost`, which is the default.

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
