"""Switch per Haier hOn - pausa lavatrice/asciugatrice."""
from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity
from .const import APPLIANCE_WASH_GROUP, DOMAIN, WM_ATTR_STATUS

_LOGGER = logging.getLogger(__name__)


def _command_names(appliance) -> list[str]:
    commands = getattr(appliance, "commands", None)
    return sorted(commands.keys()) if isinstance(commands, dict) else []


def _param_snapshot(params) -> dict:
    if not isinstance(params, dict):
        return {"<non-dict>": type(params).__name__}
    return {
        str(name): getattr(param, "value", None)
        for name, param in params.items()
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # FIX: accesso coerente alla struttura hass.data[DOMAIN][entry_id]["coordinator"]
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities = []
    for appliance_id, data in coordinator.data.items():
        app_type = data.get("type")
        appliance = data.get("appliance")
        _LOGGER.debug(
            "Switch debug: valuto appliance '%s' id=%s type=%s commands=%s",
            data.get("name"),
            appliance_id,
            app_type,
            _command_names(appliance),
        )
        if app_type in APPLIANCE_WASH_GROUP:
            if appliance and hasattr(appliance, "commands"):
                cmds = getattr(appliance, "commands", None)
                cmds = cmds if isinstance(cmds, dict) else {}
                if "pauseProgram" in cmds and "resumeProgram" in cmds:
                    _LOGGER.debug("Switch debug: creo switch pausa per id=%s", appliance_id)
                    entities.append(HonWashingMachinePauseSwitch(coordinator, appliance_id, client))
                    _LOGGER.info("Aggiunto switch: %s", data.get("name"))
                else:
                    _LOGGER.debug(
                        "Switch debug: switch pausa non creato per id=%s; pause/resume mancanti",
                        appliance_id,
                    )
        else:
            _LOGGER.debug("Switch debug: appliance id=%s ignorato, type=%s", appliance_id, app_type)
    async_add_entities(entities)

class HonWashingMachinePauseSwitch(HonBaseEntity, SwitchEntity):
    """Switch per mettere in pausa / riprendere il programma lavatrice."""

    _attr_icon = "mdi:pause-circle"

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        device_name = self._appliance_data.get("name", "Lavatrice")
        self._attr_unique_id = f"{appliance_id}_pause"
        self._attr_name = f"{device_name} - Pausa"
        _LOGGER.debug("Switch debug: inizializzato '%s' id=%s", self._attr_name, appliance_id)

    @property
    def is_on(self) -> bool:
        val = self._get_attr(WM_ATTR_STATUS, "0")
        is_paused = str(val) == "2"
        _LOGGER.debug(
            "Switch debug: is_on '%s' id=%s machMode=%s -> %s",
            self._attr_name,
            self._appliance_id,
            val,
            is_paused,
        )
        return is_paused

    async def _send_pause_command(self, command_name: str, pause_value: str) -> None:
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError("Pausa: appliance o client non disponibile")
        _LOGGER.debug(
            "Switch debug: invio comando pausa '%s' value=%s id=%s commands=%s",
            command_name,
            pause_value,
            self._appliance_id,
            _command_names(appliance),
        )
        try:
            def _do():
                async def _inner():
                    commands = getattr(appliance, "commands", None)
                    commands = commands if isinstance(commands, dict) else {}
                    command = commands.get(command_name)
                    if not command:
                        raise RuntimeError(f"Comando '{command_name}' non trovato")
                    params = getattr(command, "parameters", {})
                    _LOGGER.debug(
                        "Switch debug: command '%s' params prima=%s",
                        command_name,
                        _param_snapshot(params),
                    )
                    if isinstance(params, dict) and "pause" in params:
                        previous = getattr(params["pause"], "value", None)
                        params["pause"].value = pause_value
                        _LOGGER.debug(
                            "Switch debug: parametro pause impostato a %s (previous=%s)",
                            pause_value,
                            previous,
                        )
                    else:
                        _LOGGER.debug(
                            "Switch debug: command '%s' senza parametro pause; invio senza modifica",
                            command_name,
                        )
                    await command.send()
                    _LOGGER.debug("Switch debug: command '%s' send completato", command_name)
                client.run_command_sync(_inner())

            await self.hass.async_add_executor_job(_do)
            _LOGGER.info("Pausa: %s inviato", command_name)
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error("Pausa %s: Errore: %s", command_name, err, exc_info=True)
            raise HomeAssistantError(f"Pausa {command_name}: errore comando: {err}") from err

    async def async_turn_on(self, **kwargs) -> None:
        await self._send_pause_command("pauseProgram", "1")

    async def async_turn_off(self, **kwargs) -> None:
        await self._send_pause_command("resumeProgram", "0")
