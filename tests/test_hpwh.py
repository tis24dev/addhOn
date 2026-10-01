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


def _data() -> dict:
    fixture = _fixture()
    appliance = types.SimpleNamespace(model_attributes={"series": fixture["series"]})
    return {
        "hw-1": {
            "type": fixture["type"],
            "name": "Warmtepomp boiler",
            "attributes": dict(fixture["attributes"]),
            "settings": {},
            "appliance": appliance,
        }
    }


async def _build(platform) -> list:
    from custom_components.addhon.const import DOMAIN

    coordinator = _Coordinator(_data())
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
                "hw-1_heat_pump_state": "off",
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


if __name__ == "__main__":
    unittest.main()
