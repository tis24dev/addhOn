"""Tests for the Tier 2 read-only appliance types (capability-gated).

Tier 2 adds sensor + binary_sensor support for the appliance types that are
mapped but not validated on real devices: fridge/freezer (REF/FR/FRE), oven
(OV), dishwasher (DW), wine cellar (WC), hob (IH/HOB), hood (HO), coffee/kettle
(KT), water heater (WH) and robot vacuum (RVC). Every Tier 2 description is
CAPABILITY-GATED: the entity is created only when the device actually exposes
its attr_key. Historic types (AC/WM/WD/TD) stay ungated.

Stdlib unittest with inline Home Assistant stubs (real frozen kw_only dataclass
descriptions so the Hon* subclasses work). No real Home Assistant install
required. Stubs use getattr-guards so they coexist with the other test modules'
stubs in a shared pytest process.
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


def _install_homeassistant_stubs() -> None:
    ha = _mod("homeassistant")

    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(config_entries, "ConfigEntry", type("ConfigEntry", (), {}))

    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))

    exceptions = _mod("homeassistant.exceptions")
    base_err = getattr(exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exceptions.HomeAssistantError = base_err
    exceptions.ConfigEntryNotReady = getattr(exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {}))
    exceptions.ConfigEntryAuthFailed = getattr(exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {}))

    helpers = _mod("homeassistant.helpers")
    entity = _mod("homeassistant.helpers.entity")
    entity.DeviceInfo = getattr(entity, "DeviceInfo", dict)
    entity_platform = _mod("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = getattr(entity_platform, "AddEntitiesCallback", object)

    update_coordinator = _mod("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
            self.hass = getattr(coordinator, "hass", None)

        @property
        def available(self) -> bool:
            return getattr(self.coordinator, "last_update_success", True)

        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

    update_coordinator.CoordinatorEntity = getattr(update_coordinator, "CoordinatorEntity", CoordinatorEntity)
    update_coordinator.DataUpdateCoordinator = getattr(update_coordinator, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    update_coordinator.UpdateFailed = getattr(update_coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

    components = _mod("homeassistant.components")

    # ── sensor platform stub ──────────────────────────────────────────────
    sensor_mod = _mod("homeassistant.components.sensor")

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
        CO2 = "carbon_dioxide"
        BATTERY = "battery"
        POWER = "power"
        ENUM = "enum"

    class SensorStateClass:
        MEASUREMENT = "measurement"
        TOTAL = "total"
        TOTAL_INCREASING = "total_increasing"

    sensor_mod.SensorEntityDescription = getattr(sensor_mod, "SensorEntityDescription", SensorEntityDescription)
    sensor_mod.SensorEntity = getattr(sensor_mod, "SensorEntity", SensorEntity)
    sensor_mod.SensorDeviceClass = getattr(sensor_mod, "SensorDeviceClass", SensorDeviceClass)
    sensor_mod.SensorStateClass = getattr(sensor_mod, "SensorStateClass", SensorStateClass)

    # ── binary_sensor platform stub ───────────────────────────────────────
    binary_mod = _mod("homeassistant.components.binary_sensor")

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

    binary_mod.BinarySensorEntityDescription = getattr(binary_mod, "BinarySensorEntityDescription", BinarySensorEntityDescription)
    binary_mod.BinarySensorEntity = getattr(binary_mod, "BinarySensorEntity", BinarySensorEntity)
    binary_mod.BinarySensorDeviceClass = getattr(binary_mod, "BinarySensorDeviceClass", BinarySensorDeviceClass)

    const = _mod("homeassistant.const")

    class UnitOfEnergy:
        KILO_WATT_HOUR = "kWh"

    class UnitOfVolume:
        LITERS = "L"

    class UnitOfTime:
        MINUTES = "min"

    class UnitOfTemperature:
        CELSIUS = "°C"

    const.UnitOfEnergy = getattr(const, "UnitOfEnergy", UnitOfEnergy)
    const.UnitOfVolume = getattr(const, "UnitOfVolume", UnitOfVolume)
    const.UnitOfTime = getattr(const, "UnitOfTime", UnitOfTime)
    const.UnitOfTemperature = getattr(const, "UnitOfTemperature", UnitOfTemperature)

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    ha.components = components
    helpers.entity = entity
    helpers.entity_platform = entity_platform
    helpers.update_coordinator = update_coordinator
    components.sensor = sensor_mod
    components.binary_sensor = binary_mod


_install_homeassistant_stubs()


class FakeCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.hass = None
        self.last_update_success = True


class FakeHass:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id


def _sensor_keys(app_type: str) -> list[str]:
    from custom_components.addhon.sensor import SENSORS

    return [d.key for d in SENSORS.get(app_type, ())]


def _binary_keys(app_type: str) -> list[str]:
    from custom_components.addhon.binary_sensor import BINARY_SENSORS

    return [d.key for d in BINARY_SENSORS.get(app_type, ())]


async def _build_sensors(app_type: str, attributes: dict) -> list:
    from custom_components.addhon import sensor
    from custom_components.addhon.const import DOMAIN

    data = {"x-1": {"type": app_type, "name": "Dev", "attributes": attributes, "settings": {}}}
    coordinator = FakeCoordinator(data)
    hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
    added: list = []
    await sensor.async_setup_entry(hass, FakeEntry(), added.extend)
    return added


async def _build_binary(app_type: str, attributes: dict) -> list:
    from custom_components.addhon import binary_sensor
    from custom_components.addhon.const import DOMAIN

    data = {"x-1": {"type": app_type, "name": "Dev", "attributes": attributes, "settings": {}}}
    coordinator = FakeCoordinator(data)
    hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
    added: list = []
    await binary_sensor.async_setup_entry(hass, FakeEntry(), added.extend)
    return added


class Tier2TableTest(unittest.TestCase):
    def test_water_heater_full_keys(self) -> None:
        self.assertEqual(
            _sensor_keys("WH"),
            ["water_temp", "temp_inlet", "temp_outlet", "power", "water_volume",
             "heating_remaining", "program_phase"],
        )

    def test_vacuum_full_keys(self) -> None:
        self.assertEqual(
            _sensor_keys("RVC"),
            ["battery", "state", "remaining_time", "power_mode", "last_work_area",
             "total_work_area", "errors"],
        )

    def test_dishwasher_full_keys(self) -> None:
        self.assertEqual(
            _sensor_keys("DW"),
            ["state", "program_name", "remaining_time", "salt_level",
             "rinse_aid_level", "wash_temperature", "errors"],
        )

    def test_fr_and_fre_alias_cooling(self) -> None:
        self.assertEqual(_sensor_keys("REF"), _sensor_keys("FR"))
        self.assertEqual(_sensor_keys("REF"), _sensor_keys("FRE"))

    def test_hob_alias(self) -> None:
        self.assertEqual(_sensor_keys("IH"), _sensor_keys("HOB"))
        self.assertEqual(_binary_keys("IH"), _binary_keys("HOB"))

    def test_all_tier2_descriptions_are_gated(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        for app_type in ("REF", "FR", "FRE", "OV", "DW", "WC", "IH", "HOB", "HO", "KT", "WH", "RVC"):
            for d in SENSORS[app_type]:
                self.assertTrue(d.gated, f"{app_type}/{d.key} must be gated")

    def test_historic_types_not_gated(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        for app_type in ("AC", "WM", "WD", "TD"):
            for d in SENSORS[app_type]:
                self.assertFalse(d.gated, f"{app_type}/{d.key} must NOT be gated")

    def test_hob_binary_has_six_pan_zones(self) -> None:
        self.assertEqual(
            _binary_keys("IH"),
            [f"pan_zone{z}" for z in range(1, 7)],
        )


class Tier2GatingTest(unittest.IsolatedAsyncioTestCase):
    async def test_cooling_creates_only_reported_attrs(self) -> None:
        added = await _build_sensors("REF", {"tempZ1": "4", "tempEnv": "22", "humidityEnv": "55"})
        self.assertEqual(
            {e._attr_unique_id for e in added},
            {"x-1_temp_zone1", "x-1_temp_ambient", "x-1_humidity_ambient"},
        )

    async def test_no_attrs_means_no_entities(self) -> None:
        added = await _build_sensors("REF", {})
        self.assertEqual(added, [])

    async def test_unknown_type_no_entities(self) -> None:
        added = await _build_sensors("ZZ", {"tempZ1": "4"})
        self.assertEqual(added, [])

    async def test_oven_state_decodes_machine_mode(self) -> None:
        added = await _build_sensors("OV", {"machMode": "2"})
        state = next(e for e in added if e._attr_unique_id == "x-1_state")
        self.assertEqual(state.native_value, "running")

    async def test_dishwasher_salt_level_decodes(self) -> None:
        added = await _build_sensors("DW", {"saltStatus": "1", "rinseAidStatus": "0"})
        salt = next(e for e in added if e._attr_unique_id == "x-1_salt_level")
        rinse = next(e for e in added if e._attr_unique_id == "x-1_rinse_aid_level")
        self.assertEqual(salt.native_value, "low")
        self.assertEqual(rinse.native_value, "ok")

    async def test_vacuum_state_power_battery(self) -> None:
        added = await _build_sensors("RVC", {"prPhase": "6", "power": "1", "batteryStatus": "80"})
        by_id = {e._attr_unique_id: e for e in added}
        self.assertEqual(by_id["x-1_state"].native_value, "charging")
        self.assertEqual(by_id["x-1_power_mode"].native_value, "turbo")
        self.assertEqual(by_id["x-1_battery"].native_value, 80.0)

    async def test_unknown_enum_value_is_none(self) -> None:
        # ENUM sensors must not emit out-of-options values: an unknown code -> None
        # (the sensor reports "unknown" rather than a raw label).
        added = await _build_sensors("RVC", {"prPhase": "99"})
        state = next(e for e in added if e._attr_unique_id == "x-1_state")
        self.assertIsNone(state.native_value)


class Tier2BinaryGatingTest(unittest.IsolatedAsyncioTestCase):
    async def test_cooling_binary_gating(self) -> None:
        added = await _build_binary("REF", {"doorStatusZ1": "1", "icemakerOnOffStatus": "0"})
        self.assertEqual(
            {e._attr_unique_id for e in added},
            {"x-1_door_zone1", "x-1_ice_maker", "x-1_connectivity"},
        )
        door = next(e for e in added if e._attr_unique_id == "x-1_door_zone1")
        self.assertTrue(door.is_on)

    async def test_hob_binary_only_present_zones(self) -> None:
        added = await _build_binary("IH", {"panStatusZ1": "1", "panStatusZ3": "0"})
        self.assertEqual(
            {e._attr_unique_id for e in added},
            {"x-1_pan_zone1", "x-1_pan_zone3", "x-1_connectivity"},
        )

    async def test_water_heater_binary(self) -> None:
        added = await _build_binary("WH", {"lockStatus": "1"})
        self.assertEqual({e._attr_unique_id for e in added}, {"x-1_child_lock", "x-1_connectivity"})
        lock = next(e for e in added if e._attr_unique_id == "x-1_child_lock")
        self.assertTrue(lock.is_on)


class ConnectivityBinaryTest(unittest.IsolatedAsyncioTestCase):
    async def _conn(self, attributes):
        added = await _build_binary("AC", attributes)  # AC: no per-type set
        return next(e for e in added if e._attr_unique_id == "x-1_connectivity")

    async def test_created_for_type_without_pertype_set(self) -> None:
        added = await _build_binary("AC", {"available": True})
        self.assertEqual({e._attr_unique_id for e in added}, {"x-1_connectivity"})

    async def test_is_on_reflects_available(self) -> None:
        self.assertTrue((await self._conn({"available": True})).is_on)
        self.assertFalse((await self._conn({"available": False})).is_on)
        self.assertIsNone((await self._conn({})).is_on)

    async def test_stays_available_when_device_disconnected(self) -> None:
        # the connectivity sensor must stay AVAILABLE so it can report 'disconnected'
        conn = await self._conn({"available": False})
        self.assertTrue(conn.available)
        self.assertFalse(conn.is_on)


if __name__ == "__main__":
    unittest.main()
