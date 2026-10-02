# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Programme duration and `energyLabel` of a washer start off the HQD platform. Issue #99.

On HQD the hOn app reads the label's minutes from a `remainingTime` node (see
`HonCommand._energy_label`). Everywhere else (CHG, or no platform at all) it adds up
the category's `remainingTimes` table instead -- `actualDuration`, apk2
decomp.txt:1367613-1368630, behind `getWashDrySteamCyclesDuration` @1368632-1368677 --
and its builder (`getWmWdSendCommandPayload` @1361548, branch @1362303-1362323) turns
the minutes into the label it writes in both `attributes` and `ancillaryParameters`
(@1362484-1362736, `calculateEnergyLabel` @1364133-1364169).

The table, as the app's own sample catalog has it (Hoover HDPD4149AMBC/1-S, CHG):

    dirtyLevel  {"1": 116, "2": 151, "3": 231, "default": "231"}       the base
    dryLevel    {"+dryTypeC": {"0": 0, "1": 230, ...}, "dryTypeC": {"3": -60, ...}}
    spinSpeed   {"wash": {"0": -6, ...}, "dryTypeC": {...}, "prCode6": {...}}
    prewash     {"wash": {"0": 0, "1": 20}, "dryTypeC": {...}, ...}   and every option

A sub-table whose name starts with `+` holds minutes ADDED to a wash; one without it
holds a delta on the base of a drying-only or steam-only programme. The duration is
the sum of the entries the sent values select. Whatever the app cannot read -- an entry
that is not a number, a missing sub-table, a JavaScript TypeError -- leaves the start
without a duration, and the builder then sends '0'.

A port of the reference implementation checked against the decompiled code executed
in Node, 0 differences over 38,000 random cases
(apk2/analysis/issue99-wd/4-actual-duration-energy-label.md, strumenti/energy/ref.py).
Values are JSON values, and the JavaScript coercions that decide the result are kept:
a value is tested on its trimmed text (`stringIsNumeric`) but the table is indexed with
it raw, so " 2 " passes the test and then misses the entry "2"; `"231"` counts as 231;
an integer indexes like its text.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

# The appliance types the app computes a duration for (@1367615-1367646).
WASHER_TYPES = ("WM", "WD")
# Object.keys(ProgramType) (@1030826-1030843): any other programType means no duration.
_PROGRAM_TYPES = ("W", "D", "S", "WD", "WS", "W+S", "W+D", "W+D+S")
# The programme types that wash (@1367753-1367775).
_WASHING_TYPES = ("W", "WD", "WS", "W+D", "W+D+S", "W+S")

# What JavaScript's String.prototype.trim and Number() strip.
_JS_WHITESPACE = (
    "\t\n\x0b\x0c\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)
_DECIMAL = re.compile(
    r"[+-]?(?:Infinity|[0-9]+\.?[0-9]*(?:[eE][+-]?[0-9]+)?|\.[0-9]+(?:[eE][+-]?[0-9]+)?)"
)
_PREFIXED = re.compile(r"0(?:[xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)")
_RADIX = {"x": 16, "o": 8, "b": 2}

# JavaScript's `undefined`: a key that is not there, as opposed to a null value.
_MISSING: Any = object()


class _NoDuration(Exception):
    """The app's NaN, or a TypeError its try/catch absorbs: no duration either way."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _js_string(value: Any) -> str:
    """`String(value)` for what `Array.prototype.join` meets inside a list."""
    if value is None or value is _MISSING:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if _is_number(value):
        return _key(value)
    if isinstance(value, list):
        return ",".join(_js_string(item) for item in value)
    if isinstance(value, str):
        return value
    return "[object Object]"


def _to_number(value: Any) -> float:
    """JavaScript's `value - 0`: NaN for what cannot be read, 0 for null and blanks."""
    if value is _MISSING:
        return math.nan
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int):
        try:
            return float(value)
        except OverflowError:
            return math.copysign(math.inf, value)
    if isinstance(value, float):
        return value
    if isinstance(value, list):  # through its text: [] is 0, [5] is 5
        return _to_number(",".join(_js_string(item) for item in value))
    if not isinstance(value, str):
        return math.nan  # an object reads as "[object Object]"
    text = value.strip(_JS_WHITESPACE)
    if not text:
        return 0.0
    if _PREFIXED.fullmatch(text):
        return float(int(text[2:], _RADIX[text[1].lower()]))
    if _DECIMAL.fullmatch(text):
        return float(text.replace("Infinity", "inf"))
    return math.nan


def _truthy(value: Any) -> bool:
    """JavaScript truthiness: '0' is true, 0, '' and NaN are not."""
    if value is None or value is _MISSING:
        return False
    if _is_number(value):
        return not (value == 0 or math.isnan(value))
    if isinstance(value, (bool, str)):
        return bool(value)
    return True


def _string_is_numeric(value: Any) -> bool:
    """`stringIsNumeric` (@1365346-1365383): a number, or a non-blank string that reads
    as one; finite either way. Never a boolean, null or an object."""
    if isinstance(value, str):
        if not value.strip(_JS_WHITESPACE):
            return False
    elif not _is_number(value):
        return False
    return math.isfinite(_to_number(value))


def _key(value: Any) -> str:
    """The property name a value indexes an object with: a string as it is, a number
    as JavaScript prints it ("2", never "2.0")."""
    if isinstance(value, str):
        return value
    number = _to_number(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    if number.is_integer() and abs(number) < 1e21:
        return str(int(number))
    return repr(number)


def _index(key: str, size: int) -> int | None:
    """`key` as an array index below `size`, or None."""
    if key.isascii() and key.isdigit() and str(int(key)) == key and int(key) < size:
        return int(key)
    return None


def _member(obj: Any, key: str) -> Any:
    """`obj[key]`. Reading from null or undefined is the TypeError the app absorbs."""
    if obj is None or obj is _MISSING:
        raise _NoDuration
    if isinstance(obj, Mapping):
        return obj.get(key, _MISSING)
    if isinstance(obj, (list, str)):
        if key == "length":
            return len(obj)
        index = _index(key, len(obj))
        return _MISSING if index is None else obj[index]
    return _MISSING  # a number or a boolean has no such member


def _has(obj: Any, key: str) -> bool:
    """`key in obj`; on anything that is not an object, a TypeError."""
    if isinstance(obj, Mapping):
        return key in obj
    if isinstance(obj, list):
        return key == "length" or _index(key, len(obj)) is not None
    raise _NoDuration


def _sub_table(node: Any, name: str) -> Mapping[str, Any]:
    """`node[name]`, which must be an object: anything else is the app's NaN."""
    table = _member(node, name)
    if not isinstance(table, Mapping):
        raise _NoDuration
    return table


def _minutes(entry: Any) -> float:
    """A table entry as minutes; one that is not numeric voids the duration."""
    if not _string_is_numeric(entry):
        raise _NoDuration
    return _to_number(entry)


def _type_suffix(ancillary: Mapping[str, Any], name: str) -> str | None:
    """`dryType` / `steamType`: absent is fine, present must be a non-blank string, which
    the app trims and upper-cases (@1367688-1367740)."""
    value = ancillary.get(name, _MISSING)
    if value is _MISSING:
        return None
    if not isinstance(value, str) or not value.strip(_JS_WHITESPACE):
        raise _NoDuration
    return value.strip(_JS_WHITESPACE).upper()


def _total_minutes(
    appliance_type: str,
    params: Mapping[str, Any],
    ancillary: Mapping[str, Any],
    remaining_times: Any,
) -> float:
    """`actualDuration` (@1367613-1368630): the sum, or _NoDuration where it gives NaN."""
    if appliance_type not in WASHER_TYPES or not isinstance(remaining_times, Mapping):
        raise _NoDuration
    dirt = params.get("dirtyLevel", _MISSING)
    if not _string_is_numeric(dirt) or _to_number(dirt) < 0:
        raise _NoDuration
    program_type = ancillary.get("programType", _MISSING)
    if not isinstance(program_type, str) or program_type not in _PROGRAM_TYPES:
        raise _NoDuration
    dry_type = _type_suffix(ancillary, "dryType")
    steam_type = _type_suffix(ancillary, "steamType")

    def raw(name: str, minimum: float, strict: bool) -> Any:
        """params[name] as sent, if numeric and above (or at) `minimum`; else None."""
        value = params.get(name, _MISSING)
        if not _string_is_numeric(value):
            return None
        number = _to_number(value)
        return value if (number > minimum if strict else number >= minimum) else None

    # Washing only (@1367741-1367828): a washing type with no drying or steam asked for.
    wash_only = program_type in _WASHING_TYPES and (
        (dry_type is None and steam_type is None)
        or all(raw(name, 0, strict=True) is None
               for name in ("dryLevel", "dryTime", "steamLevel"))
    )
    # The raw values the table is indexed with (@1367829-1367871, @1367899-1367920).
    dry_level: Any = 0
    dry_time: Any = None
    steam_level: Any = 0
    if not wash_only and dry_type is not None:
        level = raw("dryLevel", 0, strict=False)
        dry_level = 0 if level is None else level
        dry_time = raw("dryTime", 0, strict=True)
    if not wash_only and steam_type is not None:
        level = raw("steamLevel", 0, strict=True)
        steam_level = 0 if level is None else level
    drying = _to_number(dry_level) > 0 or dry_time is not None
    steaming = _to_number(steam_level) > 0
    if not drying and appliance_type == "WD" and program_type == "D":
        raise _NoDuration  # a drying programme with nothing to dry (@1367881-1367898)
    steam_only = not wash_only and program_type == "S" and steaming
    dry_only = appliance_type == "WD" and program_type == "D" and drying
    wash_dry = appliance_type == "WD" and program_type not in ("D", "S") and drying
    with_steam = program_type != "D" and steaming
    spin: Any = 0
    if not dry_only and not steam_only:
        speed = raw("spinSpeed", 0, strict=False)
        spin = 0 if speed is None else speed
    program_code = params.get("prCode", _MISSING)
    if not _string_is_numeric(program_code):
        raise _NoDuration  # @1368050-1368061
    # The options' and the spin's sub-table: per programme code if the row has one,
    # else per dry type, else 'wash' (@1368133-1368146).
    wash_table = "wash" if dry_type is None else "dryType" + dry_type
    code_table = "prCode" + _key(program_code)

    total = 0.0
    has_dirt = False
    for name, node in remaining_times.items():
        if name == "dirtyLevel":  # @1368584-1368608
            dirt_key = _key(dirt)
            total += _minutes(
                _member(node, dirt_key) if _has(node, dirt_key) else _member(node, "default")
            )
            has_dirt = True
        elif name == "dryLevel":  # @1368467-1368583
            if dry_type is not None and (
                wash_only or steam_only or with_steam
                or ((dry_only or wash_dry) and dry_time is not None)
            ):
                # Only the wash's own entry 0 counts here; no entry 0 adds nothing.
                table = _sub_table(node, "+dryType" + dry_type)
                if "0" in table:
                    total += _minutes(table["0"])
            elif (dry_only or wash_dry) and _to_number(dry_level) > 0:
                table = _sub_table(node, ("" if dry_only else "+") + "dryType" + dry_type)
                total += _minutes(_member(table, _key(dry_level)))
        elif name == "dryTime":  # @1368409-1368466
            if (dry_only or wash_dry) and dry_time is not None:
                table = _sub_table(node, ("" if dry_only else "+") + "dryType" + dry_type)
                total += _minutes(_member(table, _key(dry_time)))
        elif name == "steamLevel":  # @1368315-1368408
            if steam_type is not None and (wash_only or wash_dry or dry_only):
                entry = _member(_sub_table(node, "+steamType"), "0")
                if entry is not _MISSING:
                    total += _minutes(entry)
            elif steam_only or with_steam:
                # The literal name, without the steam type, as the app spells it.
                table = _sub_table(node, ("" if steam_only else "+") + "steamType")
                total += _minutes(_member(table, _key(steam_level)))
        elif name == "spinSpeed":  # @1368228-1368314
            if not (steam_only or dry_only):
                by_type = _member(node, wash_table)
                by_code = _member(node, code_table)
                table = by_code if by_code is not _MISSING else by_type
                if not isinstance(table, Mapping):
                    raise _NoDuration
                total += _minutes(_member(table, _key(spin)))
        elif not (steam_only or dry_only):  # every option, @1368122-1368199
            by_type = _member(node, wash_table)
            by_code = _member(node, code_table)
            value = params.get(name, _MISSING)
            index = _key(value) if _string_is_numeric(value) else "0"
            if by_code is not _MISSING:
                entry = _member(by_code, index)
            elif by_type is None or by_type is _MISSING:
                entry = _MISSING
            else:
                entry = _member(by_type, index)
            total += _minutes(entry)
    if not has_dirt or not total > 0:
        raise _NoDuration  # @1368609-1368615
    return total


def actual_duration(
    appliance_type: str,
    params: Mapping[str, Any],
    ancillary: Mapping[str, Any],
    remaining_times: Any,
) -> float | None:
    """The programme's minutes as the app's builder gets them, or None for its null.

    `params`: the parameters the start sends (after the options and the dryTime rule);
    `ancillary`: the category's ancillary values as `ancillary_values` maps them;
    `remaining_times`: the category's raw `remainingTimes` group.

    `getWashDrySteamCyclesDuration` (@1368632-1368677): NaN, a total that is not a
    positive finite number, or any exception (its try/catch) is null; otherwise
    `moment.duration(x, 'minutes').asMinutes()`, milliseconds there and back.
    """
    try:
        total = _total_minutes(appliance_type, params, ancillary, remaining_times)
    except Exception:  # noqa: BLE001 - the app's try/catch, and _NoDuration's NaN
        return None
    if not math.isfinite(total) or not total > 0:
        return None
    return total * 60000 / 60000


def ancillary_values(nodes: Mapping[str, Any]) -> dict[str, Any]:
    """The category's ancillary schema as `mapCommandParameters` hands it to the
    duration (@1030332-1030440, called by the builder @1362222-1362234).

    fixed -> `fixedValue` (its `suggestedValue` when the fixed value is blank),
    enum / range -> `defaultValue`; any other node, and any value JavaScript reads as
    false ('' or 0, not '0'), leaves the key out. The duration reads programType,
    dryType and steamType only, so the app's cases for other keys (airWashPara,
    linkedSteamPrg, dryLevel / dryTime) are not reproduced.
    """
    values: dict[str, Any] = {}
    for name, node in nodes.items():
        if not isinstance(node, Mapping):
            continue
        typology = node.get("typology")
        if typology in ("enum", "range"):
            value = node.get("defaultValue")
        elif typology == "fixed":
            value = node.get("fixedValue")
            if not _truthy(value) and _truthy(node.get("suggestedValue")):
                value = node.get("suggestedValue")
        else:
            continue
        if _truthy(value):
            values[name] = value
    return values


def _last_enum_value(enum_values: Any) -> float:
    """`enumValues[length - 1]` as a number: the last value as the cloud ordered them,
    not the highest (a string's last character, as the app indexes it)."""
    if isinstance(enum_values, (list, str)) and enum_values:
        return _to_number(enum_values[-1])
    return math.nan


def energy_label(
    minutes: float | None,
    temp_node: Any,
    temp_contribution: Any,
    temp: Any,
) -> int:
    """The label the builder writes for a washer start, 0 standing for its '0'.

    `temp_node`: the category's `parameters.temp` schema node; `temp_contribution`:
    `ancillaryParameters.tempContribution.fixedValue`; `temp`: the temperature sent.

        temp not an enum, or a blank contribution                    -> 0
        floor((500 - 1.6*minutes + 5*tc*(lastT - temp)) / 100), clamped to 1..5
        no duration, or anything unreadable (NaN)                     -> 0

    The contribution is tested the JavaScript way: '0' passes and zeroes its term
    (@1362669-1362692). Same operations in the same order as `calculateEnergyLabel`
    (@1364133-1364169), so the float rounding agrees.
    """
    if not isinstance(temp_node, Mapping) or temp_node.get("typology") != "enum":
        return 0
    if not _truthy(temp_contribution):
        return 0
    last_temp = _last_enum_value(temp_node.get("enumValues"))
    sent = _to_number(temp if _truthy(temp) else 0)
    duration = math.nan if minutes is None else float(minutes)
    score = (
        500 - duration * 1.6 + 5 * _to_number(temp_contribution) * (last_temp - sent)
    ) / 100
    if math.isnan(score):
        return 0
    label = score if math.isinf(score) else math.floor(score)
    if label > 5:
        return 5
    if label <= 0:
        return 1
    return int(label)
