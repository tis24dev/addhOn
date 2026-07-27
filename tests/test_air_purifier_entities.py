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
}


class FakeCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.last_update_success = True


class FakeHass:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id


async def _build_sensors(attributes: dict) -> list:
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
    await sensor.async_setup_entry(hass, FakeEntry(), added.extend)
    return [e for e in added if not getattr(e, "_addhon_account", False)]


def _by_key(entities: list) -> dict:
    return {e.entity_description.key: e for e in entities}


class AirPurifierSensorTableTest(unittest.TestCase):
    def test_table_covers_every_documented_measurement(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        self.assertEqual(
            [d.key for d in SENSORS[APPLIANCE_AP]],
            [
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
            ],
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


if __name__ == "__main__":
    unittest.main()


# --- Task 4: standard binary sensors -----------------------------------------


async def _build_binary(attributes: dict) -> list:
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
    await binary_sensor.async_setup_entry(hass, FakeEntry(), added.extend)
    return [e for e in added if not getattr(e, "_addhon_account", False)]


class AirPurifierBinaryTableTest(unittest.TestCase):
    def test_table_is_the_two_standard_signals(self) -> None:
        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        self.assertEqual(
            [d.key for d in BINARY_SENSORS[APPLIANCE_AP]],
            ["eco_active", "problem"],
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

    def test_eco_reads_its_own_attribute_as_a_plain_toggle(self) -> None:
        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        table = {d.key: d for d in BINARY_SENSORS[APPLIANCE_AP]}
        eco = table["eco_active"]
        self.assertEqual("ecoModeStatus", eco.attr_key)
        self.assertEqual("1", eco.on_value)
        self.assertIsNone(eco.value_fn)

    def test_the_value_fn_field_defaults_off_everywhere_else(self) -> None:
        """New optional field: no existing binary sensor changes behavior."""
        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        for app_type, descriptions in BINARY_SENSORS.items():
            if app_type == APPLIANCE_AP:
                continue
            for description in descriptions:
                self.assertIsNone(description.value_fn, description.key)


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
