# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Laundry Centre (LC, #124): the washer and dryer child devices and their tables.

An LC is one MAC carrying a washer (`WM`) and a dryer (`TD`). The pure helpers of
`laundry_centre` are tested here; the `_build` runner and fakes are shared by the
entity tests of the later tasks. Stdlib unittest on the shared conftest stubs.
"""
from __future__ import annotations

import dataclasses
import enum
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
    """Seed what conftest does not; getattr-guarded so the first module wins."""
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
    sensor_mod.SensorEntity = getattr(
        sensor_mod, "SensorEntity", type("SensorEntity", (), {})
    )

    binary_mod = _mod("homeassistant.components.binary_sensor")
    components.binary_sensor = binary_mod
    binary_mod.BinarySensorEntity = getattr(
        binary_mod, "BinarySensorEntity", type("BinarySensorEntity", (), {})
    )

    button_mod = _mod("homeassistant.components.button")
    components.button = button_mod
    button_mod.ButtonEntity = getattr(
        button_mod, "ButtonEntity", type("ButtonEntity", (), {})
    )

    climate_mod = _mod("homeassistant.components.climate")
    components.climate = climate_mod
    climate_mod.ClimateEntity = getattr(
        climate_mod, "ClimateEntity", type("ClimateEntity", (), {})
    )
    climate_const = _mod("homeassistant.components.climate.const")
    climate_mod.const = climate_const
    climate_const.ClimateEntityFeature = getattr(
        climate_const,
        "ClimateEntityFeature",
        enum.IntFlag(
            "ClimateEntityFeature",
            {"TARGET_TEMPERATURE": 1, "FAN_MODE": 2, "TURN_ON": 4, "TURN_OFF": 8},
        ),
    )
    climate_const.HVACMode = getattr(
        climate_const,
        "HVACMode",
        enum.Enum(
            "HVACMode",
            {"OFF": "off", "AUTO": "auto", "COOL": "cool", "DRY": "dry", "HEAT": "heat",
             "FAN_ONLY": "fan_only"},
            type=str,
        ),
    )

    water_heater_mod = _mod("homeassistant.components.water_heater")
    components.water_heater = water_heater_mod
    water_heater_mod.WaterHeaterEntity = getattr(
        water_heater_mod, "WaterHeaterEntity", type("WaterHeaterEntity", (), {})
    )
    water_heater_mod.WaterHeaterEntityFeature = getattr(
        water_heater_mod,
        "WaterHeaterEntityFeature",
        enum.IntFlag(
            "WaterHeaterEntityFeature",
            {"TARGET_TEMPERATURE": 1, "OPERATION_MODE": 2, "AWAY_MODE": 4, "ON_OFF": 8},
        ),
    )
    for name, value in (
        ("STATE_ECO", "eco"), ("STATE_ELECTRIC", "electric"),
        ("STATE_HEAT_PUMP", "heat_pump"), ("STATE_OFF", "off"),
    ):
        setattr(water_heater_mod, name, getattr(water_heater_mod, name, value))

    ha_const = _mod("homeassistant.const")
    ha_const.ATTR_TEMPERATURE = getattr(ha_const, "ATTR_TEMPERATURE", "temperature")

    entity_platform = _mod("homeassistant.helpers.entity_platform")
    sys.modules["homeassistant.helpers"].entity_platform = entity_platform
    entity_platform.AddEntitiesCallback = getattr(
        entity_platform, "AddEntitiesCallback", object
    )


_install_platform_stubs()

from custom_components.addhon import const, laundry_centre  # noqa: E402
from custom_components.addhon.const import APPLIANCE_LC, DOMAIN  # noqa: E402

LC_ATTRIBUTES = {
    "available": True,
    "totalElectricityUsed": 43.17,
    "currentElectricityUsed": 1.12,
    "detergentPercent": 70,
    "haier_DetergentWeight": 35,
    "haier_SoftenerWeight": 1,
    "WMmachMode": 2,
    "WMremainingTimeMM": 31,
    "WMremainingTimeHH": 0,
    "WMprPhase": 5,
    "WMerrors": 0,
    "WMtotalWashCycle": 18,
    "WMtotalWaterUsed": 3192,
    "WMcurrentWaterUsed": 31,
    "WMcurrentWashCycle": 11,
    "TDmachMode": 3,
    "TDremainingTimeMM": 1,
    "TDprPhase": 7,
    "TDerrors": 0,
    "TDantiCreaseTime": 117,
    "WMremoteCtrValid": 0,
    "WMdoorStatus": 0,
    "WMdoorLockStatus": 1,
    "WMpause": 0,
    "autoDetergentStatus": 1,
    "autoSoftenerStatus": 1,
    "TDremoteCtrValid": 0,
    "TDpause": 1,
    # Derived by the engine's LC layer (Task 1).
    "WMprogramName": "hqd_wm_smart",
    "TDprogramName": "hqd_td_smart_ai",
}
DATA = {
    "mac-1": {
        "type": "LC",
        "name": "CLC108GNU1",
        "model": "CLC108GNU1",
        "attributes": LC_ATTRIBUTES,
        "settings": {},
        "statistics": {},
    }
}


# Washer-only shadow keys that carry no `WM` prefix (the dosing unit).
_UNPREFIXED_WASHER_KEYS = frozenset(
    {
        "detergentPercent", "haier_DetergentWeight", "haier_SoftenerWeight",
        "autoDetergentStatus", "autoSoftenerStatus",
    }
)


def _belongs_to_unit(key: str, unit: str) -> bool:
    return key.startswith(unit) or (unit == "WM" and key in _UNPREFIXED_WASHER_KEYS)


def _engine_attributes(gone: str) -> dict:
    """The attributes the real LC layer derives for a shadow that lacks one whole unit.

    Only SHADOW keys of the unit are stripped; the derived `<U>programName` is left to
    the engine, so the test fails if the layer invents a name for a unit that is not there.
    """
    from types import SimpleNamespace

    from custom_components.addhon.client.engine.appliances import lc
    from custom_components.addhon.client.engine.attributes import HonAttribute

    shadow = {
        k: HonAttribute({"parNewVal": v})
        for k, v in LC_ATTRIBUTES.items()
        if not k.endswith("programName") and not _belongs_to_unit(k, gone)
    }
    shadow["WMprCode"] = HonAttribute({"parNewVal": "0"}) if gone != "WM" else None
    shadow["TDprCode"] = HonAttribute({"parNewVal": "0"}) if gone != "TD" else None
    shadow = {k: v for k, v in shadow.items() if v is not None}
    parent = SimpleNamespace(settings={}, connection=True, appliance_type="LC")
    derived = lc.Appliance(parent).attributes({"parameters": shadow})
    attrs = {k: v for k, v in LC_ATTRIBUTES.items() if not _belongs_to_unit(k, gone)}
    attrs.update({k: v for k, v in derived.items() if k.endswith("programName")})
    return attrs


def _lc_appliance():
    """A fake LC appliance whose commands carry everything a command builder looks for."""
    from custom_components.addhon.client.engine.commands import HonCommand

    def rng(low, high, step=1, default=None):
        return {
            "typology": "range", "category": "command", "mandatory": 0,
            "minimumValue": str(low), "maximumValue": str(high),
            "incrementValue": str(step), "defaultValue": str(default if default is not None else low),
        }

    def enum(values, default=None):
        return {
            "typology": "enum", "category": "command", "mandatory": 0,
            "enumValues": [str(v) for v in values],
            "defaultValue": str(default if default is not None else values[0]),
        }

    def fixed(value):
        return {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": str(value)}

    appliance = types.SimpleNamespace(
        commands={}, unique_id="mac-1", options={}, attributes={"parameters": {}},
        model_attributes={}, connection=True, appliance_type="LC",
        sync_command_to_params=lambda name: None,
    )
    settings = {
        "machMode": enum([0, 1, 2, 3]),
        "tempSel": rng(10, 40),
        "spinSpeed": rng(0, 1400, 100),
        "lockStatus": enum([0, 1]),
        "delayTime": rng(0, 1440),
        "onOffStatus": enum([0, 1]),
        "windSpeed": rng(0, 5),
        "lightStatus": enum([0, 1]),
    }
    appliance.commands["settings"] = HonCommand("settings", {"parameters": settings}, appliance)
    start_params = {"onOffStatus": fixed(1), "program": enum(["a", "b"]), **settings}
    categories: dict = {}
    start = HonCommand(
        "startProgram", {"parameters": start_params}, appliance,
        categories=categories, category_name="iot_wash_rapid_30",
    )
    categories["iot_wash_rapid_30"] = start
    categories["hqd_td_smart_ai"] = HonCommand(
        "startProgram", {"parameters": dict(start_params)}, appliance,
        categories=categories, category_name="hqd_td_smart_ai",
    )
    appliance.commands["startProgram"] = start
    for name in ("stopProgram", "pauseProgram", "resumeProgram"):
        appliance.commands[name] = HonCommand(
            name, {"parameters": {"onOffStatus": fixed(0)}}, appliance
        )
    return appliance


class RefreshingCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.last_update_success = True

    async def async_refresh(self) -> None:
        pass


class RecordingHass:
    def __init__(self, data: dict) -> None:
        self.data = data


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1", options: dict | None = None) -> None:
        self.entry_id = entry_id
        self.options = dict(options or {})


class FakeDeviceRegistry:
    def __init__(self) -> None:
        self.created: list[dict] = []

    def async_get_or_create(self, **kwargs):
        self.created.append(kwargs)
        return types.SimpleNamespace(id="dev")


def _lc(attributes: dict | None = None, **extra) -> dict:
    return {
        "type": APPLIANCE_LC,
        "name": "CLC108GNU1",
        "model": "CLC108GNU1",
        "attributes": dict(attributes or {}),
        "settings": {},
        "statistics": {},
        **extra,
    }


async def _build(platform_name: str, data: dict, client=None, options=None) -> list:
    """Run a platform's `async_setup_entry` on `data` and return its entities."""
    from importlib import import_module

    platform = import_module(f"custom_components.addhon.{platform_name}")
    coordinator = RefreshingCoordinator(data)
    hass = RecordingHass(
        {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": client}}}
    )
    added: list = []
    await platform.async_setup_entry(hass, FakeEntry(options=options), added.extend)
    for entity in added:
        entity.hass = hass
    return [e for e in added if not getattr(e, "_addhon_account", False)]


class UnitDeviceTest(unittest.TestCase):
    def test_the_lc_type_code(self) -> None:
        self.assertEqual("LC", APPLIANCE_LC)
        self.assertEqual(("WM", "TD"), laundry_centre.LC_UNITS)

    def test_unit_device_info_hangs_off_the_lc(self) -> None:
        data = {"name": "CLC108GNU1", "model": "CLC108GNU1"}
        washer = laundry_centre.unit_device_info("mac-1", "WM", data)
        self.assertEqual({(DOMAIN, "mac-1_WM")}, washer["identifiers"])
        self.assertEqual((DOMAIN, "mac-1"), washer["via_device"])
        self.assertEqual("lc_washer", washer["translation_key"])
        self.assertEqual({"name": "CLC108GNU1"}, washer["translation_placeholders"])
        self.assertEqual(("Haier", "CLC108GNU1"), (washer["manufacturer"], washer["model"]))
        self.assertNotIn("name", washer)

        dryer = laundry_centre.unit_device_info("mac-1", "TD", data)
        self.assertEqual({(DOMAIN, "mac-1_TD")}, dryer["identifiers"])
        self.assertEqual("lc_dryer", dryer["translation_key"])
        self.assertEqual((DOMAIN, "mac-1"), dryer["via_device"])

    def test_unit_device_info_defaults_like_the_base_entity(self) -> None:
        info = laundry_centre.unit_device_info("mac-1", "WM", {})
        self.assertEqual({"name": "Haier Appliance"}, info["translation_placeholders"])
        self.assertEqual("Unknown", info["model"])

    def test_unit_device_id(self) -> None:
        self.assertEqual("mac-1_TD", laundry_centre.unit_device_id("mac-1", "TD"))

    def test_the_base_entity_uses_the_unit_device_only_when_a_unit_is_set(self) -> None:
        from custom_components.addhon.base_entity import HonBaseEntity

        coordinator = RefreshingCoordinator({"mac-1": _lc()})
        entity = HonBaseEntity(coordinator, "mac-1")
        self.assertIsNone(entity._unit)
        plain = entity.device_info
        self.assertEqual({(DOMAIN, "mac-1")}, plain["identifiers"])
        self.assertEqual("CLC108GNU1", plain["name"])
        self.assertNotIn("via_device", plain)

        entity._unit = "TD"
        self.assertEqual(
            laundry_centre.unit_device_info("mac-1", "TD", coordinator.data["mac-1"]),
            entity.device_info,
        )


class LiveUnitDevicesTest(unittest.TestCase):
    def test_lc_parent_id_resolves_only_unit_devices_of_an_lc(self) -> None:
        data = {"mac-1": _lc(), "x": {"type": "WM"}, "a_b": _lc()}
        resolve = laundry_centre.lc_parent_id
        self.assertEqual("mac-1", resolve("mac-1_WM", data))
        self.assertEqual("mac-1", resolve("mac-1_TD", data))
        self.assertEqual("a_b", resolve("a_b_TD", data))
        self.assertIsNone(resolve("x_WM", data))
        self.assertIsNone(resolve("mac-1", data))
        self.assertIsNone(resolve("mac-1_XX", data))
        self.assertIsNone(resolve("absent_WM", data))
        self.assertIsNone(resolve("mac-1_WM", None))
        self.assertIsNone(resolve("mac-1_WM", []))
        self.assertIsNone(resolve(None, data))

    def test_unit_devices_are_live_only_with_their_lc(self) -> None:
        both = {"WMmachMode": 2, "TDmachMode": 3}
        self.assertEqual(
            {"mac-1_WM", "mac-1_TD"},
            laundry_centre.live_unit_device_ids(
                {"mac-1": {"type": "LC", "attributes": both}, "mac-2": {"type": "WM"}}
            ),
        )

    def test_a_unit_without_keys_is_not_live(self) -> None:
        # The bare `WM` / `TD` keys are the per-unit statistics, there whatever the
        # shadow reports: they do not keep a silent unit's device alive.
        wm_only = {"WMmachMode": 2, "WM": {}, "TD": {}, "totalElectricityUsed": 1}
        self.assertEqual(
            {"mac-1_WM"},
            laundry_centre.live_unit_device_ids({"mac-1": {"type": "LC", "attributes": wm_only}}),
        )
        self.assertEqual(
            set(), laundry_centre.live_unit_device_ids({"mac-1": {"type": "LC"}})
        )
        self.assertEqual(("TD",), laundry_centre.reported_units({"TDpause": 1, "WM": {}}))

    def test_no_snapshot_no_unit_devices(self) -> None:
        self.assertEqual(set(), laundry_centre.live_unit_device_ids(None))
        self.assertEqual(set(), laundry_centre.live_unit_device_ids({"m": "junk"}))


class ParentRegistrationTest(unittest.TestCase):
    def test_parents_are_registered_before_the_platforms(self) -> None:
        reg = FakeDeviceRegistry()
        count = laundry_centre.register_parent_devices(
            reg,
            "entry-1",
            {
                "mac-1": {"type": "LC", "name": "CLC108GNU1", "model": "CLC108GNU1"},
                "mac-2": {"type": "WM"},
            },
        )
        self.assertEqual(1, count)
        self.assertEqual(
            [
                dict(
                    config_entry_id="entry-1",
                    identifiers={(DOMAIN, "mac-1")},
                    name="CLC108GNU1",
                    manufacturer="Haier",
                    model="CLC108GNU1",
                )
            ],
            reg.created,
        )

    def test_no_lc_registers_nothing(self) -> None:
        reg = FakeDeviceRegistry()
        self.assertEqual(0, laundry_centre.register_parent_devices(reg, "e", {"m": {"type": "WM"}}))
        self.assertEqual(0, laundry_centre.register_parent_devices(reg, "e", None))
        self.assertEqual([], reg.created)


class PhaseTablesTest(unittest.TestCase):
    def test_the_lc_phase_tables_are_the_apps(self) -> None:
        wm = const.LC_WASHING_PHASE_MAP
        self.assertEqual("ready", wm["0"])
        self.assertEqual("washing", wm["5"])
        self.assertEqual("spinning", wm["7"])
        self.assertEqual("tumbling", wm["16"])
        self.assertEqual("steam", wm["20"])
        self.assertEqual("washing", wm["26"])
        self.assertNotIn("27", wm)
        self.assertEqual(set(map(str, range(27))), set(wm))

        td = const.LC_TUMBLE_DRYER_PHASE_MAP
        self.assertEqual("heating", td["3"])
        self.assertEqual("cooling", td["7"])
        self.assertEqual("drying", td["11"])
        self.assertNotIn("1", td)
        self.assertEqual({"0", "2", "3", "4", "5", "7", "8", "9", "10", "11"}, set(td))


class LcSensorsTest(unittest.IsolatedAsyncioTestCase):
    async def _by_id(self, data=None) -> dict:
        return {e._attr_unique_id: e for e in await _build("sensor", data or DATA)}

    async def test_the_lc_gets_exactly_these_sensors(self) -> None:
        ids = {e._attr_unique_id for e in await _build("sensor", DATA)}
        self.assertEqual(
            {
                "mac-1_total_energy", "mac-1_current_energy",
                "mac-1_WM_state", "mac-1_WM_remaining_time", "mac-1_WM_program_name",
                "mac-1_WM_program_phase", "mac-1_WM_errors", "mac-1_WM_total_washes",
                "mac-1_WM_total_water", "mac-1_WM_current_water",
                "mac-1_WM_current_wash_cycle", "mac-1_WM_detergent_level",
                "mac-1_WM_detergent_weight", "mac-1_WM_softener_weight",
                "mac-1_TD_state", "mac-1_TD_remaining_time", "mac-1_TD_program_name",
                "mac-1_TD_program_phase", "mac-1_TD_errors", "mac-1_TD_anti_crease_time",
            },
            ids,
        )

    async def test_values_on_the_dump(self) -> None:
        by_id = await self._by_id()
        expected = {
            "mac-1_WM_state": "running",
            "mac-1_WM_remaining_time": 31,
            "mac-1_WM_program_phase": "washing",
            "mac-1_WM_errors": "0",
            "mac-1_WM_total_water": 3192,
            "mac-1_WM_current_water": 31,
            "mac-1_WM_total_washes": 18,
            "mac-1_WM_current_wash_cycle": 11,
            "mac-1_WM_detergent_level": 70,
            "mac-1_WM_detergent_weight": 35,
            "mac-1_WM_softener_weight": 1,
            "mac-1_TD_state": "paused",
            "mac-1_TD_remaining_time": 1,
            "mac-1_TD_program_phase": "cooling",
            "mac-1_TD_errors": "0",
            "mac-1_TD_anti_crease_time": 117,
            "mac-1_total_energy": 43.17,
            "mac-1_current_energy": 1.12,
        }
        for unique_id, value in expected.items():
            with self.subTest(unique_id):
                self.assertEqual(value, by_id[unique_id].native_value)

    async def test_unit_entities_sit_on_the_child_devices(self) -> None:
        for unique_id, entity in (await self._by_id()).items():
            if unique_id.startswith("mac-1_WM_"):
                expected = {(DOMAIN, "mac-1_WM")}
            elif unique_id.startswith("mac-1_TD_"):
                expected = {(DOMAIN, "mac-1_TD")}
            else:
                expected = {(DOMAIN, "mac-1")}
            with self.subTest(unique_id):
                self.assertEqual(expected, entity.device_info["identifiers"])

    def _with_catalogue(self, entities, catalog):
        from custom_components.addhon import program_labels

        for entity in entities:
            setattr(
                entity.coordinator,
                program_labels.COORDINATOR_ATTR,
                program_labels.ProgramLabels(catalog),
            )

    async def test_program_names_are_translated_through_the_remap(self) -> None:
        # A copy: the last step rewrites a programme name, and the shared fixture must
        # read the same for every test, and for this one when it is run again.
        data = {"mac-1": _lc(dict(LC_ATTRIBUTES))}
        entities = await _build("sensor", data)
        by_id = {e._attr_unique_id: e for e in entities}
        wm, td = by_id["mac-1_WM_program_name"], by_id["mac-1_TD_program_name"]
        self.assertEqual("hqd_wm_smart", wm.native_value)
        self.assertEqual("hqd_td_smart_ai", td.native_value)
        self._with_catalogue(
            entities, {"WM_WD": {"HQD_SMART": "Smart"}, "TD": {"HQD_SMART_AI": "Smart A.I."}}
        )
        self.assertEqual("Smart", wm.native_value)
        self.assertEqual("Smart A.I.", td.native_value)
        data["mac-1"]["attributes"]["WMprogramName"] = "No Program"
        self.assertEqual("No Program", wm.native_value)
        self.assertEqual("hqd_wm_smart", LC_ATTRIBUTES["WMprogramName"])

    async def test_remaining_time_tolerates_partial_and_garbage_halves(self) -> None:
        data = {"mac-1": _lc(dict(LC_ATTRIBUTES))}
        entities = await _build("sensor", data)
        sensor = {e._attr_unique_id: e for e in entities}["mac-1_WM_remaining_time"]
        attrs = data["mac-1"]["attributes"]
        for hours, minutes, expected in (
            (1, 5, 65),
            (2, None, 120),
            ("", None, None),
            (None, "x", None),
            ("nan", 5, None),
            (1, "inf", None),
            ("-inf", None, None),
            (10**400, 5, None),
            (1, 10**400, None),
            (0, 31, 31),
        ):
            attrs.pop("WMremainingTimeHH", None)
            attrs.pop("WMremainingTimeMM", None)
            if hours is not None:
                attrs["WMremainingTimeHH"] = hours
            if minutes is not None:
                attrs["WMremainingTimeMM"] = minutes
            with self.subTest(hours=hours, minutes=minutes):
                self.assertEqual(expected, sensor.native_value)

    async def test_an_hour_half_alone_creates_the_remaining_time(self) -> None:
        attributes = dict(LC_ATTRIBUTES)
        attributes.pop("WMremainingTimeMM")
        attributes["WMremainingTimeHH"] = 1
        entities = await _build("sensor", {"mac-1": _lc(attributes)})
        sensor = {e._attr_unique_id: e for e in entities}["mac-1_WM_remaining_time"]
        self.assertEqual(60, sensor.native_value)

    def test_the_lc_phase_functions(self) -> None:
        from custom_components.addhon import sensor as sensor_module

        self.assertEqual("washing", sensor_module.lc_wash_phase(5))
        self.assertIsNone(sensor_module.lc_wash_phase("27"))
        self.assertIsNone(sensor_module.lc_wash_phase(None))
        self.assertIsNone(sensor_module.lc_wash_phase("x"))
        self.assertEqual("heating", sensor_module.lc_dry_phase(3))
        self.assertIsNone(sensor_module.lc_dry_phase(1))

    async def test_a_missing_unit_creates_no_entity_for_it(self) -> None:
        for gone, kept in (("TD", "WM"), ("WM", "TD")):
            attrs = _engine_attributes(gone)
            data = {"mac-1": _lc(attrs)}
            ids = {e._attr_unique_id for e in await _build("sensor", data)}
            with self.subTest(gone=gone):
                self.assertFalse([i for i in ids if i.startswith(f"mac-1_{gone}_")])
                self.assertIn(f"mac-1_{kept}_state", ids)
                devices = {
                    next(iter(e.device_info["identifiers"]))[1]
                    for e in await _build("sensor", data)
                }
                self.assertNotIn(f"mac-1_{gone}", devices)

    async def test_existing_washer_ids_and_device_are_unchanged(self) -> None:
        data = {
            "wm_TD": {
                "type": "WM",
                "name": "Washer",
                "model": "HW80",
                "attributes": {"machMode": 2, "remainingTimeMM": 5},
                "settings": {},
                "statistics": {},
            }
        }
        entities = await _build("sensor", data)
        by_id = {e._attr_unique_id: e for e in entities}
        self.assertIn("wm_TD_state", by_id)
        self.assertIn("wm_TD_remaining_time", by_id)
        for entity in entities:
            info = entity.device_info
            self.assertEqual({(DOMAIN, "wm_TD")}, info["identifiers"])
            self.assertNotIn("via_device", info)


class LcBinarySensorsTest(unittest.IsolatedAsyncioTestCase):
    async def _by_id(self, data=None) -> dict:
        return {e._attr_unique_id: e for e in await _build("binary_sensor", data or DATA)}

    async def test_the_lc_gets_exactly_these_binary_sensors(self) -> None:
        ids = {e._attr_unique_id for e in await _build("binary_sensor", DATA)}
        self.assertEqual(
            {
                "mac-1_connectivity",
                "mac-1_WM_remote_control", "mac-1_WM_door_open", "mac-1_WM_door_lock",
                "mac-1_WM_pause", "mac-1_WM_auto_detergent", "mac-1_WM_auto_softener",
                "mac-1_TD_remote_control", "mac-1_TD_pause",
            },
            ids,
        )

    async def test_binary_values_on_the_dump(self) -> None:
        by_id = await self._by_id()
        expected = {
            "mac-1_WM_remote_control": False,
            "mac-1_WM_door_open": False,
            "mac-1_WM_door_lock": True,
            "mac-1_WM_pause": False,
            "mac-1_WM_auto_detergent": True,
            "mac-1_WM_auto_softener": True,
            "mac-1_TD_remote_control": False,
            "mac-1_TD_pause": True,
            "mac-1_connectivity": True,
        }
        for unique_id, value in expected.items():
            with self.subTest(unique_id):
                self.assertIs(value, by_id[unique_id].is_on)

    async def test_unit_binaries_sit_on_the_child_devices(self) -> None:
        for unique_id, entity in (await self._by_id()).items():
            if unique_id.startswith("mac-1_WM_"):
                expected = {(DOMAIN, "mac-1_WM")}
            elif unique_id.startswith("mac-1_TD_"):
                expected = {(DOMAIN, "mac-1_TD")}
            else:
                expected = {(DOMAIN, "mac-1")}
            with self.subTest(unique_id):
                self.assertEqual(expected, entity.device_info["identifiers"])

    async def test_the_universal_remote_control_stays_absent_on_the_lc(self) -> None:
        self.assertNotIn("mac-1_remote_control", await self._by_id())

    async def test_a_missing_unit_creates_no_binary_for_it(self) -> None:
        for gone, kept in (("TD", "WM"), ("WM", "TD")):
            data = {"mac-1": _lc(_engine_attributes(gone))}
            entities = await _build("binary_sensor", data)
            ids = {e._attr_unique_id for e in entities}
            with self.subTest(gone=gone):
                self.assertFalse([i for i in ids if i.startswith(f"mac-1_{gone}_")])
                self.assertIn(f"mac-1_{kept}_pause", ids)
                devices = {next(iter(e.device_info["identifiers"]))[1] for e in entities}
                self.assertNotIn(f"mac-1_{gone}", devices)


class LcDiagnosticsTest(unittest.IsolatedAsyncioTestCase):
    def test_coverage_maps_the_lc_keys(self) -> None:
        from custom_components.addhon import diagnostics

        mapped_attrs, _params, sources, missing = diagnostics._mapped_sets("LC")
        self.assertFalse(missing)
        for key in (
            "WMmachMode", "WMremainingTimeMM", "WMremainingTimeHH", "TDprPhase",
            "TDantiCreaseTime", "totalElectricityUsed", "autoDetergentStatus",
        ):
            self.assertIn(key, mapped_attrs)
        for row in (
            "sensor.WM_state", "sensor.TD_state", "sensor.total_energy",
            "binary_sensor.WM_pause", "binary_sensor.TD_pause",
            "binary_sensor.connectivity",
        ):
            self.assertIn(row, sources)
        self.assertNotIn("sensor.state", sources)
        self.assertFalse(
            [r for r in sources if r.split(".")[0] in {"select", "switch", "number", "button"}]
        )

    async def test_no_command_entity_for_the_lc(self) -> None:
        """Nine command platforms walk their builders over a snapshot that has commands."""
        data = {"mac-1": _lc(dict(LC_ATTRIBUTES), appliance=_lc_appliance())}
        platforms = (
            "select", "button", "switch", "number", "climate", "fan", "water_heater",
            "date", "time",
        )
        for platform in platforms:
            with self.subTest(platform):
                self.assertEqual([], await _build(platform, data), platform)
        # Control: the same commands on a washer do give command entities, so the
        # emptiness above is the LC's, not the fake's.
        washer = {"mac-1": _lc(dict(LC_ATTRIBUTES), appliance=_lc_appliance(), type="WM")}
        for platform in ("select", "button", "switch", "number"):
            with self.subTest(control=platform):
                self.assertTrue(await _build(platform, washer), platform)


if __name__ == "__main__":
    unittest.main()
