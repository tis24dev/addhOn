import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

try:
    # In real Home Assistant these symbols always exist. The import is tolerant
    # only for the test harness, which stubs homeassistant.core with the bare
    # minimum (shared sys.modules: the first stub wins, so it is more robust to
    # degrade here than to extend every stub).
    from homeassistant.core import ServiceCall, callback
except ImportError:  # pragma: no cover - only under the test stub
    ServiceCall = object  # type: ignore[assignment,misc]

    def callback(func):  # type: ignore[no-redef]
        return func
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    APPLIANCE_TD,
    ATTR_LEVEL,
    CONF_ENABLE_DEBUG,
    CONF_ENABLE_MQTT_DEBUG,
    DOMAIN,
    PLATFORMS,
    SCAN_INTERVAL,
    SERVICE_SET_LOG_LEVEL,
    SERVICE_SET_MQTT_LOG_LEVEL,
)
from .logging_utils import (
    MQTT_LOG_LEVELS,
    apply_integration_log_level,
    apply_mqtt_log_level,
    reset_integration_log_level,
    silence_mqtt_noise,
)

_LOGGER = logging.getLogger(__name__)


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register (only once) the service for the MQTT log level.

    On the first registration it also applies the default silencing of the
    realtime MQTT noise. The service is global to the domain, not per-entry, so it
    is idempotent: if already present it does nothing.

    voluptuous is imported here (not at module level) so the import of __init__
    does not depend on voluptuous: the test harness imports the package without
    always providing its stub, while this function only runs in real HA.
    """
    mqtt_service_exists = hass.services.has_service(DOMAIN, SERVICE_SET_MQTT_LOG_LEVEL)
    log_service_exists = hass.services.has_service(DOMAIN, SERVICE_SET_LOG_LEVEL)
    if mqtt_service_exists and log_service_exists:
        return

    import voluptuous as vol

    # First registration (HA start/restart): silence the noise by default.
    # On a reload of a single entry the service stays registered, so a debug level
    # possibly set at runtime is not re-silenced.
    if not mqtt_service_exists:
        silence_mqtt_noise()

    async def _handle_set_mqtt_log_level(call: ServiceCall) -> None:
        level_name = call.data[ATTR_LEVEL]
        apply_mqtt_log_level(MQTT_LOG_LEVELS[level_name])
        _LOGGER.info(
            "realtime MQTT log level set to %s", level_name.upper()
        )

    async def _handle_set_log_level(call: ServiceCall) -> None:
        level_name = call.data[ATTR_LEVEL]
        apply_integration_log_level(MQTT_LOG_LEVELS[level_name])
        _LOGGER.info(
            "Haier hOn diagnostic log level set to %s", level_name.upper()
        )

    level_schema = vol.Schema(
        {vol.Required(ATTR_LEVEL, default="debug"): vol.In(tuple(MQTT_LOG_LEVELS))}
    )

    if not mqtt_service_exists:
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_MQTT_LOG_LEVEL,
            _handle_set_mqtt_log_level,
            schema=level_schema,
        )

    if not log_service_exists:
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_LOG_LEVEL,
            _handle_set_log_level,
            schema=level_schema,
        )


@callback
def _apply_debug_options(entry: ConfigEntry, *, reset_when_off: bool = True) -> None:
    """Align the log levels to the two toggles persisted in entry.options.

    enable_debug=True  -> integration logger to DEBUG; False -> NOTSET
                          (they go back to inheriting the level configured in HA).
    enable_mqtt_debug=True -> realtime MQTT logger to DEBUG; False -> WARNING
                          (silenced).

    The MQTT level is applied AFTER the integration's one, so the explicit level
    of the MQTT child wins over the parent's cascade: enabling the integration's
    DEBUG does NOT turn the realtime noise back on if the MQTT toggle is off. NB
    the loggers are global to the process (see OptionsFlowHandler): with more than
    one entry (rare, multi-account) the levels are shared and changing the options
    of one entry re-applies them based on THAT entry, possibly resetting another
    one's active debug. The typical installation has a single account.

    reset_when_off=True (default, used by the options listener): an OFF toggle
    RESETS the level (NOTSET / WARNING), so disabling it from the UI takes effect
    immediately and also clears any manual override done with the set_log_level
    service. reset_when_off=False (used in async_setup_entry): an OFF toggle does
    NOT touch the loggers, so an integration DEBUG set at runtime via the services
    survives re-setups/retries (e.g. an unstable login) instead of being reset on
    every attempt; the default MQTT silencing on the first registration is still
    guaranteed by _async_register_services (which, however, on a reload of the only
    entry that removes and re-registers the services, also re-silences any MQTT
    level raised at runtime).
    """
    if entry.options.get(CONF_ENABLE_DEBUG, False):
        apply_integration_log_level(logging.DEBUG)
    elif reset_when_off:
        reset_integration_log_level()
    if entry.options.get(CONF_ENABLE_MQTT_DEBUG, False):
        apply_mqtt_log_level(logging.DEBUG)
    elif reset_when_off:
        silence_mqtt_noise()


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-apply the log levels on the fly when the toggles change (no reload).

    A reload would tear down auth and the MQTT channel just to change a log level;
    here we re-apply the levels on the fly, as the existing services do.
    """
    _LOGGER.debug(
        "Options debug: options updated entry=%s enable_debug=%s enable_mqtt_debug=%s",
        entry.entry_id,
        entry.options.get(CONF_ENABLE_DEBUG, False),
        entry.options.get(CONF_ENABLE_MQTT_DEBUG, False),
    )
    _apply_debug_options(entry)


def _redact_email(email: str | None) -> str | None:
    if not email:
        return None
    if "@" not in email:
        return "***"
    _, domain = email.split("@", 1)
    return f"***@{domain}"


def _redact_title(title: str | None) -> str | None:
    if not title or "@" not in title:
        return title
    prefix, domain_and_suffix = title.rsplit("@", 1)
    open_paren = prefix.rfind("(")
    safe_prefix = prefix[: open_paren + 1] if open_paren >= 0 else ""
    return f"{safe_prefix}***@{domain_and_suffix}"


async def _async_close_client(client) -> None:
    """Close HonClient without masking the original setup/unload error."""
    try:
        await client.async_close()
    except Exception as err:
        _LOGGER.warning("Error closing HonClient: %s", err)


# "Washer-only" sensors that were mistakenly created on the tumble dryers (TD)
# too: a tumble dryer does not use water and does not report loadingPercentage
# (the app gates that statistic to WM/WD), so they stayed forever "unknown"
# entities. After the per-type refactor they are no longer created: here we clean
# up the ones already registered, ONLY on TD devices.
_TD_REMOVED_SUFFIXES = (
    "_total_water",
    "_total_energy",
    "_current_energy",
    "_current_water",
    "_loading_percentage",
)


def _remove_legacy_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove from the registry the legacy entities no longer provided by the integration.

    - "Power" switch (unique_id '<id>_power'), removed in the 2.3/2.4 refactor.
    - Washer-only sensors on the tumble dryers (TD): '<td_id>_total_water',
      '_total_energy', '_current_energy', '_current_water', '_loading_percentage'.
      Removed ONLY on devices of type TD (cross-checked with the coordinator),
      never on WM/WD/AC.

    Without this cleanup there would be orphan 'unavailable' entities with the '?' badge.
    """
    from homeassistant.helpers import entity_registry as er

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get("coordinator")
    coord_data = getattr(coordinator, "data", None)
    td_ids = {
        appliance_id
        for appliance_id, device in (coord_data or {}).items()
        if isinstance(device, dict) and device.get("type") == APPLIANCE_TD
    }
    td_orphans = {
        f"{appliance_id}{suffix}"
        for appliance_id in td_ids
        for suffix in _TD_REMOVED_SUFFIXES
    }

    registry = er.async_get(hass)
    checked = 0
    removed = 0
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        checked += 1
        unique_id = reg_entry.unique_id or ""
        if unique_id.endswith("_power"):
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            _LOGGER.info("Removed legacy power entity: %s", reg_entry.entity_id)
        elif unique_id in td_orphans:
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            _LOGGER.info(
                "Removed invalid consumption entity for tumble dryer: %s", reg_entry.entity_id
            )
    _LOGGER.debug(
        "Setup debug: legacy cleanup completed for entry=%s, checked=%d, removed=%d",
        entry.entry_id,
        checked,
        removed,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Haier hOn integration from a Config Entry."""
    from .hon_client import HonClient, _requires_reauth

    # Silence by default the noise of the realtime MQTT attempts and register
    # the debug service. Done BEFORE the client setup so the logger is already at
    # WARNING when the MQTT client starts to (re)connect.
    _async_register_services(hass)

    # Apply the persisted debug toggles RIGHT AWAY, but AFTER _async_register_services
    # (which on the first registration silences the MQTT noise by default) so the
    # persisted MQTT toggle, if active, wins over that silencing. Applying them here
    # and not at the end of setup makes the DEBUG level cover the setup path too
    # (login, discovery, first refresh): that is exactly what one wants to trace when
    # enabling debug for discovery problems. reset_when_off=False: an OFF toggle must
    # not reset a DEBUG set at runtime via the services, which must survive the
    # retries of a failing setup (the default MQTT silencing stays guaranteed by
    # _async_register_services).
    _apply_debug_options(entry, reset_when_off=False)

    # FIX: the key saved by the config_flow is "email", not "username"
    email = entry.data.get("email")
    password = entry.data.get("password")

    _LOGGER.debug(
        "Setup debug: starting setup entry=%s title=%s email=%s platforms=%s scan_interval=%ss",
        entry.entry_id,
        _redact_title(getattr(entry, "title", None)),
        _redact_email(email),
        PLATFORMS,
        SCAN_INTERVAL,
    )

    if not email:
        _LOGGER.error(
            "Missing credentials in the config entry ('email' key absent). "
            "Remove and reconfigure the integration."
        )
        return False

    hon_client = HonClient(email=email, password=password)

    # Initial client setup in executor (does not block HA's event loop)
    try:
        _LOGGER.debug("Setup debug: running HonClient.setup_sync in executor")
        await hass.async_add_executor_job(hon_client.setup_sync)
        _LOGGER.debug("Setup debug: HonClient.setup_sync completed")
    except asyncio.CancelledError:
        await _async_close_client(hon_client)
        raise
    except Exception as err:
        _LOGGER.error("Unable to connect to hOn: %s", err)
        await _async_close_client(hon_client)
        if _requires_reauth(err):
            raise ConfigEntryAuthFailed(f"Invalid hOn credentials: {err}") from err
        raise ConfigEntryNotReady(f"Unable to connect to hOn: {err}") from err

    async def async_update_data() -> dict:
        """Fetch the updated data from all the hOn devices."""
        try:
            _LOGGER.debug("Coordinator debug: starting hOn data update")
            data = await hon_client.async_get_appliances_data()
            summary = [
                {
                    "id": appliance_id,
                    "name": appliance_data.get("name"),
                    "type": appliance_data.get("type"),
                    "mac": appliance_data.get("mac"),
                    "attributes": len(appliance_data.get("attributes", {}))
                    if isinstance(appliance_data.get("attributes"), dict)
                    else 0,
                    "settings": len(appliance_data.get("settings", {}))
                    if isinstance(appliance_data.get("settings"), dict)
                    else 0,
                }
                for appliance_id, appliance_data in data.items()
            ]
            _LOGGER.debug(
                "Coordinator debug: hOn data update completed, devices=%d summary=%s",
                len(data),
                summary,
            )
            return data
        except Exception as err:
            _LOGGER.debug("Coordinator debug: hOn data update failed: %s", err, exc_info=True)
            if _requires_reauth(err):
                raise ConfigEntryAuthFailed(f"Invalid hOn credentials: {err}") from err
            raise UpdateFailed(f"hOn update error: {err}") from err

    stored = False
    try:
        coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Haier hOn data",
            update_method=async_update_data,
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )

        # First fetch
        _LOGGER.debug("Setup debug: first coordinator refresh at startup")
        await coordinator.async_config_entry_first_refresh()
        _LOGGER.debug(
            "Setup debug: first refresh completed, last_update_success=%s devices=%d",
            getattr(coordinator, "last_update_success", None),
            len(coordinator.data) if isinstance(coordinator.data, dict) else 0,
        )
        coordinator.hon_client = hon_client

        # Integration version, for the diagnostics device's sw_version ("Firmware:"
        # row on the device card). Lazy import so the test stubs that import this
        # package do not need to stub homeassistant.loader; tolerant if unavailable.
        integration_version: str | None = None
        try:
            from homeassistant.loader import async_get_integration

            integration = await async_get_integration(hass, DOMAIN)
            integration_version = str(integration.version)
        except Exception as err:  # pragma: no cover - non-critical, cosmetic only
            _LOGGER.debug("Setup debug: could not resolve integration version: %s", err)

        # FIX: store both the coordinator and the client in the structure expected by all platforms
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN][entry.entry_id] = {
            "coordinator": coordinator,
            "client": hon_client,
            "integration_version": integration_version,
        }
        stored = True
        _LOGGER.debug("Setup debug: coordinator and client stored in hass.data for entry=%s", entry.entry_id)

        # Legacy entity cleanup (e.g. the removed "Power" switch): it must never
        # block the setup, so we absorb any registry errors.
        try:
            _remove_legacy_entities(hass, entry)
        except Exception as err:
            _LOGGER.debug("Legacy entity cleanup failed: %s", err)

        _LOGGER.debug("Setup debug: forwarding platforms %s", PLATFORMS)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _LOGGER.debug("Setup debug: platform forwarding completed")
    except asyncio.CancelledError:
        if stored:
            unload_platforms = getattr(hass.config_entries, "async_unload_platforms", None)
            if callable(unload_platforms):
                try:
                    await unload_platforms(entry, PLATFORMS)
                except Exception as err:
                    _LOGGER.warning("Error unloading platforms after cancelled setup: %s", err)
            hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        await _async_close_client(hon_client)
        raise
    except Exception:
        if stored:
            unload_platforms = getattr(hass.config_entries, "async_unload_platforms", None)
            if callable(unload_platforms):
                try:
                    await unload_platforms(entry, PLATFORMS)
                except Exception as err:
                    _LOGGER.warning("Error unloading platforms after failed setup: %s", err)
            hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        await _async_close_client(hon_client)
        raise

    # Setup succeeded: register a listener that re-applies the debug toggles on the
    # fly when they change (async_on_unload removes the listener when the entry is
    # unloaded, without a reload). The levels have already been applied at the start
    # of setup; here it only remains to hook up the on-the-fly update.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry when the integration is disabled."""
    _LOGGER.debug("Unload debug: unloading entry=%s platforms=%s", entry.entry_id, PLATFORMS)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    _LOGGER.debug("Unload debug: async_unload_platforms result=%s", unload_ok)
    if unload_ok:
        entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
        client = entry_data.get("client")
        if client is not None:
            _LOGGER.debug("Unload debug: closing HonClient for entry=%s", entry.entry_id)
            await _async_close_client(client)
        else:
            _LOGGER.debug("Unload debug: no HonClient to close for entry=%s", entry.entry_id)
        # Last entry removed: remove the global debug services.
        if not hass.data.get(DOMAIN):
            for service in (SERVICE_SET_MQTT_LOG_LEVEL, SERVICE_SET_LOG_LEVEL):
                if hass.services.has_service(DOMAIN, service):
                    hass.services.async_remove(DOMAIN, service)
                    _LOGGER.debug("Unload debug: removed service %s", service)
    return unload_ok
