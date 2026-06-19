"""Cross-check: every entity translation_key used by the platforms exists in the
translations, and vice versa (exact set equality per platform, for en AND it).

The platforms (sensor/binary_sensor/number/switch/select/button) name their
entities via Home Assistant's translation_key system (has_entity_name + the
`entity.<platform>.<translation_key>.name` blocks in translations/{en,it}.json).
A typo or a missing JSON entry would silently leave an entity name blank in the
UI; no other test catches that. This test imports the platform description tables
under stubs, computes each effective translation_key (description.translation_key
or description.key, plus the fixed-key entities), and asserts it matches the JSON.
"""
from __future__ import annotations

import dataclasses
import json
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

COMPONENT = REPO_ROOT / "custom_components" / "addhon"


def _mod(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_stubs() -> None:
    ha = _mod("homeassistant")

    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))

    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))

    exc = _mod("homeassistant.exceptions")
    base_err = getattr(exc, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exc.HomeAssistantError = base_err
    exc.ConfigEntryNotReady = getattr(exc, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {}))
    exc.ConfigEntryAuthFailed = getattr(exc, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {}))

    helpers = _mod("homeassistant.helpers")
    entity = _mod("homeassistant.helpers.entity")
    entity.DeviceInfo = getattr(entity, "DeviceInfo", dict)
    ep = _mod("homeassistant.helpers.entity_platform")
    ep.AddEntitiesCallback = getattr(ep, "AddEntitiesCallback", object)
    er = _mod("homeassistant.helpers.entity_registry")
    er.async_get = getattr(er, "async_get", lambda hass: None)
    er.async_entries_for_config_entry = getattr(
        er, "async_entries_for_config_entry", lambda registry, entry_id: []
    )
    uc = _mod("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator

    uc.CoordinatorEntity = getattr(uc, "CoordinatorEntity", CoordinatorEntity)
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

    const = _mod("homeassistant.const")
    for unit_cls in ("UnitOfTemperature", "UnitOfEnergy", "UnitOfTime", "UnitOfVolume"):
        if not hasattr(const, unit_cls):
            setattr(const, unit_cls, type(unit_cls, (), {
                "CELSIUS": "C", "KILO_WATT_HOUR": "kWh", "MINUTES": "min", "LITERS": "L",
            }))

    components = _mod("homeassistant.components")

    # sensor platform
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

    sensor_mod.SensorEntityDescription = getattr(sensor_mod, "SensorEntityDescription", SensorEntityDescription)
    sensor_mod.SensorEntity = getattr(sensor_mod, "SensorEntity", type("SensorEntity", (), {}))
    sensor_mod.SensorDeviceClass = getattr(sensor_mod, "SensorDeviceClass", type("SensorDeviceClass", (), {
        "TEMPERATURE": "temperature", "HUMIDITY": "humidity", "ENERGY": "energy",
        "WATER": "water", "DURATION": "duration", "PM25": "pm25", "CO2": "co2",
        "BATTERY": "battery", "POWER": "power",
    }))
    sensor_mod.SensorStateClass = getattr(sensor_mod, "SensorStateClass", type("SensorStateClass", (), {
        "MEASUREMENT": "measurement", "TOTAL": "total", "TOTAL_INCREASING": "total_increasing",
    }))

    # binary_sensor platform
    binary_mod = _mod("homeassistant.components.binary_sensor")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class BinarySensorEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        device_class: object | None = None

    binary_mod.BinarySensorEntityDescription = getattr(binary_mod, "BinarySensorEntityDescription", BinarySensorEntityDescription)
    binary_mod.BinarySensorEntity = getattr(binary_mod, "BinarySensorEntity", type("BinarySensorEntity", (), {}))
    binary_mod.BinarySensorDeviceClass = getattr(binary_mod, "BinarySensorDeviceClass", type("BinarySensorDeviceClass", (), {
        "DOOR": "door", "PROBLEM": "problem", "RUNNING": "running",
        "OCCUPANCY": "occupancy", "LIGHT": "light", "CONNECTIVITY": "connectivity",
    }))

    # number platform
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

    number_mod.NumberEntityDescription = getattr(number_mod, "NumberEntityDescription", NumberEntityDescription)
    number_mod.NumberEntity = getattr(number_mod, "NumberEntity", type("NumberEntity", (), {}))
    number_mod.NumberDeviceClass = getattr(number_mod, "NumberDeviceClass", type("NumberDeviceClass", (), {"TEMPERATURE": "temperature"}))
    number_mod.NumberMode = getattr(number_mod, "NumberMode", type("NumberMode", (), {"AUTO": "auto", "BOX": "box", "SLIDER": "slider"}))

    # switch / select / button
    _mod("homeassistant.components.switch").SwitchEntity = type("SwitchEntity", (), {})
    _mod("homeassistant.components.select").SelectEntity = type("SelectEntity", (), {})
    _mod("homeassistant.components.button").ButtonEntity = type("ButtonEntity", (), {})

    ha.config_entries = ce
    ha.core = core
    ha.exceptions = exc
    ha.helpers = helpers
    ha.const = const
    ha.components = components
    helpers.entity = entity
    helpers.entity_platform = ep
    helpers.entity_registry = er
    helpers.update_coordinator = uc
    components.sensor = sensor_mod
    components.binary_sensor = binary_mod
    components.number = number_mod


_install_stubs()


def _tk(description) -> str:
    return getattr(description, "translation_key", None) or description.key


def _collect_code_keys() -> dict[str, set[str]]:
    from custom_components.addhon import binary_sensor, number, sensor, switch

    used: dict[str, set[str]] = {}

    used["sensor"] = {
        _tk(d) for descs in sensor.SENSORS.values() for d in descs
    }
    used["binary_sensor"] = {
        _tk(d) for descs in binary_sensor.BINARY_SENSORS.values() for d in descs
    }
    used["binary_sensor"].add(_tk(binary_sensor._CONNECTIVITY))
    used["number"] = {
        _tk(d) for descs in number.NUMBERS.values() for d in descs
    }
    # HonAcSwitch names from description.key; the pause switch uses a fixed key.
    used["switch"] = {d.key for d in switch._AC_SWITCHES} | {"pause"}
    # Fixed-key entities (no description table).
    used["select"] = {"program"}
    used["button"] = {"start_program", "stop_program"}
    return used


def _load_entity_block(lang: str) -> dict[str, set[str]]:
    data = json.loads((COMPONENT / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
    entity = data.get("entity", {})
    return {platform: set(keys) for platform, keys in entity.items()}


class EntityTranslationKeyTest(unittest.TestCase):
    def test_code_keys_match_translations_exactly(self) -> None:
        used = _collect_code_keys()
        for lang in ("en", "it"):
            block = _load_entity_block(lang)
            for platform, code_keys in used.items():
                json_keys = block.get(platform, set())
                missing = code_keys - json_keys
                extra = json_keys - code_keys
                self.assertFalse(
                    missing,
                    f"[{lang}] entity.{platform}: translation_keys used in code but "
                    f"missing from translations: {sorted(missing)}",
                )
                self.assertFalse(
                    extra,
                    f"[{lang}] entity.{platform}: translation_keys in translations but "
                    f"never used by the code: {sorted(extra)}",
                )

    def test_no_platform_only_in_one_language(self) -> None:
        self.assertEqual(set(_load_entity_block("en")), set(_load_entity_block("it")))


if __name__ == "__main__":
    unittest.main()
