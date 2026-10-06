# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The heat-pump water heater platform (type HW, issue #113)."""
import asyncio
import enum
import json
import math
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.test_hpwh import _install_package_stubs, _mod  # same stubs, one place

_install_package_stubs()

water_heater = _mod("homeassistant.components.water_heater")


class _Feature(enum.IntFlag):
    TARGET_TEMPERATURE = 1
    OPERATION_MODE = 2
    AWAY_MODE = 4
    ON_OFF = 8


water_heater.WaterHeaterEntityFeature = getattr(water_heater, "WaterHeaterEntityFeature", _Feature)
water_heater.WaterHeaterEntity = getattr(water_heater, "WaterHeaterEntity", type("WaterHeaterEntity", (), {}))
for _name, _value in (("STATE_ECO", "eco"), ("STATE_ELECTRIC", "electric"),
                      ("STATE_HEAT_PUMP", "heat_pump"), ("STATE_OFF", "off")):
    setattr(water_heater, _name, getattr(water_heater, _name, _value))
_mod("homeassistant.components").water_heater = water_heater
_const = _mod("homeassistant.const")
_const.ATTR_TEMPERATURE = getattr(_const, "ATTR_TEMPERATURE", "temperature")
_const.UnitOfTemperature.FAHRENHEIT = getattr(_const.UnitOfTemperature, "FAHRENHEIT", "°F")
_switch = _mod("homeassistant.components.switch")
_switch.SwitchEntity = getattr(_switch, "SwitchEntity", type("SwitchEntity", (), {}))

from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from custom_components.addhon import switch as switch_platform  # noqa: E402
from custom_components.addhon import water_heater as platform  # noqa: E402
from custom_components.addhon.const import CONF_ENABLE_EXPERIMENTAL, DOMAIN  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "hw_hp110m8" / "attributes.json"
SENT: list = []


def _range(value, low, high, step=1):
    return types.SimpleNamespace(value=value, min=low, max=high, step=step)


def _appliance():
    settings = types.SimpleNamespace(parameters={
        "tempSel": _range(40, 35, 75), "onOffStatus": _range(0, 0, 1),
        "boostStatus": _range(0, 0, 1),
    })
    categories = {
        name: types.SimpleNamespace(parameters={
            "machMode": types.SimpleNamespace(value=code, schema_value=code)})
        for name, code in (("auto", "1"), ("eco", "2"), ("elec", "3"), ("vac", "4"))
    }
    start = types.SimpleNamespace(categories=categories, parameters={})
    return types.SimpleNamespace(
        commands={"settings": settings, "startProgram": start},
        model_attributes={"series": "m8"},
    )


class _Coordinator:
    """The pieces of HA's DataUpdateCoordinator the platform and its listener use."""

    def __init__(self, data) -> None:
        self.data = data
        self.hass = None
        self.last_update_success = True
        self.listeners: list = []

    def async_add_listener(self, update_callback):
        self.listeners.append(update_callback)

        def _remove() -> None:
            self.listeners.remove(update_callback)

        return _remove

    def fire(self) -> None:
        """What `async_set_updated_data` / a poll does: call every listener."""
        for update_callback in list(self.listeners):
            update_callback()


class _Entry:
    entry_id = "entry-1"

    def __init__(self, experimental: bool) -> None:
        self.options = {CONF_ENABLE_EXPERIMENTAL: experimental}
        self.on_unload: list = []
        self.task_names: list = []

    def async_on_unload(self, func) -> None:
        self.on_unload.append(func)

    def async_create_background_task(self, hass, target, name):
        self.task_names.append(name)
        return asyncio.ensure_future(target)

    def unload(self) -> None:
        while self.on_unload:
            self.on_unload.pop()()


class _Rig(types.SimpleNamespace):
    """One platform set up over one HW appliance: what a test needs to poke at."""

    @property
    def attributes(self) -> dict:
        return self.coordinator.data["hw-1"]["attributes"]


def _units(unit: str):
    return types.SimpleNamespace(units=types.SimpleNamespace(temperature_unit=unit))


async def _setup(module, experimental: bool, overrides: dict, appliance=None) -> _Rig:
    SENT.clear()
    attributes = dict(json.loads(FIXTURE.read_text(encoding="utf-8"))["attributes"])
    attributes.update(overrides)
    data = {"hw-1": {"type": "HW", "name": "Boiler", "attributes": attributes,
                     "settings": {}, "appliance": appliance or _appliance()}}
    coordinator = _Coordinator(data)
    hass = types.SimpleNamespace(
        data={DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}},
        config=_units(_const.UnitOfTemperature.CELSIUS),
    )
    entry = _Entry(experimental)
    added: list = []
    await module.async_setup_entry(hass, entry, added.extend)
    for entity in added:
        entity.hass = hass
    return _Rig(hass=hass, entry=entry, coordinator=coordinator, added=added)


async def _record(hass, client, appliance, patch) -> None:
    SENT.append(patch)


_PATCHERS: list = []


def setUpModule() -> None:
    # Every write is recorded instead of sent; the refresh after it is a no-op.
    # pytest does not run unittest's module cleanups, so tearDownModule stops them.
    _PATCHERS.append(mock.patch.object(platform, "async_dispatch_patch", _record))
    _PATCHERS.append(mock.patch.object(switch_platform, "async_dispatch_patch", _record))
    _PATCHERS.append(
        mock.patch(
            "custom_components.addhon.base_entity.HonBaseEntity._async_request_command_refresh",
            mock.AsyncMock(),
        )
    )
    # The eco-schedule entity service needs voluptuous and a live platform: its
    # registration is tested in test_hpwh_schedule_writes.
    _PATCHERS.append(mock.patch.object(platform, "_register_eco_schedule_service"))
    for patcher in _PATCHERS:
        patcher.start()


def tearDownModule() -> None:
    while _PATCHERS:
        _PATCHERS.pop().stop()


async def _rig(experimental: bool = True, appliance=None, **overrides) -> _Rig:
    return await _setup(platform, experimental, overrides, appliance)


async def _build(experimental: bool, appliance=None, **overrides) -> list:
    return (await _setup(platform, experimental, overrides, appliance)).added


async def _build_switches(experimental: bool, appliance=None, **overrides) -> list:
    # The platform also adds the account's debug switches; only the boost is ours.
    found = (await _setup(switch_platform, experimental, overrides, appliance)).added
    return [e for e in found if isinstance(e, switch_platform.HonHeatPumpBoostSwitch)]


async def _drain_tasks() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class WaterHeaterTest(unittest.IsolatedAsyncioTestCase):
    async def test_not_created_without_the_experimental_option(self) -> None:
        self.assertEqual(await _build(experimental=False), [])

    async def test_a_malformed_coordinator_entry_does_not_abort_the_setup(self) -> None:
        # PR #117 review: the setup loop now carries the listener's Mapping guard, so
        # one entry that is not a mapping costs nothing but itself.
        rig = await _rig()
        good = rig.coordinator.data["hw-1"]
        coordinator = _Coordinator({"broken": "not a mapping", "hw-1": good})
        rig.hass.data[DOMAIN]["entry-1"]["coordinator"] = coordinator
        added: list = []
        await platform.async_setup_entry(rig.hass, _Entry(True), added.extend)
        self.assertEqual(len(added), 1)
        self.assertIsInstance(added[0], platform.HonHeatPumpWaterHeater)

    async def test_reads_the_reporters_appliance(self) -> None:
        entity = (await _build(experimental=True))[0]
        self.assertEqual(entity.current_temperature, 33.0)
        self.assertEqual(entity.target_temperature, 40.0)
        self.assertEqual((entity.min_temp, entity.max_temp), (35.0, 75.0))
        self.assertEqual(entity.current_operation, "off")
        self.assertEqual(entity.operation_list, ["heat_pump", "eco", "electric", "off"])

    async def test_mode_change_while_off_is_refused_and_nothing_is_sent(self) -> None:
        entity = (await _build(experimental=True))[0]
        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_set_operation_mode("eco")
        self.assertEqual(ctx.exception.translation_key, "hpwh_switch_on_first")
        self.assertEqual(SENT, [])

    async def test_mode_change_sends_machmode_only(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        await entity.async_set_operation_mode("eco")
        patch = SENT[0]
        self.assertEqual((patch.command_name, dict(patch.values)), ("startProgram", {"machMode": "2"}))
        self.assertEqual(patch.program_name, "")

    async def test_each_mode_sends_the_machmode_its_schema_fixes(self) -> None:
        # Issue #115: the history recovery wrote ECO's "2" over the first category's
        # machMode in memory, and heat_pump then sent it. The schema is what names a
        # mode; the live value is not.
        appliance = _appliance()
        appliance.commands["startProgram"].categories["auto"].parameters["machMode"].value = "2"
        entity = (await _build(experimental=True, appliance=appliance, onOffStatus=1, machMode=3))[0]
        for operation in ("heat_pump", "eco", "electric"):
            await entity.async_set_operation_mode(operation)
        self.assertEqual([dict(p.values) for p in SENT],
                         [{"machMode": "1"}, {"machMode": "2"}, {"machMode": "3"}])

    async def test_eco_chosen_while_eco_is_active_sends_nothing(self) -> None:
        # The app sends no startProgram when ECO is chosen in ECO: it goes on to the eco
        # time-slot writes (apk2 decomp.txt:4506227-4506231), which are not offered here.
        entity = (await _build(experimental=True, onOffStatus=1, machMode=2))[0]
        await entity.async_set_operation_mode("eco")
        self.assertEqual(SENT, [])

    async def test_auto_or_electric_chosen_again_is_still_sent(self) -> None:
        # Only ECO is skipped: for the other modes the app sends startProgram whatever
        # the current one (apk2 decomp.txt:4506197-4506226).
        for operation, mode in (("heat_pump", 1), ("electric", 3)):
            entity = (await _build(experimental=True, onOffStatus=1, machMode=mode))[0]
            await entity.async_set_operation_mode(operation)
            self.assertEqual([dict(p.values) for p in SENT], [{"machMode": str(mode)}])

    async def test_eco_while_eco_still_obeys_the_refusals(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=0, machMode=2))[0]
        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_set_operation_mode("eco")
        self.assertEqual(ctx.exception.translation_key, "hpwh_switch_on_first")

    async def test_a_fractional_target_is_shown_on_the_whole_degree_grid(self) -> None:
        # Some M7 boilers publish a fractional target (52.2, 59.2, 62.8: issue #115
        # research). Shown as published, every +/- of Home Assistant's card asked for
        # 53.2 or 51.2, which `async_set_temperature` refuses.
        for published, shown in ((52.2, 52.0), (59.5, 60.0), (62.8, 63.0), (40.0, 40.0)):
            entity = (await _build(experimental=True, onOffStatus=1, tempSel=published))[0]
            self.assertEqual(entity.target_temperature, shown, published)
        entity = (await _build(experimental=True, onOffStatus=1, tempSel=52.2))[0]
        await entity.async_set_temperature(temperature=entity.target_temperature + 1)
        self.assertEqual([dict(p.values) for p in SENT], [{"tempSel": "53"}])

    async def test_off_mode_switches_off(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        await entity.async_set_operation_mode("off")
        self.assertEqual(dict(SENT[0].values), {"onOffStatus": "0"})

    async def test_turn_on(self) -> None:
        entity = (await _build(experimental=True))[0]
        await entity.async_turn_on()
        self.assertEqual(dict(SENT[0].values), {"onOffStatus": "1"})

    async def test_temperature(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        await entity.async_set_temperature(temperature=45)
        self.assertEqual(dict(SENT[0].values), {"tempSel": "45"})

    async def test_temperature_out_of_range_or_fractional_is_refused(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        for value in (30, 80, 45.5):
            with self.assertRaises(HomeAssistantError) as ctx:
                await entity.async_set_temperature(temperature=value)
            self.assertEqual(ctx.exception.translation_key, "invalid_setpoint", value)
        self.assertEqual(SENT, [])

    async def test_vacation_is_unknown(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1, machMode=4))[0]
        self.assertIsNone(entity.current_operation)

    async def test_an_unknown_power_state_is_unknown_not_off(self) -> None:
        entity = (await _build(experimental=True))[0]
        attributes = entity.coordinator.data["hw-1"]["attributes"]
        self.assertEqual(entity.current_operation, "off")  # the fixture's 0
        for value in ("2", ""):
            attributes["onOffStatus"] = value
            self.assertIsNone(entity.current_operation, value)
        del attributes["onOffStatus"]
        self.assertIsNone(entity.current_operation)

    async def test_turn_off(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        await entity.async_turn_off()
        self.assertEqual(dict(SENT[0].values), {"onOffStatus": "0"})

    async def test_non_numbers_are_refused_with_the_translated_error(self) -> None:
        # A range that admits 1, so True is refused for being a bool, not for its value.
        appliance = _appliance()
        appliance.commands["settings"].parameters["tempSel"] = _range(40, 0, 75)
        entity = (await _build(experimental=True, appliance=appliance, onOffStatus=1))[0]
        for value in (True, math.nan, math.inf, -math.inf):
            with self.assertRaises(HomeAssistantError) as ctx:
                await entity.async_set_temperature(temperature=value)
            self.assertEqual(ctx.exception.translation_key, "invalid_setpoint", value)
        self.assertEqual(SENT, [])

    async def test_a_fahrenheit_install_rounds_to_the_whole_degree(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        entity.hass.config = _units(_const.UnitOfTemperature.FAHRENHEIT)
        await entity.async_set_temperature(temperature=(105 - 32) * 5 / 9)  # 40.56
        self.assertEqual([dict(p.values) for p in SENT], [{"tempSel": "41"}])

    async def test_a_fractional_fahrenheit_is_refused_not_rounded(self) -> None:
        # PR #117 review: only a whole °F is rounded. 104.7 °F (40.39 °C) is refused,
        # as 40.4 °C is on a Celsius installation, instead of being sent as 40.
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        entity.hass.config = _units(_const.UnitOfTemperature.FAHRENHEIT)
        for fahrenheit in (104.7, 105.5):
            with self.assertRaises(HomeAssistantError) as ctx:
                await entity.async_set_temperature(temperature=(fahrenheit - 32) * 5 / 9)
            self.assertEqual(ctx.exception.translation_key, "invalid_setpoint", fahrenheit)
        self.assertEqual(SENT, [])
        # A finite but huge °F overflows the round trip back to °F: it must still be
        # the translated refusal, never an OverflowError (PR #117 review).
        for fahrenheit in (1e308, -1e308):
            with self.assertRaises(HomeAssistantError) as ctx:
                await entity.async_set_temperature(temperature=(fahrenheit - 32) / 1.8)
            self.assertEqual(ctx.exception.translation_key, "invalid_setpoint", fahrenheit)
        self.assertEqual(SENT, [])
        # Positive control: every whole °F of the range still goes through.
        for fahrenheit in range(95, 168):
            await entity.async_set_temperature(temperature=(fahrenheit - 32) * 5 / 9)
        self.assertEqual(len(SENT), 168 - 95)

    async def test_a_celsius_install_does_not_round(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        for value in (45.5, (105 - 32) * 5 / 9):
            with self.assertRaises(HomeAssistantError) as ctx:
                await entity.async_set_temperature(temperature=value)
            self.assertEqual(ctx.exception.translation_key, "invalid_setpoint", value)
        self.assertEqual(SENT, [])

    async def test_a_temperature_with_a_mode_is_refused(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_set_temperature(temperature=45, operation_mode="eco")
        self.assertEqual(ctx.exception.translation_key, "hpwh_set_temperature_operation_mode")
        self.assertEqual(SENT, [])

    async def test_vacation_and_sterilization_refuse_mode_and_temperature(self) -> None:
        cases = (
            ({"machMode": 4, "vacStartDate": "2026-10-01", "vacEndDate": "2026-10-10"},
             "hpwh_vacation_active"),
            ({"sterilizationCurrentStatus": 1}, "hpwh_sterilization_running"),
        )
        for overrides, key in cases:
            entity = (await _build(experimental=True, onOffStatus=1, **overrides))[0]
            with self.assertRaises(HomeAssistantError) as ctx:
                await entity.async_set_operation_mode("eco")
            self.assertEqual(ctx.exception.translation_key, key)
            with self.assertRaises(HomeAssistantError) as ctx:
                await entity.async_set_temperature(temperature=45)
            self.assertEqual(ctx.exception.translation_key, key)
            self.assertEqual(SENT, [])

    async def test_no_operation_mode_without_every_mode_category(self) -> None:
        feature = platform.WaterHeaterEntityFeature
        entity = (await _build(experimental=True))[0]
        self.assertTrue(entity.supported_features & feature.OPERATION_MODE)
        appliance = _appliance()
        del appliance.commands["startProgram"].categories["elec"]
        entity = (await _build(experimental=True, appliance=appliance))[0]
        self.assertFalse(entity.supported_features & feature.OPERATION_MODE)
        self.assertTrue(entity.supported_features & feature.TARGET_TEMPERATURE)


class BoostSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_not_created_without_the_experimental_option(self) -> None:
        self.assertEqual(await _build_switches(experimental=False), [])

    async def test_turning_on_at_target_is_refused(self) -> None:
        switch = (await _build_switches(experimental=True, onOffStatus=1, temp=40, tempSel=40))[0]
        with self.assertRaises(HomeAssistantError) as ctx:
            await switch.async_turn_on()
        self.assertEqual(ctx.exception.translation_key, "hpwh_boost_at_target")
        self.assertEqual(SENT, [])

    async def test_turning_on_below_target(self) -> None:
        switch = (await _build_switches(experimental=True, onOffStatus=1, temp=30, tempSel=40))[0]
        await switch.async_turn_on()
        self.assertEqual(dict(SENT[0].values), {"boostStatus": "1"})

    async def test_turning_off_sends_zero(self) -> None:
        switch = (await _build_switches(experimental=True, onOffStatus=1, boostStatus=1))[0]
        await switch.async_turn_off()
        self.assertEqual(dict(SENT[0].values), {"boostStatus": "0"})

    async def test_state(self) -> None:
        switch = (await _build_switches(experimental=True, onOffStatus=1, boostStatus=1))[0]
        self.assertTrue(switch.is_on)

    async def test_refusals_in_both_directions(self) -> None:
        cases = (
            ({"onOffStatus": 1, "errors": 5}, "hpwh_unavailable"),
            ({"onOffStatus": 1, "remoteCtrValid": 0}, "hpwh_unavailable"),
            ({"onOffStatus": 0}, "hpwh_switch_on_first"),
            ({"onOffStatus": 1, "machMode": 4, "vacStartDate": "2026-10-01",
              "vacEndDate": "2026-10-10"}, "hpwh_vacation_active"),
        )
        for overrides, key in cases:
            switch = (await _build_switches(experimental=True, temp=30, **overrides))[0]
            for turn in (switch.async_turn_on, switch.async_turn_off):
                with self.assertRaises(HomeAssistantError) as ctx:
                    await turn()
                self.assertEqual(ctx.exception.translation_key, key, overrides)
            self.assertEqual(SENT, [])


class BoostAutoOffListenerTest(unittest.IsolatedAsyncioTestCase):
    """The automatic boost off belongs to the config entry, not to an entity (I1/M10)."""

    async def test_the_dispatchers_own_echo_does_not_rearm_it(self) -> None:
        # The bug: a successful send writes boostStatus "0" into the local shadow
        # (dispatcher post-commit). If that "0" ended the episode, the cloud's stale
        # "1" on the next poll would look like a new boost and be switched off again,
        # once per poll.
        rig = await _rig(onOffStatus=1, boostStatus=1, temp=40)

        async def _echo(hass, client, appliance, patch) -> None:
            SENT.append(patch)
            rig.attributes["boostStatus"] = "0"
            rig.coordinator.fire()

        with mock.patch.object(platform, "async_dispatch_patch", _echo):
            rig.coordinator.fire()
            await _drain_tasks()
            rig.attributes["boostStatus"] = "1"  # the cloud has not caught up yet
            rig.coordinator.fire()
            await _drain_tasks()
        self.assertEqual([dict(p.values) for p in SENT], [{"boostStatus": "0"}])

    async def test_sent_once_when_the_water_reaches_the_target(self) -> None:
        rig = await _rig(onOffStatus=1, boostStatus=1, temp=40)
        rig.coordinator.fire()
        rig.coordinator.fire()
        await _drain_tasks()
        self.assertEqual([dict(p.values) for p in SENT], [{"boostStatus": "0"}])
        # As a background task of the entry, so Home Assistant cancels it on unload.
        self.assertEqual(rig.entry.task_names, ["addhon_hpwh_boost_auto_off"])

    async def test_not_sent_below_the_target(self) -> None:
        rig = await _rig(onOffStatus=1, boostStatus=1, temp=39)
        rig.coordinator.fire()
        await _drain_tasks()
        self.assertEqual(SENT, [])

    async def test_not_sent_when_neither_temperature_is_known(self) -> None:
        rig = await _rig(onOffStatus=1, boostStatus=1)
        del rig.attributes["temp"]
        del rig.attributes["tempSel"]
        rig.coordinator.fire()
        await _drain_tasks()
        self.assertEqual(SENT, [])

    async def test_the_water_leaving_the_target_and_coming_back_sends_again(self) -> None:
        rig = await _rig(onOffStatus=1, boostStatus=1, temp=40)
        rig.coordinator.fire()
        rig.attributes["temp"] = 39
        rig.coordinator.fire()
        rig.attributes["temp"] = 40
        rig.coordinator.fire()
        await _drain_tasks()
        self.assertEqual(len(SENT), 2)

    async def test_a_new_target_reached_within_the_same_boost_sends_again(self) -> None:
        rig = await _rig(onOffStatus=1, boostStatus=1, temp=40)
        rig.coordinator.fire()
        rig.attributes["tempSel"] = 45
        rig.attributes["temp"] = 45
        rig.coordinator.fire()
        await _drain_tasks()
        self.assertEqual(len(SENT), 2)

    async def test_a_new_target_ends_the_episode_even_with_boost_off(self) -> None:
        # PR #118 review: the target change was only seen while boost was on, so a
        # 45/45 update with boost off kept the 40 episode alive and the later boost at
        # 40/40 found it already done.
        rig = await _rig(onOffStatus=1, boostStatus=1, temp=40, tempSel=40)
        rig.coordinator.fire()
        await _drain_tasks()
        rig.attributes.update(boostStatus="0", temp=45, tempSel=45)
        rig.coordinator.fire()
        await _drain_tasks()
        rig.attributes.update(boostStatus="1", temp=40, tempSel=40)
        rig.coordinator.fire()
        await _drain_tasks()
        self.assertEqual(
            [dict(p.values) for p in SENT], [{"boostStatus": "0"}, {"boostStatus": "0"}]
        )

    async def test_float_temp_equals_string_target(self) -> None:
        rig = await _rig(onOffStatus=1, boostStatus=1, temp=40.0, tempSel="40")
        rig.coordinator.fire()
        await _drain_tasks()
        self.assertEqual(len(SENT), 1)

    async def test_works_without_a_water_heater_entity(self) -> None:
        appliance = _appliance()
        del appliance.commands["settings"].parameters["onOffStatus"]
        rig = await _rig(appliance=appliance, onOffStatus=1, boostStatus=1, temp=40)
        self.assertEqual(rig.added, [])
        rig.coordinator.fire()
        await _drain_tasks()
        self.assertEqual([dict(p.values) for p in SENT], [{"boostStatus": "0"}])

    async def test_not_registered_without_the_experimental_option(self) -> None:
        rig = await _rig(experimental=False, onOffStatus=1, boostStatus=1, temp=40)
        self.assertEqual(rig.coordinator.listeners, [])
        self.assertEqual(rig.entry.on_unload, [])

    async def test_skipped_for_the_m8b_series(self) -> None:
        appliance = _appliance()
        appliance.model_attributes = {"series": "M8B"}
        rig = await _rig(appliance=appliance, onOffStatus=1, boostStatus=1, temp=40)
        rig.coordinator.fire()
        await _drain_tasks()
        self.assertEqual(SENT, [])

    async def test_unload_removes_the_listener(self) -> None:
        rig = await _rig(onOffStatus=1, boostStatus=1, temp=40)
        self.assertEqual(len(rig.coordinator.listeners), 1)
        rig.entry.unload()
        self.assertEqual(rig.coordinator.listeners, [])
        rig.coordinator.fire()
        await _drain_tasks()
        self.assertEqual(SENT, [])

    async def test_a_failed_send_is_retried_at_most_three_times(self) -> None:
        # PR #117 review: one transient failure must not leave the boost on, and a
        # cloud that keeps refusing must not cost a warning a minute.
        rig = await _rig(onOffStatus=1, boostStatus=1, temp=40)
        calls: list = []

        async def _fail(hass, client, appliance, patch) -> None:
            calls.append(patch)
            raise HomeAssistantError("nope")

        with mock.patch.object(platform, "async_dispatch_patch", _fail):
            with self.assertLogs(platform._LOGGER, level="WARNING") as logs:
                for _ in range(6):
                    rig.coordinator.fire()
                    await _drain_tasks()
        self.assertEqual(len(calls), 3)
        self.assertIn("giving up", logs.output[-1])

    async def test_a_retry_that_succeeds_stops_the_episode(self) -> None:
        rig = await _rig(onOffStatus=1, boostStatus=1, temp=40)
        calls: list = []

        async def _fail_once(hass, client, appliance, patch) -> None:
            calls.append(patch)
            if len(calls) == 1:
                raise HomeAssistantError("transient")

        with mock.patch.object(platform, "async_dispatch_patch", _fail_once):
            with self.assertLogs(platform._LOGGER, level="WARNING"):
                for _ in range(5):
                    rig.coordinator.fire()
                    await _drain_tasks()
        self.assertEqual(len(calls), 2)

    async def test_no_second_send_while_one_is_in_flight(self) -> None:
        rig = await _rig(onOffStatus=1, boostStatus=1, temp=40)
        release = asyncio.Event()
        calls: list = []

        async def _slow(hass, client, appliance, patch) -> None:
            calls.append(patch)
            await release.wait()

        with mock.patch.object(platform, "async_dispatch_patch", _slow):
            for _ in range(3):
                rig.coordinator.fire()
                await _drain_tasks()
            self.assertEqual(len(calls), 1)
            release.set()
            await _drain_tasks()

    async def test_refused_states_send_nothing_and_cost_no_attempt(self) -> None:
        # PR #117 review: the automatic off obeys the manual switch's refusals, which the
        # app's own effect does not check. A refused update is not an attempt.
        for overrides in (
            {"onOffStatus": 0},
            {"onOffStatus": 1, "remoteCtrValid": 0},
            {"onOffStatus": 1, "errors": 5},
        ):
            with self.subTest(**overrides):
                rig = await _rig(boostStatus=1, temp=40, **overrides)
                for _ in range(4):
                    rig.coordinator.fire()
                    await _drain_tasks()
                self.assertEqual(SENT, [])
                # Once the refusal clears, the episode still has all its attempts.
                rig.attributes.update({"onOffStatus": 1, "remoteCtrValid": 1, "errors": 0})
                rig.coordinator.fire()
                await _drain_tasks()
                self.assertEqual([dict(p.values) for p in SENT], [{"boostStatus": "0"}])

    async def test_an_unexpected_error_is_logged_not_left_in_the_task(self) -> None:
        rig = await _rig(onOffStatus=1, boostStatus=1, temp=40)

        async def _boom(hass, client, appliance, patch) -> None:
            raise RuntimeError("executor blew up")

        with mock.patch.object(platform, "async_dispatch_patch", _boom):
            with self.assertLogs(platform._LOGGER, level="WARNING"):
                rig.coordinator.fire()
                await _drain_tasks()


class SeriesGateTest(unittest.IsolatedAsyncioTestCase):
    """No controls on the M7B/M8B/M11 series, whose rules are not rebuilt (M3)."""

    @staticmethod
    def _appliance(series):
        appliance = _appliance()
        appliance.model_attributes = {} if series is None else {"series": series}
        return appliance

    async def test_no_water_heater_on_an_excluded_series(self) -> None:
        for series in ("M7B", "m8b", " M11 "):
            self.assertEqual(
                await _build(experimental=True, appliance=self._appliance(series)), [], series
            )

    async def test_no_boost_switch_on_an_excluded_series(self) -> None:
        for series in ("M7B", "m8b", " M11 "):
            self.assertEqual(
                await _build_switches(experimental=True, appliance=self._appliance(series)),
                [],
                series,
            )

    async def test_a_missing_or_unknown_series_keeps_the_controls(self) -> None:
        for series in (None, "m8", "x9"):
            self.assertEqual(
                len(await _build(experimental=True, appliance=self._appliance(series))), 1, series
            )
            self.assertEqual(
                len(await _build_switches(experimental=True, appliance=self._appliance(series))),
                1,
                series,
            )
