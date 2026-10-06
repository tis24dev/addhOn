# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Heat-pump water heater (appliance type `HW`, the app's "HPWH"): read and write rules.

The rules are pure functions over the shadow attributes (only `raise_refusal` has an
effect: it turns a refusal into the localized `HomeAssistantError`), rebuilt from the
official app 2.30.7 (`apk2/decomp.txt`; the analysis is
`apk2/analysis/issue113-hw-hpwh-control-model.md`). Issue #113 is the first real
device: an HP110M8-9, `series: "m8"`.

The write side lives here too: when the app refuses a mode, temperature or boost
change (`mode_block`, `temperature_block`, `boost_block`) and the exact sparse
`CommandPatch` it sends for power, temperature, mode and boost. Sends are shaped by
the `HPWH` send profile in `send_profiles.py`, which keeps the app's own payloads
free of the fixed parameters the `setParameters` schema declares (22 mandatory ones:
every eco/silent window, both vacation dates, `operationName`).

Block 5 adds the scheduling writes the app makes on the plain m7/m8 series: the
vacation dates, the sterilization settings and the eco windows (`vacation_patch`,
`sterilization_write`, `eco_schedule_steps`), each with its own refusals.
"""
from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from datetime import time as dt_time

from homeassistant.exceptions import HomeAssistantError

from .command_dispatch import CommandPatch
from .const import DOMAIN
from .hon_commands import param_range

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


def controls_supported(series: object) -> bool:
    """Whether the controls (water heater, boost switch, boost auto-off) apply.

    `series` is `model_attributes["series"]` as the appliance publishes it; it is
    read like `HonHeatPumpStateSensor._series` does (stripped, lower-cased). The
    M7B/M8B/M11 series are excluded: the app takes other branches there (vacation by
    `vacModeDays`, a different power-off in vacation) and none of them is rebuilt.
    A missing or unknown series is not excluded.
    """
    normalized = str(series).strip().lower() if series else None
    return normalized not in _OTHER_SERIES


def appliance_series(appliance) -> object:
    """`model_attributes["series"]` as published; `controls_supported` normalizes it."""
    attributes = getattr(appliance, "model_attributes", None)
    return attributes.get("series") if isinstance(attributes, Mapping) else None


def series_key(series: object) -> str | None:
    """`series` stripped and lower-cased, the spelling `_OTHER_SERIES` is written in."""
    return str(series).strip().lower() if series else None


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

# Every attribute the eco-window sensor reads: `schedule_window` for its state,
# `eco_window_attributes` for the rest. Feeds the diagnostics like the tuple above.
HPWH_ECO_WINDOW_ATTRS: tuple[str, ...] = (
    "offPeakPeriodScheme",
    "opp1EcoDays",
    *_WINDOW_KEYS,
)

# Every attribute `vacation_active` reads, for the same diagnostics.
HPWH_VACATION_ATTRS: tuple[str, ...] = ("machMode", "vacStartDate", "vacEndDate")

# The days of `opp1EcoDays`, one bit each from Monday (0x01) to Sunday (0x40), in
# Home Assistant's own spelling (`homeassistant.const.WEEKDAYS`, the `weekday` of a
# time condition), so an automation can compare them as they are.
_ECO_DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# A window edge that does not set a window: the app's empty slot is "00:00"-"00:00".
_EMPTY_EDGES = ("", "00:00")
_WINDOW_DASH = "\u2013"


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

    Differs from the app after the day's last window with the same windows every
    day (`offPeakPeriodScheme` 1): the app gives up there (null), this returns the
    first window of tomorrow. See `heat_pump_state` for what that changes.

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
            # Today's windows are over: the app returns null (decomp.txt:2328010,
            # 2328062). The next window is tomorrow's first instead, the set one
            # with the earliest start, as the isM11 branch picks it
            # (decomp.txt:2328197-2328213). A slot without an end is not a window.
            windows = [window(2, slot) for slot in (1, 2, 3)]
            windows = [w for w in windows if w[1] and w[1] != "00:00"]
            # Its end minute still belongs to the window that ends now: the
            # comparison above is strict, `heat_pump_state` reads the end as
            # inside (PR #119 review).
            ending = [w for w in windows if w[1] == clock]
            if ending:
                return ending[0]
            return min(windows, key=lambda w: w[0], default=None)
        # With scheme 0, once today's opp2 windows are over the app goes on to the
        # opp1 windows, which belong to the OTHER days (decomp.txt:2328007-2328015:
        # only scheme 1 returns null there). Kept as the app does it, null at the
        # end included: tomorrow may take the other set of windows, and no branch
        # of the app says which one to show.
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


def eco_window_text(
    get: Callable[[str], object], now: datetime, series: object
) -> str | None:
    """The window `schedule_window` finds, as "HH:MM" en dash "HH:MM", or None.

    The same current-or-next window `heat_pump_state` works with, so the two
    sensors cannot disagree; None wherever that search gives up, the M7B/M8B/M11
    series included. The edges are the shadow strings as published.
    """
    window = schedule_window(get, now, series_key(series))
    if window is None or not window[0] or not window[1]:
        return None
    return f"{window[0]}{_WINDOW_DASH}{window[1]}"


def eco_days(mask: object) -> list[str] | None:
    """`opp1EcoDays` as the days it sets, Monday first; None when it is no mask.

    Read bit by bit (`opp1EcoDaysValues`, decomp.txt:2333044-2333050), not through
    `_hex_to_days`: this lists what the appliance stores, where `hexToDays` answers
    only for a contiguous range spelled the way the app's read path expects.
    """
    text = code(mask)
    if not text:
        return None
    try:
        value = int(text, 16)
    except ValueError:
        return None
    if not 0 <= value <= 0x7F:
        return None
    return [name for bit, name in enumerate(_ECO_DAY_NAMES) if value & (1 << bit)]


def eco_window_attributes(get: Callable[[str], object]) -> dict[str, object]:
    """The whole eco schedule, as the eco-window sensor publishes it beside its state.

    Both groups, slot 1 to 3, skipping the empty "00:00"-"00:00" slots; the scheme
    (`offPeakPeriodScheme`: 1 = the opp2 windows every day, 0 = opp2 on the days of
    `opp1EcoDays` and opp1 on the others, see `schedule_window`) as its code; the
    days decoded by `eco_days`. Raw readings, whatever the series.
    """

    def windows(period: int) -> list[str]:
        found = []
        for slot in (1, 2, 3):
            start = code(get(f"opp{period}EcoStartTime{slot}")) or ""
            end = code(get(f"opp{period}EcoEndTime{slot}")) or ""
            if start in _EMPTY_EDGES and end in _EMPTY_EDGES:
                continue
            found.append(f"{start}{_WINDOW_DASH}{end}")
        return found

    return {
        "opp1_windows": windows(1),
        "opp2_windows": windows(2),
        "off_peak_period_scheme": code(get("offPeakPeriodScheme")) or None,
        "opp1_eco_days": eco_days(get("opp1EcoDays")),
    }


def _clock(raw: object) -> tuple[int, int] | None:
    """An "H:M" shadow string as (hour, minute), padded or not; None otherwise."""
    text = code(raw)
    if not text:
        return None
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 23 or minutes > 59:
        return None
    return hours, minutes


def sterilization_clock(raw: object) -> dt_time | None:
    """`sterilizationTime` as a time of day; the app writes it unpadded ("15:0").

    Built as `String(hours + ':' + minutes)` (apk2 decomp.txt:4592323-4592332), and
    read back by `split(':')` (4591498), so both spellings are read here.
    Anything that is not an hour and a minute is None. "00:00" stays midnight: on
    the #113 appliance it may also mean never set, and nothing in the shadow tells
    the two apart.
    """
    clock = _clock(raw)
    return None if clock is None else dt_time(*clock)


def clock_text(value: dt_time) -> str:
    """A time of day as the app writes `sterilizationTime`: "15:0", "3:30"."""
    return f"{value.hour}:{value.minute}"


def heat_pump_state(
    get: Callable[[str], object], now: datetime, series: str | None
) -> str | None:
    """`getActiveStatus` for `ApplianceType.HW` (decomp.txt:2328690-2328900).

    `get` reads one shadow attribute by name. Differs from the app in three places:

    - Eco, from the end of the day's last window to midnight (same windows every
      day): the app just opened shows WORKING (OFF when switched off), because its
      window search gives up (decomp.txt:2328062) and no window reads as active
      (2328426-2328428). Here the next window is tomorrow's first, so the answer
      is SCHEDULED, switched on or off. That is what the app shows when its screen
      was opened before the window ended: the m8 dashboard searches the window
      with the time it was opened at (2325370-2325374, 2325443-2325446).
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


def vacation_active(get: Callable[[str], object], series: object) -> bool:
    """`isVacModeActive` with both of its branches (apk2 decomp.txt:2334140-2334188).

    On the M7B/M8B/M11 series (`isM11` / `isM7M8B` set) the app reads `machMode` 4
    alone: their vacation runs on `vacModeDays`, not on the two dates. Elsewhere it is
    `is_vacation_active`. `series` is read like `controls_supported` reads it.
    """
    if series_key(series) in _OTHER_SERIES:
        return code(get("machMode")) == "4"
    return is_vacation_active(get)


def vacation_date(raw: object) -> date | None:
    """`vacStartDate` / `vacEndDate` as a date; None when cleared or not a date.

    The app clears them to '' or '2000-01-01' (`isVacModeActive`); the #113
    appliance publishes "0000-00-00", which is no calendar date either.
    """
    text = code(raw)
    if not text or text in _VAC_CLEARED:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


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


def raise_refusal(reason: str | None) -> None:
    """Raise the localized error for a refusal the rules above named; None passes.

    Shared by the water heater, the boost switch and the scheduling writes (vacation,
    sterilization, eco windows). One literal raise per key:
    `test_translations` verifies raised keys against the JSON and refuses a computed
    `translation_key`.
    """
    if reason is None:
        return
    if reason == "hpwh_unavailable":
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_unavailable"
        )
    if reason == "hpwh_switch_on_first":
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_switch_on_first"
        )
    if reason == "hpwh_vacation_active":
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_vacation_active"
        )
    if reason == "hpwh_sterilization_running":
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_sterilization_running"
        )
    if reason == "hpwh_boost_at_target":
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_boost_at_target"
        )
    if reason == "hpwh_write_not_supported":
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_write_not_supported"
        )
    if reason == "hpwh_vacation_dates_order":
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_vacation_dates_order"
        )
    if reason == "hpwh_sterilization_unreadable":
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_sterilization_unreadable"
        )
    if reason == "hpwh_eco_first":
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_eco_first"
        )
    raise ValueError(f"Unhandled heat-pump water heater refusal: {reason}")


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
    """The app's dashboard switches boost off at the target (D7: numeric equality).

    Only the condition. When it may send again (once per episode) is decided by
    `water_heater._BoostAutoOff`, which owns the episode memory.
    """
    return (
        code(get("boostStatus")) == "1"
        and _js_number(get("temp")) == _js_number(get("tempSel"))
    )


# --- Scheduling writes: vacation, sterilization, eco windows (block 5) ---------------
# User decisions of 2026-10-06: only with the experimental option (the entities
# check it), only on the plain m7/m8 series, always through the `HPWH` send profile.
# The M7B/M8B/M11 branches of the app write the vacation as `vacModeDays`
# (`sendVacModePeriod`, apk2 decomp.txt:2334560-2334600) and the eco windows through
# a builder of their own (4504980-4504996), none of them rebuilt here; a missing or
# unknown series, which the water heater accepts, is not taken either.
_PLAIN_SERIES = frozenset({"m7", "m8"})


def settings_parameters(appliance) -> Mapping[str, object]:
    """The `settings` command's parameters (the active category's), or {}."""
    commands = getattr(appliance, "commands", None)
    settings = commands.get(HPWH_SETTINGS_COMMAND) if isinstance(commands, Mapping) else None
    parameters = getattr(settings, "parameters", None)
    return parameters if isinstance(parameters, Mapping) else {}


def schedule_writes_supported(appliance, keys: Collection[str]) -> bool:
    """A plain m7/m8 appliance whose `settings` schema declares every key of a write.

    The dispatcher refuses a key the schema lacks; checked here first so the user
    gets the translated `hpwh_write_not_supported` instead.
    """
    if series_key(appliance_series(appliance)) not in _PLAIN_SERIES:
        return False
    parameters = settings_parameters(appliance)
    return all(key in parameters for key in keys)


# Coordinator store (`HonBaseEntity._coordinator_store`) holding one lock per
# appliance for its schedule writes: {appliance_id: asyncio.Lock}.
HPWH_SCHEDULE_LOCK_STORE = "hpwh_schedule_locks"


def schedule_lock(store: dict, appliance_id: str) -> asyncio.Lock:
    """The lock every schedule write of one appliance holds (PR #121 review).

    The sterilization four, the vacation dates, the clear button and the eco chain
    each build what they send from the shadow, and the dispatcher's own lock covers
    one send, not that read: two writes at once could each build on a value the
    other is changing, and the second undid the first. Held from the read to the
    send (the whole chain for the eco windows), a write waits its turn and then
    reads the shadow the previous one left (decision of 2026-10-06).
    """
    lock = store.get(appliance_id)
    if lock is None:
        lock = store[appliance_id] = asyncio.Lock()
    return lock


# Vacation, `sendVacModeDate` (apk2 decomp.txt:2334500-2334545): `setParameters`
# with both dates as 'YYYY-MM-DD' and the operation name, always all three, in this
# order. Cleared with both dates at `turnOffDate` (2334636-2334637).
HPWH_VACATION_KEYS: tuple[str, ...] = ("vacStartDate", "vacEndDate", "operationName")
_VACATION_OPERATION = "grSetVacDate"
_VACATION_OFF = "2000-01-01"


def vacation_block(get: Callable[[str], object]) -> str | None:
    """Why a vacation write is refused, or None: only an appliance taking no command.

    Not refused when switched off or with a vacation under way: setting or ending
    the dates is what those states are for.
    """
    return "hpwh_unavailable" if _disabled(get) else None


def vacation_range(
    get: Callable[[str], object], *, start: date | None = None, end: date | None = None
) -> tuple[date, date]:
    """The (start, end) to send when ONE date is written (`start` or `end`).

    The other half comes from the shadow; when it is missing (cleared '' or
    '2000-01-01', the #113 placeholder '0000-00-00', anything unreadable: see
    `vacation_date`) the vacation is one day long, the shortest the app allows.
    """
    try:
        if start is not None:
            other = vacation_date(get("vacEndDate"))
            return start, other if other is not None else start + timedelta(days=1)
        if end is None:
            raise ValueError("vacation_range needs start or end")
        other = vacation_date(get("vacStartDate"))
        return (other if other is not None else end - timedelta(days=1)), end
    except OverflowError:
        # date.min / date.max: no day on the other side, so no valid range.
        day = start if start is not None else end
        return day, day


def vacation_order_block(start: date, end: date) -> str | None:
    """The app's screen refuses an end before the start AND on the same day
    (`END_DATE_MUST_BE_AFTER_TOAST`, `START_END_DATE_MUST_BE_DIFFERENT_TOAST`,
    apk2 decomp.txt:4490504-4490540)."""
    return "hpwh_vacation_dates_order" if end <= start else None


def vacation_patch(start: date, end: date) -> CommandPatch:
    return CommandPatch(
        HPWH_SETTINGS_COMMAND,
        {
            "vacStartDate": start.isoformat(),
            "vacEndDate": end.isoformat(),
            "operationName": _VACATION_OPERATION,
        },
        action="set_vacation",
    )


def vacation_clear_patch() -> CommandPatch:
    return CommandPatch(
        HPWH_SETTINGS_COMMAND,
        {
            "vacStartDate": _VACATION_OFF,
            "vacEndDate": _VACATION_OFF,
            "operationName": _VACATION_OPERATION,
        },
        action="clear_vacation",
    )


# Sterilization: `setParameters` with always these four keys, in this order, and no
# operation name (apk2 decomp.txt:4592313-4592337). The app sends what its screen
# holds; here the three not being changed come from the shadow.
HPWH_STERILIZATION_KEYS: tuple[str, ...] = (
    "sterilizationStatus",
    "sterilizationInterval",
    "sterilizationTime",
    "sterilizationTempSel",
)
# The app's picker steps 15 minutes (`minuteIncrementToArrayValues(15)`, apk2
# decomp.txt:4591951-4591958; only '00' on M11, not rebuilt).
_STERILIZATION_MINUTES = 15


def sterilization_block(get: Callable[[str], object]) -> str | None:
    """Why a sterilization write is refused, or None.

    An appliance taking no command; and a vacation, during which the app announces
    the sterilization as disabled (`STERILIZATION_DISABLED_TEMPORARLY`,
    'HPWH.VAC_MODE.STERILIZATION_DISABLED', apk2 decomp.txt:2324933-2324955;
    inferred from those labels, the screen's own guard is not traced).
    """
    if _disabled(get):
        return "hpwh_unavailable"
    if is_vacation_active(get):
        return "hpwh_vacation_active"
    return None


def _whole(raw: object) -> int | None:
    """A finite number's integer part (`split('.')[0]`), or None."""
    text = code(raw)
    try:
        value = float(text) if text else math.nan
    except ValueError:
        return None
    return int(value) if math.isfinite(value) else None


def sterilization_value(key: str, raw: object) -> str | None:
    """One of the four as the app sends it back unchanged; None when unreadable.

    The status "0"/"1"; the interval as a whole number (1 once, 2 weekly, 3 monthly,
    apk2 decomp.txt:4593677-4593745); the hour unpadded (`clock_text`); the
    temperature cut to its integer part, as the app's screen does
    (`sterilizationTempSel.split('.')`, 4591455-4591462): 62.2 on #113 goes back 62.
    """
    if key == "sterilizationStatus":
        status = code(raw)
        return status if status in ("0", "1") else None
    if key == "sterilizationTime":
        clock = sterilization_clock(raw)
        return None if clock is None else clock_text(clock)
    if key == "sterilizationInterval":
        whole = _whole(raw)
        text = code(raw)
        if whole is None or text is None or float(text) != whole:
            return None
        return str(whole)
    if key == "sterilizationTempSel":
        whole = _whole(raw)
        return None if whole is None else str(whole)
    raise ValueError(f"Not a sterilization key: {key}")


def _off_range(text: str, parameter: object) -> str | None:
    """The allowed range as text when `text` is off the parameter's grid; else None.

    A parameter without a range (the fixed `sterilizationTime`) takes anything.
    """
    bounds = param_range(parameter)
    if bounds is None:
        return None
    low, high, step = bounds
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if math.isfinite(value) and low <= value <= high:
        steps = (value - low) / step
        if abs(steps - round(steps)) < 1e-9:
            return None
    return f"{low:g}-{high:g}, step {step:g}"


_STERILIZATION_ACTIONS: dict[str, str] = {
    "sterilizationInterval": "set_sterilization_interval",
    "sterilizationTime": "set_sterilization_time",
    "sterilizationTempSel": "set_sterilization_temperature",
}


def sterilization_write(
    get: Callable[[str], object],
    parameters: Mapping[str, object],
    key: str,
    value: str,
) -> CommandPatch:
    """The four-key patch that changes `key` to `value`, or a translated refusal.

    In order: the new value against the schema (`invalid_setpoint`), the refusals
    (`sterilization_block`), then the other three, read from the shadow: one that
    cannot be read, or that the schema would refuse, is `hpwh_sterilization_unreadable`
    rather than a guess or a send the dispatcher would reject.
    """
    allowed = _off_range(value, parameters.get(key))
    if allowed is not None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="invalid_setpoint",
            translation_placeholders={"value": value, "allowed": allowed},
        )
    raise_refusal(sterilization_block(get))
    values: dict[str, str] = {}
    for name in HPWH_STERILIZATION_KEYS:
        text = value if name == key else sterilization_value(name, get(name))
        if text is None or (
            name != key and _off_range(text, parameters.get(name)) is not None
        ):
            raise_refusal("hpwh_sterilization_unreadable")
        values[name] = text
    if key == "sterilizationStatus":
        action = "sterilization_on" if value == "1" else "sterilization_off"
    else:
        action = _STERILIZATION_ACTIONS[key]
    return CommandPatch(HPWH_SETTINGS_COMMAND, values, action=action)


def sterilization_clock_value(value: dt_time) -> str:
    """A new sterilization hour as the app writes it; off its 15-minute grid refused.

    Seconds are dropped: the app's picker has none.
    """
    if value.minute % _STERILIZATION_MINUTES:
        asked = f"{value.hour:02d}:{value.minute:02d}"
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="invalid_setpoint",
            translation_placeholders={"value": asked, "allowed": "minutes 00, 15, 30, 45"},
        )
    return clock_text(value)


# Eco windows, the app's chain (`retrieveMqtt`, apk2 decomp.txt:1527781-1527905; the
# same steps when Eco is already on, 4506304-4506359): the scheme alone, with no
# operation name; then, with DIFFERENT only, the day mask with 'grSetWeekGroup';
# then the twelve windows with 'grSetEcoTime' (builder 4505046-4505653: opp1 then
# opp2, slots 1 to 3, the unused ones '00:00'). The app writes them only in Eco.
HPWH_ECO_SCHEDULE_KEYS: tuple[str, ...] = (
    "offPeakPeriodScheme",
    "opp1EcoDays",
    *_WINDOW_KEYS,
    "operationName",
)
# `EcoTiming` (apk2 decomp.txt:1051273-1051277): SAME = 1, DIFFERENT = 0.
HPWH_ECO_SCHEMES: dict[str, str] = {"same": "1", "different": "0"}
# The day names the service takes, Monday first: the bits of `opp1EcoDays`.
HPWH_ECO_DAY_NAMES: tuple[str, ...] = _ECO_DAY_NAMES
_ECO_MAX_WINDOWS = 3
_ECO_WINDOW = re.compile(
    r"\s*(\d{1,2}):(\d{2})\s*[-" + _WINDOW_DASH + r"]\s*(\d{1,2}):(\d{2})\s*"
)
_ECO_MINUTES = (0, 15, 30, 45)  # `minutesData` (apk2 decomp.txt:1051318-1051326)
_ECO_EMPTY = "00:00"


def eco_block(get: Callable[[str], object]) -> str | None:
    """Why the eco windows cannot be written, or None.

    The mode-change refusals (the app writes the windows from its mode screen,
    `onPressAutoMode`), then Eco itself: the app sends them only in the Eco flow.
    """
    reason = mode_block(get)
    if reason is not None:
        return reason
    if code(get("machMode")) != "2":
        return "hpwh_eco_first"
    return None


def _eco_windows(raw: object) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Up to three "HH:MM-HH:MM" windows, in time order, as the app's screen allows.

    On its 15-minute grid, ending after they start, not overlapping
    (`validateNewTimeValue`, apk2 decomp.txt:1051318-1051420). An en dash is a dash:
    the eco-window sensor publishes one.
    """
    items = list(raw or ())
    if len(items) > _ECO_MAX_WINDOWS:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_eco_too_many_windows"
        )
    windows: list[tuple[tuple[int, int], tuple[int, int], str]] = []
    for item in items:
        text = item if isinstance(item, str) else ""
        match = _ECO_WINDOW.fullmatch(text)
        edges = None
        if match:
            hours = (int(match.group(1)), int(match.group(3)))
            minutes = (int(match.group(2)), int(match.group(4)))
            if all(h <= 23 for h in hours) and all(m in _ECO_MINUTES for m in minutes):
                edges = (hours[0], minutes[0]), (hours[1], minutes[1])
        if edges is None or edges[1] <= edges[0]:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="hpwh_eco_window_invalid",
                translation_placeholders={"window": str(item)},
            )
        windows.append((*edges, str(item)))
    windows.sort()
    for previous, current in zip(windows, windows[1:]):
        if current[0] < previous[1]:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="hpwh_eco_window_invalid",
                translation_placeholders={"window": current[2]},
            )
    return [(start, end) for start, end, _text in windows]


def _eco_days_mask(days: object) -> str:
    """`opp1EcoDays` as the app writes it: the sum of a run of days, lower-case hex.

    The run goes from the first chosen day to the last, Monday to Sunday, never
    across Sunday (the app sums `weekDaysValue` by index, apk2
    decomp.txt:4505200-4505250, and reads back only such runs, `hexToDays`).
    """
    names = list(days or ())
    if not names or not all(name in HPWH_ECO_DAY_NAMES for name in names):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_eco_days_invalid"
        )
    indexes = sorted({HPWH_ECO_DAY_NAMES.index(name) for name in names})
    if indexes[-1] - indexes[0] + 1 != len(indexes):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_eco_days_invalid"
        )
    return format(sum(1 << index for index in indexes), "x")


def eco_schedule_steps(
    scheme: str,
    days: object = None,
    windows: object = None,
    other_windows: object = None,
) -> tuple[CommandPatch, ...]:
    """The app's chain for one schedule, step by step, or a translated refusal.

    `windows` is the app's "period 1": every day with `same`, the chosen `days` with
    `different`; it goes in the opp2 keys. `other_windows` is "period 2", the other
    days, in the opp1 keys (builder, apk2 decomp.txt:4505300-4505400). Each set is
    written in time order; with `same` the opp1 keys are all '00:00', as the builder
    leaves them.
    """
    if scheme not in HPWH_ECO_SCHEMES:
        raise ValueError(f"Unknown eco scheme: {scheme!r}")
    different = scheme == "different"
    if not different and (days or other_windows):
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="hpwh_eco_different_only"
        )
    mask = _eco_days_mask(days) if different else None
    first = _eco_windows(windows)
    other = _eco_windows(other_windows) if different else []

    steps = [
        CommandPatch(
            HPWH_SETTINGS_COMMAND,
            {"offPeakPeriodScheme": HPWH_ECO_SCHEMES[scheme]},
            action="set_eco_scheme",
        )
    ]
    if mask is not None:
        steps.append(
            CommandPatch(
                HPWH_SETTINGS_COMMAND,
                {"opp1EcoDays": mask, "operationName": "grSetWeekGroup"},
                action="set_eco_days",
                # The schema calls it a decimal range 0-40; the app and the device
                # spell it in hex (see `CommandPatch.verbatim`).
                verbatim=frozenset({"opp1EcoDays"}),
            )
        )
    values: dict[str, str] = {}
    for period, group in ((1, other), (2, first)):
        for slot in (1, 2, 3):
            start = end = _ECO_EMPTY
            if slot <= len(group):
                (sh, sm), (eh, em) = group[slot - 1]
                start, end = f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"
            values[f"opp{period}EcoStartTime{slot}"] = start
            values[f"opp{period}EcoEndTime{slot}"] = end
    values["operationName"] = "grSetEcoTime"
    steps.append(CommandPatch(HPWH_SETTINGS_COMMAND, values, action="set_eco_windows"))
    return tuple(steps)


def _js_strict_number(raw: object) -> float:
    """JavaScript `Number(x)` without the `|| 0` guard: missing is NaN, '' is 0."""
    if raw is None:
        return math.nan
    text = code(raw)
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return math.nan


def eco_changed_steps(
    get: Callable[[str], object], steps: tuple[CommandPatch, ...]
) -> tuple[CommandPatch, ...]:
    """The steps of `eco_schedule_steps` the app sends with Eco already on.

    Its save in Eco (apk2 decomp.txt:4506226-4506359) compares two things, nothing
    else:
    - the scheme with `Number(offPeakPeriodScheme)` of the shadow (4506238-4506252,
      4506288): equal, no scheme step;
    - with the scheme unchanged, the day mask with the shadow's `opp1EcoDays || '0'`,
      both in lower case (4506255-4506286): equal, no day step. After a changed scheme
      the chain sends the days with `different` whatever they were
      (`isEcoDaySended`, 1527772-1527872);
    - the windows: never compared, always sent (4506303-4506320, 1527873-1527905).
    """
    kept: list[CommandPatch] = []
    scheme_sent = True
    for step in steps:
        if "offPeakPeriodScheme" in step.values:
            wanted = float(step.values["offPeakPeriodScheme"])
            if _js_strict_number(get("offPeakPeriodScheme")) == wanted:
                scheme_sent = False
                continue
        elif "opp1EcoDays" in step.values and not scheme_sent:
            current = code(get("opp1EcoDays")) or "0"
            if str(step.values["opp1EcoDays"]).lower() == current.lower():
                continue
        kept.append(step)
    return tuple(kept)


def eco_step_applied(get: Callable[[str], object], patch: CommandPatch) -> bool:
    """Whether the shadow shows one step's values, in the appliance's own spelling.

    The day mask compares as a number ("1F" for "1f"), the windows as clocks
    ("06:00" for "6:00"); the operation name is no reading and is skipped.
    """
    for key, value in patch.values.items():
        if key == "operationName":
            continue
        current = code(get(key))
        if current is None:
            return False
        if key == "opp1EcoDays":
            try:
                same = int(current, 16) == int(str(value), 16)
            except ValueError:
                same = False
        elif key in _WINDOW_KEYS:
            same = _clock(current) is not None and _clock(current) == _clock(value)
        else:
            same = current == str(value)
        if not same:
            return False
    return True


def mapping_getter(attributes: Mapping[str, object]) -> Callable[[str], object]:
    """A `get` for the helpers above over a plain attribute mapping (tests, dumps)."""
    return attributes.get


# Energy (issue #115). `HPWHStatistics` reads three `...Year...` series
# (apk2 decomp.txt:2996565-2996700): the compressor's consumption, the backup
# element's and the heat produced, each a `;`-separated string of five yearly
# totals, oldest first, the current year last (`getBottomAxisDate`, @2999824). The
# app shows the numbers as they are, labelled kWh (@2999131-2999139): no factor
# here either. Its years tab totals all five slots of compressor + element
# (`getTotalWh` over `sumConsumptionArrays`, @2996797-2996819), 938 kWh on #115:
# that total is where every counter below starts.
#
# The counters are kept by Home Assistant rather than read. The appliance restarts
# its current year every January, and its window forgets its oldest year once it is
# full; to Home Assistant's statistics every drop is either energy counted twice or
# energy subtracted (doc 5 sections 2 and 6, i.e.
# `apk2/analysis/issue115-hw/decisioni/5-energia-entita.md`; doc 4 is
# `4-energia-dati.md` there, the series as the appliances really publish them). A
# counter that only adds the gains the guard below accepts has no drop to misread.
HPWH_YEAR_CP = "energyConsumptionYearCp"
HPWH_YEAR_EC = "energyConsumptionYearEc"
HPWH_YEAR_HEAT = "accumulatedHeatYear"
# The appliance's own day ("2026-09-30"): the guard's second witness of a new year.
HPWH_DATE = "date"
# Entity key -> the series it adds up. The key is the unique_id suffix: fixed for
# good. `total_energy` is compressor + element, two counters inside one entity, so
# that the two series sliding into the new year in different messages never makes
# the sum drop (doc 5 section 6, S5).
HPWH_ENERGY_COUNTERS: dict[str, tuple[str, ...]] = {
    "total_energy": (HPWH_YEAR_CP, HPWH_YEAR_EC),
    "compressor_energy": (HPWH_YEAR_CP,),
    "heater_energy": (HPWH_YEAR_EC,),
    "heat_produced": (HPWH_YEAR_HEAT,),
}
# The three, read together: a series of zeros is judged against the other two.
HPWH_YEAR_SERIES: tuple[str, ...] = (HPWH_YEAR_CP, HPWH_YEAR_EC, HPWH_YEAR_HEAT)
# Everything the counters read, for the diagnostics' coverage.
HPWH_ENERGY_ATTRS: tuple[str, ...] = (
    HPWH_YEAR_CP, HPWH_YEAR_EC, HPWH_YEAR_HEAT, HPWH_DATE
)
# Coordinator store (`HonBaseEntity._coordinator_store`) where each counter entity
# leaves its state for the diagnostics: {appliance_id: {key: {series: record}}}.
HPWH_ENERGY_STORE = "hpwh_energy_counters"
_YEAR_SLOTS = 5
# How long a series of five zeros has to be seen without a break before it may be a
# seed, in seconds of a monotonic clock: not a count of readings, since the poll
# re-reads an unchanged shadow every minute. A seed of zeros has no protection: if
# it was a transient, the real series after it reads as a slide (+142 kWh of
# element on #115) and moves the reference a year ahead.
_ZERO_SEED_SECONDS = 3600.0


def year_window(raw: object, *, zeros: bool = False) -> tuple[float, ...] | None:
    """A `...Year...` series as its five yearly totals, or None when it is not one.

    None, never 0, for anything but five finite numbers >= 0 (doc 5 section 7): the
    app's `cleansData` (decomp.txt:2999907-2999993) pads a short series with '0' and
    turns garbage into '0', which a chart survives and a counter does not. Five zeros
    are None too unless `zeros`: read alone, a series cannot tell an empty transient
    from a backup element that has not run in five years (`year_windows` can).
    Read as floats: a model publishing decimals would otherwise stay unknown.
    """
    if not isinstance(raw, str):
        return None
    parts = raw.split(";")
    if len(parts) != _YEAR_SLOTS:
        return None
    try:
        window = tuple(float(part) for part in parts)
    except ValueError:
        return None
    if not all(math.isfinite(value) and value >= 0 for value in window):
        return None
    return window if zeros or any(window) else None


def year_windows(get: Callable[[str], object]) -> dict[str, tuple[float, ...] | None]:
    """The three `...Year...` series of one shadow, each read by `year_window`.

    A series of five zeros is a reading only when one of the other two has a
    non-zero slot in the same shadow: an element that has not run in five years
    beside a live compressor. All three at zero together is what an empty transient
    looks like -- on a live appliance the compressor and the heat are never all zero
    after its first month (doc 4 section 9.1) -- and stays no reading.
    """
    windows = {name: year_window(get(name), zeros=True) for name in HPWH_YEAR_SERIES}
    live = any(window is not None and any(window) for window in windows.values())
    return {
        name: window if window is None or live else None
        for name, window in windows.items()
    }


def device_year(raw: object) -> int | None:
    """The year of the appliance's own `date` ("2026-09-30"), or None."""
    text = code(raw)
    try:
        return datetime.strptime(text or "", "%Y-%m-%d").year
    except ValueError:
        return None


@dataclass(frozen=True)
class YearRef:
    """The last accepted series, and the year its last slot belongs to (if known)."""

    window: tuple[float, ...] | None = None
    year: int | None = None


def year_step(
    ref: YearRef, window: tuple[float, ...] | None, year: int | None
) -> tuple[YearRef, float, bool]:
    """(new reference, kWh gained since `ref`, accepted) for one reading.

    The guard of doc 5 section 7, on DROPS of the current year only:
    - growth, or no change, of the last slot is gained as it is;
    - the new-year slide passes: last year's total moved to the second-to-last slot,
      which every m8 that crossed 2025 -> 2026 shows as an outcome (doc 4 section 6).
      `>=` admits the old year's last kWh arriving together with it, and those count;
    - a restart of the last slot in place passes only when the appliance's `date`
      is in a later year than the reference: there is then no slide to see;
    - any other drop is refused -- the year at zero for one message, an older value
      coming back -- and leaves the reference where it was. Home Assistant would
      read it as a restart and the next real value as energy;
    - so is a series from before the slide coming back after it (doc 4 section 9.1,
      last row), though its last slot RISES: +2493 kWh of heat on #115. It is the
      reference shifted one slot right, its last slot no higher than the
      reference's last year; or the appliance's `date` is in a year before the
      reference's. An unchanged series is never refused: the poll re-reads it
      between a slide and the date that follows.
    Closed years that do not move are the same year, never a slide: an element
    whose five years are all zero grows in place.
    """
    if window is None:
        return ref, 0.0, False
    old = ref.window
    if old is None:
        return YearRef(window, year), 0.0, True
    if window != old and (
        (window[:4] != old[:4] and window[1:4] == old[:3] and window[4] <= old[3])
        or (year is not None and ref.year is not None and year < ref.year)
    ):
        return ref, 0.0, False
    if window[:4] != old[:4] and window[:3] == old[1:4] and window[3] >= old[4]:
        # Slid. The new year is at least one after the reference's, even when the
        # series slides before the appliance's date turns (doc 5 section 6, S5).
        nxt = None if ref.year is None else ref.year + 1
        years = [y for y in (year, nxt) if y is not None]
        gained = (window[3] - old[4]) + window[4]
        return YearRef(window, max(years) if years else None), gained, True
    if window[4] >= old[4]:
        # Same year. The reference keeps its own year: a `date` that turns before
        # the series restarts must still be able to explain that restart.
        same = ref.year if ref.year is not None else year
        return YearRef(window, same), window[4] - old[4], True
    if year is not None and ref.year is not None and year > ref.year:
        return YearRef(window, year), window[4], True
    return ref, 0.0, False


@dataclass(frozen=True)
class Lifetime:
    """One energy counter: kWh since the five-year window of its first reading began.

    `seed` is the five-year total at the first reading, `total` the seed plus every
    accepted gain since. Past the window's fifth year the total is more than the
    app's, which forgets its oldest year: a total, no longer parity with the app.
    `holding` says the last readable series was refused, so the counter is below
    what the appliance says; `refused` is the last series refused and `refusals`
    how many times a refusal began. `zero_since` is when five zeros were first seen
    before the seed (monotonic seconds); it is not part of the restore record, so a
    restart starts the hour over.
    """

    total: float | None = None
    seed: float | None = None
    ref: YearRef = field(default_factory=YearRef)
    holding: bool = False
    refused: tuple[float, ...] | None = None
    refusals: int = 0
    zero_since: float | None = None


def lifetime_step(
    state: Lifetime, window: tuple[float, ...] | None, year: int | None, *, now: float
) -> Lifetime:
    """`state` after one reading of its series, taken at `now` (monotonic seconds).

    An unreadable series changes nothing. The first readable one is the seed: the
    sum of its five slots, the app's own total -- and Home Assistant counts nothing
    for it, since its statistics take a sensor's first value as the zero point. Five
    zeros are a seed only once seen for an hour since the first of them: until then
    they are no reading, a series that is not zero in between seeds at once, and an
    unreadable one neither stops nor restarts the hour. A
    refused series counts as a new refusal only when the counter was not already
    holding that same series: the poll re-reads an unchanged shadow every minute.
    """
    if window is None:
        return state
    if state.total is None:
        if not any(window):
            since = now if state.zero_since is None else state.zero_since
            if now - since < _ZERO_SEED_SECONDS:
                return replace(state, zero_since=since)
        total = float(sum(window))
        return Lifetime(total, total, YearRef(window, year))
    ref, gained, accepted = year_step(state.ref, window, year)
    if accepted:
        return replace(state, total=state.total + gained, ref=ref, holding=False)
    if state.holding and window == state.refused:
        return state
    return replace(state, holding=True, refused=window, refusals=state.refusals + 1)


def lifetime_as_dict(state: Lifetime) -> dict | None:
    """`state` as JSON: the restore record and the diagnostics row. None if unseeded."""
    if state.total is None or state.ref.window is None:
        return None
    return {
        "seed": state.seed,
        "total": state.total,
        "reference": list(state.ref.window),
        "year": state.ref.year,
        "holding": state.holding,
        "refused": None if state.refused is None else list(state.refused),
        "refusals": state.refusals,
    }


def _stored_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _stored_window(value: object) -> tuple[float, ...] | None:
    if not isinstance(value, list) or any(_stored_number(v) is None for v in value):
        return None
    # Five zeros pass: `year_windows` may have accepted them as a reading.
    return year_window(";".join(repr(float(v)) for v in value), zeros=True)


def lifetime_from_dict(data: object) -> Lifetime:
    """A counter from `lifetime_as_dict`'s record, or a new one when it cannot be read.

    Validated rather than trusted: a damaged or foreign record starts the counter
    over, from the five-year total of the next reading, as on its first run. The
    optional fields fall back one by one.
    """
    if not isinstance(data, Mapping):
        return Lifetime()
    total, seed = _stored_number(data.get("total")), _stored_number(data.get("seed"))
    window = _stored_window(data.get("reference"))
    if total is None or seed is None or window is None:
        return Lifetime()
    year = data.get("year")
    refusals = data.get("refusals")
    holding = data.get("holding")
    return Lifetime(
        total,
        seed,
        YearRef(window, year if type(year) is int else None),
        holding if isinstance(holding, bool) else False,
        _stored_window(data.get("refused")),
        refusals if type(refusals) is int and refusals >= 0 else 0,
    )
