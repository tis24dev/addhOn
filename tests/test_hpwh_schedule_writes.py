# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Heat-pump water heater (type HW): the scheduling writes of block 5.

User decisions of 2026-10-06: vacation dates (two `date` entities and a clear
button), sterilization (switch, `time`, two numbers) and the eco windows (the
`addhon.set_eco_schedule` service), all behind the experimental option, all on the
plain m7/m8 series only, all through the sparse `HPWH` send profile. What the app
sends is cited from apk2/decomp.txt (app 2.30.7) in `hpwh.py`; here the payloads are
pinned on the #115 HP150M8-9's own values, entity by entity, and once more through
the real loader and the real dispatcher on its rebuilt `settings` catalogue.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import types
import unittest
from datetime import date, time
from pathlib import Path
from unittest import mock

from tests.test_hpwh import _install_package_stubs, _mod  # same stubs, one place

_install_package_stubs()

from tests.test_hpwh_water_heater import platform as water_heater  # noqa: E402  (HA stubs)

from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from custom_components.addhon import button as button_platform  # noqa: E402
from custom_components.addhon import command_diagnostics  # noqa: E402
from custom_components.addhon import date as date_platform  # noqa: E402
from custom_components.addhon import hpwh  # noqa: E402
from custom_components.addhon import number as number_platform  # noqa: E402
from custom_components.addhon import switch as switch_platform  # noqa: E402
from custom_components.addhon import time as time_platform  # noqa: E402
from custom_components.addhon.const import CONF_ENABLE_EXPERIMENTAL, DOMAIN  # noqa: E402

# Kept before setUpModule replaces it: the registration has its own test below.
_REGISTER_ECO_SERVICE = getattr(water_heater, "_register_eco_schedule_service", None)
REPO = Path(__file__).resolve().parents[1]
ATTRIBUTES = REPO / "tests" / "fixtures" / "hw_hp110m8" / "attributes.json"
CATALOGUE = REPO / "tests" / "fixtures" / "hw_hp150m8"

# The #115 HP150M8-9 as its 5.26.0-beta9 diagnostics show it (2026-10-02): Eco, one
# window 00:15-23:45 every day, vacation cleared, sterilization off, weekly, 15:00,
# 65 °C. temp/tempSel from the phase-B test of 2026-10-03.
ISSUE115 = {
    "onOffStatus": 1,
    "machMode": 2,
    "temp": 55,
    "tempSel": 60,
    "offPeakPeriodScheme": 1,
    "opp1EcoDays": "7F",
    "opp2EcoStartTime1": "00:15",
    "opp2EcoEndTime1": "23:45",
    "vacStartDate": "2000-01-01",
    "vacEndDate": "2000-01-01",
    "sterilizationStatus": 0,
    "sterilizationInterval": 2,
    "sterilizationTime": "15:00",
    "sterilizationTempSel": 65.0,
}
WINDOW_KEYS = [
    f"opp{period}Eco{edge}Time{slot}"
    for period in (1, 2) for slot in (1, 2, 3) for edge in ("Start", "End")
]
SENT: list = []


def _range(value, low, high, step=1):
    return types.SimpleNamespace(value=value, min=low, max=high, step=step)


def _fixed(value):
    return types.SimpleNamespace(value=value)


def _appliance(series: str | None = "m8", drop: tuple[str, ...] = ()):
    """The #115 schema's typing (`settings.json`): what each write needs is there."""
    parameters = {
        "tempSel": _range(60, 35, 75), "onOffStatus": _range(1, 0, 1),
        "boostStatus": _range(0, 0, 1), "operationName": _fixed("grTimingPowerOnOff"),
        "vacStartDate": _fixed("0000-00-00"), "vacEndDate": _fixed("0000-00-00"),
        "sterilizationStatus": _range(0, 0, 1), "sterilizationInterval": _range(2, 1, 3),
        "sterilizationTime": _fixed("00:00"), "sterilizationTempSel": _range(65, 55, 75),
        "offPeakPeriodScheme": _range(1, 0, 1), "opp1EcoDays": _range(0, 0, 40),
        **{key: _fixed("00:00") for key in WINDOW_KEYS},
    }
    for key in drop:
        parameters.pop(key)
    categories = {
        name: types.SimpleNamespace(parameters={
            "machMode": types.SimpleNamespace(value=code, schema_value=code)})
        for name, code in (("auto", "1"), ("eco", "2"), ("elec", "3"), ("vac", "4"))
    }
    return types.SimpleNamespace(
        commands={
            "settings": types.SimpleNamespace(parameters=parameters),
            "startProgram": types.SimpleNamespace(categories=categories, parameters={}),
        },
        model_attributes={} if series is None else {"series": series},
    )


class _Coordinator:
    def __init__(self, data) -> None:
        self.data = data
        self.hass = None
        self.last_update_success = True

    def async_add_listener(self, update_callback):
        return lambda: None


class _Entry:
    entry_id = "entry-1"

    def __init__(self, experimental: bool) -> None:
        self.options = {CONF_ENABLE_EXPERIMENTAL: experimental}

    def async_on_unload(self, func) -> None:
        pass

    def async_create_background_task(self, hass, target, name):
        return asyncio.ensure_future(target)


async def _setup(module, *, experimental=True, appliance=None, app_type="HW", **overrides):
    SENT.clear()
    attributes = dict(json.loads(ATTRIBUTES.read_text(encoding="utf-8"))["attributes"])
    attributes.update(ISSUE115)
    attributes.update(overrides)
    data = {"hw-1": {"type": app_type, "name": "Boiler", "attributes": attributes,
                     "settings": {}, "appliance": appliance or _appliance()}}
    coordinator = _Coordinator(data)
    hass = types.SimpleNamespace(
        data={DOMAIN: {"entry-1": {"coordinator": coordinator, "client": object()}}},
        config=types.SimpleNamespace(units=types.SimpleNamespace(temperature_unit="°C")),
    )
    added: list = []
    await module.async_setup_entry(hass, _Entry(experimental), added.extend)
    for entity in added:
        entity.hass = hass
    return [e for e in added if str(getattr(e, "_attr_unique_id", "")).startswith("hw-1_")]


async def _record(hass, client, appliance, patch) -> None:
    SENT.append(patch)


_PATCHERS: list = []


def setUpModule() -> None:
    for module in (date_platform, button_platform, time_platform, switch_platform,
                   number_platform, water_heater):
        _PATCHERS.append(mock.patch.object(module, "async_dispatch_patch", _record))
    _PATCHERS.append(
        mock.patch(
            "custom_components.addhon.base_entity.HonBaseEntity._async_request_command_refresh",
            mock.AsyncMock(),
        )
    )
    # The entity service needs voluptuous and a live platform: tested on its own below.
    _PATCHERS.append(mock.patch.object(water_heater, "_register_eco_schedule_service"))
    for patcher in _PATCHERS:
        patcher.start()


def tearDownModule() -> None:
    while _PATCHERS:
        _PATCHERS.pop().stop()


def _sent() -> list[tuple[str, dict]]:
    return [(patch.command_name, dict(patch.values)) for patch in SENT]


def _by_id(entities) -> dict:
    return {e._attr_unique_id: e for e in entities}


STERILIZATION_115 = {
    "sterilizationStatus": "0",
    "sterilizationInterval": "2",
    "sterilizationTime": "15:0",
    "sterilizationTempSel": "65",
}


# --- vacation -----------------------------------------------------------------------


class VacationDatesWriteTest(unittest.IsolatedAsyncioTestCase):
    """`sendVacModeDate` (apk2 decomp.txt:2334500-2334545): both dates, every time."""

    async def _date(self, key: str, **kwargs):
        return _by_id(await _setup(date_platform, **kwargs))[f"hw-1_{key}"]

    async def test_read_only_without_the_experimental_option(self) -> None:
        entity = await self._date("vacation_start", experimental=False)
        with self.assertRaises(HomeAssistantError) as raised:
            await entity.async_set_value(date(2026, 12, 20))
        self.assertEqual(raised.exception.translation_key, "vacation_dates_read_only")
        self.assertEqual(SENT, [])

    async def test_the_start_with_a_cleared_end_sends_one_day(self) -> None:
        entity = await self._date("vacation_start")
        await entity.async_set_value(date(2026, 12, 20))
        self.assertEqual(_sent(), [("settings", {
            "vacStartDate": "2026-12-20", "vacEndDate": "2026-12-21",
            "operationName": "grSetVacDate",
        })])
        self.assertEqual(SENT[0].action, "set_vacation")
        entity._async_request_command_refresh.assert_awaited()

    async def test_the_end_with_a_cleared_start_starts_the_day_before(self) -> None:
        entity = await self._date("vacation_end")
        await entity.async_set_value(date(2027, 1, 6))
        self.assertEqual(_sent(), [("settings", {
            "vacStartDate": "2027-01-05", "vacEndDate": "2027-01-06",
            "operationName": "grSetVacDate",
        })])

    async def test_the_other_half_comes_from_the_shadow(self) -> None:
        entity = await self._date("vacation_end", vacStartDate="2026-12-20")
        await entity.async_set_value(date(2027, 1, 6))
        self.assertEqual(_sent()[0][1]["vacStartDate"], "2026-12-20")
        entity = await self._date("vacation_start", vacEndDate="2027-01-06")
        await entity.async_set_value(date(2026, 12, 28))
        self.assertEqual(_sent()[0][1]["vacEndDate"], "2027-01-06")

    async def test_the_113_placeholder_and_an_empty_date_are_missing_halves(self) -> None:
        for missing in ("0000-00-00", "", "garbage"):
            entity = await self._date("vacation_start", vacEndDate=missing)
            await entity.async_set_value(date(2026, 12, 20))
            self.assertEqual(_sent()[0][1]["vacEndDate"], "2026-12-21", missing)

    async def test_an_end_not_after_the_start_is_refused(self) -> None:
        # The app's screen refuses an end before the start AND the same day
        # (apk2 decomp.txt:4490504-4490540).
        for shadow_end, new_start in (("2026-12-20", date(2026, 12, 20)),
                                      ("2026-12-20", date(2026, 12, 25))):
            entity = await self._date("vacation_start", vacEndDate=shadow_end)
            with self.assertRaises(HomeAssistantError) as raised:
                await entity.async_set_value(new_start)
            self.assertEqual(raised.exception.translation_key, "hpwh_vacation_dates_order")
        entity = await self._date("vacation_end", vacStartDate="2026-12-20")
        with self.assertRaises(HomeAssistantError) as raised:
            await entity.async_set_value(date(2026, 12, 19))
        self.assertEqual(raised.exception.translation_key, "hpwh_vacation_dates_order")
        self.assertEqual(SENT, [])

    async def test_refused_while_the_appliance_takes_no_command(self) -> None:
        for overrides in ({"errors": 5}, {"remoteCtrValid": 0}):
            entity = await self._date("vacation_start", **overrides)
            with self.assertRaises(HomeAssistantError) as raised:
                await entity.async_set_value(date(2026, 12, 20))
            self.assertEqual(raised.exception.translation_key, "hpwh_unavailable", overrides)
        self.assertEqual(SENT, [])

    async def test_switched_off_or_in_vacation_is_not_refused(self) -> None:
        for overrides in ({"onOffStatus": 0},
                          {"machMode": 4, "vacStartDate": "2026-12-20",
                           "vacEndDate": "2026-12-27"}):
            entity = await self._date("vacation_end", **overrides)
            await entity.async_set_value(date(2027, 1, 6))
            self.assertEqual(len(SENT), 1, overrides)

    async def test_only_the_plain_series_with_the_schema_keys(self) -> None:
        for appliance in (_appliance("m8b"), _appliance("M11"), _appliance("m7b"),
                          _appliance(None), _appliance("x9"),
                          _appliance("m8", drop=("operationName",))):
            entity = await self._date("vacation_start", appliance=appliance)
            with self.assertRaises(HomeAssistantError) as raised:
                await entity.async_set_value(date(2026, 12, 20))
            self.assertEqual(raised.exception.translation_key, "hpwh_write_not_supported")
        self.assertEqual(SENT, [])
        for series in ("m7", " M8 "):
            entity = await self._date("vacation_start", appliance=_appliance(series))
            await entity.async_set_value(date(2026, 12, 20))
            self.assertEqual(len(SENT), 1, series)


class VacationClearButtonTest(unittest.IsolatedAsyncioTestCase):
    """`turnOffDate` (apk2 decomp.txt:2334636): both dates at 2000-01-01."""

    async def _buttons(self, **kwargs) -> dict:
        return _by_id(await _setup(button_platform, **kwargs))

    async def test_created_only_with_the_option_on_the_plain_series(self) -> None:
        button = (await self._buttons())["hw-1_vacation_clear"]
        self.assertEqual(button._attr_translation_key, "vacation_clear")
        self.assertEqual(await self._buttons(experimental=False), {})
        for appliance in (_appliance("m8b"), _appliance(None),
                          _appliance("m8", drop=("vacEndDate",))):
            self.assertEqual(await self._buttons(appliance=appliance), {})
        self.assertEqual(await self._buttons(app_type="WH"), {})

    async def test_press_sends_the_cleared_dates(self) -> None:
        button = (await self._buttons(machMode=4, vacStartDate="2026-12-20",
                                      vacEndDate="2026-12-27"))["hw-1_vacation_clear"]
        await button.async_press()
        self.assertEqual(_sent(), [("settings", {
            "vacStartDate": "2000-01-01", "vacEndDate": "2000-01-01",
            "operationName": "grSetVacDate",
        })])
        self.assertEqual(SENT[0].action, "clear_vacation")

    async def test_refused_while_the_appliance_takes_no_command(self) -> None:
        button = (await self._buttons(errors=3))["hw-1_vacation_clear"]
        with self.assertRaises(HomeAssistantError) as raised:
            await button.async_press()
        self.assertEqual(raised.exception.translation_key, "hpwh_unavailable")
        self.assertEqual(SENT, [])


# --- sterilization ------------------------------------------------------------------


class SterilizationTimeEntityTest(unittest.IsolatedAsyncioTestCase):
    """The `time` entity that replaces the block-3 sensor."""

    async def _time(self, **kwargs):
        return _by_id(await _setup(time_platform, **kwargs)).get("hw-1_sterilization_time")

    async def test_a_reading_for_everyone(self) -> None:
        for experimental in (True, False):
            entity = await self._time(experimental=experimental)
            self.assertEqual(entity.native_value, time(15, 0))
            self.assertEqual(entity._attr_translation_key, "sterilization_time")
        entity = await self._time(sterilizationTime="2:0")
        self.assertEqual(entity.native_value, time(2, 0))
        entity = await self._time(sterilizationTime="nope")
        self.assertIsNone(entity.native_value)

    async def test_not_created_without_the_attribute_nor_on_another_type(self) -> None:
        appliance = _appliance()
        attributes = dict(ISSUE115)
        attributes.pop("sterilizationTime")
        SENT.clear()
        data = {"hw-1": {"type": "HW", "attributes": attributes, "appliance": appliance}}
        coordinator = _Coordinator(data)
        hass = types.SimpleNamespace(
            data={DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
        added: list = []
        await time_platform.async_setup_entry(hass, _Entry(True), added.extend)
        self.assertEqual(added, [])
        self.assertIsNone(await self._time(app_type="WH"))

    async def test_read_only_without_the_experimental_option(self) -> None:
        entity = await self._time(experimental=False)
        with self.assertRaises(HomeAssistantError) as raised:
            await entity.async_set_value(time(3, 30))
        self.assertEqual(raised.exception.translation_key, "sterilization_time_read_only")
        self.assertEqual(SENT, [])

    async def test_the_four_keys_the_others_from_the_shadow(self) -> None:
        entity = await self._time()
        await entity.async_set_value(time(3, 30))
        self.assertEqual(_sent(), [("settings", {**STERILIZATION_115,
                                                 "sterilizationTime": "3:30"})])
        self.assertEqual(list(SENT[0].values), list(hpwh.HPWH_STERILIZATION_KEYS))
        self.assertEqual(SENT[0].action, "set_sterilization_time")

    async def test_the_113_temperature_is_cut_like_the_app(self) -> None:
        # `sterilizationTempSel.split('.')[0]` (apk2 decomp.txt:4591521-4591539).
        entity = await self._time(sterilizationTempSel=62.2, sterilizationStatus=1,
                                  sterilizationInterval=3.0)
        await entity.async_set_value(time(15, 0))
        self.assertEqual(_sent()[0][1], {
            "sterilizationStatus": "1", "sterilizationInterval": "3",
            "sterilizationTime": "15:0", "sterilizationTempSel": "62",
        })

    async def test_minutes_off_the_app_grid_are_refused(self) -> None:
        # The app's picker steps 15 minutes (apk2 decomp.txt:4591951-4591958).
        entity = await self._time()
        with self.assertRaises(HomeAssistantError) as raised:
            await entity.async_set_value(time(3, 10))
        self.assertEqual(raised.exception.translation_key, "invalid_setpoint")
        self.assertEqual(raised.exception.translation_placeholders["value"], "03:10")
        self.assertEqual(SENT, [])

    async def test_refusals(self) -> None:
        cases = (
            ({"errors": 2}, "hpwh_unavailable"),
            ({"remoteCtrValid": 0}, "hpwh_unavailable"),
            ({"machMode": 4, "vacStartDate": "2026-12-20", "vacEndDate": "2026-12-27"},
             "hpwh_vacation_active"),
            ({"sterilizationInterval": None}, "hpwh_sterilization_unreadable"),
            ({"sterilizationStatus": 7}, "hpwh_sterilization_unreadable"),
            # A shadow value the schema would refuse: sent back unchanged, it would fail.
            ({"sterilizationTempSel": 50}, "hpwh_sterilization_unreadable"),
        )
        for overrides, key in cases:
            entity = await self._time(**overrides)
            with self.assertRaises(HomeAssistantError) as raised:
                await entity.async_set_value(time(3, 30))
            self.assertEqual(raised.exception.translation_key, key, overrides)
        self.assertEqual(SENT, [])

    async def test_only_the_plain_series_with_the_schema_keys(self) -> None:
        for appliance in (_appliance("m11"), _appliance(None),
                          _appliance("m8", drop=("sterilizationTempSel",))):
            entity = await self._time(appliance=appliance)
            with self.assertRaises(HomeAssistantError) as raised:
                await entity.async_set_value(time(3, 30))
            self.assertEqual(raised.exception.translation_key, "hpwh_write_not_supported")
        self.assertEqual(SENT, [])


class SterilizationSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def _switch(self, **kwargs):
        return _by_id(await _setup(switch_platform, **kwargs)).get(
            "hw-1_sterilization_schedule"
        )

    async def test_created_only_with_the_option_on_the_plain_series(self) -> None:
        switch = await self._switch()
        self.assertEqual(switch._attr_translation_key, "sterilization_schedule")
        self.assertIsNone(await self._switch(experimental=False))
        for appliance in (_appliance("m8b"), _appliance(None),
                          _appliance("m8", drop=("sterilizationTime",))):
            self.assertIsNone(await self._switch(appliance=appliance))

    async def test_state(self) -> None:
        self.assertFalse((await self._switch()).is_on)
        self.assertTrue((await self._switch(sterilizationStatus=1.0)).is_on)
        self.assertIsNone((await self._switch(sterilizationStatus=None)).is_on)

    async def test_on_and_off_send_the_four_keys(self) -> None:
        switch = await self._switch()
        await switch.async_turn_on()
        await switch.async_turn_off()
        self.assertEqual(_sent(), [
            ("settings", {**STERILIZATION_115, "sterilizationStatus": "1"}),
            ("settings", STERILIZATION_115),
        ])
        self.assertEqual([p.action for p in SENT], ["sterilization_on", "sterilization_off"])

    async def test_refused_in_a_vacation(self) -> None:
        switch = await self._switch(machMode=4, vacStartDate="2026-12-20",
                                    vacEndDate="2026-12-27")
        with self.assertRaises(HomeAssistantError) as raised:
            await switch.async_turn_on()
        self.assertEqual(raised.exception.translation_key, "hpwh_vacation_active")
        self.assertEqual(SENT, [])


class SterilizationNumbersTest(unittest.IsolatedAsyncioTestCase):
    async def _numbers(self, **kwargs) -> dict:
        return _by_id(await _setup(number_platform, **kwargs))

    async def test_two_numbers_on_the_schema_ranges(self) -> None:
        numbers = await self._numbers()
        interval = numbers["hw-1_sterilization_interval"]
        temperature = numbers["hw-1_sterilization_temperature"]
        self.assertEqual(
            (interval.native_min_value, interval.native_max_value, interval.native_step),
            (1.0, 3.0, 1.0),
        )
        self.assertEqual(
            (temperature.native_min_value, temperature.native_max_value,
             temperature.native_step),
            (55.0, 75.0, 1.0),
        )
        self.assertEqual((interval.native_value, temperature.native_value), (2.0, 65.0))
        self.assertEqual(interval.entity_description.translation_key, "sterilization_interval")
        self.assertEqual(
            temperature.entity_description.translation_key, "sterilization_temperature"
        )

    async def test_created_only_with_the_option_on_the_plain_series(self) -> None:
        self.assertEqual(await self._numbers(experimental=False), {})
        for appliance in (_appliance("m7b"), _appliance("x9"),
                          _appliance("m8", drop=("sterilizationInterval",))):
            self.assertEqual(await self._numbers(appliance=appliance), {})

    async def test_each_sends_the_four_keys(self) -> None:
        numbers = await self._numbers()
        await numbers["hw-1_sterilization_interval"].async_set_native_value(3.0)
        await numbers["hw-1_sterilization_temperature"].async_set_native_value(70.0)
        self.assertEqual(_sent(), [
            ("settings", {**STERILIZATION_115, "sterilizationInterval": "3"}),
            ("settings", {**STERILIZATION_115, "sterilizationTempSel": "70"}),
        ])
        self.assertEqual(
            [p.action for p in SENT],
            ["set_sterilization_interval", "set_sterilization_temperature"],
        )

    async def test_a_value_off_the_schema_is_refused(self) -> None:
        numbers = await self._numbers()
        for key, value in (("hw-1_sterilization_interval", 4.0),
                           ("hw-1_sterilization_interval", 1.5),
                           ("hw-1_sterilization_temperature", 70.5),
                           ("hw-1_sterilization_temperature", 80.0)):
            with self.assertRaises(HomeAssistantError) as raised:
                await numbers[key].async_set_native_value(value)
            self.assertEqual(raised.exception.translation_key, "invalid_setpoint", value)
        self.assertEqual(SENT, [])


# --- eco windows ----------------------------------------------------------------------


def _steps(*args, **kwargs):
    return [(p.command_name, dict(p.values)) for p in hpwh.eco_schedule_steps(*args, **kwargs)]


def _windows(opp1=(), opp2=()) -> dict:
    values = {}
    for period, group in ((1, opp1), (2, opp2)):
        for slot in (1, 2, 3):
            start, end = group[slot - 1] if slot <= len(group) else ("00:00", "00:00")
            values[f"opp{period}EcoStartTime{slot}"] = start
            values[f"opp{period}EcoEndTime{slot}"] = end
    values["operationName"] = "grSetEcoTime"
    return values


class EcoScheduleStepsTest(unittest.TestCase):
    """The app's chain (apk2 decomp.txt:1527781-1527905, builder 4505046-4505653)."""

    def test_same_windows_every_day(self) -> None:
        self.assertEqual(
            _steps("same", windows=["00:15-23:45"]),
            [("settings", {"offPeakPeriodScheme": "1"}),
             ("settings", _windows(opp2=[("00:15", "23:45")]))],
        )

    def test_different_windows_go_scheme_days_windows(self) -> None:
        steps = hpwh.eco_schedule_steps(
            "different", days=["mon", "tue", "wed", "thu", "fri"],
            windows=["22:00-23:45", "06:00-08:00"], other_windows=["00:15-23:45"],
        )
        self.assertEqual(
            [(p.command_name, dict(p.values)) for p in steps],
            [("settings", {"offPeakPeriodScheme": "0"}),
             ("settings", {"opp1EcoDays": "1f", "operationName": "grSetWeekGroup"}),
             # The UI's period 1 (the chosen days) is opp2, period 2 is opp1; each
             # set in time order.
             ("settings", _windows(opp1=[("00:15", "23:45")],
                                   opp2=[("06:00", "08:00"), ("22:00", "23:45")]))],
        )
        self.assertEqual([p.action for p in steps],
                         ["set_eco_scheme", "set_eco_days", "set_eco_windows"])
        # The schema types the day mask as a decimal range 0-40 (#113, #115): only the
        # mask goes on the wire as written.
        self.assertEqual(steps[1].verbatim, frozenset({"opp1EcoDays"}))
        self.assertEqual(steps[0].verbatim, frozenset())
        self.assertEqual(list(steps[2].values), [*WINDOW_KEYS, "operationName"])

    def test_the_day_mask_is_the_apps_lowercase_unpadded_hex(self) -> None:
        # `weekDaysValue` (decomp.txt:4511355-4511367), `toString(16)` (4505245).
        for days, mask in ((["sat", "sun"], "60"), (["mon"], "1"), (["sun"], "40"),
                           (list(hpwh.HPWH_ECO_DAY_NAMES), "7f"),
                           (["wed", "tue", "tue"], "6"), (["thu", "fri", "sat"], "38")):
            steps = hpwh.eco_schedule_steps("different", days=days, windows=[])
            self.assertEqual(steps[1].values["opp1EcoDays"], mask, days)

    def test_days_must_be_a_run_from_monday_to_sunday(self) -> None:
        for days in (None, [], ["mon", "wed"], ["sun", "mon"], ["lun"], ["Mon"]):
            with self.assertRaises(HomeAssistantError) as raised:
                hpwh.eco_schedule_steps("different", days=days, windows=[])
            self.assertEqual(raised.exception.translation_key, "hpwh_eco_days_invalid", days)

    def test_days_and_other_windows_belong_to_different(self) -> None:
        for kwargs in ({"days": ["mon"]}, {"other_windows": ["06:00-07:00"]}):
            with self.assertRaises(HomeAssistantError) as raised:
                hpwh.eco_schedule_steps("same", windows=[], **kwargs)
            self.assertEqual(raised.exception.translation_key, "hpwh_eco_different_only")
        # Empty is absent.
        self.assertEqual(len(hpwh.eco_schedule_steps("same", days=[], other_windows=[])), 2)

    def test_no_window_is_twelve_empty_slots(self) -> None:
        self.assertEqual(_steps("same")[1], ("settings", _windows()))

    def test_window_spellings(self) -> None:
        # The eco-window sensor publishes an en dash; a single-digit hour is padded.
        self.assertEqual(
            _steps("same", windows=["6:00–8:15", " 22:30 - 23:45 "])[1][1],
            _windows(opp2=[("06:00", "08:15"), ("22:30", "23:45")]),
        )

    def test_a_bad_window_is_refused_by_name(self) -> None:
        for window in ("6-8", "06:10-08:00", "08:00-06:00", "06:00-06:00", "24:00-01:00",
                       "06:00", "aa:bb-cc:dd", "06:00-08:00-09:00"):
            with self.assertRaises(HomeAssistantError) as raised:
                hpwh.eco_schedule_steps("same", windows=[window])
            self.assertEqual(raised.exception.translation_key, "hpwh_eco_window_invalid")
            self.assertEqual(raised.exception.translation_placeholders, {"window": window})

    def test_overlapping_windows_are_refused(self) -> None:
        # `validateNewTimeValue` (decomp.txt:1051318-1051420) refuses an overlap.
        with self.assertRaises(HomeAssistantError) as raised:
            hpwh.eco_schedule_steps("same", windows=["06:00-08:00", "07:45-09:00"])
        self.assertEqual(raised.exception.translation_placeholders, {"window": "07:45-09:00"})
        # Touching is not overlapping.
        hpwh.eco_schedule_steps("same", windows=["06:00-08:00", "08:00-09:00"])

    def test_at_most_three_windows_per_set(self) -> None:
        four = ["01:00-02:00", "03:00-04:00", "05:00-06:00", "07:00-08:00"]
        for kwargs in ({"scheme": "same", "windows": four},
                       {"scheme": "different", "days": ["mon"], "other_windows": four}):
            with self.assertRaises(HomeAssistantError) as raised:
                hpwh.eco_schedule_steps(**kwargs)
            self.assertEqual(raised.exception.translation_key, "hpwh_eco_too_many_windows")

    def test_an_unknown_scheme_is_a_programming_error(self) -> None:
        with self.assertRaises(ValueError):
            hpwh.eco_schedule_steps("weekly", windows=[])


class EcoChangedStepsTest(unittest.TestCase):
    """The steps the app skips with Eco already on (apk2 decomp.txt:4506227-4506359).

    The scheme goes only when it differs from `Number(offPeakPeriodScheme)`; with
    the scheme unchanged, the days go only when the new mask differs from the
    shadow's (`|| '0'`), compared in lower case; after a changed scheme the days
    always follow with `different`; the windows always go.
    """

    STEPS = staticmethod(lambda: hpwh.eco_schedule_steps(
        "different", days=["mon", "tue", "wed", "thu", "fri"], windows=["06:00-08:00"]))

    def _actions(self, shadow: dict, steps=None) -> list[str]:
        steps = self.STEPS() if steps is None else steps
        return [p.action for p in hpwh.eco_changed_steps(shadow.get, steps)]

    def test_the_scheme_compares_as_a_number(self) -> None:
        # `Number(parNewVal)`: '' is 0 there, a missing value NaN.
        for current in (0, 0.0, "0", ""):
            self.assertNotIn("set_eco_scheme",
                             self._actions({"offPeakPeriodScheme": current}), current)
        for current in (1, "1", None, "x"):
            self.assertIn("set_eco_scheme",
                          self._actions({"offPeakPeriodScheme": current}), current)

    def test_the_days_compare_in_lower_case_with_the_scheme_unchanged(self) -> None:
        for current, sent in (("1F", False), ("1f", False), ("7F", True), (None, True),
                              ("", True), ("01F", True)):
            shadow = {"offPeakPeriodScheme": 0, "opp1EcoDays": current}
            self.assertEqual("set_eco_days" in self._actions(shadow), sent, current)

    def test_no_step_in_no_step_out(self) -> None:
        self.assertEqual(hpwh.eco_changed_steps({}.get, ()), ())

    def test_after_a_changed_scheme_the_days_always_go(self) -> None:
        shadow = {"offPeakPeriodScheme": 1, "opp1EcoDays": "1F"}
        self.assertEqual(self._actions(shadow),
                         ["set_eco_scheme", "set_eco_days", "set_eco_windows"])

    def test_the_windows_always_go(self) -> None:
        steps = hpwh.eco_schedule_steps("same", windows=["00:15-23:45"])
        shadow = {"offPeakPeriodScheme": 1, "opp2EcoStartTime1": "00:15",
                  "opp2EcoEndTime1": "23:45"}
        self.assertEqual(self._actions(shadow, steps), ["set_eco_windows"])


class EcoStepAppliedTest(unittest.TestCase):
    """The step is confirmed by what the appliance publishes, in its own spelling."""

    def test_the_appliance_spelling_confirms_ours(self) -> None:
        steps = hpwh.eco_schedule_steps(
            "different", days=["mon", "tue", "wed", "thu", "fri"],
            windows=["6:00-8:00"], other_windows=[],
        )
        shadow = {"offPeakPeriodScheme": 0.0, "opp1EcoDays": "1F",
                  **{key: "00:00" for key in WINDOW_KEYS},
                  "opp2EcoStartTime1": "06:00", "opp2EcoEndTime1": "08:00"}
        get = shadow.get
        self.assertTrue(all(hpwh.eco_step_applied(get, step) for step in steps))
        shadow["opp1EcoDays"] = "7F"
        self.assertFalse(hpwh.eco_step_applied(get, steps[1]))
        shadow["opp2EcoEndTime1"] = "8:15"
        self.assertFalse(hpwh.eco_step_applied(get, steps[2]))
        shadow["offPeakPeriodScheme"] = 1
        self.assertFalse(hpwh.eco_step_applied(get, steps[0]))
        shadow.pop("offPeakPeriodScheme")
        self.assertFalse(hpwh.eco_step_applied(get, steps[0]))


class EcoScheduleServiceTest(unittest.IsolatedAsyncioTestCase):
    """`addhon.set_eco_schedule` on the water heater: Eco only, one step at a time."""

    async def _heater(self, **kwargs):
        return (await _setup(water_heater, **kwargs))[0]

    async def _run(self, heater, *args, device=None, **kwargs) -> list:
        """Run the service; `device` plays the appliance between send and read."""
        events: list = []

        async def _dispatch(hass, client, appliance, patch) -> None:
            SENT.append(patch)
            events.append(("send", patch.action))
            (device or _apply)(heater, patch)

        async def _settle() -> None:
            events.append(("settle", None))

        async def _refresh() -> None:
            events.append(("refresh", None))

        with mock.patch.object(water_heater, "async_dispatch_patch", _dispatch), \
                mock.patch.object(water_heater, "_settle", _settle), \
                mock.patch.object(heater, "_async_request_command_refresh", _refresh):
            await heater.async_set_eco_schedule(*args, **kwargs)
        return events

    async def test_the_chain_waits_for_each_step(self) -> None:
        heater = await self._heater()
        events = await self._run(
            heater, scheme="different", days=["sat", "sun"],
            windows=["08:00-10:00"], other_windows=["00:15-06:00"],
        )
        self.assertEqual(events, [
            ("send", "set_eco_scheme"), ("settle", None), ("refresh", None),
            ("send", "set_eco_days"), ("settle", None), ("refresh", None),
            ("send", "set_eco_windows"), ("settle", None), ("refresh", None),
        ])
        self.assertEqual(_sent()[1], ("settings", {"opp1EcoDays": "60",
                                                   "operationName": "grSetWeekGroup"}))

    async def test_an_unchanged_scheme_is_not_sent(self) -> None:
        # #115 is already on `same` (offPeakPeriodScheme 1): only the windows go, as the
        # app with Eco already on (apk2 decomp.txt:4506285-4506298).
        heater = await self._heater()
        events = await self._run(heater, scheme="same", windows=["06:00-08:00"])
        self.assertEqual(events, [
            ("send", "set_eco_windows"), ("settle", None), ("refresh", None),
        ])

    async def test_a_changed_scheme_sends_the_days_whatever_they_were(self) -> None:
        heater = await self._heater(offPeakPeriodScheme=1, opp1EcoDays="1F")
        await self._run(heater, scheme="different", days=["mon", "tue", "wed", "thu", "fri"],
                        windows=["06:00-08:00"])
        self.assertEqual([p.action for p in SENT],
                         ["set_eco_scheme", "set_eco_days", "set_eco_windows"])

    async def test_unchanged_days_are_not_sent(self) -> None:
        heater = await self._heater(offPeakPeriodScheme=0, opp1EcoDays="1F")
        await self._run(heater, scheme="different", days=["mon", "tue", "wed", "thu", "fri"],
                        windows=["06:00-08:00"])
        self.assertEqual([p.action for p in SENT], ["set_eco_windows"])

    async def test_changed_days_with_the_same_scheme_go_before_the_windows(self) -> None:
        heater = await self._heater(offPeakPeriodScheme=0, opp1EcoDays="1F")
        events = await self._run(heater, scheme="different", days=["sat", "sun"],
                                 windows=["06:00-08:00"])
        self.assertEqual([e for e in events if e[0] == "send"],
                         [("send", "set_eco_days"), ("send", "set_eco_windows")])

    async def test_unchanged_windows_are_still_sent(self) -> None:
        # The app compares the scheme and the days, never the windows: with nothing
        # changed it still sends them (the same paths).
        heater = await self._heater()
        await self._run(heater, scheme="same", windows=["00:15-23:45"])
        self.assertEqual([p.action for p in SENT], ["set_eco_windows"])

    async def test_a_not_applied_step_counts_only_the_steps_sent(self) -> None:
        heater = await self._heater(offPeakPeriodScheme=0, opp1EcoDays="1F")

        def _ignores_the_days(entity, patch) -> None:
            if patch.action != "set_eco_days":
                _apply(entity, patch)

        with self.assertRaises(HomeAssistantError) as raised:
            await self._run(heater, scheme="different", days=["sat", "sun"], windows=[],
                            device=_ignores_the_days)
        self.assertEqual(raised.exception.translation_placeholders, {"step": "1", "steps": "2"})

    async def test_a_step_the_appliance_did_not_apply_stops_the_chain(self) -> None:
        heater = await self._heater()

        def _ignores_the_days(entity, patch) -> None:
            if patch.action != "set_eco_days":
                _apply(entity, patch)

        with self.assertRaises(HomeAssistantError) as raised:
            await self._run(heater, scheme="different", days=["mon"], windows=[],
                            device=_ignores_the_days)
        self.assertEqual(raised.exception.translation_key, "hpwh_eco_step_not_applied")
        self.assertEqual(raised.exception.translation_placeholders, {"step": "2", "steps": "3"})
        self.assertEqual([p.action for p in SENT], ["set_eco_scheme", "set_eco_days"])

    async def test_only_in_eco_and_where_a_mode_change_is_allowed(self) -> None:
        cases = (
            ({"machMode": 1}, "hpwh_eco_first"),
            ({"machMode": 3}, "hpwh_eco_first"),
            ({"onOffStatus": 0}, "hpwh_switch_on_first"),
            ({"errors": 4}, "hpwh_unavailable"),
            ({"machMode": 4, "vacStartDate": "2026-12-20", "vacEndDate": "2026-12-27"},
             "hpwh_vacation_active"),
            ({"sterilizationCurrentStatus": 1}, "hpwh_sterilization_running"),
        )
        for overrides, key in cases:
            heater = await self._heater(**overrides)
            with self.assertRaises(HomeAssistantError) as raised:
                await self._run(heater, scheme="same", windows=["00:15-23:45"])
            self.assertEqual(raised.exception.translation_key, key, overrides)
        self.assertEqual(SENT, [])

    async def test_only_the_plain_series_with_the_schema_keys(self) -> None:
        # The water heater exists on a missing or unknown series; this write does not.
        for appliance in (_appliance(None), _appliance("x9"),
                          _appliance("m8", drop=("opp1EcoDays",))):
            heater = await self._heater(appliance=appliance)
            with self.assertRaises(HomeAssistantError) as raised:
                await self._run(heater, scheme="same", windows=[])
            self.assertEqual(raised.exception.translation_key, "hpwh_write_not_supported")
        self.assertEqual(SENT, [])

    async def test_bad_input_sends_nothing(self) -> None:
        heater = await self._heater()
        with self.assertRaises(HomeAssistantError):
            await self._run(heater, scheme="different", days=["mon", "wed"], windows=[])
        self.assertEqual(SENT, [])

    def test_the_pause_lets_the_delivery_check_judge_each_step(self) -> None:
        # Past the shadow's 10 s shield and at the delivery check's own settle time,
        # so the read after each step is the cloud's and the check gets its verdict.
        self.assertEqual(water_heater._ECO_STEP_SETTLE, command_diagnostics._DELIVERY_SETTLE)

    async def test_settle_sleeps_the_pause(self) -> None:
        with mock.patch.object(water_heater.asyncio, "sleep", mock.AsyncMock()) as sleep:
            await water_heater._settle()
        sleep.assert_awaited_once_with(water_heater._ECO_STEP_SETTLE)


def _apply(entity, patch) -> None:
    """What the appliance publishes once it ran a step: our values, its spelling."""
    attributes = entity.coordinator.data["hw-1"]["attributes"]
    for key, value in patch.values.items():
        if key == "operationName":
            continue
        attributes[key] = value.upper() if key == "opp1EcoDays" else value


class ScheduleWriteLockTest(unittest.IsolatedAsyncioTestCase):
    """Two schedule writes at once on one appliance (PR #121 review, Greptile).

    Each write builds its patch from the shadow; the dispatcher's own lock covers one
    send, not that read. Decision of 2026-10-06: one lock per appliance around every
    schedule write (sterilization, vacation dates, vacation clear, the whole eco
    chain); a second write waits its turn, then reads the shadow the first one left.
    """

    async def _entities(self, **overrides) -> dict:
        """Every HW schedule entity of one appliance, on ONE coordinator."""
        SENT.clear()
        attributes = dict(json.loads(ATTRIBUTES.read_text(encoding="utf-8"))["attributes"])
        attributes.update(ISSUE115)
        attributes.update(overrides)
        self.attributes = attributes
        data = {"hw-1": {"type": "HW", "name": "Boiler", "attributes": attributes,
                         "settings": {}, "appliance": _appliance()}}
        hass = types.SimpleNamespace(
            data={DOMAIN: {"entry-1": {"coordinator": _Coordinator(data),
                                       "client": object()}}},
            config=types.SimpleNamespace(
                units=types.SimpleNamespace(temperature_unit="°C")),
        )
        added: list = []
        for module in (date_platform, button_platform, switch_platform,
                       number_platform, water_heater):
            await module.async_setup_entry(hass, _Entry(True), added.extend)
        for entity in added:
            entity.hass = hass
        return _by_id(added)

    async def _dispatch(self, hass, client, appliance, patch) -> None:
        # The cloud takes its time, then the accepted payload lands in the shadow
        # (the dispatcher's mirror after acceptance).
        for _ in range(3):
            await asyncio.sleep(0)
        SENT.append(patch)
        for key, value in patch.values.items():
            if key != "operationName":
                self.attributes[key] = value

    def _patched(self):
        stack = contextlib.ExitStack()
        for module in (date_platform, button_platform, switch_platform,
                       number_platform, water_heater):
            stack.enter_context(
                mock.patch.object(module, "async_dispatch_patch", self._dispatch))

        async def _settle() -> None:
            await asyncio.sleep(0)

        stack.enter_context(mock.patch.object(water_heater, "_settle", _settle))
        return stack

    async def test_sterilization_writes_do_not_undo_each_other(self) -> None:
        entities = await self._entities()
        with self._patched():
            await asyncio.gather(
                entities["hw-1_sterilization_schedule"].async_turn_on(),
                entities["hw-1_sterilization_temperature"].async_set_native_value(70.0),
            )
        self.assertEqual(self.attributes["sterilizationStatus"], "1")
        self.assertEqual(self.attributes["sterilizationTempSel"], "70")
        self.assertEqual(SENT[1].values["sterilizationStatus"], "1")

    async def test_two_vacation_dates_at_once_keep_both(self) -> None:
        entities = await self._entities()
        with self._patched():
            await asyncio.gather(
                entities["hw-1_vacation_start"].async_set_value(date(2026, 12, 20)),
                entities["hw-1_vacation_end"].async_set_value(date(2027, 1, 6)),
            )
        self.assertEqual(self.attributes["vacStartDate"], "2026-12-20")
        self.assertEqual(self.attributes["vacEndDate"], "2027-01-06")

    async def test_a_date_after_a_clear_reads_the_cleared_shadow(self) -> None:
        entities = await self._entities(vacStartDate="2026-12-20", vacEndDate="2026-12-27")
        with self._patched():
            await asyncio.gather(
                entities["hw-1_vacation_clear"].async_press(),
                entities["hw-1_vacation_end"].async_set_value(date(2027, 1, 6)),
            )
        # The end goes out after the clear, so its start is the day before, not the
        # 2026-12-20 the shadow held before the clear landed.
        self.assertEqual(SENT[1].values["vacStartDate"], "2027-01-05")

    async def test_two_eco_schedules_do_not_interleave(self) -> None:
        entities = await self._entities(offPeakPeriodScheme=1, opp1EcoDays="7F")
        heater = next(e for e in entities.values() if hasattr(e, "async_set_eco_schedule"))
        with self._patched():
            await asyncio.gather(
                heater.async_set_eco_schedule(
                    scheme="different", days=["sat", "sun"], windows=["08:00-10:00"],
                    other_windows=["00:15-06:00"]),
                heater.async_set_eco_schedule(scheme="same", windows=["06:00-08:00"]),
            )
        # The first chain whole (scheme, days, windows), then the second, which now
        # has to put the scheme back before its windows.
        self.assertEqual([p.action for p in SENT], [
            "set_eco_scheme", "set_eco_days", "set_eco_windows",
            "set_eco_scheme", "set_eco_windows",
        ])
        self.assertEqual(self.attributes["offPeakPeriodScheme"], "1")

    async def test_a_sterilization_write_waits_for_the_eco_chain(self) -> None:
        entities = await self._entities(offPeakPeriodScheme=1, opp1EcoDays="7F")
        heater = next(e for e in entities.values() if hasattr(e, "async_set_eco_schedule"))
        with self._patched():
            await asyncio.gather(
                heater.async_set_eco_schedule(
                    scheme="different", days=["sat", "sun"], windows=["08:00-10:00"]),
                entities["hw-1_sterilization_schedule"].async_turn_on(),
            )
        self.assertEqual([p.action for p in SENT], [
            "set_eco_scheme", "set_eco_days", "set_eco_windows", "sterilization_on",
        ])


class EcoScheduleRegistrationTest(unittest.IsolatedAsyncioTestCase):
    """The service is an entity service of the water heater platform."""

    def test_registered_with_its_schema(self) -> None:
        registered: list = []

        class _Platform:
            def async_register_entity_service(self, name, schema, method) -> None:
                registered.append((name, schema, method))

        class _Marker(str):
            def __new__(cls, key, default=None):
                marker = super().__new__(cls, key)
                marker.default = default
                return marker

        vol = types.ModuleType("voluptuous")
        vol.Required = type("Required", (_Marker,), {})
        vol.Optional = type("Optional", (_Marker,), {})
        vol.In = lambda values: ("in", tuple(values))
        vol.All = lambda *validators: ("all", validators)
        cv = types.ModuleType("homeassistant.helpers.config_validation")
        cv.ensure_list = "ensure_list"
        cv.string = "string"
        entity_platform = _mod("homeassistant.helpers.entity_platform")
        with mock.patch.dict(sys.modules, {
            "voluptuous": vol, "homeassistant.helpers.config_validation": cv,
        }), mock.patch.object(
            entity_platform, "async_get_current_platform", lambda: _Platform(), create=True
        ), mock.patch.object(_mod("homeassistant.helpers"), "config_validation", cv,
                             create=True):
            _REGISTER_ECO_SERVICE()
        [(name, schema, method)] = registered
        self.assertEqual(name, "set_eco_schedule")
        self.assertEqual(method, "async_set_eco_schedule")
        self.assertEqual({str(key) for key in schema},
                         {"scheme", "days", "windows", "other_windows"})
        markers = {str(key): key for key in schema}
        self.assertIsInstance(markers["scheme"], vol.Required)
        self.assertEqual(schema[markers["scheme"]], ("in", ("same", "different")))
        self.assertEqual(markers["windows"].default, [])

    async def test_registered_only_with_the_experimental_option(self) -> None:
        water_heater._register_eco_schedule_service.reset_mock()
        await _setup(water_heater, experimental=False)
        water_heater._register_eco_schedule_service.assert_not_called()
        await _setup(water_heater)
        water_heater._register_eco_schedule_service.assert_called_once_with()


# --- the real schema and the real dispatcher -------------------------------------------


class RealCatalogueWireTest(unittest.TestCase):
    """Every write, through the real loader and dispatcher, on the #115 catalogue.

    `settings.json` is the HP150M8-9's `settings` rebuilt from its beta9 dump: every
    fixed parameter accepts our value, and `opp1EcoDays` is the decimal range 0-40
    that refuses the app's hex mask unless it travels verbatim.
    """

    @staticmethod
    def _load():
        from custom_components.addhon.client import factory

        catalogue = json.loads((CATALOGUE / "catalog.json").read_text(encoding="utf-8"))
        settings = json.loads((CATALOGUE / "settings.json").read_text(encoding="utf-8"))

        class _Api:
            sent: list = []

            async def load_commands(self, appliance):
                commands = json.loads(json.dumps(catalogue["commands"]))
                commands["settings"] = json.loads(json.dumps(settings["settings"]))
                return commands

            async def load_favourites(self, appliance):
                return []

            async def load_command_history(self, appliance):
                return json.loads(json.dumps(catalogue["command_history"]))

            async def send_command(self, appliance, name, parameters, ancillary, *args,
                                   **kwargs):
                self.sent.append((kwargs.get("wire_command"), dict(parameters)))
                return True

        api = _Api()
        api.sent = []
        appliance = factory._native_engine_appliance_cls()(
            api, {"applianceTypeName": "HW", "applianceModelId": 1, "macAddress": "aa-bb"},
            zone=0,
        )
        asyncio.run(appliance.load_commands())
        return appliance, api

    def _wire(self, *patches) -> list:
        from custom_components.addhon.command_dispatch import CommandDispatcher

        appliance, api = self._load()
        for patch in patches:
            self.assertTrue(asyncio.run(CommandDispatcher().dispatch(appliance, patch)))
        return api.sent

    def test_the_catalogue_takes_every_write(self) -> None:
        appliance, _api = self._load()
        for keys in (hpwh.HPWH_VACATION_KEYS, hpwh.HPWH_STERILIZATION_KEYS,
                     hpwh.HPWH_ECO_SCHEDULE_KEYS):
            self.assertTrue(hpwh.schedule_writes_supported(appliance, keys), keys)

    def test_vacation(self) -> None:
        self.assertEqual(
            self._wire(hpwh.vacation_patch(date(2026, 12, 20), date(2027, 1, 6)),
                       hpwh.vacation_clear_patch()),
            [("setParameters", {"vacStartDate": "2026-12-20", "vacEndDate": "2027-01-06",
                                "operationName": "grSetVacDate"}),
             ("setParameters", {"vacStartDate": "2000-01-01", "vacEndDate": "2000-01-01",
                                "operationName": "grSetVacDate"})],
        )

    def test_sterilization(self) -> None:
        appliance, _api = self._load()
        get = {key: ISSUE115[key] for key in hpwh.HPWH_STERILIZATION_KEYS}.get
        patch = hpwh.sterilization_write(
            get, hpwh.settings_parameters(appliance), "sterilizationStatus", "1"
        )
        self.assertEqual(
            self._wire(patch),
            [("setParameters", {**STERILIZATION_115, "sterilizationStatus": "1"})],
        )

    def test_eco_schedule(self) -> None:
        steps = hpwh.eco_schedule_steps(
            "different", days=["mon", "tue", "wed", "thu", "fri"],
            windows=["06:00-08:00"], other_windows=["00:15-23:45"],
        )
        self.assertEqual(
            self._wire(*steps),
            [("setParameters", {"offPeakPeriodScheme": "0"}),
             ("setParameters", {"opp1EcoDays": "1f", "operationName": "grSetWeekGroup"}),
             ("setParameters", _windows(opp1=[("00:15", "23:45")],
                                        opp2=[("06:00", "08:00")]))],
        )

    def test_the_day_mask_needs_the_verbatim_channel(self) -> None:
        # Anti-vacuity: the same step through the schema's own setter is refused.
        from custom_components.addhon.command_dispatch import CommandDispatcher, CommandPatch

        appliance, api = self._load()
        patch = CommandPatch("settings", {"opp1EcoDays": "1f",
                                          "operationName": "grSetWeekGroup"},
                             action="set_eco_days")
        with self.assertRaises(ValueError):
            asyncio.run(CommandDispatcher().dispatch(appliance, patch))
        self.assertEqual(api.sent, [])


if __name__ == "__main__":
    unittest.main()
