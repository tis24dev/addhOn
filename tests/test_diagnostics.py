# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the enriched diagnostics dump.

The dump is what a user sends when an appliance is not mapped or mapped badly, so
the contract under test is: VALUES/ranges/enums are present, identity is redacted
(recursively) while telemetry and the readable nickname survive, and the `coverage`
block surfaces exactly the bare attributes / writable params the device exposes with
no addhon entity. Also covers the per-device hook resolving its appliance by
identifier.

Stdlib unittest with inline Home Assistant stubs (so the lazily-imported per-type
registries can be imported for the coverage axis). No real Home Assistant install.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
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
        "WEIGHT": "weight", "BATTERY": "battery", "POWER": "power", "ENUM": "enum",
        "TIMESTAMP": "timestamp",
    }))
    sensor_mod.SensorStateClass = getattr(sensor_mod, "SensorStateClass", type("SensorStateClass", (), {
        "MEASUREMENT": "measurement", "TOTAL": "total", "TOTAL_INCREASING": "total_increasing",
    }))

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

    # getattr-guarded like every other stub here: an unconditional assignment
    # CLOBBERS conftest's complete platform stubs, and an entity class binds
    # whatever base is installed the first time its module is imported. A bare
    # SelectEntity winning that race leaves the AP aroma select without the
    # `options` property real HA provides.
    switch_mod = _mod("homeassistant.components.switch")
    switch_mod.SwitchEntity = getattr(
        switch_mod, "SwitchEntity", type("SwitchEntity", (), {})
    )
    select_mod = _mod("homeassistant.components.select")
    select_mod.SelectEntity = getattr(
        select_mod, "SelectEntity", type("SelectEntity", (), {})
    )
    button_mod = _mod("homeassistant.components.button")
    button_mod.ButtonEntity = getattr(
        button_mod, "ButtonEntity", type("ButtonEntity", (), {})
    )

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

from custom_components.addhon import diagnostics  # noqa: E402
from custom_components.addhon import debug_utils  # noqa: E402
from custom_components.addhon.const import DOMAIN  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeWrapper:
    """Mimics a HonAttribute/HonParameter: a non-primitive object with a `.value`."""

    def __init__(self, value):
        self.value = value


class Opaque:
    """A non-primitive value with NO `.value` (must be stringified, not crash JSON)."""

    def __str__(self):
        return "opaque-str"


class FakeParam:
    def __init__(self, value=None, values=None, typology=None, category=None, mandatory=None, rng=None):
        self.value = value
        self.typology = typology
        self.category = category
        self.mandatory = mandatory
        if rng is not None:
            self.min, self.max, self.step = rng
        if values is not None:
            self.values = values
        elif rng is not None:
            # A range parameter DOES expose `.values` in the engine: the property
            # enumerates the whole grid. Mirrored here so any code that forgets to
            # check for a range pays the same price it would in production instead
            # of quietly reading an empty list off the fake.
            low, high, step = (int(float(v)) for v in rng)
            self.values = [str(v) for v in range(low, high + 1, max(step, 1))]


class FakeCommand:
    def __init__(self, parameters):
        self.parameters = parameters


class FakeAppliance:
    def __init__(self, commands, model_attributes=None):
        self.commands = commands
        if model_attributes is not None:
            self.model_attributes = model_attributes


class FakeApplianceNoModel:
    """An appliance implementation with NO `model_attributes` surface at all.

    Guards the getattr default: an older/foreign appliance object must not make
    the whole dump raise.
    """

    def __init__(self, commands):
        self.commands = commands


class FakeCoordinator:
    def __init__(self, data):
        self.data = data


class FakeEntry:
    def __init__(self, entry_id="e1"):
        self.entry_id = entry_id
        self.title = "(account) user@example.com"
        self.data = {"email": "user@example.com", "password": "hunter2"}
        self.options = {}


class FakeDevice:
    def __init__(self, identifiers):
        self.identifiers = identifiers


class FakeHass:
    def __init__(self, coordinator, entry_id="e1"):
        self.data = {DOMAIN: {entry_id: {"coordinator": coordinator}}}


AC_ID = "ac-unique"
WD_ID = "wd-unique"


def _build_coordinator() -> FakeCoordinator:
    ac = FakeAppliance(
        commands={
            "settings": FakeCommand({
                # climate-written -> mapped
                "tempSel": FakeParam(value="22", typology="range", rng=(16, 30, 1)),
                "machMode": FakeParam(value="1", typology="enum", values=["0", "1", "2"]),
                # AC switch param -> mapped
                "lightStatus": FakeParam(value="0", typology="enum", values=["0", "1"]),
                # no entity writes this -> unmapped writable
                "mysteryParam": FakeParam(value="3", typology="enum", values=["3", "4"]),
            }),
        },
        # Cloud CATALOGUE metadata (applianceModel.attributes), not shadow telemetry.
        model_attributes={
            "zones": "fridge|freezer|vtRoom2",
            "seriesVersion": "fd90Series7a",
            "doorNumber": 4,
        },
    )
    # No model_attributes surface at all -> the block must still build, with {}.
    wd = FakeApplianceNoModel(
        commands={
            "settings": FakeCommand({
                "program": FakeParam(value="9", typology="enum", values=["9", "10"]),  # mapped
                "spinSpeed": FakeParam(value="1000", typology="range", rng=(0, 1400, 100)),  # unmapped
            }),
        }
    )
    return FakeCoordinator({
        AC_ID: {
            "appliance": ac,
            "type": "AC",
            "name": "Salotto AC",
            "model": "AS35",
            "serial": "PLAINTEXT-SERIAL",
            "mac": "AA:BB:CC:DD:EE:FF",
            "attributes": {
                "tempIndoor": 22.5,           # mapped (sensor + custom)
                "available": True,            # mapped (connectivity)
                "settings.machMode": "1",     # dotted -> excluded from attr axis
                "weirdAcSensor": 9,           # UNMAPPED bare telemetry
                "macAddress": "AA:BB:CC:DD:EE:FF",  # identity nested in attributes
                # writable params also surface BARE in the device shadow; they must
                # NOT be reported as unmapped read-only attributes (fix: subtract
                # settings params from the attribute axis).
                "tempSel": "22",
                "machMode": "1",
                # wrapper objects (HonAttribute/HonParameter) must be JSON-coerced.
                "liveParam": FakeWrapper(7),
                "opaqueObj": Opaque(),
                # commandHistory carries identity inside VALUES (transactionId = MAC_ts)
                # and under nested keys (device.mobileId).
                "commandHistory": {
                    "command": {
                        "transactionId": "AA:BB:CC:DD:EE:FF_2024-01-01T00:00:00Z",
                        "macAddress": "AA:BB:CC:DD:EE:FF",
                        "commandName": "startProgram",
                        "device": {"mobileId": "phone-install-xyz", "os": "android"},
                    },
                },
            },
            "statistics": {},
        },
        WD_ID: {
            "appliance": wd,
            "type": "WD",
            "name": "Lavatrice di Mario",
            "model": "HW90",
            "serial": "SN-PLAINTEXT",
            "mac": "11:22:33:44:55:66",
            "attributes": {
                "machMode": "2",              # mapped (state sensor)
                "weirdNewSensor": 5,          # UNMAPPED bare telemetry (gold)
                "programsCounter": 12,        # UNMAPPED + from statistics
                "settings.spinSpeed": "1000",  # dotted -> excluded
                "serialNumber": "SN-PLAINTEXT",
                "deviceInfo": {"code": "C999", "label": "ok"},  # nested identity
            },
            "statistics": {"programsCounter": 12},
        },
    })


def _entry_diag():
    coord = _build_coordinator()
    hass = FakeHass(coord)
    result = _run(diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry()))
    blocks = {b["type"]: b for b in result["appliances"]}
    return result, blocks


class DiagnosticsValuesTest(unittest.TestCase):
    def test_attribute_values_present_and_not_redacted(self):
        _, blocks = _entry_diag()
        self.assertEqual(blocks["AC"]["attributes"]["tempIndoor"], 22.5)
        self.assertEqual(blocks["WD"]["attributes"]["machMode"], "2")

    def test_command_ranges_present(self):
        _, blocks = _entry_diag()
        tempsel = blocks["AC"]["commands"]["settings"]["tempSel"]
        self.assertEqual((tempsel["min"], tempsel["max"], tempsel["step"]), (16.0, 30.0, 1.0))

    def test_command_enums_present(self):
        _, blocks = _entry_diag()
        machmode = blocks["AC"]["commands"]["settings"]["machMode"]
        self.assertEqual(machmode["enum"], ["0", "1", "2"])
        self.assertEqual(machmode["value"], "1")

    def test_range_param_schema_omits_enumerated_grid(self):
        # A real HonParameterRange exposes BOTH min/max/step AND a .values property
        # that ENUMERATES the whole grid (up to 100k strings). _param_schema must
        # emit only min/max/step and never dump that grid into `enum`.
        grid = [str(v) for v in range(0, 1401, 100)]
        param = FakeParam(value="1000", typology="range", rng=(0, 1400, 100), values=grid)
        schema = diagnostics._param_schema(param)
        self.assertEqual((schema["min"], schema["max"], schema["step"]), (0, 1400, 100))
        self.assertNotIn("enum", schema)
        self.assertNotIn("values", schema)

    def test_a_small_range_carries_the_values_it_materialises(self):
        """min/max/step cannot answer why a 0/1 control is missing: param_range
        casts through float(), so "0"/"1" and "0.0"/"1.0" print the same here,
        while the capability gates compare those exact strings."""
        param = FakeParam(
            value="0", typology="range", rng=(0, 1, 1), values=["0", "1"]
        )
        self.assertEqual(["0", "1"], diagnostics._param_schema(param)["values"])

    def test_a_decimal_spelled_range_is_visible_as_such(self):
        """The whole point: the dump must distinguish the grid that passes a
        capability gate from the one that silently removes the control."""
        param = FakeParam(
            value="0", typology="range", rng=(0, 1, 1), values=["0.0", "1.0"]
        )
        schema = diagnostics._param_schema(param)
        self.assertEqual((schema["min"], schema["max"], schema["step"]), (0, 1, 1))
        self.assertEqual(["0.0", "1.0"], schema["values"])

    def test_the_materialised_grid_is_bounded(self):
        """Emitted only for a grid small enough to be a toggle or a few-position
        control. The bound is evaluated arithmetically, so `.values` is never read
        for a real setpoint range."""
        cap = diagnostics._RANGE_MAX_MATERIALISED
        inside = FakeParam(
            value="0", typology="range", rng=(0, cap - 1, 1),
            values=[str(v) for v in range(cap)],
        )
        outside = FakeParam(
            value="0", typology="range", rng=(0, cap, 1),
            values=[str(v) for v in range(cap + 1)],
        )
        self.assertIn("values", diagnostics._param_schema(inside))
        self.assertNotIn("values", diagnostics._param_schema(outside))

    def test_a_huge_grid_is_never_materialised_to_measure_it(self):
        """A parameter whose `.values` would explode must be refused WITHOUT the
        property ever being read: the count comes from min/max/step. Not a
        FakeParam subclass, because that one ASSIGNS self.values and a property
        cannot be assigned over."""

        class ExplodingRange:
            typology = "range"
            category = "command"
            mandatory = 0
            value = "0"
            min, max, step = 0, 100000, 1

            @property
            def values(self):  # pragma: no cover - must never be reached
                raise AssertionError("`.values` was materialised for a huge range")

        schema = diagnostics._param_schema(ExplodingRange())
        self.assertNotIn("values", schema)
        self.assertEqual(100000, schema["max"])

    def test_mac_in_value_under_benign_key_is_masked(self):
        # Identity that lands in a string VALUE under a non-redacted key (an event
        # payload, a transactionId-shaped value under a benign name) must still be
        # masked, matching the log path (debug_utils.redact_identity).
        out = diagnostics._redact(
            {"someInfo": "3c-71-bf-bd-32-2c_1699999999",
             "nested": {"note": "mac 3C:71:BF:BD:32:2C here"}}
        )
        dumped = json.dumps(out)
        self.assertNotIn("3c-71-bf-bd-32-2c", dumped)
        self.assertNotIn("3C:71:BF:BD:32:2C", dumped)
        self.assertEqual(out["someInfo"], "***_1699999999")

    def test_mac_in_value_wrapped_object_is_masked(self):
        # _jsonable also unwraps a HonAttribute/HonParameter-like object via `.value`;
        # a MAC carried inside that `.value` must be masked too, so wrapping it does
        # not smuggle the identity into the dump in cleartext.
        out = diagnostics._redact({"deviceInfo": FakeWrapper("mac 3C:71:BF:BD:32:2C")})
        dumped = json.dumps(out)
        self.assertNotIn("3C:71:BF:BD:32:2C", dumped)
        self.assertEqual(out["deviceInfo"], "mac ***")


class DiagnosticsModelAttributesTest(unittest.TestCase):
    """The model CATALOGUE block (`applianceModel.attributes`).

    It answers what the appliance IS where the shadow cannot: which zones the
    model declares, which series it belongs to. Without it a zone-indexing
    report cannot be diagnosed from the dump alone (issue #75).
    """

    def test_model_attributes_present_and_readable(self):
        _, blocks = _entry_diag()
        model = blocks["AC"]["model_attributes"]
        self.assertEqual(model["zones"], "fridge|freezer|vtRoom2")
        self.assertEqual(model["seriesVersion"], "fd90Series7a")
        self.assertEqual(model["doorNumber"], 4)

    def test_appliance_without_the_surface_gets_empty_dict(self):
        _, blocks = _entry_diag()
        self.assertEqual(blocks["WD"]["model_attributes"], {})

    def test_distinct_from_shadow_attributes(self):
        # Same block, two axes: catalogue vs telemetry. A key of one must not
        # leak into the other.
        _, blocks = _entry_diag()
        self.assertNotIn("zones", blocks["AC"]["attributes"])
        self.assertNotIn("tempIndoor", blocks["AC"]["model_attributes"])

    def test_non_mapping_surface_is_ignored(self):
        self.assertEqual(diagnostics._model_attributes(object()), {})
        self.assertEqual(
            diagnostics._model_attributes(
                FakeAppliance(commands={}, model_attributes=["zones"])
            ),
            {},
        )

    def test_redaction_still_applies_to_the_block(self):
        # model_attributes goes through _redact like every other section: a
        # catalogue row named after an identity key must not pass in cleartext.
        app = FakeAppliance(
            commands={}, model_attributes={"macAddress": "AA:BB:CC:DD:EE:FF", "zones": "fridge"}
        )
        block = diagnostics._appliance_block(
            "id1", {"appliance": app, "type": "AC", "attributes": {}, "statistics": {}}
        )
        self.assertEqual(block["model_attributes"]["macAddress"], "***")
        self.assertEqual(block["model_attributes"]["zones"], "fridge")


class DiagnosticsCoverageTest(unittest.TestCase):
    def test_unmapped_bare_attribute_surfaces(self):
        _, blocks = _entry_diag()
        self.assertIn("weirdAcSensor", blocks["AC"]["coverage"]["attributes_unmapped"])
        self.assertIn("weirdNewSensor", blocks["WD"]["coverage"]["attributes_unmapped"])

    def test_mapped_attributes_not_reported_unmapped(self):
        _, blocks = _entry_diag()
        ac_unmapped = blocks["AC"]["coverage"]["attributes_unmapped"]
        self.assertNotIn("tempIndoor", ac_unmapped)
        self.assertNotIn("available", ac_unmapped)
        self.assertNotIn("machMode", blocks["WD"]["coverage"]["attributes_unmapped"])

    def test_dotted_keys_excluded_from_attribute_axis(self):
        _, blocks = _entry_diag()
        self.assertNotIn("settings.machMode", blocks["AC"]["coverage"]["attributes_unmapped"])
        self.assertNotIn("settings.spinSpeed", blocks["WD"]["coverage"]["attributes_unmapped"])

    def test_statistics_unmapped_split_out(self):
        # statistics keys are carved OUT of the signal into their own list (the signal
        # `attributes_unmapped` is pure telemetry candidates).
        _, blocks = _entry_diag()
        cov = blocks["WD"]["coverage"]
        self.assertNotIn("programsCounter", cov["attributes_unmapped"])
        self.assertIn("programsCounter", cov["attributes_unmapped_statistics"])
        # real telemetry is signal, not statistics
        self.assertIn("weirdNewSensor", cov["attributes_unmapped"])
        self.assertNotIn("weirdNewSensor", cov["attributes_unmapped_statistics"])

    def test_unmapped_writable_params(self):
        _, blocks = _entry_diag()
        self.assertIn("mysteryParam", blocks["AC"]["coverage"]["command_params_unmapped"])
        self.assertIn("spinSpeed", blocks["WD"]["coverage"]["command_params_unmapped"])

    def test_mapped_writable_params_not_reported(self):
        _, blocks = _entry_diag()
        ac_unmapped = blocks["AC"]["coverage"]["command_params_unmapped"]
        self.assertNotIn("tempSel", ac_unmapped)       # climate-written
        self.assertNotIn("machMode", ac_unmapped)      # climate-written
        self.assertNotIn("lightStatus", ac_unmapped)   # AC switch
        self.assertNotIn("program", blocks["WD"]["coverage"]["command_params_unmapped"])


    def test_bare_writable_params_not_reported_unmapped(self):
        # A device shadow exposes writable params bare (tempSel/machMode); they belong
        # to the command-param axis, not the read-only attribute axis.
        _, blocks = _entry_diag()
        ac_unmapped = blocks["AC"]["coverage"]["attributes_unmapped"]
        self.assertNotIn("tempSel", ac_unmapped)
        self.assertNotIn("machMode", ac_unmapped)

    def test_attributes_total_is_telemetry_axis_denominator(self):
        # `attributes_total` = mapped telemetry + signal; it excludes writable mirrors,
        # statistics AND meta, so `unmapped / total` reads as a real coverage gap.
        _, blocks = _entry_diag()
        cov = blocks["AC"]["coverage"]
        # AC bare keys: tempIndoor, available, weirdAcSensor, macAddress, tempSel,
        # machMode, liveParam, opaqueObj, commandHistory (9); minus 2 writable mirrors
        # (tempSel, machMode) and 1 meta (commandHistory, dict-valued) -> 6.
        self.assertEqual(cov["attributes_total"], 6)
        self.assertLessEqual(len(cov["attributes_unmapped"]), cov["attributes_total"])


class DiagnosticsSerializationTest(unittest.TestCase):
    def test_whole_dump_is_json_serializable(self):
        # HA serializes the dump; wrapper objects in attributes must be coerced.
        result, _ = _entry_diag()
        json.dumps(result)  # must not raise

    def test_wrapper_value_unwrapped(self):
        _, blocks = _entry_diag()
        self.assertEqual(blocks["AC"]["attributes"]["liveParam"], 7)

    def test_opaque_value_stringified(self):
        _, blocks = _entry_diag()
        self.assertEqual(blocks["AC"]["attributes"]["opaqueObj"], "opaque-str")


class DiagnosticsRedactionTest(unittest.TestCase):
    def test_top_level_identity_redacted(self):
        _, blocks = _entry_diag()
        for block in blocks.values():
            self.assertEqual(block["id"], "***")
            self.assertEqual(block["serial"], "***")
            self.assertEqual(block["mac"], "***")

    def test_identity_keys_redacted_recursively(self):
        _, blocks = _entry_diag()
        self.assertEqual(blocks["AC"]["attributes"]["macAddress"], "***")
        self.assertEqual(blocks["WD"]["attributes"]["serialNumber"], "***")
        # nested dict: `code` (serial fallback) redacted, sibling preserved
        self.assertEqual(blocks["WD"]["attributes"]["deviceInfo"]["code"], "***")
        self.assertEqual(blocks["WD"]["attributes"]["deviceInfo"]["label"], "ok")

    def test_command_history_value_borne_identity_redacted(self):
        _, blocks = _entry_diag()
        cmd = blocks["AC"]["attributes"]["commandHistory"]["command"]
        self.assertEqual(cmd["transactionId"], "***")   # carried the full MAC
        self.assertEqual(cmd["macAddress"], "***")
        self.assertEqual(cmd["device"]["mobileId"], "***")
        # non-sensitive siblings survive
        self.assertEqual(cmd["commandName"], "startProgram")
        self.assertEqual(cmd["device"]["os"], "android")

    def test_nickname_kept_readable(self):
        _, blocks = _entry_diag()
        self.assertEqual(blocks["WD"]["name"], "Lavatrice di Mario")
        self.assertEqual(blocks["AC"]["name"], "Salotto AC")

    def test_no_over_redaction_of_telemetry(self):
        _, blocks = _entry_diag()
        # model/type must survive (needed for mapping)
        self.assertEqual(blocks["AC"]["model"], "AS35")
        self.assertEqual(blocks["AC"]["type"], "AC")

    def test_entry_envelope_redacted(self):
        result, _ = _entry_diag()
        self.assertEqual(result["entry"]["data"]["password"], "***")
        self.assertEqual(result["entry"]["data"]["email"], "***@example.com")
        self.assertNotIn("user@example.com", result["entry"]["title"] or "")


class DiagnosticsDeviceTest(unittest.TestCase):
    def test_device_diagnostics_returns_single_matching_appliance(self):
        coord = _build_coordinator()
        hass = FakeHass(coord)
        device = FakeDevice(identifiers={(DOMAIN, WD_ID)})
        result = _run(diagnostics.async_get_device_diagnostics(hass, FakeEntry(), device))
        self.assertIn("appliance", result)
        self.assertEqual(result["appliance"]["type"], "WD")
        self.assertEqual(result["appliance"]["name"], "Lavatrice di Mario")

    def test_device_diagnostics_unknown_device_returns_empty(self):
        coord = _build_coordinator()
        hass = FakeHass(coord)
        device = FakeDevice(identifiers={(DOMAIN, "does-not-exist")})
        self.assertEqual(_run(diagnostics.async_get_device_diagnostics(hass, FakeEntry(), device)), {})

    def test_device_diagnostics_foreign_identifier_ignored(self):
        coord = _build_coordinator()
        hass = FakeHass(coord)
        device = FakeDevice(identifiers={("some_other_domain", WD_ID)})
        self.assertEqual(_run(diagnostics.async_get_device_diagnostics(hass, FakeEntry(), device)), {})


class DiagnosticsDriftGuardTest(unittest.TestCase):
    def test_custom_mapped_attrs_pinned(self):
        # Adding a custom entity class that consumes a new bare attribute must come
        # with an update here, otherwise the attribute would be reported as unmapped.
        self.assertEqual(
            diagnostics._CUSTOM_MAPPED_ATTRS,
            {
                "WM": frozenset({"totalWashCycle", "totalWaterUsed", "machMode"}),
                "WD": frozenset({"totalWashCycle", "totalWaterUsed", "machMode"}),
                "TD": frozenset({"machMode"}),
                "AC": frozenset({"tempIndoor"}),
            },
        )

    def test_ac_climate_params_pinned(self):
        self.assertEqual(
            diagnostics._AC_CLIMATE_PARAMS,
            frozenset({"onOffStatus", "machMode", "tempSel", "windSpeed", "windDirectionVertical"}),
        )

    def test_coverage_meta_denylists_pinned(self):
        # Editing the noise denylists must be a deliberate, reviewed change.
        self.assertEqual(
            diagnostics._COVERAGE_META_ATTRS,
            frozenset({
                "resultcode", "debugenabled", "hightransrate",
                "statussyncrate", "stdtransrate", "transmode",
                "programstats", "cloudprogid", "cloudprogsrc",
                "forcedelete", "testcmdreceivestatus",
            }),
        )
        self.assertEqual(
            diagnostics._COVERAGE_META_PARAMS,
            frozenset({
                "category", "httpendpoint", "mqttendpoint", "resw", "operationname",
                "programrules", "remoteactionable", "remotevisible",
                "winddirectionverticalpositionsequence",
            }),
        )
        self.assertEqual(
            [p.pattern for p in diagnostics._COVERAGE_META_ATTR_PATTERNS],
            [r"(?i)^program\d+$"],
        )


class DiagnosticsCoverageMetaTest(unittest.TestCase):
    """Coverage noise partition: value-type envelope + scalar denylist + program slots
    move to *_meta; genuine signal stays in *_unmapped; nothing is dropped."""

    def _coverage_ac(self):
        app = FakeAppliance(commands={"settings": FakeCommand({
            # genuine writable control (no entity) -> command-param signal
            "humiditySel": FakeParam(value="50", typology="range", rng=(30, 70, 5)),
            # command plumbing -> command-param meta
            "category": FakeParam(value="setParameters", typology="enum", values=["setConfig", "setParameters"]),
            "httpEndpoint": FakeParam(value="x", typology="fixed"),
            "operationName": FakeParam(value="x", typology="fixed"),
        })})
        attrs = {
            # signal (scalar telemetry candidates)
            "errors": 0,
            "programName": "auto_set",
            "weirdSensor": 7,
            # near-misses that must STAY signal (regex/name must not catch them)
            "programsCounter": 3,
            "programClass": "eco",
            # structural envelope (dict/list value) -> meta, no name list needed
            "commandHistory": {"command": {"transactionId": "y"}},
            "lastConnEvent": {"category": "CONNECTED"},
            "mostUsedPrograms": [],
            # scalar protocol/debug noise -> meta (name denylist)
            "debugEnabled": 0,
            "transMode": 0,
            "resultCode": "0",
            "highTransRate": 1,
            "programStats": "a7;1b0;;;",  # scalar stats blob -> meta (denylist)
            "cloudProgId": "x",           # cloud plumbing -> meta (denylist)
            "forceDelete": "0",           # command plumbing -> meta (denylist)
            # program-definition slots -> meta (regex)
            "program7": "x",
            "program19": "y",
            # dotted writable mirror -> excluded from the attribute axis entirely
            "settings.machMode": "1",
        }
        return diagnostics._coverage("AC", attrs, {}, app)

    def test_signal_keeps_genuine_telemetry(self):
        cov = self._coverage_ac()
        for k in ("errors", "programName", "weirdSensor", "programsCounter", "programClass"):
            self.assertIn(k, cov["attributes_unmapped"], k)
            self.assertNotIn(k, cov["attributes_unmapped_meta"], k)

    def test_value_type_envelope_is_meta(self):
        cov = self._coverage_ac()
        for k in ("commandHistory", "lastConnEvent", "mostUsedPrograms"):
            self.assertIn(k, cov["attributes_unmapped_meta"], k)
            self.assertNotIn(k, cov["attributes_unmapped"], k)

    def test_scalar_meta_and_program_slots_are_meta(self):
        cov = self._coverage_ac()
        for k in ("debugEnabled", "transMode", "resultCode", "highTransRate",
                  "programStats", "cloudProgId", "forceDelete", "program7", "program19"):
            self.assertIn(k, cov["attributes_unmapped_meta"], k)
            self.assertNotIn(k, cov["attributes_unmapped"], k)

    def test_partition_is_lossless_and_disjoint(self):
        cov = self._coverage_ac()
        signal = set(cov["attributes_unmapped"])
        meta = set(cov["attributes_unmapped_meta"])
        stats = set(cov["attributes_unmapped_statistics"])
        self.assertEqual(signal & meta, set())
        self.assertEqual(signal & stats, set())
        self.assertEqual(meta & stats, set())

    def test_command_param_meta_split(self):
        cov = self._coverage_ac()
        self.assertIn("humiditySel", cov["command_params_unmapped"])
        for k in ("category", "httpEndpoint", "operationName"):
            self.assertIn(k, cov["command_params_unmapped_meta"], k)
            self.assertNotIn(k, cov["command_params_unmapped"], k)

    def test_command_param_total_excludes_meta(self):
        # Symmetric with attributes_total: denominator = mapped controls + signal, not
        # inflated by meta params. settings has 4 params (humiditySel + 3 meta), none
        # mapped -> total 4 - 3 meta = 1, equal to the signal count.
        cov = self._coverage_ac()
        self.assertEqual(cov["command_params_total"], 1)
        self.assertEqual(len(cov["command_params_unmapped"]), cov["command_params_total"])


class WcSettingsSwitchCoverageTest(unittest.TestCase):
    """The wine-cooler light switch writes settings.lightStatus, so lightStatus must be
    counted as mapped (not falsely reported as an unmapped writable in the gold signal).
    Regression guard for _mapped_sets/_coverage being AC-only before the WC switch shipped
    (discussion #62)."""

    def _wc_appliance(self):
        # Mirrors the real HWS77GDAU1 settings command (tests/fixtures/wc_hws77).
        return FakeAppliance(commands={"settings": FakeCommand({
            "lightStatus": FakeParam(value="0", typology="range", rng=(0, 1, 1)),  # WC switch -> mapped
            "sabbathStatus": FakeParam(value="0", typology="enum", values=["0", "1"]),  # no entity -> signal
            "tempUnit": FakeParam(value="0", typology="enum", values=["0", "1"]),
        })})

    def test_lightstatus_in_mapped_params(self):
        _, mapped_params = diagnostics._mapped_sets("WC")
        self.assertIn("lightStatus", mapped_params)

    def test_lightstatus_not_reported_unmapped(self):
        cov = diagnostics._coverage("WC", {}, {}, self._wc_appliance())
        self.assertNotIn("lightStatus", cov["command_params_unmapped"])
        self.assertNotIn("lightStatus", cov["command_params_unmapped_meta"])
        # positive control: an un-mapped writable IS still reported, proving the coverage
        # machinery ran and the lists are populated (guards against a vacuous pass).
        self.assertIn("sabbathStatus", cov["command_params_unmapped"])


class IdentityKeysDriftGuardTest(unittest.TestCase):
    """The shared log redactor (debug_utils._IDENTITY_KEYS) must redact at least
    everything the Download-Diagnostics path (diagnostics._TO_REDACT) does, so a
    new secret key added to one is not left in cleartext in the other."""

    def test_to_redact_is_subset_of_identity_keys(self) -> None:
        missing = set(diagnostics._TO_REDACT) - set(debug_utils._IDENTITY_KEYS)
        self.assertEqual(
            missing,
            set(),
            f"keys redacted by diagnostics but NOT by the log redactor: {missing}",
        )


class LastErrorDiagnosticsTest(unittest.TestCase):
    """#30: the config-entry diagnostics expose the last classified error code."""

    def test_last_error_none_without_client(self) -> None:
        # FakeHass stores only the coordinator -> no recorded error.
        result = _run(diagnostics.async_get_config_entry_diagnostics(FakeHass(_build_coordinator()), FakeEntry()))
        self.assertIn("last_error", result)
        self.assertIsNone(result["last_error"])

    def test_last_error_reports_code_and_reason(self) -> None:
        from custom_components.addhon import error_codes as ec

        class _Client:
            last_error_code = ec.NETWORK_TIMEOUT
            last_error_phase = "load_appliances"
            last_mfa_summary = None
            _refresh_token = ""

        hass = FakeHass(_build_coordinator())
        hass.data[DOMAIN]["e1"]["client"] = _Client()
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry()))
        le = result["last_error"]
        self.assertEqual("ADDHON-400", le["code"])
        self.assertEqual(ec.NETWORK_TIMEOUT.reason_en, le["reason"])
        self.assertFalse(le["requires_reauth"])
        self.assertTrue(le["ui"])
        self.assertEqual("load_appliances", le["phase"])
        self.assertFalse(le["had_refresh_token"])
        self.assertNotIn("mfa", le)  # not an MFA-band code

    def test_last_error_includes_a_leak_free_phase_ledger(self) -> None:
        # #76: without per-phase durations a report cannot say WHICH phase burned the
        # time, so no timeout hypothesis is falsifiable on the user's machine.
        from custom_components.addhon import error_codes as ec

        class _Client:
            last_error_code = ec.REFRESH_TIMEOUT
            last_error_phase = "load_appliances/auth/refresh"
            last_mfa_summary = None
            last_phase_ledger = [
                {"phase": "load_appliances/auth/refresh", "seconds": 50.0, "outcome": "timeout"},
                {"phase": "load_appliances", "seconds": 50.1, "outcome": "error"},
            ]
            _refresh_token = "rt"

        hass = FakeHass(_build_coordinator())
        hass.data[DOMAIN]["e1"]["client"] = _Client()
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry()))
        le = result["last_error"]
        self.assertEqual("ADDHON-406", le["code"])
        self.assertEqual(2, len(le["phase_ledger"]))
        blob = json.dumps(le["phase_ledger"])
        self.assertNotIn("@", blob)
        self.assertNotIn("http", blob)
        for entry in le["phase_ledger"]:
            self.assertIn(entry["outcome"], ("ok", "error", "timeout"))

    def test_last_error_omits_the_ledger_when_absent(self) -> None:
        from custom_components.addhon import error_codes as ec

        class _Client:
            last_error_code = ec.NETWORK_TIMEOUT
            last_error_phase = "load_appliances"
            last_mfa_summary = None
            last_phase_ledger = None
            _refresh_token = ""

        hass = FakeHass(_build_coordinator())
        hass.data[DOMAIN]["e1"]["client"] = _Client()
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry()))
        self.assertNotIn("phase_ledger", result["last_error"])

    def test_last_error_includes_mfa_summary_for_mfa_code(self) -> None:
        from custom_components.addhon import error_codes as ec

        class _Client:
            last_error_code = ec.MFA_REQUIRED
            last_error_phase = "mfa_challenge"
            last_mfa_summary = {"challenge_kind": "email", "can_resend": True}
            _refresh_token = "rt"

        hass = FakeHass(_build_coordinator())
        hass.data[DOMAIN]["e1"]["client"] = _Client()
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry()))
        le = result["last_error"]
        self.assertEqual("ADDHON-160", le["code"])
        self.assertTrue(le["requires_reauth"])
        self.assertTrue(le["had_refresh_token"])
        self.assertEqual({"challenge_kind": "email", "can_resend": True}, le["mfa"])


# --- Task 10: air purifier coverage and passive future-capability capture -----

AP_ID = "ap-unique"


def _build_ap_coordinator(attribute_overrides: dict | None = None) -> FakeCoordinator:
    """An AP whose schema and shadow deliberately run AHEAD of the integration.

    machMode declares 3 (undeclared by the observed devices and never writable),
    aromaStatus declares a 5 nobody has mapped, and the shadow currently REPORTS
    machMode=3. humidificationStatus is a writable parameter with no entity at all.
    """
    ap = FakeAppliance(
        commands={
            "startProgram": FakeCommand({
                "machMode": FakeParam(
                    value="2", typology="enum", values=["1", "2", "3", "4"]
                ),
                "onOffStatus": FakeParam(value="1", typology="fixed", mandatory=1),
            }),
            "stopProgram": FakeCommand({
                "onOffStatus": FakeParam(value="0", typology="fixed", mandatory=1),
            }),
            "settings": FakeCommand({
                "machMode": FakeParam(
                    value="2", typology="enum", values=["1", "2", "3", "4"]
                ),
                "lightStatus": FakeParam(
                    value="0", typology="enum", values=["0", "1", "2"]
                ),
                "lockStatus": FakeParam(value="0", typology="enum", values=["0", "1"]),
                "touchToneStatus": FakeParam(
                    value="1", typology="enum", values=["0", "1"]
                ),
                "aromaStatus": FakeParam(
                    value="0", typology="enum",
                    values=["0", "1", "2", "3", "4", "5"],
                ),
                "aromaTimeOn": FakeParam(
                    value="60", typology="range", rng=(1, 3600, 1)
                ),
                "aromaTimeOff": FakeParam(
                    value="60", typology="range", rng=(1, 3600, 1)
                ),
                # Declared by the application, deliberately unmapped (ledger B2).
                "humidificationStatus": FakeParam(
                    value="0", typology="enum", values=["0", "1"]
                ),
            }),
        }
    )
    return FakeCoordinator({
        AP_ID: {
            "appliance": ap,
            "type": "AP",
            "name": "Purificatore",
            "model": "SYNTHETIC-AP",
            "serial": "AP-PLAINTEXT-SERIAL",
            "mac": "AA:BB:CC:DD:EE:FF",
            "attributes": {
                "onOffStatus": "1",
                # A mode the integration will not offer, currently ACTIVE.
                "machMode": "3",
                "lightStatus": "0",
                "lockStatus": "0",
                "touchToneStatus": "1",
                "aromaStatus": "0",
                "aromaTimeOn": "60",
                "aromaTimeOff": "60",
                "airQuality": "0",
                "pollenLevel": "4",
                "errors": "00",
                "coLevel": "1",
                "mainFilterStatus": "34",
                # Declared by the application, no entity (ledger B1).
                "no2ValueIndoor": "7",
                "macAddress": "AA:BB:CC:DD:EE:FF",
                **(attribute_overrides or {}),
            },
            "statistics": {},
        },
    })


def _counting_text(length: int) -> str:
    """A run of digits where no character equals its neighbour, so a slice that keeps
    the right NUMBER of characters but the wrong ones is still visible."""
    return "".join(str(index % 10) for index in range(length))


def _ap_block(attribute_overrides: dict | None = None):
    coord = _build_ap_coordinator(attribute_overrides)
    hass = FakeHass(coord)
    result = _run(diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry()))
    return result["appliances"][0]


class AirPurifierCoverageTest(unittest.TestCase):
    def test_air_purifier_controlled_params_are_not_reported_unmapped(self) -> None:
        """Every parameter an AP entity reads or writes must disappear from the
        coverage signal, or the dump would send a maintainer chasing controls that
        already exist."""
        coverage = _ap_block()["coverage"]
        for param in (
            "machMode", "onOffStatus", "lightStatus", "lockStatus",
            "touchToneStatus", "aromaStatus", "aromaTimeOn", "aromaTimeOff",
        ):
            self.assertNotIn(param, coverage["command_params_unmapped"], param)
            self.assertNotIn(param, coverage["attributes_unmapped"], param)

    def test_air_purifier_unknown_param_stays_in_the_signal(self) -> None:
        coverage = _ap_block()["coverage"]
        self.assertIn("humidificationStatus", coverage["command_params_unmapped"])

    def test_air_purifier_unmapped_telemetry_stays_in_the_signal(self) -> None:
        coverage = _ap_block()["coverage"]
        self.assertIn("no2ValueIndoor", coverage["attributes_unmapped"])

    def test_air_purifier_mapped_telemetry_is_not_in_the_signal(self) -> None:
        coverage = _ap_block()["coverage"]
        for attr in ("airQuality", "pollenLevel", "errors", "coLevel"):
            self.assertNotIn(attr, coverage["attributes_unmapped"], attr)

    def test_air_purifier_entity_params_cover_every_table(self) -> None:
        """Drift guard: the diagnostics registration is one list, so a new AP
        control whose parameter is missing from it would resurface as unmapped."""
        from custom_components.addhon.air_purifier import AP_ENTITY_PARAMS
        from custom_components.addhon import number, switch

        declared = {d.param for d in switch._AIR_PURIFIER_SWITCHES}
        declared |= {d.param for d in number._AP_TIMING_NUMBERS}
        self.assertTrue(declared)
        self.assertTrue(declared <= AP_ENTITY_PARAMS, declared - AP_ENTITY_PARAMS)


class FutureCapabilityCaptureTest(unittest.TestCase):
    def test_future_capability_reports_enum_values_the_code_ignores(self) -> None:
        """Passive capture: the device declares machMode 3 and aromaStatus 5, and
        the integration handles neither. That delta is the whole point of the
        section - it is how a new firmware capability becomes visible without any
        entity being guessed into existence."""
        future = _ap_block()["future_capabilities"]
        self.assertEqual(["3"], future["enum_deltas"]["settings.machMode"])
        self.assertEqual(["3"], future["enum_deltas"]["startProgram.machMode"])
        self.assertEqual(["5"], future["enum_deltas"]["settings.aromaStatus"])

    def test_future_capability_omits_fully_handled_params(self) -> None:
        future = _ap_block()["future_capabilities"]
        for key in ("settings.lightStatus", "settings.lockStatus",
                    "settings.touchToneStatus"):
            self.assertNotIn(key, future["enum_deltas"])

    def test_future_capability_reports_an_unhandled_live_state(self) -> None:
        future = _ap_block()["future_capabilities"]
        self.assertEqual({"machMode": "3"}, future["state_values_unhandled"])

    def test_an_unhandled_state_value_is_length_capped(self) -> None:
        """The section is passive EVIDENCE, not a report: a firmware answering with a
        long blob must add a hint to the dump, never carry the blob into it.

        Nothing pinned this. The cap was written as a count constant times four,
        reading as "20 values" while meaning 80 characters, and when it was split into
        its own bound no test noticed either spelling: removing the slice entirely left
        the whole suite green.
        """
        from custom_components.addhon.diagnostics import _FUTURE_MAX_VALUE_CHARS

        # Derived from the constant, so the MECHANISM is pinned at any value it takes;
        # the value itself is bounded separately, because a cap large enough to carry
        # the blob would satisfy the mechanism while defeating its purpose.
        self.assertLessEqual(_FUTURE_MAX_VALUE_CHARS, 256)
        # Every character differs from its neighbours, so `startswith` pins WHICH
        # window was kept. A blob of one repeated character satisfies it under any
        # shifted slice, which left the cap pinned by length alone.
        blob = _counting_text(_FUTURE_MAX_VALUE_CHARS * 3)
        future = _ap_block({"machMode": blob})["future_capabilities"]
        captured = future["state_values_unhandled"]["machMode"]
        self.assertEqual(_FUTURE_MAX_VALUE_CHARS, len(captured))
        self.assertTrue(blob.startswith(captured))

    def test_an_unhandled_state_value_at_the_cap_is_untouched(self) -> None:
        """The control: the cap must not trim a value that fits, or every unhandled
        reading would arrive mangled.

        It sits ON the bound rather than well under it. A short value cannot tell a
        slice at the cap from a slice one character either side of it, so the control
        that read a plain "3" repeated an assertion the suite already made and could
        not fail on its own.
        """
        from custom_components.addhon.diagnostics import _FUTURE_MAX_VALUE_CHARS

        exact = _counting_text(_FUTURE_MAX_VALUE_CHARS)
        future = _ap_block({"machMode": exact})["future_capabilities"]
        self.assertEqual({"machMode": exact}, future["state_values_unhandled"])

    def test_future_capability_carries_no_identity(self) -> None:
        import json

        future = _ap_block()["future_capabilities"]
        encoded = json.dumps(future)
        for identity in ("AA:BB:CC:DD:EE:FF", "AP-PLAINTEXT-SERIAL", "SYNTHETIC-AP",
                         "ap-unique"):
            self.assertNotIn(identity, encoded, identity)

    def test_future_capability_is_absent_for_a_type_with_no_registry(self) -> None:
        _, blocks = _entry_diag()
        self.assertEqual({}, blocks["AC"]["future_capabilities"])

    def test_future_capability_output_is_bounded(self) -> None:
        """A firmware declaring hundreds of values must not blow up the dump."""
        coord = _build_ap_coordinator()
        params = coord.data[AP_ID]["appliance"].commands["settings"].parameters
        params["aromaStatus"].values = [str(n) for n in range(500)]
        hass = FakeHass(coord)
        result = _run(
            diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry())
        )
        future = result["appliances"][0]["future_capabilities"]
        delta = future["enum_deltas"]["settings.aromaStatus"]
        self.assertEqual(diagnostics._FUTURE_MAX_VALUES, len(delta))
        self.assertTrue(future["truncated"])

    def test_future_capability_does_not_enumerate_a_range_parameter(self) -> None:
        """Whatever the registry says, a range parameter is never enumerated:
        `.values` on a HonParameterRange walks the whole grid (up to 100000 strings)
        synchronously, which is why _param_schema refuses it too. The registry
        currently lists no range param, so this pins the guard rather than the
        registry."""
        from custom_components.addhon import air_purifier

        coord = _build_ap_coordinator()
        hass = FakeHass(coord)
        patched = {**air_purifier.AP_HANDLED_VALUES, "aromaTimeOn": frozenset({"60"})}
        original = air_purifier.AP_HANDLED_VALUES
        air_purifier.AP_HANDLED_VALUES = patched
        try:
            result = _run(
                diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry())
            )
        finally:
            air_purifier.AP_HANDLED_VALUES = original
        future = result["appliances"][0]["future_capabilities"]
        self.assertNotIn("settings.aromaTimeOn", future["enum_deltas"])

    def test_future_capability_normalizes_a_numeric_state(self) -> None:
        """A client that hands back 2.0 instead of "2" must not turn a handled value
        into a phantom future capability."""
        coord = _build_ap_coordinator()
        attributes = coord.data[AP_ID]["attributes"]
        attributes["lightStatus"] = 2.0
        attributes["machMode"] = 3.0
        hass = FakeHass(coord)
        result = _run(
            diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry())
        )
        future = result["appliances"][0]["future_capabilities"]
        self.assertEqual({"machMode": "3"}, future["state_values_unhandled"])

    def test_future_capability_keeps_the_schema_signal_readable(self) -> None:
        """The raw schema and the raw values stay in the dump: capture must not
        make the evidence harder to read."""
        block = _ap_block()
        self.assertEqual(
            ["1", "2", "3", "4"],
            block["commands"]["settings"]["machMode"]["enum"],
        )
        self.assertEqual("3", block["attributes"]["machMode"])
        self.assertEqual("4", block["attributes"]["pollenLevel"])


# --- the entity inventory ------------------------------------------------------


class FakeRegistryEntry:
    """A registry row, with only the fields the inventory is allowed to read."""

    def __init__(self, unique_id, entity_id, disabled_by=None, hidden_by=None):
        self.unique_id = unique_id
        self.entity_id = entity_id
        self.disabled_by = disabled_by
        self.hidden_by = hidden_by


class FakeState:
    def __init__(self, restored=False):
        self.attributes = {"restored": True} if restored else {}


class FakeStates:
    """The state machine: only the entities a platform actually added are live."""

    def __init__(self, live=(), restored=()):
        self._states = {entity_id: FakeState() for entity_id in live}
        self._states.update(
            {entity_id: FakeState(restored=True) for entity_id in restored}
        )

    def get(self, entity_id):
        return self._states.get(entity_id)


class RegistryHass(FakeHass):
    def __init__(self, coordinator, rows=None, states=None, entry_id="e1"):
        super().__init__(coordinator, entry_id)
        self.rows = [] if rows is None else rows
        if states is not None:
            self.states = states


def _install_registry(hass, rows=None, raises=False, registry=object()):
    """Point the stubbed entity_registry at THIS hass, for one test."""
    import homeassistant.helpers.entity_registry as er

    def async_get(_hass):
        if raises:
            raise RuntimeError("registry not loaded")
        return registry

    def async_entries_for_config_entry(_registry, _entry_id):
        return hass.rows if rows is None else rows

    er.async_get = async_get
    er.async_entries_for_config_entry = async_entries_for_config_entry


def _restore_registry():
    import homeassistant.helpers.entity_registry as er

    er.async_get = lambda hass: None
    er.async_entries_for_config_entry = lambda registry, entry_id: []


AP_ROWS = (
    ("ap-unique_child_lock", "switch.purificatore_child_lock"),
    ("ap-unique_touch_tone", "switch.purificatore_touch_tone"),
    ("ap-unique_purifier", "fan.purificatore"),
    ("ap-unique_aroma", "select.purificatore_aroma"),
)


class EntityInventoryTest(unittest.TestCase):
    def tearDown(self) -> None:
        _restore_registry()

    def _dump(self, rows, states=None, raises=False):
        coord = _build_ap_coordinator()
        hass = RegistryHass(coord, rows=rows, states=states)
        _install_registry(hass, raises=raises)
        result = _run(
            diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry())
        )
        return result, result["appliances"][0]["entities"]

    def _rows(self, pairs=AP_ROWS, **kwargs):
        return [FakeRegistryEntry(uid, eid, **kwargs) for uid, eid in pairs]

    def test_the_registered_entities_are_listed_per_domain(self) -> None:
        live = FakeStates(live=[eid for _uid, eid in AP_ROWS])
        _result, entities = self._dump(self._rows(), states=live)
        self.assertEqual(
            {
                "switch": ["child_lock", "touch_tone"],
                "fan": ["purifier"],
                "select": ["aroma"],
            },
            entities["by_domain"],
        )
        self.assertEqual("ok", entities["status"])

    def test_an_appliance_with_no_rows_reports_nothing_created(self) -> None:
        """NOT "unavailable": the dump looked and found nothing, which is the whole
        finding. Reporting it as a failed lookup would erase it."""
        _result, entities = self._dump([], states=FakeStates())
        self.assertEqual("ok", entities["status"])
        self.assertEqual({}, entities["by_domain"])

    def test_an_unreadable_registry_is_not_an_empty_one(self) -> None:
        result, entities = self._dump(self._rows(), raises=True)
        self.assertEqual({"status": "unavailable"}, entities)
        self.assertEqual({"status": "unavailable"}, result["platforms"])

    def test_a_disabled_entity_is_reported_as_disabled_not_as_missing(self) -> None:
        """Home Assistant writes no state for a disabled entity, so a naive live
        check would report every user-disabled control as a platform crash."""
        rows = self._rows(AP_ROWS[:1], disabled_by="user") + self._rows(AP_ROWS[1:])
        live = FakeStates(live=[eid for _uid, eid in AP_ROWS[1:]])
        _result, entities = self._dump(rows, states=live)
        self.assertEqual({"switch.child_lock": "user"}, entities["disabled"])
        self.assertNotIn("not_created", entities)
        self.assertIn("child_lock", entities["by_domain"]["switch"])

    def test_a_hidden_entity_is_reported_as_hidden(self) -> None:
        """"Hidden" is what "I cannot see it in Home Assistant" usually means, and
        it is invisible in every other section of the dump."""
        rows = self._rows(AP_ROWS[:1], hidden_by="user") + self._rows(AP_ROWS[1:])
        live = FakeStates(live=[eid for _uid, eid in AP_ROWS])
        _result, entities = self._dump(rows, states=live)
        self.assertEqual({"switch.child_lock": "user"}, entities["hidden"])

    def test_a_row_with_no_live_entity_is_reported_as_not_created(self) -> None:
        """The registry survives reloads, so a platform that broke AFTER a working
        install still has its rows. Only the state machine tells them apart."""
        live = FakeStates(live=[eid for _uid, eid in AP_ROWS[2:]])
        _result, entities = self._dump(self._rows(), states=live)
        self.assertEqual(
            ["switch.child_lock", "switch.touch_tone"], entities["not_created"]
        )

    def test_a_restored_state_counts_as_not_created(self) -> None:
        """Home Assistant leaves a placeholder state carrying restored=True for a
        registered entity nothing added; that is the same finding as no state."""
        states = FakeStates(
            live=[eid for _uid, eid in AP_ROWS[1:]],
            restored=[AP_ROWS[0][1]],
        )
        _result, entities = self._dump(self._rows(), states=states)
        self.assertEqual(["switch.child_lock"], entities["not_created"])

    def test_without_a_state_machine_the_status_says_so(self) -> None:
        """FakeHass has no `states`. The section must not claim a cross-check it
        could not run, and must not invent a not_created list."""
        _result, entities = self._dump(self._rows())
        self.assertEqual("registry_only", entities["status"])
        self.assertNotIn("not_created", entities)

    def test_account_entities_are_counted_by_domain_never_attributed(self) -> None:
        """They are created unconditionally, so a domain missing from here is a
        platform that did not finish. That is what separates a dead platform from a
        device that was gated out."""
        rows = self._rows() + self._rows(
            (
                ("e1_diag_debug_logging", "switch.addhon_debug_logging"),
                ("e1_diag_mqtt_realtime_debug", "switch.addhon_mqtt_debug"),
            )
        )
        result, entities = self._dump(rows, states=FakeStates())
        self.assertEqual({"switch": 2}, result["platforms"]["account"])
        self.assertNotIn("diag_debug_logging", entities["by_domain"].get("switch", []))

    def test_a_dead_platform_is_distinguishable_from_a_gated_device(self) -> None:
        """The reported case. No switch row anywhere INCLUDING the unconditional
        account ones means the switch platform never finished; a gated device would
        still leave those two behind."""
        rows = self._rows(AP_ROWS[2:])
        result, entities = self._dump(rows, states=FakeStates())
        self.assertEqual({}, result["platforms"]["account"])
        self.assertNotIn("switch", entities["by_domain"])
        self.assertNotIn("switch", result["platforms"]["appliance_totals"])

    def test_a_leftover_account_row_does_not_clear_a_dead_platform(self) -> None:
        """The harder half of the same case: the platform ran on an EARLIER install,
        so its unconditional account entity is still in the registry. Counting that
        row at face value would vouch for the platform that just died."""
        account = (("e1_diag_debug_logging", "switch.addhon_debug_logging"),)
        rows = self._rows(AP_ROWS[2:]) + self._rows(account)
        states = FakeStates(
            live=[eid for _uid, eid in AP_ROWS[2:]],
            restored=[account[0][1]],
        )
        result, _entities = self._dump(rows, states=states)
        self.assertEqual({"switch": 1}, result["platforms"]["account"])
        self.assertEqual({"switch": 1}, result["platforms"]["account_not_created"])

    def test_a_live_account_row_clears_the_platform(self) -> None:
        """The other side of it: the account entity is alive, so the platform ran,
        and a missing appliance control is a capability gate rather than a crash."""
        account = (("e1_diag_debug_logging", "switch.addhon_debug_logging"),)
        rows = self._rows(AP_ROWS[2:]) + self._rows(account)
        states = FakeStates(
            live=[eid for _uid, eid in AP_ROWS[2:]] + [account[0][1]]
        )
        result, _entities = self._dump(rows, states=states)
        self.assertEqual({"switch": 1}, result["platforms"]["account"])
        self.assertEqual({}, result["platforms"]["account_not_created"])

    def test_a_disabled_account_row_is_not_reported_as_uncreated(self) -> None:
        """A user who disabled the debug switch has no state either, and that must
        not read as a dead platform."""
        account = (("e1_diag_debug_logging", "switch.addhon_debug_logging"),)
        rows = self._rows(AP_ROWS[2:]) + self._rows(account, disabled_by="user")
        states = FakeStates(live=[eid for _uid, eid in AP_ROWS[2:]])
        result, _entities = self._dump(rows, states=states)
        self.assertEqual({"switch": 1}, result["platforms"]["account"])
        self.assertEqual({}, result["platforms"]["account_not_created"])

    def test_a_row_of_another_config_entry_shape_is_counted_unattributed(self) -> None:
        rows = self._rows((("someone-else_child_lock", "switch.other_child_lock"),))
        result, _entities = self._dump(rows, states=FakeStates())
        self.assertEqual(1, result["platforms"]["unattributed"])

    def test_the_longest_appliance_prefix_wins(self) -> None:
        """Prefixes nest for real: a multi-zone appliance expands into `<id>` and
        `<id>_z1`, so the shorter id must not swallow the longer id's rows."""
        inventory, _platforms = diagnostics._entity_inventory(
            [FakeRegistryEntry("ap-unique_z1_purifier", "fan.zone_one")],
            ["ap-unique", "ap-unique_z1"],
            "e1",
            None,
        )
        self.assertEqual({}, inventory["ap-unique"]["by_domain"])
        self.assertEqual(
            {"fan": ["purifier"]}, inventory["ap-unique_z1"]["by_domain"]
        )

    def test_a_non_string_appliance_id_cannot_break_the_dump(self) -> None:
        inventory, platforms = diagnostics._entity_inventory(
            [FakeRegistryEntry("ap-unique_purifier", "fan.p")],
            [None, 7, "ap-unique"],
            "e1",
            None,
        )
        self.assertEqual({"fan": ["purifier"]}, inventory["ap-unique"]["by_domain"])
        self.assertEqual(0, platforms["unattributed"])

    def test_the_inventory_is_bounded(self) -> None:
        cap = diagnostics._ENTITY_MAX_PER_DOMAIN
        rows = self._rows(
            tuple(
                (f"ap-unique_sensor_{n}", f"sensor.p_{n}") for n in range(cap + 5)
            )
        )
        result, entities = self._dump(rows, states=FakeStates())
        self.assertEqual(cap, len(entities["by_domain"]["sensor"]))
        self.assertTrue(entities["truncated"])
        self.assertTrue(result["platforms"]["truncated"])

    def test_only_the_appliance_that_lost_rows_is_marked_truncated(self) -> None:
        """A complete inventory carrying someone else's truncation flag reads as
        incomplete. With one appliance the flag's scope is unobservable, so this
        needs a second, small one."""
        cap = diagnostics._ENTITY_MAX_PER_DOMAIN
        big = [
            FakeRegistryEntry(f"ap-unique_sensor_{n}", f"sensor.p_{n}")
            for n in range(cap + 5)
        ]
        small = [FakeRegistryEntry("ap-two_purifier", "fan.second")]

        inventory, platforms = diagnostics._entity_inventory(
            big + small, ["ap-unique", "ap-two"], "e1", None
        )

        self.assertTrue(inventory["ap-unique"]["truncated"])
        self.assertNotIn("truncated", inventory["ap-two"])
        self.assertTrue(platforms["truncated"])

    def test_the_device_dump_carries_the_same_inventory(self) -> None:
        """The tester downloads diagnostics from the DEVICE page, not the entry."""
        coord = _build_ap_coordinator()
        hass = RegistryHass(
            coord,
            rows=self._rows(),
            states=FakeStates(live=[eid for _uid, eid in AP_ROWS]),
        )
        _install_registry(hass)
        device = FakeDevice(identifiers={(DOMAIN, AP_ID)})
        result = _run(
            diagnostics.async_get_device_diagnostics(hass, FakeEntry(), device)
        )
        self.assertEqual(
            ["child_lock", "touch_tone"],
            result["appliance"]["entities"]["by_domain"]["switch"],
        )


class EntityInventoryScopeTest(unittest.TestCase):
    """The claims the single-appliance tests structurally cannot make."""

    def tearDown(self) -> None:
        _restore_registry()

    def test_two_entities_sharing_a_suffix_across_domains_do_not_collide(self) -> None:
        """A wine cooler holds a `light` SWITCH and a `light` BINARY SENSOR: same
        unique_id suffix, different domains, both legitimate. Keyed by the bare
        suffix, one silently overwrote the other and the reader could not tell
        which entity a finding was about."""
        rows = [
            FakeRegistryEntry("wc-1_light", "switch.cantina_light", disabled_by="user"),
            FakeRegistryEntry("wc-1_light", "binary_sensor.cantina_light"),
        ]
        inventory, _platforms = diagnostics._entity_inventory(
            rows, ["wc-1"], "e1", FakeStates().get
        )
        section = inventory["wc-1"]

        self.assertEqual({"switch.light": "user"}, section["disabled"])
        self.assertEqual(["binary_sensor.light"], section["not_created"])
        self.assertEqual(
            {"switch": ["light"], "binary_sensor": ["light"]}, section["by_domain"]
        )

    def test_a_zone_row_never_lands_on_the_base_appliance(self) -> None:
        """A zoned appliance is `<base>_z1`. When the poll drops the zone but its
        registry rows survive, the row must not fall back onto the base appliance
        and drag its not_created finding with it."""
        rows = [
            FakeRegistryEntry("ac-1_child_lock", "switch.ac_child_lock"),
            FakeRegistryEntry("ac-1_z1_purifier", "fan.ac_zone_one"),
        ]
        inventory, platforms = diagnostics._entity_inventory(
            rows, ["ac-1"], "e1", FakeStates().get
        )

        self.assertEqual({"switch": ["child_lock"]}, inventory["ac-1"]["by_domain"])
        self.assertEqual(["switch.child_lock"], inventory["ac-1"]["not_created"])
        self.assertEqual(1, platforms["unattributed"])

    def test_a_present_zone_still_gets_its_own_rows(self) -> None:
        """The guard above must not cost a correctly-declared zone its entities."""
        rows = [
            FakeRegistryEntry("ac-1_child_lock", "switch.ac_child_lock"),
            FakeRegistryEntry("ac-1_z1_purifier", "fan.ac_zone_one"),
        ]
        inventory, platforms = diagnostics._entity_inventory(
            rows, ["ac-1", "ac-1_z1"], "e1", None
        )

        self.assertEqual({"switch": ["child_lock"]}, inventory["ac-1"]["by_domain"])
        self.assertEqual({"fan": ["purifier"]}, inventory["ac-1_z1"]["by_domain"])
        self.assertEqual(0, platforms["unattributed"])

    def test_only_the_zone_shape_is_treated_as_a_zone(self) -> None:
        """The guard keys on the `z<N>_` shape the id builder produces, not on the
        letter: a future entity key that merely starts with a z stays attributed."""
        rows = [FakeRegistryEntry("ac-1_zone_mode", "select.ac_zone_mode")]
        inventory, platforms = diagnostics._entity_inventory(
            rows, ["ac-1"], "e1", None
        )

        self.assertEqual({"select": ["zone_mode"]}, inventory["ac-1"]["by_domain"])
        self.assertEqual(0, platforms["unattributed"])

    def test_the_entry_dump_keeps_two_appliances_apart(self) -> None:
        """Attribution through the real entry point, which every other dump-level
        test cannot check because it builds a single appliance."""
        coord = _build_ap_coordinator()
        coord.data["ap-unique_z1"] = dict(coord.data[AP_ID])
        rows = [
            FakeRegistryEntry("ap-unique_child_lock", "switch.first_child_lock"),
            FakeRegistryEntry("ap-unique_z1_purifier", "fan.second"),
        ]
        hass = RegistryHass(coord, rows=rows, states=FakeStates())
        _install_registry(hass)
        result = _run(
            diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry())
        )
        # Blocks come out in coordinator order: the base appliance, then its zone.
        base, zone = result["appliances"]

        self.assertEqual({"switch": ["child_lock"]}, base["entities"]["by_domain"])
        self.assertEqual({"fan": ["purifier"]}, zone["entities"]["by_domain"])
        self.assertEqual(
            {"switch": 1, "fan": 1}, result["platforms"]["appliance_totals"]
        )

    def test_the_device_dump_carries_only_its_own_appliance(self) -> None:
        """The device hook builds the inventory over EVERY id on purpose; this pins
        that the sibling's rows do not leak into the requested block."""
        coord = _build_ap_coordinator()
        coord.data["ap-unique_z1"] = dict(coord.data[AP_ID])
        rows = [
            FakeRegistryEntry("ap-unique_child_lock", "switch.first_child_lock"),
            FakeRegistryEntry("ap-unique_z1_purifier", "fan.second"),
        ]
        hass = RegistryHass(coord, rows=rows, states=FakeStates())
        _install_registry(hass)
        result = _run(
            diagnostics.async_get_device_diagnostics(
                hass, FakeEntry(), FakeDevice(identifiers={(DOMAIN, AP_ID)})
            )
        )
        self.assertEqual(
            {"switch": ["child_lock"]},
            result["appliance"]["entities"]["by_domain"],
        )

    def test_the_entry_totals_add_the_appliances_up(self) -> None:
        """appliance_totals was only ever asserted negatively, so the accumulation
        itself was never exercised."""
        rows = [
            FakeRegistryEntry("ap-a_child_lock", "switch.a_child_lock"),
            FakeRegistryEntry("ap-a_touch_tone", "switch.a_touch_tone"),
            FakeRegistryEntry("ap-b_child_lock", "switch.b_child_lock"),
            FakeRegistryEntry("ap-b_purifier", "fan.b"),
        ]
        inventory, platforms = diagnostics._entity_inventory(
            rows, ["ap-a", "ap-b"], "e1", None
        )

        self.assertEqual({"switch": 3, "fan": 1}, platforms["appliance_totals"])
        counted = sum(
            len(keys)
            for section in inventory.values()
            for keys in section["by_domain"].values()
        )
        self.assertEqual(sum(platforms["appliance_totals"].values()), counted)

    def test_a_hidden_entity_is_still_checked_for_life(self) -> None:
        """Deliberately asymmetric with the disabled case: hidden entities ARE
        added by their platform and do get a state, so a hidden row with no state
        is the same finding as any other."""
        rows = [
            FakeRegistryEntry(
                "ap-1_child_lock", "switch.p_child_lock", hidden_by="integration"
            )
        ]
        inventory, _platforms = diagnostics._entity_inventory(
            rows, ["ap-1"], "e1", FakeStates().get
        )
        section = inventory["ap-1"]

        self.assertEqual({"switch.child_lock": "integration"}, section["hidden"])
        self.assertEqual(["switch.child_lock"], section["not_created"])

    def test_the_lists_come_out_sorted(self) -> None:
        """Fed in reverse order, so a test that never observes ordering cannot
        pass by accident."""
        rows = [
            FakeRegistryEntry("ap-1_touch_tone", "switch.p_tt"),
            FakeRegistryEntry("ap-1_child_lock", "switch.p_cl"),
            FakeRegistryEntry("ap-1_aroma", "select.p_aroma"),
        ]
        inventory, _platforms = diagnostics._entity_inventory(
            rows, ["ap-1"], "e1", FakeStates().get
        )
        section = inventory["ap-1"]

        self.assertEqual(["child_lock", "touch_tone"], section["by_domain"]["switch"])
        self.assertEqual(
            ["select.aroma", "switch.child_lock", "switch.touch_tone"],
            section["not_created"],
        )


class EntityInventoryIdentityTest(unittest.TestCase):
    def tearDown(self) -> None:
        _restore_registry()

    def test_no_raw_identifier_reaches_the_dump(self) -> None:
        """The appliance id here is NOT MAC-shaped on purpose: the module's MAC
        regex would mask a colon-separated id on its own, and a test built on one
        would pass even if the inventory emitted the raw unique_id."""
        import json

        rows = [
            FakeRegistryEntry(
                f"{AP_ID}_child_lock", "switch.purificatore_di_mario_child_lock"
            )
        ]
        coord = _build_ap_coordinator()
        hass = RegistryHass(coord, rows=rows, states=FakeStates())
        _install_registry(hass)
        encoded = json.dumps(
            _run(diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry()))
        )

        self.assertNotIn(AP_ID, encoded)
        self.assertNotIn("purificatore_di_mario", encoded)
        # The qualified key looks like an entity_id and is not one: its right-hand
        # side is the code-authored unique_id suffix, never the nickname slug.
        self.assertNotIn("switch.purificatore", encoded)
        self.assertIn("child_lock", encoded)


if __name__ == "__main__":
    unittest.main()
