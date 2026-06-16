# Haier hOn Integration for Home Assistant

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

## Prerequisites

- Home Assistant 2024.1 or newer
- Python 3.11+
- Internet connection (cloud API only, no local option)
- Haier hOn app account credentials

## Installation

### Method 1: Manual (Developer Mode)

1. **Clone or download** this integration to your Home Assistant custom integrations folder:

```bash
git clone https://github.com/yourusername/haier-hon-integration.git /path/to/config/custom_components/haier_hon
```

If you don't have a `custom_components` folder, create it:

```bash
mkdir -p /path/to/config/custom_components
```

2. **Restart Home Assistant** to load the integration.

3. **Add the integration** via Settings → Devices & Services → Create Automation → Haier hOn, or manually add to `configuration.yaml`:

```yaml
haier_hon:
  username: your-haier-email@example.com
  password: your-haier-password
```

### Method 2: HACS (if available)

Add this repository to HACS as a custom integration and install from the UI.

## Configuration

### Basic Setup

Edit your `configuration.yaml`:

```yaml
haier_hon:
  username: your-haier-email@example.com
  password: your-haier-password
```

### Advanced Options

```yaml
haier_hon:
  username: your-haier-email@example.com
  password: your-haier-password
  scan_interval: 60          # Update interval in seconds (default: 60)
  timeout: 10                # API request timeout in seconds (default: 10)
```

## How It Works

The integration operates in three layers:

### 1. Home Assistant Integration Layer

Handles entity discovery, service calls, and data updates. On initialization, it queries your Haier account for all paired devices and creates Home Assistant entities for each one.

### 2. pyhOn Library

A lightweight Python client for the Haier hOn API. It manages:
- **Authentication** — exchanges credentials for a session token
- **Device enumeration** — fetches device list and metadata
- **Command routing** — sends control commands (startProgram, settings, stopProgram)
- **State polling** — retrieves current device status and attributes

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

### `haier_hon.send_command`

Send a raw command to a device.

```yaml
service: haier_hon.send_command
data:
  device_id: "AC_UNIT_ID"
  command: "startProgram"
  parameters:
    temperature: 22
    mode: "cool"
```

## Troubleshooting

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

- **Asyncio background loop** — pyhOn operations run in a dedicated thread-safe event loop to avoid blocking Home Assistant
- **Command routing** — different device types expect different command structures; the integration detects and routes appropriately
- **Attribute extraction** — device-specific attributes are extracted from real device diagnostics, not hardcoded

### Extending the Integration

To add support for a new Haier device:

1. Pair it in the Haier hOn app
2. Enable debug logging and capture the device diagnostics
3. Add a new entity platform in `custom_components/haier_hon/entities/`
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
