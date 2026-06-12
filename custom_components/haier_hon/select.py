"""Select per Haier hOn - selezione programma lavatrice."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity
from .const import APPLIANCE_WASH_GROUP, DOMAIN

_LOGGER = logging.getLogger(__name__)

PROGRAM_PARAM_NAMES = ("program", "prCode")
PROGRAM_SELECT_COMMANDS = ("settings", "setProgram", "setProgramme", "programSettings")


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
        appliance = data.get("appliance")
        if (
            data.get("type") in APPLIANCE_WASH_GROUP
            and HonProgramSelect.supports_appliance(appliance)
        ):
            entities.append(HonProgramSelect(coordinator, appliance_id, client))
            _LOGGER.info("Aggiunto select programma: %s", data.get("name"))
    async_add_entities(entities)


class HonProgramSelect(HonBaseEntity, SelectEntity):
    """Select per la selezione del programma lavatrice/asciugatrice."""

    _attr_icon = "mdi:format-list-bulleted"

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        device_name = self._appliance_data.get("name", "Lavatrice")
        self._attr_unique_id = f"{appliance_id}_program"
        self._attr_name = f"{device_name} - Programma"

        self._program_map: dict[str, str] = {}
        appliance = self._appliance
        if appliance is not None:
            self._program_map = self._load_programs(appliance)

        self._program_reverse: dict[str, str] = {v: k for k, v in self._program_map.items()}
        self._attr_options = list(self._program_reverse.keys())

    @classmethod
    def supports_appliance(cls, appliance) -> bool:
        """Ritorna True solo se esiste un comando programma non-start."""
        command_info = cls._find_program_command(appliance)
        if command_info is None:
            return False
        _, command, param_name = command_info
        return bool(cls._program_values(command, param_name))

    @staticmethod
    def _find_program_command(appliance):
        if appliance is None:
            return None
        commands = appliance.commands if isinstance(appliance.commands, dict) else {}
        for command_name in PROGRAM_SELECT_COMMANDS:
            command = commands.get(command_name)
            if command is None:
                continue
            params = getattr(command, "parameters", None)
            if not isinstance(params, dict):
                continue
            for param_name in PROGRAM_PARAM_NAMES:
                if param_name in params:
                    return command_name, command, param_name
        return None

    @staticmethod
    def _program_values(command, param_name: str) -> dict[str, str]:
        params = getattr(command, "parameters", {})
        prog_param = params.get(param_name) if isinstance(params, dict) else None
        if prog_param is None:
            return {}
        for attr in ("values", "value_list", "options"):
            raw = getattr(prog_param, attr, None)
            if isinstance(raw, dict):
                return {str(code): str(label) for code, label in raw.items()}
            if isinstance(raw, (list, tuple)):
                return {str(value): str(value) for value in raw}
        return {}

    @staticmethod
    def _load_programs(appliance) -> dict[str, str]:
        try:
            command_info = HonProgramSelect._find_program_command(appliance)
            if command_info is None:
                return {}
            _, command, param_name = command_info
            values = HonProgramSelect._program_values(command, param_name)
            if not values:
                return {}
            return {
                str(code): str(label) if label else str(code)
                for code, label in values.items()
            }
        except Exception as err:
            _LOGGER.debug("Errore caricamento programmi dinamici: %s", err)
            return {}

    @property
    def current_option(self) -> str | None:
        # FIX: controllare esplicitamente is not None invece di usare 'or' che scarta 0
        code = None
        for key in (
            "settings.program",
            "settings.prCode",
            "startProgram.program",
            "startProgram.prCode",
            "prCode",
        ):
            val = self._get_attr(key)
            if val is not None:
                code = val
                break
        
        if code is None:
            return None
        return self._program_map.get(str(code))

    async def async_select_option(self, option: str) -> None:
        code = self._program_reverse.get(option)
        if code is None:
            raise HomeAssistantError(f"Select: programma '{option}' non trovato nella mappa")
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError("Select: appliance o client non disponibile")
        try:
            def _do():
                async def _inner():
                    command_info = self._find_program_command(appliance)
                    if command_info is None:
                        commands = appliance.commands if isinstance(appliance.commands, dict) else {}
                        raise RuntimeError(
                            "Comando selezione programma sicuro non trovato. "
                            f"Disponibili: {list(commands.keys())}"
                        )
                    command_name, command, param_name = command_info
                    command.parameters[param_name].value = code
                    await command.send()

                client.run_command_sync(_inner())

            await self.hass.async_add_executor_job(_do)
            _LOGGER.info("Select: programma '%s' (code=%s) inviato", option, code)
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error("Select: errore selezione programma '%s': %s", option, err, exc_info=True)
            raise HomeAssistantError(
                f"Select: errore selezione programma '{option}': {err}"
            ) from err
