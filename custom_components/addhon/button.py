"""Buttons for explicit Haier hOn actions."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonAccountEntity, HonBaseEntity
from .const import (
    APPLIANCE_WASH_GROUP,
    CONF_ENABLE_DEBUG,
    CONF_ENABLE_MQTT_DEBUG,
    DOMAIN,
    PROGRAM_PARAM_NAMES,
    PROGRAM_PENDING_STORE,
)
from .debug_utils import command_names, param_snapshot
from .logging_utils import reset_integration_log_level, silence_mqtt_noise

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configure the buttons for explicit physical actions."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities = []
    for appliance_id, data in coordinator.data.items():
        app_type = data.get("type")
        _LOGGER.debug(
            "Button debug: evaluating appliance '%s' id=%s type=%s commands=%s",
            data.get("name"),
            appliance_id,
            app_type,
            command_names(data.get("appliance")),
        )
        if app_type not in APPLIANCE_WASH_GROUP:
            _LOGGER.debug("Button debug: appliance id=%s ignored, type=%s", appliance_id, app_type)
            continue
        appliance = data.get("appliance")
        commands = getattr(appliance, "commands", None)
        commands = commands if isinstance(commands, dict) else {}
        if "startProgram" in commands:
            _LOGGER.debug("Button debug: creating startProgram button for id=%s", appliance_id)
            entities.append(
                HonProgramCommandButton(
                    coordinator,
                    appliance_id,
                    client,
                    command_name="startProgram",
                    unique_suffix="start_program",
                    translation_key="start_program",
                    icon="mdi:play-circle",
                )
            )
        if "stopProgram" in commands:
            _LOGGER.debug("Button debug: creating stopProgram button for id=%s", appliance_id)
            entities.append(
                HonProgramCommandButton(
                    coordinator,
                    appliance_id,
                    client,
                    command_name="stopProgram",
                    unique_suffix="stop_program",
                    translation_key="stop_program",
                    icon="mdi:stop-circle",
                    command_parameters={"onOffStatus": "0"},
                )
            )
    # Account-level debug action buttons (one set per config entry).
    sw_version = entry_data.get("integration_version")
    entities.append(HonForceRefreshButton(coordinator, entry, sw_version))
    entities.append(HonResetDebugButton(entry, sw_version))
    async_add_entities(entities)


class HonProgramCommandButton(HonBaseEntity, ButtonEntity):
    """Button to send clearly explicit start/stop commands."""

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        client=None,
        *,
        command_name: str,
        unique_suffix: str,
        translation_key: str,
        icon: str,
        command_parameters: dict[str, str] | None = None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._command_name = command_name
        self._command_parameters = command_parameters or {}
        self._attr_unique_id = f"{appliance_id}_{unique_suffix}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        _LOGGER.debug(
            "Button debug: initialized '%s' id=%s command=%s fixed_params=%s",
            self._attr_unique_id,
            appliance_id,
            command_name,
            self._command_parameters,
        )

    async def async_press(self) -> None:
        """Send the explicit physical command."""
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )

        # Start: we apply the program chosen from the select (if present).
        # We read it here on the HA event loop and pass it into _inner.
        store = self._coordinator_store(PROGRAM_PENDING_STORE)
        pending_program = (
            store.get(self._appliance_id)
            if self._command_name == "startProgram"
            else None
        )
        _LOGGER.debug(
            "Button debug: press '%s' id=%s pending_program=%s store=%s commands=%s",
            self._command_name,
            self._appliance_id,
            pending_program,
            dict(store),
            command_names(appliance),
        )
        try:
            def _do():
                async def _inner():
                    commands = getattr(appliance, "commands", None)
                    commands = commands if isinstance(commands, dict) else {}
                    command = commands.get(self._command_name)
                    if not command:
                        raise RuntimeError(
                            f"Command '{self._command_name}' not found. "
                            f"Available: {list(commands.keys())}"
                        )
                    params = getattr(command, "parameters", {})
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "Button debug: before command '%s' params=%s",
                            self._command_name,
                            param_snapshot(params),
                        )
                    if pending_program is not None:
                        # Fail-safe: if we cannot attach the chosen program to
                        # startProgram, do NOT start (this avoids starting a
                        # program different from the selected one).
                        applied = False
                        if isinstance(params, dict):
                            for pname in PROGRAM_PARAM_NAMES:
                                if pname in params:
                                    previous = getattr(params[pname], "value", None)
                                    params[pname].value = pending_program
                                    applied = True
                                    _LOGGER.debug(
                                        "Button debug: applied pending_program=%s to parameter '%s' "
                                        "(previous=%s)",
                                        pending_program,
                                        pname,
                                        previous,
                                    )
                                    break
                        if not applied:
                            available = (
                                list(params.keys()) if isinstance(params, dict) else params
                            )
                            raise RuntimeError(
                                "Selected program not applicable to "
                                f"'{self._command_name}': no parameter "
                                f"{PROGRAM_PARAM_NAMES} among {available}"
                            )
                    for name, value in self._command_parameters.items():
                        if name in params:
                            previous = getattr(params[name], "value", None)
                            params[name].value = value
                            _LOGGER.debug(
                                "Button debug: set fixed parameter '%s'=%s (previous=%s)",
                                name,
                                value,
                                previous,
                            )
                        else:
                            _LOGGER.debug(
                                "Button debug: fixed parameter '%s' not present in command '%s'",
                                name,
                                self._command_name,
                            )
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "Button debug: sending command '%s' final_params=%s",
                            self._command_name,
                            param_snapshot(params),
                        )
                    await command.send()
                    _LOGGER.debug("Button debug: command '%s' send completed", self._command_name)

                client.run_command_sync(_inner())

            await self.hass.async_add_executor_job(_do)
            # Start succeeded: the program is now "real", we clear the pending
            # choice so the select goes back to reflecting the device state.
            if pending_program is not None:
                store.pop(self._appliance_id, None)
                _LOGGER.debug(
                    "Button debug: pending program consumed and removed, store=%s",
                    dict(store),
                )
            _LOGGER.info("Button: command '%s' sent", self._command_name)
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error(
                "Button %s: command error: %s",
                self._command_name, err, exc_info=True,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err


class HonForceRefreshButton(HonAccountEntity, ButtonEntity):
    """Force an immediate coordinator refresh (debug polling/discovery)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:refresh"
    _attr_translation_key = "force_refresh"

    def __init__(self, coordinator, entry, sw_version: str | None = None) -> None:
        super().__init__(entry, "force_refresh", sw_version)
        self._coordinator = coordinator

    async def async_press(self) -> None:
        _LOGGER.debug(
            "Button debug: force refresh requested (entry=%s)",
            getattr(self._entry, "entry_id", None),
        )
        await self._coordinator.async_request_refresh()


class HonResetDebugButton(HonAccountEntity, ButtonEntity):
    """Turn both debug toggles off and restore the default log levels.

    Always resets the loggers (clears any runtime ``set_log_level`` override) and,
    if either toggle is on, persists them off via ``async_update_entry`` (which the
    options listener then re-applies).
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:restart"
    _attr_translation_key = "reset_debug"

    def __init__(self, entry, sw_version: str | None = None) -> None:
        super().__init__(entry, "reset_debug", sw_version)

    async def async_press(self) -> None:
        _LOGGER.debug(
            "Button debug: reset debug requested (entry=%s)",
            getattr(self._entry, "entry_id", None),
        )
        reset_integration_log_level()
        silence_mqtt_noise()
        options = self._entry_options
        if options.get(CONF_ENABLE_DEBUG) or options.get(CONF_ENABLE_MQTT_DEBUG):
            self.hass.config_entries.async_update_entry(
                self._entry,
                options={
                    **options,
                    CONF_ENABLE_DEBUG: False,
                    CONF_ENABLE_MQTT_DEBUG: False,
                },
            )
