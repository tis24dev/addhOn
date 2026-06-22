# Debug device

Every configured hOn account gets a dedicated **addhOn diagnostica** device: a
Home Assistant *service* device that collects the integration's debug controls in
one place, so you can flip them from a dashboard without opening the **Configure**
dialog. It stays out of your room dashboards because it is a service device, not a
physical appliance.

Find it under **Settings -> Devices & Services -> addhOn -> addhOn diagnostica**.

## What it exposes

| Entity | Type | Section | What it does |
|--------|------|---------|--------------|
| Debug logging | switch | Configuration | Raise the integration loggers to DEBUG. Persisted (same value as the Options flow toggle). |
| MQTT realtime debug | switch | Configuration | Make the MQTT realtime channel verbose. Persisted. |
| Refresh now | button | Configuration | Force an immediate data refresh (handy when debugging discovery or polling). |
| Reset debug | button | Configuration | Turn both toggles off and restore the default log levels (also clears a runtime `set_log_level` override). |
| Debug status | sensor | Diagnostic | At a glance: `off` / `integration` / `mqtt` / `full`. |
| Integration log level | sensor | Diagnostic | Effective level of `custom_components.addhon`. |
| MQTT log level | sensor | Diagnostic | Effective level of the MQTT realtime logger. |
| Appliances discovered | sensor | Diagnostic | Number of appliances returned by the last refresh. |
| Last refresh | sensor | Diagnostic | Timestamp of the last successful refresh. |
| Update OK | binary sensor | Diagnostic | Whether the last refresh succeeded. |

The two switches stay in sync with the **Configure** dialog (changing one updates
the other). The realtime services `addhon.set_log_level` and
`addhon.set_mqtt_log_level` still work for one-off, non-persistent changes.

> Note: the log levels are process-global. With a single account (the usual case)
> everything is consistent. If you configure more than one hOn account, the log
> level is shared across all of them, so the per-account log-level sensors reflect
> the whole process, not just that account.

## Dashboard card (core cards, no extra HACS frontend)

The entity ids below assume Home Assistant's default slug for the device name
(`addhon_diagnostica`). Confirm the real ids in **Developer Tools -> States** (they
change if you rename the device).

### Entities card

```yaml
type: entities
title: addhOn Debug
icon: mdi:bug
show_header_toggle: false
state_color: true
entities:
  - type: section
    label: Toggles
  - entity: switch.addhon_diagnostica_debug_logging
    name: Debug logging
  - entity: switch.addhon_diagnostica_mqtt_realtime_debug
    name: MQTT realtime debug
  - type: section
    label: Status
  - entity: sensor.addhon_diagnostica_debug_status
    name: Debug status
  - entity: sensor.addhon_diagnostica_integration_log_level
    name: Integration log level
  - entity: sensor.addhon_diagnostica_mqtt_log_level
    name: MQTT log level
  - entity: sensor.addhon_diagnostica_appliances_discovered
    name: Appliances discovered
  - entity: sensor.addhon_diagnostica_last_refresh
    name: Last refresh
  - entity: binary_sensor.addhon_diagnostica_update_ok
    name: Update OK
  - type: divider
  - entity: button.addhon_diagnostica_refresh_now
    name: Refresh now
  - entity: button.addhon_diagnostica_reset_debug
    name: Reset debug
```

### Tile grid (more compact)

```yaml
type: grid
columns: 2
square: false
cards:
  - type: tile
    entity: switch.addhon_diagnostica_debug_logging
    name: Debug logging
    color: red
    tap_action:
      action: toggle
  - type: tile
    entity: switch.addhon_diagnostica_mqtt_realtime_debug
    name: MQTT realtime
    color: amber
    tap_action:
      action: toggle
  - type: tile
    entity: sensor.addhon_diagnostica_debug_status
    name: Status
  - type: tile
    entity: button.addhon_diagnostica_refresh_now
    name: Refresh now
    tap_action:
      action: perform-action
      perform_action: button.press
      target:
        entity_id: button.addhon_diagnostica_refresh_now
```

Remove the lines for any entity you do not use; the cards degrade gracefully.
