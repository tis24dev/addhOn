"""Control of the diagnostic log levels for the integration.

The MQTT client (now OUR ``client.transport.mqtt``) emits an
INFO-level message for every realtime (re)connection attempt ("Lifecycle
Attempting Connect / Connection Failure / Disconnection / Connection Success").
When the push slot is contended (e.g. the appliance is shared and the owner
holds the channel), these attempts fail in a continuous loop and flood the log
even though there is nothing to do on the integration side: the data stays up to
date via polling anyway.

By default we lower that logger to WARNING (the attempts disappear, any real
warnings/errors remain). The ``addhon.set_mqtt_log_level`` service raises the
level on the fly (e.g. ``debug``) to diagnose realtime problems, and lowers it
back to ``warning`` to silence it again.

No intra-package or homeassistant imports: the module can be loaded in isolation
(via importlib) and is therefore testable without Home Assistant stubs.
"""
from __future__ import annotations

import logging

# Loggers to raise/lower when discovery, setup, reauth and polling need to be
# diagnosed. The whole client is now native, so this is
# the only namespace. The MQTT logger stays separate under MQTT_NOISE_LOGGERS, so
# discovery debug does not turn the realtime noise back on.
INTEGRATION_DEBUG_LOGGERS: tuple[str, ...] = (
    "custom_components.addhon",
)

# Loggers responsible for the realtime MQTT noise: the MQTT client is OURS.
MQTT_NOISE_LOGGERS: tuple[str, ...] = (
    "custom_components.addhon.client.transport.mqtt",
)

# Level applied by default: hides the INFO/DEBUG attempts, lets real
# warnings and errors through.
DEFAULT_MQTT_LOG_LEVEL = logging.WARNING

# Level names accepted by the service, mapped onto the logging module's values.
MQTT_LOG_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def apply_mqtt_log_level(level: int) -> None:
    """Set ``level`` on all the noisy realtime MQTT loggers."""
    for name in MQTT_NOISE_LOGGERS:
        logging.getLogger(name).setLevel(level)


def apply_integration_log_level(level: int) -> None:
    """Set ``level`` on the loggers useful for setup/discovery/polling debug."""
    for name in INTEGRATION_DEBUG_LOGGERS:
        logging.getLogger(name).setLevel(level)


def silence_mqtt_noise() -> None:
    """Apply the default level: silence the reconnection attempts."""
    apply_mqtt_log_level(DEFAULT_MQTT_LOG_LEVEL)


def reset_integration_log_level() -> None:
    """Reset the integration loggers to NOTSET (they inherit HA's level).

    Used when the "Enable debug log" toggle is turned off: instead of forcing a
    fixed level (e.g. WARNING) we remove the override, so the loggers go back to
    inheriting the level configured by Home Assistant. Does NOT touch the MQTT
    loggers (those stay managed by silence_mqtt_noise / set_mqtt_log_level).
    """
    for name in INTEGRATION_DEBUG_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)
