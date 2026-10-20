# Integration notes

What each wished-for device needs, what it can become on the iHost, and what
Butter would show. Written 2026-10-20 after checking the vendors' APIs and the
open-source projects that already speak to them. Effort is a rough size for the
bridge plus Butter: S is an evening, M a few evenings, L a week or more.

The iHost's Open API knows 18 device categories (plug, switch, light, curtain,
the sensors, button, camera, fan, air conditioner, thermostat and so on). There
is no category for a vacuum, a dishwasher, a fridge, a washing machine or a
doorbell, so those appliances become a set of sensors and switches on the hub,
and Butter gets a purpose-built card fed by the bridge directly.

| Device | Route | Local? | Effort | Blocked on |
| --- | --- | --- | --- | --- |
| Sonoff Wi-Fi switch | eWeLink Smart Home add-on, already installed | LAN control | none | your eWeLink sign-in in the add-on |
| TP-Link Deco | Deco's own web API | yes | S–M | Deco admin password |
| Tado thermostat | HomeKit over the Tado bridge (V3+) | yes | M | model, and the bridge not paired with Apple Home |
| Bosch dishwasher | Home Connect cloud API | no | M | a Home Connect developer app |
| LG fridge + WashTower | ThinQ Connect cloud API | no | M | your ThinQ personal access token |
| Dreame D20 | Dreamehome cloud protocol | no | M–L | Dreamehome username and password |
| SwitchBot video doorbell | SwitchBot cloud API + webhook relay | no | M | relay hosting, token and secret |

## Sonoff Wi-Fi switch

No code. CoolKit's eWeLink Smart Home add-on is installed on the iHost; sign in
to its web UI with your eWeLink account and press Sync on the device. Only
models with LAN control work, and a few people report "Failed to control" on
some devices. Once synced it is an ordinary hub device for Butter.

## TP-Link Deco

**Verified:** the Deco at 192.168.68.1 answers the legacy RSA login endpoints
(`/cgi-bin/luci/;stok=/login?form=keys` and `form=auth`), which is the flow the
existing integrations use. Some 2026 firmware builds replaced that flow and
break those integrations; yours has not.

**Library:** `tplink-deco-api` on PyPI (needs Python 3.14+; the bridge image
would move to it). It returns the client list (name, MAC, IP, online, band),
the Deco units (model, firmware), CPU and memory, WAN status, and it can block
clients and add, change or remove fixed-IP reservations.

**On the hub:** chosen clients (phones) as presence sensors, category
`motionSensor`, `detect` true while the phone is on the Wi-Fi. iHost scenes can
then react to arriving and leaving.

**In Butter:** a network screen from the bridge's API: who is online, on which
Deco, which band, internet status. Plus one button that reserves fixed IPs for
the hub, the Midea units and the lamp, which ends the address-change problem for
good.

**Needs:** the Deco admin password, pasted into the bridge's Setup page.

## Tado thermostat

The cloud API is the wrong route. Since 2025 it uses an OAuth device-code
login and, per Tado's own support page, a base quota of 100 requests a day,
raised to 20,000 only with an Auto-Assist subscription. One zone polled every
quarter hour already eats the free quota.

**V3+ (Internet Bridge):** the bridge is a HomeKit accessory. A HomeKit
controller on the LAN gets the zones as thermostats with no cloud and no
quota, which is how Home Assistant users run Tado offline. The bridge accepts
exactly one HomeKit controller, so this only works if it is not paired with
Apple Home (or you are willing to unpair it). Pairing uses the code printed on
the bridge. Hot water is not exposed over HomeKit. Library: `aiohomekit`.

**Tado X:** Thread and Matter instead of HomeKit. The iHost has no Matter
controller and running one inside the bridge is a large job, so X would be
cloud-only, which is not worth building at 100 requests a day.

**On the hub:** `thermostat` with temperature, humidity, target setpoint and
mode, the same mapping the Midea heat pump already uses, so Butter needs no
new UI.

**Needs:** which Tado it is, and whether the bridge is already paired with
Apple Home.

## Bosch dishwasher

**Official route:** the Home Connect cloud API. You register a developer
application at developer.home-connect.com with your Home Connect account and
get a client id and secret; the bridge then authorises once (the API offers an
authorisation-code flow and a device flow) and keeps the refresh token. Limits
are 1,000 requests per day and 50 per minute per user, which is plenty for one
appliance because state changes arrive on a server-sent event stream, so the
bridge does not poll.

**What it gives:** power state, door, selected and active program, remaining
time, progress, and program start if Remote Start is enabled on the machine.

**Local route, for later:** the `hcpy` project speaks to the dishwasher
directly over a TLS-PSK WebSocket after a one-time cloud download of the
appliance's keys. It calls itself "very, very beta". Worth revisiting if the
cloud route proves limiting; not a first choice.

**On the hub:** `switch` for start, sensors for state and remaining time.

**In Butter:** an appliance card (program, remaining time, door, progress) that
the LG WashTower will share.

**Needs:** the dishwasher model and a Home Connect developer app's client id
and secret, pasted into Setup. The developer app's test account must be the
Home Connect account the dishwasher is in.

## LG fridge and WashTower

The official ThinQ Connect cloud API with the `thinqconnect` Python SDK, which
has device types for the refrigerator and the WashTower, including its washer
and dryer halves. State changes arrive over MQTT push. Fridge: fridge and
freezer temperatures, door, express modes. WashTower: washer and dryer state,
cycle, remaining time, remote start where the appliance allows it.

**Needs:** a personal access token created at connect-pat.lgthinq.com with all
six scopes, and the two-letter country code of the ThinQ account. Steps are in
the chat log from 2026-10-20. Blocked until you find the LG credentials.

## Dreame D20

Paired through the Dreame app, which switches the robot's local interface
off, so control has to go through Dreame's cloud. The MIT-licensed
`Tasshack/dreame-vacuum` project implements that protocol and can be ported
into the bridge. First version: start, pause, dock, state, battery and
consumables. Maps need the cloud's map pipeline and are out of scope at first.

**On the hub:** a `switch` (on = clean, off = dock) plus battery and a state
sensor.

**In Butter:** a vacuum card from the bridge's API.

**Needs:** Dreamehome username and password kept on the iHost, pasted into
Setup. Say if that is not acceptable; there is no other route for this model.

## SwitchBot video doorbell

SwitchBot's cloud API v1.1 lists the doorbell (W6702000) with status, commands
and webhook events for ring, motion and person detection, each with a
timestamp and a snapshot image. No stream URL exists, and the hub only accepts
cameras as RTSP or ESP32 streams, so there will be no live view on the hub.

**On the hub:** a `button` that presses on a ring, a `motionSensor`, and a
battery level, usable in scenes.

**In Butter:** the snapshot and the event list.

**The catch:** events arrive by webhook, so SwitchBot needs a URL it can reach.
Either a small relay on a free Cloudflare Worker that the bridge polls, or a
port forward on the Deco to the iHost, which is not recommended. Requests are
signed with HMAC-SHA256 from a token and secret shown in the app's developer
options; 10,000 calls a day.

**Needs:** the doorbell model, agreement on a relay, and the token and secret
pasted into Setup.

## Suggested order

1. **Deco.** Local, one password, and it fixes the address problem.
2. **Tado**, if it is a V3+ that can take a HomeKit pairing. Local, and the
   hub already understands thermostats.
3. **Bosch dishwasher** over the official cloud API.
4. **LG** when the token exists.
5. **Dreame D20.**
6. **Doorbell**, once there is a relay to receive its events.

## Sources

- Home Connect developer docs: https://developer.home-connect.com/
- hcpy, local Home Connect: https://github.com/osresearch/hcpy
- Tado REST API limits: https://support.tado.com/en/articles/12165739-limitation-for-rest-api-usage
- Tado V3+ bridge over HomeKit, one controller at a time: https://github.com/AmpScm/TadoLocal
- tplink-deco-api: https://github.com/roquerodrigo/tplink-deco-api
- Deco firmware login change: https://github.com/rhanekom/hass-tplink-deco-r4
- ThinQ Connect SDK: https://github.com/thinq-connect/pythinqconnect
- Dreame cloud and local notes: https://dv.tasshack.com/configuration/local
- SwitchBot API: https://github.com/OpenWonderLabs/SwitchBotAPI
- Doorbell webhook payloads: https://github.com/OpenWonderLabs/SwitchBotAPI/pull/509
- eWeLink CUBE Open API V2: https://ewelink.cc/ewelink-cube/introduce-open-api/document/
