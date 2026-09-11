# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

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

from homeassistant.exceptions import HomeAssistantError

from .base_entity import HonBaseEntity
from .client.engine.parameter.range import HonParameterRange
from .const import (
    DOMAIN,
    PROGRAM_PARAM_NAMES,
    PROGRAM_PENDING_OPTIONS,
    PROGRAM_PENDING_STORE,
)
from .client.engine.exceptions import ApiError
from .debug_utils import redact_id
from .hon_commands import (
    SYNTHETIC_CATEGORY,
    get_command,
    get_commands,
    param_range,
    param_values,
)
from .param_rollback import restore_params, snapshot_params

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


async def async_send_program(hass, client, appliance, program_code: str) -> None:
    """Apply ``program_code`` to the ``startProgram`` command and send it, SWAP-AWARE.

    Setting the ``program``/``prCode`` parameter SWAPS the active startProgram command in
    ``appliance.commands`` (a ``HonParameterProgram`` setter does ``command.category =
    value`` -> ``appliance.commands["startProgram"] = categories[code]``, replacing the
    object), so we re-read the now-active command and send THAT, not the stale pre-swap
    object. Mirrors the proven washer flow (button.py:200-251); used by the fridge program
    select for an IMMEDIATE send (no buffer/Start-button cycle). The per-program fixed
    ancillaries (programFamily/zone/holidayMode/quickMode*/remoteActionable/remoteVisible)
    ride along in the swapped category's own schema and are serialized by ``command.send()``
    -- we never set them by hand.
    """
    if not appliance or not client:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="appliance_or_client_unavailable",
        )

    def _do():
        async def _inner():
            commands = get_commands(appliance)
            command = commands.get(STARTPROGRAM_COMMAND)
            if command is None:
                raise RuntimeError(
                    f"Command '{STARTPROGRAM_COMMAND}' not found on the device"
                )
            params = getattr(command, "parameters", None)
            params = params if isinstance(params, dict) else {}
            # Snapshot for rollback (mirrors async_send_command). Applying the program
            # SWAPS appliance.commands[startProgram] to the selected category (a
            # HonParameterProgram setter does command.category=code; the param's own
            # __dict__ is untouched). A prCode/enum-typed program param instead mutates its
            # own value. If send() then fails we must undo BOTH possibilities, otherwise
            # select.py (which skips the refresh on error) would keep running against a
            # local command/category the cloud never accepted, until the next coordinator
            # poll. The command-pointer reset below is the load-bearing undo for the swap;
            # the param-__dict__ restore covers the value-mutating (prCode) case.
            original_command = command
            snapshots = snapshot_params(params)
            try:
                applied = False
                for pname in PROGRAM_PARAM_NAMES:
                    if pname in params:
                        params[pname].value = program_code
                        applied = True
                        break
                if not applied:
                    raise RuntimeError(
                        f"startProgram has no program parameter {PROGRAM_PARAM_NAMES} "
                        f"among {sorted(params)}"
                    )
                # Re-read after the category swap so send() targets the SELECTED program's
                # command (carrying its fixed ancillaries), not the stale pre-swap object.
                refreshed = commands.get(STARTPROGRAM_COMMAND)
                if refreshed is not None and refreshed is not command:
                    _LOGGER.debug(
                        "ProgramSend debug: startProgram swapped after program apply (%s)",
                        program_code,
                    )
                    command = refreshed
                await command.send()
            except Exception:
                # Restore the pre-swap parameter state (shared helper copies __dict__
                # directly, so a value-mutating param does not re-fire its rules) and
                # reset the swapped command pointer to the original.
                restore_params(params, snapshots)
                commands[STARTPROGRAM_COMMAND] = original_command
                raise
            _LOGGER.debug(
                "ProgramSend debug: startProgram '%s' send completed id=%s",
                program_code,
                redact_id(getattr(appliance, "id", None)),
            )

        client.run_command_sync(_inner())

    try:
        await hass.async_add_executor_job(_do)
    except ApiError as error:
        # The REACHABLE refusal, translated here so every program sender answers it the
        # same way the sparse dispatcher already does (`async_dispatch_patch`).
        # `HonCommand._send_parameters` raises `ApiError` on a falsy api result and its
        # message is the untranslated literal "Can't send command", which reached the
        # user as "Comando non riuscito: Can't send command" -- the exact failure the
        # dispatcher's own branch was written to remove. Without this the two halves of
        # one fridge switch answered a single cloud refusal with two different messages:
        # `command_rejected` on the off, this literal on the on.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_rejected",
        ) from error


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
        if step <= 0:
            return []
        out: list[str] = []
        # Index-based enumeration (lo + i*step), NOT a `+= step` accumulator: the
        # accumulator compounds float error on decimal steps and can DROP the final grid
        # point -- the exact defect range.py.values was rewritten to avoid. Each point is
        # rounded to the lo/step precision so a decimal step renders "20.7", not
        # "20.700000000000003". The +1e-9 only absorbs the i*step rounding drift; a step
        # that overshoots the max (e.g. 0..10 step 20) still emits a single value, never
        # one beyond hi. Bounded by _MAX_RANGE_CHOICES so a malformed range cannot loop.
        # Reuse HonParameterRange._decimals (single source of truth for grid precision)
        # so this matches range.py.values' rounding and the tokens keep round-tripping.
        ndigits = max(HonParameterRange._decimals(lo), HonParameterRange._decimals(step))
        for index in range(_MAX_RANGE_CHOICES):
            current = round(lo + index * step, ndigits)
            if current > hi + 1e-9:
                break
            token = _num_str(current)
            if token not in drop and token not in out:
                out.append(token)
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
        lo, hi, step = rng
        if step <= 0:
            return False
        if not drop:
            # >= 2 reachable values: lo AND lo+step must both fall within [lo, hi]. A range
            # whose step overshoots the max (e.g. 0..10 step 20) has ONE real value -> not a
            # control (max > min alone would wrongly accept it).
            return lo + step <= hi + 1e-9
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

    def _selected_category(self):
        """The SELECTED program's category command, or None."""
        code = self._selected_program_code()
        if code is None:
            return None
        command = startprogram_command(self._appliance)
        categories = getattr(command, "categories", None) if command is not None else None
        if not isinstance(categories, dict):
            return None
        return categories.get(code)

    def _prescribed_raw(self):
        """What the SELECTED program prescribes for this option, or None.

        The hOn app shows exactly this: with a program freshly picked from the catalogue
        its `setValue` falls through to `dictionaryParameter.fixedValue ?? defaultValue`
        of the selected category (decomp.txt:1778771-1778793), which is
        ``HonParameter.schema_value``. Reading the category's ``value`` instead would
        report the user's own past choice as the program's prescription -- three writers
        leave one in there and nothing resets it (see `schema_value`'s own docstring).

        Three cases answer None, each falling the caller back to the device reading:

        - the program is a FAVOURITE. It IS a saved user configuration, so its VALUE is
          the prescription and its schema node belongs to the base program. The app
          agrees: opening a favourite fills `setValue`'s highest-precedence slot
          (@3617979) instead of reading the schema.
        - a RULE can write this parameter. The cascade fires when the options are applied
          at Start, so the wire would carry something else and the display would be a
          promise the payload does not keep. Measured: rule `spinSpeed <- temp==20 -> 400`
          with a schema default of 1400. No washing-group catalogue on disk carries a
          `programRules`, so this is a guard, not a live path (analysis section 9.8).
        - the schema prescribes nothing, or the category does not declare the option.
        """
        category = self._selected_category()
        if category is None:
            return None
        param = self._category_option_param()
        if param is None:
            return None
        if getattr(category, "is_favourite", False):
            return getattr(param, "value", None)
        if self._param in (getattr(category, "rule_targets", None) or ()):
            return None
        return getattr(param, "schema_value", None)

    def _renderable(self, raw) -> bool:
        """True if THIS control can display ``raw``; the base control always can.

        A switch is a boolean by construction and never refuses. The select and the number
        override this: a prescription outside the option map or off the live grid would
        blank the entity, and an honest device reading beats a blank (analysis 8.5.2 --
        on the reporter's own dump 34 programs pin `temp` and 93 pin `dirtyLevel`, several
        to codes the merged control does not offer)."""
        return True

    def _current_raw(self):
        """Pending value if buffered, else what the selected program prescribes, else the
        live device value.

        Reads ``_get_attr(param)`` (the direct attribute the device reports) and then
        ``_get_attr("startProgram." + param)`` (startProgram is not shadow-synced into
        attributes, unlike ``settings``)."""
        pending = self._pending().get(self._param)
        if pending is not None:
            return pending
        prescribed = self._prescribed_raw()
        if prescribed is not None and self._renderable(prescribed):
            return prescribed
        raw = self._get_attr(self._param)
        if raw is not None:
            return raw
        return self._get_attr(f"{STARTPROGRAM_COMMAND}.{self._param}")

    def _active_option_param(self):
        """Resolve this param off the ACTIVE startProgram command's parameters only.

        The ACTIVE command is the LAST STARTED program, not the selected one: the category
        swap happens in button.py at Start, and `HonCommandLoader._set_last_category`
        re-points it from the command history on every load. Selecting a program in the
        select writes the pending store and nothing else, so this reflects the user's
        choice only after they have actually started it. `_selected_option_param` is the
        program-accurate resolver; this stays as its second-best fallback (it IS the right
        answer while a cycle runs, when no program is pending).

        Never calls ``available_settings`` (no range enumeration). Returns None if the
        command/param is absent."""
        command = startprogram_command(self._appliance)
        params = getattr(command, "parameters", None) if command is not None else None
        if isinstance(params, dict):
            return params.get(self._param)
        return None

    def _selected_program_code(self) -> str | None:
        """The BUFFERED program code -- the one the user has actually picked -- or None.

        ``select.async_select_option`` writes ONLY ``PROGRAM_PENDING_STORE`` (no send, no
        category swap), so this store is the only place the current choice exists before
        Start. ``SYNTHETIC_CATEGORY`` is refused: it is the ``{"_": self}`` placeholder a
        category-less command reports and never a program code."""
        code = self._coordinator_store(PROGRAM_PENDING_STORE).get(self._appliance_id)
        if code is None or str(code) in ("", SYNTHETIC_CATEGORY):
            return None
        return str(code)

    def _category_option_param(self):
        """Resolve this param off the SELECTED program's startProgram CATEGORY, or None.

        ``HonCommand.categories`` holds one command object per program, each with its OWN
        ``parameters`` -- the schema the hOn app itself rebuilds its option set from
        (``normalize`` @decomp.txt:1773682, see apk/analysis/issue98-99-program-options-
        and-wd-dry.md). A program that does not expose this option simply has no such key,
        and one that pins it carries a ``HonParameterFixed``. Cheap: one dict lookup per
        read, no ``available_settings``, no range enumeration.

        Returns None when nothing is pending, when the program parameter is not
        category-backed (a ``prCode``-typed enum keys the store by code, not by category
        name, so the lookup misses), or when the category omits the param."""
        category = self._selected_category()
        params = getattr(category, "parameters", None) if category is not None else None
        if isinstance(params, dict):
            return params.get(self._param)
        return None

    def _selected_option_param(self, drop: tuple[str, ...] = ()):
        """This param as the SELECTED program declares it, else the widest fallback.

        A program IS pending -> its own category, and on failure the merged superset. The
        active command is deliberately NOT consulted in this branch: it describes the last
        program STARTED (see ``_active_option_param``), so preferring it over the merged
        param would answer a question about the selected program with a different
        program's narrower value set -- the very substitution this resolver exists to
        remove (PR #103 review, sourcery-ai + greptile).

        NOTHING is pending -> the active command, then the merged superset. Here the
        active command is the right answer rather than a stale one: with no selection to
        honour, the program whose category is loaded is what the machine last ran or is
        running, which is the PR #38 behaviour this preserves.

        A candidate is taken only when it is genuinely SETTABLE there
        (``is_settable_option``); otherwise the walk falls through to the merged param.
        That clause is load-bearing and deliberate: a program that pins or omits the
        option leaves the control on the merged param, keeping the value set it has today
        instead of degenerating into an empty select or a zero-width number. It does mean
        the control stays writable with values that program will not take -- HIDING it is
        issue #98's own request and belongs to the ``available`` gate, a separate and
        user-visible decision (an unavailable entity breaks the automations that write it)
        and not something to smuggle in through a resolver. Until then the two outcomes
        are the documented ones: an absent option is skipped by
        ``apply_pending_options`` at Start, and a pinned one is refused by the engine
        setter with ``command_error``.

        ``drop`` is the description's sentinel tuple, passed through so a sentinel-only
        candidate is not mistaken for a usable one."""
        if self._selected_program_code() is not None:
            candidate = self._category_option_param()
        else:
            candidate = self._active_option_param()
        if candidate is not None and is_settable_option(candidate, drop):
            return candidate
        return self._option_param

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
