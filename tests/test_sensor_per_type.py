"""Tests for the per-appliance-type sensor refactor.

Covers the per-type SENSORS description table (AC/WM/WD/TD). TD gets
state/remaining/program/phase/dry_level/loading/delay/errors plus cycles (from
programsCounter, still no water/energy); WM/WD add program/phase/spin/temp/soil/
load/delay/errors on top of the consumption set (WD also dry_level); AC adds the
air-quality sensors (PM2.5/CO2/CH2O). Also covers the legacy registry cleanup of
the washer-only sensors that used to land on dryers.

Stdlib unittest with inline Home Assistant stubs (incl. a real dataclass
SensorEntityDescription so HonSensorEntityDescription can subclass it).
No real Home Assistant install required.
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

    entity_registry = _mod("homeassistant.helpers.entity_registry")
    entity_registry.async_get = getattr(entity_registry, "async_get", lambda hass: None)
    entity_registry.async_entries_for_config_entry = getattr(
        entity_registry, "async_entries_for_config_entry", lambda registry, entry_id: []
    )

    # homeassistant.components.sensor: SensorEntityDescription must be a frozen
    # kw_only dataclass so HonSensorEntityDescription can extend it.
    components = _mod("homeassistant.components")
    sensor_mod = _mod("homeassistant.components.sensor")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:
        key: str
        name: str | None = None
        icon: str | None = None
        native_unit_of_measurement: str | None = None
        device_class: object | None = None
        state_class: object | None = None

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

    class SensorStateClass:
        MEASUREMENT = "measurement"
        TOTAL = "total"
        TOTAL_INCREASING = "total_increasing"

    sensor_mod.SensorEntityDescription = getattr(sensor_mod, "SensorEntityDescription", SensorEntityDescription)
    sensor_mod.SensorEntity = getattr(sensor_mod, "SensorEntity", SensorEntity)
    sensor_mod.SensorDeviceClass = getattr(sensor_mod, "SensorDeviceClass", SensorDeviceClass)
    sensor_mod.SensorStateClass = getattr(sensor_mod, "SensorStateClass", SensorStateClass)

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
    helpers.entity_registry = entity_registry
    components.sensor = sensor_mod


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


class PerTypeTableTest(unittest.TestCase):
    def _keys(self, app_type: str) -> list[str]:
        from custom_components.haier_hon.sensor import SENSORS

        return [d.key for d in SENSORS.get(app_type, ())]

    def test_ac_keys(self) -> None:
        self.assertEqual(
            self._keys("AC"),
            ["temp_indoor", "temp_outdoor", "humidity_indoor", "compressor_freq",
             "total_energy", "pm25", "co2", "ch2o"],
        )

    def test_wm_keys(self) -> None:
        self.assertEqual(
            self._keys("WM"),
            ["state", "remaining_time", "program_name", "program_phase", "spin_speed",
             "wash_temperature", "dirty_level", "loading_percentage", "delay_time",
             "errors", "total_washes", "total_water", "total_energy", "current_energy",
             "current_water"],
        )

    def test_wd_is_wm_plus_dry_level(self) -> None:
        self.assertEqual(
            self._keys("WD"),
            ["state", "remaining_time", "program_name", "program_phase", "spin_speed",
             "wash_temperature", "dirty_level", "dry_level", "loading_percentage",
             "delay_time", "errors", "total_washes", "total_water", "total_energy",
             "current_energy", "current_water"],
        )

    def test_td_keys(self) -> None:
        self.assertEqual(
            self._keys("TD"),
            ["state", "remaining_time", "program_name", "program_phase", "dry_level",
             "loading_percentage", "delay_time", "errors", "total_washes"],
        )

    def test_td_has_no_water_or_energy(self) -> None:
        keys = set(self._keys("TD"))
        self.assertNotIn("total_water", keys)
        self.assertNotIn("current_water", keys)
        self.assertNotIn("total_energy", keys)
        self.assertNotIn("current_energy", keys)

    def test_td_cycles_read_programscounter(self) -> None:
        from custom_components.haier_hon.sensor import SENSORS

        td = {d.key: d for d in SENSORS["TD"]}
        self.assertEqual(td["total_washes"].attr_key, "programsCounter")

    def test_wm_cycles_read_totalwashcycle(self) -> None:
        from custom_components.haier_hon.sensor import SENSORS

        wm = {d.key: d for d in SENSORS["WM"]}
        self.assertEqual(wm["total_washes"].attr_key, "totalWashCycle")


class SensorBuildTest(unittest.IsolatedAsyncioTestCase):
    async def test_td_builds_only_expected_entities(self) -> None:
        from custom_components.haier_hon import sensor
        from custom_components.haier_hon.const import DOMAIN

        data = {
            "td-1": {
                "type": "TD",
                "name": "Dryer",
                "attributes": {"machMode": "1", "remainingTimeMM": 220, "programsCounter": 42},
                "settings": {},
            }
        }
        coordinator = FakeCoordinator(data)
        hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
        added: list = []

        await sensor.async_setup_entry(hass, FakeEntry(), added.extend)

        self.assertEqual(
            {e._attr_unique_id for e in added},
            {"td-1_state", "td-1_remaining_time", "td-1_program_name",
             "td-1_program_phase", "td-1_dry_level", "td-1_loading_percentage",
             "td-1_delay_time", "td-1_errors", "td-1_total_washes"},
        )
        cycles = next(e for e in added if e._attr_unique_id == "td-1_total_washes")
        self.assertEqual(cycles.native_value, 42.0)  # reads programsCounter
        state = next(e for e in added if e._attr_unique_id == "td-1_state")
        self.assertEqual(state.native_value, "In esecuzione")  # WM_STATE_MAP["1"]

    async def test_td_cycles_can_read_programscounter_from_statistics(self) -> None:
        from custom_components.haier_hon import sensor
        from custom_components.haier_hon.const import DOMAIN

        data = {
            "td-1": {
                "type": "TD",
                "name": "Dryer",
                "attributes": {"machMode": "1", "remainingTimeMM": 220},
                "statistics": {"programsCounter": 27},
                "settings": {},
            }
        }
        coordinator = FakeCoordinator(data)
        hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
        added: list = []

        await sensor.async_setup_entry(hass, FakeEntry(), added.extend)

        cycles = next(e for e in added if e._attr_unique_id == "td-1_total_washes")
        self.assertEqual(cycles.native_value, 27.0)

    async def test_wm_keeps_water_and_energy(self) -> None:
        from custom_components.haier_hon import sensor
        from custom_components.haier_hon.const import DOMAIN

        data = {
            "wm-1": {
                "type": "WM",
                "name": "Washer",
                "attributes": {"totalWaterUsed": 67751, "totalElectricityUsed": 687.44},
                "settings": {},
            }
        }
        coordinator = FakeCoordinator(data)
        hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator}}})
        added: list = []

        await sensor.async_setup_entry(hass, FakeEntry(), added.extend)

        uids = {e._attr_unique_id for e in added}
        self.assertIn("wm-1_total_water", uids)
        self.assertIn("wm-1_current_water", uids)
        water = next(e for e in added if e._attr_unique_id == "wm-1_total_water")
        self.assertEqual(water.native_value, 67751.0)


class TdLegacyCleanupTest(unittest.TestCase):
    def test_removes_only_td_washeronly_sensors(self) -> None:
        from homeassistant.helpers import entity_registry as er
        from custom_components.haier_hon import _remove_legacy_entities
        from custom_components.haier_hon.const import DOMAIN

        class RegEntry:
            def __init__(self, entity_id, unique_id):
                self.entity_id = entity_id
                self.unique_id = unique_id

        class FakeRegistry:
            def __init__(self, entries):
                self._entries = list(entries)
                self.removed: list = []

            def async_remove(self, entity_id):
                self.removed.append(entity_id)

        registry = FakeRegistry([
            RegEntry("sensor.dryer_total_water", "td-1_total_water"),       # remove
            RegEntry("sensor.dryer_total_energy", "td-1_total_energy"),     # remove
            RegEntry("sensor.dryer_current_energy", "td-1_current_energy"), # remove
            RegEntry("sensor.dryer_current_water", "td-1_current_water"),   # remove
            RegEntry("sensor.dryer_cycles", "td-1_total_washes"),           # KEEP (dryer)
            RegEntry("sensor.dryer_state", "td-1_state"),                   # KEEP
            RegEntry("sensor.washer_total_water", "wm-1_total_water"),      # KEEP (WM, not TD)
            RegEntry("switch.washer_power", "wm-1_power"),                  # remove (legacy power)
        ])
        self.addCleanup(setattr, er, "async_get", er.async_get)
        self.addCleanup(setattr, er, "async_entries_for_config_entry", er.async_entries_for_config_entry)
        er.async_get = lambda hass: registry
        er.async_entries_for_config_entry = lambda reg, entry_id: list(reg._entries)

        coordinator = FakeCoordinator({
            "td-1": {"type": "TD", "name": "Dryer"},
            "wm-1": {"type": "WM", "name": "Washer"},
        })
        hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator}}})

        _remove_legacy_entities(hass, FakeEntry())

        self.assertEqual(
            sorted(registry.removed),
            sorted([
                "sensor.dryer_total_water",
                "sensor.dryer_total_energy",
                "sensor.dryer_current_energy",
                "sensor.dryer_current_water",
                "switch.washer_power",
            ]),
        )
        # The dryer cycles/state and the washer's real water sensor survive.
        self.assertNotIn("sensor.dryer_cycles", registry.removed)
        self.assertNotIn("sensor.dryer_state", registry.removed)
        self.assertNotIn("sensor.washer_total_water", registry.removed)


if __name__ == "__main__":
    unittest.main()
