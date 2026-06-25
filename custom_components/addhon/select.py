"""Haier hOn select - washer program selection."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity
from .const import (
    APPLIANCE_WASH_GROUP,
    DOMAIN,
    PROGRAM_PARAM_NAMES,
    PROGRAM_PENDING_STORE,
)
from .debug_utils import redact_id, redact_store

_LOGGER = logging.getLogger(__name__)

# "Safe" commands (they do not start a cycle) to read/write the program from.
PROGRAM_SELECT_COMMANDS = ("settings", "setProgram", "setProgramme", "programSettings")
# Commands to DRAW the program list from. We also include startProgram as a
# metadata source: the selection is decoupled from the start (see
# async_select_option), so reading the options from startProgram does NOT start
# the appliance. Without this, the washers/dryers that expose the program only
# via startProgram were left without a select (orphan "unavailable" entity).
PROGRAM_SOURCE_COMMANDS = PROGRAM_SELECT_COMMANDS + ("startProgram",)


def _command_names(appliance) -> list[str]:
    commands = getattr(appliance, "commands", None)
    return sorted(commands.keys()) if isinstance(commands, dict) else []


def _param_names(command) -> list[str]:
    params = getattr(command, "parameters", None)
    return sorted(params.keys()) if isinstance(params, dict) else []


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # FIX: consistent access to the hass.data[DOMAIN][entry_id]["coordinator"] structure
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities = []
    for appliance_id, data in coordinator.data.items():
        appliance = data.get("appliance")
        app_type = data.get("type")
        _LOGGER.debug(
            "Select debug: evaluating appliance '%s' id=%s type=%s commands=%s",
            data.get("name"),
            redact_id(appliance_id),
            app_type,
            _command_names(appliance),
        )
        if app_type not in APPLIANCE_WASH_GROUP:
            _LOGGER.debug("Select debug: appliance id=%s ignored, type=%s", redact_id(appliance_id), app_type)
            continue
        if HonProgramSelect.supports_appliance(appliance):
            entities.append(HonProgramSelect(coordinator, appliance_id, client))
            _LOGGER.info("Added program select: id=%s", redact_id(appliance_id))
        else:
            _LOGGER.debug(
                "Select debug: no program select for '%s' id=%s; "
                "no source command with parameters %s",
                data.get("name"),
                redact_id(appliance_id),
                PROGRAM_PARAM_NAMES,
            )
    async_add_entities(entities)


class HonProgramSelect(HonBaseEntity, SelectEntity):
    """Select for the washer/dryer program selection."""

    _attr_icon = "mdi:format-list-bulleted"

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._attr_unique_id = f"{appliance_id}_program"
        self._attr_translation_key = "program"

        self._program_map: dict[str, str] = {}
        appliance = self._appliance
        if appliance is not None:
            self._program_map = self._load_programs(appliance)

        self._program_reverse: dict[str, str] = {v: k for k, v in self._program_map.items()}
        self._attr_options = list(self._program_reverse.keys())
        _LOGGER.debug(
            "Select debug: initialized '%s' id=%s programs=%d map=%s",
            redact_id(self._attr_unique_id, appliance_id),
            redact_id(appliance_id),
            len(self._program_map),
            self._program_map,
        )

    @classmethod
    def supports_appliance(cls, appliance) -> bool:
        """True if there is a command (including startProgram) with a populated
        program parameter from which to build the option list."""
        command_info = cls._find_program_command(appliance)
        if command_info is None:
            _LOGGER.debug(
                "Select debug: supports_appliance=False, no program command. commands=%s",
                _command_names(appliance),
            )
            return False
        _, command, param_name = command_info
        values = cls._program_values(command, param_name)
        _LOGGER.debug(
            "Select debug: supports_appliance command param=%s values_count=%d params=%s",
            param_name,
            len(values),
            _param_names(command),
        )
        return bool(values)

    @staticmethod
    def _find_program_command(appliance):
        if appliance is None:
            return None
        commands = getattr(appliance, "commands", None)
        commands = commands if isinstance(commands, dict) else {}
        for command_name in PROGRAM_SOURCE_COMMANDS:
            command = commands.get(command_name)
            if command is None:
                _LOGGER.debug("Select debug: source command '%s' absent", command_name)
                continue
            params = getattr(command, "parameters", None)
            if not isinstance(params, dict):
                _LOGGER.debug(
                    "Select debug: source command '%s' without parameters dict: %s",
                    command_name,
                    type(params).__name__,
                )
                continue
            for param_name in PROGRAM_PARAM_NAMES:
                if param_name in params:
                    _LOGGER.debug(
                        "Select debug: found program command '%s' parameter '%s' params=%s",
                        command_name,
                        param_name,
                        sorted(params.keys()),
                    )
                    return command_name, command, param_name
        return None

    @staticmethod
    def _program_values(command, param_name: str) -> dict[str, str]:
        params = getattr(command, "parameters", {})
        prog_param = params.get(param_name) if isinstance(params, dict) else None
        if prog_param is None:
            _LOGGER.debug("Select debug: program parameter '%s' absent", param_name)
            return {}
        for attr in ("values", "value_list", "options"):
            raw = getattr(prog_param, attr, None)
            if isinstance(raw, dict):
                values = {str(code): str(label) for code, label in raw.items()}
                _LOGGER.debug(
                    "Select debug: program values from attr '%s' dict count=%d values=%s",
                    attr,
                    len(values),
                    values,
                )
                return values
            if isinstance(raw, (list, tuple)):
                values = {str(value): str(value) for value in raw}
                _LOGGER.debug(
                    "Select debug: program values from attr '%s' list count=%d values=%s",
                    attr,
                    len(values),
                    values,
                )
                return values
        _LOGGER.debug("Select debug: no values/value_list/options for parameter '%s'", param_name)
        return {}

    @staticmethod
    def _load_programs(appliance) -> dict[str, str]:
        try:
            command_info = HonProgramSelect._find_program_command(appliance)
            if command_info is None:
                _LOGGER.debug("Select debug: _load_programs without command_info")
                return {}
            command_name, command, param_name = command_info
            values = HonProgramSelect._program_values(command, param_name)
            if not values:
                _LOGGER.debug(
                    "Select debug: _load_programs command '%s' parameter '%s' without values",
                    command_name,
                    param_name,
                )
                return {}
            programs = {
                str(code): str(label) if label else str(code)
                for code, label in values.items()
            }
            _LOGGER.debug(
                "Select debug: _load_programs from command '%s' parameter '%s': %s",
                command_name,
                param_name,
                programs,
            )
            return programs
        except Exception as err:
            _LOGGER.debug("Error loading dynamic programs: %s", err)
            return {}

    @property
    def current_option(self) -> str | None:
        # 1) Choice awaiting start ("set only"): we show it immediately,
        #    until the user starts the cycle with the "Avvia programma" button.
        pending = self._coordinator_store(PROGRAM_PENDING_STORE).get(self._appliance_id)
        if pending is not None:
            label = self._program_map.get(str(pending))
            if label is not None:
                _LOGGER.debug(
                    "Select debug: current_option uses pending id=%s code=%s label=%s",
                    redact_id(self._appliance_id),
                    pending,
                    label,
                )
                return label
            _LOGGER.debug(
                "Select debug: current_option pending id=%s code=%s not present in map=%s",
                redact_id(self._appliance_id),
                pending,
                self._program_map,
            )

        # 2) Real state from the device. We try both the program name and the code
        #    (prCode/program) and use the first that matches a known option.
        #    FIX: check is not None instead of 'or', which would discard 0.
        #    Order: first the keys that expose the program NAME (mappable when the
        #    option list is built from a list of names, as on the real models),
        #    then the numeric prCode codes. So, when the device publishes a numeric
        #    prCode not present in the per-name map, resolution happens directly on
        #    the name without generating DEBUG noise "not mapped" for a code that
        #    would not be mappable anyway.
        for key in (
            "programName",
            "settings.program",
            "startProgram.program",
            "program",
            "settings.prCode",
            "startProgram.prCode",
            "prCode",
        ):
            val = self._get_attr(key)
            if val is None:
                _LOGGER.debug("Select debug: current_option key '%s' absent", key)
                continue
            token = str(val)
            # token may be a code (a map key) or already a label (e.g. programName
            # exposes the program name).
            if token in self._program_map:
                _LOGGER.debug(
                    "Select debug: current_option key '%s' token code=%s label=%s",
                    key,
                    token,
                    self._program_map[token],
                )
                return self._program_map[token]
            if token in self._program_reverse:
                _LOGGER.debug(
                    "Select debug: current_option key '%s' token label=%s",
                    key,
                    token,
                )
                return token
            _LOGGER.debug(
                "Select debug: current_option key '%s' token=%s not mapped; map=%s",
                key,
                token,
                self._program_map,
            )
        _LOGGER.debug("Select debug: current_option not available for id=%s", redact_id(self._appliance_id))
        return None

    async def async_select_option(self, option: str) -> None:
        code = self._program_reverse.get(option)
        if code is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="program_not_found",
                translation_placeholders={"program": option},
            )
        # "Set only": we store the choice WITHOUT sending any command.
        # Selecting a program must never start the appliance; the start happens
        # with the "Avvia programma" button, which reads this pending program and
        # applies it to the startProgram command.
        store = self._coordinator_store(PROGRAM_PENDING_STORE)
        _LOGGER.debug(
            "Select debug: before selection option=%s code=%s store=%s",
            option,
            code,
            redact_store(store),
        )
        store[self._appliance_id] = code
        _LOGGER.info(
            "Select: program '%s' (code=%s) set; start it with 'Avvia programma'",
            option, code,
        )
        _LOGGER.debug("Select debug: after selection store=%s", redact_store(store))
        self.async_write_ha_state()
