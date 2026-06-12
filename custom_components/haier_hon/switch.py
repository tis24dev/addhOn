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
        if data.get("type") in APPLIANCE_WASH_GROUP:
            appliance = data.get("appliance")
            if appliance and hasattr(appliance, "commands"):
                cmds = appliance.commands if isinstance(appliance.commands, dict) else {}
                if "pauseProgram" in cmds and "resumeProgram" in cmds:
                    entities.append(HonWashingMachinePauseSwitch(coordinator, appliance_id, client))
            _LOGGER.info("Aggiunto switch: %s", data.get("name"))
    async_add_entities(entities)

class HonWashingMachinePauseSwitch(HonBaseEntity, SwitchEntity):
    """Switch per mettere in pausa / riprendere il programma lavatrice."""

    _attr_icon = "mdi:pause-circle"

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        device_name = self._appliance_data.get("name", "Lavatrice")
        self._attr_unique_id = f"{appliance_id}_pause"
        self._attr_name = f"{device_name} - Pausa"

    @property
    def is_on(self) -> bool:
        val = self._get_attr(WM_ATTR_STATUS, "0")
        return str(val) == "2"

    async def _send_pause_command(self, command_name: str, pause_value: str) -> None:
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError("Pausa: appliance o client non disponibile")
        try:
            def _do():
                async def _inner():
                    commands = appliance.commands if isinstance(appliance.commands, dict) else {}
                    command = commands.get(command_name)
                    if not command:
                        raise RuntimeError(f"Comando '{command_name}' non trovato")
                    if hasattr(command, "parameters") and "pause" in command.parameters:
                        command.parameters["pause"].value = pause_value
                    await command.send()
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
