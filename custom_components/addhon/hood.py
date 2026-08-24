# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cooker hood (HO): parameter names, live bounds, and the write-channel decision.

Dependency-light on purpose, like `air_purifier.py`: it owns the HO-specific facts
so the Home Assistant platform modules stay thin, and it never reads a model name,
a nickname or a serial. What a hood can do is derived exclusively from its LIVE
command schema, so a hood declaring four speed levels offers four and one
declaring six offers six -- `model_attributes.speedLevel` is never consulted.

THE WRITE CHANNEL, AND WHY IT DIVERGES FROM THE OFFICIAL APP
------------------------------------------------------------
The app drives the hood entirely through `startProgram`/`stopProgram`: a speed
change is `startProgram {windSpeed, onOffStatus: "1"}` and a light toggle is
`startProgram {lightStatus}`. We deliberately do NOT follow it for the first two.

`client/transport/api.py::send_command` appends `data["programName"] =
program_name.upper()` to EVERY `startProgram` whose command carries a category
name, and this hood's `startProgram` is a categorised command (the dump shows the
synthetic `startProgram.program` parameter, the signature of
`client/engine/commands.py::_create_parameters`). Sending a speed through
`startProgram` would therefore put a `programName` on the wire that the app never
sends, on a device whose only proven-executed command in the whole dossier is a
`stopProgram`. So:

  * speed and power-on  -> the `settings` command's `windSpeed`, declared writable
    in the live schema and the parameter Andre0512/hon has been writing on hoods
    for years;
  * light               -> the `settings` command's `lightStatus`, same reasoning,
    driven by the shared `HonSettingsSwitch`;
  * switch off          -> `stopProgram`, the one command this exact hood is known
    to have accepted AND executed (its `commandHistory` carries both
    `timestampAccepted` and `timestampExecuted`). `api.send_command` adds
    `programName` only to `startProgram`, so the concern above does not apply.

The `settings` channel also cannot leak a category selector. The synthetic
`category` parameter lives in the `custom` group, and `HonCommand.parameter_groups`
only puts the `parameters` group on the wire -- provided nothing ever ASKS for
`category` (or `program`) by name. Those two are the dispatcher's `_SELECTOR_KEYS`:
naming either one in a patch or in a settings dict makes the engine rewrite
`appliance.commands` permanently and ships the key verbatim to the cloud. No entity
of this integration may put them in a write, and a test pins the wire payload.

See `diagnostics/issue83-84/SPEC-implementazione.md` sections 1.2 and 3 for the
full argument and the evidence behind it.
"""
from __future__ import annotations

from .hon_commands import command_param, param_range

# The command a hood setting is written through, and the one that stops it.
HOOD_SETTINGS_COMMAND = "settings"
HOOD_STOP_COMMAND = "stopProgram"

# The four parameters HO entities read as state and/or write as command fields.
HOOD_SPEED_PARAM = "windSpeed"
HOOD_LIGHT_PARAM = "lightStatus"
HOOD_DELAY_TIME_PARAM = "delayTime"
HOOD_DELAY_STATUS_PARAM = "delayTimeStatus"

# Declared next to the constants the platforms actually use, exactly like
# `air_purifier.AP_ENTITY_PARAMS` and for the same reason: `diagnostics._mapped_sets`
# cannot see an entity that has no description table, so a name missing from here
# is reported to the user as an unmapped control that in fact already ships. The
# hood fan is such an entity (a fixed-key class, not a table row), and `windSpeed`
# was exactly the name the coverage block kept accusing.
HOOD_ENTITY_PARAMS = frozenset(
    {
        HOOD_SPEED_PARAM,
        HOOD_LIGHT_PARAM,
        HOOD_DELAY_TIME_PARAM,
        HOOD_DELAY_STATUS_PARAM,
    }
)

# Lowest wind speed that actually moves air. 0 is a valid schema value but it means
# "fan stopped", and Home Assistant expresses that as `percentage = 0` / `turn_off`,
# never as a speed step -- so the percentage axis starts here.
HOOD_MIN_SPEED = 1


def speed_levels(appliance) -> tuple[int, ...] | None:
    """Every wind-speed level this hood declares as writable, lowest first.

    Read from the LIVE `settings.windSpeed` bounds, which is also the capability
    gate of the fan entity: a hood that does not declare the parameter as a
    writable range gets no fan rather than a control that writes into nothing.

    THE DECLARED INCREMENT IS PART OF THE GRID, not a detail to round away. A
    hood declaring 0..6 step 2 accepts 0, 2, 4 and 6 and refuses everything in
    between, so the levels are enumerated from the device's own low bound and
    only then filtered: walking from `HOOD_MIN_SPEED` instead would produce
    1/3/5, three values that hood rejects. The hood of issue #83 declares step 1
    and is unaffected either way, which is exactly why the grid has to come from
    the schema rather than from what that one device happens to report.

    Levels below `HOOD_MIN_SPEED` are dropped because the schema counts the
    stopped state (0) among its values while the percentage axis must not: Home
    Assistant expresses "stopped" as `percentage = 0` / `turn_off`, never as a
    speed step. `model_attributes.speedLevel` reports the same 5 on the hood of
    issue #83, but it is a catalogue value and the schema is the operative one.

    Returns None for a degenerate axis (fewer than two levels), so a firmware
    that pinned `windSpeed` to a single value loses the fan instead of shipping a
    slider with one position.
    """
    param = command_param(appliance, HOOD_SETTINGS_COMMAND, HOOD_SPEED_PARAM)
    if param is None:
        return None
    bounds = param_range(param)
    if bounds is None:
        return None
    low, high, step = bounds
    # A missing, zero or fractional increment falls back to 1: the schema counts
    # wind speeds in whole steps, and a range that cannot say how wide its own
    # step is has told us nothing that beats the obvious default.
    increment = int(step) if step and step >= 1 else 1
    levels = tuple(
        value
        for value in range(int(low), int(high) + 1, increment)
        if value >= HOOD_MIN_SPEED
    )
    return levels if len(levels) > 1 else None


def speed_level(raw) -> int | None:
    """The reported wind speed as an integer level, or None when unreadable.

    The client folds an attribute through `str_to_float`, so the same value can
    arrive as 3, 3.0 or "3"; anything that is not a number at all (an empty
    reading a device sends while booting) reads as unknown rather than as zero,
    which would claim the hood is off.
    """
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None
