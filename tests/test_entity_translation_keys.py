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
import re
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
    dr = _mod("homeassistant.helpers.device_registry")
    dr.DeviceEntryType = getattr(
        dr, "DeviceEntryType", type("DeviceEntryType", (), {"SERVICE": "service"})
    )
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
    for unit_cls in ("UnitOfTemperature", "UnitOfEnergy", "UnitOfTime", "UnitOfVolume", "UnitOfMass"):
        if not hasattr(const, unit_cls):
            setattr(const, unit_cls, type(unit_cls, (), {
                "CELSIUS": "C", "KILO_WATT_HOUR": "kWh", "MINUTES": "min", "LITERS": "L",
                "GRAMS": "g", "KILOGRAMS": "kg", "SECONDS": "s",
            }))
    const.EntityCategory = getattr(
        const, "EntityCategory", type("EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"})
    )

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
        options: object | None = None

    sensor_mod.SensorEntityDescription = getattr(sensor_mod, "SensorEntityDescription", SensorEntityDescription)
    sensor_mod.SensorEntity = getattr(sensor_mod, "SensorEntity", type("SensorEntity", (), {}))
    sensor_mod.SensorDeviceClass = getattr(sensor_mod, "SensorDeviceClass", type("SensorDeviceClass", (), {
        "TEMPERATURE": "temperature", "HUMIDITY": "humidity", "ENERGY": "energy",
        "WATER": "water", "DURATION": "duration", "PM25": "pm25", "CO2": "co2",
        "PM10": "pm10", "CO": "carbon_monoxide", "AQI": "aqi",
        "VOLATILE_ORGANIC_COMPOUNDS_PARTS": "volatile_organic_compounds_parts",
        "WEIGHT": "weight",
        "BATTERY": "battery", "POWER": "power", "ENUM": "enum",
        "TIMESTAMP": "timestamp",
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
        "HEAT": "heat",
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
    helpers.device_registry = dr
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
    # Derived custom-class sensor (not a description-table row).
    used["sensor"].add(sensor.HonMeanWaterConsumption._attr_translation_key)
    # Account-level diagnostic sensors (fixed-key, not in the per-type table).
    used["sensor"].update(
        {"debug_status", "integration_log_level", "mqtt_log_level",
         "appliances_discovered", "last_refresh"}
    )
    used["binary_sensor"] = {
        _tk(d) for descs in binary_sensor.BINARY_SENSORS.values() for d in descs
    }
    used["binary_sensor"].add(_tk(binary_sensor._CONNECTIVITY))
    # Universal capability-gated binaries (not in the per-type table).
    for d in binary_sensor._UNIVERSAL_GATED:
        used["binary_sensor"].add(_tk(d))
    # Account-level diagnostic binary (fixed key).
    used["binary_sensor"].add("update_ok")
    used["number"] = {
        _tk(d) for descs in number.NUMBERS.values() for d in descs
    }
    # HonAcSwitch names from description.key; the pause + debug switches use fixed keys.
    used["switch"] = {d.key for d in switch._AC_SWITCHES} | {
        "pause", "debug_logging", "mqtt_realtime_debug"
    }
    # Fixed-key entities (no description table).
    used["select"] = {"program"}
    used["button"] = {"start_program", "stop_program", "force_refresh", "reset_debug"}
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


def _code_translation_key_literals() -> set[str]:
    """Every `translation_key="..."` / `_attr_translation_key = "..."` literal in the
    integration source (covers raised-exception keys, fixed entity keys, and the
    divergent entity keys - i.e. everything not defaulted from description.key)."""
    keys: set[str] = set()
    pattern = re.compile(r'translation_key\s*=\s*"([a-z0-9_]+)"')
    for path in COMPONENT.rglob("*.py"):
        if "translations" in path.parts:
            continue
        keys.update(pattern.findall(path.read_text(encoding="utf-8")))
    return keys


class CodeTranslationKeyLiteralsTest(unittest.TestCase):
    def test_every_literal_key_is_translated(self) -> None:
        used = _code_translation_key_literals()
        for lang in ("en", "it"):
            data = json.loads((COMPONENT / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
            entity_keys = {k for plat in data.get("entity", {}).values() for k in plat}
            exc_keys = set(data.get("exceptions", {}))
            # The device-name translation_key (the "addhOn diagnostica" service
            # device) lives under the top-level "device" section, not under entity.
            device_keys = set(data.get("device", {}))
            missing = used - (entity_keys | exc_keys | device_keys)
            self.assertFalse(
                missing,
                f"[{lang}] translation_key literals used in code with no entity/exceptions "
                f"entry: {sorted(missing)}",
            )

    def test_no_orphan_exception_keys(self) -> None:
        used = _code_translation_key_literals()
        for lang in ("en", "it"):
            data = json.loads((COMPONENT / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
            exc_keys = set(data.get("exceptions", {}))
            orphan = exc_keys - used
            self.assertFalse(
                orphan,
                f"[{lang}] exceptions entries never raised by the code: {sorted(orphan)}",
            )

    def test_exceptions_parity(self) -> None:
        en = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
        it = json.loads((COMPONENT / "translations" / "it.json").read_text(encoding="utf-8"))
        self.assertEqual(set(en.get("exceptions", {})), set(it.get("exceptions", {})))


def _exception_raise_sites() -> list[tuple[str, set[str]]]:
    """(translation_key, placeholder_keys) for each raise site in the source."""
    pattern = re.compile(
        r'translation_key="(\w+)"(?:,\s*translation_placeholders=\{([^}]*)\})?'
    )
    sites: list[tuple[str, set[str]]] = []
    for path in COMPONENT.rglob("*.py"):
        if "translations" in path.parts:
            continue
        for m in pattern.finditer(path.read_text(encoding="utf-8")):
            placeholders = set(re.findall(r'"(\w+)":', m.group(2) or ""))
            sites.append((m.group(1), placeholders))
    return sites


class ExceptionPlaceholderTest(unittest.TestCase):
    """The translation_placeholders provided at each raise site must EXACTLY match
    the {tokens} in the translated message. A mismatch degrades the message in HA
    (suppress(KeyError) leaves a literal {token}); no other test catches it."""

    def test_placeholders_match_message_tokens(self) -> None:
        sites = _exception_raise_sites()
        for lang in ("en", "it"):
            data = json.loads((COMPONENT / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
            exc = data.get("exceptions", {})
            checked = 0
            for key, placeholders in sites:
                if key not in exc:  # entity translation_key literal, not an exception
                    continue
                tokens = set(re.findall(r"\{(\w+)\}", exc[key]["message"]))
                self.assertEqual(
                    tokens,
                    placeholders,
                    f"[{lang}] exception '{key}': message tokens {sorted(tokens)} != "
                    f"placeholders supplied in code {sorted(placeholders)}",
                )
                checked += 1
            self.assertGreater(checked, 0, "no exception raise sites were checked")


def _iter_translation_strings(node, path=""):
    """Yield (json_path, string_value) for every string leaf in a translations tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_translation_strings(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _iter_translation_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


class PlaceholderQuotingTest(unittest.TestCase):
    """hassfest rejects placeholders wrapped in single quotes: in ICU MessageFormat
    a single quote escapes literal text, so '{program}' renders verbatim instead of
    being substituted. This guard reproduces that CI check locally so the failure is
    caught before push, in every translation string (not only exceptions)."""

    _SINGLE_QUOTED = re.compile(r"'\{\w+\}'")

    def test_no_placeholder_inside_single_quotes(self) -> None:
        for lang in ("en", "it"):
            data = json.loads((COMPONENT / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
            offenders = [
                (path, value)
                for path, value in _iter_translation_strings(data)
                if self._SINGLE_QUOTED.search(value)
            ]
            self.assertEqual(
                offenders,
                [],
                f"[{lang}] placeholder wrapped in single quotes (hassfest rejects this): {offenders}",
            )


def _collect_sensor_state_options() -> dict[str, set[str]]:
    """Per translation_key, the union of `options` across ENUM sensor descriptions."""
    from custom_components.addhon import sensor

    by_tk: dict[str, set[str]] = {}
    for descs in sensor.SENSORS.values():
        for d in descs:
            options = getattr(d, "options", None)
            if not options:
                continue
            by_tk.setdefault(_tk(d), set()).update(options)
    return by_tk


class SensorStateTranslationTest(unittest.TestCase):
    """Every ENUM sensor's `options` (the machine keys it can report) must have a
    matching `entity.sensor.<tk>.state.<key>` label in BOTH languages, with no
    extras. Without this an ENUM state would show as a raw key in the UI."""

    def test_enum_options_match_state_translations(self) -> None:
        by_tk = _collect_sensor_state_options()
        self.assertTrue(by_tk, "no ENUM sensor options were collected")
        for lang in ("en", "it"):
            data = json.loads((COMPONENT / "translations" / f"{lang}.json").read_text(encoding="utf-8"))
            sensor_block = data.get("entity", {}).get("sensor", {})
            for tk, options in by_tk.items():
                state = set(sensor_block.get(tk, {}).get("state", {}))
                self.assertEqual(
                    options,
                    state,
                    f"[{lang}] entity.sensor.{tk}.state keys {sorted(state)} != "
                    f"ENUM options used in code {sorted(options)}",
                )

    def test_same_translation_key_descriptions_share_options(self) -> None:
        # ENUM sensors that share a translation_key must declare IDENTICAL options,
        # so the single per-tk state block is unambiguous (the cross-check above
        # unions options; this rules out divergent same-tk option sets).
        from custom_components.addhon import sensor

        seen: dict[str, tuple[str, ...]] = {}
        for descs in sensor.SENSORS.values():
            for d in descs:
                options = getattr(d, "options", None)
                if not options:
                    continue
                tk = _tk(d)
                opts = tuple(sorted(options))
                if tk in seen:
                    self.assertEqual(
                        seen[tk],
                        opts,
                        f"translation_key '{tk}' has ENUM descriptions with differing "
                        f"options: {seen[tk]} vs {opts}",
                    )
                else:
                    seen[tk] = opts


if __name__ == "__main__":
    unittest.main()
