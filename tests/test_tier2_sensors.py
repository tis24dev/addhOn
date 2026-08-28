# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

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

    # getattr-guarded: conftest installs a COMPLETE CoordinatorEntity before any
    # test module, so ConnectivityBinaryTest gets `available` in every order without
    # this module replacing the shared base with a narrower one.
    update_coordinator.CoordinatorEntity = getattr(
        update_coordinator, "CoordinatorEntity", CoordinatorEntity
    )
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
        HEAT = "heat"

    binary_mod.BinarySensorEntityDescription = getattr(binary_mod, "BinarySensorEntityDescription", BinarySensorEntityDescription)
    binary_mod.BinarySensorEntity = getattr(binary_mod, "BinarySensorEntity", BinarySensorEntity)
    binary_mod.BinarySensorDeviceClass = getattr(binary_mod, "BinarySensorDeviceClass", BinarySensorDeviceClass)

    # ── number platform stub (so importing custom_components.addhon.number works
    #    standalone, not only when an earlier test module installed it) ─────────
    number_mod = _mod("homeassistant.components.number")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class NumberEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        device_class: object | None = None
        native_unit_of_measurement: str | None = None
        mode: object | None = None

    class NumberEntity:
        pass

    class NumberDeviceClass:
        TEMPERATURE = "temperature"

    class NumberMode:
        BOX = "box"
        AUTO = "auto"
        SLIDER = "slider"

    number_mod.NumberEntityDescription = getattr(number_mod, "NumberEntityDescription", NumberEntityDescription)
    number_mod.NumberEntity = getattr(number_mod, "NumberEntity", NumberEntity)
    number_mod.NumberDeviceClass = getattr(number_mod, "NumberDeviceClass", NumberDeviceClass)
    number_mod.NumberMode = getattr(number_mod, "NumberMode", NumberMode)

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
    helpers.device_registry = device_registry
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
    def __init__(self, entry_id: str = "entry-1", options: dict | None = None) -> None:
        self.entry_id = entry_id
        # A real ConfigEntry always exposes options; the platforms read the
        # experimental gate off it.
        self.options = dict(options or {})


def _sensor_keys(app_type: str) -> list[str]:
    from custom_components.addhon.sensor import SENSORS

    return [d.key for d in SENSORS.get(app_type, ())]


def _binary_keys(app_type: str) -> list[str]:
    from custom_components.addhon.binary_sensor import BINARY_SENSORS

    return [d.key for d in BINARY_SENSORS.get(app_type, ())]


class FakeFixedParam:
    """A program's pinned parameter: `typology: "fixed"` plus the value it pins."""

    typology = "fixed"

    def __init__(self, value) -> None:
        self.value = value


class FakeCategory:
    def __init__(self, parameters: dict | None = None) -> None:
        self.parameters = parameters or {}


class FakeStartProgram:
    def __init__(self, categories: dict) -> None:
        self.categories = categories
        self.parameters = {}


class FakeCategorylessStartProgram:
    """A startProgram with no categories at all.

    `HonCommand.categories` answers `{"_": self}` in that case, so a walk over
    categories sees the command itself under a placeholder key that is not a program
    name (commands.py).
    """

    def __init__(self, parameters: dict) -> None:
        self.parameters = parameters

    @property
    def categories(self) -> dict:
        return {"_": self}


class FakeApplianceModel:
    """Just enough appliance for the model-catalogue gate and the program lookup.

    `model_attributes` is the flattened `applianceModel.attributes` the engine exposes,
    and `commands["startProgram"].categories` is the per-program schema the My Zone mode
    is resolved against.
    """

    def __init__(self, zones: str | None = None, programs: dict | None = None) -> None:
        self.model_attributes = {} if zones is None else {"zones": zones}
        self.commands = {}
        if programs is not None:
            self.commands["startProgram"] = FakeStartProgram(
                {
                    code: FakeCategory(
                        {} if pinned is None else {"tempSelZ3": FakeFixedParam(pinned)}
                    )
                    for code, pinned in programs.items()
                }
            )


class FakeEnumParam:
    """A category ancillary (`zone`, `programFamily`), engine-CLEANED to lowercase."""

    typology = "enum"

    def __init__(self, values) -> None:
        self.values = [str(value) for value in values]
        self.value = self.values[0] if self.values else ""


class FakeProgramParam:
    """The `program` parameter of `startProgram`: the live enum of offered codes."""

    typology = "enum"

    def __init__(self, values) -> None:
        self.values = list(values)
        self.value = self.values[0] if self.values else ""


def _drawer_capable_fridge():
    """A fridge on which `select.my_zone_mode` really can be built (#93).

    Everything `ref_programs.my_zone_codes` demands: the model declares the drawer, the
    live `startProgram.program` enum offers the three codes, and each of their categories
    pins `tempSelZ3`, is zoned on the drawer alone and is not a `download` preset. The
    sensor must step aside on exactly this appliance, and only on it.
    """
    appliance = FakeApplianceModel(zones="fridge|freezer|vtRoom1", programs={})
    codes = {"zero_fresh": "0", "quick_cool": "2", "fruit_and_veg": "5"}
    categories = {
        code: FakeCategory(
            {
                "tempSelZ3": FakeFixedParam(pinned),
                "zone": FakeEnumParam(["vtroom1"]),
                "programFamily": FakeEnumParam(["dashboard"]),
            }
        )
        for code, pinned in codes.items()
    }
    start = FakeStartProgram(categories)
    start.parameters = {"program": FakeProgramParam(list(codes))}
    appliance.commands["startProgram"] = start
    return appliance


# The fridge of issue #93: a vtRoom1 drawer, and the three programs that drive it by
# pinning tempSelZ3 -- the shape read off the sibling model's catalogue in the APK.
def _my_zone_fridge(zones: str | None = "fridge|freezer|vtRoom1"):
    return FakeApplianceModel(
        zones=zones,
        programs={
            "auto_set": None,
            "super_cool": None,
            "quick_cool": "2",
            "zero_fresh": "0",
            "fruit_and_veg": "5",
        },
    )


async def _build_sensors(
    app_type: str,
    attributes: dict,
    options: dict | None = None,
    appliance=None,
) -> list:
    from custom_components.addhon import sensor
    from custom_components.addhon.const import DOMAIN

    data = {"x-1": {"type": app_type, "name": "Dev", "attributes": attributes, "settings": {}, "appliance": appliance}}
    coordinator = FakeCoordinator(data)
    hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
    added: list = []
    await sensor.async_setup_entry(hass, FakeEntry(options=options), added.extend)
    # Drop the account-level diagnostic entities so the per-appliance assertions
    # below see only the appliance sensors.
    return [e for e in added if not getattr(e, "_addhon_account", False)]


async def _build_binary(app_type: str, attributes: dict, options: dict | None = None) -> list:
    from custom_components.addhon import binary_sensor
    from custom_components.addhon.const import DOMAIN

    data = {"x-1": {"type": app_type, "name": "Dev", "attributes": attributes, "settings": {}}}
    coordinator = FakeCoordinator(data)
    hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
    added: list = []
    await binary_sensor.async_setup_entry(hass, FakeEntry(options=options), added.extend)
    # Drop the account-level diagnostic entities (see _build_sensors).
    return [e for e in added if not getattr(e, "_addhon_account", False)]


def _experimental(enabled: bool) -> dict:
    from custom_components.addhon.const import CONF_ENABLE_EXPERIMENTAL

    return {CONF_ENABLE_EXPERIMENTAL: enabled}


# The shadow of the reporting HA2MTSJ68MC (issue #84), trimmed to the per-zone
# families and to the four zones it actually has. Every value is 0 because the hob
# was offline for ten hours when the dump was taken: the NAMES are evidence, the
# values are not, which is why nothing below asserts a state this device reported.
HOB_ATTRIBUTES = {
    "available": True,
    **{f"panStatusZ{z}": 0 for z in range(1, 5)},
    **{f"onOffStatusZ{z}": 0 for z in range(1, 5)},
    **{f"hotStatusZ{z}": 0 for z in range(1, 5)},
    **{f"errorZ{z}": 0 for z in range(1, 5)},
    **{f"powerZ{z}": 0 for z in range(1, 5)},
    **{f"combiModeZ{z}": 0 for z in range(1, 5)},
    **{f"tempZ{z}": 0 for z in range(1, 5)},
    **{f"prCodeZ{z}": 0 for z in range(1, 5)},
    **{f"prPhaseZ{z}": 0 for z in range(1, 5)},
    **{f"remainingTimeHHZ{z}": 0 for z in range(1, 5)},
    **{f"remainingTimeMMZ{z}": 0 for z in range(1, 5)},
    "lockStatus": 0,
    "timerHH": 0,
    "timerMM": 0,
    "remoteCtrValid": 0,
}


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
            ["state", "program_name", "remaining_time", "delay_time", "salt_level",
             "rinse_aid_level", "water_hardness", "wash_temperature", "errors"],
        )

    def test_oven_full_keys(self) -> None:
        self.assertEqual(
            _sensor_keys("OV"),
            ["state", "program_name", "temp_cavity", "remaining_time",
             "delay_time", "program_duration", "probe_temp_1", "probe_temp_2",
             "errors"],
        )

    def test_wine_full_keys(self) -> None:
        self.assertEqual(
            _sensor_keys("WC"),
            ["state", "program_name", "temp_ambient", "temp_zone1", "temp_zone2",
             "humidity_zone1", "humidity_zone2", "remaining_time", "errors"],
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

    def test_historic_core_not_gated_only_optionals_gated(self) -> None:
        from custom_components.addhon.sensor import SENSORS

        # Historic types keep their CORE sensors always-created (gated=False); only
        # the optional gvigroux-harvested add-ons (air-quality / auto-dose) are gated.
        expected_gated = {
            "AC": {"pm10", "voc", "co", "air_quality"},
            "WM": {"current_wash_cycle", "remaining_rinses", "detergent_level",
                   "detergent_weight", "softener_weight", "estimated_weight"},
            "WD": {"current_wash_cycle", "remaining_rinses", "detergent_level",
                   "detergent_weight", "softener_weight", "estimated_weight"},
            "TD": set(),
        }
        for app_type, gated_keys in expected_gated.items():
            actual = {d.key for d in SENSORS[app_type] if d.gated}
            self.assertEqual(actual, gated_keys, f"{app_type} gated set mismatch")

    def test_hob_binary_still_has_six_pan_zones_first(self) -> None:
        """The original assertion, narrowed rather than widened.

        It was the sentinel for the IH/HOB alias -- the two types must resolve to
        the SAME tuple object -- and for the pan family staying six wide. Growing
        it into "the whole set" would have retired that sentinel along with it, so
        the pan check keeps its own test and the full set gets a second one below.
        """
        self.assertEqual(
            [f"pan_zone{z}" for z in range(1, 7)],
            _binary_keys("IH")[:6],
        )

    def test_hob_binary_full_keys(self) -> None:
        # Spelled out, in table order: this is what a hob owner's device page
        # offers, and a row appearing or disappearing here is a user-visible
        # change that should have to be typed out.
        zones = range(1, 7)
        self.assertEqual(
            [f"pan_zone{z}" for z in zones]
            + [f"zone_on_zone{z}" for z in zones]
            + [f"hot_zone{z}" for z in zones]
            + [f"error_zone{z}" for z in zones]
            + [f"combi_mode_zone{z}" for z in zones]
            + ["child_lock"],
            _binary_keys("IH"),
        )

    def test_hob_full_keys(self) -> None:
        # The sensor half, which had no test at all before the per-zone readings
        # of #84.3 arrived.
        zones = range(1, 7)
        self.assertEqual(
            # sensorTempZ{N} covers the same six zones as every other per-zone
            # family: the table used to stop at five, so a hob reporting
            # sensorTempZ6 got no temp_zone6 (PR #87 review).
            [f"temp_zone{z}" for z in zones]
            + [f"power_zone{z}" for z in zones]
            + [f"plate_temp_zone{z}" for z in zones]
            + [f"program_code_zone{z}" for z in zones]
            + [f"program_phase_zone{z}" for z in zones]
            + ["timer_hh", "timer_mm"],
            _sensor_keys("IH"),
        )

    def test_the_hob_child_lock_reuses_the_wash_group_description(self) -> None:
        """Same object, not a twin: same parameter, same meaning, same label.

        Also the guard against giving it `device_class=LOCK`, whose polarity is
        inverted in Home Assistant -- a hob reporting lockStatus=1 would display
        as unlocked, which is the opposite of the truth on a safety control.
        """
        from custom_components.addhon.binary_sensor import _CHILD_LOCK, BINARY_SENSORS

        child_lock = next(d for d in BINARY_SENSORS["IH"] if d.key == "child_lock")
        self.assertIs(_CHILD_LOCK, child_lock)
        self.assertIsNone(child_lock.device_class)
        self.assertEqual("lockStatus", child_lock.attr_key)


class HobZoneReadingsTest(unittest.IsolatedAsyncioTestCase):
    """The per-zone readings of issue #84 point 3.

    Every family is generated for six zones and gated per attribute, so the four
    a HA2MTSJ68MC reports produce four entities and a hypothetical six-zone hob
    produces six -- without either being written down anywhere.
    """

    async def test_a_four_zone_hob_gets_four_of_each_live_family(self) -> None:
        keys = {e._attr_unique_id for e in await _build_binary("IH", HOB_ATTRIBUTES)}
        for family in ("pan_zone", "zone_on_zone", "hot_zone", "error_zone"):
            self.assertEqual(
                {f"x-1_{family}{z}" for z in range(1, 5)},
                {k for k in keys if k.startswith(f"x-1_{family}")},
                family,
            )
        self.assertIn("x-1_child_lock", keys)

    async def test_a_hob_reporting_two_zones_gets_two(self) -> None:
        attributes = {
            "available": True,
            "hotStatusZ1": 0,
            "hotStatusZ3": 1,
        }
        keys = {e._attr_unique_id for e in await _build_binary("IH", attributes)}
        self.assertEqual(
            {"x-1_hot_zone1", "x-1_hot_zone3", "x-1_connectivity"}, keys
        )

    async def test_residual_heat_reads_the_panel_flag(self) -> None:
        attributes = dict(HOB_ATTRIBUTES, hotStatusZ2=1)
        added = await _build_binary("IH", attributes)
        by_id = {e._attr_unique_id: e for e in added}
        self.assertIs(True, by_id["x-1_hot_zone2"].is_on)
        self.assertIs(False, by_id["x-1_hot_zone1"].is_on)

    async def test_a_healthy_hob_reports_no_zone_error(self) -> None:
        """The regression this family exists to avoid.

        A comparison against a raw code lights PROBLEM on every zone forever: the
        hob spells "no error" as 0 and the app spells it "00", and the client
        folds "01" to "1" on the way in, so no single literal can be the off
        value. `has_problem` is the shared rule that already knows all three.
        """
        added = await _build_binary("IH", dict(HOB_ATTRIBUTES, errorZ1="00", errorZ2=0))
        by_id = {e._attr_unique_id: e for e in added}
        self.assertIs(False, by_id["x-1_error_zone1"].is_on)
        self.assertIs(False, by_id["x-1_error_zone2"].is_on)

    async def test_a_real_zone_fault_still_reports(self) -> None:
        added = await _build_binary("IH", dict(HOB_ATTRIBUTES, errorZ3="05"))
        by_id = {e._attr_unique_id: e for e in added}
        self.assertIs(True, by_id["x-1_error_zone3"].is_on)

    def test_every_no_error_spelling_reads_as_healthy(self) -> None:
        # Directly, on the shared rule, so the reason the assertions above pass is
        # visible rather than inferred.
        from custom_components.addhon.air_purifier import has_problem

        for healthy in ("0", "00", 0, 0.0, "0.0", ""):
            self.assertFalse(has_problem(healthy), repr(healthy))
        for faulty in ("1", "05", "0A", 5):
            self.assertTrue(has_problem(faulty), repr(faulty))

    async def test_the_power_level_is_a_level_and_not_watts(self) -> None:
        added = await _build_sensors("IH", dict(HOB_ATTRIBUTES, powerZ1=9))
        power = next(e for e in added if e._attr_unique_id == "x-1_power_zone1")
        self.assertEqual(9.0, power.native_value)
        self.assertIsNone(power.entity_description.device_class)
        self.assertIsNone(power.entity_description.native_unit_of_measurement)

    async def test_the_remaining_time_combines_both_halves(self) -> None:
        attributes = dict(HOB_ATTRIBUTES, remainingTimeHHZ1=1, remainingTimeMMZ1=30)
        added = await _build_sensors("IH", attributes)
        remaining = next(
            e for e in added if e._attr_unique_id == "x-1_remaining_time_zone1"
        )
        self.assertEqual(90, remaining.native_value)

    async def test_a_non_finite_half_reads_unknown_rather_than_raising(self) -> None:
        """`float("nan")` parses; `int()` of it does not.

        The guard used to wrap only the two `float()` calls, so a device sending
        "nan" or "inf" reached `int()` unprotected: ValueError on the first,
        OverflowError on the second, both escaping a PROPERTY. Home Assistant
        surfaces that as a broken entity, which is a worse answer than "unknown"
        to a reading the device itself could not express. Reported on PR #87.
        """
        for hours, minutes in (
            ("nan", 30), (1, "nan"), ("inf", 30), (1, "-inf"), ("nan", "nan"),
        ):
            attributes = dict(
                HOB_ATTRIBUTES, remainingTimeHHZ1=hours, remainingTimeMMZ1=minutes
            )
            added = await _build_sensors("IH", attributes)
            remaining = next(
                e for e in added if e._attr_unique_id == "x-1_remaining_time_zone1"
            )
            self.assertIsNone(remaining.native_value, (hours, minutes))

    async def test_the_remaining_time_needs_both_halves_to_exist(self) -> None:
        # Half a clock is worse than none: the minute half alone reads 5 minutes
        # for a two-hour-five programme.
        attributes = {"available": True, "remainingTimeMMZ1": 30}
        added = await _build_sensors("IH", attributes)
        self.assertEqual([], [e for e in added if "remaining_time" in e._attr_unique_id])

    async def test_the_remaining_time_exists_per_reported_zone(self) -> None:
        added = await _build_sensors("IH", HOB_ATTRIBUTES)
        self.assertEqual(
            [f"x-1_remaining_time_zone{z}" for z in range(1, 5)],
            [e._attr_unique_id for e in added if "remaining_time" in e._attr_unique_id],
        )

    async def test_an_unreadable_half_reads_unknown(self) -> None:
        attributes = dict(HOB_ATTRIBUTES, remainingTimeHHZ1="", remainingTimeMMZ1=30)
        added = await _build_sensors("IH", attributes)
        remaining = [e for e in added if e._attr_unique_id == "x-1_remaining_time_zone1"]
        # The key is present in the shadow, so the entity is created; the reading
        # itself is what refuses to be invented.
        self.assertEqual(1, len(remaining))
        self.assertIsNone(remaining[0].native_value)

    async def test_the_child_lock_reads_the_panel_lock(self) -> None:
        added = await _build_binary("IH", dict(HOB_ATTRIBUTES, lockStatus=1))
        lock = next(e for e in added if e._attr_unique_id == "x-1_child_lock")
        self.assertIs(True, lock.is_on)

    async def test_the_dead_families_are_absent_by_default(self) -> None:
        # tempZ*, prCodeZ*, prPhaseZ*, combiModeZ* and the hob timer are all in
        # this hob's shadow and none of them has moved since 2022.
        sensors = {e._attr_unique_id for e in await _build_sensors("IH", HOB_ATTRIBUTES)}
        binaries = {e._attr_unique_id for e in await _build_binary("IH", HOB_ATTRIBUTES)}
        for absent in (
            "x-1_plate_temp_zone1",
            "x-1_program_code_zone1",
            "x-1_program_phase_zone1",
            "x-1_timer_hh",
            "x-1_timer_mm",
        ):
            self.assertNotIn(absent, sensors, absent)
        self.assertNotIn("x-1_combi_mode_zone1", binaries)

    async def test_the_dead_families_appear_when_experimental_is_on(self) -> None:
        sensors = {
            e._attr_unique_id
            for e in await _build_sensors("IH", HOB_ATTRIBUTES, _experimental(True))
        }
        binaries = {
            e._attr_unique_id
            for e in await _build_binary("IH", HOB_ATTRIBUTES, _experimental(True))
        }
        for present in (
            "x-1_plate_temp_zone1",
            "x-1_program_code_zone1",
            "x-1_program_phase_zone1",
            "x-1_timer_hh",
            "x-1_timer_mm",
        ):
            self.assertIn(present, sensors, present)
        self.assertIn("x-1_combi_mode_zone1", binaries)

    async def test_the_live_families_do_not_depend_on_the_option(self) -> None:
        # The option must add readings, never move the ones a hob owner already
        # relies on.
        default = {e._attr_unique_id for e in await _build_binary("IH", HOB_ATTRIBUTES)}
        enabled = {
            e._attr_unique_id
            for e in await _build_binary("IH", HOB_ATTRIBUTES, _experimental(True))
        }
        self.assertTrue(default < enabled)
        self.assertIn("x-1_hot_zone1", default)

    async def test_the_plate_temperature_does_not_replace_the_sensor_one(self) -> None:
        # `temp_zone{N}` reads sensorTempZ{N} and other hobs publish it; the new
        # family reads tempZ{N} under its own key so neither displaces the other.
        attributes = {"available": True, "sensorTempZ1": 40, "tempZ1": 55}
        added = await _build_sensors("IH", attributes, _experimental(True))
        by_id = {e._attr_unique_id: e for e in added}
        self.assertEqual(40.0, by_id["x-1_temp_zone1"].native_value)
        self.assertEqual(55.0, by_id["x-1_plate_temp_zone1"].native_value)

    async def test_the_plate_temperature_claims_nothing_it_cannot_prove(self) -> None:
        """It shipped as a TEMPERATURE in °C with statistics enabled, and none of
        the three is something this integration can stand behind.

        `tempZ{N}` has not moved since 2022 on the only hob anyone has, and that
        model declares `probe = "0"` -- no probe. The reading being a plate
        temperature at all is an inference from a parameter NAME in the decompiled
        app. `device_class` + unit tell Home Assistant what the number IS, and
        `state_class=MEASUREMENT` writes it into long-term statistics under that
        unit: promoting the entity later is additive, unpicking a year of
        statistics recorded in the wrong unit is not.

        The positive control below is the point of the test: the CONFIRMED
        temperature on the same hob keeps all three, so this is a claim withdrawn
        where the evidence is missing and not a device_class deleted everywhere.
        """
        attributes = {"available": True, "sensorTempZ1": 40, "tempZ1": 55}
        by_id = {
            e._attr_unique_id: e
            for e in await _build_sensors("IH", attributes, _experimental(True))
        }
        plate = by_id["x-1_plate_temp_zone1"].entity_description
        self.assertIsNone(plate.device_class)
        self.assertIsNone(plate.native_unit_of_measurement)
        self.assertIsNone(plate.state_class)
        self.assertTrue(plate.experimental)

        confirmed = by_id["x-1_temp_zone1"].entity_description
        self.assertIsNotNone(confirmed.device_class)
        self.assertIsNotNone(confirmed.native_unit_of_measurement)
        self.assertIsNotNone(confirmed.state_class)

    async def test_the_hob_alias_produces_the_same_entities(self) -> None:
        ih = [e._attr_unique_id for e in await _build_binary("IH", HOB_ATTRIBUTES)]
        hob = [e._attr_unique_id for e in await _build_binary("HOB", HOB_ATTRIBUTES)]
        self.assertEqual(ih, hob)


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

    async def test_fridge_errors_is_gated_like_every_other_reading(self) -> None:
        """issue #93: the fridge was the one type with an app error channel and no entity."""
        self.assertNotIn(
            "x-1_errors",
            {e._attr_unique_id for e in await _build_sensors("REF", {"tempZ1": "4"})},
        )
        added = await _build_sensors("REF", {"errors": "0"})
        self.assertEqual({e._attr_unique_id for e in added}, {"x-1_errors"})

    async def test_fridge_errors_reaches_fr_and_fre_too(self) -> None:
        for app_type in ("FR", "FRE"):
            added = await _build_sensors(app_type, {"errors": "0"})
            self.assertIn("x-1_errors", {e._attr_unique_id for e in added}, app_type)

    async def test_fridge_errors_folds_every_healthy_spelling(self) -> None:
        """The device spells "no fault" four ways and the app accepts all four.

        Without the fold the same healthy appliance would publish 0, "0", "00" and "100"
        as four different states -- and "100" is a reserved bit the app whitelists by
        name, not a fault (apk/analysis/issue93-ref-unmapped-values.md section 3.2).
        """
        for healthy in (0, "0", "00", "000", "100", "", None):
            added = await _build_sensors("REF", {"errors": healthy, "tempZ1": "4"})
            entity = next(e for e in added if e._attr_unique_id == "x-1_errors")
            self.assertEqual(entity.native_value, "0", repr(healthy))

    async def test_fridge_errors_passes_a_real_code_through(self) -> None:
        """A hex bitmask is an opaque token: the meaning lives behind a cloud lookup we
        cannot make, so the raw value is the only honest rendering -- and it may carry
        several bits at once, REF being the type the app exempts from bit reduction."""
        for code in ("80", "0A", "E12", "1200"):
            added = await _build_sensors("REF", {"errors": code})
            entity = next(e for e in added if e._attr_unique_id == "x-1_errors")
            self.assertEqual(entity.native_value, code, code)

    async def test_fridge_errors_falls_back_to_the_singular_spelling(self) -> None:
        """`COMMON_PARAMS_ENUM` declares both `errors` and `error`, and the app reads the
        singular when the plural is absent."""
        added = await _build_sensors("REF", {"error": "80"})
        entity = next(e for e in added if e._attr_unique_id == "x-1_errors")
        self.assertEqual(entity.native_value, "80")

    async def test_my_zone_mode_needs_the_model_to_declare_the_drawer(self) -> None:
        """The gate is the MODEL CATALOGUE, not the shadow.

        `zones` is what the app filters its fridge zone cards on. Gating on the shadow
        instead would be the mistake the app's own abandoned helper made: it derived the
        zone list from the truthiness of `tempSelZ*` and would have dropped a My Zone
        whose register reads 0 -- which is a real mode, not an absence (#93).
        """
        attrs = {"tempSelZ3": "0"}
        with_zone = await _build_sensors("REF", attrs, appliance=_my_zone_fridge())
        self.assertIn("x-1_my_zone_mode", {e._attr_unique_id for e in with_zone})

        for zones in ("fridge|freezer", None):
            without = await _build_sensors(
                "REF", attrs, appliance=_my_zone_fridge(zones=zones)
            )
            self.assertNotIn(
                "x-1_my_zone_mode", {e._attr_unique_id for e in without}, repr(zones)
            )

    async def test_my_zone_mode_still_needs_the_register(self) -> None:
        """Both gates, not either: a declared drawer with no register reports nothing."""
        added = await _build_sensors(
            "REF", {"tempZ1": "4"}, appliance=_my_zone_fridge()
        )
        self.assertNotIn("x-1_my_zone_mode", {e._attr_unique_id for e in added})

    async def test_my_zone_mode_denies_when_the_catalogue_is_silent(self) -> None:
        """No appliance, no catalogue, no entity. Stricter than the app, on purpose: an
        entity invented out of silence cannot be told from one that belongs."""
        added = await _build_sensors("REF", {"tempSelZ3": "0"}, appliance=None)
        self.assertNotIn("x-1_my_zone_mode", {e._attr_unique_id for e in added})

    async def _my_zone_state(self, value, appliance=None):
        added = await _build_sensors(
            "REF",
            {"tempSelZ3": value},
            appliance=appliance if appliance is not None else _my_zone_fridge(),
        )
        return next(e for e in added if e._attr_unique_id == "x-1_my_zone_mode")

    async def test_my_zone_mode_reads_the_devices_own_program_catalogue_first(self) -> None:
        """The value is a program's signature, and the device says which program.

        `getMyZoneMappedMode` asks `getModeNameFromCommands` before it looks at the
        number at all, so the answer is a startProgram slug -- the same vocabulary
        `select.ref_program` offers, which is what makes the two read as one surface.
        """
        for value, expected in (("0", "zero_fresh"), ("2", "quick_cool"), ("5", "fruit_and_veg")):
            entity = await self._my_zone_state(value)
            self.assertEqual(entity.native_value, expected, value)

    async def test_my_zone_mode_falls_back_to_the_static_table(self) -> None:
        """A fridge whose catalogue pins nothing still gets the app's second answer.

        And here `2` is `chiller`, not `quick_cool`: the app's own 2-branch only says
        Quick Cool when a program claims the value (or when it runs in demo mode).
        """
        bare = FakeApplianceModel(zones="fridge|freezer|vtRoom1", programs={})
        for value, expected in (
            ("0", "zero_fresh"), ("2", "chiller"), ("3", "cool_drink"),
            ("4", "cheese"), ("5", "fruit_and_veg"),
        ):
            entity = await self._my_zone_state(value, appliance=bare)
            self.assertEqual(entity.native_value, expected, value)

    async def test_my_zone_mode_is_unknown_for_a_value_nothing_explains(self) -> None:
        """`17` is what the Holiday program's rules pin the register to, and while
        Holiday runs the drawer is in none of its own modes -- the app shows
        NO_MODE_SELECTED there too. `1` is simply not a mode on any known model."""
        for value in ("17", "1", "", "-5"):
            entity = await self._my_zone_state(value)
            self.assertIsNone(entity.native_value, value)

    async def test_every_fridge_door_the_shadow_reports_becomes_an_entity(self) -> None:
        """A four-door fridge gets four doors (discussion #94).

        `doorStatusZ4` was the one zone the cooling table skipped: the reporter's
        HCW58F18EWMP publishes it, `temp_zone4` was already there, and his diagnostics
        printed `doorStatusZ4` under `attributes_unmapped` while `doorStatusZ3` sat two
        lines away under `attributes_expected_absent`.
        """
        added = await _build_binary(
            "REF",
            {
                "doorStatusZ1": "0", "door2StatusZ1": "0",
                "doorStatusZ2": "0", "doorStatusZ4": "0",
            },
        )
        self.assertEqual(
            ["door_zone1", "door2_zone1", "door_zone2", "door_zone4"],
            [e.entity_description.key for e in added
             if e.entity_description.key.startswith("door")],
        )

    async def test_a_door_the_shadow_does_not_report_is_not_invented(self) -> None:
        # The capability gate is what keeps the widened table honest: a two-door fridge
        # must not grow two entities that will never leave `unknown`.
        added = await _build_binary("REF", {"doorStatusZ1": "0"})
        keys = {e.entity_description.key for e in added}
        for absent in ("door_zone2", "door_zone3", "door_zone4"):
            self.assertNotIn(absent, keys, absent)

    async def test_the_fourth_door_reads_open_and_closed(self) -> None:
        for raw, expected in (("0", False), ("1", True), (0, False), (1, True)):
            added = await _build_binary("REF", {"doorStatusZ4": raw})
            entity = next(
                e for e in added if e.entity_description.key == "door_zone4"
            )
            self.assertIs(expected, entity.is_on, repr(raw))

    async def test_the_sensor_steps_aside_where_the_select_is_built(self) -> None:
        """One drawer, one entity with that name (#93).

        `select.my_zone_mode` reports the same register under the same label, so on an
        appliance whose catalogue lets the writable control be built the read-only half
        must not also appear -- two identically-named entities on one device is the noise
        this release set out to remove. Untested until now: every other fixture in this
        class declares no `zone` ancillary, so `my_zone_codes` is empty there and the
        suppression branch is never reached.
        """
        added = await _build_sensors(
            "REF", {"tempSelZ3": "0"}, appliance=_drawer_capable_fridge()
        )
        self.assertNotIn("x-1_my_zone_mode", {e._attr_unique_id for e in added})
        # ...and only that one: the suppression is keyed on the description, so a
        # careless gate would take the whole `requires_zone` branch with it.
        added_all = await _build_sensors(
            "REF",
            {"tempSelZ3": "0", "tempZ1": "5"},
            appliance=_drawer_capable_fridge(),
        )
        self.assertIn("x-1_temp_zone1", {e._attr_unique_id for e in added_all})

    async def test_the_sensor_stays_where_no_select_can_be_built(self) -> None:
        """The other side of the same gate: a fridge whose catalogue carries no drawer
        PROGRAM keeps the reading, because nothing replaces it."""
        entity = await self._my_zone_state("0")
        self.assertEqual("zero_fresh", entity.native_value)

    async def test_my_zone_mode_options_admit_every_state_it_can_report(self) -> None:
        """An ENUM sensor may not report a state outside its options, and the catalogue
        lookup can answer with any slug the MODEL declares -- which the static table
        cannot know. Hence the per-entity widening."""
        entity = await self._my_zone_state("0")
        self.assertIn("zero_fresh", entity._attr_options)
        self.assertIn("quick_cool", entity._attr_options)
        # ...and the static vocabulary survives the widening.
        self.assertIn("chiller", entity._attr_options)

    async def test_my_zone_mode_never_reports_outside_its_options(self) -> None:
        """The catalogue is read at every refresh while options are frozen at
        construction, so a slug the widening never saw must fall through, not leak."""
        entity = await self._my_zone_state("0")
        entity._attr_options = ["chiller"]
        self.assertEqual(entity.native_value, "zero_fresh")

    async def test_the_fixed_value_lookup_skips_the_synthetic_category(self) -> None:
        """The helper's own contract, pinned apart from the sensor that uses it.

        `program_code_for_fixed_value` is public in `hon_commands`, and the sensor's
        options guard would mask a regression here (the placeholder never reaches the
        allowed set, so it falls through anyway). Tested directly so the function is
        answerable for itself.
        """
        from custom_components.addhon.hon_commands import program_code_for_fixed_value

        appliance = FakeApplianceModel()
        appliance.commands["startProgram"] = FakeCategorylessStartProgram(
            {"tempSelZ3": FakeFixedParam("0")}
        )
        self.assertIsNone(
            program_code_for_fixed_value(appliance, "tempSelZ3", "0")
        )
        # A real category with the same pinned value still answers.
        appliance.commands["startProgram"] = FakeStartProgram(
            {"zero_fresh": FakeCategory({"tempSelZ3": FakeFixedParam("0")})}
        )
        self.assertEqual(
            program_code_for_fixed_value(appliance, "tempSelZ3", "0"), "zero_fresh"
        )

    async def test_my_zone_mode_never_reports_the_synthetic_category(self) -> None:
        """A category-less startProgram must not be mistaken for a program.

        `HonCommand.categories` reports such a command as `{"_": self}`, so a naive walk
        answers "_" -- and because the same walk also widens `options`, the
        out-of-options guard would have admitted it. The sensor would have shown a bare
        "_", with no translation, as the drawer's mode.
        """
        appliance = FakeApplianceModel(zones="fridge|freezer|vtRoom1")
        appliance.commands["startProgram"] = FakeCategorylessStartProgram(
            {"tempSelZ3": FakeFixedParam("0")}
        )
        added = await _build_sensors("REF", {"tempSelZ3": "0"}, appliance=appliance)
        entity = next(e for e in added if e._attr_unique_id == "x-1_my_zone_mode")
        self.assertNotIn("_", entity._attr_options)
        # ...and it falls through to the static table, which is the right answer here.
        self.assertEqual(entity.native_value, "zero_fresh")

    async def test_my_zone_mode_reaches_fr_and_fre_too(self) -> None:
        for app_type in ("FR", "FRE"):
            added = await _build_sensors(
                app_type, {"tempSelZ3": "0"}, appliance=_my_zone_fridge()
            )
            self.assertIn(
                "x-1_my_zone_mode", {e._attr_unique_id for e in added}, app_type
            )

    async def test_fridge_errors_is_diagnostic(self) -> None:
        """Not a headline reading: the app keeps its own fridge error banner dark until a
        `config/troubleshooting` verdict comes back, and ships no REF error push at all."""
        from homeassistant.const import EntityCategory

        added = await _build_sensors("REF", {"errors": "0"})
        entity = next(e for e in added if e._attr_unique_id == "x-1_errors")
        self.assertEqual(
            entity.entity_description.entity_category, EntityCategory.DIAGNOSTIC
        )

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

    async def test_dryer_temp_level(self) -> None:
        # TD gains a temperature-level sensor (tempLevel); live-confirmed on HD100.
        added = await _build_sensors("TD", {"tempLevel": "4", "machMode": "1"})
        tl = next(e for e in added if e._attr_unique_id == "x-1_temp_level")
        self.assertEqual(tl.native_value, 4.0)

    async def test_washer_stain_type_decodes(self) -> None:
        # WM stain_type ENUM: code -> machine key, 0 -> none, unknown -> None.
        # 9/13/15 corrected to the app's stainOptions (were ice_cream/rust/perfume).
        cases = (
            ("1", "wine"), ("0", "none"), ("26", "fruit"), ("99", None),
            ("9", "chocolate"), ("13", "chili_oil"), ("15", "color_pencil"),
        )
        for raw, expected in cases:
            added = await _build_sensors("WM", {"stainType": raw})
            st = next(e for e in added if e._attr_unique_id == "x-1_stain_type")
            self.assertEqual(st.native_value, expected)

    async def test_washer_state_decodes(self) -> None:
        # WM state ENUM uses the authoritative MachineMode: 2=running, 3=paused.
        for raw, expected in (
            ("0", "idle"), ("1", "selection"), ("2", "running"),
            ("3", "paused"), ("7", "finished"), ("99", None),
        ):
            added = await _build_sensors("WM", {"machMode": raw})
            st = next(e for e in added if e._attr_unique_id == "x-1_state")
            self.assertEqual(st.native_value, expected)


class ConstMapTest(unittest.TestCase):
    def test_ac_fan_map_auto_is_5(self) -> None:
        from custom_components.addhon.const import AC_FAN_MAP, AC_FAN_MAP_REVERSE

        self.assertEqual(AC_FAN_MAP.get("5"), "auto")
        self.assertNotIn("0", AC_FAN_MAP)  # the device rejects windSpeed 0
        self.assertEqual(AC_FAN_MAP_REVERSE["auto"], "5")
        self.assertEqual(AC_FAN_MAP_REVERSE["high"], "1")

    def test_wm_state_map_authoritative_codes(self) -> None:
        from custom_components.addhon.const import WM_STATE_MAP

        # Authoritative MachineMode semantics (decomp): 1=selection, 2=running,
        # 3=paused, 7=finished; "half_load" is a program option, never a state.
        self.assertEqual(WM_STATE_MAP["1"], "selection")
        self.assertEqual(WM_STATE_MAP["2"], "running")
        self.assertEqual(WM_STATE_MAP["3"], "paused")
        self.assertEqual(WM_STATE_MAP["7"], "finished")
        self.assertNotIn("half_load", WM_STATE_MAP.values())

    def test_stain_map_full_table(self) -> None:
        from custom_components.addhon.const import STAIN_TYPE_MAP

        # Locks the entire app stainOptions table (decomp.txt:977322-977422).
        self.assertEqual(STAIN_TYPE_MAP, {
            "0": "none", "1": "wine", "2": "grass", "3": "soil", "4": "blood",
            "5": "milk", "6": "cooking_oil", "7": "tea", "8": "coffee",
            "9": "chocolate", "10": "lip_gloss", "11": "curry", "12": "milk_tea",
            "13": "chili_oil", "14": "blue_ink", "15": "color_pencil",
            "16": "shoe_cream", "17": "oil_pastel", "18": "blueberry", "19": "sweat",
            "20": "egg", "21": "ketchup", "22": "baby_food", "23": "soy_sauce",
            "24": "bean_paste", "25": "chili_sauce", "26": "fruit",
        })


class PauseSwitchTest(unittest.IsolatedAsyncioTestCase):
    def _switch(self, mach_mode: str):
        sw = _mod("homeassistant.components.switch")
        sw.SwitchEntity = getattr(sw, "SwitchEntity", type("SwitchEntity", (), {}))
        from custom_components.addhon.switch import HonWashingMachinePauseSwitch

        data = {"x-1": {"type": "WM", "name": "Dev",
                        "attributes": {"machMode": mach_mode}, "settings": {}}}
        return HonWashingMachinePauseSwitch(FakeCoordinator(data), "x-1", None)

    def test_is_on_only_when_paused(self) -> None:
        self.assertTrue(self._switch("3").is_on)   # 3 = PAUSE_MODE
        self.assertFalse(self._switch("2").is_on)  # 2 = EXECUTION (running)
        self.assertFalse(self._switch("0").is_on)


class Tier2BinaryGatingTest(unittest.IsolatedAsyncioTestCase):
    async def test_cooling_binary_gating(self) -> None:
        added = await _build_binary("REF", {"doorStatusZ1": "1", "icemakerOnOffStatus": "0"})
        self.assertEqual(
            {e._attr_unique_id for e in added},
            {"x-1_door_zone1", "x-1_ice_maker", "x-1_connectivity"},
        )
        door = next(e for e in added if e._attr_unique_id == "x-1_door_zone1")
        self.assertTrue(door.is_on)

    async def test_cooling_binary_mode_flags(self) -> None:
        # Read-only active-mode flags (quickModeZ1/Z2, intelligenceMode, holidayMode),
        # capability-gated and decoded as 0/1. Live-confirmed present on the real fridge.
        added = await _build_binary(
            "REF",
            {"quickModeZ1": "1", "quickModeZ2": "0", "intelligenceMode": "1", "holidayMode": "0"},
        )
        by_id = {e._attr_unique_id: e for e in added}
        self.assertEqual(
            set(by_id),
            {"x-1_quick_cool", "x-1_quick_freeze", "x-1_auto_set",
             "x-1_holiday_mode", "x-1_connectivity"},
        )
        self.assertTrue(by_id["x-1_quick_cool"].is_on)
        self.assertFalse(by_id["x-1_quick_freeze"].is_on)
        self.assertTrue(by_id["x-1_auto_set"].is_on)
        self.assertFalse(by_id["x-1_holiday_mode"].is_on)

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

    async def test_ac_binary_gating(self) -> None:
        # AC gains a per-type binary set (filter change + formaldehyde cleaning),
        # capability-gated like all binary sensors. Live-confirmed on the real AC.
        added = await _build_binary("AC", {"filterChangeStatusLocal": "1", "ch2oCleaningStatus": "0"})
        by_id = {e._attr_unique_id: e for e in added}
        self.assertEqual(
            set(by_id), {"x-1_filter_change", "x-1_ch2o_cleaning", "x-1_connectivity"}
        )
        self.assertTrue(by_id["x-1_filter_change"].is_on)
        self.assertFalse(by_id["x-1_ch2o_cleaning"].is_on)


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


class GvigrouxImportTest(unittest.IsolatedAsyncioTestCase):
    """Live-tested mapping items adopted from gvigroux/hon (real-device evidence)."""

    async def test_oven_gains_program_delay_errors(self) -> None:
        added = await _build_sensors("OV", {
            "machMode": "2", "programName": "PIZZA", "delayTime": 30,
            "errors": "00", "temp": 180, "remainingTimeMM": 20,
        })
        uids = {e._attr_unique_id for e in added}
        self.assertIn("x-1_program_name", uids)
        self.assertIn("x-1_delay_time", uids)
        self.assertIn("x-1_errors", uids)

    async def test_oven_preheat_binary(self) -> None:
        added = await _build_binary("OV", {"preheatStatus": "1"})
        preheat = next(e for e in added if e._attr_unique_id == "x-1_preheat")
        self.assertTrue(preheat.is_on)

    def test_oven_number_fallback_is_oven_range(self) -> None:
        from custom_components.addhon.const import APPLIANCE_OV
        from custom_components.addhon.number import NUMBERS

        target = {d.key: d for d in NUMBERS[APPLIANCE_OV]}["target_temp"]
        self.assertEqual(
            (target.fallback_min, target.fallback_max, target.fallback_step),
            (50.0, 280.0, 5.0),
        )

    async def test_dishwasher_wash_temp_reads_temp_or_temperature(self) -> None:
        # Both key variants build the sensor and yield the value (gate/read both).
        for attrs in ({"temp": "45"}, {"temperature": "45"}):
            added = await _build_sensors("DW", attrs)
            wt = next(e for e in added if e._attr_unique_id == "x-1_wash_temperature")
            self.assertEqual(wt.native_value, 45.0)

    def test_oven_delay_time_is_duration_minutes(self) -> None:
        from homeassistant.components.sensor import SensorDeviceClass
        from homeassistant.const import UnitOfTime

        from custom_components.addhon.const import APPLIANCE_OV
        from custom_components.addhon.sensor import SENSORS

        d = {x.key: x for x in SENSORS[APPLIANCE_OV]}["delay_time"]
        self.assertEqual(d.device_class, SensorDeviceClass.DURATION)
        self.assertEqual(d.native_unit_of_measurement, UnitOfTime.MINUTES)

    def test_oven_preheat_device_class_is_heat(self) -> None:
        from homeassistant.components.binary_sensor import BinarySensorDeviceClass

        from custom_components.addhon.binary_sensor import BINARY_SENSORS
        from custom_components.addhon.const import APPLIANCE_OV

        d = {x.key: x for x in BINARY_SENSORS[APPLIANCE_OV]}["preheat"]
        self.assertEqual(d.device_class, BinarySensorDeviceClass.HEAT)

    async def test_wine_cooler_zone1_temp_and_humidity(self) -> None:
        added = await _build_sensors("WC", {
            "temp": 12, "tempZ2": 8, "humidityZ1": 60, "humidityZ2": 65,
        })
        by_uid = {e._attr_unique_id: e for e in added}
        self.assertEqual(by_uid["x-1_temp_zone1"].native_value, 12.0)  # reads `temp`
        self.assertIn("x-1_temp_zone2", by_uid)
        self.assertIn("x-1_humidity_zone1", by_uid)
        self.assertIn("x-1_humidity_zone2", by_uid)

    async def test_wine_cooler_zone1_ignores_tempz1(self) -> None:
        # zone1 actual temp is `temp`, not `tempZ1` (which does not exist for WC).
        added = await _build_sensors("WC", {"tempZ1": 99})
        self.assertNotIn("x-1_temp_zone1", {e._attr_unique_id for e in added})

    # --- gvigroux P1 harvest: air-quality / auto-dose / options (all gated) ---

    async def test_ac_air_quality_appears_only_when_reported(self) -> None:
        added = await _build_sensors("AC", {
            "pm10ValueIndoor": "6", "vocValueIndoor": "1", "coLevel": "0",
            "airQuality": "2",
        })
        by_uid = {e._attr_unique_id: e for e in added}
        for k in ("pm10", "voc", "co", "air_quality"):
            self.assertIn(f"x-1_{k}", by_uid)
        self.assertEqual(by_uid["x-1_air_quality"].native_value, 2.0)
        # absent when the device does not report them (gated)
        uids2 = {e._attr_unique_id for e in await _build_sensors("AC", {})}
        for k in ("pm10", "voc", "co", "air_quality"):
            self.assertNotIn(f"x-1_{k}", uids2)

    def test_ac_air_quality_device_classes(self) -> None:
        # pm10 is a real mass concentration; voc/co/air_quality are level indexes
        # surfaced as plain integers (no misleading device_class/unit).
        from homeassistant.components.sensor import SensorDeviceClass

        from custom_components.addhon.const import APPLIANCE_AC
        from custom_components.addhon.sensor import SENSORS

        ac = {d.key: d for d in SENSORS[APPLIANCE_AC]}
        self.assertEqual(ac["pm10"].device_class, SensorDeviceClass.PM10)
        self.assertEqual(ac["pm10"].native_unit_of_measurement, "µg/m³")
        for k in ("voc", "co", "air_quality"):
            self.assertIsNone(ac[k].device_class, f"{k} must be class-less")
            self.assertIsNone(ac[k].native_unit_of_measurement, f"{k} must be unitless")

    async def test_dishwasher_delay_hardness_and_option_binaries(self) -> None:
        added = await _build_sensors("DW", {"delayTime": "30", "waterHard": "3"})
        uids = {e._attr_unique_id for e in added}
        self.assertIn("x-1_delay_time", uids)
        hard = next(e for e in added if e._attr_unique_id == "x-1_water_hardness")
        self.assertEqual(hard.native_value, 3.0)
        bins = await _build_binary("DW", {
            "extraDry": "1", "halfLoad": "0", "openDoor": "1", "ecoExpress": "0",
        })
        buids = {e._attr_unique_id for e in bins}
        for k in ("extra_dry", "half_load", "auto_open_door", "eco_express"):
            self.assertIn(f"x-1_{k}", buids)
        extra = next(e for e in bins if e._attr_unique_id == "x-1_extra_dry")
        self.assertTrue(extra.is_on)

    async def test_washer_dose_sensors_and_option_binaries(self) -> None:
        added = await _build_sensors("WM", {
            "currentWashCycle": "2", "remainingRinseIterations": "1",
            "detergentPercent": "80", "haier_DetergentWeight": "35",
            "haier_SoftenerWeight": "20",
        })
        by_uid = {e._attr_unique_id: e for e in added}
        for k in ("current_wash_cycle", "remaining_rinses", "detergent_level",
                  "detergent_weight", "softener_weight"):
            self.assertIn(f"x-1_{k}", by_uid)
        self.assertEqual(by_uid["x-1_detergent_weight"].native_value, 35.0)
        self.assertEqual(by_uid["x-1_softener_weight"].native_value, 20.0)
        bins = {e._attr_unique_id: e for e in await _build_binary("WM", {
            "nightWashStatus": "1", "steamStatus": "0", "energySavingStatus": "1",
        })}
        self.assertTrue(bins["x-1_night_wash"].is_on)
        self.assertFalse(bins["x-1_steam"].is_on)
        self.assertTrue(bins["x-1_energy_saving"].is_on)

    async def test_wine_cooler_state_program_errors(self) -> None:
        added = await _build_sensors("WC", {
            "machMode": "2", "programName": "RED", "errors": "00",
        })
        by_uid = {e._attr_unique_id: e for e in added}
        self.assertEqual(by_uid["x-1_state"].native_value, "running")  # MACHINE_MODE_MAP
        self.assertEqual(by_uid["x-1_program_name"].native_value, "RED")
        self.assertEqual(by_uid["x-1_errors"].native_value, "00")

    async def test_wine_cooler_state_absent_when_unreported(self) -> None:
        uids = {e._attr_unique_id for e in await _build_sensors("WC", {})}
        for k in ("state", "program_name", "errors"):
            self.assertNotIn(f"x-1_{k}", uids)

    async def test_oven_program_duration(self) -> None:
        added = await _build_sensors("OV", {"prTime": "2700"})
        pd = next(e for e in added if e._attr_unique_id == "x-1_program_duration")
        self.assertEqual(pd.native_value, 2700.0)

    def test_oven_program_duration_is_seconds(self) -> None:
        # prTime is seconds (range 1..86395), NOT minutes.
        from homeassistant.components.sensor import SensorDeviceClass
        from homeassistant.const import UnitOfTime

        from custom_components.addhon.const import APPLIANCE_OV
        from custom_components.addhon.sensor import SENSORS

        pd = {d.key: d for d in SENSORS[APPLIANCE_OV]}["program_duration"]
        self.assertEqual(pd.native_unit_of_measurement, UnitOfTime.SECONDS)
        self.assertEqual(pd.device_class, SensorDeviceClass.DURATION)

    async def test_dishwasher_options_absent_when_unreported(self) -> None:
        uids = {e._attr_unique_id for e in await _build_sensors("DW", {})}
        for k in ("delay_time", "water_hardness"):
            self.assertNotIn(f"x-1_{k}", uids)
        buids = {e._attr_unique_id for e in await _build_binary("DW", {})}
        for k in ("extra_dry", "half_load", "auto_open_door", "eco_express"):
            self.assertNotIn(f"x-1_{k}", buids)

    async def test_washer_dose_absent_when_unreported(self) -> None:
        uids = {e._attr_unique_id for e in await _build_sensors("WM", {})}
        for k in ("current_wash_cycle", "remaining_rinses", "detergent_level",
                  "detergent_weight", "softener_weight"):
            self.assertNotIn(f"x-1_{k}", uids)
        buids = {e._attr_unique_id for e in await _build_binary("WM", {})}
        for k in ("night_wash", "steam", "energy_saving"):
            self.assertNotIn(f"x-1_{k}", buids)

    # --- Wave 1/2 harvest: estimated weight / remote control / mean water
    #     consumption (all gated) ---

    async def test_estimated_weight_gated_with_fallback(self) -> None:
        # actualWeight builds it; the `weight` fallback also builds it; absent when
        # neither is reported. WM and WD both carry it.
        for app_type in ("WM", "WD"):
            added = await _build_sensors(app_type, {"actualWeight": "3.5"})
            ew = next(e for e in added if e._attr_unique_id == "x-1_estimated_weight")
            self.assertEqual(ew.native_value, 3.5)
            added = await _build_sensors(app_type, {"weight": "4"})
            ew = next(e for e in added if e._attr_unique_id == "x-1_estimated_weight")
            self.assertEqual(ew.native_value, 4.0)  # reads the `weight` fallback
            uids = {e._attr_unique_id for e in await _build_sensors(app_type, {})}
            self.assertNotIn("x-1_estimated_weight", uids)

    def test_estimated_weight_device_class_kg(self) -> None:
        from homeassistant.components.sensor import SensorDeviceClass
        from homeassistant.const import UnitOfMass

        from custom_components.addhon.const import APPLIANCE_WM
        from custom_components.addhon.sensor import SENSORS

        d = {x.key: x for x in SENSORS[APPLIANCE_WM]}["estimated_weight"]
        self.assertEqual(d.device_class, SensorDeviceClass.WEIGHT)
        self.assertEqual(d.native_unit_of_measurement, UnitOfMass.KILOGRAMS)
        self.assertEqual(d.attr_fallbacks, ("weight",))

    async def test_remote_control_universal_gated(self) -> None:
        # Present on ANY type that reports remoteCtrValid (AC has no per-type set;
        # REF is a Tier-2 type) and absent otherwise. is_on follows "1".
        for app_type in ("AC", "REF"):
            added = await _build_binary(app_type, {"remoteCtrValid": "1"})
            rc = next(e for e in added if e._attr_unique_id == "x-1_remote_control")
            self.assertTrue(rc.is_on)
            uids = {e._attr_unique_id for e in await _build_binary(app_type, {})}
            self.assertNotIn("x-1_remote_control", uids)

    async def test_mean_water_consumption(self) -> None:
        # WM/WD only, gated on BOTH source attrs; value = water/(cycles-1).
        for app_type in ("WM", "WD"):
            added = await _build_sensors(
                app_type, {"totalWaterUsed": "100", "totalWashCycle": "6"})
            mw = next(e for e in added if e._attr_unique_id == "x-1_mean_water_consumption")
            self.assertEqual(mw.native_value, 20.0)  # 100 / (6-1)
        # <=0 denominator (first cycle) -> None, not a divide-by-zero
        added = await _build_sensors("WM", {"totalWaterUsed": "100", "totalWashCycle": "1"})
        mw = next(e for e in added if e._attr_unique_id == "x-1_mean_water_consumption")
        self.assertIsNone(mw.native_value)

    async def test_mean_water_absent_without_both_attrs_or_on_dryer(self) -> None:
        # Needs BOTH attrs; the tumble dryer never builds it (no water).
        for attrs in ({"totalWaterUsed": "100"}, {"totalWashCycle": "6"}):
            uids = {e._attr_unique_id for e in await _build_sensors("WM", attrs)}
            self.assertNotIn("x-1_mean_water_consumption", uids)
        uids = {e._attr_unique_id for e in await _build_sensors(
            "TD", {"totalWaterUsed": "100", "totalWashCycle": "6"})}
        self.assertNotIn("x-1_mean_water_consumption", uids)


if __name__ == "__main__":
    unittest.main()
