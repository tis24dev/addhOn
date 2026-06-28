"""Writable program-option controls for the washing group (WM/WD/TD), discussion #35.

The washer/dryer expose start/stop/pause + a program select, but no way to tune the
program (spin speed, temperature, dry level, extra rinses, delayed start, ...). Those
options are PARAMETERS of the ``startProgram`` command, not a separate service: the app
picks a program, overlays the chosen option values and sends ONE ``startProgram`` bundle.

This module is the shared core for that feature:

- the GATE: a curated candidate catalog (the decompiled-app / andre0512 superset) is
  filtered by the device's runtime ``startProgram`` schema with a ">= 2 reachable values /
  not fixed" rule. So an entity is created only when the param is genuinely settable on
  THIS model -- improving on andre0512's static per-type list, which renders a wall of
  "No disponible" controls for the params that are fixed on the user's unit. Schema =
  values; the const label maps supply labels only.
- the BUFFER + apply-on-start: option entities write to a pending-options store on the
  coordinator (parallel to PROGRAM_PENDING_STORE) and DO NOT send. The "Start program"
  button applies the buffered options to the post-swap ``startProgram`` command and sends
  once (see button.py). This mirrors the existing program buffer/apply-on-start pattern.
- the shared mixin ``HonProgramOptionEntity`` (pending read/write, live-or-pending read,
  capability gate) reused by the switch/select/number option entities.

CRITICAL gate detail: check ``option_range`` FIRST and never call ``.values`` on a
``HonParameterRange`` to count it -- the range ``.values`` enumerates min..max and a
malformed range can loop. A ``HonParameterFixed`` (or a single-value enum) fails the gate,
so the entity is never created (auto-removes the fixed toggles on the user's models).
"""
from __future__ import annotations

import logging

from .base_entity import HonBaseEntity
from .const import PROGRAM_PENDING_OPTIONS
from .debug_utils import redact_id
from .hon_commands import get_command, param_range, param_values

_LOGGER = logging.getLogger(__name__)

# The single runtime command that carries the program + its options. Selecting a program
# swaps the active category (program.py / commands.py), but the command name is stable.
STARTPROGRAM_COMMAND = "startProgram"

# Safety cap when materializing the reachable values of a range parameter: a malformed
# range (huge max / tiny step) must never loop unbounded while building a select's options.
_MAX_RANGE_CHOICES = 1000


def startprogram_command(appliance):
    """The device's ``startProgram`` command, or None if absent."""
    return get_command(appliance, STARTPROGRAM_COMMAND)


def startprogram_option_param(appliance, name: str):
    """Resolve option parameter ``name`` across the ``startProgram`` program categories.

    Prefers ``HonCommand.available_settings`` (the richest, non-fixed variant of the
    param across program categories -- stable across program swaps), falling back to the
    active command's ``parameters`` (and to None) so it also works with the lightweight
    fake commands used by the tests."""
    command = startprogram_command(appliance)
    if command is None:
        return None
    settings = getattr(command, "available_settings", None)
    if isinstance(settings, dict) and name in settings:
        return settings[name]
    params = getattr(command, "parameters", None)
    if isinstance(params, dict):
        return params.get(name)
    return None


def option_range(param) -> tuple[float, float, float] | None:
    """(min, max, step) of a range parameter, or None if it is not a range.

    Thin wrapper over ``hon_commands.param_range`` (duck-types on min/max/step); kept as a
    named helper so the gate reads as the design-of-record."""
    return param_range(param)


def _num_str(value) -> str:
    """Format a numeric value as a clean string: '12' not '12.0', '22.5' kept."""
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def normalize_code(value) -> str | None:
    """Normalize a raw device/schema value to its canonical option code.

    Collapses a numeric value to a clean integer/float string ("13.0"/"13"/13 -> "13",
    "360.0" -> "360"), accepting a decimal comma ("4,5" -> "4.5"); a non-numeric value
    (an enum key like "iot_smart") passes through as ``str(value)``; None stays None.
    Used so a device reading and a schema-derived code compare/serialize consistently
    (the range/enum setters reject "13.0" against an enum/range that expects "13")."""
    if value is None:
        return None
    try:
        return _num_str(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return str(value)


def option_value_set(param, drop: tuple[str, ...] = ()) -> list[str]:
    """Distinct ENUM values (clean string codes) of ``param`` minus ``drop``; [] for a range.

    Range is detected FIRST (via ``option_range``) and returns [] -- this never touches
    the range ``.values`` (which would enumerate min..max). For an enum (or a fixed param,
    whose base ``.values`` is the single fixed value) it returns the distinct, normalized
    values (so "12.0"/"12" collapse and a sentinel in ``drop`` matches the clean form)."""
    if param is None:
        return []
    if option_range(param) is not None:
        return []
    out: list[str] = []
    for value in param_values(param):
        code = normalize_code(value)
        if code is None or code in drop or code in out:
            continue
        out.append(code)
    return out


def option_choices(param, drop: tuple[str, ...] = ()) -> list[str]:
    """Selectable values (clean strings) of an option param, enum OR range, minus ``drop``.

    For an enum: its distinct values. For a range: the reachable values min..max step
    (so a range-typed categorical like the dryer's dryLevel[12,13,14] still yields a
    select's options). Range is checked FIRST and bounded by ``_MAX_RANGE_CHOICES`` so a
    malformed range can never loop unbounded."""
    if param is None:
        return []
    rng = option_range(param)
    if rng is not None:
        lo, hi, step = rng
        out: list[str] = []
        current = lo
        count = 0
        while current <= hi + step / 2 and count < _MAX_RANGE_CHOICES:
            token = _num_str(current)
            if token not in drop and token not in out:
                out.append(token)
            current += step
            count += 1
        return out
    return option_value_set(param, drop)


def is_settable_option(param, drop: tuple[str, ...] = ()) -> bool:
    """True if ``param`` is genuinely settable on this model (>= 2 reachable / not fixed).

    ``option_range`` is checked FIRST (never ``.values`` on a range). For a range with no
    sentinels to drop this is the cheap ``max > min``; with sentinels it counts the
    reachable NON-sentinel members (so a sentinel-dominated dryLevel range gates OFF).
    An enum needs >= 2 distinct non-sentinel values. A fixed param (or a 1-value enum, or
    a sentinel-only range/enum, or None) -> False -> the entity is never created."""
    if param is None:
        return False
    rng = option_range(param)
    if rng is not None:
        if not drop:
            return rng[1] > rng[0]
        return len(option_choices(param, drop)) >= 2
    return len(option_value_set(param, drop)) >= 2


def apply_pending_options(params: dict, options: dict) -> list[str]:
    """Apply buffered option values to a command's parameters (the apply-on-start step).

    For each ``(name, value)`` in ``options``: if ``name`` is a parameter of ``params``,
    set ``params[name].value = value`` (the engine setter validates it); skip-with-debug
    if the param is absent for the SELECTED program (a different program may not expose it).
    MUST be called on the POST-SWAP command (see button.py): selecting the program swaps
    the active startProgram command, and the options have to land on the new one.

    Returns the list of names actually applied."""
    applied: list[str] = []
    if not isinstance(params, dict):
        return applied
    for name, value in options.items():
        param = params.get(name)
        if param is None:
            _LOGGER.debug(
                "apply_pending_options: option '%s' absent in the selected program "
                "command; skipped",
                name,
            )
            continue
        param.value = value
        applied.append(name)
        _LOGGER.debug("apply_pending_options: applied option '%s'=%s", name, value)
    return applied


class HonProgramOptionEntity(HonBaseEntity):
    """Shared mixin for the writable program-option entities (switch/select/number).

    Read = pending (the buffered, not-yet-started choice) else the live device value.
    Write = to the coordinator pending-options store ONLY (no send); the real send happens
    in button.py on "Start program". ``available`` is the base connection+present check with
    NO ``remoteCtrValid`` gate (consistent with start/pause: a refused write surfaces as a
    command_error, the control is not hidden). Subclasses pass the hOn ``param_name`` they
    buffer/read up to ``__init__``."""

    def __init__(self, coordinator, appliance_id: str, param_name: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._param = param_name
        # Resolve the startProgram option parameter ONCE. ``available_settings`` merges the
        # parameter across ALL program categories and measures each range by its ``.values``
        # (which enumerates min..max) -- expensive on a 100-program washer. Caching the
        # resolved param here keeps the per-read available/value/range access off that hot
        # path (the live min/max/step are still read fresh off this cached object).
        self._option_param = startprogram_option_param(self._appliance, param_name)

    def _options_store(self) -> dict:
        """The top-level pending-options store {appliance_id: {param: value}}."""
        return self._coordinator_store(PROGRAM_PENDING_OPTIONS)

    def _pending(self) -> dict:
        """The buffered option map for THIS appliance ({param: value}), or {}."""
        per = self._options_store().get(self._appliance_id)
        return per if isinstance(per, dict) else {}

    def _buffer(self, value: str) -> None:
        """Store the chosen value WITHOUT sending; the start button applies it later."""
        store = self._options_store()
        per = store.get(self._appliance_id)
        if not isinstance(per, dict):
            per = {}
            store[self._appliance_id] = per
        per[self._param] = value
        _LOGGER.debug(
            "ProgramOption debug: buffered '%s'=%s id=%s param=%s",
            redact_id(getattr(self, "_attr_unique_id", None), self._appliance_id)
            or self.__class__.__name__,
            value,
            redact_id(self._appliance_id),
            self._param,
        )
        self.async_write_ha_state()

    def _current_raw(self):
        """Pending value if buffered, else the live device value.

        Reads ``_get_attr(param)`` (the direct attribute the device reports) and then
        ``_get_attr("startProgram." + param)`` (startProgram is not shadow-synced into
        attributes, unlike ``settings``)."""
        pending = self._pending().get(self._param)
        if pending is not None:
            return pending
        raw = self._get_attr(self._param)
        if raw is not None:
            return raw
        return self._get_attr(f"{STARTPROGRAM_COMMAND}.{self._param}")

    @property
    def available(self) -> bool:
        # Base connection+present check, NO remoteCtrValid gate (see class docstring).
        return super().available

    @classmethod
    def supports(cls, appliance, param_name: str, drop: tuple[str, ...] = ()) -> bool:
        """Capability gate: the device declares ``param_name`` in startProgram AND it is
        genuinely settable (>= 2 reachable values / not fixed)."""
        param = startprogram_option_param(appliance, param_name)
        if param is None:
            return False
        return is_settable_option(param, drop)
