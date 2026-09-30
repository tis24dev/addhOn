# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Heat-pump water heater (appliance type `HW`, the app's "HPWH"): read and write rules.

Everything here is a pure function over the shadow attributes, rebuilt from the
official app 2.30.7 (`apk2/decomp.txt`; the analysis is
`apk/analysis/issue113-hw-hpwh-control-model.md`). Issue #113 is the first real
device: an HP110M8-9, `series: "m8"`.

The write side lives here too: when the app refuses a mode, temperature or boost
change (`mode_block`, `temperature_block`, `boost_block`) and the exact sparse
`CommandPatch` it sends for power, temperature, mode and boost. Sends are shaped by
the `HPWH` send profile in `send_profiles.py`, which keeps the app's own payloads
free of the fixed parameters the `setParameters` schema declares (22 mandatory ones:
every eco/silent window, both vacation dates, `operationName`).
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import datetime

from .command_dispatch import CommandPatch

# `HPWHMachMode` (decomp.txt:599199-599208): AUTO='1', ECO='2', ELEC='3', VAC='4'.
# The same codes are the `machMode` fixed by the startProgram programs
# auto/eco/elec/vac of the real device's schema.
HPWH_MODE_MAP: dict[str, str] = {
    "1": "auto",
    "2": "eco",
    "3": "electric",
    "4": "vacation",
}

# The app's own names for the same codes, as `modeType` spells them
# (decomp.txt:2333955-2333968): the dashboard builds `<HPWHMachMode key>_MODE`.
_AUTO, _ECO, _ELEC, _VAC = "AUTO_MODE", "ECO_MODE", "ELEC_MODE", "VAC_MODE"
_APP_MODES: dict[str, str] = {"1": _AUTO, "2": _ECO, "3": _ELEC, "4": _VAC}

# `getWaterPercentage` (decomp.txt:2355651): `remainingWaterLevel` is a 0-12 scale.
# The app answers 70 for any other value; here that is unknown, because 70 is not a
# reading.
_WATER_LEVEL_PERCENT: dict[int, int] = {
    0: 8, 1: 8, 2: 16, 3: 25, 4: 35, 5: 41, 6: 50,
    7: 58, 8: 65, 9: 75, 10: 83, 11: 91, 12: 100,
}

# `opp1EcoDaysValues` and `daysOfWeek` (decomp.txt:2333044-2333050): one hex byte per
# day, Monday first, paired with the JavaScript `getDay()` number of that day.
_ECO_DAY_BYTES = ("01", "02", "04", "08", "10", "20", "40")
_JS_DAYS = (1, 2, 3, 4, 5, 6, 0)

# Series whose eco/vacation logic takes other branches in the app (`isM7M8B`,
# `isM11`, decomp.txt:2327397-2327435). Only the plain branch is rebuilt below.
_OTHER_SERIES = frozenset({"m7b", "m8b", "m11"})

# The phases `heat_pump_state` can answer, in the app's order
# (`EnumHeatPumpWaterHeaterPhase`, decomp.txt:2329986-2330012). NOTCONNECTED is not
# among them: a disconnected appliance makes the entity unavailable instead.
HPWH_STATE_OFF = "off"
HPWH_STATE_WORKING = "working"
HPWH_STATE_KEEP_WARM = "keep_warm"
HPWH_STATE_STERILIZING = "sterilizing"
HPWH_STATE_SCHEDULED = "scheduled"
HPWH_STATE_ERROR = "error"
HPWH_STATE_REMOTE_CONTROL_OFF = "remote_control_off"
HPWH_STATES: tuple[str, ...] = (
    HPWH_STATE_OFF,
    HPWH_STATE_WORKING,
    HPWH_STATE_KEEP_WARM,
    HPWH_STATE_STERILIZING,
    HPWH_STATE_SCHEDULED,
    HPWH_STATE_ERROR,
    HPWH_STATE_REMOTE_CONTROL_OFF,
)

_WINDOW_KEYS = tuple(
    f"opp{period}Eco{edge}Time{slot}"
    for period in (1, 2)
    for slot in (1, 2, 3)
    for edge in ("Start", "End")
)

# Every attribute `heat_pump_state` reads. The diagnostics coverage and source index
# are built from this tuple, so the two cannot drift from the code below.
HPWH_STATE_ATTRS: tuple[str, ...] = (
    "onOffStatus",
    "machMode",
    "temp",
    "tempSel",
    "sterilizationCurrentStatus",
    "errors",
    "error",
    "remoteCtrValid",
    "offPeakPeriodScheme",
    "opp1EcoDays",
    *_WINDOW_KEYS,
)


def code(raw: object) -> str | None:
    """A shadow value as the app sees it: the string of `parNewVal`.

    Our engine hands numbers back as int or float; an integral float is folded to its
    integer spelling so that `1.0` compares like the app's `'1'`.
    """
    if raw is None:
        return None
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return str(raw).strip()


def _js_number(raw: object) -> float:
    """JavaScript `Number(x || 0)` for the values these helpers read.

    `Number('7F')` is NaN in the app, and that matters: `getScheduleTime` only gives up
    on `Number(opp1EcoDays) < 1`, which NaN never is.
    """
    text = code(raw)
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return math.nan


def mode_key(raw: object) -> str | None:
    """`machMode` as its `HPWH_MODE_MAP` key; an integral float (1.0) reads as "1"."""
    return HPWH_MODE_MAP.get(code(raw) or "")


def water_level_percent(raw: object) -> int | None:
    """`remainingWaterLevel` as the percentage the app shows (10 -> 83).

    The app calls `Number(remainingWaterLevel)` without the `|| 0` guard, so a
    missing level is NaN there and lands on its 70 fallback: unknown here.
    """
    text = code(raw)
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    if not value.is_integer():
        return None
    return _WATER_LEVEL_PERCENT.get(int(value))


def _hex_to_days(mask: object) -> tuple[int, int] | None:
    """`hexToDays` (decomp.txt:2333056): the contiguous day range a mask encodes.

    A single day is matched on its exact two-character spelling; a range is found by
    summing consecutive day bytes from Monday until the sum equals the mask. Anything
    else (an empty mask, a non-contiguous set, an unpadded single day like "1") yields
    no range, exactly as in the app.
    """
    text = code(mask)
    if text is None:
        return None
    if text in _ECO_DAY_BYTES:
        day = _JS_DAYS[_ECO_DAY_BYTES.index(text)]
        return day, day
    try:
        target = int(text, 16)
    except ValueError:
        return None
    for first in range(len(_ECO_DAY_BYTES)):
        total = int(_ECO_DAY_BYTES[first], 16)
        for last in range(first + 1, len(_ECO_DAY_BYTES)):
            total += int(_ECO_DAY_BYTES[last], 16)
            if total == target:
                return _JS_DAYS[first], _JS_DAYS[last]
    return None


def _js_day(now: datetime) -> int:
    """JavaScript `Date.getDay()`: Sunday is 0."""
    return (now.weekday() + 1) % 7


def schedule_window(
    get: Callable[[str], object], now: datetime, series: str | None
) -> tuple[str, str] | None:
    """`getScheduleTime(..., true, ...)` (decomp.txt:2327831-2328258), plain series.

    The current or next eco window as `(start, end)` strings, or None. Windows are
    compared as "HH:MM" STRINGS, as the app does; a "00:00"-"00:00" slot is never
    current or upcoming, which is how the app skips an empty one.

    Returns None for the M7B/M8B/M11 series, whose branches are not rebuilt;
    `heat_pump_state` then answers unknown wherever the schedule would matter.
    """
    if series in _OTHER_SERIES:
        return None
    if _js_number(get("opp1EcoDays")) < 1:
        return None
    clock = now.strftime("%H:%M")

    def window(period: int, slot: int) -> tuple[str, str]:
        start = code(get(f"opp{period}EcoStartTime{slot}")) or ""
        end = code(get(f"opp{period}EcoEndTime{slot}")) or ""
        return start, end

    day = _js_day(now)
    days = _hex_to_days(get("opp1EcoDays"))
    if days is None:
        in_days = False
    elif days[0] <= days[1]:
        in_days = days[0] <= day <= days[1]
    else:
        in_days = day >= days[0] or day <= days[1]

    scheme = _js_number(get("offPeakPeriodScheme"))
    # `offPeakPeriodScheme` 1 = the same windows every day (EcoTiming.SAME,
    # decomp.txt:1051274-1051278): only the opp2 windows count. 0 = different windows:
    # opp2 on the days of `opp1EcoDays`, opp1 on the others.
    use_opp2 = scheme == 1 or (scheme == 0 and in_days)
    if use_opp2:
        for slot in (1, 2, 3):
            start, end = window(2, slot)
            if clock < start or clock < end:
                return start, end
        if scheme == 1:
            return None
        # With scheme 0, once today's opp2 windows are over the app goes on to the
        # opp1 windows, which belong to the OTHER days (decomp.txt:2328007-2328015:
        # only scheme 1 returns null there). Kept as the app does it: the state
        # this feeds has to match what the app shows, not what the schedule means.
    for slot in (1, 2):
        start, end = window(1, slot)
        if clock < start or clock < end:
            return start, end
    # The app checks only the START of the third opp1 window (decomp.txt:2328027):
    # a third window already under way is not returned.
    start, end = window(1, 3)
    if clock < start:
        return start, end
    return None


def heat_pump_state(
    get: Callable[[str], object], now: datetime, series: str | None
) -> str | None:
    """`getActiveStatus` for `ApplianceType.HW` (decomp.txt:2328690-2328900).

    `get` reads one shadow attribute by name. Differs from the app in two places:

    - `tempSel < temp` is compared as numbers, where the app compares the two shadow
      strings. Both give the same answer for every two-digit temperature, which is
      the whole settable range (35-75).
    - On the M7B/M8B/M11 series, whose schedule branches are not rebuilt, the answer
      is None (unknown) unless the mode is AUTO or ELEC, the only two for which the
      app never consults the schedule. Guessing there would publish "working" for an
      appliance the app may be showing as scheduled.
    """
    # `getErrorCode` (decomp.txt:1353141): `errors`, or `error` when that is empty.
    errors = get("errors")
    if not code(errors):
        errors = get("error")
    if _js_number(errors) > 0:
        return HPWH_STATE_ERROR
    if code(get("remoteCtrValid")) == "0":
        return HPWH_STATE_REMOTE_CONTROL_OFF

    mode = _APP_MODES.get(code(get("machMode")) or "")
    if series in _OTHER_SERIES and mode not in (_AUTO, _ELEC):
        return None
    window = schedule_window(get, now, series)
    # `getActiveValue` (decomp.txt:2328360-2328510), called with vacation = inactive.
    if mode in (_AUTO, _ELEC):
        active = True
    elif mode == _ECO:
        clock = now.strftime("%H:%M")
        active = window is None or window[0] <= clock <= window[1]
    else:
        active = False
    scheduled = not active and window is not None and bool(window[0])

    if code(get("onOffStatus")) != "1" and not scheduled:
        return HPWH_STATE_OFF
    if _js_number(get("machMode")) > 0 and active:
        target = _js_number(get("tempSel"))
        current = _js_number(get("temp"))
        return HPWH_STATE_KEEP_WARM if target < current else HPWH_STATE_WORKING
    if code(get("sterilizationCurrentStatus")) == "1":
        return HPWH_STATE_STERILIZING
    if scheduled:
        return HPWH_STATE_SCHEDULED
    return HPWH_STATE_WORKING


HPWH_SETTINGS_COMMAND = "settings"
HPWH_START_COMMAND = "startProgram"
# Home Assistant operation mode -> startProgram category (D1). The app's
# ModeSettings offers exactly these three (apk2 decomp.txt:4498519-4498529).
HPWH_MODE_CATEGORIES: dict[str, str] = {
    "heat_pump": "auto",
    "eco": "eco",
    "electric": "elec",
}
_VAC_CLEARED = ("", "2000-01-01")


def _disabled(get: Callable[[str], object]) -> bool:
    errors = get("errors")
    if not code(errors):
        errors = get("error")
    return _js_number(errors) > 0 or code(get("remoteCtrValid")) == "0"


def _is_on(get: Callable[[str], object]) -> bool:
    return code(get("onOffStatus")) == "1"


def is_vacation_active(get: Callable[[str], object]) -> bool:
    """`isVacModeActive` for the plain series (apk2 decomp.txt:2334140-2334200)."""
    if code(get("machMode")) != "4":
        return False
    return all(
        (code(get(name)) or "") not in _VAC_CLEARED
        for name in ("vacStartDate", "vacEndDate")
    )


def _sterilizing(get: Callable[[str], object]) -> bool:
    return code(get("sterilizationCurrentStatus")) == "1"


def mode_block(get: Callable[[str], object]) -> str | None:
    """Why the app would refuse a mode change, or None (onPressAutoMode)."""
    if _disabled(get):
        return "hpwh_unavailable"
    if not _is_on(get):
        return "hpwh_switch_on_first"
    if is_vacation_active(get):
        return "hpwh_vacation_active"
    if _sterilizing(get):
        return "hpwh_sterilization_running"
    return None


def temperature_block(get: Callable[[str], object]) -> str | None:
    """Why the app would refuse a new target, or None (onPressTempMode)."""
    if is_vacation_active(get):
        return "hpwh_vacation_active"
    if _disabled(get):
        return "hpwh_unavailable"
    if not _is_on(get):
        return "hpwh_switch_on_first"
    if _sterilizing(get):
        return "hpwh_sterilization_running"
    return None


def boost_block(get: Callable[[str], object], turning_on: bool) -> str | None:
    """Why the app would refuse the boost toggle, or None (OptionSettings)."""
    if _disabled(get):
        return "hpwh_unavailable"
    if not _is_on(get):
        return "hpwh_switch_on_first"
    if is_vacation_active(get):
        return "hpwh_vacation_active"
    if turning_on and _js_number(get("temp")) >= _js_number(get("tempSel")):
        return "hpwh_boost_at_target"
    return None


def power_patch(on: bool) -> CommandPatch:
    return CommandPatch(
        HPWH_SETTINGS_COMMAND,
        {"onOffStatus": "1" if on else "0"},
        action="power_on" if on else "power_off",
    )


def temperature_patch(get: Callable[[str], object], value: int) -> CommandPatch:
    values: dict[str, str] = {"tempSel": str(int(value))}
    if code(get("boostStatus")) == "1" and value < _js_number(get("temp")):
        values["boostStatus"] = "0"
    return CommandPatch(HPWH_SETTINGS_COMMAND, values, action="set_temperature")


def mode_patch(category: str, machmode: str) -> CommandPatch:
    """`startProgram {machMode}` with no `programName`, as ModeSettings sends it.

    The category is selected in `prepare` so that `machMode` lands on the category it
    belongs to: writing it on whichever category happened to be active would
    overwrite that category's own fixed value in memory.
    """

    def _select(parameters: dict) -> None:
        parameters["program"].value = category

    return CommandPatch(
        HPWH_START_COMMAND,
        {"machMode": machmode},
        action=f"set_mode_{category}",
        prepare=_select,
        program_name="",
    )


def boost_patch(on: bool) -> CommandPatch:
    return CommandPatch(
        HPWH_SETTINGS_COMMAND,
        {"boostStatus": "1" if on else "0"},
        action="boost_on" if on else "boost_off",
    )


def boost_auto_off_due(get: Callable[[str], object]) -> bool:
    """The app's dashboard switches boost off at the target (D7: numeric equality)."""
    return (
        code(get("boostStatus")) == "1"
        and _js_number(get("temp")) == _js_number(get("tempSel"))
    )


def mapping_getter(attributes: Mapping[str, object]) -> Callable[[str], object]:
    """A `get` for the helpers above over a plain attribute mapping (tests, dumps)."""
    return attributes.get
