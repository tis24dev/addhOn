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
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import date, datetime

from homeassistant.exceptions import HomeAssistantError

from .command_dispatch import CommandPatch
from .const import DOMAIN

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


def sterilization_time(raw: object) -> str | None:
    """`sterilizationTime` as "HH:MM"; the app writes it unpadded ("15:0").

    Built as `String(hours + ':' + minutes)` (apk2 decomp.txt:4592323-4592332), so
    both halves are padded here. Anything that is not an hour and a minute is None.
    "00:00" stays as it is: on the #113 appliance it may also mean never set, and
    nothing in the shadow tells the two apart.
    """
    text = code(raw)
    if not text:
        return None
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    hours, minutes = int(parts[0]), int(parts[1])
    if hours > 23 or minutes > 59:
        return None
    return f"{hours:02d}:{minutes:02d}"


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

    Shared by the water heater and the boost switch. One literal raise per key:
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


def mapping_getter(attributes: Mapping[str, object]) -> Callable[[str], object]:
    """A `get` for the helpers above over a plain attribute mapping (tests, dumps)."""
    return attributes.get
