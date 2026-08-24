# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the air purifier (AP) Home Assistant entities.

Every AP entity is capability-gated: it exists only when the device actually
reports its source attribute (and, for the writable ones, declares the parameter
in its live command schema). Nothing here may depend on a model name.

Stdlib unittest with inline Home Assistant stubs, matching the convention of the
sibling sensor tests. The stubs are getattr-guarded so they coexist with the
other modules' stubs in a shared pytest process; conftest.py already supplies the
entity-stack stubs (CoordinatorEntity, const units, DeviceInfo).
"""
from __future__ import annotations

import copy
import dataclasses
import json
import re
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

    # conftest.py already installs the translatable HomeAssistantError; the
    # setup-flow subclasses the integration imports are seeded here.
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
        native_unit_of_measurement: str | None = None
        device_class: object | None = None
        state_class: object | None = None
        options: object | None = None

    class SensorEntity:
        pass

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
    sensor_mod.SensorEntity = getattr(sensor_mod, "SensorEntity", SensorEntity)
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
        device_class: object | None = None

    class BinarySensorEntity:
        pass

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
        binary_mod, "BinarySensorEntity", BinarySensorEntity
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

from custom_components.addhon.const import APPLIANCE_AP  # noqa: E402


# A purifier reporting every attribute the AP tables know about, powered on.
FULL_ATTRIBUTES = {
    "onOffStatus": "1",
    "machMode": "2",
    "temp": "22",
    "humidityIndoor": "45",
    "pm2p5ValueIndoor": "12",
    "pm10ValueIndoor": "18",
    "vocValueIndoor": "2",
    "airQuality": "0",
    "windSpeed": "3",
    "mainFilterStatus": "34",
    "preFilterStatus": "10",
    "totalWorkTime": "1234",
    "errors": "00",
    "coLevel": "1",
    "pollenLevel": "2",
    "ecoModeStatus": "1",
    # writable controls (light / lock / tone / aroma)
    "lightStatus": "0",
    "lockStatus": "0",
    "touchToneStatus": "1",
    "aromaStatus": "0",
    "aromaTimeOn": "60",
    "aromaTimeOff": "60",
}


class FakeCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.last_update_success = True


class FakeHass:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1", options: dict | None = None) -> None:
        self.entry_id = entry_id
        # A real ConfigEntry always exposes options; the experimental gate reads it.
        self.options = dict(options or {})


def _experimental(enabled: bool) -> dict:
    from custom_components.addhon.const import CONF_ENABLE_EXPERIMENTAL

    return {CONF_ENABLE_EXPERIMENTAL: enabled}


async def _build_sensors(attributes: dict, options: dict | None = None) -> list:
    from custom_components.addhon import sensor
    from custom_components.addhon.const import DOMAIN

    data = {
        "ap-1": {
            "type": APPLIANCE_AP,
            "name": "Purifier",
            "attributes": attributes,
            "settings": {},
        }
    }
    coordinator = FakeCoordinator(data)
    hass = FakeHass(
        {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}}
    )
    added: list = []
    await sensor.async_setup_entry(hass, FakeEntry(options=options), added.extend)
    return [e for e in added if not getattr(e, "_addhon_account", False)]


def _by_key(entities: list) -> dict:
    return {e.entity_description.key: e for e in entities}


class AirPurifierSensorTableTest(unittest.TestCase):
    # The standard readings, in table order. The experimental rows are listed
    # separately so a new one cannot be slipped into the standard set unnoticed.
    STANDARD = [
        "temp_indoor",
        "humidity_indoor",
        "pm25",
        "pm10",
        "voc",
        "air_quality",
        "fan_speed",
        "filter_life",
        "filter_cleaning",
        "total_work_time",
        "errors",
        "co",
        "pollen_level",
    ]
    EXPERIMENTAL = ["air_quality_label"]

    def test_table_covers_every_documented_measurement(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        self.assertEqual(
            [d.key for d in SENSORS[APPLIANCE_AP]],
            self.STANDARD + self.EXPERIMENTAL,
        )

    def test_the_standard_rows_are_exactly_the_non_experimental_ones(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        self.assertEqual(
            self.STANDARD,
            [d.key for d in SENSORS[APPLIANCE_AP] if not d.experimental],
        )

    def test_every_description_is_capability_gated(self) -> None:
        """AP is not a historic always-created type: a device that does not report
        an attribute must not get an "unknown" entity for it."""
        from custom_components.addhon.sensor import SENSORS

        for description in SENSORS[APPLIANCE_AP]:
            self.assertTrue(description.gated, description.key)

    def test_filters_are_percentages_and_not_batteries(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        filters = [
            d for d in SENSORS[APPLIANCE_AP]
            if d.key in {"filter_life", "filter_cleaning"}
        ]
        self.assertEqual(2, len(filters))
        for description in filters:
            self.assertEqual("%", description.native_unit_of_measurement)
            self.assertEqual("measurement", description.state_class)
            self.assertEqual("mdi:air-filter", description.icon)
            self.assertIsNone(description.device_class)

    def test_measured_quantities_use_verified_units(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        table = {d.key: d for d in SENSORS[APPLIANCE_AP]}
        self.assertEqual("°C", table["temp_indoor"].native_unit_of_measurement)
        self.assertEqual("temperature", table["temp_indoor"].device_class)
        self.assertEqual("%", table["humidity_indoor"].native_unit_of_measurement)
        self.assertEqual("humidity", table["humidity_indoor"].device_class)
        for key, device_class in (("pm25", "pm25"), ("pm10", "pm10")):
            self.assertEqual("µg/m³", table[key].native_unit_of_measurement)
            self.assertEqual(device_class, table[key].device_class)

    def test_ordinal_readings_invent_no_unit_or_class(self) -> None:
        """VOC, air quality, wind speed, CO and pollen are small level indexes in
        the official app, not concentrations or a 0-500 AQI. Attaching a unit or a
        device class would assert a measurement the device never reports."""
        from custom_components.addhon.sensor import SENSORS

        table = {d.key: d for d in SENSORS[APPLIANCE_AP]}
        for key in ("voc", "air_quality", "fan_speed", "co", "pollen_level"):
            self.assertIsNone(table[key].native_unit_of_measurement, key)
            self.assertIsNone(table[key].device_class, key)
            self.assertIsNone(table[key].state_class, key)

    def test_total_work_time_is_a_monotonic_minute_counter(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        table = {d.key: d for d in SENSORS[APPLIANCE_AP]}
        description = table["total_work_time"]
        self.assertEqual("min", description.native_unit_of_measurement)
        self.assertEqual("duration", description.device_class)
        self.assertEqual("total_increasing", description.state_class)


class AirPurifierSensorGatingTest(unittest.IsolatedAsyncioTestCase):
    async def test_full_device_creates_every_sensor(self) -> None:
        entities = await _build_sensors(FULL_ATTRIBUTES)
        self.assertEqual(13, len(entities))

    async def test_only_reported_attributes_create_sensors(self) -> None:
        entities = await _build_sensors(
            {"onOffStatus": "1", "temp": "20", "mainFilterStatus": "5"}
        )
        self.assertEqual(
            {"temp_indoor", "filter_life"}, set(_by_key(entities))
        )

    async def test_no_attributes_means_no_sensors(self) -> None:
        self.assertEqual([], await _build_sensors({}))


class AirPurifierSensorValueTest(unittest.IsolatedAsyncioTestCase):
    async def test_filters_report_remaining_life_from_consumption(self) -> None:
        entities = _by_key(await _build_sensors(FULL_ATTRIBUTES))
        self.assertEqual(66, entities["filter_life"].native_value)
        self.assertEqual(90, entities["filter_cleaning"].native_value)

    async def test_filter_remaining_is_clamped(self) -> None:
        entities = _by_key(
            await _build_sensors(
                {
                    **FULL_ATTRIBUTES,
                    "mainFilterStatus": "150",
                    "preFilterStatus": "-20",
                }
            )
        )
        self.assertEqual(0, entities["filter_life"].native_value)
        self.assertEqual(100, entities["filter_cleaning"].native_value)

    async def test_normal_error_spellings_all_read_as_zero(self) -> None:
        for raw in (0, "0", "00", "100"):
            entities = _by_key(
                await _build_sensors({**FULL_ATTRIBUTES, "errors": raw})
            )
            self.assertEqual("0", entities["errors"].native_value, raw)

    async def test_a_real_error_code_is_passed_through(self) -> None:
        entities = _by_key(
            await _build_sensors({**FULL_ATTRIBUTES, "errors": "E12"})
        )
        self.assertEqual("E12", entities["errors"].native_value)

    async def test_measurements_render_their_raw_numbers(self) -> None:
        entities = _by_key(await _build_sensors(FULL_ATTRIBUTES))
        self.assertEqual(22.0, entities["temp_indoor"].native_value)
        self.assertEqual(45.0, entities["humidity_indoor"].native_value)
        self.assertEqual(12.0, entities["pm25"].native_value)
        self.assertEqual(1234.0, entities["total_work_time"].native_value)


class AirPurifierAvailabilityTest(unittest.IsolatedAsyncioTestCase):
    """At power-off the device publishes zero sentinels for temperature,
    humidity and the particulate values, and retains stale VOC / air-quality /
    wind readings. Those entities have to hide rather than present a sentinel as
    a measurement; the filter, time, error and raw diagnostic entities stay."""

    POWERED_OFF = {**FULL_ATTRIBUTES, "onOffStatus": "0"}

    async def test_environmental_sensors_hide_while_off(self) -> None:
        entities = _by_key(await _build_sensors(self.POWERED_OFF))
        for key in (
            "temp_indoor", "humidity_indoor", "pm25", "pm10",
            "voc", "air_quality", "fan_speed",
        ):
            self.assertFalse(entities[key].available, key)

    async def test_diagnostic_sensors_stay_available_while_off(self) -> None:
        entities = _by_key(await _build_sensors(self.POWERED_OFF))
        for key in (
            "filter_life", "filter_cleaning", "total_work_time",
            "errors", "co", "pollen_level",
        ):
            self.assertTrue(entities[key].available, key)

    async def test_everything_is_available_while_running(self) -> None:
        entities = _by_key(await _build_sensors(FULL_ATTRIBUTES))
        for key, entity in entities.items():
            self.assertTrue(entity.available, key)

    async def test_an_unreported_power_state_hides_the_measurements(self) -> None:
        """An absent power state is not a confirmation that the readings are
        live, so the environmental entities stay hidden."""
        attributes = {k: v for k, v in FULL_ATTRIBUTES.items() if k != "onOffStatus"}
        entities = _by_key(await _build_sensors(attributes))
        self.assertFalse(entities["temp_indoor"].available)
        self.assertTrue(entities["filter_life"].available)

    async def test_availability_follows_the_coordinator(self) -> None:
        """The power gate is additional to, never a replacement for, the base
        availability rules."""
        from custom_components.addhon import sensor
        from custom_components.addhon.const import DOMAIN

        data = {
            "ap-1": {
                "type": APPLIANCE_AP,
                "name": "Purifier",
                "attributes": FULL_ATTRIBUTES,
                "settings": {},
            }
        }
        coordinator = FakeCoordinator(data)
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}}
        )
        added: list = []
        await sensor.async_setup_entry(hass, FakeEntry(), added.extend)
        entities = _by_key(
            [e for e in added if not getattr(e, "_addhon_account", False)]
        )

        coordinator.last_update_success = False
        self.assertFalse(entities["temp_indoor"].available)
        self.assertFalse(entities["filter_life"].available)


class AirPurifierDescriptionFlagTest(unittest.TestCase):
    def test_power_requirement_is_declared_per_description(self) -> None:
        """The availability rule is data on the description, not a key list
        hardcoded inside `available`."""
        from custom_components.addhon.sensor import SENSORS

        requires_power = {
            d.key for d in SENSORS[APPLIANCE_AP] if d.requires_power
        }
        self.assertEqual(
            {
                "temp_indoor", "humidity_indoor", "pm25", "pm10",
                "voc", "air_quality", "fan_speed",
                # The interpreted label reads the same live telemetry as the raw
                # air-quality sensor, so it hides at power-off for the same reason.
                "air_quality_label",
            },
            requires_power,
        )

    def test_no_other_appliance_type_requires_power(self) -> None:
        """The new flag defaults off, so no existing entity changes behavior."""
        from custom_components.addhon.sensor import SENSORS

        for app_type, descriptions in SENSORS.items():
            if app_type == APPLIANCE_AP:
                continue
            for description in descriptions:
                self.assertFalse(description.requires_power, description.key)


# --- Task 4: standard binary sensors -----------------------------------------


async def _build_binary(attributes: dict, options: dict | None = None) -> list:
    from custom_components.addhon import binary_sensor
    from custom_components.addhon.const import DOMAIN

    data = {
        "ap-1": {
            "type": APPLIANCE_AP,
            "name": "Purifier",
            "attributes": attributes,
            "settings": {},
        }
    }
    coordinator = FakeCoordinator(data)
    hass = FakeHass(
        {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}}
    )
    added: list = []
    await binary_sensor.async_setup_entry(
        hass, FakeEntry(options=options), added.extend
    )
    return [e for e in added if not getattr(e, "_addhon_account", False)]


class AirPurifierBinaryTableTest(unittest.TestCase):
    def test_table_is_the_two_standard_signals_plus_one_experimental(self) -> None:
        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        self.assertEqual(
            [d.key for d in BINARY_SENSORS[APPLIANCE_AP]],
            ["eco_active", "problem", "co_alarm"],
        )

    def test_the_standard_signals_are_exactly_the_non_experimental_ones(self) -> None:
        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        self.assertEqual(
            ["eco_active", "problem"],
            [d.key for d in BINARY_SENSORS[APPLIANCE_AP] if not d.experimental],
        )

    def test_problem_is_a_problem_class_derived_from_the_error_code(self) -> None:
        """The problem state comes from has_problem(), so every "no error"
        spelling the device uses is handled, not one literal code."""
        from custom_components.addhon.air_purifier import has_problem
        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        table = {d.key: d for d in BINARY_SENSORS[APPLIANCE_AP]}
        problem = table["problem"]
        self.assertEqual("problem", problem.device_class)
        self.assertEqual("errors", problem.attr_key)
        self.assertIs(has_problem, problem.value_fn)

    def test_eco_reads_its_own_attribute_through_the_ap_toggle_rule(self) -> None:
        """It used to rely on the platform's generic `on_value` comparison, which is
        shared with every other appliance family and stringifies the live value
        itself. `is_engaged` keeps the purifier's own signal on the AP rule (see
        ShadowSpellingTest)."""
        from custom_components.addhon.air_purifier import is_engaged
        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        table = {d.key: d for d in BINARY_SENSORS[APPLIANCE_AP]}
        eco = table["eco_active"]
        self.assertEqual("ecoModeStatus", eco.attr_key)
        self.assertIs(is_engaged, eco.value_fn)

    def test_the_value_fn_field_is_declared_and_not_inherited(self) -> None:
        """Every binary sensor that derives its state names itself here.

        The field is optional and defaults off, so a description that grows one by
        accident -- copied from a sibling row, say -- silently stops honouring the
        platform's shared `on_value` rule. Pinned as the exact set rather than as
        "nothing outside the AP has one" so that adding a derived reading stays a
        deliberate edit while the guard keeps covering every other row.
        """
        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        declared = {
            (app_type, description.key)
            for app_type, descriptions in BINARY_SENSORS.items()
            for description in descriptions
            if description.value_fn is not None
        }
        self.assertEqual(
            {
                (APPLIANCE_AP, "problem"),
                (APPLIANCE_AP, "eco_active"),
                (APPLIANCE_AP, "co_alarm"),
                # The hood spells this one as the text "false", not as 0/1, so the
                # shared comparison would read every value as off.
                ("HO", "filter_cleaning"),
            }
            # The hob's per-zone fault reads through `has_problem` because "no
            # error" has three spellings on that device; the flex-bridge flag has
            # two distinct "on" values.
            | _hob_keys("error_zone", "combi_mode_zone"),
            declared,
        )


class AirPurifierBinaryGatingTest(unittest.IsolatedAsyncioTestCase):
    async def test_full_device_creates_both_plus_connectivity(self) -> None:
        entities = _by_key(await _build_binary(FULL_ATTRIBUTES))
        self.assertEqual({"eco_active", "problem", "connectivity"}, set(entities))

    async def test_each_signal_is_gated_on_its_own_attribute(self) -> None:
        only_eco = _by_key(await _build_binary({"ecoModeStatus": "0"}))
        self.assertIn("eco_active", only_eco)
        self.assertNotIn("problem", only_eco)

        only_problem = _by_key(await _build_binary({"errors": "0"}))
        self.assertIn("problem", only_problem)
        self.assertNotIn("eco_active", only_problem)

    async def test_a_bare_device_gets_connectivity_only(self) -> None:
        entities = _by_key(await _build_binary({}))
        self.assertEqual({"connectivity"}, set(entities))


class AirPurifierBinaryStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_eco_is_on_only_for_raw_one(self) -> None:
        for raw, expected in (("1", True), ("0", False), ("2", False), (1, True)):
            entities = _by_key(
                await _build_binary({**FULL_ATTRIBUTES, "ecoModeStatus": raw})
            )
            self.assertIs(expected, entities["eco_active"].is_on, raw)

    async def test_problem_is_off_for_every_normal_error_spelling(self) -> None:
        for raw in (0, "0", "00", "100", 100):
            entities = _by_key(
                await _build_binary({**FULL_ATTRIBUTES, "errors": raw})
            )
            self.assertIs(False, entities["problem"].is_on, raw)

    async def test_problem_is_on_for_a_real_error(self) -> None:
        for raw in ("E12", "1000", 7):
            entities = _by_key(
                await _build_binary({**FULL_ATTRIBUTES, "errors": raw})
            )
            self.assertIs(True, entities["problem"].is_on, raw)

    async def test_an_unreported_reading_stays_unknown(self) -> None:
        """An empty attribute is missing data, not evidence of a healthy device,
        so the entity reports unknown rather than "no problem"."""
        entities = _by_key(
            await _build_binary(
                {**FULL_ATTRIBUTES, "errors": "", "ecoModeStatus": ""}
            )
        )
        self.assertIsNone(entities["problem"].is_on)
        self.assertIsNone(entities["eco_active"].is_on)


class AirPurifierBinaryAvailabilityTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_signals_stay_available_while_off(self) -> None:
        """Neither is live telemetry: the eco setting and the error code are
        reported and meaningful with the purifier stopped."""
        entities = _by_key(
            await _build_binary({**FULL_ATTRIBUTES, "onOffStatus": "0"})
        )
        self.assertTrue(entities["eco_active"].available)
        self.assertTrue(entities["problem"].available)


# --- Task 5: the fan platform -------------------------------------------------

AP_SCHEMA = {
    "startProgram": {
        "parameters": {
            "machMode": {
                "typology": "enum", "category": "command", "mandatory": 0,
                "defaultValue": "2", "enumValues": ["1", "2", "4"],
            },
            "onOffStatus": {
                "typology": "fixed", "category": "command", "mandatory": 1,
                "fixedValue": "1",
            },
        }
    },
    "stopProgram": {
        "parameters": {
            "onOffStatus": {
                "typology": "fixed", "category": "command", "mandatory": 1,
                "fixedValue": "0",
            },
        }
    },
    "settings": {
        "parameters": {
            "machMode": {
                "typology": "enum", "category": "command", "mandatory": 0,
                "defaultValue": "2", "enumValues": ["1", "2", "4"],
            },
            "lightStatus": {
                "typology": "enum", "category": "command", "mandatory": 0,
                "defaultValue": "0", "enumValues": ["0", "1", "2"],
            },
        }
    },
}


class _ApAppliance:
    def __init__(self) -> None:
        self.zone = 0
        self.options: dict[str, str] = {}
        self.commands: dict[str, object] = {}
        self.unique_id = "ap-1"


def _appliance(schema: dict | None = None):
    from custom_components.addhon.client.engine.commands import HonCommand

    appliance = _ApAppliance()
    for name, body in copy.deepcopy(schema or AP_SCHEMA).items():
        appliance.commands[name] = HonCommand(name, body, appliance)
    return appliance


class RecordingClient:
    """Captures the patches the entity dispatches, without touching a device."""

    def __init__(self, fail: Exception | None = None, answer: object = True) -> None:
        self.patches: list = []
        self.fail = fail
        # What dispatch_patch_sync reports back, and it defaults to what PRODUCTION
        # reports: the dispatcher's own bool, propagated verbatim by
        # _run_on_hon_loop. This fake used to answer None, and that alone is what once
        # forced the adapter's acceptance check to be looser than the dispatcher's own
        # rule. False is the dispatcher saying it rolled the transaction back.
        self.answer = answer

    def dispatch_patch_sync(self, appliance, patch) -> object:
        if self.fail is not None:
            raise self.fail
        self.patches.append(patch)
        return self.answer


class RecordingHass:
    def __init__(self, data: dict) -> None:
        self.data = data

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class RefreshingCoordinator(FakeCoordinator):
    def __init__(self, data: dict) -> None:
        super().__init__(data)
        self.refreshes = 0

    async def async_refresh(self) -> None:
        self.refreshes += 1


async def _build_fan(
    attributes: dict | None = None,
    schema: dict | None = None,
    client: RecordingClient | None = None,
):
    from custom_components.addhon import fan
    from custom_components.addhon.const import DOMAIN

    data = {
        "ap-1": {
            "type": APPLIANCE_AP,
            "name": "Purifier",
            "attributes": FULL_ATTRIBUTES if attributes is None else attributes,
            "appliance": _appliance(schema),
            "settings": {},
        }
    }
    coordinator = RefreshingCoordinator(data)
    recording = client if client is not None else RecordingClient()
    hass = RecordingHass(
        {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": recording}}}
    )
    added: list = []
    await fan.async_setup_entry(hass, FakeEntry(), added.extend)
    for entity in added:
        entity.hass = hass
    return added, recording, coordinator


def _sent(client: RecordingClient) -> list[tuple[str, dict]]:
    return [(p.command_name, dict(p.values)) for p in client.patches]


class AirPurifierFanSetupTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_capable_purifier_gets_one_fan(self) -> None:
        entities, _client, _coord = await _build_fan()
        self.assertEqual(1, len(entities))
        self.assertEqual("ap-1_purifier", entities[0].unique_id)

    async def test_presets_come_from_the_live_enum_intersection(self) -> None:
        entities, _client, _coord = await _build_fan()
        self.assertEqual(["sleep", "auto", "max"], entities[0].preset_modes)

    async def test_presets_shrink_with_the_live_schema(self) -> None:
        schema = copy.deepcopy(AP_SCHEMA)
        for command in ("startProgram", "settings"):
            schema[command]["parameters"]["machMode"]["enumValues"] = ["2", "4"]
            schema[command]["parameters"]["machMode"]["defaultValue"] = "2"
        entities, _client, _coord = await _build_fan(schema=schema)
        self.assertEqual(["auto", "max"], entities[0].preset_modes)

    async def test_no_fan_without_the_stop_command(self) -> None:
        schema = copy.deepcopy(AP_SCHEMA)
        del schema["stopProgram"]
        entities, _client, _coord = await _build_fan(schema=schema)
        self.assertEqual([], entities)

    async def test_no_fan_without_the_mode_state(self) -> None:
        attributes = {k: v for k, v in FULL_ATTRIBUTES.items() if k != "machMode"}
        entities, _client, _coord = await _build_fan(attributes=attributes)
        self.assertEqual([], entities)

    async def test_a_non_purifier_gets_no_fan(self) -> None:
        from custom_components.addhon import fan
        from custom_components.addhon.const import DOMAIN

        data = {
            "ac-1": {
                "type": "AC",
                "name": "Split",
                "attributes": FULL_ATTRIBUTES,
                "appliance": _appliance(),
                "settings": {},
            }
        }
        coordinator = RefreshingCoordinator(data)
        hass = RecordingHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}}
        )
        added: list = []
        await fan.async_setup_entry(hass, FakeEntry(), added.extend)
        self.assertEqual([], added)

    async def test_the_feature_flags_declare_preset_and_both_switches(self) -> None:
        from homeassistant.components.fan import FanEntityFeature

        entities, _client, _coord = await _build_fan()
        features = entities[0].supported_features
        for flag in (
            FanEntityFeature.PRESET_MODE,
            FanEntityFeature.TURN_ON,
            FanEntityFeature.TURN_OFF,
        ):
            self.assertTrue(features & flag, flag)


class AirPurifierFanStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_running_state_and_preset(self) -> None:
        entities, _client, _coord = await _build_fan()
        self.assertIs(True, entities[0].is_on)
        self.assertEqual("auto", entities[0].preset_mode)

    async def test_a_stopped_purifier_has_no_active_preset(self) -> None:
        entities, _client, _coord = await _build_fan(
            {**FULL_ATTRIBUTES, "onOffStatus": "0", "machMode": "0"}
        )
        self.assertIs(False, entities[0].is_on)
        self.assertIsNone(entities[0].preset_mode)

    async def test_the_off_sentinel_never_becomes_a_preset(self) -> None:
        """machMode=0 is a read-only off marker; even reported alongside a powered
        state it must not be presented as a selectable mode."""
        entities, _client, _coord = await _build_fan(
            {**FULL_ATTRIBUTES, "onOffStatus": "1", "machMode": "0"}
        )
        self.assertIsNone(entities[0].preset_mode)

    async def test_a_stopped_purifier_reports_no_preset_even_with_a_live_mode(
        self,
    ) -> None:
        """The device may keep the last active machMode in its shadow after
        stopping. The fan must still report no preset: a preset shown on a
        stopped fan claims the purifier is running that mode."""
        entities, _client, _coord = await _build_fan(
            {**FULL_ATTRIBUTES, "onOffStatus": "0", "machMode": "2"}
        )
        self.assertIs(False, entities[0].is_on)
        self.assertIsNone(entities[0].preset_mode)

    async def test_an_undeclared_mode_reads_as_no_preset(self) -> None:
        entities, _client, _coord = await _build_fan(
            {**FULL_ATTRIBUTES, "machMode": "3"}
        )
        self.assertIsNone(entities[0].preset_mode)

    async def test_an_unreported_power_state_creates_no_fan(self) -> None:
        """Without a power reading the fan cannot render its own state, so it is
        not created at all rather than shipped permanently unknown."""
        entities, _client, _coord = await _build_fan(
            {**FULL_ATTRIBUTES, "onOffStatus": ""}
        )
        self.assertEqual([], entities)


class AirPurifierFanWriteTest(unittest.IsolatedAsyncioTestCase):
    async def test_turn_off_stops_the_program_with_no_values(self) -> None:
        entities, client, coordinator = await _build_fan()
        await entities[0].async_turn_off()
        self.assertEqual([("stopProgram", {})], _sent(client))
        self.assertEqual(1, coordinator.refreshes)

    async def test_turn_on_defaults_to_auto_after_a_reload(self) -> None:
        """The last-mode store is volatile, so a fresh reload has nothing to
        replay and must pick a deterministic mode rather than an arbitrary one."""
        entities, client, _coord = await _build_fan(
            {**FULL_ATTRIBUTES, "onOffStatus": "0", "machMode": "0"}
        )
        await entities[0].async_turn_on()
        self.assertEqual([("startProgram", {"machMode": "2"})], _sent(client))

    async def test_turn_on_replays_the_last_active_mode(self) -> None:
        entities, client, _coord = await _build_fan(
            {**FULL_ATTRIBUTES, "onOffStatus": "1", "machMode": "4"}
        )
        entities[0]._handle_coordinator_update()
        await entities[0].async_turn_on()
        self.assertEqual([("startProgram", {"machMode": "4"})], _sent(client))

    async def test_the_off_sentinel_never_replaces_the_last_mode(self) -> None:
        entities, client, _coord = await _build_fan(
            {**FULL_ATTRIBUTES, "onOffStatus": "1", "machMode": "1"}
        )
        entity = entities[0]
        entity._handle_coordinator_update()
        # the purifier stops: machMode falls back to the sentinel
        _coord.data["ap-1"]["attributes"] = {
            **FULL_ATTRIBUTES, "onOffStatus": "0", "machMode": "0",
        }
        entity._handle_coordinator_update()
        await entity.async_turn_on()
        self.assertEqual([("startProgram", {"machMode": "1"})], _sent(client))

    async def test_an_undeclared_mode_never_replaces_the_last_mode(self) -> None:
        entities, client, _coord = await _build_fan(
            {**FULL_ATTRIBUTES, "onOffStatus": "1", "machMode": "3"}
        )
        entities[0]._handle_coordinator_update()
        await entities[0].async_turn_on()
        self.assertEqual([("startProgram", {"machMode": "2"})], _sent(client))

    async def test_setting_a_preset_while_running_uses_settings(self) -> None:
        entities, client, coordinator = await _build_fan()
        await entities[0].async_set_preset_mode("sleep")
        self.assertEqual([("settings", {"machMode": "1"})], _sent(client))
        self.assertEqual(1, coordinator.refreshes)

    async def test_setting_a_preset_while_stopped_starts_the_purifier(self) -> None:
        entities, client, _coord = await _build_fan(
            {**FULL_ATTRIBUTES, "onOffStatus": "0", "machMode": "0"}
        )
        await entities[0].async_set_preset_mode("max")
        self.assertEqual([("startProgram", {"machMode": "4"})], _sent(client))

    async def test_turn_on_with_an_explicit_preset(self) -> None:
        entities, client, _coord = await _build_fan(
            {**FULL_ATTRIBUTES, "onOffStatus": "0", "machMode": "0"}
        )
        await entities[0].async_turn_on(preset_mode="sleep")
        self.assertEqual([("startProgram", {"machMode": "1"})], _sent(client))

    async def test_a_selected_preset_becomes_the_replayed_mode(self) -> None:
        entities, client, _coord = await _build_fan()
        await entities[0].async_set_preset_mode("max")
        await entities[0].async_turn_off()
        await entities[0].async_turn_on()
        self.assertEqual(
            [
                ("settings", {"machMode": "4"}),
                ("stopProgram", {}),
                ("startProgram", {"machMode": "4"}),
            ],
            _sent(client),
        )

    async def test_no_write_ever_carries_an_unrelated_field(self) -> None:
        """Sparse by construction: a mode change must not restate the light or any
        other setting the user did not touch."""
        entities, client, _coord = await _build_fan()
        await entities[0].async_set_preset_mode("sleep")
        await entities[0].async_turn_off()
        await entities[0].async_turn_on()
        for _command, values in _sent(client):
            self.assertLessEqual(set(values), {"machMode"})


class AirPurifierFanErrorTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_percentage_is_refused_rather_than_dropped(self) -> None:
        """The purifier has no speed axis, so SET_SPEED is not declared and a
        percentage cannot be honoured. Dropping it silently would start the device
        in the REMEMBERED mode and return success, leaving the automation to read as
        though the requested speed had been applied."""
        from homeassistant.exceptions import HomeAssistantError

        entities, client, coordinator = await _build_fan(
            {**FULL_ATTRIBUTES, "onOffStatus": "0", "machMode": "0"}
        )
        with self.assertRaises(HomeAssistantError) as caught:
            await entities[0].async_turn_on(percentage=50)
        self.assertEqual("speed_not_supported", caught.exception.translation_key)
        self.assertEqual([], _sent(client))
        self.assertEqual(0, coordinator.refreshes)

    async def test_the_declared_features_carry_no_speed(self) -> None:
        """The refusal above is only coherent while no speed is advertised: an entity
        declaring SET_SPEED and then refusing every percentage would be worse than
        either choice alone."""
        from homeassistant.components.fan import FanEntityFeature

        entities, _client, _coord = await _build_fan()
        self.assertFalse(entities[0].supported_features & FanEntityFeature.SET_SPEED)

    async def test_an_unknown_preset_is_a_localized_error(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        entities, client, _coord = await _build_fan()
        with self.assertRaises(HomeAssistantError) as caught:
            await entities[0].async_set_preset_mode("turbo")
        self.assertEqual("command_error", caught.exception.translation_key)
        self.assertEqual([], _sent(client))

    async def test_a_transport_failure_is_a_localized_error(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        entities, _client, coordinator = await _build_fan(
            client=RecordingClient(fail=RuntimeError("cloud down"))
        )
        with self.assertRaises(HomeAssistantError) as caught:
            await entities[0].async_turn_off()
        self.assertEqual("command_error", caught.exception.translation_key)
        self.assertEqual(
            {"error": "cloud down"}, caught.exception.translation_placeholders
        )
        self.assertEqual(0, coordinator.refreshes)

    async def test_a_missing_client_is_reported_before_any_write(self) -> None:
        from custom_components.addhon import fan
        from custom_components.addhon.const import DOMAIN
        from homeassistant.exceptions import HomeAssistantError

        data = {
            "ap-1": {
                "type": APPLIANCE_AP,
                "name": "Purifier",
                "attributes": FULL_ATTRIBUTES,
                "appliance": _appliance(),
                "settings": {},
            }
        }
        coordinator = RefreshingCoordinator(data)
        hass = RecordingHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}}
        )
        added: list = []
        await fan.async_setup_entry(hass, FakeEntry(), added.extend)
        added[0].hass = hass

        with self.assertRaises(HomeAssistantError) as caught:
            await added[0].async_turn_off()
        self.assertEqual(
            "appliance_or_client_unavailable", caught.exception.translation_key
        )


class AirPurifierFanArchitectureTest(unittest.TestCase):
    def test_the_fan_never_uses_the_legacy_sender(self) -> None:
        """Every AP write is transactional: `async_send_command` applies values to
        a whole command and sends it, which is what the dispatcher replaces.

        Scoped to the purifier CLASS, not to fan.py, because the platform also
        hosts the cooker hood, which writes `settings.windSpeed` through the legacy
        sender on purpose (see hood.py). A whole-file grep would have to be deleted
        the day a second fan arrived; reading the class body keeps the purifier's
        invariant alive with a second entity in the same module.
        """
        import ast

        path = (
            Path(__file__).parents[1]
            / "custom_components" / "addhon" / "fan.py"
        )
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        purifier = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "HonAirPurifierFan"
        )
        body = ast.get_source_segment(source, purifier) or ""
        self.assertTrue(body, "the purifier class body could not be read")

        self.assertNotIn("async_send_command", body)
        self.assertNotIn("async_send_settings", body)
        self.assertNotIn("run_command_sync", body)
        self.assertIn("async_dispatch_patch", body)

    def test_fan_is_a_declared_platform(self) -> None:
        from custom_components.addhon.const import PLATFORMS

        self.assertIn("fan", PLATFORMS)


# --- Task 6: the three-position panel light -----------------------------------


async def _build_panel_light(
    attributes: dict | None = None,
    schema: dict | None = None,
    client: RecordingClient | None = None,
):
    from custom_components.addhon import select
    from custom_components.addhon.const import DOMAIN

    data = {
        "ap-1": {
            "type": APPLIANCE_AP,
            "name": "Purifier",
            "attributes": FULL_ATTRIBUTES if attributes is None else attributes,
            "appliance": _appliance(schema),
            "settings": {},
        }
    }
    coordinator = RefreshingCoordinator(data)
    recording = client if client is not None else RecordingClient()
    hass = RecordingHass(
        {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": recording}}}
    )
    added: list = []
    await select.async_setup_entry(hass, FakeEntry(), added.extend)
    lights = [e for e in added if (e.unique_id or "").endswith("_panel_light")]
    for entity in lights:
        entity.hass = hass
    return lights, recording, coordinator


class AirPurifierPanelLightSetupTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_capable_purifier_gets_one_panel_light(self) -> None:
        entities, _client, _coord = await _build_panel_light()
        self.assertEqual(1, len(entities))
        self.assertEqual("ap-1_panel_light", entities[0].unique_id)

    async def test_only_the_exact_three_level_schema_creates_it(self) -> None:
        """A two- or four-value schema is different device behavior; reusing the
        observed three-level mapping against it would send an undeclared value."""
        for values in (["0", "1"], ["0", "1", "2", "3"], ["1", "2"]):
            schema = copy.deepcopy(AP_SCHEMA)
            schema["settings"]["parameters"]["lightStatus"]["enumValues"] = values
            schema["settings"]["parameters"]["lightStatus"]["defaultValue"] = values[0]
            entities, _client, _coord = await _build_panel_light(schema=schema)
            self.assertEqual([], entities, values)

    async def test_nothing_without_the_parameter(self) -> None:
        schema = copy.deepcopy(AP_SCHEMA)
        del schema["settings"]["parameters"]["lightStatus"]
        entities, _client, _coord = await _build_panel_light(schema=schema)
        self.assertEqual([], entities)

    async def test_nothing_without_the_state(self) -> None:
        attributes = {k: v for k, v in FULL_ATTRIBUTES.items() if k != "lightStatus"}
        entities, _client, _coord = await _build_panel_light(attributes=attributes)
        self.assertEqual([], entities)

    async def test_the_positions_read_dimmest_first(self) -> None:
        """The display order is independent of the raw encoding: whichever way the
        encoding is settled, the user sees off, low, high."""
        entities, _client, _coord = await _build_panel_light()
        self.assertEqual(["off", "low", "high"], entities[0].options)


class AirPurifierPanelLightStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_raw_level_maps_to_its_position(self) -> None:
        for raw, option in (("0", "off"), ("1", "low"), ("2", "high")):
            entities, _client, _coord = await _build_panel_light(
                {**FULL_ATTRIBUTES, "lightStatus": raw}
            )
            self.assertEqual(option, entities[0].current_option, raw)

    async def test_an_unreported_level_creates_nothing(self) -> None:
        entities, _client, _coord = await _build_panel_light(
            {**FULL_ATTRIBUTES, "lightStatus": ""}
        )
        self.assertEqual([], entities)

    async def test_an_undeclared_raw_level_reads_as_unknown(self) -> None:
        """Folding it onto a neighbouring position would show a level the panel is
        not at."""
        entities, _client, _coord = await _build_panel_light(
            {**FULL_ATTRIBUTES, "lightStatus": "7"}
        )
        self.assertIsNone(entities[0].current_option)

    async def test_a_state_update_never_resizes_the_offered_positions(self) -> None:
        """The option list comes from the schema at construction. A shadow value
        outside it must not add or drop a position."""
        entities, _client, coordinator = await _build_panel_light()
        entity = entities[0]
        coordinator.data["ap-1"]["attributes"] = {
            **FULL_ATTRIBUTES, "lightStatus": "7",
        }
        entity._handle_coordinator_update()
        self.assertEqual(["off", "low", "high"], entity.options)

    async def test_it_stays_readable_with_the_purifier_stopped(self) -> None:
        """The panel level is a setting the device keeps while stopped, not live
        telemetry, so it is not gated on power like the environment sensors."""
        entities, _client, _coord = await _build_panel_light(
            {**FULL_ATTRIBUTES, "onOffStatus": "0"}
        )
        self.assertTrue(entities[0].available)


class AirPurifierPanelLightWriteTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_position_sends_its_raw_level(self) -> None:
        for option, raw in (("off", "0"), ("low", "1"), ("high", "2")):
            entities, client, coordinator = await _build_panel_light()
            await entities[0].async_select_option(option)
            self.assertEqual(
                [("settings", {"lightStatus": raw})], _sent(client), option
            )
            self.assertEqual(1, coordinator.refreshes, option)

    async def test_no_write_ever_carries_an_unrelated_field(self) -> None:
        entities, client, _coord = await _build_panel_light()
        entity = entities[0]
        for option in ("high", "low", "off"):
            await entity.async_select_option(option)
        for _command, values in _sent(client):
            self.assertEqual({"lightStatus"}, set(values))

    async def test_selecting_a_position_never_touches_the_power_state(self) -> None:
        entities, client, _coord = await _build_panel_light(
            {**FULL_ATTRIBUTES, "onOffStatus": "0"}
        )
        await entities[0].async_select_option("high")
        self.assertEqual([("settings", {"lightStatus": "2"})], _sent(client))


class AirPurifierPanelLightErrorTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_unknown_position_is_refused_before_any_send(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        entities, client, coordinator = await _build_panel_light()
        with self.assertRaises(HomeAssistantError) as caught:
            await entities[0].async_select_option("medium")
        self.assertEqual("invalid_setpoint", caught.exception.translation_key)
        self.assertEqual([], _sent(client))
        self.assertEqual(0, coordinator.refreshes)

    async def test_a_transport_failure_is_a_localized_error(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        entities, _client, coordinator = await _build_panel_light(
            client=RecordingClient(fail=RuntimeError("cloud down"))
        )
        with self.assertRaises(HomeAssistantError) as caught:
            await entities[0].async_select_option("off")
        self.assertEqual("command_error", caught.exception.translation_key)
        self.assertEqual(0, coordinator.refreshes)


class AirPurifierPanelLightArchitectureTest(unittest.TestCase):
    def test_the_panel_light_never_uses_the_legacy_sender(self) -> None:
        import inspect

        from custom_components.addhon import select

        source = inspect.getsource(select.HonAirPurifierPanelLightSelect)
        self.assertNotIn("async_send_command", source)
        self.assertNotIn("async_send_settings", source)
        self.assertIn("async_dispatch_patch", source)

    def test_the_light_platform_is_gone(self) -> None:
        """The panel has three steps and no brightness axis, so it is a select.
        The platform stays out of PLATFORMS or Home Assistant would set up a
        platform that creates nothing."""
        from custom_components.addhon.const import PLATFORMS

        self.assertNotIn("light", PLATFORMS)
        self.assertFalse(
            (
                Path(__file__).parents[1]
                / "custom_components" / "addhon" / "light.py"
            ).exists()
        )

    def test_the_replaced_entity_is_purged_from_the_registry(self) -> None:
        """Same unique_id, different domain: the cleanup MUST be scoped to the
        light domain, or it would delete the select that replaces it."""
        import inspect

        from custom_components.addhon import _remove_legacy_entities

        source = inspect.getsource(_remove_legacy_entities)
        self.assertIn('domain == "light"', source)
        self.assertIn('_panel_light', source)


# --- Task 7: child-lock and touch-tone switches -------------------------------


async def _build_switches(
    attributes: dict | None = None,
    schema: dict | None = None,
    client: RecordingClient | None = None,
):
    from custom_components.addhon import switch
    from custom_components.addhon.const import DOMAIN

    data = {
        "ap-1": {
            "type": APPLIANCE_AP,
            "name": "Purifier",
            "attributes": FULL_ATTRIBUTES if attributes is None else attributes,
            "appliance": _appliance(schema),
            "settings": {},
        }
    }
    coordinator = RefreshingCoordinator(data)
    recording = client if client is not None else RecordingClient()
    hass = RecordingHass(
        {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": recording}}}
    )
    added: list = []
    await switch.async_setup_entry(hass, FakeEntry(), added.extend)
    appliance_switches = [
        e for e in added if getattr(e, "_appliance_id", None) == "ap-1"
    ]
    for entity in appliance_switches:
        entity.hass = hass
    return appliance_switches, recording, coordinator


def _switch_by_key(entities: list) -> dict:
    return {e.entity_description.key: e for e in entities}


TOGGLE_SCHEMA_EXTRA = {
    "lockStatus": {
        "typology": "enum", "category": "command", "mandatory": 0,
        "defaultValue": "0", "enumValues": ["0", "1"],
    },
    "touchToneStatus": {
        "typology": "enum", "category": "command", "mandatory": 0,
        "defaultValue": "1", "enumValues": ["0", "1"],
    },
}


def _toggle_schema(**overrides):
    schema = copy.deepcopy(AP_SCHEMA)
    schema["settings"]["parameters"].update(copy.deepcopy(TOGGLE_SCHEMA_EXTRA))
    for param, values in overrides.items():
        if values is None:
            del schema["settings"]["parameters"][param]
        else:
            schema["settings"]["parameters"][param]["enumValues"] = values
            schema["settings"]["parameters"][param]["defaultValue"] = values[0]
    return schema


class AirPurifierSwitchSetupTest(unittest.IsolatedAsyncioTestCase):
    async def test_both_toggles_are_created(self) -> None:
        entities, _client, _coord = await _build_switches(schema=_toggle_schema())
        self.assertEqual(
            {"child_lock", "touch_tone"}, set(_switch_by_key(entities))
        )

    async def test_unique_ids_are_per_appliance(self) -> None:
        entities, _client, _coord = await _build_switches(schema=_toggle_schema())
        by_key = _switch_by_key(entities)
        self.assertEqual("ap-1_child_lock", by_key["child_lock"].unique_id)
        self.assertEqual("ap-1_touch_tone", by_key["touch_tone"].unique_id)

    async def test_each_toggle_needs_its_writable_parameter(self) -> None:
        entities, _client, _coord = await _build_switches(
            schema=_toggle_schema(lockStatus=None)
        )
        self.assertEqual({"touch_tone"}, set(_switch_by_key(entities)))

    async def test_each_toggle_needs_its_state_attribute(self) -> None:
        attributes = {
            k: v for k, v in FULL_ATTRIBUTES.items() if k != "touchToneStatus"
        }
        entities, _client, _coord = await _build_switches(
            attributes=attributes, schema=_toggle_schema()
        )
        self.assertEqual({"child_lock"}, set(_switch_by_key(entities)))

    async def test_a_non_binary_parameter_creates_no_toggle(self) -> None:
        """A three-value lock is different device behavior; writing "1" against it
        would assert a meaning the schema does not declare."""
        entities, _client, _coord = await _build_switches(
            schema=_toggle_schema(lockStatus=["0", "1", "2"])
        )
        self.assertEqual({"touch_tone"}, set(_switch_by_key(entities)))

    async def test_no_toggles_without_a_settings_command(self) -> None:
        schema = _toggle_schema()
        del schema["settings"]
        entities, _client, _coord = await _build_switches(schema=schema)
        self.assertEqual([], entities)


class AirPurifierSwitchStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_state_maps_the_raw_flag(self) -> None:
        for raw, expected in (("1", True), ("0", False), (1, True), (0, False)):
            entities, _client, _coord = await _build_switches(
                {**FULL_ATTRIBUTES, "lockStatus": raw}, schema=_toggle_schema()
            )
            self.assertIs(
                expected, _switch_by_key(entities)["child_lock"].is_on, raw
            )

    async def test_an_undeclared_raw_value_is_not_on(self) -> None:
        entities, _client, _coord = await _build_switches(
            {**FULL_ATTRIBUTES, "lockStatus": "7"}, schema=_toggle_schema()
        )
        self.assertIs(False, _switch_by_key(entities)["child_lock"].is_on)

    async def test_both_toggles_stay_available_while_off(self) -> None:
        """Neither is live telemetry: a lock and a beep setting are meaningful and
        changeable with the purifier stopped."""
        entities, _client, _coord = await _build_switches(
            {**FULL_ATTRIBUTES, "onOffStatus": "0"}, schema=_toggle_schema()
        )
        for key, entity in _switch_by_key(entities).items():
            self.assertTrue(entity.available, key)


class AirPurifierSwitchWriteTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_toggle_writes_only_its_own_field(self) -> None:
        cases = (
            ("child_lock", "lockStatus"),
            ("touch_tone", "touchToneStatus"),
        )
        for key, param in cases:
            entities, client, coordinator = await _build_switches(
                schema=_toggle_schema()
            )
            entity = _switch_by_key(entities)[key]
            await entity.async_turn_on()
            await entity.async_turn_off()
            self.assertEqual(
                [("settings", {param: "1"}), ("settings", {param: "0"})],
                _sent(client),
                key,
            )
            self.assertEqual(2, coordinator.refreshes, key)

    async def test_a_toggle_never_restates_a_sibling(self) -> None:
        entities, client, _coord = await _build_switches(schema=_toggle_schema())
        by_key = _switch_by_key(entities)
        await by_key["child_lock"].async_turn_on()
        await by_key["touch_tone"].async_turn_off()
        for _command, values in _sent(client):
            self.assertEqual(1, len(values))


class AirPurifierSwitchErrorTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_transport_failure_is_a_localized_error(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        entities, _client, coordinator = await _build_switches(
            schema=_toggle_schema(),
            client=RecordingClient(fail=RuntimeError("cloud down")),
        )
        with self.assertRaises(HomeAssistantError) as caught:
            await _switch_by_key(entities)["child_lock"].async_turn_on()
        self.assertEqual("command_error", caught.exception.translation_key)
        self.assertEqual(0, coordinator.refreshes)


class AirPurifierSwitchArchitectureTest(unittest.TestCase):
    def test_the_legacy_settings_switches_are_untouched(self) -> None:
        """The AC and wine-cooler switches intentionally send a whole command via
        the legacy path. The AP switches must not be folded into them."""
        from custom_components.addhon.switch import _SETTINGS_SWITCHES
        from custom_components.addhon.const import APPLIANCE_AP

        self.assertNotIn(APPLIANCE_AP, _SETTINGS_SWITCHES)

    def test_the_ap_switches_are_a_separate_sparse_class(self) -> None:
        from custom_components.addhon import switch

        self.assertTrue(
            issubclass(switch.HonAirPurifierSwitch, switch.SwitchEntity)
        )
        self.assertNotIn(
            switch.HonSettingsSwitch, switch.HonAirPurifierSwitch.__mro__
        )

    def test_the_ap_switch_setter_uses_the_dispatcher(self) -> None:
        import inspect

        from custom_components.addhon import switch

        source = inspect.getsource(switch.HonAirPurifierSwitch)
        self.assertIn("async_dispatch_patch", source)
        self.assertNotIn("async_send_settings", source)


class AirPurifierSwitchAddTimeTest(unittest.IsolatedAsyncioTestCase):
    """What Home Assistant does to the entity BEFORE it exists (issue #67).

    Both toggles were built, logged as built, and then dropped by
    `entity_platform._async_add_entity`, which reads `entity.name` -> device class
    -> `entity_description.device_class`. The description was a plain dataclass
    without that field, so all four (two toggles x two purifiers) died with
    AttributeError. Every existing test built the entity and then asked it a
    question of its own -- is_on, unique_id, the write path -- which is the one
    thing HA does not do first.
    """

    async def test_the_toggles_survive_the_add_path(self) -> None:
        entities, _client, _coordinator = await _build_switches(schema=_toggle_schema())
        for key, entity in _switch_by_key(entities).items():
            with self.subTest(key=key):
                # The exact attribute lookup that raised, through HA's own
                # precedence. No device class is intended: None, not a crash.
                self.assertIsNone(entity.device_class, key)

    def test_the_description_carries_what_home_assistant_reads(self) -> None:
        """The description is published as `entity_description`, so it is HA's to
        read: it must be an actual SwitchEntityDescription, not a look-alike."""
        from custom_components.addhon import switch

        self.assertTrue(
            issubclass(
                switch.HonAirPurifierSwitchDescription, switch.SwitchEntityDescription
            )
        )
        for description in switch._AIR_PURIFIER_SWITCHES:
            with self.subTest(key=description.key):
                self.assertIsNone(description.device_class)

    def test_only_the_published_description_needs_the_ha_base(self) -> None:
        """The sibling description stays a plain dataclass on purpose: its entity
        keeps it in `_desc`, so HA never reads it. Pinned so a future "make them
        consistent" pass does not read the fix as a rule about all of them."""
        import inspect

        from custom_components.addhon import switch

        source = inspect.getsource(switch.HonSettingsSwitch)
        self.assertIn("self._desc = description", source)
        self.assertNotIn("self.entity_description", source)


class AirPurifierSwitchSkipLoggingTest(unittest.IsolatedAsyncioTestCase):
    """A rejected toggle must say WHY.

    Both gates used to `continue` in silence while the summary line named only
    what was built, so a purifier missing a control looked exactly like a
    purifier that never reached the branch. A field report sat in that state for
    two weeks because neither the log nor the dump carried the deciding input.
    """

    async def _skip_records(self, **kwargs):
        import logging

        from custom_components.addhon import switch

        with self.assertLogs(switch._LOGGER, level=logging.DEBUG) as caught:
            await _build_switches(**kwargs)
        return [line for line in caught.output if "no purifier" in line]

    async def test_a_capability_rejection_names_the_capability(self) -> None:
        """lockStatus declared over three values is not a toggle, so the gate
        refuses it. The log must say which capability said no."""
        records = await self._skip_records(
            schema=_toggle_schema(lockStatus=["0", "1", "2"])
        )
        joined = "\n".join(records)

        self.assertIn("child_lock", joined)
        self.assertIn("supports_lock", joined)
        self.assertIn("supports_lock=False", joined)

    async def test_a_missing_state_is_distinguished_from_a_missing_capability(
        self,
    ) -> None:
        """The two gates fail for opposite reasons and the reader must be able to
        tell them apart: here the schema is complete and the STATE is absent."""
        attributes = {k: v for k, v in FULL_ATTRIBUTES.items() if k != "lockStatus"}
        records = await self._skip_records(
            attributes=attributes, schema=_toggle_schema()
        )
        joined = "\n".join(records)

        self.assertIn("supports_lock=True", joined)
        self.assertIn("reports_lockStatus=False", joined)

    async def test_the_rejection_carries_the_capability_set(self) -> None:
        """The materialised schema values are what the gate compares, and no
        other artifact carries them: the dump casts a range's bounds to float, so
        "0"/"1" and "0.0"/"1.0" are indistinguishable there."""
        records = await self._skip_records(
            schema=_toggle_schema(lockStatus=["0", "1", "2"])
        )
        joined = "\n".join(records)

        self.assertIn("lock_values=", joined)
        self.assertIn("settings_command=", joined)

    async def test_nothing_is_logged_when_both_toggles_are_built(self) -> None:
        records = await self._skip_records(schema=_toggle_schema())
        self.assertEqual([], records)

    async def test_the_rejection_never_carries_the_appliance_id(self) -> None:
        records = await self._skip_records(
            schema=_toggle_schema(lockStatus=["0", "1", "2"])
        )
        joined = "\n".join(records)

        self.assertNotIn("ap-1", joined)
        self.assertIn("***", joined)


class _ExplodingAppliance:
    """An appliance whose schema cannot be read at all."""

    zone = 0
    unique_id = "ap-broken"

    @property
    def commands(self):
        raise RuntimeError("schema unreadable")


class SwitchPlatformIsolationTest(unittest.IsolatedAsyncioTestCase):
    """One unreadable appliance must cost only its own switches.

    The setup loop used to run inline: a single exception left `async_setup_entry`
    before `async_add_entities`, so EVERY switch of the entry disappeared, the
    account debug toggles included. From the outside that is indistinguishable from
    "this integration has no child lock".
    """

    async def _setup(self, data: dict):
        from custom_components.addhon import switch
        from custom_components.addhon.const import DOMAIN

        coordinator = RefreshingCoordinator(data)
        hass = RecordingHass(
            {
                DOMAIN: {
                    "entry-1": {
                        "coordinator": coordinator,
                        "client": RecordingClient(),
                    }
                }
            }
        )
        added: list = []
        await switch.async_setup_entry(hass, FakeEntry(), added.extend)
        return added

    def _data(self, extra: dict | None = None) -> dict:
        data = {
            "ap-1": {
                "type": APPLIANCE_AP,
                "name": "Purifier",
                "attributes": FULL_ATTRIBUTES,
                "appliance": _appliance(_toggle_schema()),
                "settings": {},
            }
        }
        data.update(extra or {})
        return data

    async def test_a_healthy_appliance_survives_a_broken_sibling(self) -> None:
        added = await self._setup(
            self._data(
                {
                    "ap-broken": {
                        "type": APPLIANCE_AP,
                        "name": "Broken",
                        "attributes": FULL_ATTRIBUTES,
                        "appliance": _ExplodingAppliance(),
                        "settings": {},
                    }
                }
            )
        )
        healthy = [e for e in added if getattr(e, "_appliance_id", None) == "ap-1"]
        self.assertEqual({"child_lock", "touch_tone"}, set(_switch_by_key(healthy)))

    async def test_the_account_debug_toggles_survive_it_too(self) -> None:
        """They are not tied to any appliance, so an appliance failure must not
        reach them. Counted by identity, not by key: they carry no description."""
        from custom_components.addhon.switch import HonDebugSwitch

        added = await self._setup(
            self._data(
                {
                    "ap-broken": {
                        "type": APPLIANCE_AP,
                        "name": "Broken",
                        "attributes": FULL_ATTRIBUTES,
                        "appliance": _ExplodingAppliance(),
                        "settings": {},
                    }
                }
            )
        )
        self.assertEqual(
            2, len([e for e in added if isinstance(e, HonDebugSwitch)])
        )

    async def test_a_coordinator_without_data_yet_adds_the_debug_toggles(self) -> None:
        """`coordinator.data` is None until the first refresh lands. Iterating it
        raised, which took the platform down before it created anything."""
        from custom_components.addhon import switch
        from custom_components.addhon.const import DOMAIN
        from custom_components.addhon.switch import HonDebugSwitch

        coordinator = RefreshingCoordinator({})
        coordinator.data = None
        hass = RecordingHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}}
        )
        added: list = []
        await switch.async_setup_entry(hass, FakeEntry(), added.extend)

        self.assertEqual(
            2, len([e for e in added if isinstance(e, HonDebugSwitch)])
        )


# --- Task 8: aroma selection --------------------------------------------------

AROMA_SCHEMA_EXTRA = {
    "aromaStatus": {
        "typology": "enum", "category": "command", "mandatory": 0,
        "defaultValue": "0", "enumValues": ["0", "1", "2", "3", "4"],
    },
    "aromaTimeOn": {
        "typology": "range", "category": "command", "mandatory": 0,
        "minimumValue": "1", "maximumValue": "3600", "incrementValue": "1",
        "defaultValue": "60",
    },
    "aromaTimeOff": {
        "typology": "range", "category": "command", "mandatory": 0,
        "minimumValue": "1", "maximumValue": "3600", "incrementValue": "1",
        "defaultValue": "60",
    },
}


def _aroma_schema(**overrides):
    schema = copy.deepcopy(AP_SCHEMA)
    schema["settings"]["parameters"].update(copy.deepcopy(AROMA_SCHEMA_EXTRA))
    for param, values in overrides.items():
        if values is None:
            del schema["settings"]["parameters"][param]
        else:
            schema["settings"]["parameters"][param]["enumValues"] = values
            schema["settings"]["parameters"][param]["defaultValue"] = values[0]
    return schema


async def _build_selects(
    attributes: dict | None = None,
    schema: dict | None = None,
    client: RecordingClient | None = None,
):
    from custom_components.addhon import select
    from custom_components.addhon.const import DOMAIN

    data = {
        "ap-1": {
            "type": APPLIANCE_AP,
            "name": "Purifier",
            "attributes": FULL_ATTRIBUTES if attributes is None else attributes,
            "appliance": _appliance(schema if schema is not None else _aroma_schema()),
            "settings": {},
        }
    }
    coordinator = RefreshingCoordinator(data)
    recording = client if client is not None else RecordingClient()
    hass = RecordingHass(
        {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": recording}}}
    )
    added: list = []
    await select.async_setup_entry(hass, FakeEntry(), added.extend)
    # The AP branch also builds the panel light select; these tests are about the
    # aroma one, and each has its own suite.
    aroma = [e for e in added if (e.unique_id or "").endswith("_aroma")]
    for entity in aroma:
        entity.hass = hass
    return aroma, recording, coordinator


class AirPurifierAromaSetupTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_capable_purifier_gets_one_aroma_select(self) -> None:
        entities, _client, _coord = await _build_selects()
        self.assertEqual(1, len(entities))
        self.assertEqual("ap-1_aroma", entities[0].unique_id)

    async def test_options_are_the_full_canonical_set(self) -> None:
        entities, _client, _coord = await _build_selects()
        self.assertEqual(
            ["off", "soft", "mid", "h_biotics", "custom"], entities[0].options
        )

    async def test_options_follow_the_live_enum(self) -> None:
        entities, _client, _coord = await _build_selects(
            schema=_aroma_schema(aromaStatus=["0", "1"])
        )
        self.assertEqual(["off", "soft"], entities[0].options)

    async def test_custom_is_withheld_without_both_timing_ranges(self) -> None:
        """Status 4 alone cannot complete the Custom contract, so the option is not
        offered rather than sent without its timing fields."""
        for missing in ("aromaTimeOn", "aromaTimeOff"):
            entities, _client, _coord = await _build_selects(
                schema=_aroma_schema(**{missing: None})
            )
            self.assertEqual(
                ["off", "soft", "mid", "h_biotics"], entities[0].options, missing
            )

    async def test_no_select_without_the_aroma_parameter(self) -> None:
        entities, _client, _coord = await _build_selects(
            schema=_aroma_schema(aromaStatus=None)
        )
        self.assertEqual([], entities)

    async def test_no_select_without_the_aroma_state(self) -> None:
        attributes = {k: v for k, v in FULL_ATTRIBUTES.items() if k != "aromaStatus"}
        entities, _client, _coord = await _build_selects(attributes=attributes)
        self.assertEqual([], entities)


class AirPurifierAromaStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_current_option_maps_the_raw_state(self) -> None:
        for raw, option in (
            ("0", "off"), ("1", "soft"), ("2", "mid"),
            ("3", "h_biotics"), ("4", "custom"),
        ):
            entities, _client, _coord = await _build_selects(
                {**FULL_ATTRIBUTES, "aromaStatus": raw}
            )
            self.assertEqual(option, entities[0].current_option, raw)

    async def test_an_unoffered_raw_state_reads_as_unknown(self) -> None:
        """A live value outside the offered set must read unknown, never be mapped
        to an option the user could not have chosen."""
        entities, _client, _coord = await _build_selects(
            {**FULL_ATTRIBUTES, "aromaStatus": "4"},
            schema=_aroma_schema(aromaTimeOff=None),
        )
        self.assertNotIn("custom", entities[0].options)
        self.assertIsNone(entities[0].current_option)

    async def test_an_undeclared_raw_state_reads_as_unknown(self) -> None:
        entities, _client, _coord = await _build_selects(
            {**FULL_ATTRIBUTES, "aromaStatus": "9"}
        )
        self.assertIsNone(entities[0].current_option)

    async def test_the_select_is_unavailable_while_the_purifier_is_off(self) -> None:
        """The observed application sends aroma patches only during an active
        session, and selecting a mode must not implicitly start the appliance."""
        entities, _client, _coord = await _build_selects(
            {**FULL_ATTRIBUTES, "onOffStatus": "0"}
        )
        self.assertFalse(entities[0].available)

    async def test_the_select_is_available_while_running(self) -> None:
        entities, _client, _coord = await _build_selects()
        self.assertTrue(entities[0].available)


class AirPurifierAromaWriteTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_normal_mode_sends_only_the_status(self) -> None:
        for option, raw in (
            ("off", "0"), ("soft", "1"), ("mid", "2"), ("h_biotics", "3"),
        ):
            entities, client, coordinator = await _build_selects()
            await entities[0].async_select_option(option)
            self.assertEqual(
                [("settings", {"aromaStatus": raw})], _sent(client), option
            )
            self.assertEqual(1, coordinator.refreshes, option)

    async def test_custom_sends_the_status_and_both_current_times(self) -> None:
        entities, client, _coord = await _build_selects(
            {**FULL_ATTRIBUTES, "aromaTimeOn": "30", "aromaTimeOff": "90"}
        )
        await entities[0].async_select_option("custom")
        self.assertEqual(
            [(
                "settings",
                {"aromaStatus": "4", "aromaTimeOn": "30", "aromaTimeOff": "90"},
            )],
            _sent(client),
        )

    async def test_custom_falls_back_to_the_schema_defaults(self) -> None:
        """A live timing value outside the declared range is not sent back; the
        command's own value stands in, so Custom stays completable."""
        entities, client, _coord = await _build_selects(
            {**FULL_ATTRIBUTES, "aromaTimeOn": "99999", "aromaTimeOff": ""}
        )
        await entities[0].async_select_option("custom")
        self.assertEqual(
            [(
                "settings",
                {"aromaStatus": "4", "aromaTimeOn": "60", "aromaTimeOff": "60"},
            )],
            _sent(client),
        )

    async def test_no_selection_ever_starts_the_purifier(self) -> None:
        entities, client, _coord = await _build_selects()
        for option in ("off", "soft", "mid", "h_biotics", "custom"):
            await entities[0].async_select_option(option)
        for command, values in _sent(client):
            self.assertEqual("settings", command)
            self.assertNotIn("onOffStatus", values)
            self.assertNotIn("machMode", values)

    async def test_a_normal_mode_never_carries_a_timing_field(self) -> None:
        entities, client, _coord = await _build_selects()
        await entities[0].async_select_option("soft")
        self.assertEqual({"aromaStatus"}, set(_sent(client)[0][1]))


class AirPurifierAromaErrorTest(unittest.IsolatedAsyncioTestCase):
    async def test_an_unoffered_option_is_rejected_without_a_write(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        entities, client, _coord = await _build_selects(
            schema=_aroma_schema(aromaStatus=["0", "1"])
        )
        with self.assertRaises(HomeAssistantError):
            await entities[0].async_select_option("h_biotics")
        self.assertEqual([], _sent(client))

    async def test_a_transport_failure_is_a_localized_error(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        entities, _client, coordinator = await _build_selects(
            client=RecordingClient(fail=RuntimeError("cloud down"))
        )
        with self.assertRaises(HomeAssistantError) as caught:
            await entities[0].async_select_option("soft")
        self.assertEqual("command_error", caught.exception.translation_key)
        self.assertEqual(0, coordinator.refreshes)


class AirPurifierAromaArchitectureTest(unittest.TestCase):
    def test_the_aroma_select_uses_the_dispatcher(self) -> None:
        import inspect

        from custom_components.addhon import select

        source = inspect.getsource(select.HonAirPurifierAromaSelect)
        self.assertIn("async_dispatch_patch", source)
        self.assertNotIn("async_send_settings", source)


# --- Task 9: the experimental option and the custom timing numbers -----------


# The per-zone families the induction hob generates, spelled out here so the
# "exact set" pins below stay hand-checkable while covering 30-odd rows. The zone
# ceiling is repeated on purpose: widening the tables to seven zones has to fail
# these tests rather than pass silently.
_HOB_TYPES = ("IH", "HOB")
_HOB_ZONE_RANGE = range(1, 7)


def _hob_keys(*families: str) -> set[tuple[str, str]]:
    return {
        (app_type, f"{family}{zone}")
        for app_type in _HOB_TYPES
        for family in families
        for zone in _HOB_ZONE_RANGE
    }


class ExperimentalGateTest(unittest.IsolatedAsyncioTestCase):
    """Nothing whose meaning is inferred from incomplete evidence may exist on a
    default installation."""

    async def test_no_experimental_sensor_by_default(self) -> None:
        keys = set(_by_key(await _build_sensors(FULL_ATTRIBUTES)))
        self.assertNotIn("air_quality_label", keys)
        self.assertEqual(13, len(keys))

    async def test_no_experimental_binary_by_default(self) -> None:
        keys = set(_by_key(await _build_binary(FULL_ATTRIBUTES)))
        self.assertEqual({"eco_active", "problem", "connectivity"}, keys)

    async def test_an_explicit_false_is_the_same_as_absent(self) -> None:
        keys = set(_by_key(await _build_sensors(FULL_ATTRIBUTES, _experimental(False))))
        self.assertNotIn("air_quality_label", keys)

    def test_the_flag_defaults_off_for_every_other_sensor(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        experimental = {
            (app_type, d.key)
            for app_type, descs in SENSORS.items()
            for d in descs
            if d.experimental
        }
        self.assertEqual(
            {(APPLIANCE_AP, "air_quality_label")}
            | _hob_keys("plate_temp_zone", "program_code_zone", "program_phase_zone")
            | {(app_type, key) for app_type in _HOB_TYPES
               for key in ("timer_hh", "timer_mm")},
            experimental,
        )

    def test_the_flag_defaults_off_for_every_other_binary(self) -> None:
        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        experimental = {
            (app_type, d.key)
            for app_type, descs in BINARY_SENSORS.items()
            for d in descs
            if d.experimental
        }
        self.assertEqual(
            {(APPLIANCE_AP, "co_alarm")} | _hob_keys("combi_mode_zone"),
            experimental,
        )

    async def test_the_raw_co_sensor_stays_standard(self) -> None:
        """The interpreted alarm is experimental; the raw level it reads is not,
        so enabling the option must not move or duplicate the raw sensor."""
        default = _by_key(await _build_sensors(FULL_ATTRIBUTES))
        enabled = _by_key(await _build_sensors(FULL_ATTRIBUTES, _experimental(True)))
        self.assertIn("co", default)
        self.assertIn("co", enabled)
        self.assertFalse(default["co"].entity_description.experimental)


class ExperimentalAirQualityLabelTest(unittest.IsolatedAsyncioTestCase):
    async def _label(self, raw: str | None):
        attributes = dict(FULL_ATTRIBUTES)
        if raw is None:
            attributes.pop("airQuality")
        else:
            attributes["airQuality"] = raw
        return _by_key(await _build_sensors(attributes, _experimental(True)))

    async def test_the_option_creates_the_label(self) -> None:
        entities = await self._label("0")
        self.assertIn("air_quality_label", entities)
        self.assertEqual(
            "ap-1_air_quality_label", entities["air_quality_label"].unique_id
        )

    async def test_it_is_gated_on_the_source_attribute(self) -> None:
        self.assertNotIn("air_quality_label", await self._label(None))

    async def test_the_only_confirmed_value_reads_good(self) -> None:
        entity = (await self._label("0"))["air_quality_label"]
        self.assertEqual("good", entity.native_value)
        self.assertTrue(entity.available)

    async def test_an_unconfirmed_value_hides_instead_of_guessing(self) -> None:
        for raw in ("1", "2", "3", "9"):
            entity = (await self._label(raw))["air_quality_label"]
            self.assertIsNone(entity.native_value, raw)
            self.assertFalse(entity.available, raw)

    async def test_it_hides_with_the_purifier_stopped(self) -> None:
        entities = _by_key(
            await _build_sensors(
                {**FULL_ATTRIBUTES, "onOffStatus": "0"}, _experimental(True)
            )
        )
        self.assertFalse(entities["air_quality_label"].available)

    def test_it_is_an_enum_limited_to_the_confirmed_labels(self) -> None:
        from custom_components.addhon.air_purifier import AP_AIR_QUALITY_LABELS
        from custom_components.addhon.sensor import SENSORS

        table = {d.key: d for d in SENSORS[APPLIANCE_AP]}
        description = table["air_quality_label"]
        self.assertEqual("enum", description.device_class)
        self.assertEqual(
            sorted(set(AP_AIR_QUALITY_LABELS.values())), list(description.options)
        )
        self.assertIsNone(description.native_unit_of_measurement)
        self.assertIsNone(description.state_class)


class ExperimentalCoAlarmTest(unittest.IsolatedAsyncioTestCase):
    async def _co(self, raw: str | None):
        attributes = dict(FULL_ATTRIBUTES)
        if raw is None:
            attributes.pop("coLevel")
        else:
            attributes["coLevel"] = raw
        return _by_key(await _build_binary(attributes, _experimental(True)))

    async def test_the_option_creates_the_alarm(self) -> None:
        entities = await self._co("1")
        self.assertIn("co_alarm", entities)
        self.assertEqual("ap-1_co_alarm", entities["co_alarm"].unique_id)

    async def test_it_is_gated_on_the_source_attribute(self) -> None:
        self.assertNotIn("co_alarm", await self._co(None))

    async def test_it_is_on_only_for_the_observed_alarm_value(self) -> None:
        entity = (await self._co("2"))["co_alarm"]
        self.assertTrue(entity.is_on)
        self.assertTrue(entity.available)

    async def test_the_observed_quiet_value_reports_no_alarm(self) -> None:
        """Raw 0 held across every capture of a healthy device in a room with no
        combustion source, so it is reported as such. Leaving it unavailable made
        the entity look broken on exactly the devices that are fine."""
        entity = (await self._co("0"))["co_alarm"]
        self.assertIs(False, entity.is_on)
        self.assertTrue(entity.available)

    async def test_a_value_between_the_two_ends_still_hides(self) -> None:
        """Only the two ends have been observed with their meaning. A middle
        reading is not evidence of safety, so it must not read as one."""
        for raw in ("1", "3"):
            entity = (await self._co(raw))["co_alarm"]
            self.assertIsNone(entity.is_on, raw)
            self.assertFalse(entity.available, raw)

    async def test_it_stays_readable_with_the_purifier_stopped(self) -> None:
        entities = _by_key(
            await _build_binary(
                {**FULL_ATTRIBUTES, "coLevel": "2", "onOffStatus": "0"},
                _experimental(True),
            )
        )
        self.assertTrue(entities["co_alarm"].available)

    def test_it_is_diagnostic_and_never_a_safety_device_class(self) -> None:
        from homeassistant.components.binary_sensor import BinarySensorDeviceClass

        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        table = {d.key: d for d in BINARY_SENSORS[APPLIANCE_AP]}
        description = table["co_alarm"]
        self.assertEqual("diagnostic", description.entity_category)
        self.assertNotEqual(BinarySensorDeviceClass.SAFETY, description.device_class)
        self.assertIsNone(description.device_class)

    def test_no_air_purifier_binary_claims_to_be_a_safety_device(self) -> None:
        from homeassistant.components.binary_sensor import BinarySensorDeviceClass

        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        for description in BINARY_SENSORS[APPLIANCE_AP]:
            self.assertNotEqual(
                BinarySensorDeviceClass.SAFETY, description.device_class, description.key
            )


async def _build_numbers(
    attributes: dict | None = None,
    schema: dict | None = None,
    client: RecordingClient | None = None,
    options: dict | None = None,
):
    from custom_components.addhon import number
    from custom_components.addhon.const import DOMAIN

    data = {
        "ap-1": {
            "type": APPLIANCE_AP,
            "name": "Purifier",
            "attributes": FULL_ATTRIBUTES if attributes is None else attributes,
            "appliance": _appliance(schema if schema is not None else _aroma_schema()),
            "settings": {},
        }
    }
    coordinator = RefreshingCoordinator(data)
    recording = client if client is not None else RecordingClient()
    hass = RecordingHass(
        {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": recording}}}
    )
    added: list = []
    await number.async_setup_entry(
        hass,
        FakeEntry(options=_experimental(True) if options is None else options),
        added.extend,
    )
    for entity in added:
        entity.hass = hass
    return added, recording, coordinator


CUSTOM_ACTIVE = {**FULL_ATTRIBUTES, "aromaStatus": "4"}


class AirPurifierTimeNumberSetupTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_numbers_without_the_option(self) -> None:
        entities, _client, _coord = await _build_numbers(options={})
        self.assertEqual([], entities)

    async def test_both_timings_are_created_with_the_option(self) -> None:
        entities, _client, _coord = await _build_numbers()
        self.assertEqual(
            {"aroma_time_on", "aroma_time_off"},
            {e.entity_description.key for e in entities},
        )

    async def test_unique_ids_do_not_encode_the_support_level(self) -> None:
        entities, _client, _coord = await _build_numbers()
        ids = {e.unique_id for e in entities}
        self.assertEqual({"ap-1_aroma_time_on", "ap-1_aroma_time_off"}, ids)
        for unique_id in ids:
            self.assertNotIn("experimental", unique_id)

    async def test_a_device_without_the_custom_mode_gets_no_numbers(self) -> None:
        entities, _client, _coord = await _build_numbers(
            schema=_aroma_schema(aromaStatus=["0", "1", "2"])
        )
        self.assertEqual([], entities)

    async def test_a_missing_timing_parameter_removes_both_numbers(self) -> None:
        """Custom cannot be completed without both fields, so the mode is not
        offered at all and neither timing is writable."""
        entities, _client, _coord = await _build_numbers(
            schema=_aroma_schema(aromaTimeOn=None)
        )
        self.assertEqual([], entities)

    async def test_an_unreported_timing_creates_no_number_for_it(self) -> None:
        attributes = {k: v for k, v in CUSTOM_ACTIVE.items() if k != "aromaTimeOff"}
        entities, _client, _coord = await _build_numbers(attributes=attributes)
        self.assertEqual(
            {"aroma_time_on"}, {e.entity_description.key for e in entities}
        )

    async def test_bounds_and_step_come_from_the_live_schema(self) -> None:
        entities, _client, _coord = await _build_numbers()
        for entity in entities:
            self.assertEqual(1.0, entity.native_min_value)
            self.assertEqual(3600.0, entity.native_max_value)
            self.assertEqual(1.0, entity.native_step)

    async def test_a_narrower_schema_narrows_the_entity(self) -> None:
        schema = _aroma_schema()
        schema["settings"]["parameters"]["aromaTimeOn"].update(
            {"minimumValue": "10", "maximumValue": "600", "incrementValue": "5"}
        )
        entities, _client, _coord = await _build_numbers(schema=schema)
        by_key = {e.entity_description.key: e for e in entities}
        self.assertEqual(10.0, by_key["aroma_time_on"].native_min_value)
        self.assertEqual(600.0, by_key["aroma_time_on"].native_max_value)
        self.assertEqual(5.0, by_key["aroma_time_on"].native_step)
        self.assertEqual(1.0, by_key["aroma_time_off"].native_min_value)

    async def test_the_bounds_follow_the_parameter_after_setup(self) -> None:
        """Read live on every access, not snapshotted at construction: the engine
        rules can move min/max/step while the entry stays loaded, and offering a
        stale range would let the UI submit a value the device now rejects."""
        entities, _client, coordinator = await _build_numbers()
        by_key = {e.entity_description.key: e for e in entities}
        appliance = coordinator.data["ap-1"]["appliance"]
        parameter = appliance.commands["settings"].parameters["aromaTimeOn"]
        parameter.min = 5
        parameter.max = 600
        parameter.step = 5
        self.assertEqual(5.0, by_key["aroma_time_on"].native_min_value)
        self.assertEqual(600.0, by_key["aroma_time_on"].native_max_value)
        self.assertEqual(5.0, by_key["aroma_time_on"].native_step)
        self.assertEqual(3600.0, by_key["aroma_time_off"].native_max_value)

    async def test_the_timings_are_seconds(self) -> None:
        entities, _client, _coord = await _build_numbers()
        for entity in entities:
            self.assertEqual("s", entity.entity_description.native_unit_of_measurement)

    async def test_no_other_appliance_gains_a_number(self) -> None:
        """The AP branch is additive: the fridge/oven tables are untouched."""
        from custom_components.addhon.number import NUMBERS

        self.assertNotIn(APPLIANCE_AP, NUMBERS)


class AirPurifierTimeNumberStateTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_live_readings_are_reported(self) -> None:
        entities, _client, _coord = await _build_numbers(
            attributes={**CUSTOM_ACTIVE, "aromaTimeOn": "30", "aromaTimeOff": "90"}
        )
        by_key = {e.entity_description.key: e for e in entities}
        self.assertEqual(30.0, by_key["aroma_time_on"].native_value)
        self.assertEqual(90.0, by_key["aroma_time_off"].native_value)

    async def test_a_non_numeric_reading_is_unknown(self) -> None:
        entities, _client, _coord = await _build_numbers(
            attributes={**CUSTOM_ACTIVE, "aromaTimeOn": "n/a"}
        )
        by_key = {e.entity_description.key: e for e in entities}
        self.assertIsNone(by_key["aroma_time_on"].native_value)

    async def test_they_are_available_while_custom_runs(self) -> None:
        entities, _client, _coord = await _build_numbers(attributes=CUSTOM_ACTIVE)
        for entity in entities:
            self.assertTrue(entity.available, entity.entity_description.key)

    async def test_they_hide_when_custom_is_not_the_live_mode(self) -> None:
        """The write repeats aromaStatus=4, so allowing it in another mode would
        switch the mode as a side effect."""
        for raw in ("0", "1", "2", "3"):
            entities, _client, _coord = await _build_numbers(
                attributes={**FULL_ATTRIBUTES, "aromaStatus": raw}
            )
            for entity in entities:
                self.assertFalse(entity.available, raw)

    async def test_they_hide_while_the_purifier_is_stopped(self) -> None:
        entities, _client, _coord = await _build_numbers(
            attributes={**CUSTOM_ACTIVE, "onOffStatus": "0"}
        )
        for entity in entities:
            self.assertFalse(entity.available, entity.entity_description.key)


class AirPurifierTimeNumberWriteTest(unittest.IsolatedAsyncioTestCase):
    async def _set(self, key: str, value: float, attributes: dict | None = None):
        entities, client, coordinator = await _build_numbers(
            attributes=CUSTOM_ACTIVE if attributes is None else attributes
        )
        by_key = {e.entity_description.key: e for e in entities}
        await by_key[key].async_set_native_value(value)
        return client, coordinator

    async def test_changing_the_on_time_sends_the_status_and_that_field_only(
        self,
    ) -> None:
        client, _coord = await self._set("aroma_time_on", 30)
        self.assertEqual(
            [("settings", {"aromaStatus": "4", "aromaTimeOn": "30"})], _sent(client)
        )

    async def test_changing_the_off_time_sends_the_status_and_that_field_only(
        self,
    ) -> None:
        client, _coord = await self._set("aroma_time_off", 120)
        self.assertEqual(
            [("settings", {"aromaStatus": "4", "aromaTimeOff": "120"})], _sent(client)
        )

    async def test_a_write_never_carries_the_sibling_timing(self) -> None:
        """Repeating the sibling would push a stale local value over a concurrent
        change to the other field."""
        client, _coord = await self._set("aroma_time_on", 30)
        self.assertNotIn("aromaTimeOff", client.patches[0].values)

    async def test_a_write_never_carries_power_or_mode(self) -> None:
        client, _coord = await self._set("aroma_time_on", 30)
        for forbidden in ("onOffStatus", "machMode"):
            self.assertNotIn(forbidden, client.patches[0].values)

    async def test_an_integral_value_is_sent_without_a_decimal_tail(self) -> None:
        client, _coord = await self._set("aroma_time_on", 30.0)
        self.assertEqual("30", client.patches[0].values["aromaTimeOn"])

    async def test_the_action_names_the_intent(self) -> None:
        client, _coord = await self._set("aroma_time_on", 30)
        self.assertEqual("ap_set_aroma_time", client.patches[0].action)

    async def test_a_successful_write_refreshes(self) -> None:
        _client, coordinator = await self._set("aroma_time_on", 30)
        self.assertEqual(1, coordinator.refreshes)


class AirPurifierTimeNumberErrorTest(unittest.IsolatedAsyncioTestCase):
    async def _refuse(self, key: str, value: float, attributes: dict | None = None):
        from homeassistant.exceptions import HomeAssistantError

        entities, client, coordinator = await _build_numbers(
            attributes=CUSTOM_ACTIVE if attributes is None else attributes
        )
        by_key = {e.entity_description.key: e for e in entities}
        with self.assertRaises(HomeAssistantError) as caught:
            await by_key[key].async_set_native_value(value)
        self.assertEqual([], _sent(client))
        self.assertEqual(0, coordinator.refreshes)
        return caught.exception

    async def test_a_write_outside_the_live_range_is_refused(self) -> None:
        error = await self._refuse("aroma_time_on", 7200)
        self.assertEqual("command_error", error.translation_key)

    async def test_a_write_below_the_live_range_is_refused(self) -> None:
        await self._refuse("aroma_time_on", 0)

    async def test_a_write_with_custom_inactive_changes_nothing(self) -> None:
        """It must refuse rather than trust the UI to have hidden it, and it must
        never switch the aroma mode as a side effect of a timing change."""
        error = await self._refuse(
            "aroma_time_on", 30, {**FULL_ATTRIBUTES, "aromaStatus": "1"}
        )
        self.assertEqual("aroma_custom_not_active", error.translation_key)

    async def test_a_transport_failure_is_a_localized_error(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        entities, _client, coordinator = await _build_numbers(
            attributes=CUSTOM_ACTIVE,
            client=RecordingClient(fail=RuntimeError("cloud down")),
        )
        by_key = {e.entity_description.key: e for e in entities}
        with self.assertRaises(HomeAssistantError) as caught:
            await by_key["aroma_time_on"].async_set_native_value(30)
        self.assertEqual("command_error", caught.exception.translation_key)
        self.assertEqual(0, coordinator.refreshes)


class AirPurifierTimeNumberArchitectureTest(unittest.TestCase):
    def test_the_ap_number_dispatches(self) -> None:
        import inspect

        from custom_components.addhon import number

        source = inspect.getsource(number.HonAirPurifierTimeNumber)
        self.assertIn("async_dispatch_patch", source)
        self.assertNotIn("async_send_command", source)

    def test_the_shared_number_keeps_both_senders(self) -> None:
        # `HonNumber` serves the fridge/freezer/wine-cooler/oven setpoints, which
        # need the whole command group on the wire, AND the cooker hood's delayed
        # switch-off, which must not send it (the hood's `settings` group carries
        # clock fields the device never mirrors back). One description field picks
        # the channel, so BOTH senders have to be present here: losing the legacy
        # one would make every fridge setpoint sparse, losing the dispatcher one
        # would put the hood's clock back in the payload. Which appliance gets
        # which is pinned behaviourally in test_number_setpoints and
        # test_hood_entities.
        import inspect

        from custom_components.addhon import number

        source = inspect.getsource(number.HonNumber)
        self.assertIn("async_send_command", source)
        self.assertIn("async_dispatch_patch", source)


# --- Task 10: a schema ahead of the integration creates nothing ---------------


AHEAD_SCHEMA_EXTRA = {
    "machMode": {
        "typology": "enum", "category": "command", "mandatory": 0,
        "defaultValue": "2", "enumValues": ["1", "2", "3", "4"],
    },
    "aromaStatus": {
        "typology": "enum", "category": "command", "mandatory": 0,
        "defaultValue": "0", "enumValues": ["0", "1", "2", "3", "4", "5"],
    },
    "aromaTimeOn": {
        "typology": "range", "category": "command", "mandatory": 0,
        "minimumValue": "1", "maximumValue": "3600", "incrementValue": "1",
        "defaultValue": "60",
    },
    "aromaTimeOff": {
        "typology": "range", "category": "command", "mandatory": 0,
        "minimumValue": "1", "maximumValue": "3600", "incrementValue": "1",
        "defaultValue": "60",
    },
    # Declared by the application, deliberately unmapped.
    "humidificationStatus": {
        "typology": "enum", "category": "command", "mandatory": 0,
        "defaultValue": "0", "enumValues": ["0", "1"],
    },
}


def _ahead_schema():
    """A device whose schema and shadow run AHEAD of this code: an extra machMode,
    an extra aromaStatus and a whole unmapped parameter."""
    schema = copy.deepcopy(AP_SCHEMA)
    schema["settings"]["parameters"].update(copy.deepcopy(TOGGLE_SCHEMA_EXTRA))
    schema["settings"]["parameters"].update(copy.deepcopy(AHEAD_SCHEMA_EXTRA))
    schema["startProgram"]["parameters"]["machMode"]["enumValues"] = [
        "1", "2", "3", "4"
    ]
    return schema


AHEAD_ATTRIBUTES = {
    **FULL_ATTRIBUTES,
    "machMode": "3",
    "no2ValueIndoor": "7",
    "humidificationStatus": "0",
}


class FutureCapabilityCreatesNothingTest(unittest.IsolatedAsyncioTestCase):
    """Passive capture only: an undeclared value or an unknown parameter must show
    up in diagnostics and change no entity, option or write."""

    async def test_an_unknown_parameter_creates_no_entity(self) -> None:
        sensors = set(_by_key(await _build_sensors(AHEAD_ATTRIBUTES)))
        binaries = set(_by_key(await _build_binary(AHEAD_ATTRIBUTES)))
        self.assertEqual(set(AirPurifierSensorTableTest.STANDARD), sensors)
        self.assertEqual({"eco_active", "problem", "connectivity"}, binaries)

    async def test_an_extra_mode_is_not_offered_as_a_preset(self) -> None:
        entities, _client, _coord = await _build_fan(
            attributes=AHEAD_ATTRIBUTES, schema=_ahead_schema()
        )
        self.assertEqual(["sleep", "auto", "max"], entities[0].preset_modes)

    async def test_an_active_unhandled_mode_reads_as_no_preset(self) -> None:
        """The purifier is RUNNING in mode 3. The fan must report no preset rather
        than inventing a name for it, and must stay on."""
        entities, _client, _coord = await _build_fan(
            attributes=AHEAD_ATTRIBUTES, schema=_ahead_schema()
        )
        self.assertTrue(entities[0].is_on)
        self.assertIsNone(entities[0].preset_mode)

    async def test_an_extra_aroma_value_is_not_offered(self) -> None:
        entities, _client, _coord = await _build_selects(
            AHEAD_ATTRIBUTES, schema=_ahead_schema()
        )
        self.assertEqual(
            ["off", "soft", "mid", "h_biotics", "custom"], entities[0].options
        )

    async def test_an_unknown_parameter_creates_no_number(self) -> None:
        entities, _client, _coord = await _build_numbers(
            attributes=AHEAD_ATTRIBUTES, schema=_ahead_schema()
        )
        self.assertEqual(
            {"aroma_time_on", "aroma_time_off"},
            {e.entity_description.key for e in entities},
        )

    async def test_an_unknown_parameter_is_never_written(self) -> None:
        entities, client, _coord = await _build_switches(
            attributes=AHEAD_ATTRIBUTES, schema=_ahead_schema()
        )
        by_key = _switch_by_key(entities)
        await by_key["child_lock"].async_turn_on()
        self.assertEqual([("settings", {"lockStatus": "1"})], _sent(client))


# --- shared shadow-value spelling ---------------------------------------------
#
# `air_purifier.raw_text` is the single rule for turning a live value into the
# spelling the schema uses. The six AP read paths used to hand-roll a weaker
# `str(raw)`, which disagreed with `environment_available` on the SAME attribute.
#
# The fixtures below are deliberately built from real `HonAttribute` objects, not
# from plain strings like the rest of this module: the divergence is created by the
# ENGINE, whose `.value` routes through `str_to_float`, so a plain-string fixture
# cannot reproduce it. A cloud value spelled "1.0" reaches the platform as the
# float 1.0, and `str(1.0)` matches no schema value.


def _shadow(**overrides) -> dict:
    """FULL_ATTRIBUTES with `overrides` wrapped the way the client wraps them."""
    from custom_components.addhon.client.engine.attributes import HonAttribute

    return {
        **FULL_ATTRIBUTES,
        **{key: HonAttribute(value) for key, value in overrides.items()},
    }


class ShadowSpellingTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_decimal_power_state_still_reads_on(self) -> None:
        entities, _client, _coord = await _build_fan(_shadow(onOffStatus="1.0"))
        self.assertIs(True, entities[0].is_on)

    async def test_an_unwrapped_bool_power_state_still_reads_on(self) -> None:
        """`_get_attr` documents that some client versions hand back an already
        unwrapped raw value; a real bool then bypasses the engine's own folding."""
        entities, _client, _coord = await _build_fan(
            {**FULL_ATTRIBUTES, "onOffStatus": True}
        )
        self.assertIs(True, entities[0].is_on)

    async def test_a_decimal_mode_still_maps_to_its_preset(self) -> None:
        entities, _client, _coord = await _build_fan(_shadow(machMode="2.0"))
        self.assertEqual("auto", entities[0].preset_mode)

    async def test_a_decimal_mode_is_still_remembered_for_a_bare_turn_on(self) -> None:
        """The store only accepts a raw the live schema declares, so an
        unnormalized "4.0" is dropped and the next bare turn-on silently falls back
        to the default preset instead of restoring Max."""
        entities, client, _coord = await _build_fan(_shadow(machMode="4.0"))
        fan_entity = entities[0]
        fan_entity._handle_coordinator_update()
        await fan_entity.async_turn_off()
        await fan_entity.async_turn_on()
        # The schema-mandatory onOffStatus is added by the dispatcher, which this
        # recording client stands in for, so the intent carries only machMode.
        self.assertEqual(
            [("stopProgram", {}), ("startProgram", {"machMode": "4"})], _sent(client)
        )

    async def test_a_decimal_light_level_still_maps(self) -> None:
        entities, _client, _coord = await _build_panel_light(
            _shadow(lightStatus="0.0")
        )
        self.assertEqual("off", entities[0].current_option)

    async def test_a_decimal_toggle_still_reads_on(self) -> None:
        entities, _client, _coord = await _build_switches(
            _shadow(lockStatus="1.0", touchToneStatus="1.0"), schema=_toggle_schema()
        )
        by_key = _switch_by_key(entities)
        self.assertIs(True, by_key["child_lock"].is_on)
        self.assertIs(True, by_key["touch_tone"].is_on)

    async def test_a_decimal_eco_status_still_reads_engaged(self) -> None:
        entities = _by_key(await _build_binary(_shadow(ecoModeStatus="1.0")))
        self.assertIs(True, entities["eco_active"].is_on)

    async def test_a_decimal_aroma_mode_still_maps_to_its_option(self) -> None:
        entities, _client, _coord = await _build_selects(
            _shadow(aromaStatus="1.0"), schema=_aroma_schema()
        )
        self.assertEqual("soft", entities[0].current_option)

    async def test_a_decimal_custom_aroma_keeps_the_timings_usable(self) -> None:
        """The number refuses the write when Custom is not the reported mode, so an
        unnormalized "4.0" hides both timings and rejects the user's value right
        after they selected Custom."""
        entities, client, _coord = await _build_numbers(_shadow(aromaStatus="4.0"))
        self.assertEqual(2, len(entities))
        for entity in entities:
            self.assertTrue(entity.available, entity.entity_description.key)
        by_key = {e.entity_description.key: e for e in entities}
        await by_key["aroma_time_on"].async_set_native_value(120)
        # The intent restates the mode alongside the timing, in the SCHEMA spelling:
        # the "4.0" the device reported must never reach the wire.
        self.assertEqual(
            [("settings", {"aromaStatus": "4", "aromaTimeOn": "120"})], _sent(client)
        )

    def test_no_ap_read_path_stringifies_a_live_value_by_hand(self) -> None:
        """Structural, per MEMBER rather than per file.

        A whole-file substring scan cannot express this: three of the five platform
        modules also serve other appliance families, whose reads legitimately keep
        the older `str(raw)` platform convention. So each AP member is located by
        AST and checked on its own source, which a revert of any single site fails.
        """
        import ast
        import textwrap

        root = Path(__file__).parents[1] / "custom_components" / "addhon"
        sites = (
            ("fan.py", "HonAirPurifierFan", "_raw_mode"),
            ("fan.py", "HonAirPurifierFan", "is_on"),
            ("select.py", "HonAirPurifierPanelLightSelect", "current_option"),
            ("switch.py", "HonAirPurifierSwitch", "is_on"),
            ("select.py", "HonAirPurifierAromaSelect", "current_option"),
            ("number.py", "HonAirPurifierTimeNumber", "_custom_active"),
        )
        for module, class_name, member in sites:
            where = f"{module}:{class_name}.{member}"
            source = (root / module).read_text(encoding="utf-8")
            tree = ast.parse(source)
            classes = [
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == class_name
            ]
            self.assertEqual(1, len(classes), where)
            members = [
                node
                for node in classes[0].body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == member
            ]
            self.assertEqual(1, len(members), where)
            body = ast.get_source_segment(source, members[0]) or ""
            self.assertIn("raw_text(", body, where)
            # Any hand-rolled stringification of the live value, whatever it is
            # named: the read must not spell the canonicalization itself.
            for call in ast.walk(ast.parse(textwrap.dedent(body))):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "str"
                ):
                    self.fail(f"{where}: str() applied to a live value")

    def test_every_ap_binary_resolves_its_value_in_the_ap_module(self) -> None:
        """The AP binaries route through the SHARED platform comparison, so without
        a value_fn a purifier signal would be the one AP read left on the older
        convention."""
        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        for description in BINARY_SENSORS[APPLIANCE_AP]:
            self.assertIsNotNone(description.value_fn, description.key)
            self.assertEqual(
                "custom_components.addhon.air_purifier",
                description.value_fn.__module__,
                description.key,
            )


class RawTextRuleTest(unittest.TestCase):
    """The helper itself. Its callers only ever exercise the paths their own device
    values reach, which left two of its three branches unpinned."""

    def test_a_bool_renders_as_the_device_spelling(self) -> None:
        from custom_components.addhon.air_purifier import raw_text

        self.assertEqual("1", raw_text(True))
        self.assertEqual("0", raw_text(False))

    def test_an_integral_float_drops_its_decimal_tail(self) -> None:
        from custom_components.addhon.air_purifier import raw_text

        self.assertEqual("60", raw_text(60.0))
        self.assertEqual("0", raw_text(-0.0))

    def test_a_fractional_float_keeps_its_decimals(self) -> None:
        """A range parameter may legitimately be fractional; truncating it here would
        send a value the user did not ask for."""
        from custom_components.addhon.air_purifier import raw_text

        self.assertEqual("60.5", raw_text(60.5))

    def test_non_numeric_text_passes_through(self) -> None:
        from custom_components.addhon.air_purifier import raw_text

        self.assertEqual("E12", raw_text("E12"))
        self.assertEqual("2", raw_text("2"))

    def test_an_engaged_status_reads_through_the_same_rule(self) -> None:
        from custom_components.addhon.air_purifier import is_engaged

        self.assertIs(True, is_engaged(1.0))
        self.assertIs(True, is_engaged(True))
        self.assertIs(True, is_engaged("1"))
        self.assertIs(False, is_engaged("0"))
        self.assertIs(False, is_engaged("2"))


class StoppedPurifierWriteTest(unittest.IsolatedAsyncioTestCase):
    """`available` hides the aroma select and the two timing numbers while the
    purifier is stopped, but a hidden entity still receives service calls, and the
    snapshot it reads can be a refresh behind the device. Both write paths refuse.

    The availability half is already pinned by the per-platform tests above, so this
    class only covers the write path and keeps one happy-path control per platform to
    prove the guard is not refusing everything.
    """

    STOPPED = {**FULL_ATTRIBUTES, "onOffStatus": "0"}
    # Power NOT REPORTED, which environment_available deliberately treats as "not
    # confirmed on" rather than as running. The doctrine is stated in
    # air_purifier.environment_available and pinned for the read side by
    # AirPurifierAvailabilityTest; the write side must not be more trusting than the
    # read side on the same attribute.
    UNREPORTED = {k: v for k, v in FULL_ATTRIBUTES.items() if k != "onOffStatus"}

    async def test_the_aroma_select_refuses_a_write_while_stopped(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        entities, client, coordinator = await _build_selects(
            self.STOPPED, schema=_aroma_schema()
        )
        with self.assertRaises(HomeAssistantError) as caught:
            await entities[0].async_select_option("soft")
        self.assertEqual("purifier_not_running", caught.exception.translation_key)
        self.assertEqual([], _sent(client))
        self.assertEqual(0, coordinator.refreshes)

    async def test_the_aroma_select_refuses_a_write_on_unreported_power(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        entities, client, _coord = await _build_selects(
            self.UNREPORTED, schema=_aroma_schema()
        )
        with self.assertRaises(HomeAssistantError) as caught:
            await entities[0].async_select_option("soft")
        self.assertEqual("purifier_not_running", caught.exception.translation_key)
        self.assertEqual([], _sent(client))

    async def test_no_option_can_implicitly_start_the_purifier(self) -> None:
        """The refusal exists for this. The aroma command carries no power field, so a
        write while stopped is either dropped by the device or starts it as a side
        effect of a mode change. Covers EVERY offered option, including "off", which
        is refused too: the rule is about when a patch may be sent, not about which
        value it carries."""
        from homeassistant.exceptions import HomeAssistantError

        entities, client, _coord = await _build_selects(
            self.STOPPED, schema=_aroma_schema()
        )
        options = entities[0].options
        self.assertEqual(["off", "soft", "mid", "h_biotics", "custom"], options)
        for option in options:
            with self.assertRaises(HomeAssistantError):
                await entities[0].async_select_option(option)
        self.assertEqual([], _sent(client))

    async def test_the_timing_numbers_refuse_a_write_while_stopped(self) -> None:
        """Custom is a SETTING the device can retain while stopped, so the
        pre-existing Custom check alone does not cover this."""
        from homeassistant.exceptions import HomeAssistantError

        entities, client, coordinator = await _build_numbers(
            {**self.STOPPED, "aromaStatus": "4"}
        )
        by_key = {e.entity_description.key: e for e in entities}
        self.assertEqual({"aroma_time_on", "aroma_time_off"}, set(by_key))
        for key in sorted(by_key):
            with self.assertRaises(HomeAssistantError) as caught:
                await by_key[key].async_set_native_value(30)
            self.assertEqual(
                "purifier_not_running", caught.exception.translation_key, key
            )
        self.assertEqual([], _sent(client))
        self.assertEqual(0, coordinator.refreshes)

    async def test_power_is_reported_before_the_custom_mode(self) -> None:
        """A stopped purifier outside Custom fails both checks. Naming the power one
        is the actionable message, since turning Custom on would not help."""
        from homeassistant.exceptions import HomeAssistantError

        entities, _client, _coord = await _build_numbers(
            {**self.STOPPED, "aromaStatus": "1"}
        )
        by_key = {e.entity_description.key: e for e in entities}
        with self.assertRaises(HomeAssistantError) as caught:
            await by_key["aroma_time_on"].async_set_native_value(30)
        self.assertEqual("purifier_not_running", caught.exception.translation_key)

    async def test_a_running_purifier_still_writes(self) -> None:
        """The control: a guard that refused everything would leave every assertion
        above green."""
        entities, client, coordinator = await _build_selects(schema=_aroma_schema())
        await entities[0].async_select_option("soft")
        self.assertEqual([("settings", {"aromaStatus": "1"})], _sent(client))
        self.assertEqual(1, coordinator.refreshes)

        entities, client, coordinator = await _build_numbers(CUSTOM_ACTIVE)
        by_key = {e.entity_description.key: e for e in entities}
        await by_key["aroma_time_off"].async_set_native_value(90)
        self.assertEqual(
            [("settings", {"aromaStatus": "4", "aromaTimeOff": "90"})], _sent(client)
        )
        self.assertEqual(1, coordinator.refreshes)


class SharedAttributeNamingTest(unittest.TestCase):
    """Two purifier entities reading the SAME live attribute must name the same thing.

    The Italian carbon-monoxide binary read "Indicazione Monossido" while the sensor
    on the very same `coLevel` read "Monossido di Carbonio", so the pair looked like
    two different substances on one dashboard. English never diverged, because both
    labels were built from "Carbon monoxide".

    The pairs are DERIVED from the description tables rather than listed here: a new
    entity that reuses an existing attribute is covered the day it lands, which is
    exactly how this one slipped in.
    """

    #: Filler that carries no referent. "Indicazione X" vs "X" must still match on X.
    _NOISE = frozenset(
        {
            "indicazione",
            "giudizio",
            "livello",
            "indication",
            "rating",
            "level",
            "sperimentale",
            "experimental",
            "non",
            "not",
            "certified",
            "certificato",
            "rilevatore",
            "detector",
            "and",
            "the",
            "del",
            "della",
            "dell",
            "di",
            "in",
            "of",
        }
    )

    @staticmethod
    def _enum_device_class():
        from custom_components.addhon.sensor import SensorDeviceClass

        return SensorDeviceClass.ENUM

    @staticmethod
    def _translations() -> dict:
        root = Path(__file__).parents[1] / "custom_components" / "addhon"
        return {
            lang: json.loads(
                (root / "translations" / f"{lang}.json").read_text(encoding="utf-8")
            )
            for lang in ("en", "it")
        }

    @staticmethod
    def _ap_descriptions() -> list[tuple[str, object]]:
        from custom_components.addhon.binary_sensor import BINARY_SENSORS
        from custom_components.addhon.sensor import SENSORS

        return [("sensor", d) for d in SENSORS[APPLIANCE_AP]] + [
            ("binary_sensor", d) for d in BINARY_SENSORS[APPLIANCE_AP]
        ]

    def _words(self, name: str) -> set[str]:
        return {
            word
            for word in re.findall(r"[^\W\d_]+", name.lower(), re.UNICODE)
            if len(word) > 2 and word not in self._NOISE
        }

    @staticmethod
    def _pairs() -> dict:
        """{attribute: [(platform, key), ...]} for attributes read by more than one
        AP entity, minus the ones whose label is dictated by a device class.

        `errors` backs both the raw code sensor and the PROBLEM binary; the latter
        takes its name from the Home Assistant device class ("Problem" / "Guasto"),
        which is the platform's vocabulary and not this feature's to align. The
        carbon-monoxide binary is deliberately NOT exempt: it carries no device
        class, precisely because SAFETY would present it as a certified detector.

        ENUM is not an exemption. It declares the VALUE type, not a name, so
        `air_quality_label` still has to name the same thing as `air_quality`.
        """
        by_attribute: dict = {}
        exempt = set()
        for platform, description in SharedAttributeNamingTest._ap_descriptions():
            by_attribute.setdefault(description.attr_key, []).append(
                (platform, description.key)
            )
            device_class = getattr(description, "device_class", None)
            if device_class is not None and str(device_class) != str(
                SharedAttributeNamingTest._enum_device_class()
            ):
                exempt.add(description.attr_key)
        return {
            attribute: entities
            for attribute, entities in by_attribute.items()
            if len(entities) > 1 and attribute not in exempt
        }

    def test_the_sweep_finds_the_pairs_it_is_meant_to(self) -> None:
        """Anti-vacuity, and it pins the exemption instead of leaving it implicit: an
        empty sweep would otherwise read the same as a clean one, and a future entity
        that carries a device class would drop its whole pair out unnoticed."""
        self.assertEqual({"coLevel", "airQuality"}, set(self._pairs()))
        by_attribute: dict = {}
        for _platform, description in self._ap_descriptions():
            by_attribute.setdefault(description.attr_key, []).append(description.key)
        shared = {
            attribute
            for attribute, keys in by_attribute.items()
            if len(keys) > 1
        }
        self.assertEqual({"errors"}, shared - set(self._pairs()), "exempted pairs")
        self.assertEqual(
            ["errors", "problem"], sorted(by_attribute["errors"]), "exempted pair"
        )

    def test_entities_on_one_attribute_name_their_referent_identically(self) -> None:
        """Strip the qualifiers and what is left must MATCH, not merely overlap.

        Overlap is the check this started as, and it was worthless here: the bug
        being fixed was "Indicazione Monossido" against "Monossido di Carbonio",
        which overlaps on "monossido" and passes. A truncated referent is exactly the
        shape that reads as two different things on a dashboard, so nothing short of
        equality catches it.

        That makes `_NOISE` load-bearing, and the tempting way to silence a failure
        is to add the missing noun to it. The referent sizes are therefore compared
        ACROSS languages: dropping "carbonio" into `_NOISE` to hide the Italian pair
        leaves it naming the substance with one word where English uses two, and that
        fails here instead.

        What this cannot judge is whether the shared name is the RIGHT one. Two
        labels that are identically wrong pass, by construction: this is a
        consistency check between siblings, and correctness of the wording is the
        reviewer's.
        """
        translations = self._translations()
        sizes: dict[str, dict[str, int]] = {}
        for attribute, entities in sorted(self._pairs().items()):
            for lang, data in translations.items():
                names = {
                    f"{platform}.{key}": data["entity"][platform][key]["name"]
                    for platform, key in entities
                }
                referents = {name: self._words(name) for name in names.values()}
                distinct = {frozenset(words) for words in referents.values()}
                self.assertEqual(
                    1,
                    len(distinct),
                    f"{lang}: {attribute} labels name different things: {referents}",
                )
                referent = distinct.pop()
                self.assertTrue(referent, f"{lang}: {attribute} has no referent")
                sizes.setdefault(attribute, {})[lang] = len(referent)
        for attribute, per_language in sorted(sizes.items()):
            self.assertEqual(
                1,
                len(set(per_language.values())),
                f"{attribute} is named with a different number of content words per "
                f"language, which is what stripping one into _NOISE looks like: "
                f"{per_language}",
            )


class RefusedTransactionTest(unittest.IsolatedAsyncioTestCase):
    """A refusal the dispatcher reports by RETURN must still reach the user.

    `CommandDispatcher.dispatch` has three outcomes: it raises, it returns True, or it
    returns False having rolled the transaction back. The adapter discarded that
    False, so every AP entity went on to refresh and report success on a write the
    appliance never accepted. It is unreachable through the current stack, where the
    api answers with a literal True and HonCommand raises ApiError on anything falsy,
    which is the reason to handle it rather than trust it.
    """

    async def test_every_ap_write_path_surfaces_a_refusal(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        async def fan(client):
            entities, _c, coord = await _build_fan(client=client)
            return entities[0].async_turn_on(), coord

        async def light(client):
            entities, _c, coord = await _build_panel_light(client=client)
            return entities[0].async_select_option("high"), coord

        async def switch(client):
            entities, _c, coord = await _build_switches(
                schema=_toggle_schema(), client=client
            )
            return _switch_by_key(entities)["child_lock"].async_turn_on(), coord

        async def select(client):
            entities, _c, coord = await _build_selects(
                schema=_aroma_schema(), client=client
            )
            return entities[0].async_select_option("soft"), coord

        async def number(client):
            entities, _c, coord = await _build_numbers(CUSTOM_ACTIVE, client=client)
            by_key = {e.entity_description.key: e for e in entities}
            return by_key["aroma_time_on"].async_set_native_value(30), coord

        for build in (fan, light, switch, select, number):
            with self.subTest(platform=build.__name__):
                client = RecordingClient(answer=False)
                coroutine, coordinator = await build(client)
                with self.assertRaises(HomeAssistantError) as caught:
                    await coroutine
                self.assertEqual(
                    "command_rejected", caught.exception.translation_key
                )
                # The refusal must also stop the optimistic refresh that would have
                # redrawn the entity as though the write had landed.
                self.assertEqual(0, coordinator.refreshes)

    async def test_the_reachable_refusal_is_localized_too(self) -> None:
        """A real hOn refusal arrives as ApiError, not as False, and used to reach the
        user as the generic failure wrapping the English literal "Can't send
        command"."""
        from custom_components.addhon.client.engine.exceptions import ApiError
        from homeassistant.exceptions import HomeAssistantError

        entities, _client, coordinator = await _build_fan(
            client=RecordingClient(fail=ApiError("Can't send command"))
        )
        with self.assertRaises(HomeAssistantError) as caught:
            await entities[0].async_turn_on()
        self.assertEqual("command_rejected", caught.exception.translation_key)
        self.assertNotIn(
            "Can't send command", str(caught.exception.translation_placeholders or {})
        )
        self.assertEqual(0, coordinator.refreshes)

    async def test_any_other_transport_failure_stays_generic(self) -> None:
        """Only a refusal gets the refusal message: a timeout is a different fault and
        the user needs to be able to tell them apart."""
        from homeassistant.exceptions import HomeAssistantError

        entities, _client, _coord = await _build_fan(
            client=RecordingClient(fail=TimeoutError("cloud down"))
        )
        with self.assertRaises(HomeAssistantError) as caught:
            await entities[0].async_turn_on()
        self.assertEqual("command_error", caught.exception.translation_key)

    async def test_a_plain_success_is_unaffected(self) -> None:
        """The control: a guard that refused everything would leave the assertions
        above green. True is what the real client reports, and every other AP write
        test in this module now runs through the same answer."""
        entities, client, coordinator = await _build_fan(
            client=RecordingClient(answer=True)
        )
        await entities[0].async_turn_on()
        self.assertEqual(1, len(client.patches))
        self.assertEqual(1, coordinator.refreshes)


class PublicSurfaceTest(unittest.TestCase):
    """`air_purifier.__all__` is the module's declared contract, so it has to match.

    It did not. `AP_CUSTOM_AROMA` was imported by both the aroma select and the
    timing numbers while absent from the list, and the list itself had drifted out of
    alphabetical order as each task appended to the end.
    """

    ROOT = Path(__file__).parents[1] / "custom_components" / "addhon"

    def _declared(self) -> list[str]:
        import re

        source = (self.ROOT / "air_purifier.py").read_text(encoding="utf-8")
        start = source.index("__all__ = [")
        return re.findall(r'"([^"]+)"', source[start : source.index("]", start)])

    def test_every_name_another_module_imports_is_declared(self) -> None:
        """The direction that matters: importing a name the module does not export is
        relying on a surface it never promised. The reverse is allowed, since a
        constant may exist to state a rule (AP_WRITABLE_MODES) rather than to be
        imported."""
        import ast

        declared = set(self._declared())
        imported: set[str] = set()
        for path in sorted(self.ROOT.rglob("*.py")):
            if path.name == "air_purifier.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and node.module.endswith("air_purifier")
                ):
                    imported |= {alias.name for alias in node.names}
        self.assertGreaterEqual(len(imported), 10, imported)
        self.assertEqual(set(), imported - declared)

    def test_the_declaration_stays_sorted(self) -> None:
        declared = self._declared()
        self.assertEqual(sorted(declared), declared)
        self.assertEqual(len(set(declared)), len(declared))


if __name__ == "__main__":
    unittest.main()
