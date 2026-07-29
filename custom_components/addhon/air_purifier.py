# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Air purifier (AP) mappings, capability discovery, and command intents.

Dependency-light on purpose: this module owns every AP-specific decision so the
Home Assistant platform modules stay thin. It never reads a model name, a
nickname, or a serial. Which entities exist and which values they may write is
derived exclusively from the LIVE command schema and the LIVE state attributes,
so a device declaring a different subset simply loses the affected feature
instead of needing a model exception.

Every write is expressed as a ``CommandPatch`` for the transactional dispatcher:
this module builds intents and never sends anything itself. Mandatory schema
keys (the purifier's ``onOffStatus``) are added by the dispatcher from the
schema, so an intent never duplicates them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .command_dispatch import CommandPatch
from .hon_commands import (
    SETTINGS_COMMANDS,
    get_command,
    param_range,
    param_values,
)

# Raw machMode values that may be WRITTEN. 0 is the off-state sentinel the
# device reports while stopped, and 3 is undeclared by the observed schemas and
# absent from their official UI; neither may ever become a writable preset even
# if a schema starts enumerating it.
AP_WRITABLE_MODES = frozenset({"1", "2", "4"})

AP_MODE_TO_PRESET = {"1": "sleep", "2": "auto", "4": "max"}
AP_PRESET_TO_MODE = {preset: raw for raw, preset in AP_MODE_TO_PRESET.items()}

# The panel light encoding is INVERSE: a higher raw value is a dimmer panel.
AP_LIGHT_TO_BRIGHTNESS = {"2": 0, "1": 128, "0": 255}
AP_BRIGHTNESS_TO_LIGHT = {
    brightness: raw for raw, brightness in AP_LIGHT_TO_BRIGHTNESS.items()
}

AP_AROMA_TO_OPTION = {
    "0": "off",
    "1": "soft",
    "2": "mid",
    "3": "h_biotics",
    "4": "custom",
}
AP_OPTION_TO_AROMA = {option: raw for raw, option in AP_AROMA_TO_OPTION.items()}

AP_CUSTOM_AROMA = "4"

# EXPERIMENTAL interpretations. Both are behind the experimental option because a
# single raw value has been observed together with its meaning; the rest of each
# scale is unknown. They map ONLY what is confirmed and return None for everything
# else, so an unconfirmed reading hides instead of being presented as understood.
#
# Air quality: the device reports a small ordinal and only raw 0 has been seen
# paired with its label in the official application.
AP_AIR_QUALITY_LABELS = {"0": "good"}

# Carbon monoxide: raw 2 is the observed candidate for an alarming device. The
# all-clear value is NOT known, which is why nothing here ever reports "no
# alarm" and why the entity must never carry a safety device class.
AP_CO_ALARM_RAW = "2"

# Raw error payloads that all mean "no error". The device uses several spellings
# interchangeably, including 100.
_NORMAL_ERROR_CODES = frozenset({0, 100})

_START_COMMAND = "startProgram"
_STOP_COMMAND = "stopProgram"

_LIGHT_PARAM = "lightStatus"
_LOCK_PARAM = "lockStatus"
_TONE_PARAM = "touchToneStatus"
_AROMA_PARAM = "aromaStatus"
_AROMA_TIME_ON_PARAM = "aromaTimeOn"
_AROMA_TIME_OFF_PARAM = "aromaTimeOff"
_MODE_PARAM = "machMode"
_POWER_ATTR = "onOffStatus"

# Required raw value sets. The light must declare exactly its three observed
# levels: a two-value or four-value schema is a different device behavior, and
# guessing at it would send an undeclared value.
_LIGHT_VALUES = frozenset({"0", "1", "2"})
_TOGGLE_VALUES = frozenset({"0", "1"})

_Range = tuple[float, float, float]

# Every parameter an AP entity reads as state and/or writes as a command field.
# Declared here, next to the constants the platforms actually use, so the
# diagnostics coverage calculation cannot drift from the entities: a name missing
# from this set would be reported to the user as an unmapped control that in fact
# already exists. A test derives the switch and number tables and asserts they are
# covered.
AP_ENTITY_PARAMS = frozenset(
    {
        _POWER_ATTR,
        _MODE_PARAM,
        _LIGHT_PARAM,
        _LOCK_PARAM,
        _TONE_PARAM,
        _AROMA_PARAM,
        _AROMA_TIME_ON_PARAM,
        _AROMA_TIME_OFF_PARAM,
    }
)

# Raw values the integration actually HANDLES per parameter. A device declaring or
# reporting anything outside these is running ahead of this code; diagnostics
# reports the delta so a new firmware capability becomes visible without any entity
# being guessed into existence. Range parameters are absent on purpose: a numeric
# range has no enumerable "unhandled value".
AP_HANDLED_VALUES: dict[str, frozenset[str]] = {
    _POWER_ATTR: _TOGGLE_VALUES,
    _MODE_PARAM: AP_WRITABLE_MODES,
    _LIGHT_PARAM: frozenset(AP_LIGHT_TO_BRIGHTNESS),
    _LOCK_PARAM: _TOGGLE_VALUES,
    _TONE_PARAM: _TOGGLE_VALUES,
    _AROMA_PARAM: frozenset(AP_AROMA_TO_OPTION),
}


def _attr_value(attributes: Any, key: str) -> Any:
    """Read one live attribute, tolerating both shapes the client returns.

    Depending on the client version an attribute is a ``HonAttribute`` (whose
    ``.value`` may legitimately be 0 or False) or an already-unwrapped raw
    value. An empty string means "not reported" and reads as None.
    """
    if not isinstance(attributes, dict):
        return None
    value = attributes.get(key)
    if value is None:
        return None
    if hasattr(value, "value"):
        value = value.value
    if value == "":
        return None
    return value


def reports_attribute(attributes: Any, key: str) -> bool:
    """True when the appliance actually REPORTS `key`.

    A present-but-empty attribute means "not available" (see `_attr_value`), so a
    plain `key in attributes` check would create an entity that can only ever read
    unknown. Used by the AP platforms to gate the read half of a control, next to
    the capability check that gates the write half.
    """
    return _attr_value(attributes, key) is not None


def raw_text(value: Any) -> str:
    """Canonical string form of a device value, for comparing against the schema.

    The schema spells every value as a string ("1", "2", "60"), so anything
    compared against it has to arrive in that spelling. Three sources feed this:

    - Home Assistant, on the WRITE path, hands real Python types: a switch passes
      a bool and a number passes a float, and str(True) / str(60.0) match no
      schema value. This is the case the helper exists for.
    - `HonAttribute.value`, on the READ path, already routes through
      `str_to_float`, which folds bool, int and integral float onto an int
      (True -> 1, 1.0 -> 1, "01" -> 1). Those need no help.
    - a decimal-spelled numeric STRING from the cloud ("1.0") is the one read-path
      spelling `str_to_float` keeps as a float, so it is also the one a bare
      str() gets wrong.

    Non-integral floats and non-numeric text pass through untouched: 60.5 stays
    "60.5" and "E12" stays "E12".
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def filter_remaining(raw: Any) -> int | None:
    """Remaining filter life percent from the device's CONSUMED percentage.

    Returns None for an unreported or unparseable value so the sensor reads
    unknown rather than claiming a full or an empty filter.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        consumed = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, 100 - int(consumed)))


def normalize_error(raw: Any) -> str:
    """Fold every "no error" spelling onto "0" and pass anything else through.

    The device reports a healthy state as numeric or string zero, as a
    zero-padded "00", and as "100". Non-numeric codes such as "E12" are real
    errors and are returned stripped but otherwise untouched.

    Parsed as a float rather than an int on purpose: the client may hand back an
    already-numeric 0.0, and int("0.0") raises, which would have promoted a
    healthy device to a permanent problem state.
    """
    if raw is None:
        return "0"
    text = str(raw).strip()
    if not text:
        return "0"
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer() and int(number) in _NORMAL_ERROR_CODES:
        return "0"
    return text


def has_problem(raw: Any) -> bool:
    """True when the normalized error code is anything but "no error"."""
    return normalize_error(raw) != "0"


def is_engaged(raw: Any) -> bool:
    """True when a 0/1 purifier status reads as engaged.

    Exists so an AP binary that has no derived meaning still resolves its raw value
    through `raw_text` like every other AP read, instead of through the generic
    `on_value` comparison in the binary_sensor platform. That comparison is shared
    with every other appliance family and follows the older platform convention;
    routing the purifier's own signals through this module keeps one rule for the
    whole feature.
    """
    return raw_text(raw) == "1"


def air_quality_label(raw: Any) -> str | None:
    """The confirmed label for an air-quality ordinal, else None.

    EXPERIMENTAL. Only one ordinal has been observed together with its label, so
    every other value returns None and the entity hides rather than inventing a
    scale the device may not use.
    """
    if raw is None:
        return None
    return AP_AIR_QUALITY_LABELS.get(raw_text(raw))


def co_alarm(raw: Any) -> bool | None:
    """True for the observed alarming CO value, None for anything unconfirmed.

    EXPERIMENTAL. Deliberately never returns False: the values that mean "no
    carbon monoxide" have not been observed, so reporting an all-clear would
    assert safety on no evidence. None keeps the entity unavailable instead.
    """
    if raw is None:
        return None
    return True if raw_text(raw) == AP_CO_ALARM_RAW else None


def environment_available(attributes: Any) -> bool:
    """True only while the purifier is CONFIRMED powered on.

    While off, the device publishes zero sentinels for temperature, humidity and
    the particulate values and retains stale VOC/air-quality/wind readings, so
    every environmental entity has to hide instead of presenting a sentinel as a
    measurement. An unreported power state is not a confirmation, so it hides
    too.
    """
    return raw_text(_attr_value(attributes, _POWER_ATTR)) == "1"


@dataclass(frozen=True, slots=True)
class AirPurifierCapabilities:
    """What THIS purifier can actually read and write, from its live schema.

    Every field is derived, never assumed. A device missing one parameter loses
    exactly the feature that parameter backs and keeps the rest.
    """

    settings_command: str | None
    can_start: bool
    can_stop: bool
    start_modes: frozenset[str]
    preset_modes: frozenset[str]
    light_values: frozenset[str]
    lock_values: frozenset[str]
    tone_values: frozenset[str]
    aroma_values: frozenset[str]
    aroma_time_on: _Range | None
    aroma_time_off: _Range | None
    has_power_state: bool
    has_mode_state: bool

    @property
    def writable_modes(self) -> frozenset[str]:
        """Modes selectable from EITHER purifier state.

        A preset is set through `startProgram` while the purifier is off and
        through the settings command while it is running, so only a mode both
        commands declare can be honoured whenever the user picks it. Offering a
        mode present in just one of them would surface a preset that is rejected
        depending on when it is selected.
        """
        return self.start_modes & self.preset_modes

    @property
    def supports_fan(self) -> bool:
        """Power and preset control needs both transitions, at least one mode
        writable from either state, and the state to render them from."""
        return bool(
            self.can_start
            and self.can_stop
            and self.writable_modes
            and self.has_power_state
            and self.has_mode_state
        )

    @property
    def supports_light(self) -> bool:
        return self.light_values == _LIGHT_VALUES

    @property
    def supports_lock(self) -> bool:
        return self.lock_values == _TOGGLE_VALUES

    @property
    def supports_tone(self) -> bool:
        return self.tone_values == _TOGGLE_VALUES

    @property
    def supports_aroma(self) -> bool:
        return bool(self.aroma_values)

    @property
    def supports_custom_aroma(self) -> bool:
        """Custom needs its status value AND both timings: without them the
        Custom contract cannot be completed, so the mode is not offered."""
        return (
            AP_CUSTOM_AROMA in self.aroma_values
            and self.aroma_time_on is not None
            and self.aroma_time_off is not None
        )

    @property
    def preset_options(self) -> tuple[str, ...]:
        """Preset names in canonical order, limited to the live writable set."""
        return tuple(
            preset
            for raw, preset in AP_MODE_TO_PRESET.items()
            if raw in self.writable_modes
        )

    @property
    def aroma_options(self) -> tuple[str, ...]:
        """Aroma option names in canonical order, limited to the live values.

        Custom is withheld unless its timing parameters are writable too.
        """
        return tuple(
            option
            for raw, option in AP_AROMA_TO_OPTION.items()
            if raw in self.aroma_values
            and (raw != AP_CUSTOM_AROMA or self.supports_custom_aroma)
        )


def _writable_values(command: Any, param_name: str) -> frozenset[str]:
    """Declared enum values of a writable parameter, or an empty set."""
    params = getattr(command, "parameters", None) if command is not None else None
    if not isinstance(params, dict):
        return frozenset()
    param = params.get(param_name)
    if param is None:
        return frozenset()
    return frozenset(param_values(param))


def _writable_range(command: Any, param_name: str) -> _Range | None:
    params = getattr(command, "parameters", None) if command is not None else None
    if not isinstance(params, dict):
        return None
    param = params.get(param_name)
    if param is None:
        return None
    return param_range(param)


def _settings_command_name(appliance: Any) -> str | None:
    """The one settings-style command this device exposes.

    Resolved once for the whole capability set rather than per parameter, so
    every settings patch targets the same command and cannot be split across two
    of them.
    """
    for name in SETTINGS_COMMANDS:
        if get_command(appliance, name) is not None:
            return name
    return None


def discover_capabilities(appliance: Any, attributes: Any) -> AirPurifierCapabilities:
    """Derive the AP feature set from the live command schema and state."""
    settings_name = _settings_command_name(appliance)
    settings = get_command(appliance, settings_name) if settings_name else None
    start = get_command(appliance, _START_COMMAND)
    stop = get_command(appliance, _STOP_COMMAND)

    return AirPurifierCapabilities(
        settings_command=settings_name,
        can_start=start is not None,
        can_stop=stop is not None,
        # Intersecting with AP_WRITABLE_MODES is what keeps the off sentinel and
        # the allergen mode out, no matter what a schema enumerates.
        start_modes=_writable_values(start, _MODE_PARAM) & AP_WRITABLE_MODES,
        preset_modes=_writable_values(settings, _MODE_PARAM) & AP_WRITABLE_MODES,
        light_values=_writable_values(settings, _LIGHT_PARAM),
        lock_values=_writable_values(settings, _LOCK_PARAM),
        tone_values=_writable_values(settings, _TONE_PARAM),
        aroma_values=_writable_values(settings, _AROMA_PARAM),
        aroma_time_on=_writable_range(settings, _AROMA_TIME_ON_PARAM),
        aroma_time_off=_writable_range(settings, _AROMA_TIME_OFF_PARAM),
        has_power_state=_attr_value(attributes, _POWER_ATTR) is not None,
        has_mode_state=_attr_value(attributes, _MODE_PARAM) is not None,
    )


def _require(supported: bool, action: str) -> None:
    if not supported:
        raise ValueError(f"air purifier does not support {action}")


def _checked_value(value: Any, allowed: frozenset[str], action: str) -> str:
    raw = raw_text(value)
    if raw not in allowed:
        raise ValueError(
            f"{action}: {raw!r} is not one of {sorted(allowed)}"
        )
    return raw


def _checked_time(value: Any, bounds: _Range | None, name: str) -> str:
    if bounds is None:
        raise ValueError(f"{name} is not writable on this appliance")
    if value is None:
        raise ValueError(f"{name} is required for the custom aroma mode")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name}: {value!r} is not numeric") from error
    low, high, _step = bounds
    if not low <= number <= high:
        raise ValueError(f"{name}: {number} outside [{low}, {high}]")
    return raw_text(number)


def _settings_patch(
    capabilities: AirPurifierCapabilities,
    values: dict[str, str],
    action: str,
) -> CommandPatch:
    if capabilities.settings_command is None:
        raise ValueError("appliance exposes no settings command")
    return CommandPatch(capabilities.settings_command, values, action=f"ap_{action}")


def ap_patch(
    action: str,
    capabilities: AirPurifierCapabilities,
    *,
    value: Any = None,
    time_on: Any = None,
    time_off: Any = None,
) -> CommandPatch:
    """Build the exact intent for one AP action.

    Carries only the fields the user is changing; the dispatcher adds the
    schema's mandatory keys. Raises ValueError for an unknown action, an
    unsupported feature, or a value the live schema does not declare, so a
    rejected intent never reaches the device.
    """
    match action:
        case "turn_on":
            _require(capabilities.supports_fan, action)
            mode = _checked_value(value, capabilities.start_modes, action)
            return CommandPatch(
                _START_COMMAND, {_MODE_PARAM: mode}, action="ap_turn_on"
            )
        case "turn_off":
            _require(capabilities.supports_fan, action)
            return CommandPatch(_STOP_COMMAND, {}, action="ap_turn_off")
        case "set_preset":
            _require(capabilities.supports_fan, action)
            mode = _checked_value(value, capabilities.preset_modes, action)
            return _settings_patch(capabilities, {_MODE_PARAM: mode}, action)
        case "set_light":
            _require(capabilities.supports_light, action)
            raw = _checked_value(value, capabilities.light_values, action)
            return _settings_patch(capabilities, {_LIGHT_PARAM: raw}, action)
        case "set_lock":
            _require(capabilities.supports_lock, action)
            raw = _checked_value(value, capabilities.lock_values, action)
            return _settings_patch(capabilities, {_LOCK_PARAM: raw}, action)
        case "set_tone":
            _require(capabilities.supports_tone, action)
            raw = _checked_value(value, capabilities.tone_values, action)
            return _settings_patch(capabilities, {_TONE_PARAM: raw}, action)
        case "set_aroma":
            _require(capabilities.supports_aroma, action)
            raw = _checked_value(value, capabilities.aroma_values, action)
            if raw != AP_CUSTOM_AROMA:
                return _settings_patch(capabilities, {_AROMA_PARAM: raw}, action)
            _require(capabilities.supports_custom_aroma, "the custom aroma mode")
            return _settings_patch(
                capabilities,
                {
                    _AROMA_PARAM: raw,
                    _AROMA_TIME_ON_PARAM: _checked_time(
                        time_on, capabilities.aroma_time_on, _AROMA_TIME_ON_PARAM
                    ),
                    _AROMA_TIME_OFF_PARAM: _checked_time(
                        time_off, capabilities.aroma_time_off, _AROMA_TIME_OFF_PARAM
                    ),
                },
                action,
            )
        case "set_aroma_time":
            # Adjusting one custom timing while Custom is already the active
            # mode. The status is repeated because the device requires it to
            # accept a timing write, but the SIBLING timing is deliberately
            # omitted: including it would push whatever value the caller
            # happened to hold over a concurrent change to the other field.
            #
            # Whether Custom is actually the current mode is state, which this
            # module does not read: the caller must gate on it and refuse the
            # action rather than have this patch switch the mode as a side
            # effect.
            _require(capabilities.supports_custom_aroma, "the custom aroma mode")
            if time_on is None and time_off is None:
                raise ValueError("set_aroma_time: no timing value to change")
            values = {_AROMA_PARAM: AP_CUSTOM_AROMA}
            if time_on is not None:
                values[_AROMA_TIME_ON_PARAM] = _checked_time(
                    time_on, capabilities.aroma_time_on, _AROMA_TIME_ON_PARAM
                )
            if time_off is not None:
                values[_AROMA_TIME_OFF_PARAM] = _checked_time(
                    time_off, capabilities.aroma_time_off, _AROMA_TIME_OFF_PARAM
                )
            return _settings_patch(capabilities, values, action)
        case unknown:
            raise ValueError(f"unknown air purifier action {unknown!r}")


__all__ = [
    "AP_AIR_QUALITY_LABELS",
    "AP_AROMA_TO_OPTION",
    "AP_BRIGHTNESS_TO_LIGHT",
    "AP_CO_ALARM_RAW",
    "AP_CUSTOM_AROMA",
    "AP_ENTITY_PARAMS",
    "AP_HANDLED_VALUES",
    "AP_LIGHT_TO_BRIGHTNESS",
    "AP_MODE_TO_PRESET",
    "AP_OPTION_TO_AROMA",
    "AP_PRESET_TO_MODE",
    "AP_WRITABLE_MODES",
    "AirPurifierCapabilities",
    "air_quality_label",
    "ap_patch",
    "co_alarm",
    "discover_capabilities",
    "environment_available",
    "filter_remaining",
    "has_problem",
    "is_engaged",
    "normalize_error",
    "raw_text",
    "reports_attribute",
]
