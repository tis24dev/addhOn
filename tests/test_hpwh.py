# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Heat-pump water heater (type HW, issue #113): the read-only entities.

The pure helpers in `hpwh.py` rebuild what the hOn app 2.30.7 derives
(`apk2/analysis/issue113-hw-hpwh-control-model.md`); the entity tests build the
platforms over the attributes the reporter's HP110M8-9 really published
(`tests/fixtures/hw_hp110m8/attributes.json`).
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "hw_hp110m8" / "attributes.json"


def _mod(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_package_stubs() -> None:
    """The `homeassistant` pieces conftest does not provide (getattr-guarded)."""
    ha = _mod("homeassistant")
    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exceptions = _mod("homeassistant.exceptions")
    base_err = getattr(
        exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {})
    )
    exceptions.HomeAssistantError = base_err
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {})
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {})
    )
    components = _mod("homeassistant.components")
    sensor_mod = _mod("homeassistant.components.sensor")
    sensor_mod.SensorEntity = getattr(
        sensor_mod, "SensorEntity", type("SensorEntity", (), {})
    )
    components.sensor = sensor_mod
    # conftest provides the binary-sensor description and device class but not the
    # entity base, so this file could not import the platform when run on its own.
    binary_mod = _mod("homeassistant.components.binary_sensor")
    binary_mod.BinarySensorEntity = getattr(
        binary_mod, "BinarySensorEntity", type("BinarySensorEntity", (), {})
    )
    components.binary_sensor = binary_mod
    date_mod = _mod("homeassistant.components.date")
    date_mod.DateEntity = getattr(date_mod, "DateEntity", type("DateEntity", (), {}))
    components.date = date_mod
    const = _mod("homeassistant.const")
    const.UnitOfEnergy = getattr(
        const, "UnitOfEnergy", type("UnitOfEnergy", (), {"KILO_WATT_HOUR": "kWh"})
    )
    const.UnitOfVolume = getattr(
        const, "UnitOfVolume", type("UnitOfVolume", (), {"LITERS": "L"})
    )
    const.UnitOfTime = getattr(
        const, "UnitOfTime", type("UnitOfTime", (), {"MINUTES": "min", "SECONDS": "s"})
    )
    const.UnitOfTemperature = getattr(
        const, "UnitOfTemperature", type("UnitOfTemperature", (), {"CELSIUS": "°C"})
    )
    const.UnitOfMass = getattr(
        const, "UnitOfMass", type("UnitOfMass", (), {"GRAMS": "g", "KILOGRAMS": "kg"})
    )
    const.EntityCategory = getattr(
        const,
        "EntityCategory",
        type("EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"}),
    )
    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.components = components
    ha.const = const


_install_package_stubs()

from custom_components.addhon import hpwh  # noqa: E402
from custom_components.addhon.hpwh import (  # noqa: E402
    heat_pump_state,
    mapping_getter,
    mode_key,
    schedule_window,
    water_level_percent,
)
from custom_components.addhon.hpwh import (  # noqa: E402
    boost_auto_off_due,
    boost_block,
    boost_patch,
    is_vacation_active,
    mode_block,
    mode_patch,
    power_patch,
    temperature_block,
    temperature_patch,
)

# 2026-09-28 is a Monday (JavaScript getDay 1); 2026-10-03 a Saturday (6).
MONDAY = datetime(2026, 9, 28, 3, 0)
SATURDAY = datetime(2026, 10, 3, 3, 0)


def _at(hour: int, minute: int) -> datetime:
    return MONDAY.replace(hour=hour, minute=minute)


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _attrs(**overrides) -> dict:
    attributes = dict(_fixture()["attributes"])
    attributes.update(overrides)
    return attributes


def _state(attributes: dict, now: datetime = MONDAY, series: str | None = "m8") -> str:
    return heat_pump_state(mapping_getter(attributes), now, series)


def _window(attributes: dict, now: datetime = MONDAY, series: str | None = "m8"):
    return schedule_window(mapping_getter(attributes), now, series)


class WaterLevelTest(unittest.TestCase):
    """`getWaterPercentage` (apk2 decomp.txt:2355651): a 0-12 scale."""

    def test_the_reporters_level_is_the_apps_83_percent(self) -> None:
        self.assertEqual(water_level_percent(_fixture()["attributes"]["remainingWaterLevel"]), 83)

    def test_the_whole_table(self) -> None:
        expected = {0: 8, 1: 8, 2: 16, 3: 25, 4: 35, 5: 41, 6: 50,
                    7: 58, 8: 65, 9: 75, 10: 83, 11: 91, 12: 100}
        for level, percent in expected.items():
            self.assertEqual(water_level_percent(level), percent, level)

    def test_string_and_integral_float_read_like_the_integer(self) -> None:
        self.assertEqual(water_level_percent("10"), 83)
        self.assertEqual(water_level_percent(10.0), 83)

    def test_outside_the_table_is_unknown_not_the_apps_70(self) -> None:
        for raw in (13, -1, 10.5, "abc", None, ""):
            self.assertIsNone(water_level_percent(raw), raw)


class ModeTest(unittest.TestCase):
    """`HPWHMachMode` (apk2 decomp.txt:599199-599208)."""

    def test_every_code_and_its_numeric_spellings(self) -> None:
        for raw, key in (("1", "auto"), (2, "eco"), (3.0, "electric"), ("4", "vacation")):
            self.assertEqual(mode_key(raw), key, raw)

    def test_an_unknown_code_is_unknown(self) -> None:
        for raw in ("0", 5, 1.5, None, ""):
            self.assertIsNone(mode_key(raw), raw)


class EcoDaysTest(unittest.TestCase):
    """`hexToDays` (apk2 decomp.txt:2333056): Monday-first hex bytes, JS day numbers."""

    def test_every_day_wraps_from_monday_to_sunday(self) -> None:
        self.assertEqual(hpwh._hex_to_days("7F"), (1, 0))
        self.assertEqual(hpwh._hex_to_days("7f"), (1, 0))

    def test_monday_to_friday(self) -> None:
        self.assertEqual(hpwh._hex_to_days("1f"), (1, 5))

    def test_single_days_match_their_padded_byte_only(self) -> None:
        self.assertEqual(hpwh._hex_to_days("01"), (1, 1))
        self.assertEqual(hpwh._hex_to_days("40"), (0, 0))
        # The app's own write path can produce "1" for Monday alone; its read path
        # does not recognise it, and neither does this one.
        self.assertIsNone(hpwh._hex_to_days("1"))

    def test_a_non_contiguous_set_has_no_range(self) -> None:
        self.assertIsNone(hpwh._hex_to_days("05"))  # Monday + Wednesday
        self.assertIsNone(hpwh._hex_to_days(None))
        self.assertIsNone(hpwh._hex_to_days("zz"))


class ScheduleWindowTest(unittest.TestCase):
    """`getScheduleTime` (apk2 decomp.txt:2327831) for the plain series."""

    def test_the_reporters_empty_windows_give_no_schedule(self) -> None:
        self.assertIsNone(_window(_attrs()))

    def test_same_scheme_returns_the_current_opp2_window(self) -> None:
        attributes = _attrs(opp2EcoStartTime1="01:00", opp2EcoEndTime1="06:00")
        self.assertEqual(_window(attributes), ("01:00", "06:00"))

    def test_same_scheme_returns_the_next_opp2_window(self) -> None:
        attributes = _attrs(opp2EcoStartTime1="04:00", opp2EcoEndTime1="06:00")
        self.assertEqual(_window(attributes), ("04:00", "06:00"))

    def test_same_scheme_after_the_last_opp2_window_returns_tomorrows_first(self) -> None:
        # The app returns null here (apk2 decomp.txt:2328062); the next window is
        # the first one of tomorrow instead, as its isM11 branch answers.
        attributes = _attrs(
            opp2EcoStartTime1="01:00", opp2EcoEndTime1="02:00",
            opp1EcoStartTime1="04:00", opp1EcoEndTime1="05:00",
        )
        # opp1 is never consulted with offPeakPeriodScheme 1.
        self.assertEqual(_window(attributes), ("01:00", "02:00"))

    def test_tomorrows_first_is_the_set_window_with_the_earliest_start(self) -> None:
        # A slot without an end ("00:00") is not a window; a "00:00" start is.
        attributes = _attrs(
            opp2EcoStartTime1="01:00", opp2EcoEndTime1="02:00",
            opp2EcoStartTime2="02:00", opp2EcoEndTime2="00:00",
            opp2EcoStartTime3="00:00", opp2EcoEndTime3="00:30",
        )
        self.assertEqual(_window(attributes), ("00:00", "00:30"))
        attributes["opp2EcoEndTime3"] = "00:00"
        self.assertEqual(_window(attributes), ("01:00", "02:00"))

    def test_different_scheme_still_gives_up_after_the_last_window(self) -> None:
        # Not extended to scheme 0: there the app goes on to the opp1 windows of
        # the other days first, and "tomorrow" may take the other set.
        attributes = _attrs(
            offPeakPeriodScheme=0,
            opp1EcoDays="1f",
            opp2EcoStartTime1="01:00", opp2EcoEndTime1="02:00",
        )
        self.assertIsNone(_window(attributes, MONDAY))

    def test_different_scheme_uses_opp2_on_the_mask_days_and_opp1_elsewhere(self) -> None:
        attributes = _attrs(
            offPeakPeriodScheme=0,
            opp1EcoDays="1f",
            opp2EcoStartTime1="04:00", opp2EcoEndTime1="05:00",
            opp1EcoStartTime1="07:00", opp1EcoEndTime1="08:00",
        )
        self.assertEqual(_window(attributes, MONDAY), ("04:00", "05:00"))
        self.assertEqual(_window(attributes, SATURDAY), ("07:00", "08:00"))

    def test_different_scheme_falls_through_to_opp1_after_the_last_opp2_window(self) -> None:
        # The app's own behaviour (apk2 decomp.txt:2328007-2328015): on a mask day,
        # once the opp2 windows are over, the opp1 windows of the other days follow.
        attributes = _attrs(
            offPeakPeriodScheme=0,
            opp1EcoDays="1f",
            opp2EcoStartTime1="01:00", opp2EcoEndTime1="02:00",
            opp1EcoStartTime1="07:00", opp1EcoEndTime1="08:00",
        )
        self.assertEqual(_window(attributes, MONDAY), ("07:00", "08:00"))

    def test_a_zero_day_mask_means_no_schedule(self) -> None:
        attributes = _attrs(opp1EcoDays="0", opp2EcoStartTime1="04:00", opp2EcoEndTime1="05:00")
        self.assertIsNone(_window(attributes))

    def test_the_third_opp1_window_is_matched_on_its_start_only(self) -> None:
        attributes = _attrs(
            offPeakPeriodScheme=0,
            opp1EcoDays="40",  # Sunday only, so Monday takes opp1
            opp1EcoStartTime3="02:00", opp1EcoEndTime3="05:00",
        )
        # 03:00 is inside the window, but the app returns it only before it starts.
        self.assertIsNone(_window(attributes))

    def test_other_series_are_not_rebuilt(self) -> None:
        attributes = _attrs(opp2EcoStartTime1="04:00", opp2EcoEndTime1="06:00")
        for series in ("m7b", "m8b", "m11"):
            self.assertIsNone(_window(attributes, series=series), series)


class HeatPumpStateTest(unittest.TestCase):
    """`getActiveStatus` for ApplianceType.HW (apk2 decomp.txt:2328690)."""

    def test_the_reporters_appliance_is_off(self) -> None:
        self.assertEqual(_state(_attrs()), "off")

    def test_on_in_auto_below_target_is_working(self) -> None:
        self.assertEqual(_state(_attrs(onOffStatus=1)), "working")

    def test_on_above_target_is_keeping_warm(self) -> None:
        self.assertEqual(_state(_attrs(onOffStatus=1, temp=45)), "keep_warm")

    def test_an_error_code_wins(self) -> None:
        self.assertEqual(_state(_attrs(onOffStatus=1, errors=3)), "error")
        self.assertEqual(_state(_attrs(errors="", error="5")), "error")

    def test_remote_control_disabled(self) -> None:
        self.assertEqual(_state(_attrs(onOffStatus=1, remoteCtrValid="0")), "remote_control_off")

    def test_eco_before_its_window_is_scheduled_even_when_off(self) -> None:
        attributes = _attrs(machMode=2, opp2EcoStartTime1="04:00", opp2EcoEndTime1="06:00")
        self.assertEqual(_state(attributes), "scheduled")

    def test_eco_inside_its_window_is_working(self) -> None:
        attributes = _attrs(
            onOffStatus=1, machMode=2,
            opp2EcoStartTime1="01:00", opp2EcoEndTime1="06:00",
        )
        self.assertEqual(_state(attributes), "working")

    def test_eco_without_windows_counts_as_active(self) -> None:
        self.assertEqual(_state(_attrs(onOffStatus=1, machMode=2)), "working")

    def test_issue_115_after_its_window_waits_for_tomorrows(self) -> None:
        # Issue #115 (HP150M8-9): eco, one window 00:15-23:45, on, 62 towards 65.
        attributes = _attrs(
            onOffStatus=1, machMode=2, temp=62, tempSel=65.0,
            opp2EcoStartTime1="00:15", opp2EcoEndTime1="23:45",
        )
        self.assertEqual(_state(attributes, _at(0, 14)), "scheduled")
        self.assertEqual(_state(attributes, _at(0, 15)), "working")
        # The app counts the end minute as inside the window (decomp.txt:2328453).
        self.assertEqual(_state(attributes, _at(23, 45)), "working")
        # The app just opened says working here; it says scheduled when its screen
        # was opened before 23:45.
        self.assertEqual(_state(attributes, _at(23, 46)), "scheduled")
        self.assertEqual(_state(attributes, _at(23, 59)), "scheduled")
        # Off, the same stretch reads scheduled too, as before the window.
        attributes["onOffStatus"] = 0
        self.assertEqual(_state(attributes, _at(23, 46)), "scheduled")
        self.assertEqual(_state(attributes, _at(12, 0)), "off")

    def test_a_daytime_window_is_scheduled_all_evening(self) -> None:
        # The hon-test-data #65 dump (HP110M8-9, Amsterdam): eco, 11:00-16:00, on,
        # 65 of 65. The app just opened says working from 16:01 to midnight.
        attributes = _attrs(
            onOffStatus=1, machMode=2, temp=65, tempSel=65.0,
            opp2EcoStartTime1="11:00", opp2EcoEndTime1="16:00",
        )
        self.assertEqual(_state(attributes, _at(10, 59)), "scheduled")
        self.assertEqual(_state(attributes, _at(16, 0)), "working")
        self.assertEqual(_state(attributes, _at(16, 1)), "scheduled")
        self.assertEqual(_state(attributes, _at(18, 0)), "scheduled")

    def test_the_last_of_several_windows_is_working_through_its_end_minute(self) -> None:
        # PR #119 review: with an earlier window first, the end minute of the last one
        # took tomorrow's first window and read scheduled. One window alone hid it,
        # since tomorrow's first IS that window.
        attributes = _attrs(
            onOffStatus=1, machMode=2, temp=60, tempSel=65.0,
            opp2EcoStartTime1="01:00", opp2EcoEndTime1="02:00",
            opp2EcoStartTime2="11:00", opp2EcoEndTime2="16:00",
        )
        self.assertEqual(_window(attributes, _at(16, 0)), ("11:00", "16:00"))
        self.assertEqual(_state(attributes, _at(16, 0)), "working")
        self.assertEqual(_window(attributes, _at(16, 1)), ("01:00", "02:00"))
        self.assertEqual(_state(attributes, _at(16, 1)), "scheduled")

    def test_vacation_falls_through_to_sterilization_or_working(self) -> None:
        self.assertEqual(_state(_attrs(onOffStatus=1, machMode=4)), "working")
        self.assertEqual(
            _state(_attrs(onOffStatus=1, machMode=4, sterilizationCurrentStatus=1)),
            "sterilizing",
        )

    def test_series_not_rebuilt_are_unknown_where_the_schedule_matters(self) -> None:
        for series in ("m7b", "m8b", "m11"):
            for mode in (2, 4):  # eco, vacation
                self.assertIsNone(
                    _state(_attrs(onOffStatus=1, machMode=mode), series=series),
                    (series, mode),
                )
            # AUTO never consults the schedule, so the answer stays exact.
            self.assertEqual(_state(_attrs(onOffStatus=1), series=series), "working")
            # And an error still wins before any of that.
            self.assertEqual(_state(_attrs(machMode=2, errors=3), series=series), "error")

    def test_an_active_mode_reports_working_before_sterilization(self) -> None:
        # The app checks the active mode first: AUTO with a cycle under way still
        # reads as working.
        attributes = _attrs(onOffStatus=1, sterilizationCurrentStatus=1)
        self.assertEqual(_state(attributes), "working")


class ControlRulesTest(unittest.TestCase):
    """What the app lets a user do, and what it sends (apk2 2.30.7)."""

    def _get(self, **overrides):
        return mapping_getter(_attrs(onOffStatus=1, **overrides))

    def test_everything_is_allowed_on_a_healthy_appliance_that_is_on(self) -> None:
        get = self._get()
        self.assertIsNone(mode_block(get))
        self.assertIsNone(temperature_block(get))
        self.assertIsNone(boost_block(get, turning_on=True))

    def test_off_asks_to_switch_on_first(self) -> None:
        get = mapping_getter(_attrs())  # the reporter's appliance is off
        self.assertEqual(mode_block(get), "hpwh_switch_on_first")
        self.assertEqual(temperature_block(get), "hpwh_switch_on_first")
        self.assertEqual(boost_block(get, turning_on=True), "hpwh_switch_on_first")

    def test_an_error_or_remote_control_off_disables(self) -> None:
        for overrides in ({"errors": 3}, {"remoteCtrValid": "0"}):
            get = self._get(**overrides)
            self.assertEqual(mode_block(get), "hpwh_unavailable", overrides)
            self.assertEqual(temperature_block(get), "hpwh_unavailable", overrides)

    def test_vacation_blocks_mode_temperature_and_boost(self) -> None:
        get = self._get(machMode=4, vacStartDate="2026-10-01", vacEndDate="2026-10-08")
        self.assertTrue(is_vacation_active(get))
        self.assertEqual(mode_block(get), "hpwh_vacation_active")
        self.assertEqual(temperature_block(get), "hpwh_vacation_active")
        self.assertEqual(boost_block(get, turning_on=True), "hpwh_vacation_active")

    def test_cleared_vacation_dates_are_not_a_vacation(self) -> None:
        get = self._get(machMode=4, vacStartDate="2000-01-01", vacEndDate="2000-01-01")
        self.assertFalse(is_vacation_active(get))

    def test_sterilization_blocks_mode_and_temperature_not_boost(self) -> None:
        get = self._get(sterilizationCurrentStatus=1)
        self.assertEqual(mode_block(get), "hpwh_sterilization_running")
        self.assertEqual(temperature_block(get), "hpwh_sterilization_running")
        self.assertIsNone(boost_block(get, turning_on=False))

    def test_boost_cannot_start_at_or_above_target(self) -> None:
        self.assertEqual(
            boost_block(self._get(temp=40), turning_on=True), "hpwh_boost_at_target"
        )
        self.assertIsNone(boost_block(self._get(temp=40, boostStatus=1), turning_on=False))

    def test_power_patch(self) -> None:
        patch = power_patch(True)
        self.assertEqual((patch.command_name, dict(patch.values)), ("settings", {"onOffStatus": "1"}))
        self.assertEqual(dict(power_patch(False).values), {"onOffStatus": "0"})

    def test_temperature_patch_switches_boost_off_below_the_current_temperature(self) -> None:
        get = self._get(temp=45, boostStatus=1)
        self.assertEqual(
            dict(temperature_patch(get, 40).values), {"tempSel": "40", "boostStatus": "0"}
        )
        self.assertEqual(dict(temperature_patch(get, 50).values), {"tempSel": "50"})
        self.assertEqual(
            dict(temperature_patch(self._get(temp=45), 40).values), {"tempSel": "40"}
        )

    def test_mode_patch_carries_machmode_only_and_no_program_name(self) -> None:
        patch = mode_patch("eco", "2")
        self.assertEqual(patch.command_name, "startProgram")
        self.assertEqual(dict(patch.values), {"machMode": "2"})
        self.assertEqual(patch.program_name, "")
        self.assertIsNotNone(patch.prepare)

    def test_boost_patch(self) -> None:
        self.assertEqual(dict(boost_patch(True).values), {"boostStatus": "1"})
        self.assertEqual(dict(boost_patch(False).values), {"boostStatus": "0"})

    def test_boost_auto_off(self) -> None:
        self.assertTrue(boost_auto_off_due(self._get(boostStatus=1, temp=40)))
        self.assertFalse(boost_auto_off_due(self._get(boostStatus=1, temp=39)))
        self.assertFalse(boost_auto_off_due(self._get(boostStatus=0, temp=40)))

    def test_every_refusal_is_raised_with_its_own_key(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        for key in (
            "hpwh_unavailable",
            "hpwh_switch_on_first",
            "hpwh_vacation_active",
            "hpwh_sterilization_running",
            "hpwh_boost_at_target",
        ):
            with self.assertRaises(HomeAssistantError) as ctx:
                hpwh.raise_refusal(key)
            self.assertEqual(ctx.exception.translation_key, key)
        hpwh.raise_refusal(None)  # no refusal, nothing raised
        with self.assertRaises(ValueError):
            hpwh.raise_refusal("hpwh_not_a_refusal")

    def test_controls_are_not_offered_on_the_series_whose_rules_are_not_rebuilt(self) -> None:
        for series in ("m7b", "M8B", " M11 ", "m11"):
            self.assertFalse(hpwh.controls_supported(series), series)
        # Only those three: a missing or unknown series keeps the controls.
        for series in ("m8", "M8", None, "", "x9"):
            self.assertTrue(hpwh.controls_supported(series), series)


# The schedule and vacation values the #115 HP150M8-9 really published (addhOn
# 5.25.0 diagnostics, 2026-09-29): one window 00:15-23:45 every day, dates cleared.
_ISSUE115 = {
    "machMode": 2,
    "offPeakPeriodScheme": 1,
    "opp1EcoDays": "7F",
    "opp2EcoStartTime1": "00:15",
    "opp2EcoEndTime1": "23:45",
    "vacStartDate": "2000-01-01",
    "vacEndDate": "2000-01-01",
    "sterilizationTime": "15:00",
}


class VacationTest(unittest.TestCase):
    """`isVacModeActive` (apk2 decomp.txt:2334140-2334188), both branches."""

    def _active(self, series: str | None = "m8", **overrides) -> bool:
        return hpwh.vacation_active(mapping_getter(_attrs(**overrides)), series)

    def test_plain_series_need_the_mode_and_both_dates(self) -> None:
        dates = {"vacStartDate": "2026-10-10", "vacEndDate": "2026-10-20"}
        self.assertTrue(self._active(machMode=4, **dates))
        self.assertTrue(self._active(machMode="4", **dates))
        self.assertFalse(self._active(machMode=1, **dates))

    def test_plain_series_read_cleared_dates_as_no_vacation(self) -> None:
        for cleared in ("2000-01-01", ""):
            with self.subTest(cleared=cleared):
                self.assertFalse(
                    self._active(machMode=4, vacStartDate=cleared, vacEndDate="2026-10-20")
                )
                self.assertFalse(
                    self._active(machMode=4, vacStartDate="2026-10-10", vacEndDate=cleared)
                )

    def test_the_113_placeholder_is_not_a_cleared_date_for_the_app(self) -> None:
        # The #113 HP110M8-9 publishes "0000-00-00". The app compares with '' and
        # '2000-01-01' only, so with machMode 4 it reads as a vacation under way.
        self.assertTrue(self._active(machMode=4))
        self.assertFalse(self._active(machMode=1))

    def test_the_other_series_read_the_mode_alone(self) -> None:
        for series in ("m7b", "M8B", " m11 "):
            with self.subTest(series=series):
                self.assertTrue(
                    self._active(series, machMode=4, vacStartDate="2000-01-01",
                                 vacEndDate="2000-01-01")
                )
                self.assertFalse(self._active(series, machMode=1))

    def test_an_unknown_or_missing_series_takes_the_plain_branch(self) -> None:
        cleared = {"vacStartDate": "2000-01-01", "vacEndDate": "2000-01-01"}
        self.assertFalse(self._active(None, machMode=4, **cleared))
        self.assertFalse(self._active("x9", machMode=4, **cleared))


class VacationDateTest(unittest.TestCase):
    """`vacStartDate` / `vacEndDate` as calendar dates."""

    def test_a_set_date(self) -> None:
        from datetime import date

        self.assertEqual(hpwh.vacation_date("2026-10-10"), date(2026, 10, 10))

    def test_cleared_missing_and_unparsable_dates_are_none(self) -> None:
        for raw in ("2000-01-01", "", None, "0000-00-00", "2026-02-30", "10/10/2026", 0):
            self.assertIsNone(hpwh.vacation_date(raw), raw)


class SterilizationTimeTest(unittest.TestCase):
    """`sterilizationTime`: the app writes it without leading zeros ("15:0")."""

    def test_padded_as_hh_mm(self) -> None:
        for raw, text in (("15:0", "15:00"), ("2:0", "02:00"), ("7:30", "07:30"),
                          ("15:00", "15:00"), ("00:00", "00:00")):
            self.assertEqual(hpwh.sterilization_time(raw), text, raw)

    def test_missing_or_not_a_time_is_none(self) -> None:
        for raw in (None, "", "abc", "24:00", "12:60", "12", "1:2:3", 15):
            self.assertIsNone(hpwh.sterilization_time(raw), raw)


class EcoWindowTest(unittest.TestCase):
    """The eco-window sensor: `schedule_window` as text, the raw schedule as attributes."""

    def test_the_115_window_is_current_at_night(self) -> None:
        get = mapping_getter(_attrs(**_ISSUE115))
        self.assertEqual(hpwh.eco_window_text(get, _at(3, 0), "m8"), "00:15\u201323:45")

    def test_no_window_set_is_none(self) -> None:
        self.assertIsNone(hpwh.eco_window_text(mapping_getter(_attrs()), MONDAY, "m8"))

    def test_the_other_series_are_unknown(self) -> None:
        get = mapping_getter(_attrs(**_ISSUE115))
        self.assertIsNone(hpwh.eco_window_text(get, _at(3, 0), "m8b"))

    def test_the_attributes_of_the_115_schedule(self) -> None:
        self.assertEqual(
            hpwh.eco_window_attributes(mapping_getter(_attrs(**_ISSUE115))),
            {
                "opp1_windows": [],
                "opp2_windows": ["00:15\u201323:45"],
                "off_peak_period_scheme": "1",
                "opp1_eco_days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            },
        )

    def test_every_non_empty_slot_of_both_groups_in_order(self) -> None:
        attributes = _attrs(
            offPeakPeriodScheme=0,
            opp1EcoDays="1F",
            opp1EcoStartTime1="06:00", opp1EcoEndTime1="08:00",
            opp1EcoStartTime3="00:00", opp1EcoEndTime3="05:00",
            opp2EcoStartTime2="22:00", opp2EcoEndTime2="23:30",
        )
        result = hpwh.eco_window_attributes(mapping_getter(attributes))
        self.assertEqual(result["opp1_windows"], ["06:00\u201308:00", "00:00\u201305:00"])
        self.assertEqual(result["opp2_windows"], ["22:00\u201323:30"])
        self.assertEqual(result["off_peak_period_scheme"], "0")
        self.assertEqual(result["opp1_eco_days"], ["mon", "tue", "wed", "thu", "fri"])

    def test_the_day_mask_is_read_bit_by_bit(self) -> None:
        for mask, days in (("60", ["sat", "sun"]), ("01", ["mon"]), ("1", ["mon"]),
                           ("05", ["mon", "wed"]), ("00", [])):
            self.assertEqual(hpwh.eco_days(mask), days, mask)

    def test_an_unreadable_day_mask_is_none(self) -> None:
        for mask in (None, "", "zz", "80", "FF"):
            self.assertIsNone(hpwh.eco_days(mask), mask)

    def test_the_diagnostics_tuples_name_what_the_helpers_read(self) -> None:
        self.assertEqual(
            set(hpwh.HPWH_ECO_WINDOW_ATTRS),
            {"offPeakPeriodScheme", "opp1EcoDays", *hpwh._WINDOW_KEYS},
        )
        self.assertEqual(
            hpwh.HPWH_VACATION_ATTRS, ("machMode", "vacStartDate", "vacEndDate")
        )


class _Coordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.hass = None
        self.last_update_success = True


class _Hass:
    def __init__(self, data: dict) -> None:
        self.data = data


class _Entry:
    entry_id = "entry-1"

    def __init__(self) -> None:
        self.options: dict = {}


def _data(series: str | None = None) -> dict:
    fixture = _fixture()
    appliance = types.SimpleNamespace(
        model_attributes={"series": series or fixture["series"]}
    )
    return {
        "hw-1": {
            "type": fixture["type"],
            "name": "Warmtepomp boiler",
            "attributes": dict(fixture["attributes"]),
            "settings": {},
            "appliance": appliance,
        }
    }


async def _build(platform, data: dict | None = None) -> list:
    from custom_components.addhon.const import DOMAIN

    coordinator = _Coordinator(data if data is not None else _data())
    hass = _Hass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
    added: list = []
    await platform.async_setup_entry(hass, _Entry(), added.extend)
    return [e for e in added if str(getattr(e, "_attr_unique_id", "")).startswith("hw-1_")]


class HeatPumpEntitiesTest(unittest.IsolatedAsyncioTestCase):
    """The entities the reporter's appliance gets, with its real values."""

    async def test_the_sensors_and_their_values(self) -> None:
        from custom_components.addhon import sensor

        with mock.patch.object(
            sensor.HonHeatPumpStateSensor, "_now", staticmethod(lambda: MONDAY)
        ), mock.patch.object(
            sensor.HonHeatPumpEcoWindowSensor, "_now", staticmethod(lambda: MONDAY)
        ):
            entities = {e._attr_unique_id: e for e in await _build(sensor)}
            values = {key: entity.native_value for key, entity in entities.items()}
        self.assertEqual(
            values,
            {
                "hw-1_water_temp": 33.0,
                "hw-1_target_temp": 40.0,
                "hw-1_hot_water_available": 83,
                "hw-1_heat_pump_mode": "auto",
                "hw-1_errors": "0",
                "hw-1_sterilization_time": "00:00",
                "hw-1_heat_pump_state": "off",
                # Every window of the #113 appliance is "00:00"-"00:00".
                "hw-1_eco_window": None,
            },
        )

    async def test_the_state_sensor_publishes_every_app_phase_as_an_option(self) -> None:
        from custom_components.addhon import sensor

        state = next(e for e in await _build(sensor) if e._attr_unique_id == "hw-1_heat_pump_state")
        self.assertEqual(set(state._attr_options), set(hpwh.HPWH_STATES))

    async def test_the_binary_sensors_and_their_values(self) -> None:
        from custom_components.addhon import binary_sensor

        entities = {
            e._attr_unique_id: e.is_on
            for e in await _build(binary_sensor)
            # Every appliance type gets the connectivity binary; not an HW entity.
            if e._attr_unique_id != "hw-1_connectivity"
        }
        self.assertEqual(
            entities,
            {
                "hw-1_compressor_heating": False,
                "hw-1_electric_heating": False,
                "hw-1_boost": False,
                "hw-1_sterilization_running": False,
                "hw-1_vacation_active": False,
                "hw-1_defrost": False,
            },
        )

    async def test_the_heating_flags_read_any_non_zero_as_running(self) -> None:
        from custom_components.addhon import binary_sensor

        entities = {e._attr_unique_id: e for e in await _build(binary_sensor)}
        compressor = entities["hw-1_compressor_heating"]
        compressor.coordinator.data["hw-1"]["attributes"]["compressorHeatingCurrentStatus"] = 2
        self.assertTrue(compressor.is_on)
        boost = entities["hw-1_boost"]
        boost.coordinator.data["hw-1"]["attributes"]["boostStatus"] = 1.0
        self.assertTrue(boost.is_on)

    async def test_the_mode_sensor_reads_an_integral_float(self) -> None:
        from custom_components.addhon import sensor

        mode = next(e for e in await _build(sensor) if e._attr_unique_id == "hw-1_heat_pump_mode")
        mode.coordinator.data["hw-1"]["attributes"]["machMode"] = 1.0
        self.assertEqual(mode.native_value, "auto")

    async def test_no_hw_entity_without_its_attribute(self) -> None:
        from custom_components.addhon import sensor

        data = _data()
        data["hw-1"]["attributes"].pop("machMode")
        data["hw-1"]["attributes"].pop("remainingWaterLevel")
        from custom_components.addhon.const import DOMAIN

        coordinator = _Coordinator(data)
        hass = _Hass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
        added: list = []
        await sensor.async_setup_entry(hass, _Entry(), added.extend)
        keys = {e._attr_unique_id for e in added}
        self.assertNotIn("hw-1_heat_pump_mode", keys)
        self.assertNotIn("hw-1_hot_water_available", keys)
        self.assertNotIn("hw-1_heat_pump_state", keys)
        self.assertIn("hw-1_water_temp", keys)


def _vacation(data: dict, **overrides) -> dict:
    data["hw-1"]["attributes"].update(
        {"machMode": 4, "vacStartDate": "2026-10-10", "vacEndDate": "2026-10-20"}
    )
    data["hw-1"]["attributes"].update(overrides)
    return data


class HeatPumpVacationEntitiesTest(unittest.IsolatedAsyncioTestCase):
    """The vacation binary, the source mask and the two vacation dates."""

    async def _binaries(self, data: dict) -> dict:
        from custom_components.addhon import binary_sensor

        return {e._attr_unique_id: e for e in await _build(binary_sensor, data)}

    async def test_the_vacation_binary_follows_the_app_rule(self) -> None:
        entities = await self._binaries(_vacation(_data()))
        self.assertTrue(entities["hw-1_vacation_active"].is_on)
        cleared = await self._binaries(
            _vacation(_data(), vacStartDate="2000-01-01", vacEndDate="2000-01-01")
        )
        self.assertFalse(cleared["hw-1_vacation_active"].is_on)

    async def test_the_vacation_binary_reads_the_mode_alone_on_the_other_series(self) -> None:
        data = _vacation(_data("M8B"), vacStartDate="2000-01-01", vacEndDate="2000-01-01")
        entities = await self._binaries(data)
        self.assertTrue(entities["hw-1_vacation_active"].is_on)

    async def test_the_vacation_binary_has_no_device_class_and_is_enabled(self) -> None:
        from custom_components.addhon import binary_sensor

        entity = (await self._binaries(_data()))["hw-1_vacation_active"]
        self.assertIsNone(entity.entity_description.device_class)
        self.assertTrue(
            getattr(entity.entity_description, "entity_registry_enabled_default", True)
        )
        self.assertIsInstance(entity, binary_sensor.HonHeatPumpVacationBinarySensor)

    async def test_no_vacation_binary_without_the_mode(self) -> None:
        data = _data()
        data["hw-1"]["attributes"].pop("machMode")
        self.assertNotIn("hw-1_vacation_active", await self._binaries(data))

    async def test_the_compressor_reads_off_during_a_vacation(self) -> None:
        # `getEnergySources` (decomp.txt:2331127-2331182): the vacation masks the
        # compressor only; the electric row tests the disconnection alone, so the
        # resistance keeps following its own flag (user decision, 2026-10-06).
        data = _vacation(
            _data(), compressorHeatingCurrentStatus=1, electricHeatingCurrentStatus=1
        )
        entities = await self._binaries(data)
        self.assertFalse(entities["hw-1_compressor_heating"].is_on)
        self.assertTrue(entities["hw-1_electric_heating"].is_on)
        # Outside a vacation the same flags read as running.
        data["hw-1"]["attributes"]["machMode"] = 2
        self.assertTrue(entities["hw-1_compressor_heating"].is_on)
        self.assertTrue(entities["hw-1_electric_heating"].is_on)

    async def test_the_sources_follow_the_series_rule_too(self) -> None:
        data = _vacation(
            _data("m11"), compressorHeatingCurrentStatus=1,
            vacStartDate="2000-01-01", vacEndDate="2000-01-01",
        )
        entities = await self._binaries(data)
        self.assertFalse(entities["hw-1_compressor_heating"].is_on)

    async def test_a_missing_source_flag_stays_unknown_in_a_vacation(self) -> None:
        entities = await self._binaries(_vacation(_data()))
        compressor = entities["hw-1_compressor_heating"]
        compressor.coordinator.data["hw-1"]["attributes"].pop(
            "compressorHeatingCurrentStatus"
        )
        self.assertIsNone(compressor.is_on)

    async def test_a_disconnected_appliance_hides_the_sources(self) -> None:
        # The app's other mask (`applianceDisconnected`) is the entity's own
        # availability: the base entity goes unavailable, so no second rule here.
        data = _data()
        data["hw-1"]["attributes"].update(
            {"available": False, "compressorHeatingCurrentStatus": 1}
        )
        entities = await self._binaries(data)
        self.assertFalse(entities["hw-1_compressor_heating"].available)
        self.assertFalse(entities["hw-1_electric_heating"].available)

    async def test_the_vacation_dates(self) -> None:
        from datetime import date as calendar_date

        from custom_components.addhon import date

        data = _data()
        data["hw-1"]["attributes"].update(
            {"vacStartDate": "2026-10-10", "vacEndDate": "2000-01-01"}
        )
        entities = {e._attr_unique_id: e for e in await _build(date, data)}
        self.assertEqual(set(entities), {"hw-1_vacation_start", "hw-1_vacation_end"})
        self.assertEqual(
            entities["hw-1_vacation_start"].native_value, calendar_date(2026, 10, 10)
        )
        self.assertIsNone(entities["hw-1_vacation_end"].native_value)
        self.assertEqual(entities["hw-1_vacation_start"]._attr_translation_key, "vacation_start")
        self.assertEqual(entities["hw-1_vacation_end"]._attr_translation_key, "vacation_end")

    async def test_the_113_placeholder_dates_read_as_unknown(self) -> None:
        from custom_components.addhon import date

        entities = await _build(date)
        self.assertEqual(len(entities), 2)
        for entity in entities:
            self.assertIsNone(entity.native_value, entity._attr_unique_id)

    async def test_the_dates_are_read_only_for_now(self) -> None:
        from datetime import date as calendar_date

        from homeassistant.exceptions import HomeAssistantError

        from custom_components.addhon import date
        from custom_components.addhon.const import DOMAIN

        entity = (await _build(date))[0]
        with self.assertRaises(HomeAssistantError) as raised:
            await entity.async_set_value(calendar_date(2026, 10, 10))
        self.assertEqual(raised.exception.translation_domain, DOMAIN)
        self.assertEqual(raised.exception.translation_key, "vacation_dates_read_only")

    async def test_no_date_without_its_attribute_nor_on_another_type(self) -> None:
        from custom_components.addhon import date

        data = _data()
        data["hw-1"]["attributes"].pop("vacEndDate")
        keys = {e._attr_unique_id for e in await _build(date, data)}
        self.assertEqual(keys, {"hw-1_vacation_start"})
        other = _data()
        other["hw-1"]["type"] = "WH"
        self.assertEqual(await _build(date, other), [])


class HeatPumpScheduleEntitiesTest(unittest.IsolatedAsyncioTestCase):
    """The defrost binary, the sterilization time and the eco window."""

    async def test_defrost_is_a_disabled_running_binary(self) -> None:
        from custom_components.addhon import binary_sensor

        entities = {e._attr_unique_id: e for e in await _build(binary_sensor)}
        defrost = entities["hw-1_defrost"]
        description = defrost.entity_description
        self.assertEqual(description.attr_key, "autoDefrostStatus")
        self.assertEqual(description.device_class, binary_sensor.BinarySensorDeviceClass.RUNNING)
        self.assertFalse(description.entity_registry_enabled_default)
        self.assertFalse(defrost.is_on)
        defrost.coordinator.data["hw-1"]["attributes"]["autoDefrostStatus"] = 1.0
        self.assertTrue(defrost.is_on)

    async def test_the_sterilization_time_is_padded(self) -> None:
        from custom_components.addhon import sensor

        data = _data()
        data["hw-1"]["attributes"]["sterilizationTime"] = "15:0"
        entities = {e._attr_unique_id: e for e in await _build(sensor, data)}
        self.assertEqual(entities["hw-1_sterilization_time"].native_value, "15:00")

    async def test_the_eco_window_of_the_115_appliance(self) -> None:
        from custom_components.addhon import sensor

        data = _data()
        data["hw-1"]["attributes"].update(_ISSUE115)
        entities = {e._attr_unique_id: e for e in await _build(sensor, data)}
        window = entities["hw-1_eco_window"]
        with mock.patch.object(
            sensor.HonHeatPumpEcoWindowSensor, "_now", staticmethod(lambda: _at(3, 0))
        ):
            self.assertEqual(window.native_value, "00:15\u201323:45")
        self.assertEqual(window.extra_state_attributes["opp2_windows"], ["00:15\u201323:45"])
        self.assertEqual(window._attr_translation_key, "eco_window")

    async def test_the_eco_window_takes_the_series_of_the_appliance(self) -> None:
        from custom_components.addhon import sensor

        data = _data("M11")
        data["hw-1"]["attributes"].update(_ISSUE115)
        entities = {e._attr_unique_id: e for e in await _build(sensor, data)}
        with mock.patch.object(
            sensor.HonHeatPumpEcoWindowSensor, "_now", staticmethod(lambda: _at(3, 0))
        ):
            self.assertIsNone(entities["hw-1_eco_window"].native_value)

    def test_the_date_platform_is_set_up(self) -> None:
        from custom_components.addhon.const import PLATFORMS

        self.assertIn("date", PLATFORMS)

    async def test_no_eco_window_without_the_schedule(self) -> None:
        from custom_components.addhon import sensor

        data = _data()
        data["hw-1"]["attributes"].pop("opp1EcoDays")
        keys = {e._attr_unique_id for e in await _build(sensor, data)}
        self.assertNotIn("hw-1_eco_window", keys)
        self.assertIn("hw-1_heat_pump_state", keys)


# --- Energy counters (issue #115) -------------------------------------------------
# The three `...Year...` series and the appliance date, as the two reporters' m8
# appliances really published them (#113 once, #115 on three days).
ENERGY = REPO_ROOT / "tests" / "fixtures" / "hw_energy" / "series.json"
CP, EC, HEAT = "energyConsumptionYearCp", "energyConsumptionYearEc", "accumulatedHeatYear"
# Entity key -> the series it counts.
ENERGY_KEYS = {
    "total_energy": (CP, EC),
    "compressor_energy": (CP,),
    "heater_energy": (EC,),
    "heat_produced": (HEAT,),
}


def _snapshot(name: str) -> dict:
    return dict(json.loads(ENERGY.read_text(encoding="utf-8"))["snapshots"][name]["attributes"])


def _ha_counted(states) -> float:
    """kWh Home Assistant's long-term statistics add up for a TOTAL sensor.

    The rule of `sensor/recorder.py` for `state_class: total` without `last_reset`,
    identical in 2024.12.0 and 2026.9.4: a state that is not a number is skipped,
    the first number is the zero point, and every later number adds its difference
    to the previous one, negative included.
    """
    total, previous = 0.0, None
    for state in states:
        if isinstance(state, bool) or not isinstance(state, (int, float)):
            continue
        if previous is not None:
            total += state - previous
        previous = state
    return total


class YearWindowTest(unittest.TestCase):
    """A `...Year...` series: five yearly totals, oldest first, current year last."""

    def test_the_reporters_series(self) -> None:
        for name, expected in (
            ("113_2026-09-27", {CP: (0, 0, 0, 47, 72), EC: (0, 0, 0, 1, 15),
                                HEAT: (0, 0, 0, 338, 440)}),
            ("115_2026-09-29", {CP: (0, 0, 0, 215, 581), EC: (0, 0, 0, 100, 42),
                                HEAT: (0, 0, 0, 818, 2496)}),
            ("115_2026-10-03", {CP: (0, 0, 0, 215, 586), EC: (0, 0, 0, 100, 42),
                                HEAT: (0, 0, 0, 818, 2519)}),
        ):
            attributes = _snapshot(name)
            for series, window in expected.items():
                self.assertEqual(hpwh.year_window(attributes[series]), window, (name, series))

    def test_anything_but_five_finite_non_negative_numbers_is_none(self) -> None:
        # None, never 0: a 0 published to a counter is a restart of the meter.
        for raw in (
            None, "", "0;0;0;215", "0;0;0;215;581;0", "0;0;0;215;581;",
            "0;0;0;215;x", "0;0;0;215;-1", "0;0;0;215;nan", "0;0;0;215;inf",
            "0;0;0;215;5,5", "0,0,0,215,581", 581, 0, ["0", "0", "0", "215", "581"],
        ):
            self.assertIsNone(hpwh.year_window(raw), raw)

    def test_the_whole_series_at_zero_is_not_a_reading(self) -> None:
        # Doc 5 §7: the strict reader drops it, so a transient 0;0;0;0;0 never
        # reaches a counter (scenario S2).
        self.assertIsNone(hpwh.year_window("0;0;0;0;0"))

    def test_decimals_are_kept(self) -> None:
        self.assertEqual(hpwh.year_window("0;0;0;1.5;2.25"), (0.0, 0.0, 0.0, 1.5, 2.25))


class YearWindowsTest(unittest.TestCase):
    """The three series of one shadow, read together: when five zeros are a reading."""

    @staticmethod
    def _read(**series) -> dict:
        return hpwh.year_windows(mapping_getter(series))

    def test_five_zeros_are_a_reading_when_a_sibling_is_not_zero(self) -> None:
        # A backup element that has not run in five years: a real zero.
        windows = self._read(
            energyConsumptionYearCp="0;0;0;215;581", energyConsumptionYearEc="0;0;0;0;0",
            accumulatedHeatYear="0;0;0;818;2496",
        )
        self.assertEqual(
            windows,
            {CP: (0, 0, 0, 215, 581), EC: (0.0,) * 5, HEAT: (0, 0, 0, 818, 2496)},
        )

    def test_all_three_at_zero_together_is_no_reading(self) -> None:
        zeros = "0;0;0;0;0"
        windows = self._read(
            energyConsumptionYearCp=zeros, energyConsumptionYearEc=zeros,
            accumulatedHeatYear=zeros,
        )
        self.assertEqual(windows, {CP: None, EC: None, HEAT: None})

    def test_only_a_readable_sibling_with_a_non_zero_slot_counts(self) -> None:
        for siblings in (
            {},
            {"energyConsumptionYearCp": "0;0;0;215"},
            {"energyConsumptionYearCp": "0;0;0;0;0", "accumulatedHeatYear": "garbage"},
        ):
            windows = self._read(energyConsumptionYearEc="0;0;0;0;0", **siblings)
            self.assertIsNone(windows[EC], siblings)
        windows = self._read(
            energyConsumptionYearEc="0;0;0;0;0", accumulatedHeatYear="0;0;0;0;1"
        )
        self.assertEqual(windows[EC], (0.0,) * 5)

    def test_the_series_alone_still_reads_five_zeros_as_nothing(self) -> None:
        self.assertIsNone(hpwh.year_window("0;0;0;0;0"))
        self.assertEqual(hpwh.year_window("0;0;0;0;0", zeros=True), (0.0,) * 5)
        self.assertIsNone(hpwh.year_window("0;0;0;0", zeros=True))


class DeviceYearTest(unittest.TestCase):
    def test_the_year_of_the_appliance_date(self) -> None:
        self.assertEqual(hpwh.device_year("2026-09-30"), 2026)
        self.assertEqual(hpwh.device_year(" 2027-01-01 "), 2027)

    def test_anything_but_a_calendar_date_is_none(self) -> None:
        for raw in (None, "", "0000-00-00", "2026-13-01", "2026", "garbage", 2026):
            self.assertIsNone(hpwh.device_year(raw), raw)


class YearStepTest(unittest.TestCase):
    """The guard: which reading may move a counter, and by how much."""

    REF = hpwh.YearRef((0.0, 0.0, 0.0, 818.0, 2496.0), 2026)  # #115's heat, 2026-09-29

    def _step(self, raw: str, year: int | None = 2026, ref=None):
        return hpwh.year_step(self.REF if ref is None else ref, hpwh.year_window(raw), year)

    def test_the_first_reading_is_the_reference_and_gains_nothing(self) -> None:
        ref, gained, accepted = hpwh.year_step(
            hpwh.YearRef(), hpwh.year_window("0;0;0;818;2496"), 2026
        )
        self.assertEqual((ref, gained, accepted), (self.REF, 0.0, True))

    def test_growth_and_no_change(self) -> None:
        ref, gained, accepted = self._step("0;0;0;818;2499")
        self.assertEqual((ref.window[-1], ref.year, gained, accepted), (2499.0, 2026, 3.0, True))
        self.assertEqual(self._step("0;0;0;818;2496"), (self.REF, 0.0, True))

    def test_the_new_year_slide(self) -> None:
        # What all three m8 that crossed 2025 -> 2026 show as an outcome.
        ref, gained, accepted = self._step("0;0;818;2496;0")
        self.assertEqual((ref.year, gained, accepted), (2027, 0.0, True))
        # The old year's last kWh may arrive together with the slide.
        ref, gained, accepted = self._step("0;0;818;2497;2")
        self.assertEqual((gained, accepted), (3.0, True))

    def test_a_slide_before_the_date_still_moves_the_reference_year(self) -> None:
        # Doc 5 §6, S5: the series slides while the appliance date is still December.
        ref, _gained, _accepted = self._step("0;0;818;2496;0", year=2026)
        self.assertEqual(ref.year, 2027)
        ref, _gained, _accepted = self._step("0;0;818;2496;0", year=None)
        self.assertEqual(ref.year, 2027)

    def test_a_restart_in_place_needs_a_later_appliance_year(self) -> None:
        # S6: no slide, the current year restarts from 0 together with `date`.
        ref, gained, accepted = self._step("0;0;0;818;4", year=2027)
        self.assertEqual((ref.window[-1], ref.year, gained, accepted), (4.0, 2027, 4.0, True))
        # S6b: the same drop one message before `date` changes is held.
        self.assertEqual(self._step("0;0;0;818;0", year=2026), (self.REF, 0.0, False))
        self.assertEqual(self._step("0;0;0;818;0", year=None), (self.REF, 0.0, False))

    def test_every_other_drop_is_refused(self) -> None:
        for raw in (
            "0;0;0;818;0",     # S3: the current year alone at zero for one message
            "0;0;0;818;2490",  # an older value coming back
            "0;0;818;2495;0",  # a slide whose old year lost a kWh
            "0;818;2496;0;0",  # two years at once
        ):
            self.assertEqual(self._step(raw), (self.REF, 0.0, False), raw)

    def test_an_unreadable_series_leaves_the_reference(self) -> None:
        self.assertEqual(hpwh.year_step(self.REF, None, 2027), (self.REF, 0.0, False))

    def test_an_unknown_reference_year_is_filled_in(self) -> None:
        ref = hpwh.YearRef(self.REF.window, None)
        new, gained, accepted = hpwh.year_step(ref, hpwh.year_window("0;0;0;818;2497"), 2026)
        self.assertEqual((new.year, gained, accepted), (2026, 1.0, True))

    AFTER = hpwh.YearRef((0.0, 0.0, 818.0, 2496.0, 3.0), 2027)  # #115's heat, slid

    def test_a_series_from_before_the_slide_is_refused(self) -> None:
        # A pre-new-year series coming back after the slide is a RISE of the last
        # slot, and was worth +2493 kWh of heat on #115. Its shape gives it away: the
        # reference moved one slot to the right, the last slot no higher than the
        # reference's last year. With the appliance date, without it, or with the
        # date of before the new year coming back with it.
        for year in (2027, None, 2026):
            self.assertEqual(
                self._step("0;0;0;818;2496", year=year, ref=self.AFTER),
                (self.AFTER, 0.0, False),
                year,
            )
        # An older state of the same year, too.
        self.assertEqual(
            self._step("0;0;0;818;2490", year=2027, ref=self.AFTER), (self.AFTER, 0.0, False)
        )

    def test_a_date_before_the_reference_year_refuses_any_change(self) -> None:
        # Not the shape above, but the appliance says it is still last year.
        self.assertEqual(
            self._step("0;0;818;2496;5", year=2026, ref=self.AFTER), (self.AFTER, 0.0, False)
        )
        # The same series once the date has turned is growth.
        ref, gained, accepted = self._step("0;0;818;2496;5", year=2027, ref=self.AFTER)
        self.assertEqual((ref.window[-1], gained, accepted), (5.0, 2.0, True))
        # An unchanged series is no change, whatever the date: the poll re-reads it
        # between a slide and the date that follows it.
        self.assertEqual(
            self._step("0;0;818;2496;3", year=2026, ref=self.AFTER), (self.AFTER, 0.0, True)
        )

    def test_the_slide_and_the_growth_after_it_are_not_taken_for_it(self) -> None:
        ref, gained, accepted = self._step("0;0;818;2496;0", year=2027)
        self.assertEqual((ref.year, gained, accepted), (2027, 0.0, True))
        ref, gained, accepted = hpwh.year_step(ref, hpwh.year_window("0;0;818;2496;7"), 2027)
        self.assertEqual((ref.window[-1], gained, accepted), (7.0, 7.0, True))
        # A first-year appliance: its closed years are all zero, like the reference
        # shifted right, but growth leaves them as they are.
        first = hpwh.YearRef((0.0, 0.0, 0.0, 0.0, 5.0), 2026)
        _ref, gained, accepted = hpwh.year_step(first, hpwh.year_window("0;0;0;0;6"), 2026)
        self.assertEqual((gained, accepted), (1.0, True))

    def test_a_reference_of_zeros_grows_in_place(self) -> None:
        # An element that never ran: the first kWh is this year's, not a slide.
        zeros = hpwh.YearRef((0.0,) * 5, 2026)
        ref, gained, accepted = hpwh.year_step(zeros, hpwh.year_window("0;0;0;0;1"), 2026)
        self.assertEqual((ref.year, gained, accepted), (2026, 1.0, True))
        zero = hpwh.year_window("0;0;0;0;0", zeros=True)
        self.assertEqual(hpwh.year_step(zeros, zero, 2026), (zeros, 0.0, True))


class LifetimeTest(unittest.TestCase):
    """One counter: seeded with the app's five-year total, then accepted gains only."""

    def test_the_seed_is_the_five_year_total(self) -> None:
        # The app's years tab: `getTotalWh` over all five slots (decomp.txt:2996797-2996819).
        state = hpwh.lifetime_step(
            hpwh.Lifetime(), hpwh.year_window("0;0;0;215;581"), 2026, now=0.0
        )
        self.assertEqual((state.seed, state.total), (796.0, 796.0))
        self.assertEqual(state.ref, hpwh.YearRef((0.0, 0.0, 0.0, 215.0, 581.0), 2026))

    def test_nothing_before_a_readable_series(self) -> None:
        state = hpwh.lifetime_step(hpwh.Lifetime(), None, 2026, now=0.0)
        self.assertEqual(state, hpwh.Lifetime())
        self.assertIsNone(hpwh.lifetime_as_dict(state))

    def test_gains_add_up_and_a_drop_is_held(self) -> None:
        state = hpwh.Lifetime()
        for raw in ("0;0;0;215;581", "0;0;0;215;585", "0;0;0;215;0", "0;0;0;215;0",
                    "0;0;0;215;586"):
            state = hpwh.lifetime_step(state, hpwh.year_window(raw), 2026, now=0.0)
        self.assertEqual(state.total, 801.0)
        self.assertFalse(state.holding)
        # The drop is remembered, counted once though it was read twice.
        self.assertEqual(state.refused, (0.0, 0.0, 0.0, 215.0, 0.0))
        self.assertEqual(state.refusals, 1)

    def test_the_same_drop_coming_back_is_counted_again(self) -> None:
        state = hpwh.Lifetime()
        for raw in ("0;0;0;215;581", "0;0;0;215;0", "0;0;0;215;581", "0;0;0;215;0"):
            state = hpwh.lifetime_step(state, hpwh.year_window(raw), 2026, now=0.0)
        self.assertEqual((state.total, state.holding, state.refusals), (796.0, True, 2))

    ZEROS = (0.0,) * 5
    REAL = (0.0, 0.0, 0.0, 100.0, 42.0)  # #115's element

    HOUR = 3600.0

    def _run(self, *readings) -> "hpwh.Lifetime":
        """`readings` are (window, monotonic seconds) pairs."""
        state = hpwh.Lifetime()
        for window, now in readings:
            state = hpwh.lifetime_step(state, window, 2026, now=now)
        return state

    def test_five_zeros_seed_only_after_an_hour_without_a_break(self) -> None:
        zeros, t0 = self.ZEROS, 500.0
        self.assertIsNone(self._run((zeros, t0)).total)
        self.assertIsNone(self._run((zeros, t0), (zeros, t0 + 59 * 60)).total)
        state = self._run((zeros, t0), (zeros, t0 + 1800), (zeros, t0 + self.HOUR))
        self.assertEqual((state.seed, state.total, state.ref.window), (0.0, 0.0, zeros))
        # The hour is measured from the first zero, not counted in readings: a poll
        # every ten seconds for 59 minutes is still no seed.
        many = [(zeros, t0 + 10 * i) for i in range(355)]
        self.assertIsNone(self._run(*many).total)

    def test_a_single_zero_before_the_real_series_is_no_seed(self) -> None:
        state = self._run((self.ZEROS, 0.0), (self.REAL, 60.0))
        self.assertEqual((state.seed, state.total, state.refusals), (142.0, 142.0, 0))

    def test_zero_for_half_an_hour_then_the_real_series(self) -> None:
        state = self._run((self.ZEROS, 0.0), (self.ZEROS, 1800.0), (self.REAL, 1860.0))
        self.assertEqual((state.seed, state.total, state.refusals), (142.0, 142.0, 0))

    def test_a_series_that_is_not_zero_in_between_seeds_at_once(self) -> None:
        # The real series is the seed, and the zero after it, an hour after the
        # first one, is a refused drop: the clock does not run past a seed.
        state = self._run(
            (self.ZEROS, 0.0), (self.ZEROS, 1800.0), (self.REAL, 1860.0),
            (self.ZEROS, self.HOUR + 100),
        )
        self.assertEqual((state.seed, state.total, state.refusals), (142.0, 142.0, 1))

    def test_an_unreadable_series_neither_counts_nor_resets(self) -> None:
        zeros = self.ZEROS
        self.assertIsNone(self._run((zeros, 0.0), (None, 1800.0), (zeros, 3599.0)).total)
        self.assertEqual(
            self._run((zeros, 0.0), (None, 1800.0), (zeros, self.HOUR)).total, 0.0
        )

    def test_after_the_seed_five_zeros_are_judged_at_once(self) -> None:
        # The rule is for the seed only: once seeded, the counter grows from zeros
        # at the first reading, as before.
        state = self._run(
            (self.ZEROS, 0.0), (self.ZEROS, self.HOUR), ((0.0, 0.0, 0.0, 0.0, 1.0), 3601.0)
        )
        self.assertEqual(state.total, 1.0)

    def test_the_timer_is_not_part_of_the_restore_record(self) -> None:
        self.assertIsNone(hpwh.lifetime_as_dict(self._run((self.ZEROS, 0.0))))
        seeded = self._run((self.ZEROS, 0.0), (self.ZEROS, self.HOUR))
        record = hpwh.lifetime_as_dict(seeded)
        self.assertNotIn("zero_since", record)
        self.assertIsNone(hpwh.lifetime_from_dict(record).zero_since)

    def test_the_restore_record_round_trips(self) -> None:
        state = hpwh.Lifetime()
        for raw in ("0;0;0;215;581", "0;0;0;215;585", "0;0;0;215;0"):
            state = hpwh.lifetime_step(state, hpwh.year_window(raw), 2026, now=0.0)
        record = hpwh.lifetime_as_dict(state)
        self.assertEqual(
            record,
            {"seed": 796.0, "total": 800.0, "reference": [0.0, 0.0, 0.0, 215.0, 585.0],
             "year": 2026, "holding": True, "refused": [0.0, 0.0, 0.0, 215.0, 0.0],
             "refusals": 1},
        )
        json.dumps(record)
        self.assertEqual(hpwh.lifetime_from_dict(record), state)

    def test_a_damaged_record_starts_over(self) -> None:
        good = {"seed": 796.0, "total": 800.0, "reference": [0, 0, 0, 215, 585], "year": 2026}
        self.assertEqual(hpwh.lifetime_from_dict(good).total, 800.0)
        for bad in (
            None, [], {}, {**good, "total": -1}, {**good, "total": "800"},
            {**good, "total": True}, {**good, "total": float("nan")},
            {**good, "seed": None}, {**good, "reference": "0;0;0;215;585"},
            {**good, "reference": [0, 0, 0, 585]}, {**good, "reference": [0, 0, 0, -1, 5]},
        ):
            self.assertEqual(hpwh.lifetime_from_dict(bad), hpwh.Lifetime(), bad)
        # Five zeros are a reference `year_windows` may have accepted: kept.
        zeros = hpwh.lifetime_from_dict({**good, "seed": 0, "total": 3, "reference": [0] * 5})
        self.assertEqual((zeros.total, zeros.ref.window), (3.0, (0.0,) * 5))
        # The optional fields fall back one by one.
        fallback = hpwh.lifetime_from_dict(
            {**good, "year": "2026", "holding": "yes", "refused": [1], "refusals": -2}
        )
        self.assertEqual(
            (fallback.ref.year, fallback.holding, fallback.refused, fallback.refusals),
            (None, False, None, 0),
        )

    def test_the_tables(self) -> None:
        self.assertEqual(hpwh.HPWH_ENERGY_COUNTERS, ENERGY_KEYS)
        self.assertEqual(hpwh.HPWH_ENERGY_ATTRS, (CP, EC, HEAT, "date"))


class _StoredData:
    """What Home Assistant hands back from a previous run (`RestoredExtraData`)."""

    def __init__(self, data) -> None:
        self._data = data

    def as_dict(self):
        return self._data


def _energy_data(attributes: dict) -> dict:
    appliance = types.SimpleNamespace(model_attributes={"series": "m8"})
    return {
        "hw-1": {
            "type": "HW",
            "name": "Termo",
            "attributes": dict(attributes),
            "settings": {},
            "appliance": appliance,
        }
    }


class HeatPumpEnergyEntitiesTest(unittest.IsolatedAsyncioTestCase):
    """The four energy counters of the heat-pump water heater (#115)."""

    async def _energy(
        self, attributes: dict | None = None, *, stored: dict | None = None,
        experimental: bool = False,
    ) -> dict:
        from custom_components.addhon import sensor
        from custom_components.addhon.const import CONF_ENABLE_EXPERIMENTAL, DOMAIN

        data = _energy_data(_snapshot("115_2026-09-29") if attributes is None else attributes)
        coordinator = _Coordinator(data)
        hass = _Hass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
        entry = _Entry()
        entry.options = {CONF_ENABLE_EXPERIMENTAL: experimental}
        added: list = []
        await sensor.async_setup_entry(hass, entry, added.extend)
        entities = {
            key: e
            for e in added
            if (key := str(getattr(e, "_attr_unique_id", "")).removeprefix("hw-1_"))
            in ENERGY_KEYS
        }
        for key, entity in entities.items():
            previous = None if stored is None else stored.get(key)

            async def _last(previous=previous):
                return None if previous is None else _StoredData(previous)

            with mock.patch.object(entity, "async_get_last_extra_data", _last):
                await entity.async_added_to_hass()
        return entities

    @staticmethod
    def _push(entity, **attributes) -> object:
        """One coordinator update carrying `attributes`; None removes one."""
        current = entity.coordinator.data["hw-1"]["attributes"]
        for name, value in attributes.items():
            if value is None:
                current.pop(name, None)
            else:
                current[name] = value
        entity._handle_coordinator_update()
        return entity.native_value

    @staticmethod
    def _values(entities: dict) -> dict:
        return {key: entity.native_value for key, entity in entities.items()}

    async def test_the_values_on_the_reporters_appliances_at_first_start(self) -> None:
        # The seed is the app's five-year total: 938 kWh on #115 is the `total` of
        # the app's years tab (Cp + Ec, decomp.txt:2996797-2996819).
        for name, expected in (
            ("113_2026-09-27", {"total_energy": 135.0, "compressor_energy": 119.0,
                                "heater_energy": 16.0, "heat_produced": 778.0}),
            ("115_2026-09-29", {"total_energy": 938.0, "compressor_energy": 796.0,
                                "heater_energy": 142.0, "heat_produced": 3314.0}),
            ("115_2026-10-02", {"total_energy": 942.0, "compressor_energy": 800.0,
                                "heater_energy": 142.0, "heat_produced": 3334.0}),
            ("115_2026-10-03", {"total_energy": 943.0, "compressor_energy": 801.0,
                                "heater_energy": 142.0, "heat_produced": 3337.0}),
        ):
            self.assertEqual(self._values(await self._energy(_snapshot(name))), expected, name)

    async def test_the_reporters_days_one_after_the_other(self) -> None:
        entities = await self._energy(_snapshot("115_2026-09-29"))
        for name, expected in (
            ("115_2026-10-02", {"total_energy": 942.0, "compressor_energy": 800.0,
                                "heater_energy": 142.0, "heat_produced": 3334.0}),
            ("115_2026-10-03", {"total_energy": 943.0, "compressor_energy": 801.0,
                                "heater_energy": 142.0, "heat_produced": 3337.0}),
        ):
            for entity in entities.values():
                self._push(entity, **_snapshot(name))
            self.assertEqual(self._values(entities), expected, name)

    async def test_what_each_entity_declares(self) -> None:
        from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

        entities = await self._energy()
        self.assertEqual(set(entities), set(ENERGY_KEYS))
        for key, entity in entities.items():
            self.assertEqual(entity._attr_unique_id, f"hw-1_{key}")
            self.assertEqual(entity._attr_translation_key, key)
            self.assertEqual(entity._attr_native_unit_of_measurement, "kWh", key)
            self.assertEqual(entity._attr_state_class, SensorStateClass.TOTAL, key)
            self.assertEqual(entity._counted, ENERGY_KEYS[key])
        for key in ("total_energy", "compressor_energy", "heater_energy"):
            self.assertEqual(entities[key]._attr_device_class, SensorDeviceClass.ENERGY, key)
        # Heat is not a consumption: no device class.
        self.assertIsNone(entities["heat_produced"]._attr_device_class)
        enabled = {
            key: getattr(entity, "_attr_entity_registry_enabled_default", True)
            for key, entity in entities.items()
        }
        self.assertEqual(
            enabled,
            {"total_energy": True, "compressor_energy": False, "heater_energy": False,
             "heat_produced": True},
        )

    async def test_not_behind_the_experimental_option(self) -> None:
        self.assertEqual(set(await self._energy(experimental=False)), set(ENERGY_KEYS))
        self.assertEqual(set(await self._energy(experimental=True)), set(ENERGY_KEYS))

    async def test_an_entity_exists_only_with_every_series_it_counts(self) -> None:
        attributes = _snapshot("115_2026-09-29")
        attributes.pop(EC)
        self.assertEqual(set(await self._energy(attributes)), {"compressor_energy", "heat_produced"})
        # The date is not required: the guard only loses its second test.
        attributes = _snapshot("115_2026-09-29")
        attributes.pop("date")
        self.assertEqual(set(await self._energy(attributes)), set(ENERGY_KEYS))

    async def test_another_type_gets_none(self) -> None:
        from custom_components.addhon import sensor
        from custom_components.addhon.const import DOMAIN

        data = _energy_data(_snapshot("115_2026-09-29"))
        data["hw-1"]["type"] = "WH"
        coordinator = _Coordinator(data)
        hass = _Hass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
        added: list = []
        await sensor.async_setup_entry(hass, _Entry(), added.extend)
        self.assertFalse(
            [e for e in added if isinstance(e, sensor.HonHeatPumpEnergySensor)]
        )

    async def test_unknown_until_every_series_is_readable(self) -> None:
        attributes = _snapshot("115_2026-09-29")
        attributes[EC] = "0;0;0;100"
        entities = await self._energy(attributes)
        self.assertIsNone(entities["total_energy"].native_value)
        self.assertIsNone(entities["heater_energy"].native_value)
        self.assertEqual(entities["compressor_energy"].native_value, 796.0)
        # The first readable series seeds its counter, and the sum appears.
        total = entities["total_energy"]
        self.assertEqual(self._push(total, energyConsumptionYearEc="0;0;0;100;42"), 938.0)

    @staticmethod
    def _clock(clock: list):
        """Patch the counters' monotonic clock to read `clock[0]`, in seconds."""
        from custom_components.addhon import sensor

        return mock.patch.object(
            sensor.HonHeatPumpEnergySensor, "_monotonic", staticmethod(lambda: clock[0])
        )

    async def test_a_series_of_zeros_counts_when_a_sibling_is_not_zero(self) -> None:
        # A stable zero: the seed once it has been seen for an hour without a break.
        attributes = _snapshot("115_2026-09-29")
        attributes[EC] = "0;0;0;0;0"
        clock = [1000.0]
        with self._clock(clock):
            entities = await self._energy(attributes)
            heater, total = entities["heater_energy"], entities["total_energy"]
            self.assertEqual((heater.native_value, total.native_value), (None, None))
            clock[0] += 59 * 60
            self.assertEqual((self._push(heater), self._push(total)), (None, None))
            clock[0] += 60
            self.assertEqual((self._push(heater), self._push(total)), (0.0, 796.0))
            # Its first kWh is counted.
            self.assertEqual(self._push(total, energyConsumptionYearEc="0;0;0;0;1"), 797.0)

    async def test_a_zero_for_half_an_hour_then_the_real_series(self) -> None:
        # A transient at the first start without restore data: no seed at zero, so
        # no phantom; the seed is the real series.
        attributes = _snapshot("115_2026-09-29")
        attributes[EC] = "0;0;0;0;0"
        clock = [0.0]
        with self._clock(clock):
            entities = await self._energy(attributes)
            published = {key: [entity.native_value] for key, entity in entities.items()}
            clock[0] = 1800.0
            for entity in entities.values():
                published[entity._key].append(self._push(entity))
            clock[0] = 1860.0
            for entity in entities.values():
                self._push(entity, energyConsumptionYearEc="0;0;0;100;42")
                published[entity._key].append(entity.native_value)
        self.assertEqual(published["heater_energy"], [None, None, 142.0])
        self.assertEqual(published["total_energy"], [None, None, 938.0])
        for key, states in published.items():
            self.assertEqual(_ha_counted(states), 0, key)
        self.assertEqual(entities["heater_energy"]._counters[EC].seed, 142.0)

    async def test_a_real_series_in_between_seeds_at_once(self) -> None:
        attributes = _snapshot("115_2026-09-29")
        attributes[EC] = "0;0;0;0;0"
        clock = [0.0]
        with self._clock(clock):
            heater = (await self._energy(attributes))["heater_energy"]
            clock[0] = 1800.0
            self.assertIsNone(self._push(heater))
            clock[0] = 1860.0
            self.assertEqual(
                self._push(heater, energyConsumptionYearEc="0;0;0;100;42"), 142.0
            )
            clock[0] = 3700.0
            self.assertEqual(self._push(heater, energyConsumptionYearEc="0;0;0;0;0"), 142.0)
        self.assertEqual(heater._counters[EC].refusals, 1)

    async def test_all_three_at_zero_is_no_reading(self) -> None:
        zeros = "0;0;0;0;0"
        attributes = {"date": "2026-09-30", CP: zeros, EC: zeros, HEAT: zeros}
        self.assertEqual(
            self._values(await self._energy(attributes)), dict.fromkeys(ENERGY_KEYS)
        )

    async def test_a_transient_at_zero_after_the_seed_moves_nothing(self) -> None:
        entities = await self._energy()
        zeros = "0;0;0;0;0"
        # All three at once: no reading, no refusal.
        for entity in entities.values():
            self._push(entity, **{CP: zeros, EC: zeros, HEAT: zeros})
        self.assertEqual(
            self._values(entities),
            {"total_energy": 938.0, "compressor_energy": 796.0, "heater_energy": 142.0,
             "heat_produced": 3314.0},
        )
        self.assertEqual(entities["heat_produced"]._counters[HEAT].refusals, 0)
        # One alone, beside live siblings: a reading, and a drop the guard refuses.
        heater = entities["heater_energy"]
        self._push(heater, **{CP: "0;0;0;215;581", EC: "0;0;0;100;42",
                              HEAT: "0;0;0;818;2496"})
        self.assertEqual(self._push(heater, energyConsumptionYearEc=zeros), 142.0)
        self.assertEqual(heater._counters[EC].refusals, 1)
        self.assertEqual(self._push(heater, energyConsumptionYearEc="0;0;0;100;42"), 142.0)

    async def test_the_series_from_before_the_slide_is_refused(self) -> None:
        # #115's heat at the year's end, slid, and the old series coming back: with
        # the new date, without any date, and with the old date coming back too.
        for date in ("2027-01-01", None, "2026-12-31"):
            start = _snapshot("115_2026-09-29")
            start["date"] = "2026-12-31"
            heat = (await self._energy(start))["heat_produced"]
            self.assertEqual(
                self._push(heat, date="2027-01-01", accumulatedHeatYear="0;0;818;2496;3"),
                3317.0,
            )
            # `date=None` takes the attribute out of the shadow.
            self.assertEqual(
                self._push(heat, date=date, accumulatedHeatYear="0;0;0;818;2496"),
                3317.0,
                date,
            )
            self.assertEqual(heat._counters[HEAT].refusals, 1, date)
            # Real growth after it is counted from the slid series.
            self.assertEqual(
                self._push(heat, date="2027-01-02", accumulatedHeatYear="0;0;818;2496;5"),
                3319.0,
                date,
            )

    async def test_a_drop_is_held_and_said_once(self) -> None:
        heat = (await self._energy())["heat_produced"]
        with self.assertLogs("custom_components.addhon.sensor", level="INFO") as logs:
            self.assertEqual(self._push(heat, accumulatedHeatYear="0;0;0;818;0"), 3314.0)
            self.assertEqual(self._push(heat, accumulatedHeatYear="0;0;0;818;0"), 3314.0)
        self.assertEqual(len(logs.records), 1, logs.output)
        self.assertIn("accumulatedHeatYear", logs.output[0])
        self.assertEqual(self._push(heat, accumulatedHeatYear="0;0;0;818;2497"), 3315.0)

    async def test_an_unreadable_series_keeps_the_counter(self) -> None:
        heater = (await self._energy())["heater_energy"]
        self.assertEqual(self._push(heater, energyConsumptionYearEc="0;0;0;100"), 142.0)
        self.assertEqual(self._push(heater, energyConsumptionYearEc=None), 142.0)
        # The reference survived the gap: a drop right after it is still refused.
        self.assertEqual(self._push(heater, energyConsumptionYearEc="0;0;0;100;0"), 142.0)
        self.assertEqual(self._push(heater, energyConsumptionYearEc="0;0;0;100;43"), 143.0)

    async def test_a_previous_run_is_continued(self) -> None:
        stored = {"heat_produced": {HEAT: {
            "seed": 3314.0, "total": 3320.0, "reference": [0, 0, 0, 818, 2502], "year": 2026,
        }}}
        attributes = _snapshot("115_2026-09-29")
        attributes[HEAT] = "0;0;0;818;2505"
        heat = (await self._energy(attributes, stored=stored))["heat_produced"]
        self.assertEqual(heat.native_value, 3323.0)

    async def test_a_previous_run_survives_a_drop_at_start(self) -> None:
        stored = {"heat_produced": {HEAT: {
            "seed": 3314.0, "total": 3314.0, "reference": [0, 0, 0, 818, 2496], "year": 2026,
        }}}
        attributes = _snapshot("115_2026-09-29")
        attributes[HEAT] = "0;0;0;818;0"
        heat = (await self._energy(attributes, stored=stored))["heat_produced"]
        self.assertEqual(heat.native_value, 3314.0)

    async def test_without_a_previous_run_the_first_reading_is_the_seed(self) -> None:
        # The one window the guard cannot see (doc 5 §6, restart without restore
        # data): a transient at the very first reading becomes the seed, and the
        # real value after it is counted as a gain.
        attributes = _snapshot("115_2026-09-29")
        attributes[HEAT] = "0;0;0;818;0"
        heat = (await self._energy(attributes))["heat_produced"]
        self.assertEqual(heat.native_value, 818.0)
        self.assertEqual(self._push(heat, accumulatedHeatYear="0;0;0;818;2496"), 3314.0)

    async def test_a_damaged_previous_run_starts_over(self) -> None:
        stored = {"heat_produced": {HEAT: {"total": "garbage"}}, "total_energy": "garbage"}
        entities = await self._energy(stored=stored)
        self.assertEqual(entities["heat_produced"].native_value, 3314.0)
        self.assertEqual(entities["total_energy"].native_value, 938.0)

    async def test_what_is_stored_for_the_next_start(self) -> None:
        total = (await self._energy())["total_energy"]
        self._push(total, energyConsumptionYearCp="0;0;0;215;0")
        record = total.extra_restore_state_data.as_dict()
        self.assertEqual(set(record), {CP, EC})
        self.assertEqual(
            record[CP],
            {"seed": 796.0, "total": 796.0, "reference": [0.0, 0.0, 0.0, 215.0, 581.0],
             "year": 2026, "holding": True, "refused": [0.0, 0.0, 0.0, 215.0, 0.0],
             "refusals": 1},
        )
        json.dumps(record)

    async def test_nothing_is_stored_before_a_readable_series(self) -> None:
        attributes = _snapshot("115_2026-09-29")
        attributes[HEAT] = "0;0;0"
        heat = (await self._energy(attributes))["heat_produced"]
        self.assertIsNone(heat.native_value)
        self.assertIsNone(heat.extra_restore_state_data)

    async def test_the_counters_are_left_for_the_diagnostics(self) -> None:
        entities = await self._energy()
        self._push(entities["heat_produced"], accumulatedHeatYear="0;0;0;818;0")
        store = entities["heat_produced"].coordinator.hpwh_energy_counters["hw-1"]
        self.assertEqual(set(store), set(ENERGY_KEYS))
        self.assertEqual(set(store["total_energy"]), {CP, EC})
        heat = store["heat_produced"][HEAT]
        self.assertEqual((heat["seed"], heat["total"], heat["holding"]), (3314.0, 3314.0, True))

    async def _scenario(self, key: str, start: dict, messages) -> tuple[list, float]:
        """Publish `messages` to `key`; return the states and what HA counts."""
        entity = (await self._energy(start))[key]
        published = [entity.native_value]
        for message in messages:
            published.append(self._push(entity, **message))
        numbers = [s for s in published if isinstance(s, float)]
        self.assertEqual(numbers, sorted(numbers), published)  # never decreasing
        return published, _ha_counted(published)

    async def test_doc5_scenarios_count_only_what_the_appliance_used(self) -> None:
        # Doc 5 §6, with year-end values (Cp 790, Ec 60, heat 3300). Each case:
        # the series messages, and the kWh really used across them.
        december = {"date": "2026-12-31", CP: "0;0;0;215;790", EC: "0;0;0;100;60",
                    HEAT: "0;0;0;818;3300"}
        sixth = {"date": "2026-12-31", CP: "700;760;800;780;790", EC: "50;40;30;20;60",
                 HEAT: "3000;3100;3200;3250;3300"}
        cases = (
            ("S2 all three at zero, then back", "total_energy", december,
             [{CP: "0;0;0;0;0", EC: "0;0;0;0;0", HEAT: "0;0;0;0;0"},
              {CP: "0;0;0;215;790", EC: "0;0;0;100;60", HEAT: "0;0;0;818;3300"},
              {CP: "0;0;0;215;791"}], 1),
            ("S2 heat alone at zero, then back", "heat_produced", december,
             [{HEAT: "0;0;0;0;0"}, {HEAT: "0;0;0;818;3300"}, {HEAT: "0;0;0;818;3301"}], 1),
            ("S3 current year at zero", "total_energy", december,
             [{CP: "0;0;0;215;0"}, {EC: "0;0;0;100;0"}, {CP: "0;0;0;215;790"},
              {EC: "0;0;0;100;60"}, {CP: "0;0;0;215;791"}], 1),
            ("S4 new year, one message", "total_energy", december,
             [{"date": "2027-01-01", CP: "0;0;215;790;0", EC: "0;0;100;60;0"},
              {CP: "0;0;215;790;2"}], 2),
            ("S5 Cp, then Ec, then date", "total_energy", december,
             [{CP: "0;0;215;790;0"}, {EC: "0;0;100;60;0"}, {"date": "2027-01-01"},
              {CP: "0;0;215;790;1"}, {EC: "0;0;100;60;1"}], 2),
            ("S5 heat slides with the old date", "heat_produced", december,
             [{HEAT: "0;0;818;3301;0"}, {"date": "2027-01-01"}, {HEAT: "0;0;818;3301;4"}], 5),
            ("S6 restart in place with date", "total_energy", december,
             [{"date": "2027-01-01", CP: "0;0;0;215;0", EC: "0;0;0;100;0"},
              {CP: "0;0;0;215;2"}], 2),
            ("S6b restart in place, date after", "total_energy", december,
             [{CP: "0;0;0;215;0", EC: "0;0;0;100;0"}, {"date": "2027-01-01"},
              {CP: "0;0;0;215;3"}], 3),
            ("S7 sixth year", "total_energy", sixth,
             [{"date": "2027-01-01", CP: "760;800;780;790;0", EC: "40;30;20;60;0"},
              {CP: "760;800;780;790;4"}], 4),
            ("S9 series stuck", "total_energy", december,
             [{"date": "2027-01-02"}, {"date": "2027-01-03"}], 0),
        )
        for name, key, start, messages, used in cases:
            with self.subTest(name), self.assertNoLogs(level="WARNING"):
                _published, counted = await self._scenario(key, dict(start), messages)
                self.assertEqual(counted, used)

    async def test_doc5_s8_a_correction_down_is_held_not_counted(self) -> None:
        # S8: the backup element's year corrected from 50 to 42. The counter holds
        # and counts again only above 50: the correction is never energy, and of the
        # 11 kWh used after it (42 -> 53) the 8 that bring the year back to 50 are
        # not counted -- the price of the guard, as in doc 5 §6.
        start = {"date": "2026-11-01", CP: "0;0;0;215;500", EC: "0;0;0;100;50",
                 HEAT: "0;0;0;818;2000"}
        published, counted = await self._scenario(
            "heater_energy", start,
            [{EC: "0;0;0;100;42"}, {EC: "0;0;0;100;49"}, {EC: "0;0;0;100;53"}],
        )
        self.assertEqual(published, [150.0, 150.0, 150.0, 153.0])
        self.assertEqual(counted, 3)

    async def test_ha_never_counts_a_drop_where_the_raw_slot_would(self) -> None:
        heat = (await self._energy())["heat_produced"]
        messages = ("0;0;0;818;2497", "0;0;0;818;0", "0;0;0;818;2497", "0;0;0;0;0",
                    "0;0;0;818;2499", "0;0;0;818;2490", "0;0;0;818;2500",
                    "0;0;818;2500;0", "0;0;818;2500;3", "0;0;818", "0;0;818;2500;5")
        published = [heat.native_value]
        raw_last = [2496.0]
        for raw in messages:
            # The new year arrives with the slide.
            extra = {"date": "2027-01-01"} if raw == "0;0;818;2500;0" else {}
            published.append(self._push(heat, accumulatedHeatYear=raw, **extra))
            window = hpwh.year_window(raw)
            raw_last.append(None if window is None else window[-1])
        self.assertEqual(_ha_counted(published), 9)
        # The last slot read as it is, on a TOTAL_INCREASING sensor, is worth a
        # whole year of phantom kWh over the same messages (the reset rule of
        # `sensor/recorder.py`: under 90 % of the previous value, count it all).
        phantom, previous = 0.0, None
        for state in raw_last:
            if state is None:
                continue
            if previous is not None:
                phantom += state if state < 0.9 * previous else state - previous
            previous = state
        self.assertGreater(phantom - 9, 2400)


if __name__ == "__main__":
    unittest.main()
