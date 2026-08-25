# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cooker hood (HO) controls: fan, light, delayed switch-off, extra readings (#83).

The schema below is transcribed from the diagnostics dump of the reporting user's
HADG6DS46BWIFI, including the `settings` command's CATEGORISED shape -- that shape
is the whole reason the `category` selector exists, and the payload assertions here
exist to prove it never reaches the wire.

Stdlib unittest on the shared conftest stubs; no real Home Assistant install.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import dataclasses
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _mod(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_platform_stubs() -> None:
    """Seed what conftest does not: the modules `custom_components.addhon` imports.

    getattr-guarded throughout, so whichever test module lands first wins and this
    one never narrows a shared stub (see test_stub_hygiene).
    """
    ha = _mod("homeassistant")

    config_entries = _mod("homeassistant.config_entries")
    ha.config_entries = config_entries
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )

    core = _mod("homeassistant.core")
    ha.core = core
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    core.callback = getattr(core, "callback", lambda func: func)

    exceptions = _mod("homeassistant.exceptions")
    ha.exceptions = exceptions
    base_error = exceptions.HomeAssistantError
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_error,), {})
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions,
        "ConfigEntryAuthFailed",
        type("ConfigEntryAuthFailed", (base_error,), {}),
    )

    components = _mod("homeassistant.components")
    ha.components = components

    sensor_mod = _mod("homeassistant.components.sensor")
    components.sensor = sensor_mod

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        entity_category: object | None = None
        native_unit_of_measurement: str | None = None
        device_class: object | None = None
        state_class: object | None = None
        options: object | None = None

    class SensorDeviceClass:
        TEMPERATURE = "temperature"
        HUMIDITY = "humidity"
        ENERGY = "energy"
        WATER = "water"
        DURATION = "duration"
        PM25 = "pm25"
        PM10 = "pm10"
        CO2 = "carbon_dioxide"
        CO = "carbon_monoxide"
        AQI = "aqi"
        VOLATILE_ORGANIC_COMPOUNDS_PARTS = "volatile_organic_compounds_parts"
        WEIGHT = "weight"
        BATTERY = "battery"
        POWER = "power"
        ENUM = "enum"
        TIMESTAMP = "timestamp"

    class SensorStateClass:
        MEASUREMENT = "measurement"
        TOTAL = "total"
        TOTAL_INCREASING = "total_increasing"

    sensor_mod.SensorEntityDescription = getattr(
        sensor_mod, "SensorEntityDescription", SensorEntityDescription
    )
    sensor_mod.SensorEntity = getattr(
        sensor_mod, "SensorEntity", type("SensorEntity", (), {})
    )
    sensor_mod.SensorDeviceClass = getattr(
        sensor_mod, "SensorDeviceClass", SensorDeviceClass
    )
    sensor_mod.SensorStateClass = getattr(
        sensor_mod, "SensorStateClass", SensorStateClass
    )

    binary_mod = _mod("homeassistant.components.binary_sensor")
    components.binary_sensor = binary_mod

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class BinarySensorEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        entity_category: object | None = None
        device_class: object | None = None

    class BinarySensorDeviceClass:
        DOOR = "door"
        PROBLEM = "problem"
        RUNNING = "running"
        OCCUPANCY = "occupancy"
        LIGHT = "light"
        CONNECTIVITY = "connectivity"
        HEAT = "heat"
        SAFETY = "safety"
        LOCK = "lock"
        POWER = "power"

    binary_mod.BinarySensorEntityDescription = getattr(
        binary_mod, "BinarySensorEntityDescription", BinarySensorEntityDescription
    )
    binary_mod.BinarySensorEntity = getattr(
        binary_mod, "BinarySensorEntity", type("BinarySensorEntity", (), {})
    )
    binary_mod.BinarySensorDeviceClass = getattr(
        binary_mod, "BinarySensorDeviceClass", BinarySensorDeviceClass
    )

    entity_platform = _mod("homeassistant.helpers.entity_platform")
    sys.modules["homeassistant.helpers"].entity_platform = entity_platform
    entity_platform.AddEntitiesCallback = getattr(
        entity_platform, "AddEntitiesCallback", object
    )


_install_platform_stubs()

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.addhon import hood  # noqa: E402
from custom_components.addhon.const import APPLIANCE_HO  # noqa: E402

# The live `settings` command of the reporting hood, verbatim from the dump apart
# from the two endpoint strings (not parameters, and never sent). `windSpeed` 0..5
# and `lightStatus` 0..1 are the two writable controls issue #83 asks for.
HOOD_SETTINGS_PARAMS = {
    "clockHH": {"typology": "range", "category": "command", "mandatory": 0,
                "minimumValue": "0", "maximumValue": "21", "incrementValue": "1",
                "defaultValue": "0"},
    "clockMM": {"typology": "range", "category": "command", "mandatory": 0,
                "minimumValue": "0", "maximumValue": "59", "incrementValue": "1",
                "defaultValue": "0"},
    "clockSS": {"typology": "range", "category": "command", "mandatory": 0,
                "minimumValue": "0", "maximumValue": "59", "incrementValue": "1",
                "defaultValue": "0"},
    "delayTime": {"typology": "range", "category": "command", "mandatory": 0,
                  "minimumValue": "1", "maximumValue": "99", "incrementValue": "1",
                  "defaultValue": "1"},
    "delayTimeStatus": {"typology": "range", "category": "command", "mandatory": 0,
                        "minimumValue": "0", "maximumValue": "1",
                        "incrementValue": "1", "defaultValue": "0"},
    "filterCleaningAlarmStatus": {"typology": "fixed", "category": "command",
                                  "mandatory": 0, "fixedValue": "1"},
    "lightStatus": {"typology": "range", "category": "command", "mandatory": 0,
                    "minimumValue": "0", "maximumValue": "1", "incrementValue": "1",
                    "defaultValue": "0"},
    "quickDelayTimeStatus": {"typology": "range", "category": "command",
                             "mandatory": 0, "minimumValue": "0",
                             "maximumValue": "1", "incrementValue": "1",
                             "defaultValue": "0"},
    "windSpeed": {"typology": "range", "category": "command", "mandatory": 0,
                  "minimumValue": "0", "maximumValue": "5", "incrementValue": "1",
                  "defaultValue": "0"},
}

# `stopProgram` pins all three of its parameters, light included: switching the fan
# off switches the light off, and that is the device's own declaration.
HOOD_STOP_PARAMS = {
    "onOffStatus": {"typology": "fixed", "category": "command", "mandatory": 1,
                    "fixedValue": "0"},
    "windSpeed": {"typology": "fixed", "category": "command", "mandatory": 0,
                  "fixedValue": "0"},
    "lightStatus": {"typology": "fixed", "category": "command", "mandatory": 0,
                    "fixedValue": "0"},
}

# The raw cloud key the hood's single `startProgram` category is filed under. The
# dump stores only the CLEANED name ("undefined"), so this literal is a
# reconstruction: what the schema proves is its SHAPE -- it contains the substring
# "PROGRAM" (or the engine would have named the synthetic parameter differently)
# and its last dot-segment lowercases to "undefined". Nothing below asserts the
# string itself; what is asserted is that whatever it is never reaches the wire.
HOOD_START_CATEGORY = "PROGRAMS.HO.UNDEFINED"

HOOD_START_PARAMS = {
    "onOffStatus": {"typology": "fixed", "category": "command", "mandatory": 1,
                    "fixedValue": "1"},
    "windSpeed": {"typology": "range", "category": "command", "mandatory": 0,
                  "minimumValue": "0", "maximumValue": "5", "incrementValue": "1",
                  "defaultValue": "0"},
    "lightStatus": {"typology": "range", "category": "command", "mandatory": 0,
                    "minimumValue": "0", "maximumValue": "1", "incrementValue": "1",
                    "defaultValue": "1"},
}

# The shadow the hood publishes while stopped, trimmed to the keys the entities read.
HOOD_ATTRIBUTES = {
    "available": True,
    "windSpeed": 0,
    "lightStatus": 0,
    "onOffStatus": 0,
    "machMode": 0,
    "errors": 0,
    "delayTime": 0,
    "delayTimeStatus": 0,
    "quickDelayTimeStatus": 0,
    "filterCleaningAlarmStatus": 1,
    "filterCleaningStatus": "false",
    "lastWorkTime": 11147,
}


class RecordingApi:
    """Stands in for the transport: records exactly what would go on the wire."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_command(
        self, appliance, command, parameters, ancillary_parameters, program_name=""
    ) -> bool:
        self.sent.append(
            {
                "command": command,
                "parameters": dict(parameters),
                "ancillary": dict(ancillary_parameters),
                "program_name": program_name,
            }
        )
        return True


class _HoodAppliance:
    def __init__(self) -> None:
        self.zone = 0
        self.options: dict[str, str] = {}
        self.commands: dict[str, object] = {}
        self.unique_id = "ho-1"
        self.api = RecordingApi()
        self.synced: list[str] = []
        # The device SHADOW, deliberately empty of the clock fields: the reporting
        # hood's dump carries `windSpeed`, `lightStatus`, `delayTime` and
        # `delayTimeStatus` as bare attributes and no `clockHH`/`clockMM`/`clockSS`
        # at all. That absence is the whole reason the settings writes are sparse.
        self.attributes: dict[str, dict] = {"parameters": {}}
        self.synced_payloads: list[dict] = []

    def sync_command_to_params(self, name: str) -> None:
        self.synced.append(name)

    def sync_payload_to_params(self, params) -> None:
        # The dispatcher's post-commit reconciliation. Recorded rather than
        # applied: this fixture has no shadow parameters to fold the payload into.
        self.synced_payloads.append(dict(params))


# The INACTIVE half of the settings command. The dump prints only the active
# category, so the parameter names here are the two `settings.*` readings its
# attribute view carries and its active category does not; what this fixture needs
# from them is only that a SECOND category exists, which the dump proves outright
# (`category` is an enum of exactly ["setConfig", "setParameters"]).
HOOD_CONFIG_PARAMS = {
    "httpEndpoint": {"typology": "fixed", "category": "command", "mandatory": 0,
                     "fixedValue": ""},
    "mqttEndpoint": {"typology": "fixed", "category": "command", "mandatory": 0,
                     "fixedValue": ""},
}


def _appliance(
    settings_params: dict | None = None,
    *,
    categorised: bool = True,
    start_params: dict | None = None,
):
    """A hood appliance carrying the live schema of the reporting device.

    `categorised` reproduces the shape the command loader really builds for this
    hood: a `settings` command with TWO categories, `setParameters` active, and a
    `startProgram` filed under ONE placeholder category, which is what makes the
    engine attach the synthetic selector parameters. Building the fixture the flat
    way, or with a single category, would make every "no selector" assertion below
    pass for the wrong reason -- a selector with nothing to select can swap
    nothing -- and it would hide the `programName` the transport stamps on a
    categorised `startProgram`.

    THE SPEED RANGE IS ONE KNOB, MIRRORED ONTO BOTH COMMANDS, because the real hood
    declares the identical 0..5 range on `settings.windSpeed` and on
    `startProgram.windSpeed`. A test that reshapes `settings_params["windSpeed"]`
    is reshaping the speed axis, and the axis has to move on the command the fan
    actually writes. Pass `start_params` explicitly to break the mirror and drive
    the two commands apart.
    """
    from custom_components.addhon.client.engine.commands import HonCommand

    appliance = _HoodAppliance()
    params = copy.deepcopy(
        HOOD_SETTINGS_PARAMS if settings_params is None else settings_params
    )
    start = copy.deepcopy(
        HOOD_START_PARAMS if start_params is None else start_params
    )
    if start_params is None:
        if "windSpeed" in params:
            start["windSpeed"] = copy.deepcopy(params["windSpeed"])
        else:
            start.pop("windSpeed", None)
    categories: dict[str, HonCommand] = {}
    settings = HonCommand(
        "settings",
        {"parameters": params},
        appliance,
        categories=categories if categorised else None,
        category_name="setParameters" if categorised else "",
    )
    if categorised:
        categories["setParameters"] = settings
        categories["setConfig"] = HonCommand(
            "settings",
            {"parameters": copy.deepcopy(HOOD_CONFIG_PARAMS)},
            appliance,
            categories=categories,
            category_name="setConfig",
        )
    appliance.commands["settings"] = settings
    appliance.commands["stopProgram"] = HonCommand(
        "stopProgram", {"parameters": copy.deepcopy(HOOD_STOP_PARAMS)}, appliance
    )
    start_categories: dict[str, HonCommand] = {}
    appliance.commands["startProgram"] = HonCommand(
        "startProgram",
        {"parameters": start},
        appliance,
        categories=start_categories if categorised else None,
        category_name=HOOD_START_CATEGORY if categorised else "",
    )
    if categorised:
        # Filed under the CLEANED name, exactly like `HonCommandLoader`: the raw key
        # goes to `category_name`, `_clean_name` lowercases its last dot-segment,
        # and that is the key the categories dict uses.
        start_categories["undefined"] = appliance.commands["startProgram"]
    return appliance


class RunningClient:
    """Runs whatever the two senders hand it, like the real client's loop.

    Both entry points are REAL: `run_command_sync` drives the legacy sender's
    coroutine and `dispatch_patch_sync` drives the actual `CommandDispatcher`, so
    every payload assertion below reads what the transport would have received --
    not what a fake dispatcher decided to record.
    """

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.patches: list = []

    def run_command_sync(self, coro):
        if self.fail is not None:
            coro.close()
            raise self.fail
        return asyncio.run(coro)

    def dispatch_patch_sync(self, appliance, patch):
        from custom_components.addhon.command_dispatch import CommandDispatcher

        if self.fail is not None:
            raise self.fail
        self.patches.append(patch)
        return asyncio.run(CommandDispatcher().dispatch(appliance, patch))


class RecordingHass:
    def __init__(self, data: dict) -> None:
        self.data = data

    async def async_add_executor_job(self, func, *args):
        # A real worker thread, not an inline call: the legacy sender ends in
        # `client.run_command_sync`, which drives a coroutine to completion and
        # cannot do that from inside the running loop. Mirrors production, where
        # the client owns a loop of its own.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(func, *args).result(timeout=5)


class RefreshingCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.last_update_success = True
        self.refreshes = 0

    async def async_refresh(self) -> None:
        self.refreshes += 1


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1", options: dict | None = None) -> None:
        self.entry_id = entry_id
        self.options = dict(options or {})


def _entry_data(attributes: dict | None = None, appliance=None) -> dict:
    return {
        "ho-1": {
            "type": APPLIANCE_HO,
            "name": "Hood",
            "model": "HADG6DS46BWIFI",
            "attributes": (
                copy.deepcopy(HOOD_ATTRIBUTES) if attributes is None else attributes
            ),
            "appliance": _appliance() if appliance is None else appliance,
            "settings": {},
            "statistics": {},
        }
    }


async def _build(platform_name: str, data: dict, client=None, options=None) -> list:
    from importlib import import_module

    from custom_components.addhon.const import DOMAIN

    platform = import_module(f"custom_components.addhon.{platform_name}")
    coordinator = RefreshingCoordinator(data)
    hass = RecordingHass(
        {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": client}}}
    )
    added: list = []
    await platform.async_setup_entry(
        hass, FakeEntry(options=options), added.extend
    )
    for entity in added:
        entity.hass = hass
    # Drop the account-level diagnostic entities: they are not per-appliance.
    return [e for e in added if not getattr(e, "_addhon_account", False)]


class HoodFanGatingTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_hood_declaring_a_wind_speed_range_gets_a_fan(self) -> None:
        added = await _build("fan", _entry_data())
        self.assertEqual(["ho-1_hood"], [e._attr_unique_id for e in added])

    async def test_a_hood_without_a_writable_wind_speed_gets_no_fan(self) -> None:
        params = {
            name: body
            for name, body in HOOD_SETTINGS_PARAMS.items()
            if name != "windSpeed"
        }
        data = _entry_data(appliance=_appliance(params))
        self.assertEqual([], await _build("fan", data))

    async def test_the_fan_is_gated_on_the_command_it_writes(self) -> None:
        # `settings` still declares a perfectly good 0..5 range here; `startProgram`
        # does not declare the parameter at all. Gating on the first while writing
        # to the second would ship a slider with nowhere to land.
        start = {
            name: body
            for name, body in HOOD_START_PARAMS.items()
            if name != "windSpeed"
        }
        data = _entry_data(appliance=_appliance(start_params=start))
        self.assertIn("windSpeed", HOOD_SETTINGS_PARAMS)
        self.assertEqual([], await _build("fan", data))

    async def test_a_pinned_wind_speed_gets_no_fan(self) -> None:
        # A firmware that fixes windSpeed to a single value offers no choice; a
        # slider with one position would be worse than no slider.
        params = dict(HOOD_SETTINGS_PARAMS)
        params["windSpeed"] = {
            "typology": "range", "category": "command", "mandatory": 0,
            "minimumValue": "0", "maximumValue": "0", "incrementValue": "1",
            "defaultValue": "0",
        }
        data = _entry_data(appliance=_appliance(params))
        self.assertEqual([], await _build("fan", data))

    async def test_the_speed_axis_comes_from_the_schema_not_from_the_model(
        self,
    ) -> None:
        # `model_attributes.speedLevel` is 5 on this hood too, so a fixture that
        # agreed with the schema could not tell the two sources apart: the schema
        # here declares FOUR levels while the catalogue would still say five.
        params = dict(HOOD_SETTINGS_PARAMS)
        params["windSpeed"] = {
            "typology": "range", "category": "command", "mandatory": 0,
            "minimumValue": "0", "maximumValue": "4", "incrementValue": "1",
            "defaultValue": "0",
        }
        data = _entry_data(appliance=_appliance(params))
        added = await _build("fan", data)
        self.assertEqual(4, added[0].speed_count)


class HoodFanPercentageTest(unittest.IsolatedAsyncioTestCase):
    async def _fan(self, wind_speed, client=None):
        attributes = copy.deepcopy(HOOD_ATTRIBUTES)
        attributes["windSpeed"] = wind_speed
        added = await _build("fan", _entry_data(attributes), client=client)
        return added[0]

    async def test_the_declared_features_are_speed_and_power(self) -> None:
        from homeassistant.components.fan import FanEntityFeature

        fan_entity = await self._fan(0)
        self.assertEqual(
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF,
            fan_entity.supported_features,
        )

    async def test_five_levels_become_five_steps(self) -> None:
        fan_entity = await self._fan(0)
        self.assertEqual(5, fan_entity.speed_count)
        self.assertEqual(20, fan_entity.percentage_step)

    async def test_a_stopped_hood_reads_zero_percent_and_off(self) -> None:
        fan_entity = await self._fan(0)
        self.assertEqual(0, fan_entity.percentage)
        self.assertIs(False, fan_entity.is_on)

    async def test_the_lowest_and_highest_levels_map_to_the_ends(self) -> None:
        lowest = await self._fan(1)
        highest = await self._fan(5)
        self.assertEqual(20, lowest.percentage)
        self.assertEqual(100, highest.percentage)
        self.assertIs(True, lowest.is_on)
        self.assertIs(True, highest.is_on)

    async def test_every_level_round_trips_through_the_percentage_axis(self) -> None:
        # The property and the setter must agree: a level rendered as N % has to be
        # the level a write of N % produces, or the slider drifts one step per drag.
        for level in (1, 2, 3, 4, 5):
            fan_entity = await self._fan(level, client=RunningClient())
            await fan_entity.async_set_percentage(fan_entity.percentage)
            sent = fan_entity._appliance.api.sent[-1]
            self.assertEqual(str(level), sent["parameters"]["windSpeed"], level)

    async def test_a_level_above_the_declared_range_is_clamped_not_overflowed(
        self,
    ) -> None:
        fan_entity = await self._fan(9)
        self.assertEqual(100, fan_entity.percentage)

    async def test_an_unreadable_level_reads_unknown_rather_than_off(self) -> None:
        attributes = copy.deepcopy(HOOD_ATTRIBUTES)
        attributes["windSpeed"] = ""
        added = await _build("fan", _entry_data(attributes))
        self.assertIsNone(added[0].percentage)
        self.assertIsNone(added[0].is_on)


class HoodFanSteppedGridTest(unittest.IsolatedAsyncioTestCase):
    """A hood whose wind speed advances in steps larger than 1.

    The hood of issue #83 declares `incrementValue: "1"`, so every integer between
    the bounds is legal there and the increment could be dropped without anyone
    noticing. A hood declaring 0..6 step 2 accepts 0, 2, 4 and 6 and REFUSES 1, 3
    and 5: modelling it as six selectable levels puts three phantom notches on the
    slider and turns most of the slider into a command the device rejects.
    Reported independently by two reviewers on PR #87.

    The grid is enumerated from the device's own lower bound, not from
    `HOOD_MIN_SPEED`: walking 1, 3, 5 from 1 would be just as off-grid as
    interpolating, and it would look right in a test that only counted the levels.
    """

    _STEPPED = {
        "typology": "range", "category": "command", "mandatory": 0,
        "minimumValue": "0", "maximumValue": "6", "incrementValue": "2",
        "defaultValue": "0",
    }

    async def _fan(self, wind_speed=0, client=None):
        params = dict(HOOD_SETTINGS_PARAMS)
        params["windSpeed"] = self._STEPPED
        attributes = copy.deepcopy(HOOD_ATTRIBUTES)
        attributes["windSpeed"] = wind_speed
        data = _entry_data(attributes, appliance=_appliance(params))
        added = await _build("fan", data, client=client)
        return added[0]

    def test_the_levels_are_the_declared_grid(self) -> None:
        params = dict(HOOD_SETTINGS_PARAMS)
        params["windSpeed"] = self._STEPPED
        self.assertEqual((2, 4, 6), hood.speed_levels(_appliance(params)))

    async def test_three_legal_levels_become_three_steps(self) -> None:
        fan_entity = await self._fan()
        self.assertEqual(3, fan_entity.speed_count)

    async def test_every_percentage_writes_a_level_the_schema_declares(self) -> None:
        # The whole point: NO write may land between two legal levels.
        for percent in range(1, 101):
            fan_entity = await self._fan(client=RunningClient())
            await fan_entity.async_set_percentage(percent)
            sent = fan_entity._appliance.api.sent[-1]["parameters"]["windSpeed"]
            self.assertIn(sent, {"2", "4", "6"}, percent)

    async def test_the_grid_round_trips(self) -> None:
        for level in (2, 4, 6):
            fan_entity = await self._fan(level, client=RunningClient())
            await fan_entity.async_set_percentage(fan_entity.percentage)
            sent = fan_entity._appliance.api.sent[-1]["parameters"]["windSpeed"]
            self.assertEqual(str(level), sent, level)

    async def test_turning_on_bare_uses_the_lowest_LEGAL_level(self) -> None:
        # Not HOOD_MIN_SPEED: 1 is not a value this hood accepts.
        fan_entity = await self._fan(client=RunningClient())
        await fan_entity.async_turn_on()
        sent = fan_entity._appliance.api.sent[-1]["parameters"]["windSpeed"]
        self.assertEqual("2", sent)

    async def test_an_off_grid_reading_is_placed_without_raising(self) -> None:
        # A firmware is free to report a level the writable range does not list.
        # `percentage` must survive it: a property that raises takes the entity
        # down, and the reading is already the device's own inconsistency.
        fan_entity = await self._fan(3)
        self.assertIn(fan_entity.percentage, {33, 66})
        self.assertIs(True, fan_entity.is_on)


class HoodFanStepOneUnchangedTest(unittest.IsolatedAsyncioTestCase):
    """The step-1 hood must convert EXACTLY as it did before the grid landed.

    The percentage axis moved from the ranged helpers to the ordered-list ones,
    which agree on a contiguous 1..N list and diverge on everything else. This
    pins the agreement on the hood we actually have a dump for, so a later change
    to the conversion cannot quietly shift the hood of issue #83 by one notch.
    """

    async def _fan(self, wind_speed, client=None):
        attributes = copy.deepcopy(HOOD_ATTRIBUTES)
        attributes["windSpeed"] = wind_speed
        added = await _build("fan", _entry_data(attributes), client=client)
        return added[0]

    async def test_the_five_levels_keep_their_percentages(self) -> None:
        for level, percent in ((1, 20), (2, 40), (3, 60), (4, 80), (5, 100)):
            fan_entity = await self._fan(level)
            self.assertEqual(percent, fan_entity.percentage, level)

    async def test_the_percentage_boundaries_keep_their_levels(self) -> None:
        for percent, level in (
            (1, "1"), (20, "1"), (21, "2"), (40, "2"),
            (41, "3"), (60, "3"), (61, "4"), (80, "4"),
            (81, "5"), (100, "5"),
        ):
            fan_entity = await self._fan(0, client=RunningClient())
            await fan_entity.async_set_percentage(percent)
            sent = fan_entity._appliance.api.sent[-1]["parameters"]["windSpeed"]
            self.assertEqual(level, sent, percent)


class HoodFanWriteTest(unittest.IsolatedAsyncioTestCase):
    async def _fan(self, client=None, *, running: int = 0):
        """A hood fan, stopped by default; `running` reports a live wind speed.

        The two are not interchangeable for the OFF path: a fan already at zero is
        deliberately left alone (writing would wake the panel), so every test of
        what an off PUTS ON THE WIRE has to start from a hood that is extracting.
        """
        client = RunningClient() if client is None else client
        attributes = copy.deepcopy(HOOD_ATTRIBUTES)
        attributes["windSpeed"] = running
        added = await _build("fan", _entry_data(attributes), client=client)
        return added[0]

    async def test_turning_on_without_a_speed_uses_the_lowest_level(self) -> None:
        fan_entity = await self._fan()
        await fan_entity.async_turn_on()
        sent = fan_entity._appliance.api.sent
        self.assertEqual(1, len(sent))
        self.assertEqual("startProgram", sent[0]["command"])
        self.assertEqual("1", sent[0]["parameters"]["windSpeed"])

    async def test_turning_on_with_a_percentage_uses_that_level(self) -> None:
        fan_entity = await self._fan()
        await fan_entity.async_turn_on(percentage=60)
        self.assertEqual("3", fan_entity._appliance.api.sent[0]["parameters"]["windSpeed"])

    async def test_a_sliver_of_a_percent_still_starts_the_fan(self) -> None:
        # Rounding to nearest would make the bottom tenth of the slider a silent
        # no-op that reads as "the integration ignored me".
        fan_entity = await self._fan()
        await fan_entity.async_set_percentage(1)
        self.assertEqual("1", fan_entity._appliance.api.sent[0]["parameters"]["windSpeed"])

    async def test_an_off_grid_percentage_rounds_up_to_the_next_level(self) -> None:
        # Five levels put slider notches at 20/40/60/80/100, but a script can ask
        # for anything. 30 % sits between level 1 and level 2: rounding DOWN would
        # make "a bit more than level 1" mean level 1, which is the same silent
        # no-op the sliver case above rules out, one notch higher.
        fan_entity = await self._fan()
        await fan_entity.async_set_percentage(30)
        self.assertEqual("2", fan_entity._appliance.api.sent[0]["parameters"]["windSpeed"])

    async def test_zero_percent_is_a_zero_speed_not_a_stop(self) -> None:
        # The regression issue #83 came back for: a 0 % slider used to send
        # `stopProgram`, which darkens the control panel, and from the dark panel
        # the hood ignores everything Home Assistant sends it afterwards.
        fan_entity = await self._fan(running=3)
        await fan_entity.async_set_percentage(0)
        sent = fan_entity._appliance.api.sent
        self.assertEqual(["startProgram"], [s["command"] for s in sent])
        self.assertEqual("0", sent[0]["parameters"]["windSpeed"])

    async def test_turning_the_fan_off_leaves_the_panel_and_the_light_alone(
        self,
    ) -> None:
        # The payload is the whole assertion: `lightStatus` is absent, so the light
        # keeps whatever state the user left it in, and `onOffStatus` is the "1" the
        # schema pins on this command, so the panel stays lit.
        fan_entity = await self._fan(running=3)
        await fan_entity.async_turn_off()
        sent = fan_entity._appliance.api.sent
        self.assertEqual(["startProgram"], [s["command"] for s in sent])
        self.assertEqual(
            {"windSpeed": "0", "onOffStatus": "1"}, sent[0]["parameters"]
        )

    async def test_turning_off_a_stopped_fan_does_not_wake_the_panel(self) -> None:
        # Every write on this channel carries the schema's mandatory
        # `onOffStatus = "1"`, so an off sent to a hood that is ALREADY stopped
        # would switch the appliance on. `fan.turn_off` and a
        # `homeassistant.turn_off` sweeping an area both reach this method whatever
        # the entity currently reports, so the guard has to be in the entity.
        fan_entity = await self._fan(running=0)
        self.assertIs(False, fan_entity.is_on)
        await fan_entity.async_turn_off()
        self.assertEqual([], fan_entity._appliance.api.sent)
        # The user still asked for something, so the state is still reconciled.
        self.assertEqual(1, fan_entity.coordinator.refreshes)

    async def test_a_refresh_that_reveals_a_running_fan_still_sends_the_off(
        self,
    ) -> None:
        # The cached reading is what the guard looks at, and it can be stale: a
        # hood started from its own panel or from the app still reports zero until
        # the next poll. Skipping on that first look would make `turn_off` report
        # success while the fan kept running, so the guard refreshes and looks
        # again before deciding it has nothing to do.
        fan_entity = await self._fan(running=0)
        coordinator = fan_entity.coordinator
        polled = coordinator.async_refresh

        async def reveal_a_running_fan() -> None:
            await polled()
            coordinator.data["ho-1"]["attributes"]["windSpeed"] = 3

        coordinator.async_refresh = reveal_a_running_fan
        await fan_entity.async_turn_off()
        self.assertEqual(
            {"windSpeed": "0", "onOffStatus": "1"},
            fan_entity._appliance.api.sent[0]["parameters"],
        )

    async def test_an_unreadable_level_still_writes_the_off(self) -> None:
        # Unknown is not a reason to swallow a command the user asked for, and the
        # guard must not turn a hood whose reading broke into a fan that cannot be
        # stopped.
        attributes = copy.deepcopy(HOOD_ATTRIBUTES)
        attributes["windSpeed"] = ""
        added = await _build("fan", _entry_data(attributes), client=RunningClient())
        fan_entity = added[0]
        self.assertIsNone(fan_entity.is_on)
        await fan_entity.async_turn_off()
        self.assertEqual(
            {"windSpeed": "0", "onOffStatus": "1"},
            fan_entity._appliance.api.sent[0]["parameters"],
        )

    async def test_a_speed_change_is_the_official_app_body(self) -> None:
        # `apk/decomp.txt:3137444-3137456`: the app's hood speed intent is exactly
        # these two keys. We do not build it -- the dispatcher adds `onOffStatus`
        # because the live schema marks it mandatory -- but the result has to match.
        fan_entity = await self._fan()
        await fan_entity.async_set_percentage(60)
        self.assertEqual(
            {"windSpeed": "3", "onOffStatus": "1"},
            fan_entity._appliance.api.sent[0]["parameters"],
        )

    async def test_no_hood_write_ever_ships_a_program_name(self) -> None:
        # `api.send_command` appends `programName` to EVERY startProgram whose
        # command carries a category name (pinned by
        # test_transport_api::test_send_command_start_program_adds_program_name),
        # and this hood's startProgram IS categorised -- the fixture files it under
        # a raw key exactly as the loader does. The app's own hood body has no such
        # field, so `hood.hood_patch` suppresses it, and the empty string recorded
        # below is that suppression reaching the transport.
        fan_entity = await self._fan(running=3)
        appliance = fan_entity._appliance
        self.assertEqual(
            HOOD_START_CATEGORY, appliance.commands["startProgram"].category
        )
        await fan_entity.async_set_percentage(80)
        await fan_entity.async_turn_off()
        sent = appliance.api.sent
        self.assertEqual(["startProgram", "startProgram"], [s["command"] for s in sent])
        self.assertEqual(["", ""], [s["program_name"] for s in sent])

    async def test_every_write_refreshes_the_state(self) -> None:
        fan_entity = await self._fan()
        await fan_entity.async_turn_on()
        self.assertEqual(1, fan_entity.coordinator.refreshes)

    async def test_a_missing_client_raises_the_localized_error(self) -> None:
        added = await _build("fan", _entry_data(), client=None)
        with self.assertRaises(HomeAssistantError) as caught:
            await added[0].async_turn_on()
        self.assertEqual(
            "appliance_or_client_unavailable", caught.exception.translation_key
        )

    async def test_a_transport_failure_becomes_a_localized_command_error(self) -> None:
        fan_entity = await self._fan(RunningClient(fail=RuntimeError("boom")))
        with self.assertRaises(HomeAssistantError) as caught:
            await fan_entity.async_turn_on()
        self.assertEqual("command_error", caught.exception.translation_key)


class HoodSelectorKeyTest(unittest.IsolatedAsyncioTestCase):
    """The `category`/`program` selector keys must never reach the wire.

    They are the dispatcher's `_SELECTOR_KEYS`: naming one makes the engine rewrite
    `appliance.commands` permanently AND ships the key verbatim to the cloud. No
    other write path in this repository does that, and nothing about the hood needs
    it -- the synthetic parameter lives in the `custom` group, which the send path
    skips, as long as no caller asks for it by name.
    """

    def test_the_fixture_really_carries_a_live_selector(self) -> None:
        # Without this the assertions below would pass on a hood that has no
        # selector to leak in the first place. The swap is exercised directly, so
        # the fixture is proven capable of the damage the tests deny.
        appliance = _appliance()
        settings = appliance.commands["settings"]
        self.assertIn("category", settings.parameters)
        self.assertEqual("custom", settings.parameters["category"].group)
        self.assertEqual(["setConfig", "setParameters"], settings.parameters["category"].values)

        settings.parameters["category"].value = "setConfig"
        self.assertIsNot(settings, appliance.commands["settings"])

    async def test_no_hood_write_ever_ships_category_or_program(self) -> None:
        client = RunningClient()
        appliance = _appliance()
        settings = appliance.commands["settings"]
        # A hood reported as EXTRACTING, so the fan's off is a real write and not
        # the idle no-op of `HonHoodFan.async_turn_off`.
        attributes = copy.deepcopy(HOOD_ATTRIBUTES)
        attributes["windSpeed"] = 3
        data = _entry_data(attributes, appliance=appliance)
        fan_entity = (await _build("fan", data, client=client))[0]
        switches = await _build("switch", data, client=client)
        power = next(s for s in switches if s._attr_unique_id == "ho-1_power")
        light = next(s for s in switches if s._attr_unique_id == "ho-1_light")
        timer = next(s for s in switches if s._attr_unique_id == "ho-1_delay_timer")
        number = (await _build("number", data, client=client))[0]

        await fan_entity.async_turn_on(percentage=100)
        await fan_entity.async_turn_off()
        await light.async_turn_on()
        await light.async_turn_off()
        await timer.async_turn_on()
        await number.async_set_native_value(30)
        await power.async_turn_on()
        await power.async_turn_off()

        self.assertEqual(8, len(appliance.api.sent), appliance.api.sent)
        for sent in appliance.api.sent:
            self.assertNotIn("category", sent["parameters"], sent)
            self.assertNotIn("program", sent["parameters"], sent)
            self.assertNotIn("category", sent["ancillary"], sent)
            self.assertNotIn("program", sent["ancillary"], sent)
        # The other half of the damage, and the half the wire cannot show: naming
        # the selector makes the engine REPLACE `appliance.commands['settings']`
        # with another category, permanently, for every later write.
        self.assertIs(settings, appliance.commands["settings"])

    async def test_no_hood_module_asks_for_a_selector_key_by_name(self) -> None:
        # The payload assertion above covers the paths a test can drive. This one
        # covers the ones it cannot: the only way a selector reaches the wire is a
        # caller naming it, so no hood-facing module may spell either name.
        component = REPO_ROOT / "custom_components" / "addhon"
        for name in ("hood.py", "fan.py"):
            source = (component / name).read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("*"):
                    continue
                self.assertNotIn('"category"', line, f"{name}: {line}")
                self.assertNotIn('"program"', line, f"{name}: {line}")


class HoodSparseWritePayloadTest(unittest.IsolatedAsyncioTestCase):
    """The EXACT key set a `settings` write puts on the wire.

    Every hood write once travelled through the FULL-command sender, which
    transmits the whole `parameters` group -- `clockHH`, `clockMM` and `clockSS`
    included. The device does not mirror those three into its shadow (they are
    absent from every bare attribute of the reporting dump), so
    `sync_params_to_command` can never refresh them: they sit at the 0 the schema
    loaded, and a light toggle, a speed change or a timer edit therefore reset the
    hood's CLOCK to zero. The same payload restated `filterCleaningAlarmStatus="1"`
    on every action. The wine cooler's #62 was this bug in another appliance.

    No test in this suite watched the key SET before this class: every payload
    assertion elsewhere reads ONE key out of the dict, and each of them is equally
    satisfied by a dict with nine. The `settings` writes are sparse patches through
    the transactional dispatcher now, and `RunningClient.dispatch_patch_sync` runs
    the REAL dispatcher, so what is asserted below is what the transport receives.

    `stopProgram` is a sparse patch too now, but it is not in here because it is not
    a `settings` write and carries no clock: it belongs to the power switch, and
    `HoodPowerSwitchTest.test_switching_off_is_the_payload_the_device_executed`
    pins its key set instead.
    """

    # What a full-group send would add to every one of the three writes below.
    # (The fan used to be a fourth; it writes `startProgram` now, which is what
    # test_a_speed_change_is_not_a_settings_write_at_all asserts.)
    LEAKED_BY_A_FULL_GROUP_SEND = (
        "clockHH",
        "clockMM",
        "clockSS",
        "filterCleaningAlarmStatus",
        "quickDelayTimeStatus",
    )

    async def _controls(self):
        """The three `settings` writers of one hood, plus the fan, over one shared
        appliance. The fan is here to prove it is NOT one of them any more."""
        client = RunningClient()
        appliance = _appliance()
        data = _entry_data(appliance=appliance)
        fan_entity = (await _build("fan", data, client=client))[0]
        switches = await _build("switch", data, client=client)
        number = (await _build("number", data, client=client))[0]
        return types.SimpleNamespace(
            appliance=appliance,
            fan=fan_entity,
            light=next(s for s in switches if s._attr_unique_id == "ho-1_light"),
            timer=next(
                s for s in switches if s._attr_unique_id == "ho-1_delay_timer"
            ),
            number=number,
        )

    def test_the_fixture_declares_what_a_full_group_send_would_leak(self) -> None:
        # Anti-vacuity, and the only thing standing between the assertions below
        # and a fixture that proves nothing: a `settings` command carrying just the
        # four written parameters would satisfy every "and nothing else" in this
        # class while the real device kept losing its clock.
        settings = _appliance().commands["settings"]
        for name in self.LEAKED_BY_A_FULL_GROUP_SEND:
            self.assertIn(name, settings.parameters, name)
        self.assertNotIn(
            "clockHH",
            HOOD_ATTRIBUTES,
            "the shadow now mirrors the clock; the premise of the fix has changed",
        )

    async def test_a_speed_change_is_not_a_settings_write_at_all(self) -> None:
        # The fan left this channel: `windSpeed` now travels on `startProgram`,
        # which is the only command that can also light the control panel. Asserted
        # here rather than only in HoodFanWriteTest so that moving it back would
        # have to face the clock argument this class exists for.
        controls = await self._controls()
        await controls.fan.async_set_percentage(60)
        sent = controls.appliance.api.sent[-1]
        self.assertEqual("startProgram", sent["command"])
        self.assertEqual({"windSpeed": "3", "onOffStatus": "1"}, sent["parameters"])

    async def test_a_light_toggle_carries_light_status_alone(self) -> None:
        controls = await self._controls()
        await controls.light.async_turn_on()
        self.assertEqual(
            {"lightStatus": "1"}, controls.appliance.api.sent[-1]["parameters"]
        )

    async def test_arming_the_timer_carries_its_flag_alone(self) -> None:
        controls = await self._controls()
        await controls.timer.async_turn_on()
        self.assertEqual(
            {"delayTimeStatus": "1"}, controls.appliance.api.sent[-1]["parameters"]
        )

    async def test_a_delay_time_edit_carries_the_delay_alone(self) -> None:
        controls = await self._controls()
        await controls.number.async_set_native_value(45)
        self.assertEqual(
            {"delayTime": "45"}, controls.appliance.api.sent[-1]["parameters"]
        )

    async def test_no_settings_write_ever_touches_the_hood_clock(self) -> None:
        # The four writes together, stated as the harm rather than as the key set:
        # whichever of them a future edit moves back to the full-command sender,
        # this fails.
        controls = await self._controls()
        await controls.fan.async_turn_on()
        await controls.light.async_turn_off()
        await controls.timer.async_turn_on()
        await controls.number.async_set_native_value(20)
        sent = [s for s in controls.appliance.api.sent if s["command"] == "settings"]
        # Three, not four: the fan's write above is a `startProgram` now, and the
        # test_a_speed_change_is_not_a_settings_write_at_all above is what says so.
        self.assertEqual(3, len(sent), controls.appliance.api.sent)
        for payload in sent:
            for name in self.LEAKED_BY_A_FULL_GROUP_SEND:
                self.assertNotIn(name, payload["parameters"], payload)
            self.assertEqual(1, len(payload["parameters"]), payload)


class HoodSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_power_the_light_and_the_timer_are_switches(self) -> None:
        added = await _build("switch", _entry_data(), client=RunningClient())
        self.assertEqual(
            ["ho-1_power", "ho-1_light", "ho-1_delay_timer"],
            [e._attr_unique_id for e in added],
        )

    async def test_the_light_is_gated_on_the_settings_parameter(self) -> None:
        params = {
            name: body
            for name, body in HOOD_SETTINGS_PARAMS.items()
            if name != "lightStatus"
        }
        data = _entry_data(appliance=_appliance(params))
        added = await _build("switch", data, client=RunningClient())
        self.assertEqual(
            ["ho-1_power", "ho-1_delay_timer"], [e._attr_unique_id for e in added]
        )

    async def test_the_light_writes_the_settings_light_status(self) -> None:
        appliance = _appliance()
        added = await _build(
            "switch", _entry_data(appliance=appliance), client=RunningClient()
        )
        light = next(e for e in added if e._attr_unique_id == "ho-1_light")
        await light.async_turn_on()
        sent = appliance.api.sent[-1]
        self.assertEqual("settings", sent["command"])
        self.assertEqual("1", sent["parameters"]["lightStatus"])

    async def test_the_light_reads_the_shadow_back(self) -> None:
        attributes = copy.deepcopy(HOOD_ATTRIBUTES)
        attributes["lightStatus"] = 1
        added = await _build("switch", _entry_data(attributes), client=RunningClient())
        light = next(e for e in added if e._attr_unique_id == "ho-1_light")
        self.assertIs(True, light.is_on)

    async def test_the_hood_uses_a_switch_and_not_the_light_platform(self) -> None:
        # The light platform was tried, removed and pinned out by
        # test_air_purifier_entities::test_the_light_platform_is_gone. The hood
        # reports a plain 0/1 with no brightness, so a switch is both enough and
        # honest; this asserts the hood did not quietly reopen that decision.
        from custom_components.addhon.const import PLATFORMS

        self.assertNotIn("light", PLATFORMS)


class HoodPowerSwitchTest(unittest.IsolatedAsyncioTestCase):
    """The panel axis: `onOffStatus`, one command per direction.

    The hood of issue #83 has THREE states, not two. `onOffStatus` is the lit-or-
    dark control panel, and while it is 0 the device ignores every speed and light
    command it receives -- it does not even beep, which is its acknowledgement
    tone. The first shipped version had no entity for this axis and darkened the
    panel from the fan's off, so Home Assistant could switch the hood off exactly
    once and then had to wait for someone to touch the glass.
    """

    async def _power(self, appliance=None, attributes=None, client=None):
        client = RunningClient() if client is None else client
        data = _entry_data(attributes, appliance=appliance)
        added = await _build("switch", data, client=client)
        return next(e for e in added if e._attr_unique_id == "ho-1_power")

    async def test_switching_on_is_a_bare_wake_up(self) -> None:
        # No values of ours at all: the dispatcher adds `onOffStatus` because the
        # live schema marks it mandatory and pins it to "1". Anything else in this
        # payload would be a speed or a light the user did not ask for.
        appliance = _appliance()
        power = await self._power(appliance)
        await power.async_turn_on()
        sent = appliance.api.sent[-1]
        self.assertEqual("startProgram", sent["command"])
        self.assertEqual({"onOffStatus": "1"}, sent["parameters"])

    async def test_switching_off_is_the_payload_the_device_executed(self) -> None:
        # Byte-for-byte the parameters of the one command this hood's own
        # `commandHistory` carries with both a `timestampAccepted` AND a
        # `timestampExecuted`. Switching the hood off switches its light off with
        # it: `stopProgram` pins all three values itself.
        appliance = _appliance()
        power = await self._power(appliance)
        await power.async_turn_off()
        sent = appliance.api.sent[-1]
        self.assertEqual("stopProgram", sent["command"])
        self.assertEqual(
            {"windSpeed": "0", "lightStatus": "0", "onOffStatus": "0"},
            sent["parameters"],
        )

    async def test_switching_off_names_only_what_this_hood_declares(self) -> None:
        # `stopProgram` schemas vary per model -- the minimal `{onOffStatus}` shape
        # is ordinary in this cloud, and a hood with no lamp is a device family this
        # platform already gates for. Naming a parameter the command does not
        # declare makes the dispatcher raise, which reaches the user as a command
        # error and leaves the switch stuck on.
        appliance = _appliance()
        from custom_components.addhon.client.engine.commands import HonCommand

        appliance.commands["stopProgram"] = HonCommand(
            "stopProgram",
            {"parameters": {
                "onOffStatus": {"typology": "fixed", "category": "command",
                                "mandatory": 1, "fixedValue": "0"},
            }},
            appliance,
        )
        power = await self._power(appliance)
        await power.async_turn_off()
        sent = appliance.api.sent[-1]
        self.assertEqual("stopProgram", sent["command"])
        self.assertEqual({"onOffStatus": "0"}, sent["parameters"])

    async def test_its_diagnostics_tag_is_its_own_unique_id_suffix(self) -> None:
        # The diagnostics join key is `f"{domain}.{unique_id suffix}"`
        # (`diagnostics.py` builds it that way for every registry row), so a tag
        # that does not match the suffix never joins and the entity reports a null
        # source. Tying the two together here is what makes renaming one without
        # the other fail, instead of shipping a row that silently says nothing.
        from custom_components.addhon import diagnostics

        power = await self._power()
        suffix = power._attr_unique_id.split("_", 1)[1]
        _attrs, _params, sources, _unavailable = diagnostics._mapped_sets(APPLIANCE_HO)
        self.assertIn(f"switch.{suffix}", sources)

    async def test_neither_direction_ships_a_program_name(self) -> None:
        appliance = _appliance()
        power = await self._power(appliance)
        await power.async_turn_on()
        await power.async_turn_off()
        self.assertEqual(["", ""], [s["program_name"] for s in appliance.api.sent])

    async def test_it_reads_the_panel_flag_back(self) -> None:
        attributes = copy.deepcopy(HOOD_ATTRIBUTES)
        attributes["onOffStatus"] = 1
        self.assertIs(True, (await self._power(attributes=attributes)).is_on)
        attributes["onOffStatus"] = 0
        self.assertIs(False, (await self._power(attributes=attributes)).is_on)

    async def test_an_unreadable_flag_is_unknown_rather_than_off(self) -> None:
        # A hood that stopped reporting its own power flag has not told us it is
        # off, and an entity that claims "off" invites an automation to "fix" it.
        attributes = copy.deepcopy(HOOD_ATTRIBUTES)
        attributes.pop("onOffStatus")
        self.assertIsNone((await self._power(attributes=attributes)).is_on)

    async def test_it_is_gated_on_the_command_that_can_set_the_flag(self) -> None:
        # A hood whose startProgram does not declare `onOffStatus` cannot be woken
        # at all, and a switch that reads correctly and writes nothing is the one
        # failure a capability gate exists to prevent.
        start = {
            name: body
            for name, body in HOOD_START_PARAMS.items()
            if name != "onOffStatus"
        }
        data = _entry_data(appliance=_appliance(start_params=start))
        added = await _build("switch", data, client=RunningClient())
        self.assertEqual(
            ["ho-1_light", "ho-1_delay_timer"], [e._attr_unique_id for e in added]
        )

    async def test_it_is_gated_on_the_command_that_switches_the_hood_off(self) -> None:
        appliance = _appliance()
        del appliance.commands["stopProgram"]
        added = await _build(
            "switch", _entry_data(appliance=appliance), client=RunningClient()
        )
        self.assertEqual(
            ["ho-1_light", "ho-1_delay_timer"], [e._attr_unique_id for e in added]
        )

    async def test_a_missing_client_raises_the_localized_error(self) -> None:
        added = await _build("switch", _entry_data(), client=None)
        power = next(e for e in added if e._attr_unique_id == "ho-1_power")
        with self.assertRaises(HomeAssistantError) as caught:
            await power.async_turn_on()
        self.assertEqual(
            "appliance_or_client_unavailable", caught.exception.translation_key
        )

    async def test_a_transport_failure_becomes_a_localized_command_error(self) -> None:
        power = await self._power(client=RunningClient(fail=RuntimeError("boom")))
        with self.assertRaises(HomeAssistantError) as caught:
            await power.async_turn_off()
        self.assertEqual("command_error", caught.exception.translation_key)

    async def test_every_write_refreshes_the_state(self) -> None:
        power = await self._power()
        await power.async_turn_on()
        self.assertEqual(1, power.coordinator.refreshes)


class HoodNumberTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_delay_number_reads_its_bounds_from_the_device(self) -> None:
        added = await _build("number", _entry_data(), client=RunningClient())
        self.assertEqual(["ho-1_delay_time"], [e._attr_unique_id for e in added])
        number = added[0]
        self.assertEqual(1.0, number.native_min_value)
        self.assertEqual(99.0, number.native_max_value)
        self.assertEqual(1.0, number.native_step)

    async def test_the_delay_number_is_a_configuration_control(self) -> None:
        from homeassistant.const import EntityCategory

        added = await _build("number", _entry_data(), client=RunningClient())
        self.assertEqual(
            EntityCategory.CONFIG, added[0].entity_description.entity_category
        )

    async def test_the_delay_number_does_not_borrow_the_washer_label(self) -> None:
        # On a washer `delay_time` postpones the START; on a hood it postpones the
        # STOP. Same parameter name, opposite meaning.
        added = await _build("number", _entry_data(), client=RunningClient())
        self.assertEqual("delay_off_time", added[0]._attr_translation_key)

    async def test_the_delay_number_writes_the_settings_delay_time(self) -> None:
        appliance = _appliance()
        added = await _build(
            "number", _entry_data(appliance=appliance), client=RunningClient()
        )
        await added[0].async_set_native_value(45)
        sent = appliance.api.sent[-1]
        self.assertEqual("settings", sent["command"])
        self.assertEqual("45", sent["parameters"]["delayTime"])


class HoodReadingsTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_hood_sensor_set(self) -> None:
        added = await _build("sensor", _entry_data())
        self.assertEqual(
            ["ho-1_fan_speed", "ho-1_errors", "ho-1_last_work_time"],
            [e._attr_unique_id for e in added],
        )

    async def test_the_hood_binary_set(self) -> None:
        added = await _build("binary_sensor", _entry_data())
        self.assertEqual(
            [
                "ho-1_light",
                "ho-1_filter_clean_needed",
                "ho-1_filter_cleaning",
                "ho-1_running",
                "ho-1_connectivity",
            ],
            [e._attr_unique_id for e in added],
        )

    async def test_last_work_time_claims_no_unit_it_cannot_prove(self) -> None:
        # 11147 is either minutes of lifetime use or seconds of the last session,
        # and nothing in the app or the dump decides. A DURATION class would have
        # to pick one; a state_class would record statistics in the wrong unit.
        added = await _build("sensor", _entry_data())
        work = next(e for e in added if e._attr_unique_id == "ho-1_last_work_time")
        self.assertIsNone(work.entity_description.native_unit_of_measurement)
        self.assertIsNone(work.entity_description.device_class)
        self.assertIsNone(work.entity_description.state_class)
        self.assertEqual(11147.0, work.native_value)

    async def test_the_textual_filter_cleaning_flag_is_understood(self) -> None:
        # The hood spells this one "false"/"true", not 0/1: the platform's shared
        # comparison would read BOTH as off.
        for raw, expected in (("false", False), ("true", True), (0, False), (1, True)):
            attributes = copy.deepcopy(HOOD_ATTRIBUTES)
            attributes["filterCleaningStatus"] = raw
            added = await _build("binary_sensor", _entry_data(attributes))
            cleaning = next(
                e for e in added if e._attr_unique_id == "ho-1_filter_cleaning"
            )
            self.assertIs(expected, cleaning.is_on, raw)

    async def test_an_unknown_filter_cleaning_spelling_hides_the_entity(self) -> None:
        attributes = copy.deepcopy(HOOD_ATTRIBUTES)
        attributes["filterCleaningStatus"] = "maintenance"
        added = await _build("binary_sensor", _entry_data(attributes))
        cleaning = next(e for e in added if e._attr_unique_id == "ho-1_filter_cleaning")
        self.assertIsNone(cleaning.is_on)
        self.assertIs(False, cleaning.available)

    async def test_running_reads_the_devices_own_power_flag(self) -> None:
        attributes = copy.deepcopy(HOOD_ATTRIBUTES)
        attributes["onOffStatus"] = 1
        added = await _build("binary_sensor", _entry_data(attributes))
        running = next(e for e in added if e._attr_unique_id == "ho-1_running")
        self.assertIs(True, running.is_on)

    async def test_every_hood_reading_is_capability_gated(self) -> None:
        added = await _build("sensor", _entry_data({"available": True}))
        self.assertEqual([], added)
        binaries = await _build("binary_sensor", _entry_data({"available": True}))
        # Connectivity is universal and never gated: it must be able to say
        # "disconnected".
        self.assertEqual(["ho-1_connectivity"], [e._attr_unique_id for e in binaries])


class HoodDiagnosticsCoverageTest(unittest.TestCase):
    def test_the_hood_controls_are_no_longer_reported_unmapped(self) -> None:
        from custom_components.addhon import diagnostics

        mapped_attrs, mapped_params, sources, _unavailable = diagnostics._mapped_sets(
            APPLIANCE_HO
        )
        for name in (
            "windSpeed",
            "lightStatus",
            "onOffStatus",
            "delayTime",
            "delayTimeStatus",
        ):
            self.assertIn(name, mapped_attrs, name)
            self.assertIn(name, mapped_params, name)
        self.assertIn("fan.hood", sources)
        # `onOffStatus` rides along on every fan write because the schema marks it
        # mandatory, but the entity that CHOOSES it is the power switch, and that
        # is the row a reader chasing the parameter has to land on.
        self.assertEqual(["windSpeed"], sources["fan.hood"]["write"])
        self.assertEqual(["onOffStatus"], sources["switch.power"]["write"])
        self.assertEqual(["onOffStatus"], sources["switch.power"]["read"])

    def test_another_type_did_not_inherit_the_hood_parameters(self) -> None:
        # The block is type-gated; a missing gate would fold the hood's names into
        # every other appliance's coverage and quietly hide a real gap there.
        from custom_components.addhon import diagnostics

        mapped_attrs, mapped_params, sources, _ = diagnostics._mapped_sets("OV")
        self.assertNotIn("windSpeed", mapped_params)
        self.assertNotIn("delayTimeStatus", mapped_attrs)
        self.assertNotIn("fan.hood", sources)


if __name__ == "__main__":
    unittest.main()
