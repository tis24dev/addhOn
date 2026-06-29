"""Haier hOn select - washer program selection + writable program options."""
from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity
from .const import (
    APPLIANCE_FR,
    APPLIANCE_FRE,
    APPLIANCE_REF,
    APPLIANCE_TD,
    APPLIANCE_WASH_GROUP,
    APPLIANCE_WD,
    APPLIANCE_WM,
    DIRTY_LEVEL_LABELS,
    DOMAIN,
    DRY_LEVEL_LABELS_TD,
    DRY_LEVEL_LABELS_WM,
    DRY_LEVEL_SENTINELS,
    PROGRAM_PARAM_NAMES,
    PROGRAM_PENDING_OPTIONS,
    PROGRAM_PENDING_STORE,
    STEAM_LEVEL_LABELS,
    TEMP_LEVEL_LABELS,
)
from .debug_utils import redact_id, redact_store
from .hon_commands import async_send_command
from .program_options import (
    HonProgramOptionEntity,
    async_send_program,
    normalize_code,
    option_choices,
)

# Fridge family (REF/FR/FRE): the writable program/mode select (discussion #40).
_COOLING_TYPES = (APPLIANCE_REF, APPLIANCE_FR, APPLIANCE_FRE)
# Synthetic "no program" option -> stopProgram (the global mode reset).
REF_PROGRAM_OFF = "off"
# Read-back ONLY: a live device mode flag (0/1) -> the startProgram program code it
# corresponds to. Used to derive current_option from the device truth (never from
# startProgram.program, which only carries the recovered default category). The app's own
# identity map (refrigeration.md section 1): superCool=quickModeZ1, superFreeze=quickModeZ2,
# holiday=holidayMode, autoSet=intelligenceMode. Double-gated at read time: the code must
# also be in the device's live program enum.
_REF_MODE_FLAG_TO_PROGRAM: dict[str, str] = {
    "quickModeZ1": "super_cool",
    "quickModeZ2": "super_freeze",
    "holidayMode": "holiday",
    "intelligenceMode": "auto_set",
}

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HonProgramOptionSelectDescription:
    """A categorical program option rendered as a select (#35).

    `label_map` maps raw schema values -> machine keys for the select state translations
    (None = render the raw values, used for the numeric spin/temp enums). `drop` removes
    unselectable sentinels. `types` gates the appliance families; dryLevel is TYPE-GATED
    (WM/WD vs TD) because value 1 means EXTRA_DRY on WM/WD but IRON_DRY on TD, so the two
    descriptions share the `dry_level` translation_key but carry disjoint label maps.
    """

    key: str                 # base of the unique_id suffix (opt_<key>)
    param: str
    translation_key: str
    types: tuple[str, ...]
    label_map: dict[str, str] | None = None
    drop: tuple[str, ...] = ()
    icon: str | None = None


_WASH_TYPES = (APPLIANCE_WM, APPLIANCE_WD)
_DRY_TYPES = (APPLIANCE_TD,)

# Candidate program-option selects, capability-gated by the device schema. spin/temp are
# numeric enums on the real washers -> selects with raw numeric labels (no state block).
# dryLevel/tempLevel/dirtyLevel/steamLevel are label-mapped (state translations).
_PROGRAM_OPTION_SELECTS: tuple[HonProgramOptionSelectDescription, ...] = (
    HonProgramOptionSelectDescription(
        key="spin_speed", param="spinSpeed", translation_key="spin_speed",
        types=_WASH_TYPES, icon="mdi:speedometer",
    ),
    HonProgramOptionSelectDescription(
        key="wash_temp", param="temp", translation_key="wash_temp",
        types=_WASH_TYPES, icon="mdi:thermometer",
    ),
    HonProgramOptionSelectDescription(
        key="dry_level", param="dryLevel", translation_key="dry_level",
        types=_WASH_TYPES, label_map=DRY_LEVEL_LABELS_WM, drop=DRY_LEVEL_SENTINELS,
        icon="mdi:tumble-dryer",
    ),
    HonProgramOptionSelectDescription(
        key="dry_level", param="dryLevel", translation_key="dry_level",
        types=_DRY_TYPES, label_map=DRY_LEVEL_LABELS_TD, drop=DRY_LEVEL_SENTINELS,
        icon="mdi:tumble-dryer",
    ),
    HonProgramOptionSelectDescription(
        key="temp_level", param="tempLevel", translation_key="temp_level",
        types=_DRY_TYPES, label_map=TEMP_LEVEL_LABELS, icon="mdi:thermometer",
    ),
    HonProgramOptionSelectDescription(
        key="dirty_level", param="dirtyLevel", translation_key="dirty_level",
        types=_WASH_TYPES, label_map=DIRTY_LEVEL_LABELS, icon="mdi:liquid-spot",
    ),
    HonProgramOptionSelectDescription(
        key="steam_level", param="steamLevel", translation_key="steam_level",
        types=_WASH_TYPES, label_map=STEAM_LEVEL_LABELS, icon="mdi:weather-fog",
    ),
)

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
        if app_type in _COOLING_TYPES:
            # Fridge family (#40): one writable program/mode select, sends immediately.
            if HonRefProgramSelect.supports_appliance(appliance):
                entities.append(HonRefProgramSelect(coordinator, appliance_id, client))
                _LOGGER.info("Added REF program select: id=%s", redact_id(appliance_id))
            else:
                _LOGGER.debug(
                    "Select debug: no REF program select for '%s' id=%s; "
                    "needs startProgram(program enum) + stopProgram",
                    data.get("name"),
                    redact_id(appliance_id),
                )
            continue
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
        # Writable program-option selects (#35): capability-gated on the live schema.
        created_opts: list[str] = []
        for desc in _PROGRAM_OPTION_SELECTS:
            if app_type not in desc.types:
                continue
            if not HonProgramOptionSelect.supports(appliance, desc.param, desc.drop):
                continue
            entities.append(HonProgramOptionSelect(coordinator, appliance_id, desc, client))
            created_opts.append(desc.key)
        if created_opts:
            _LOGGER.info(
                "Added %d program-option selects: id=%s",
                len(created_opts),
                redact_id(appliance_id),
            )
        _LOGGER.debug(
            "Select debug: option selects for id=%s type=%s -> %s",
            redact_id(appliance_id),
            app_type,
            created_opts,
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
        previous_code = store.get(self._appliance_id)
        _LOGGER.debug(
            "Select debug: before selection option=%s code=%s store=%s",
            option,
            code,
            redact_store(store),
        )
        store[self._appliance_id] = code
        # Changing the program invalidates any buffered program OPTIONS: they were chosen
        # for the previous program and must not silently carry into the new one at Start
        # (PR #38 / Greptile P1). Clear them ONLY on an actual program change, so
        # re-selecting the SAME program keeps the user's buffered options.
        cleared = (
            self._coordinator_store(PROGRAM_PENDING_OPTIONS).pop(self._appliance_id, None)
            if previous_code != code
            else None
        )
        _LOGGER.info(
            "Select: program '%s' (code=%s) set; start it with 'Avvia programma'",
            option, code,
        )
        if cleared:
            _LOGGER.debug(
                "Select debug: program change id=%s cleared pending options=%s",
                redact_id(self._appliance_id),
                sorted(cleared) if isinstance(cleared, dict) else None,
            )
        _LOGGER.debug("Select debug: after selection store=%s", redact_store(store))
        self.async_write_ha_state()


class HonProgramOptionSelect(HonProgramOptionEntity, SelectEntity):
    """Categorical program option (dry level, spin speed, soil level, ...) buffered onto
    the startProgram command; applied + sent on the "Start program" button."""

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        description: HonProgramOptionSelectDescription,
        client=None,
    ) -> None:
        super().__init__(coordinator, appliance_id, description.param, client)
        self._desc = description
        self._attr_translation_key = description.translation_key
        self._attr_unique_id = f"{appliance_id}_opt_{description.key}"
        if description.icon:
            self._attr_icon = description.icon
        label_map = description.label_map or {}
        # The param is resolved + cached once by the mixin; materialize its codes here.
        choices = (
            option_choices(self._option_param, description.drop)
            if self._option_param is not None
            else []
        )
        # raw schema value -> base label (label map, raw value as fallback).
        base_keys = {raw: label_map.get(raw, raw) for raw in choices}
        # Collision-aware disambiguation (PR #38 / Greptile P2): when two EXPOSED raw codes
        # share a label (DRY_LEVEL_LABELS_TD maps e.g. 1 & 12 both to "iron_dry"), suffixing
        # ONLY the colliding ones with their raw code keeps every code selectable and keeps
        # the reverse map injective (otherwise one raw would be unreachable). Non-colliding
        # labels are untouched, so the common case keeps its translatable `state.<key>`; a
        # suffixed colliding key has no translation and renders literally (rare-model-only).
        label_counts: dict[str, int] = {}
        for label in base_keys.values():
            label_counts[label] = label_counts.get(label, 0) + 1
        self._raw_to_key: dict[str, str] = {
            raw: (f"{label} ({raw})" if label_counts[label] > 1 else label)
            for raw, label in base_keys.items()
        }
        self._key_to_raw: dict[str, str] = {key: raw for raw, key in self._raw_to_key.items()}
        # One distinct option per exposed raw code (keys are now unique; order preserved).
        self._attr_options = list(self._raw_to_key.values())
        _LOGGER.debug(
            "Select debug: init option select '%s' id=%s param=%s options=%s",
            redact_id(self._attr_unique_id, appliance_id),
            redact_id(appliance_id),
            description.param,
            self._attr_options,
        )

    @property
    def current_option(self) -> str | None:
        raw = self._current_raw()
        if raw is None:
            return None
        return self._raw_to_key.get(normalize_code(raw))

    async def async_select_option(self, option: str) -> None:
        raw = self._key_to_raw.get(option)
        if raw is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_setpoint",
                translation_placeholders={
                    "value": option,
                    "allowed": ", ".join(self._attr_options),
                },
            )
        self._buffer(raw)


class HonRefProgramSelect(HonBaseEntity, SelectEntity):
    """Writable program/mode select for fridges (REF/FR/FRE), discussion #40.

    Fridge "modes" (super cool, super freeze, holiday, plus the iot_* presets) are NOT
    writable settings booleans: they are ``startProgram`` PROGRAMS, cleared by
    ``stopProgram`` (a GLOBAL reset that zeroes every mode flag). The app/cloud model is
    mutually exclusive (one program at a time), so the faithful HA shape is a single
    select. Options are ``off`` plus the device's LIVE ``startProgram.program`` enum
    (capability-gated, never hard-coded). Unlike the washer select, selecting here sends
    IMMEDIATELY (no buffer/Start-button cycle): a program -> ``startProgram(program=X)``,
    ``off`` -> ``stopProgram``. ``current_option`` is derived from the live device mode
    FLAGS, NOT from ``startProgram.program`` (which only carries the recovered default
    category and would otherwise pin a phantom mode forever)."""

    _attr_icon = "mdi:snowflake"

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._attr_unique_id = f"{appliance_id}_ref_program"
        self._attr_translation_key = "ref_program"
        # Build the option list from the live program enum read SPECIFICALLY off the
        # startProgram command -- the same command async_send_program targets -- so the
        # option SOURCE can never diverge from the SEND target (a fridge could in theory
        # also expose another program-bearing command; we deliberately ignore it here).
        self._program_codes: list[str] = []
        resolved = self._start_program_param(self._appliance)
        if resolved is not None:
            command, param_name = resolved
            self._program_codes = list(
                HonProgramSelect._program_values(command, param_name).keys()
            )
        self._attr_options = [REF_PROGRAM_OFF, *self._program_codes]
        _LOGGER.debug(
            "Select debug: initialized REF program '%s' id=%s options=%s",
            redact_id(self._attr_unique_id, appliance_id),
            redact_id(appliance_id),
            self._attr_options,
        )

    @staticmethod
    def _start_program_param(appliance):
        """(startProgram command, program-param name), or None.

        Resolved ONLY on ``startProgram`` (not the broader PROGRAM_SOURCE_COMMANDS walk the
        washer uses) so the fridge select's option source is always the very command it
        sends to."""
        commands = getattr(appliance, "commands", None)
        commands = commands if isinstance(commands, dict) else {}
        command = commands.get("startProgram")
        params = getattr(command, "parameters", None) if command is not None else None
        if not isinstance(params, dict):
            return None
        for param_name in PROGRAM_PARAM_NAMES:
            if param_name in params:
                return command, param_name
        return None

    @classmethod
    def supports_appliance(cls, appliance) -> bool:
        """True if the device exposes startProgram with a populated program enum AND a
        stopProgram command (so the ``off`` reset is real)."""
        commands = getattr(appliance, "commands", None)
        commands = commands if isinstance(commands, dict) else {}
        if "stopProgram" not in commands:
            _LOGGER.debug(
                "Select debug: REF select skipped, no stopProgram. commands=%s",
                _command_names(appliance),
            )
            return False
        resolved = cls._start_program_param(appliance)
        if resolved is None:
            _LOGGER.debug(
                "Select debug: REF select skipped, startProgram has no program param. "
                "commands=%s",
                _command_names(appliance),
            )
            return False
        command, param_name = resolved
        return bool(HonProgramSelect._program_values(command, param_name))

    @property
    def current_option(self) -> str | None:
        # Device truth from the mode FLAGS (double-gated: the code must also be a live
        # program option). No mode set -> off. Deliberately ignores startProgram.program.
        for flag, code in _REF_MODE_FLAG_TO_PROGRAM.items():
            if code in self._program_codes and str(self._get_attr(flag)) == "1":
                _LOGGER.debug(
                    "Select debug: REF current_option id=%s flag=%s -> %s",
                    redact_id(self._appliance_id), flag, code,
                )
                return code
        return REF_PROGRAM_OFF

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="program_not_found",
                translation_placeholders={"program": option},
            )
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        try:
            if option == REF_PROGRAM_OFF:
                _LOGGER.info(
                    "Select: REF program off -> stopProgram id=%s",
                    redact_id(self._appliance_id),
                )
                await async_send_command(self.hass, client, appliance, "stopProgram", {})
            else:
                _LOGGER.info(
                    "Select: REF program '%s' -> startProgram id=%s",
                    option, redact_id(self._appliance_id),
                )
                await async_send_program(self.hass, client, appliance, option)
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Select: REF program '%s' error: %s", option, err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err
        await self._async_request_command_refresh()
