# MQTT realtime logging

The integration pulls device data by polling. On top of that, the integration's
native client also opens an **MQTT realtime channel** (AWS IoT) to receive
instant push updates. That channel is optional: if it never connects, sensors
still update every poll cycle.

## Why the log was noisy

The native MQTT client logs one INFO line for every (re)connect attempt
(`Lifecycle Attempting Connect`, `Connection Failure`, `Disconnection`, ...).
When the realtime push slot is **contended**, those attempts fail in a loop and
flood the log. This is expected and **not a bug**: the data is still correct,
delivered by polling. See [Background](#background-why-it-flaps) below.

## Default behaviour: silenced

At setup the integration lowers the `custom_components.addhon.client.transport.mqtt`
logger to `WARNING`, so the reconnect chatter is hidden while real warnings/errors
still come through. Nothing else changes; polling is unaffected.

## Enable debug logging (on demand)

For missing devices or discovery issues, start with
[Discovery debugging](discovery-debugging.md). MQTT is optional and does not
control device enumeration.

The simplest way is the persistent UI toggle: open the integration, choose
**Configure**, and turn on **Enable MQTT realtime debug**. It survives restarts
and only covers this integration's MQTT logger (not the underlying AWS IoT
libraries). Turn it off to silence the channel again.

When you prefer a one-off (non-persistent) change, raise the level with the
dedicated service:

```yaml
action: addhon.set_mqtt_log_level
data:
  level: debug      # debug | info | warning | error
```

(Developer Tools -> Actions, or from an automation/script.)

To go back to quiet:

```yaml
action: addhon.set_mqtt_log_level
data:
  level: warning
```

The service change is **not persistent**: after a Home Assistant restart the
channel is silenced again (the service is opt-in per session; use the Configure
toggle above for a persistent setting). The built-in `logger.set_level` action
with `custom_components.addhon.client.transport.mqtt: debug` does the same.

## Background: why it flaps

The hOn cloud authorises the MQTT connection with the account's token through an
AWS IoT custom authorizer; the realtime stream for an appliance is tied to its
**owner** account. With a shared/guest account (the usual "second account for
Home Assistant" setup) the realtime slot is reclaimed by the owner's session,
so Home Assistant only holds it for a few seconds per token-refresh window and
gets `NOT_AUTHORIZED` the rest of the time. There is no integration-side fix:
realtime push on a shared appliance is inherently single-subscriber, and polling
already keeps every entity up to date.

## Where it lives (code)

- `custom_components/addhon/logging_utils.py` - logger names, level map,
  `apply_mqtt_log_level()` / `silence_mqtt_noise()` (no Home Assistant imports,
  unit-tested in isolation).
- `custom_components/addhon/__init__.py` - applies the default silence and
  registers the `set_mqtt_log_level` service on setup.
- `custom_components/addhon/services.yaml` - service definition.
- `tests/test_mqtt_log_level.py` - tests.
