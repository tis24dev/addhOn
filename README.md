# addhOn

A custom Home Assistant integration for controlling Haier appliances via the hOn cloud API. Supports air conditioning units and washing machines with full entity discovery and command routing.

## Features

- **Automatic device discovery** — discovers all paired Haier devices in your account
- **Multiple device support** — AC units, washing machines, and other hOn-compatible appliances
- **Real-time status** — monitors device state, temperature, modes, and cycle progress
- **Command execution** — control HVAC modes, set temperatures, start/stop programs, and more
- **Asyncio optimization** — dedicated background loop for reliable API communication
- **Smart attribute mapping** — device-specific attribute keys extracted from real diagnostics
- **Full Lovelace support** — integrates seamlessly with Home Assistant UI and automations

## Supported Devices

### Tested & Working

- **AC Unit:** Haier AS35PBPHRA-PRE
- **Washing Machine:** Haier HW80-B14959TU1IT

Other hOn-compatible Haier appliances should work — feel free to test and report.

## Localization

The user interface is multi-language. Entity names, the config and options
screens, service names and descriptions, and user-facing error messages are
provided as translations (currently English and Italian) and follow Home
Assistant's configured language. The code, comments and log messages are
English-only.

> **Upgrading to v5.0.0:** entity *friendly names* are now localized and use Home
> Assistant's `has_entity_name` format (`<device> <entity>`), so the displayed
> names change. Entity IDs are unchanged, so dashboards and automations that
> reference `entity_id` keep working.

## Prerequisites

- Home Assistant 2024.12.0 or newer
- Python 3.11+
- Internet connection (cloud API only, no local option)
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

3. **Add the integration** via Settings → Devices & Services → Create Automation → Haier hOn, or manually add to `configuration.yaml`:

```yaml
addhon:
  username: your-haier-email@example.com
  password: your-haier-password
```

### Method 2: HACS (if available)

Add this repository to HACS as a custom integration and install from the UI.

## Configuration

### Basic Setup

Edit your `configuration.yaml`:

```yaml
addhon:
  username: your-haier-email@example.com
  password: your-haier-password
```

### Advanced Options

```yaml
addhon:
  username: your-haier-email@example.com
  password: your-haier-password
  scan_interval: 60          # Update interval in seconds (default: 60)
  timeout: 10                # API request timeout in seconds (default: 10)
```

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

### Climate Entity (AC Unit)

- **Entity ID:** `climate.bedroom_ac` (example)
- **Attributes:**
  - `hvac_mode` — off, cool, heat, auto, dry, fan_only
  - `current_temperature` — indoor temp
  - `target_temperature` — set point
  - `fan_mode` — auto, low, medium, high, highest
  - `swing_mode` — off, vertical, horizontal, both

### Washing Machine

- **Entity ID:** `sensor.washing_machine_status` (example)
- **Attributes:**
  - `program` — current or last run program name
  - `duration` — remaining time in minutes
  - `cycle_status` — running, finished, error
  - Temperature, spin speed, and other cycle parameters

## Services

### `addhon.send_command`

Send a raw command to a device.

```yaml
service: addhon.send_command
data:
  device_id: "AC_UNIT_ID"
  command: "startProgram"
  parameters:
    temperature: 22
    mode: "cool"
```

## Troubleshooting

> **Debug logging:** open the integration and choose **Configure** to toggle
> **Enable debug logging** (integration) and **Enable MQTT realtime debug**
> independently. Both persist across restarts. See
> [`docs/discovery-debugging.md`](docs/discovery-debugging.md) and
> [`docs/mqtt-realtime-logging.md`](docs/mqtt-realtime-logging.md).

### Authentication Failed

- Check your Haier email and password in `configuration.yaml`
- Verify the account is active in the Haier hOn app
- If 2FA is enabled, disable it temporarily for the integration account

### Device Not Discovered

- Ensure the device is paired in the Haier hOn app
- Check internet connectivity
- Restart Home Assistant after adding credentials

### Slow Updates / Timeouts

- Increase the `timeout` value in configuration
- Increase `scan_interval` to reduce polling frequency
- Check your ISP connection (hOn API is cloud-hosted)

### HVAC Mode Map Issues

The integration auto-detects supported modes per device. If your AC doesn't respond to a mode:
- Check `climate.{name}_debug_modes` in Home Assistant Developer Tools → States
- Report unsupported modes on GitHub issues

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
4. Test with your device and submit a pull request

## Limitations

- **Cloud-only** — requires internet connection; no local fallback
- **Rate limiting** — the Haier API has undocumented rate limits; avoid polling faster than every 30 seconds
- **Token expiry** — sessions expire after ~7 days of inactivity; the integration auto-refreshes on first use
- **Brand-specific** — only works with Haier devices using the hOn API; other brands (Candy, Arçelik) use different APIs

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
- **Documentation:** Check the wiki for detailed guides

---

Built with ❤️ for Home Assistant enthusiasts.
