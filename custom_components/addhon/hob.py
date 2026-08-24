# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Induction hob (IH/HOB): the one thing a hob lets a remote client change.

Dependency-light on purpose, like `air_purifier.py` and `hood.py`: it owns the
IH-specific facts so the Home Assistant platform modules stay thin, and it derives
everything from the LIVE command schema plus the model catalogue -- never from a
model name, a nickname or a serial.

WHAT A HOB CANNOT DO, AND WHY THIS MODULE IS SO SMALL
-----------------------------------------------------
There is no remote way to switch a cooking zone on, off, or up. The live schema of
a HA2MTSJ68MC declares exactly ONE free parameter across all its commands --
`settings.powerManagement` -- and no `stopProgram` at all; its `startProgram` is a
recipe bundle of two dozen mostly-fixed fields. The decompiled app agrees: the hob
screens issue `startProgram` and nothing else, and the panel is where power is set.
Issue #84's first point is therefore MITIGABLE, not solvable, and shipping a
"zone power" control that silently did nothing would be worse than shipping none.

WHAT `powerManagement` ACTUALLY IS
----------------------------------
The intake LIMIT of the whole hob in kW, not the power of anything. The app's own
component is named `HobsPowerManagment` and renders each level through a
level-to-kW dictionary; `model_attributes.power` (15 on the reporting device) is a
different quantity entirely, the number of panel steps ONE zone has. Lowering the
limit while cooking reduces what the zones can draw, which is why every label this
module produces talks about a limit and never about power.

The level-to-kW dictionary depends on the model. The app picks the second of two
by a series test it calls `isSupernova`, and the first otherwise; the reporting
hob is `series6`, so it gets the first, whose six entries match its declared 0..5
range exactly. Both are transcribed below, and the OFFERED options are always the
intersection of a dictionary with the levels the device declares -- a hob whose
range disagreed with its series loses the levels it cannot honour instead of being
offered a value the cloud would reject.
"""
from __future__ import annotations

from .hon_commands import command_param, param_values

HOB_SETTINGS_COMMAND = "settings"
HOB_POWER_LIMIT_PARAM = "powerManagement"

# Every parameter an IH entity reads as state and/or writes as a command field.
# Same contract as `air_purifier.AP_ENTITY_PARAMS`: `diagnostics._mapped_sets`
# walks description tables and cannot see a fixed-key entity, so a name missing
# from here is reported to the user as an unmapped control that already ships.
HOB_ENTITY_PARAMS = frozenset({HOB_POWER_LIMIT_PARAM})

# `isSupernova(series)` in the app: the series values whose hobs use the second
# level-to-kW dictionary. Spelled as the app spells them, lowercased on comparison
# because the catalogue value is echoed from the cloud and nothing normalises it.
HOB_SUPERNOVA_SERIES = frozenset({"invisible", "double", "tft"})

# Level -> kW, for the models the app does NOT class as Supernova. None is "no
# limit", which is what level 0 means: the hob draws whatever it needs.
_POWER_LIMIT_KW = {
    "0": None,
    "1": "2.5",
    "2": "3.5",
    "3": "4.5",
    "4": "5.5",
    "5": "6.5",
}

# Level -> kW for the Supernova models. A DIFFERENT scale, not an extension of the
# one above: level 1 is 2.0 kW here and 2.5 kW there, so picking the wrong
# dictionary mislabels every option rather than merely running short.
_POWER_LIMIT_KW_SUPERNOVA = {
    "0": None,
    "1": "2.0",
    "2": "2.5",
    "3": "3.0",
    "4": "3.5",
    "5": "4.5",
    "6": "5.5",
    "7": "6.8",
    "8": "7.2",
}


def _option_key(kilowatts: str | None) -> str:
    """Stable machine key for one kW step, rendered per language by the state
    translations. The decimal point becomes an underscore because a translation
    key may not carry one, and the number stays in the key so a reader of the
    JSON can see which option is which without a lookup table."""
    if kilowatts is None:
        return "no_limit"
    return "kw_" + kilowatts.replace(".", "_")


# Every option key either dictionary can produce. Exported so the translations can
# be checked against the code rather than against a hand-kept list.
HOB_POWER_LIMIT_OPTIONS: tuple[str, ...] = tuple(
    dict.fromkeys(
        _option_key(value)
        for mapping in (_POWER_LIMIT_KW, _POWER_LIMIT_KW_SUPERNOVA)
        for value in mapping.values()
    )
)


def is_supernova(series: object) -> bool:
    """True when this hob uses the second level-to-kW scale."""
    return str(series or "").strip().lower() in HOB_SUPERNOVA_SERIES


def power_limit_levels(appliance, series: object) -> dict[str, str] | None:
    """Level -> option key for the intake limits this hob really offers, or None.

    None is the capability gate: a hob that does not declare `powerManagement` as
    a writable, enumerable parameter gets no control at all rather than one that
    writes into nothing.

    The result is the INTERSECTION of the series' dictionary with the levels the
    device declares, in the device's own order. A level the schema offers and the
    dictionary does not is dropped, because the integration cannot say what its
    limit would be and an unlabelled step is not a choice a user can make; a level
    the dictionary knows and the schema does not is dropped for the opposite
    reason, since the cloud would reject it.

    A single surviving level is treated as no control: a picker with one entry
    cannot change anything, and offering it suggests otherwise.
    """
    param = command_param(appliance, HOB_SETTINGS_COMMAND, HOB_POWER_LIMIT_PARAM)
    if param is None:
        return None
    mapping = _POWER_LIMIT_KW_SUPERNOVA if is_supernova(series) else _POWER_LIMIT_KW
    levels = {
        raw: _option_key(mapping[raw])
        for raw in param_values(param)
        if raw in mapping
    }
    return levels if len(levels) > 1 else None
