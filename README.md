<div align="center">
# addhOn

### 

***A custom Home Assistant integration for controlling Haier appliances via the hOn cloud API. It discovers your paired appliances, exposes them as Home Assistant entities, and routes control commands to the supported types (see [Supported Devices](#supported-devices)).

</div>

## Features

- **Automatic device discovery** — discovers all paired Haier devices in your account
- **Multiple device support** — AC units, washing machines, and other hOn-compatible appliances
- **Real-time status** — monitors device state, temperature, modes, and cycle progress
- **Command execution** — control HVAC modes, set temperatures, start/stop programs, and more
- **Asyncio optimization** — dedicated background loop for reliable API communication
- **Smart attribute mapping** — device-specific attribute keys extracted from real diagnostics
- **Full Lovelace support** — integrates seamlessly with Home Assistant UI and automations

## Prerequisites

- Home Assistant 2024.12.0 or newer
- Haier hOn app account credentials

## Installation

### Method 1: Manual (Developer Mode)

1. **Clone or download** this integration to your Home Assistant custom integrations folder:

```bash
git clone https://github.com/telard-pixel/addhOn.git
cp -r addhOn/custom_components/addhon /path/to/config/custom_components/addhon
```

If you don't have a `custom_components` folder, create it:

```bash
mkdir -p /path/to/config/custom_components
```

2. **Restart Home Assistant** to load the integration.

3. **Add the integration** via Settings → Devices & Services → **Add Integration**, search for **addhOn**, then enter your Haier hOn email and password in the dialog. All configuration happens in the UI; there is no `configuration.yaml` setup.

### Method 2: HACS (if available)

Add this repository to HACS as a custom integration and install from the UI.

### Initial setup

1. Go to Settings → Devices & Services → **Add Integration**.
2. Search for **addhOn** and select it.
3. Enter your Haier hOn account email and password and submit. The credentials
   are validated against the hOn cloud and stored in the config entry.

If your hOn session later expires, Home Assistant shows a **Reconfigure**
(re-authentication) prompt asking only for the password again; no need to remove
and re-add the integration.

### Options

Open the integration entry and choose **Configure** to toggle:

- **Enable debug logging** — verbose integration logs.
- **Enable MQTT realtime debug** — verbose logs for the live MQTT stream.

Both persist across restarts. The polling interval is fixed at 60 seconds.

## Supported Devices

### Supported appliance types

Air conditioners (AC), washing machines (WM), tumble dryers (TD), washer-dryers
(WD), refrigerators and freezers (REF/FR/FRE), ovens (OV), dishwashers (DW), wine
coolers (WC), hobs (IH/HOB), hoods (HO), coffee machines/kettles (KT), water
heaters (WH) and robot vacuums (RVC). Air conditioners and laundry appliances have
full control; the other types are exposed mainly as read-only sensors, with a few
controls where they have been mapped.

### Tested on real hardware

- **AC Unit:** Haier AS35PBPHRA-PRE
- **Washing Machine:** Haier HW80-B14959TU1IT
- **Tumble Dryer:** Haier HD100-C367GU1-IT
- **Refrigerator:** Haier HDPW5620CNPK

Other hOn-compatible Haier appliances should work; feel free to test and report.

## Localization

The user interface is multi-language. Entity names, the config and options
screens, service names and descriptions, and user-facing error messages are
provided as translations (currently English and Italian) and follow Home
Assistant's configured language. The code, comments and log messages are
English-only.

## How It Works

The integration operates in three layers:

### 1. Home Assistant Integration Layer

Handles entity discovery, service calls, and data updates. On initialization, it queries your Haier account for all paired devices and creates Home Assistant entities for each one.

### 2. Native hOn Client

A self-contained Python client (`custom_components/addhon/client/`), with no
third-party hOn library vendored in. It manages:
- **Authentication**: Salesforce OAuth login exchanging credentials for the
  Cognito/id tokens the API expects (`client/transport/auth.py`)
- **Device enumeration**: fetches the full device list and metadata, including
  offline appliances (`client/transport/api.py`)
- **Command routing**: builds and sends control commands (startProgram,
  settings, stopProgram) with their parameters and rules (`client/engine/`)
- **State polling and real-time updates**: HTTP polling plus an AWS IoT MQTT
  stream for live state (`client/transport/mqtt.py`)

### 3. Haier Cloud API (hOn)

The backend cloud service that handles:
- Token exchange and session management
- Command execution on your physical devices
- Real-time state synchronization
- Device pairing and account management

## Entities

Each discovered appliance becomes a Home Assistant **device**; the entities it
exposes depend on its type. A **connectivity** binary sensor is always present and
stays available even when the appliance is offline, so you can tell whether it is
reachable (all other entities become *unavailable* while it is offline).

### Climate (AC)

- **HVAC modes:** off, auto, cool, dry, heat, fan_only
- **Fan modes:** auto, low, medium, high
- **Swing:** off / on (vertical swing), when the device exposes it
- **Temperature:** current temperature and target set point (16-30 °C)

### Laundry (washing machine, tumble dryer, washer-dryer)

- **Sensors:** state, program name, program phase, remaining time and, depending
  on the model, wash temperature, spin speed, dry level, delay time, plus energy
  and water counters
- **Controls:** start/stop and the available programs and options, via switch,
  select, number and button entities

### Other appliances

Refrigerators, ovens, dishwashers, water heaters, robot vacuums and the remaining
types are exposed mainly through sensors (and a few controls where they have been
mapped).

## Services

Device control is done through the normal entities (climate, switch, number,
select, button), not through a service call. The integration exposes only two
diagnostic services (also available from Developer Tools → Actions):

### `addhon.set_log_level`

Set the integration's diagnostic log level at runtime.

```yaml
action: addhon.set_log_level
data:
  level: debug   # one of: debug, info, warning, error
```

### `addhon.set_mqtt_log_level`

Set the verbosity of the realtime MQTT stream logger at runtime.

```yaml
action: addhon.set_mqtt_log_level
data:
  level: warning   # one of: debug, info, warning, error
```

## Troubleshooting

> **Debug logging:** open the integration and choose **Configure** to toggle
> **Enable debug logging** (integration) and **Enable MQTT realtime debug**
> independently. Both persist across restarts. See
> [`docs/discovery-debugging.md`](docs/discovery-debugging.md) and
> [`docs/mqtt-realtime-logging.md`](docs/mqtt-realtime-logging.md).

### Authentication Failed

- Re-enter your Haier email and password via the integration's re-authentication prompt (or remove and re-add the integration)
- Verify the account is active in the Haier hOn app
- If 2FA is enabled, disable it temporarily for the integration account

### Device Not Discovered

- Ensure the device is paired in the Haier hOn app
- Check internet connectivity
- After pairing a new device in the app, reload the integration (or restart Home Assistant)

### HVAC Mode / Fan Mode Issues

The integration auto-detects the modes each AC supports. If your AC does not respond to a mode:
- Enable debug logging (see above); the climate entity logs its detected `hvac_modes` and `fan_modes` at startup
- Report the unsupported mode on GitHub issues

## Development Notes

### Architecture Decisions

- **Asyncio background loop** — the native client's operations run in a dedicated thread-safe event loop to avoid blocking Home Assistant
- **Command routing** — different device types expect different command structures; the integration detects and routes appropriately
- **Attribute extraction** — device-specific attributes are extracted from real device diagnostics, not hardcoded

### Extending the Integration

To add support for a new Haier device:

1. Pair it in the Haier hOn app
2. Enable debug logging and capture the device diagnostics (see [`docs/discovery-debugging.md`](docs/discovery-debugging.md))
3. Add or extend the relevant platform file (`sensor.py`, `binary_sensor.py`, `number.py`, `select.py`, `switch.py`, ...) and, if the device type needs it, its capability map
4. Test with your device, then open a pull request (or an issue with the captured diagnostics)

## License

MIT License — see LICENSE file for details.

## Contributing

Issues and pull requests are welcome! Please include:
- Home Assistant version
- Device model number
- Debug logs (see [`docs/discovery-debugging.md`](docs/discovery-debugging.md))
- Steps to reproduce

## Support

- **Issues:** GitHub Issues
- **Discussions:** GitHub Discussions
- **Documentation:** see the [`docs/`](docs/) folder

---

Built with ❤️ for Home Assistant enthusiasts.
