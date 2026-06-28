<div align="center">

# addhOn

### Add your Haier devices to your home automation system

**A custom Home Assistant integration for controlling Haier appliances via the hOn cloud API. It discovers your paired appliances, exposes them as Home Assistant entities, and routes control commands to the supported types**

[![Release](https://img.shields.io/github/v/release/tis24dev/addhOn?logo=github&label=release)](https://github.com/tis24dev/addhOn/releases)
[![CI](https://img.shields.io/github/actions/workflow/status/tis24dev/addhOn/ci.yml?branch=dev&label=CI&logo=github)](https://github.com/tis24dev/addhOn/actions/workflows/ci.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?logo=homeassistant&logoColor=white)](https://hacs.xyz/)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.12%2B-41BDF5.svg?logo=homeassistant&logoColor=white)](https://www.home-assistant.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Last commit](https://img.shields.io/github/last-commit/tis24dev/addhOn?logo=github)](https://github.com/tis24dev/addhOn/commits)

</div>

---

## Features

- **Automatic device discovery** — discovers all paired Haier devices in your account
- **Multiple device support** — AC units, washing machines, and other hOn-compatible appliances
- **Real-time status** — monitors device state, temperature, modes, and cycle progress
- **Command execution** — control HVAC modes, set temperatures, start/stop programs, and more
- **Asyncio optimization** — dedicated background loop for reliable API communication
- **Smart attribute mapping** — device-specific attribute keys extracted from real diagnostics
- **Full Lovelace support** — integrates seamlessly with Home Assistant UI and automations
- **Two-factor authentication (2FA)** — Two-factor authentication during the login process is supported
- **Multilingual integration** — Currently available in Italian and English, with additional languages available upon request.

## Installation

### Prerequisites

- Home Assistant 2024.12.0 or newer
- Haier hOn app account credentials

### Method 1: HACS

Add this repository to HACS as a custom integration and install from the UI.

1. Login in Home Assistant UI → Select HACS in the left
2. Tap the three dots (menu) in the upper-right corner
3. Select **Custom repositories**
4. Add the repo link: **https://github.com/tis24dev/addhOn/** and type: **Integration**
5. Save
6. Search for **addhOn** and select it
7. Click the button in the lower right corner to download, then restart
8. Go to Settings → Devices & Services → **Add Integration**
9. Search for **addhOn** and select it
10. Enter your Haier hOn account email and password and submit. The credentials
   are validated against the hOn cloud and stored in the config entry.

If your hOn session later expires, Home Assistant shows a **Reconfigure**
(re-authentication) prompt asking only for the password again; no need to remove
and re-add the integration.

### Options

Open the integration entry and choose **Configure** to toggle:

- **Enable debug logging** — verbose integration logs.
- **Enable MQTT realtime debug** — verbose logs for the live MQTT stream.

Both persist across restarts. The polling interval is fixed at 60 seconds.

These toggles are also exposed as switches on a dedicated **addhOn diagnostics**
device (Settings > Devices & Services > addhOn), alongside read-only diagnostics
and quick-action buttons (refresh now, reset debug). A ready-to-paste dashboard
card is in [`docs/debug-device.md`](docs/debug-device.md).

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
- **Washing Machine:** Candy TCA286TM5-S
- **Tumble Dryer:** Haier HD100-C367GU1-IT
- **Tumble Dryer:** Haier HD90-A3959 INT
- **Refrigerator:** Haier HDPW5620CNPK

Other hOn-compatible Haier appliances should work; feel free to test and report.

## Troubleshooting

### Capture debug logs

1. **Enable** — open **addhOn diagnostics** (Settings → Devices & Services →
   addhOn) and turn on **Debug logging**. Add **MQTT realtime debug** only when
   investigating push/MQTT updates. *(Same as integration → Configure → Enable
   debug logging.)*
2. **Reproduce** — trigger the problem; press **Refresh now** to force an
   immediate poll for discovery/polling issues.
3. **Download** — Settings → System → Logs → **Download full log**, then attach
   the `home-assistant.log` to your GitHub issue.
4. **Disable** — press **Reset debug** on the same device (turns both toggles off
   and restores the default log levels), or just switch them off.

Both toggles persist across restarts. Details:
[`docs/discovery-debugging.md`](docs/discovery-debugging.md) and
[`docs/mqtt-realtime-logging.md`](docs/mqtt-realtime-logging.md).

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

## Contributing

Issues and pull requests are welcome! Please include:
- Home Assistant version
- Device model number
- Steps to reproduce

## Support

- **Issues:** GitHub Issues
- **Discussions:** GitHub Discussions
- **Documentation:** see the [`docs/`](docs/) folder

---

Built with ❤️ for Home Assistant enthusiasts.
