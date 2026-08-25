# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cooker hood (HO): parameter names, live bounds, and the write-channel decision.

Dependency-light on purpose, like `air_purifier.py`: it owns the HO-specific facts
so the Home Assistant platform modules stay thin, and it never reads a model name,
a nickname or a serial. What a hood can do is derived exclusively from its LIVE
command schema, so a hood declaring four speed levels offers four and one
declaring six offers six -- `model_attributes.speedLevel` is never consulted.

THE DEVICE HAS THREE STATES, NOT TWO
------------------------------------
Reported from the field on the hood of issue #83, and consistent with everything
the live schema and the decompiled app say:

  1. `onOffStatus` is the DISPLAY, not the fan. 1 is the lit panel you get by
     tapping the glass; 0 is the dark panel.
  2. While the panel is dark the hood ignores `windSpeed` and `lightStatus`
     outright -- it does not even beep, which is its acknowledgement tone.
  3. `windSpeed = 0` stops the fan and LEAVES THE PANEL LIT. Stopping extraction
     and switching the appliance off are two different actions.

The first shipped version of this module collapsed 2 and 3: the fan's off sent
`stopProgram`, which pins all three parameters to 0, so switching the fan off put
the hood in the dark state and no later write could bring it back. Home Assistant
could switch the hood off exactly once and then had to wait for someone to walk
over and touch the glass.

THE WRITE CHANNEL
-----------------
Anything that has to reach `onOffStatus` must travel on `startProgram`: it is the
only command of the three that declares the parameter as 1, and the `settings`
command does not declare it at all. So:

  * speed, and the fan's own off -> `startProgram` carrying `windSpeed` alone. The
    dispatcher adds the command's one mandatory field, `onOffStatus`, pinned to "1"
    by the schema, which reproduces the official app's speed body EXACTLY
    (`{windSpeed, onOffStatus: "1"}`, `apk/decomp.txt:3137444-3137456`). A speed
    change therefore also wakes a dark panel, like the app;
  * switch on                    -> `startProgram` with nothing but that mandatory
    `onOffStatus`, which is the app's wake-up without a speed attached;
  * light, delayed switch-off    -> the `settings` command's `lightStatus` and
    `delayTime`/`delayTimeStatus`, driven by the shared `HonSettingsSwitch`. Left
    where they are on purpose: the reporter confirmed both work through that
    channel, and the app's own light body carries no `onOffStatus` either, so
    neither ours nor theirs lights the lamp on a dark panel;
  * switch off                   -> `stopProgram`, the one command this exact hood
    is known to have accepted AND executed (its `commandHistory` carries both
    `timestampAccepted` and `timestampExecuted`), sent with the same three pinned
    zeroes that history records.

`programName`, AND WHY `hood_patch` EXISTS
------------------------------------------
`client/transport/api.py::send_command` appends `data["programName"] =
program_name.upper()` to EVERY `startProgram` whose command carries a category
name, and this hood's `startProgram` is categorised (the dump shows the synthetic
`startProgram` selector parameter, the signature of
`client/engine/commands.py::_create_parameters`). The category is a placeholder the
cloud invented for a command that starts nothing: its cleaned name is "undefined"
and the app's translation catalogue has no `PROGRAMS.HO.*` key at all.

The app never sends the field. Its three hood senders -- two in the send-command
epic, one in the watchdog epic -- each build the body inline with the same ten
keys, and `programName` is not among them (`apk/decomp.txt:1815935-1816012`,
`:1816015-1816086`, `:1817613-1817697`). So every hood write goes out through
`hood_patch`, which pins the suppression in ONE place rather than trusting each
entity to remember it.

WHAT NO HOOD WRITE MAY NAME
---------------------------
The synthetic selector lives in the `custom` group, and
`HonCommand.parameter_groups` only puts the `parameters` group on the wire --
provided nothing ever ASKS for it by name. The selector names are the dispatcher's
`_SELECTOR_KEYS`: naming either one in a patch or in a settings dict makes the
engine rewrite `appliance.commands` permanently and ships the key verbatim to the
cloud. No entity of this integration may put them in a write, and a test both pins
the wire payload and scans this module for the literals.

See `diagnostics/issue83-84/SPEC-implementazione.md` sections 1.2 and 3 for the
original argument and `apk/analysis/issue83-hood-controls.md` for the evidence that
overturned it.
"""
from __future__ import annotations

from .command_dispatch import CommandPatch
from .hon_commands import command_param, param_range

# The command a hood setting is written through, the one that reaches the panel
# and the fan, and the one that switches the appliance off.
HOOD_SETTINGS_COMMAND = "settings"
HOOD_START_COMMAND = "startProgram"
HOOD_STOP_COMMAND = "stopProgram"

# The five parameters HO entities read as state and/or write as command fields.
HOOD_SPEED_PARAM = "windSpeed"
HOOD_LIGHT_PARAM = "lightStatus"
HOOD_POWER_PARAM = "onOffStatus"
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
        HOOD_POWER_PARAM,
        HOOD_DELAY_TIME_PARAM,
        HOOD_DELAY_STATUS_PARAM,
    }
)


def hood_patch(command_name: str, values: dict[str, str], action: str) -> CommandPatch:
    """One hood intent, with the `programName` suppression already applied.

    EVERY hood write must go through here. `program_name=""` is what keeps the
    placeholder category off the wire on a `startProgram`; the transport ignores
    the field entirely for any other command, so passing it unconditionally costs
    nothing and removes the only way an entity could get this wrong.

    Sparse by construction: the dispatcher transmits these values plus whatever the
    live schema marks mandatory, and nothing else. That is what stops a `settings`
    write from also carrying the three clock fields the device never mirrors back
    (it would zero the hood's clock on every toggle -- the wine cooler's #62 in
    another appliance), and what makes a `startProgram` speed change come out as
    the app's own two-key body.
    """
    return CommandPatch(command_name, values, action=action, program_name="")

# Lowest wind speed that actually moves air. 0 is a valid schema value but it means
# "fan stopped", and Home Assistant expresses that as `percentage = 0` / `turn_off`,
# never as a speed step -- so the percentage axis starts here.
HOOD_MIN_SPEED = 1


def speed_levels(appliance) -> tuple[int, ...] | None:
    """Every wind-speed level this hood declares as writable, lowest first.

    Read from the LIVE `startProgram.windSpeed` bounds, which is also the
    capability gate of the fan entity: a hood that does not declare the parameter
    as a writable range gets no fan rather than a control that writes into nothing.

    Read from the command the fan WRITES, not from `settings`, even though this
    hood declares the same 0..5 range on both. A gate on one command and a write on
    another is a gate that can pass while the write has nowhere to land.

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
    param = command_param(appliance, HOOD_START_COMMAND, HOOD_SPEED_PARAM)
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
