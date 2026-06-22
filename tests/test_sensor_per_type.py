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
    device_registry = _mod("homeassistant.helpers.device_registry")
    device_registry.DeviceEntryType = getattr(
        device_registry, "DeviceEntryType", type("DeviceEntryType", (), {"SERVICE": "service"})
    )
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
        SECONDS = "s"

    class UnitOfTemperature:
        CELSIUS = "°C"

    class UnitOfMass:
        GRAMS = "g"
        KILOGRAMS = "kg"

    const.UnitOfEnergy = getattr(const, "UnitOfEnergy", UnitOfEnergy)
    const.UnitOfVolume = getattr(const, "UnitOfVolume", UnitOfVolume)
    const.UnitOfTime = getattr(const, "UnitOfTime", UnitOfTime)
    const.UnitOfTemperature = getattr(const, "UnitOfTemperature", UnitOfTemperature)
    const.UnitOfMass = getattr(const, "UnitOfMass", UnitOfMass)
    const.EntityCategory = getattr(
        const, "EntityCategory", type("EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"})
    )

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    ha.components = components
    helpers.entity = entity
    helpers.entity_platform = entity_platform
    helpers.update_coordinator = update_coordinator
    helpers.entity_registry = entity_registry
    helpers.device_registry = device_registry
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
        from custom_components.addhon.sensor import SENSORS

        return [d.key for d in SENSORS.get(app_type, ())]

    def test_ac_keys(self) -> None:
        self.assertEqual(
            self._keys("AC"),
            ["temp_indoor", "temp_outdoor", "humidity_indoor", "compressor_freq",
             "total_energy", "pm25", "co2", "ch2o", "pm10", "voc", "co",
             "air_quality"],
        )

    def test_wm_keys(self) -> None:
        self.assertEqual(
            self._keys("WM"),
            ["state", "remaining_time", "program_name", "program_phase", "spin_speed",
             "wash_temperature", "dirty_level", "stain_type", "loading_percentage",
             "delay_time", "errors", "total_washes", "total_water", "total_energy",
             "current_energy", "current_water", "current_wash_cycle",
             "remaining_rinses", "detergent_level", "detergent_weight",
             "softener_weight", "estimated_weight"],
        )

    def test_wd_is_wm_plus_dry_level(self) -> None:
        self.assertEqual(
            self._keys("WD"),
            ["state", "remaining_time", "program_name", "program_phase", "spin_speed",
             "wash_temperature", "dirty_level", "stain_type", "dry_level",
             "loading_percentage", "delay_time", "errors", "total_washes", "total_water",
             "total_energy", "current_energy", "current_water", "current_wash_cycle",
             "remaining_rinses", "detergent_level", "detergent_weight",
             "softener_weight", "estimated_weight"],
        )

    def test_td_keys(self) -> None:
        self.assertEqual(
            self._keys("TD"),
            ["state", "remaining_time", "program_name", "program_phase", "dry_level",
             "delay_time", "errors", "temp_level", "total_washes"],
        )

    def test_td_has_no_water_or_energy(self) -> None:
        keys = set(self._keys("TD"))
        self.assertNotIn("total_water", keys)
        self.assertNotIn("current_water", keys)
        self.assertNotIn("total_energy", keys)
        self.assertNotIn("current_energy", keys)

    def test_td_cycles_read_programscounter(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        td = {d.key: d for d in SENSORS["TD"]}
        self.assertEqual(td["total_washes"].attr_key, "programsCounter")

    def test_wm_cycles_read_totalwashcycle(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        wm = {d.key: d for d in SENSORS["WM"]}
        self.assertEqual(wm["total_washes"].attr_key, "totalWashCycle")


class SensorBuildTest(unittest.IsolatedAsyncioTestCase):
    async def test_td_builds_only_expected_entities(self) -> None:
        from custom_components.addhon import sensor
        from custom_components.addhon.const import DOMAIN

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
        # Ignore the account-level diagnostic sensors added once per entry.
        added = [e for e in added if not getattr(e, "_addhon_account", False)]

        self.assertEqual(
            {e._attr_unique_id for e in added},
            {"td-1_state", "td-1_remaining_time", "td-1_program_name",
             "td-1_program_phase", "td-1_dry_level",
             "td-1_delay_time", "td-1_errors", "td-1_temp_level", "td-1_total_washes"},
        )
        cycles = next(e for e in added if e._attr_unique_id == "td-1_total_washes")
        self.assertEqual(cycles.native_value, 42.0)  # reads programsCounter
        state = next(e for e in added if e._attr_unique_id == "td-1_state")
        self.assertEqual(state.native_value, "selection")  # WM_STATE_MAP["1"]

    async def test_td_cycles_can_read_programscounter_from_statistics(self) -> None:
        from custom_components.addhon import sensor
        from custom_components.addhon.const import DOMAIN

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
        from custom_components.addhon import sensor
        from custom_components.addhon.const import DOMAIN

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
        from custom_components.addhon import _remove_legacy_entities
        from custom_components.addhon.const import DOMAIN

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
            RegEntry("sensor.dryer_load", "td-1_loading_percentage"),       # remove (TD has no loading)
            RegEntry("sensor.dryer_cycles", "td-1_total_washes"),           # KEEP (dryer)
            RegEntry("sensor.dryer_state", "td-1_state"),                   # KEEP
            RegEntry("sensor.washer_total_water", "wm-1_total_water"),      # KEEP (WM, not TD)
            RegEntry("sensor.washer_load", "wm-1_loading_percentage"),      # KEEP (WM keeps loading)
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
                "sensor.dryer_load",
                "switch.washer_power",
            ]),
        )
        # The dryer cycles/state and the washer's real water/loading sensors survive.
        self.assertNotIn("sensor.dryer_cycles", registry.removed)
        self.assertNotIn("sensor.dryer_state", registry.removed)
        self.assertNotIn("sensor.washer_total_water", registry.removed)
        self.assertNotIn("sensor.washer_load", registry.removed)


class LoadingPercentageValueFnTest(unittest.TestCase):
    """A-001: loadingPercentage is a history list, not a scalar."""

    def _fn(self):
        from custom_components.addhon.sensor import _loading_pct

        return _loading_pct

    def test_none_returns_none(self) -> None:
        self.assertIsNone(self._fn()(None))

    def test_empty_list_returns_none(self) -> None:
        self.assertIsNone(self._fn()([]))

    def test_app_mock_fixture_matches_app_average(self) -> None:
        # The official app's own loadingPercentage mock (decomp.txt:3560687); the
        # app KPI = mean of the clamped ratios = mean(90, 71.4, 60, 60) = 70.4.
        raw = [
            {"current": 0.9, "max": 1, "date": "2026-06-20"},
            {"current": 5, "max": 7, "date": "2026-06-19"},
            {"current": 0.6, "max": 1, "date": "2026-06-18"},
            {"current": 0.6, "max": 1, "date": "2026-06-17"},
        ]
        self.assertEqual(self._fn()(raw), 70.4)

    def test_average_of_records(self) -> None:
        raw = [
            {"current": 4, "max": 8, "date": "2026-06-10"},
            {"current": 6, "max": 8, "date": "2026-06-20"},
        ]
        self.assertEqual(self._fn()(raw), 62.5)  # mean(50.0, 75.0)

    def test_max_zero_backfilled_from_fleet(self) -> None:
        # The max==0 record borrows the largest max (6); mean(50.0, 16.7) = 33.3.
        raw = [
            {"current": 3, "max": 6, "date": "2026-06-10"},
            {"current": 1, "max": 0, "date": "2026-06-20"},
        ]
        self.assertEqual(self._fn()(raw), 33.3)

    def test_all_max_zero_returns_none(self) -> None:
        # The exact offline-shadow HW80 payload: no usable normalizer anywhere.
        raw = [
            {"current": 1, "max": 0, "date": "2026-06-20"},
            {"current": 2, "max": 0, "date": "2026-06-19"},
        ]
        self.assertIsNone(self._fn()(raw))

    def test_clamp_keeps_result_within_100(self) -> None:
        # current > max must be clamped (Math.min in the app), never exceed 100%.
        self.assertEqual(self._fn()([{"current": 9, "max": 5}]), 100.0)
        # Cross-scale fleet fallback would overflow without the clamp.
        raw = [
            {"current": 0.6, "max": 1, "date": "2026-06-10"},
            {"current": 9, "max": 0, "date": "2026-06-20"},
        ]
        # mean(60.0, clamp(9->1)/1*100=100.0) = 80.0; never 9/1*100 = 900.
        self.assertEqual(self._fn()(raw), 80.0)

    def test_averages_all_records(self) -> None:
        # No date windowing: every valid record contributes to the mean.
        raw = [{"current": 8, "max": 8}] * 3 + [{"current": 0, "max": 8}] * 3
        self.assertEqual(self._fn()(raw), 50.0)  # mean(100, 100, 100, 0, 0, 0)

    def test_order_independent(self) -> None:
        raw = [
            {"current": 2, "max": 8, "date": "a"},
            {"current": 6, "max": 8, "date": "b"},
            {"current": 8, "max": 8, "date": "c"},
        ]
        self.assertEqual(self._fn()(raw), self._fn()(list(reversed(raw))))

    def test_full_drum(self) -> None:
        self.assertEqual(self._fn()([{"current": 8, "max": 8}]), 100.0)

    def test_current_zero_preserved(self) -> None:
        self.assertEqual(self._fn()([{"current": 0, "max": 8}]), 0.0)

    def test_non_dict_entries_ignored(self) -> None:
        self.assertEqual(self._fn()(["garbage", 3, {"current": 4, "max": 8}]), 50.0)

    def test_missing_or_non_numeric_keys_return_none(self) -> None:
        self.assertIsNone(self._fn()([{"max": 8}]))
        self.assertIsNone(self._fn()([{"current": "abc", "max": 8}]))

    def test_scalar_passthrough(self) -> None:
        self.assertEqual(self._fn()(55), 55.0)
        self.assertEqual(self._fn()("55"), 55.0)

    def test_non_numeric_scalar_returns_none(self) -> None:
        self.assertIsNone(self._fn()("abc"))

    def test_non_finite_scalar_returns_none(self) -> None:
        self.assertIsNone(self._fn()(float("nan")))
        self.assertIsNone(self._fn()(float("inf")))

    def test_non_finite_max_is_unusable(self) -> None:
        # inf/nan max must not act as a denominator (would inject a bogus 0.0).
        self.assertIsNone(self._fn()([{"current": 4, "max": float("inf")}]))
        self.assertIsNone(self._fn()([{"current": 4, "max": float("nan")}]))
        # With a sibling that has a real max, the bad-max record borrows it (8).
        raw = [{"current": 8, "max": 8}, {"current": 4, "max": float("inf")}]
        self.assertEqual(self._fn()(raw), 75.0)  # mean(100.0, clamp(4/8)=50.0)


class LoadingPercentageBuildTest(unittest.IsolatedAsyncioTestCase):
    """End-to-end: the value_fn is wired through native_value for WM/WD/TD."""

    async def _native_value(self, app_type: str, loading) -> object:
        from custom_components.addhon import sensor
        from custom_components.addhon.const import DOMAIN

        uid = f"{app_type.lower()}-1"
        data = {
            uid: {
                "type": app_type,
                "name": app_type,
                "attributes": {"loadingPercentage": loading},
                "settings": {},
            }
        }
        coordinator = FakeCoordinator(data)
        hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator}}})
        added: list = []
        await sensor.async_setup_entry(hass, FakeEntry(), added.extend)
        entity = next(
            e for e in added if e._attr_unique_id == f"{uid}_loading_percentage"
        )
        return entity.native_value

    async def test_wm_list_does_not_crash_and_computes_pct(self) -> None:
        value = await self._native_value(
            "WM", [{"current": 6, "max": 8, "date": "2026-06-20"}]
        )
        self.assertEqual(value, 75.0)

    async def test_wm_offline_shadow_payload_is_none(self) -> None:
        value = await self._native_value("WM", [{"current": 1, "max": 0, "date": "x"}])
        self.assertIsNone(value)

    async def test_wd_uses_same_value_fn(self) -> None:
        value = await self._native_value(
            "WD", [{"current": 4, "max": 8, "date": "2026-06-20"}]
        )
        self.assertEqual(value, 50.0)

    async def test_td_has_no_loading_sensor(self) -> None:
        # The app gates loadingPercentage to WM/WD only; TD must not build it.
        from custom_components.addhon import sensor
        from custom_components.addhon.const import DOMAIN

        data = {
            "td-1": {
                "type": "TD",
                "name": "TD",
                "attributes": {"loadingPercentage": [{"current": 8, "max": 8}]},
                "settings": {},
            }
        }
        coordinator = FakeCoordinator(data)
        hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator}}})
        added: list = []
        await sensor.async_setup_entry(hass, FakeEntry(), added.extend)
        self.assertNotIn(
            "td-1_loading_percentage", {e._attr_unique_id for e in added}
        )


if __name__ == "__main__":
    unittest.main()
