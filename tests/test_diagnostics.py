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

import ast
import asyncio
import dataclasses
import importlib
import json
import sys
import types
import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone, tzinfo
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
from custom_components.addhon import const  # noqa: E402
from custom_components.addhon.const import DOMAIN  # noqa: E402
from custom_components.addhon.const import (  # noqa: E402
    CONF_AUTH_DIAGNOSTICS,
    CONF_ENABLE_DEBUG,
    CONF_ENABLE_EXPERIMENTAL,
    CONF_ENABLE_MQTT_DEBUG,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeWrapper:
    """Mimics a HonAttribute/HonParameter: a non-primitive object with a `.value`."""

    def __init__(self, value):
        self.value = value


class FakeShadowAttribute:
    """A HonAttribute: a `.value` PLUS the cloud instant that last moved it.

    Deliberately NOT folded into `FakeWrapper` above: that one stands for every
    `.value` wrapper in the engine (a HonParameter has no instant), and giving
    it a `last_update` would put a row in every timestamp map in this file.
    """

    def __init__(self, value, last_update=None):
        self.value = value
        self.last_update = last_update


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


# Pinned instants for the shared AC fixture. They are constants rather than
# offsets from now() so that a section asserting an AGE can subtract them from a
# frozen clock; a fixture built on the wall clock is how age assertions turn
# into tests that fail once a year on a slow machine.
AC_STAMP_OLD = datetime(2026, 4, 9, 5, 31, 2, tzinfo=timezone.utc)
AC_STAMP_NEW = datetime(2026, 4, 9, 5, 34, 16, tzinfo=timezone.utc)

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
                # Wrapped the way the real shadow arrives: a value AND the
                # instant the cloud last moved it. Every other section must be
                # blind to the wrapper -- `_jsonable` still unwraps it to 22.5,
                # and the coverage denominator still counts one bare key.
                "tempIndoor": FakeShadowAttribute(22.5, AC_STAMP_OLD),  # mapped
                "available": True,            # mapped (connectivity)
                "settings.machMode": "1",     # dotted -> excluded from attr axis
                "weirdAcSensor": 9,           # UNMAPPED bare telemetry
                "macAddress": "AA:BB:CC:DD:EE:FF",  # identity nested in attributes
                # writable params also surface BARE in the device shadow; they must
                # NOT be reported as unmapped read-only attributes (fix: subtract
                # settings params from the attribute axis).
                # tempSel carries the NEWEST instant in this fixture, so a
                # section reporting "when did anything last move" has a
                # deterministic answer. machMode carries none, which is the
                # `null` row: `attributes.py:73` is a walrus guard, so an update
                # with no lastUpdate never clears a previous instant.
                "tempSel": FakeShadowAttribute("22", AC_STAMP_NEW),
                "machMode": FakeShadowAttribute("1", None),
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
        # `spinSpeed` is NOT unmapped: `select.opt_spin_speed` writes it, and
        # `entities.sources` names it. Pinning it here pinned the contradiction.
        self.assertNotIn("spinSpeed", blocks["WD"]["coverage"]["command_params_unmapped"])

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
        # The AC fixture is built with `statistics: {}`, so the `- len(statistics)`
        # term in the formula is identically 0 here and dropping it leaves the whole
        # suite green. WD is the only block carrying a statistics key, and every real
        # washer, dryer and fridge has them (the live REF has four).
        wd = blocks["WD"]["coverage"]
        # WD bare keys: available, weirdNewSensor, serialNumber, programsCounter,
        # deviceInfo (5); minus 1 statistics (programsCounter) and 1 meta
        # (deviceInfo, dict-valued) -> 3.
        self.assertEqual(wd["attributes_total"], 3)
        self.assertEqual(["programsCounter"], wd["attributes_unmapped_statistics"])


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
        # The fixture MAC is canonical, so `_MAC_RE` alone masks it and the clause
        # above cannot see the key-name path die. Pin the key-name path on shapes
        # the six-octet regex CANNOT match -- unseparated, and the placeholder the
        # engine itself substitutes (tests/fixtures/ref_10136/attributes.json).
        self.assertEqual("***", diagnostics._redact({"mac": "AABBCCDDEEFF"})["mac"])
        self.assertEqual(
            "***", diagnostics._redact({"macAddress": "xx-xx-xx-xx-xx-xx"})["macAddress"]
        )

    def test_identity_keys_redacted_recursively(self):
        _, blocks = _entry_diag()
        self.assertEqual(blocks["AC"]["attributes"]["macAddress"], "***")
        # As above: isolate the key-name path from `_MAC_RE` so this clause measures
        # the mapping it is named for rather than the value regex.
        self.assertEqual(
            "***", diagnostics._redact({"MacAddress": "AABBCCDDEEFF"})["MacAddress"]
        )
        self.assertEqual(blocks["WD"]["attributes"]["serialNumber"], "***")
        # nested dict: `code` (serial fallback) redacted, sibling preserved
        self.assertEqual(blocks["WD"]["attributes"]["deviceInfo"]["code"], "***")
        self.assertEqual(blocks["WD"]["attributes"]["deviceInfo"]["label"], "ok")

    # NAMED on purpose, never `for key in _TO_REDACT`: a loop over the constant proves
    # every SURVIVING member still works but goes quiet when a member is removed.
    # Measured: deleting mac_address / mobile_id / serial_number / transaction_id from
    # _TO_REDACT, _IDENTITY_KEYS and the old test pin together left the WHOLE suite
    # green -- they were the four names of the twenty that this list did not cover.
    _MUST_MASK = (
        "serial", "serialnumber", "serial_number",
        "mac", "macaddress", "mac_address",
        "code", "nickname", "nick_name", "email",
        "password", "token", "access_token", "refresh_token",
        "authorization", "secret",
        "transactionid", "transaction_id", "mobileid", "mobile_id",
    )

    def test_credential_key_names_are_redacted_anywhere_they_appear(self):
        """The entry envelope redacts with explicit literals, so the CREDENTIAL half
        of `_TO_REDACT` is reachable only through this generic path: a credential
        arriving under one of these names inside an appliance's attributes or a
        nested cloud payload. Deleting all nine names left the full suite green.

        Widened from nine names to all twenty: the eleven that were missing had no
        behavioural observer on EITHER redaction path, only a set-equality pin that
        any consistent edit silences.
        """
        for key in self._MUST_MASK:
            self.assertEqual(
                "***", diagnostics._redact({key: "s3cret"})[key], key
            )
            # and one level down, where a cloud payload actually carries them
            nested = diagnostics._redact({"attributes": {key: "s3cret"}})
            self.assertEqual("***", nested["attributes"][key], key)
        # positive control: a non-identity sibling must survive, so the loop above
        # cannot pass by _redact masking everything it is handed.
        self.assertEqual("ok", diagnostics._redact({"label": "ok"})["label"])

    def test_every_redacted_key_has_a_behavioural_assertion(self):
        """Replaces nothing -- this is the assertion the drift guard only approximates.

        `IdentityKeysDriftGuardTest` compares `_TO_REDACT` to `_IDENTITY_KEYS`: one
        constant against another. Because the two sets are IDENTICAL it fires for all
        twenty names and separates nothing, and an edit touching both is invisible to
        it. This ties the constant to the list that actually DRIVES `_redact`, so a
        name added to `_TO_REDACT` fails here until it is given a real assertion.
        """
        self.assertEqual(set(self._MUST_MASK), set(diagnostics._TO_REDACT))

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


def _dump_options(options: dict) -> dict:
    """`entry.options` as the entry dump renders it."""
    entry = FakeEntry()
    entry.options = options
    return _run(
        diagnostics.async_get_config_entry_diagnostics(
            FakeHass(_build_coordinator()), entry
        )
    )["entry"]["options"]


class EntryOptionsRedactionTest(unittest.TestCase):
    """`entry.options` reaches the dump straight out of storage, and the dump is
    the file users paste into a GitHub issue. The three toggles that exist today
    are booleans and leak nothing, so what is pinned here is what happens to the
    FOURTH option: whoever adds it, whatever it ends up carrying."""

    def test_the_known_toggles_keep_their_real_values(self):
        self.assertEqual(
            {
                CONF_ENABLE_DEBUG: True,
                CONF_ENABLE_EXPERIMENTAL: True,
                CONF_ENABLE_MQTT_DEBUG: False,
            },
            _dump_options(
                {
                    CONF_ENABLE_DEBUG: True,
                    CONF_ENABLE_MQTT_DEBUG: False,
                    CONF_ENABLE_EXPERIMENTAL: True,
                }
            ),
        )

    def test_an_unknown_option_is_named_but_never_shown(self):
        options = _dump_options(
            {CONF_ENABLE_DEBUG: True, "api_endpoint": "https://bob:hunter2@hon.example"}
        )
        # The NAME survives on purpose: an option this build does not recognise is
        # worth reporting, and a dropped key reports nothing.
        self.assertIn("api_endpoint", options)
        self.assertEqual("***", options["api_endpoint"])
        self.assertNotIn("hunter2", json.dumps(options))
        # positive control: the mask is keyed on the whitelist, not applied to the
        # whole block, so the clauses above cannot pass by redacting everything.
        self.assertEqual(True, options[CONF_ENABLE_DEBUG])

    def test_the_sign_in_trace_flag_is_not_an_option(self):
        """`CONF_AUTH_DIAGNOSTICS` sits beside the three toggles in const.py, which is
        exactly why it is worth stating: the config flow strips it before anything is
        persisted, so it never IS an option, and an entry that somehow carries one is
        a stranger like any other."""
        self.assertEqual(
            {CONF_AUTH_DIAGNOSTICS: "***"},
            _dump_options({CONF_AUTH_DIAGNOSTICS: True}),
        )

    def test_an_admitted_key_holding_a_non_boolean_is_masked_too(self):
        # The whitelist admits a name, but what makes that name safe to print is
        # the shape behind it: the Configure screen stores `bool(...)`, so a
        # string here was written by something else and nothing vouches for it.
        options = _dump_options(
            {CONF_ENABLE_DEBUG: "https://bob:hunter2@hon.example", CONF_ENABLE_MQTT_DEBUG: True}
        )
        self.assertEqual("***", options[CONF_ENABLE_DEBUG])
        self.assertNotIn("hunter2", json.dumps(options))
        # positive control: the mask is keyed on the value's shape, not applied
        # to every admitted key once one of them looks wrong.
        self.assertEqual(True, options[CONF_ENABLE_MQTT_DEBUG])

    def test_no_options_render_as_no_options(self):
        self.assertEqual({}, _dump_options({}))

    def test_the_keys_come_out_sorted(self):
        # Same install, two downloads: an issue thread routinely carries both, and a
        # storage-order mapping makes them diff on nothing that happened.
        self.assertEqual(
            ["alpha", CONF_ENABLE_DEBUG, "zulu"],
            list(_dump_options({"zulu": 1, CONF_ENABLE_DEBUG: True, "alpha": 2})),
        )


def _options_flow_conf_names() -> set[str]:
    """The `CONF_*` constants `OptionsFlowHandler` names inside its own body.

    Parsed, never imported: config_flow reaches the transport auth chain (and
    `yarl`, which the stubs in this file do not provide), and the module whose
    next toggle this guard exists to catch is precisely the one it must not fail
    to read. Scoped to the class because `CONF_AUTH_DIAGNOSTICS` is named
    elsewhere in the same file by the credentials flow, which writes `entry.data`
    and not `entry.options`.
    """
    source = (REPO_ROOT / "custom_components" / "addhon" / "config_flow.py").read_text(
        encoding="utf-8"
    )
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == "OptionsFlowHandler":
            return {
                child.id
                for child in ast.walk(node)
                if isinstance(child, ast.Name) and child.id.startswith("CONF_")
            }
    raise AssertionError(
        "config_flow.OptionsFlowHandler was not found, so the option keys the "
        "Configure screen writes could not be read. If the handler was renamed, "
        "point this guard at the new name -- do not delete it."
    )


class EntryOptionsWhitelistGuardTest(unittest.TestCase):
    """The behavioural tests above prove the mask works on the keys they name. This
    ties the whitelist to the screen that fills `entry.options`, so an option added
    to the Configure form later has to be ADMITTED here deliberately instead of
    riding into every dump because nobody thought about it."""

    def test_the_whitelist_is_exactly_what_the_options_screen_writes(self):
        offered = {getattr(const, name) for name in _options_flow_conf_names()}
        self.assertEqual(
            offered,
            set(diagnostics._KNOWN_OPTIONS),
            "diagnostics._KNOWN_OPTIONS and the keys OptionsFlowHandler writes have "
            "diverged. A new toggle must be added to _KNOWN_OPTIONS only once its "
            "value is known to be safe to publish; otherwise leave it masked and "
            "drop it from this comparison with a reason.",
        )


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
        result = _run(diagnostics.async_get_device_diagnostics(hass, FakeEntry(), device))
        # No appliance resolved -- and the dump is dated anyway. Both halves
        # matter: nothing about the device leaks into the degraded document,
        # and the document still says when it was taken.
        self.assertEqual(["generated_at"], list(result))

    def test_device_diagnostics_foreign_identifier_ignored(self):
        coord = _build_coordinator()
        hass = FakeHass(coord)
        device = FakeDevice(identifiers={("some_other_domain", WD_ID)})
        with _FrozenClock(FROZEN):
            result = _run(
                diagnostics.async_get_device_diagnostics(hass, FakeEntry(), device)
            )
        # A foreign integration's identifier resolves nothing here, and the degraded
        # dump is still dated. Exact-dict on the VALUE, not `list(result)`: this is
        # the only test reaching the FIRST early return (appliance_id is None), so
        # the stamp there has no other guard -- DumpTimestampTest's two degraded
        # cases both resolve a DOMAIN identifier and land on the SECOND return.
        self.assertEqual({"generated_at": FROZEN_TEXT}, result)


class AccountDeviceDiagnosticsTest(unittest.TestCase):
    """The synthetic per-account service device carries a "Download diagnostics"
    button of its own, and its identifier -- `entry_id` + ACCOUNT_DEVICE_SUFFIX -- is
    never a key of `coordinator.data`, which is indexed by appliance id. Before the
    account route that lookup could only miss, so the button could only ever hand
    back the degraded dated stub. That is what the ADDHON-210 reporter downloaded and
    sent us, instead of the self-contained dump 5.17.0 shipped for exactly that case:
    with zero appliances the service device is the ONLY device that exists, so its
    broken button was the only one there was to press.
    """

    @staticmethod
    def _account_device(entry):
        """The real service device, built by the function that BUILDS it.

        Spelling `f"{entry_id}_diagnostics"` again here would make this a one-sided
        pin that keeps passing after the suffix moves; reading it out of
        `account_device_info` means a rename has to fail here instead of silently
        re-breaking the button. Same two-sided pin as
        `test_legacy_cleanup.py`'s account-device guard.
        """
        from custom_components.addhon.base_entity import account_device_info

        identifiers = set(account_device_info(entry)["identifiers"])
        # Anti-vacuity: a device with no identifiers resolves no appliance_id and
        # would reach a degraded dump for an unrelated reason.
        assert len(identifiers) == 1, identifiers
        return FakeDevice(identifiers=identifiers)

    def test_the_account_device_returns_the_entry_dump_itself(self):
        # Not "a dump with the same keys": the SAME document. One clock, one shape,
        # one set of privacy guarantees to argue about -- the reason this route
        # delegates instead of assembling a second, subtly different, top-level
        # object that would drift out of step with the first.
        entry = FakeEntry()
        with _FrozenClock(FROZEN):
            result = _run(
                diagnostics.async_get_device_diagnostics(
                    FakeHass(_build_coordinator()), entry, self._account_device(entry)
                )
            )
            expected = _run(
                diagnostics.async_get_config_entry_diagnostics(
                    FakeHass(_build_coordinator()), FakeEntry()
                )
            )
        self.assertEqual(
            [
                "generated_at",
                "entry",
                "last_error",
                "platforms",
                "last_poll",
                "last_fetch",
                "appliances",
            ],
            list(result),
        )
        self.assertEqual(expected, result)

    def test_the_account_device_dump_is_read_from_one_instant(self):
        # The device path reads `_utcnow` before its own degraded returns. On this
        # route that read would be a second one, discarded, while the document
        # carries the entry dump's instant -- harmless today, but it is the shape
        # that lets two ages in one document disagree tomorrow.
        entry = FakeEntry()
        with _FrozenClock(FROZEN) as clock:
            result = _run(
                diagnostics.async_get_device_diagnostics(
                    FakeHass(_build_coordinator()), entry, self._account_device(entry)
                )
            )
        self.assertEqual(FROZEN_TEXT, result["generated_at"])
        self.assertEqual(1, clock.calls)

    def test_an_account_dump_with_no_appliances_still_carries_the_evidence(self):
        # THE ADDHON-210 CASE, verbatim: the cloud answered, the walk found nothing,
        # `coordinator.data` is `{}`. What the reporter needed to send is exactly
        # what a `{"generated_at": ...}` stub cannot say -- how the one inventory
        # call went (`last_fetch`), what the poll counted (`last_poll`), which
        # platforms forwarded (`platforms`) and whether an error was recorded
        # (`last_error`). An empty `appliances` list is the SYMPTOM, not the dump
        # failing to build.
        entry = FakeEntry()
        result = _run(
            diagnostics.async_get_device_diagnostics(
                FakeHass(FakeCoordinator({})), entry, self._account_device(entry)
            )
        )
        self.assertEqual([], result["appliances"])
        for key in ("entry", "last_error", "platforms", "last_poll", "last_fetch"):
            self.assertIn(key, result)

    def test_an_appliance_id_merely_ending_in_the_suffix_stays_an_appliance(self):
        # The guard against the shortcut this file refuses everywhere:
        # `ident[1].endswith(ACCOUNT_DEVICE_SUFFIX)` would answer an APPLIANCE
        # question with the account dump for any cloud id that happens to end that
        # way. The comparison is against the id built from THIS entry, and nothing
        # else.
        entry = FakeEntry()
        decoy_id = f"x{entry.entry_id}{const.ACCOUNT_DEVICE_SUFFIX}"
        # Anti-vacuity: the decoy must really be the near-miss it claims to be.
        self.assertTrue(decoy_id.endswith(const.ACCOUNT_DEVICE_SUFFIX))
        self.assertNotEqual(
            f"{entry.entry_id}{const.ACCOUNT_DEVICE_SUFFIX}", decoy_id
        )
        coord = _build_coordinator()
        coord.data[decoy_id] = coord.data.pop(WD_ID)
        result = _run(
            diagnostics.async_get_device_diagnostics(
                FakeHass(coord), entry, FakeDevice(identifiers={(DOMAIN, decoy_id)})
            )
        )
        self.assertEqual(["generated_at", "appliance"], list(result))
        self.assertEqual("WD", result["appliance"]["type"])

    def test_an_entry_with_no_id_does_not_take_the_account_route(self):
        # With an empty `entry_id` the expected identifier collapses to the bare
        # suffix, which is a perfectly plausible appliance id. The route is skipped
        # rather than guessed at, and the previous behaviour stands.
        entry = FakeEntry(entry_id="")
        device = FakeDevice(identifiers={(DOMAIN, const.ACCOUNT_DEVICE_SUFFIX)})
        with _FrozenClock(FROZEN):
            result = _run(
                diagnostics.async_get_device_diagnostics(
                    FakeHass(_build_coordinator()), entry, device
                )
            )
        self.assertEqual({"generated_at": FROZEN_TEXT}, result)

    def test_no_raw_identity_reaches_the_account_dump(self):
        # Same whole-document scan as `AttributeTimestampPrivacyTest`, run through
        # the new route: delegating must not become a way of reaching the cloud
        # values WITHOUT the validations, so the claim "byte for byte the entry
        # dump" is checked on the property that matters rather than assumed from
        # the call site.
        entry = FakeEntry()
        encoded = json.dumps(
            _run(
                diagnostics.async_get_device_diagnostics(
                    FakeHass(_build_coordinator()), entry, self._account_device(entry)
                )
            )
        )
        for identity in (
            "PLAINTEXT-SERIAL",
            "SN-PLAINTEXT",
            "AA:BB:CC:DD:EE:FF",
            "11:22:33:44:55:66",
            "phone-install-xyz",
            "user@example.com",
            "hunter2",
        ):
            self.assertNotIn(identity, encoded)


# The bare attributes CUSTOM entity classes consume. They have no description
# table, so no walk can derive them: `HonMeanWaterConsumption` needs both
# `totalWaterUsed` and `totalWashCycle`, `HonWashingMachinePauseSwitch` reads
# `machMode`, `HaierClimateEntity` reads `tempIndoor`.
#
# This used to be a constant in diagnostics.py seeded into `mapped_attrs`, plus a
# literal-vs-literal test pinning it. Both are gone: measured on every type it
# covered, the registry walk already supplies all eight names, so the seed
# corrected nothing and the pin fired only when someone edited the constant --
# exactly when they had already remembered it. What remains is the GUARANTEE,
# checked below against behaviour: if a description table is ever narrowed so one
# of these stops being derivable, the dump would start calling it unmapped, and
# this list is what makes that fail loudly instead of being silently patched.
_CUSTOM_ENTITY_ATTRS: dict[str, frozenset[str]] = {
    "WM": frozenset({"totalWashCycle", "totalWaterUsed", "machMode"}),
    "WD": frozenset({"totalWashCycle", "totalWaterUsed", "machMode"}),
    "TD": frozenset({"machMode"}),
    "AC": frozenset({"tempIndoor"}),
}


def _session_drop_reasons() -> tuple | None:
    """The `SETUP_DROP_REASONS` tuple `client/session.py` declares, or None.

    Parsed, never imported, the same idiom `_options_flow_conf_names` above applies to
    config_flow and for the same kind of reason: session.py imports `aiohttp` at module
    level and the stubs in this file do not provide it, so the module whose next drop
    reason this guard exists to catch is precisely the one it must not fail to read.

    None when the constant is not declared yet: the setup census that fills these
    counts lands in a later commit, and this guard arms itself the moment it does
    rather than being written from memory afterwards.
    """
    source = (
        REPO_ROOT / "custom_components" / "addhon" / "client" / "session.py"
    ).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if "SETUP_DROP_REASONS" in names and isinstance(node.value, ast.Tuple):
            return tuple(
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant)
            )
    return None


def _probe_outcome_tokens() -> set[str]:
    """Every string `probe_appliance_list` can put under "outcome", read from its body.

    Read with `ast` rather than by calling the probe over a table of responses: a table
    proves that the tokens it triggers are known, never that no OTHER token exists in
    the function. This guard is about the tokens nobody thought to trigger.
    """
    source = (
        REPO_ROOT
        / "custom_components"
        / "addhon"
        / "client"
        / "transport"
        / "parse.py"
    ).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "probe_appliance_list":
            return {
                value.value
                for mapping in ast.walk(node)
                if isinstance(mapping, ast.Dict)
                for key, value in zip(mapping.keys, mapping.values)
                if isinstance(key, ast.Constant)
                and key.value == "outcome"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            }
    raise AssertionError(
        "parse.probe_appliance_list was not found, so the outcome tokens it can "
        "produce could not be read. If it was renamed, point this guard at the new "
        "name -- do not delete it."
    )


_API_SOURCE = (
    REPO_ROOT / "custom_components" / "addhon" / "client" / "transport" / "api.py"
)


def _api_account_tokens() -> tuple:
    """The `ACCOUNT_TOKENS` tuple `transport/api.py` declares.

    Parsed, never imported, the same idiom `_session_drop_reasons` applies to
    session.py: api.py reaches `connection.py` and through it `aiohttp`, which the
    stubs in this file deliberately do not provide, so the module whose next verdict
    this guard exists to catch is precisely the one it must not fail to read.
    """
    for node in ast.walk(ast.parse(_API_SOURCE.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if "ACCOUNT_TOKENS" in names and isinstance(node.value, ast.Tuple):
            return tuple(
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant)
            )
    raise AssertionError(
        "transport/api.py no longer declares ACCOUNT_TOKENS as a tuple literal: point "
        "this guard at the new form -- do not delete it."
    )


def _account_match_verdicts() -> set[str]:
    """Every string `account_match` can RETURN, read from its body.

    The second half of the pin, and the one a tuple constant cannot give on its own:
    `ACCOUNT_TOKENS` is a promise about what the function emits, and nothing but the
    function's own returns can check it. Read with `ast` rather than by calling the
    function over a table of responses, for the reason `_probe_outcome_tokens` states:
    a table proves the tokens it triggers are known, never that no OTHER token exists.
    """
    for node in ast.walk(ast.parse(_API_SOURCE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == "account_match":
            return {
                statement.value.value
                for statement in ast.walk(node)
                if isinstance(statement, ast.Return)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            }
    raise AssertionError(
        "transport/api.py::account_match was not found, so the verdicts it can produce "
        "could not be read. If it was renamed, point this guard at the new name -- do "
        "not delete it."
    )


class DiagnosticsDriftGuardTest(unittest.TestCase):
    def test_custom_mapped_attrs_are_not_reported_unmapped(self):
        """The behavioural half of the pin above: the EFFECT the comment there claims,
        stated independently of which mechanism delivers it.

        The pin is a literal-vs-literal comparison, so it fires only when someone
        edits the constant -- exactly when they already remembered it -- and says
        nothing about whether the constant still does anything. Measured: it does
        not. Every one of the eight entries is already supplied by the registry walk
        on all four types, so `_CUSTOM_MAPPED_ATTRS` currently contributes NOTHING
        and emptying its only consumer (diagnostics.py:631) leaves the whole suite
        green. It is a safety net for a table change that has not happened; this test
        pins the guarantee it exists to provide, and so keeps holding whether the net
        is load-bearing or redundant.
        """
        for app_type, attrs in _CUSTOM_ENTITY_ATTRS.items():
            cov = diagnostics._coverage(
                app_type,
                {attr: "1" for attr in attrs},
                {},
                FakeAppliance(commands={}),
            )
            for attr in attrs:
                self.assertNotIn(
                    attr, cov["attributes_unmapped"], f"{app_type}.{attr}"
                )

    def test_stop_tokens_track_the_parser_path(self):
        """`stopped_at` may only name a segment the parser actually walks.

        The token set is duplicated in diagnostics.py rather than imported (the HA
        layer imports no transport internals anywhere else in this file), and this is
        what makes the duplication safe: a level added to or renamed in the response
        path has to be reflected here, or the dump would answer "other" for the very
        level it was added to report.
        """
        from custom_components.addhon.client.transport import parse

        self.assertEqual(set(parse.APPLIANCE_LIST_PATH), set(diagnostics._FETCH_STOPS))

    def test_node_type_tokens_track_the_probe(self):
        # "other" exists only on the reader side: the probe emits it too, but the
        # reader must also be able to say it about a token the probe never wrote.
        from custom_components.addhon.client.transport import parse

        self.assertEqual(
            set(parse._NODE_TYPES) | {"other"}, set(diagnostics._FETCH_NODE_TYPES)
        )

    def test_skip_reason_tokens_track_the_session(self):
        # An ASSERT, not a skip. The hatch this replaces was written while the setup
        # census was still a separate commit, so `None` meant "not landed yet"; the
        # constant has landed, and from here `None` can only mean the one thing this
        # guard exists to catch -- session.py stopped declaring SETUP_DROP_REASONS in
        # a form the parser recognises. A skip would report success for exactly that.
        # Same standard as `_probe_outcome_tokens` beside it, which raises.
        declared = _session_drop_reasons()
        self.assertIsNotNone(
            declared,
            "client/session.py no longer declares SETUP_DROP_REASONS as a tuple "
            "literal: point this guard at the new form -- do not delete it.",
        )
        self.assertEqual(declared, diagnostics._FETCH_SKIP_REASONS)

    def test_fetch_outcomes_track_the_writers(self):
        # Two writers fill `outcome`: the probe, and the except branch of
        # load_appliances, which is the only producer of "raised". The reader's domain
        # must be exactly their union -- a token missing here is printed as "other",
        # which is the one reading the block cannot afford for its main field.
        self.assertEqual(
            _probe_outcome_tokens() | {"raised"}, set(diagnostics._FETCH_OUTCOMES)
        )

    def test_account_verdicts_track_the_writer(self):
        # A THREE-WAY pin, because two of the three can drift on their own.
        # `account_match` is the producer, `ACCOUNT_TOKENS` is the promise it makes to
        # the outside, and `_FETCH_ACCOUNTS` is what this file allows into a document.
        # A verdict added to the function and not to the reader is printed as "other",
        # and "other" as the answer to "whose appliances are these" is the one reading
        # nobody can act on -- it is indistinguishable from a value this build refused.
        declared = _api_account_tokens()
        self.assertEqual(set(declared), _account_match_verdicts())
        self.assertEqual(set(declared), set(diagnostics._FETCH_ACCOUNTS))
        # The tuple has no duplicates: a repeated token would make the two set
        # comparisons above agree while the declaration itself was wrong.
        self.assertEqual(len(declared), len(set(declared)))

    def test_the_census_keys_and_the_block_keys_are_the_same_set(self):
        """Every key the WRITER puts in the census is a key the block declares.

        The structural half of `test_the_census_the_probe_builds_survives_into_the_dump`
        below, which checks two responses. This checks the key set itself, so a field
        added to the probe and forgotten in `_fetch_empty` -- or renamed on one side --
        fails here instead of quietly rendering as a missing key in half the states.

        The five keys the census does NOT carry are named rather than subtracted
        blindly: `age_s` is computed by the reader, and the four counters are what
        SETUP made of the list rather than what the CALL returned, so they arrive from
        the client and not from this mapping.
        """
        from custom_components.addhon.client.transport import parse

        census = {
            "at": None,
            "status": None,
            "code": None,
            **parse.probe_appliance_list(
                {"modules": {"applianceList": {"payload": {"appliances": []}}}}
            ),
            "account": "no_appliances",
        }
        block = diagnostics._fetch_empty()
        self.assertLessEqual(set(census), set(block))
        self.assertEqual(
            {"age_s", "expanded", "built", "skipped", "degraded"},
            set(block) - set(census),
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
    def test_a_container_valued_statistics_key_goes_to_statistics_not_meta(self):
        """Order matters between the two carve-outs, and only a container proves it.

        `_coverage` partitions unmapped keys statistics-first, then meta (a Mapping
        or list value, or a denylisted name), then signal. A key that is BOTH a
        statistics key and container-valued satisfies both rules, so which list it
        lands in is decided purely by which carve-out runs first -- and nothing
        asserted that until now. It is not hypothetical: the live fridge publishes
        `mostUsedPrograms` as an empty list AND as a statistics key
        (diagnostics/live-2026-06-22/device-REF.json), so on real hardware this is
        the only shape that exercises the precedence.
        """
        cov = diagnostics._coverage(
            "REF",
            {"mostUsedPrograms": [], "someBlob": {"a": 1}},
            {"mostUsedPrograms": []},
            FakeAppliance(commands={}),
        )
        self.assertIn("mostUsedPrograms", cov["attributes_unmapped_statistics"])
        # ... and NOT double-counted into the meta list, which the sibling
        # container lands in to prove the meta rule is live in the same call.
        self.assertNotIn("mostUsedPrograms", cov["attributes_unmapped_meta"])
        self.assertIn("someBlob", cov["attributes_unmapped_meta"])

    """Coverage noise partition: value-type envelope + scalar denylist + program slots
    move to *_meta; genuine signal stays in *_unmapped; nothing is dropped."""

    def _ac_appliance(self):
        return FakeAppliance(commands={"settings": FakeCommand({
            # genuine writable control (no entity) -> command-param signal
            "humiditySel": FakeParam(value="50", typology="range", rng=(30, 70, 5)),
            # a MAPPED control (climate owns tempSel): counted into the denominator
            # but never into the signal. Without one, `total` and `len(signal)`
            # collapse to the same number and the denominator's mapped half is
            # unfalsifiable -- redefining it as the signal count alone, so every dump
            # reads 100% covered, leaves the class green.
            "tempSel": FakeParam(value="22", typology="range", rng=(16, 30, 1)),
            # command plumbing -> command-param meta
            "category": FakeParam(value="setParameters", typology="enum", values=["setConfig", "setParameters"]),
            "httpEndpoint": FakeParam(value="x", typology="fixed"),
            "operationName": FakeParam(value="x", typology="fixed"),
        })})

    # Split out of `_coverage_ac` so a test can re-run the same fixture with one key
    # removed and compare the two denominators (see the derived-names test below).
    _AC_ATTRS = {
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

    def _coverage_ac(self):
        return diagnostics._coverage("AC", self._AC_ATTRS, {}, self._ac_appliance())

    def test_signal_keeps_genuine_telemetry(self):
        cov = self._coverage_ac()
        for k in ("errors", "weirdSensor", "programsCounter", "programClass"):
            self.assertIn(k, cov["attributes_unmapped"], k)
            self.assertNotIn(k, cov["attributes_unmapped_meta"], k)

    def test_engine_derived_names_are_not_charged_to_the_device(self):
        """`programName` is written by `ApplianceExtra.attributes`, not by the appliance.

        Before issue #93 it landed in `attributes_unmapped` -- the list this module calls
        the gold signal -- and told the reader their device exposed a value addhOn had
        failed to map. It is addhOn's own output.

        It is NAMED rather than dropped: silently vanishing from every list would make
        the coverage section look lossy against the attributes block beside it.
        """
        cov = self._coverage_ac()
        self.assertIn("programName", cov["attributes_unmapped_derived"])
        self.assertNotIn("programName", cov["attributes_unmapped"])
        self.assertNotIn("programName", cov["attributes_unmapped_meta"])

    def test_derived_names_leave_the_denominator_too(self):
        """They are not telemetry, so they must not inflate the coverage ratio either --
        the same treatment statistics and meta already get. Measured by removing the name
        from the fixture: if the denominator were still counting it, dropping it would
        move `attributes_total`."""
        with_name = self._coverage_ac()
        self.assertEqual(with_name["attributes_unmapped_derived"], ["programName"])
        without = diagnostics._coverage(
            "AC",
            {k: v for k, v in self._AC_ATTRS.items() if k != "programName"},
            {},
            self._ac_appliance(),
        )
        self.assertEqual(without["attributes_unmapped_derived"], [])
        self.assertEqual(with_name["attributes_total"], without["attributes_total"])

    def test_engine_derived_attrs_matches_the_engine(self):
        """`_ENGINE_DERIVED_ATTRS` must name every field the engine writes itself.

        A drift guard, not a restatement: the list is a carve-out from the coverage
        SIGNAL, so a newly derived field that nobody adds here is silently reported as a
        device capability the appliance failed to expose -- the exact failure of issue
        #93, and one that no other test can see because nothing reads these fields.

        The engine side is READ FROM SOURCE rather than imported: the assignments live
        inside `attributes()` bodies and only run with a live appliance, so importing
        would prove nothing. Two shapes are collected, which is all the engine uses:
        `data["<name>"] = ...` in the per-type layers, and `self._attributes["<name>"]
        = ...` in `Appliance`.
        """
        import ast as _ast
        from pathlib import Path

        engine = Path(diagnostics.__file__).resolve().parent / "client" / "engine"
        sources = [engine / "appliance.py", *sorted((engine / "appliances").glob("*.py"))]
        self.assertTrue(len(sources) > 5, "engine layout moved; this guard is blind")

        written: set[str] = set()
        for path in sources:
            for node in _ast.walk(_ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, (_ast.Assign, _ast.AugAssign)):
                    continue
                targets = node.targets if isinstance(node, _ast.Assign) else [node.target]
                for target in targets:
                    if not isinstance(target, _ast.Subscript):
                        continue
                    key = target.slice
                    if not (isinstance(key, _ast.Constant) and isinstance(key.value, str)):
                        continue
                    base = target.value
                    is_data = isinstance(base, _ast.Name) and base.id == "data"
                    is_attrs = (
                        isinstance(base, _ast.Attribute)
                        and base.attr == "_attributes"
                        and isinstance(base.value, _ast.Name)
                        and base.value.id == "self"
                    )
                    # `data["parameters"][name] = ...` writes a SHADOW parameter, not a
                    # derived top-level field: its base is a Subscript, not a bare Name.
                    if is_data or is_attrs:
                        written.add(key.value)

        self.assertEqual(
            written,
            set(diagnostics._ENGINE_DERIVED_ATTRS),
            "the engine derives a different set of attributes than _ENGINE_DERIVED_ATTRS "
            "claims; add the new name there or the next dump will blame the device for it",
        )

    def test_the_fridge_selects_identity_fields_count_as_mapped(self):
        """A fixed-key entity's reads must reach `mapped_attrs`, not only `sources`.

        `_CUSTOM_ENTITY_SOURCES` feeds `entities.sources` and nothing else, so naming the
        select's seven attributes there while mapping none of them would report `prCode`
        and `prStr` as unmapped device capabilities on the very dump whose `sources`
        block names the entity that reads them -- rebuilding, for the two members nobody
        had noticed, the contradiction issue #93 was about.

        `programName` is deliberately NOT here: it is engine-derived, so it belongs to
        the derived bucket. Counting it as mapped telemetry would put addhOn's own output
        back into the device's denominator.
        """
        cov = diagnostics._coverage(
            "REF",
            {"prCode": "12", "prStr": "eco", "programName": "No Program"},
            {},
            FakeAppliance(commands={}),
        )
        self.assertEqual(cov["attributes_unmapped"], [])
        self.assertEqual(cov["attributes_unmapped_derived"], ["programName"])

    def test_the_mapped_identity_fields_track_the_selects_own_tuple(self):
        """Computed from the select's tuple, not restated: a field added there is picked
        up here, and one that becomes engine-derived drops out, with no second list to
        forget."""
        from custom_components.addhon.select import HonRefProgramSelect

        mapped_attrs = diagnostics._mapped_sets("REF")[0]
        expected = set(HonRefProgramSelect._REF_ACTIVE_PROGRAM_ATTRS) - set(
            diagnostics._ENGINE_DERIVED_ATTRS
        )
        self.assertTrue(expected, "the tuple is now all engine-derived; re-check this")
        self.assertTrue(expected <= mapped_attrs)
        # The derived member of the tuple stays out. Only that one: `available` is also
        # engine-derived and IS in `mapped_attrs`, because the connectivity binary really
        # reads it -- which is why the derived bucket is a carve-out from the unmapped
        # signal and not a blanket exclusion.
        self.assertNotIn("programName", mapped_attrs)

    def test_a_type_that_maps_the_name_is_untouched(self):
        """The carve-out fires on the SIGNAL only, after the mapped subtraction, so a
        washer -- which really does read `programName` through `sensor.program_name` --
        keeps it out of every unmapped list, derived included, and still counts it.
        """
        cov = diagnostics._coverage(
            "WM", {"programName": "cottons"}, {}, FakeAppliance(commands={})
        )
        self.assertEqual(cov["attributes_unmapped_derived"], [])
        self.assertEqual(cov["attributes_unmapped"], [])
        self.assertEqual(cov["attributes_total"], 1)

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
        # LOSSLESS, the half the name promises and the body never checked: every bare
        # key of the fixture lands in exactly one of the three lists. A key that falls
        # out of all three is a device capability the maintainer never learns exists,
        # which is silent and permanent -- whereas a double count is visible noise.
        # Dropping a row from the meta list without re-homing it leaves the three
        # disjointness assertions above green.
        derived = set(cov["attributes_unmapped_derived"])
        self.assertEqual(signal & derived, set())
        self.assertEqual(meta & derived, set())
        self.assertEqual(stats & derived, set())
        self.assertEqual(
            {
                "errors", "programName", "weirdSensor", "programsCounter",
                "programClass", "commandHistory", "lastConnEvent",
                "mostUsedPrograms", "debugEnabled", "transMode", "resultCode",
                "highTransRate", "programStats", "cloudProgId", "forceDelete",
                "program7", "program19",
            },
            signal | meta | stats | derived,
        )

    def test_command_param_meta_split(self):
        cov = self._coverage_ac()
        self.assertIn("humiditySel", cov["command_params_unmapped"])
        for k in ("category", "httpEndpoint", "operationName"):
            self.assertIn(k, cov["command_params_unmapped_meta"], k)
            self.assertNotIn(k, cov["command_params_unmapped"], k)

    def test_command_param_total_excludes_meta(self):
        # Symmetric with attributes_total: denominator = mapped controls + signal, not
        # inflated by meta params. settings has 5 params (humiditySel + tempSel + 3
        # meta) -> total 5 - 3 meta = 2: one mapped control (tempSel) plus one signal
        # (humiditySel). The two summands are deliberately different numbers so the
        # denominator cannot be satisfied by the signal count alone.
        cov = self._coverage_ac()
        self.assertEqual(cov["command_params_total"], 2)
        self.assertEqual(["humiditySel"], cov["command_params_unmapped"])
        self.assertLess(
            len(cov["command_params_unmapped"]), cov["command_params_total"]
        )


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
        _, mapped_params, _sources, _missing = diagnostics._mapped_sets("WC")
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

    def test_last_error_says_so_when_there_is_no_client_to_ask(self) -> None:
        # A 5.9.3 field dump read `"last_error": null, "appliances": []` and was
        # triaged as an account owning no appliances; it was a failed setup, whose
        # client never reached hass.data. `null` must not be the answer to a
        # question nobody could ask. A failed setup now leaves a record and reads
        # `setup_failed` instead; this marker covers what is left, an entry with no
        # session and nothing said about why.
        result = _run(diagnostics.async_get_config_entry_diagnostics(FakeHass(_build_coordinator()), FakeEntry()))
        self.assertEqual({"status": "client_absent"}, result["last_error"])
        # And it must not be readable as a recorded failure: a consumer keying on
        # `code` would otherwise report an error nobody classified.
        self.assertNotIn("code", result["last_error"])

    def test_last_error_stays_null_when_the_client_recorded_no_failure(self) -> None:
        # The other half of the pair above: a client that WAS asked and had nothing
        # keeps returning null, so the marker cannot be produced by simply owning no
        # appliances. Both dumps carry an empty `appliances`, which is exactly the
        # pair the field report could not tell apart.
        class _Client:
            last_error_code = None

        hass = FakeHass(FakeCoordinator({}))
        hass.data[DOMAIN]["e1"]["client"] = _Client()
        healthy = _run(diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry()))
        failed_setup = _run(
            diagnostics.async_get_config_entry_diagnostics(FakeHass(FakeCoordinator({})), FakeEntry())
        )
        self.assertIsNone(healthy["last_error"])
        self.assertEqual([], healthy["appliances"])
        self.assertEqual([], failed_setup["appliances"])
        self.assertNotEqual(healthy["last_error"], failed_setup["last_error"])

    def test_last_error_folds_missing_entry_data_into_the_same_marker(self) -> None:
        # "No entry data for this entry" and "a bucket with neither a client nor a
        # failure record" are ONE state on purpose (diagnostics.py `_last_error`).
        # They were once the same for a stronger reason -- nothing could produce the
        # second -- and `_store_setup_failure` has since made a client-less bucket a
        # real state, but a client-less bucket with NOTHING in it still is not, and
        # inventing a token for it would send a triager hunting a distinction nothing
        # writes. This pins the fold: an entry absent from hass.data reports the same
        # marker as the record-less bucket above.
        hass = FakeHass(_build_coordinator(), entry_id="other-entry")
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry()))
        self.assertEqual({"status": "client_absent"}, result["last_error"])

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
            # A summary carrying MORE than the two published keys, so the assertion
            # below measures the WHITELIST PROJECTION rather than restating the
            # fixture. `_last_error` bypasses `_redact` by design, so this projection
            # is the only filter on the path; MfaContext (client/transport/oauth.py)
            # holds the JS-Remoting csrf and a signed authorization in exactly these
            # shapes, and masks them in its own __repr__ to keep them out of LOGS.
            last_mfa_summary = {
                "challenge_kind": "email",
                "can_resend": True,
                "host": "eu-login.haier.example",
                "verify": {"csrf": "c-123", "authorization": "signed-blob"},
            }
            _refresh_token = "rt"

        hass = FakeHass(_build_coordinator())
        hass.data[DOMAIN]["e1"]["client"] = _Client()
        result = _run(diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry()))
        le = result["last_error"]
        self.assertEqual("ADDHON-160", le["code"])
        self.assertTrue(le["requires_reauth"])
        self.assertTrue(le["had_refresh_token"])
        self.assertEqual({"challenge_kind": "email", "can_resend": True}, le["mfa"])
        blob = json.dumps(result)
        for secret in ("csrf", "c-123", "authorization", "signed-blob",
                       "eu-login.haier.example"):
            self.assertNotIn(secret, blob, secret)

    def test_last_error_suppresses_a_stale_mfa_summary_outside_the_band(self) -> None:
        """hon_client's submit/resend except-branches (hon_client.py:748, :775) set a
        NON-MFA code without clearing `last_mfa_summary` -- the clears at :753-755 are
        on the success path. The 160-169 band gate is the only thing stopping a 2FA
        challenge summary from being attached to a network fault, which would send a
        triager down the 2FA path for a timeout.
        """
        from custom_components.addhon import error_codes as ec

        # BOTH edges. "Outside the band" is a two-sided statement and only the
        # upper side used to be sampled, so `160 <= code` could be deleted --
        # or written `code <= 169` -- with the whole repository green. The lower
        # half is the reachable one: `classify()`'s auth cascade
        # (error_codes.py:420-429) answers 100/110/120/130/140 for exactly the
        # "token-after-verify" failure this docstring names, and hon_client.py:748
        # assigns it with `last_mfa_summary` still set.
        for code in (ec.INVALID_CREDENTIALS, ec.NETWORK_TIMEOUT):  # 100 below, 400 above
            with self.subTest(code=code.code):

                class _Client:
                    last_error_code = code
                    last_error_phase = "mfa_send"
                    last_mfa_summary = {"challenge_kind": "email", "can_resend": True}
                    _refresh_token = ""

                hass = FakeHass(_build_coordinator())
                hass.data[DOMAIN]["e1"]["client"] = _Client()
                result = _run(
                    diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry())
                )
                self.assertNotIn("mfa", result["last_error"])


class LastPollCensusDiagnosticsTest(unittest.TestCase):
    """The entry dump says whether the poll returned nothing or dropped everything.

    A real 5.9.3 field dump reads `"data": {"entry": {...}, "last_error": null,
    "appliances": []}` in full, and three states produce that exact file: an account
    with no appliances, a poll that dropped every appliance the cloud returned, and a
    setup that left no client at all. The `Partial update: %d/%d` WARNING separates
    the first two on the user's machine and never reaches the file, so `last_poll`
    carries the same fact into the dump: counts plus ADDHON catalog labels.
    """

    def _hass_with_census(self, census, coordinator=None):
        class _Client:
            last_poll_census = census

        hass = FakeHass(coordinator if coordinator is not None else FakeCoordinator({}))
        hass.data[DOMAIN]["e1"]["client"] = _Client()
        return hass

    def test_last_poll_is_null_when_no_cycle_completed(self) -> None:
        # No client at all (the failed-setup state): the key is still there, so a
        # reader never has to wonder whether this dump predates the census.
        result = _run(
            diagnostics.async_get_config_entry_diagnostics(
                FakeHass(_build_coordinator()), FakeEntry()
            )
        )
        self.assertIn("last_poll", result)
        self.assertIsNone(result["last_poll"])

    def test_an_empty_account_and_a_dropped_one_no_longer_look_alike(self) -> None:
        empty = _run(
            diagnostics.async_get_config_entry_diagnostics(
                self._hass_with_census({"returned": 0, "kept": 0, "dropped": []}),
                FakeEntry(),
            )
        )
        dropped = _run(
            diagnostics.async_get_config_entry_diagnostics(
                self._hass_with_census(
                    {
                        "returned": 3,
                        "kept": 0,
                        "dropped": [
                            {"code": "ADDHON-450", "reason": "hOn server error"},
                            {"code": "ADDHON-450", "reason": "hOn server error"},
                            {"code": "ADDHON-220", "reason": "Could not load appliance data"},
                        ],
                    }
                ),
                FakeEntry(),
            )
        )
        # The two dumps still agree on everything the field report carried, which is
        # the whole reason the census had to be added rather than inferred.
        self.assertEqual([], empty["appliances"])
        self.assertEqual([], dropped["appliances"])
        self.assertIsNone(empty["last_error"])
        self.assertIsNone(dropped["last_error"])
        # ...and disagree where it counts.
        self.assertEqual(0, empty["last_poll"]["returned"])
        self.assertEqual([], empty["last_poll"]["dropped"])
        self.assertEqual(3, dropped["last_poll"]["returned"])
        self.assertEqual(0, dropped["last_poll"]["kept"])
        self.assertEqual(
            ["ADDHON-450", "ADDHON-450", "ADDHON-220"],
            [drop["code"] for drop in dropped["last_poll"]["dropped"]],
        )

    def test_the_census_the_client_builds_survives_into_the_dump(self) -> None:
        # Built by the WRITER (hon_client._poll_census) rather than hand-written, so
        # this pins the whole path: the reader publishes what the poll recorded, and
        # the two halves cannot drift into agreeing only in this file. The failure
        # handed to it carries a nickname and a MAC, which is what a cloud error body
        # or a transport URL looks like; `last_poll` skips `_redact` by design, so the
        # leak check is run over the finished document.
        from custom_components.addhon.hon_client import _poll_census

        census = _poll_census(
            3,
            2,
            [("***", RuntimeError("decode error for Kitchen Washer at AA:BB:CC:DD:EE:FF"))],
        )
        result = _run(
            diagnostics.async_get_config_entry_diagnostics(
                self._hass_with_census(census, _build_coordinator()), FakeEntry()
            )
        )
        self.assertEqual(census, result["last_poll"])
        self.assertEqual(3, result["last_poll"]["returned"])
        self.assertEqual(2, result["last_poll"]["kept"])
        self.assertEqual(
            ["ADDHON-470"], [drop["code"] for drop in result["last_poll"]["dropped"]]
        )
        blob = json.dumps(result)  # also proves the block is serializable as it stands
        for leak in ("Kitchen Washer", "AA:BB:CC:DD:EE:FF", "decode error for"):
            self.assertNotIn(leak, blob, leak)


class _FetchClient:
    """The client double `_build_last_fetch` reads.

    It carries only the five names the block asks for, and every one of them is
    optional: the reader reaches them through `getattr(..., None)` precisely because a
    client between two sessions, or a double in a test, is not obliged to have them.
    """

    def __init__(self, census=None, **attrs) -> None:
        self.last_appliance_fetch = census
        for name, value in attrs.items():
            setattr(self, name, value)


class _RaisingFetchClient:
    """A client whose `setup_drops` raises, the way a property reading a session
    being torn down on the hOn loop thread can."""

    last_appliance_fetch = {"outcome": "ok", "count": 0}

    @property
    def setup_drops(self):
        raise RuntimeError("session torn down mid-dump")


class LastFetchDiagnosticsTest(unittest.TestCase):
    """The dump names the level at which the appliance-list walk stopped.

    The field case: the cloud answered with `result` keys ['executionTime', 'modules',
    'success'] and `modules` keys ['applianceList'], the hOn app showed two appliances
    for the account, and the dump read `"appliances": []` with `"last_error": null`.
    That file is compatible with six different states, and the maintainer could not
    answer the only question that matters -- whose bug it is -- from it. `last_fetch`
    is what separates them, and every value in it is a token of a frozenset written in
    diagnostics.py, a range-checked int, a shape-checked catalog label or an instant
    `_stamp_text` validated: never a string chosen by the cloud.
    """

    def _dump(self, client=None, coordinator=None):
        hass = FakeHass(coordinator if coordinator is not None else FakeCoordinator({}))
        if client is not None:
            hass.data[DOMAIN]["e1"]["client"] = client
        return _run(diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry()))

    def _fetch(self, census=None, **attrs):
        return self._dump(_FetchClient(census, **attrs))["last_fetch"]

    def test_last_fetch_is_never_null(self) -> None:
        # `null` is the reading this block exists to abolish: it is what a dump says
        # today for "no session", "no fetch" and "a fetch this reader could not read"
        # alike. Every harness in this file must produce a dict with a state token.
        dumps = [
            self._dump(),
            self._dump(_FetchClient()),
            self._dump(_FetchClient({"outcome": "ok", "count": 0})),
            self._dump(_FetchClient("not-a-mapping")),
            self._dump(_RaisingFetchClient()),
            _entry_diag()[0],
        ]
        for result in dumps:
            with self.subTest(state=result["last_fetch"].get("state")):
                self.assertIsInstance(result["last_fetch"], dict)
                self.assertIn(result["last_fetch"]["state"], diagnostics._FETCH_STATES)

    def test_no_client_reads_client_absent(self) -> None:
        # The whole suite runs on a bucket holding only {"coordinator": ...}: no
        # client to ask, and no record of a failure either. Saying so is a different
        # statement from "the client was asked and had nothing" -- and, since
        # `_store_setup_failure` exists, a different one from "the setup failed, and
        # here is what the call returned before it did" (SetupFailureRecordTest).
        fetch = self._dump()["last_fetch"]
        self.assertEqual("client_absent", fetch["state"])
        for key, value in fetch.items():
            if key == "state":
                continue
            with self.subTest(key=key):
                self.assertEqual({} if key in ("skipped", "degraded") else None, value)

    def test_a_client_without_a_census_reads_never_ran(self) -> None:
        fetch = self._fetch(None)
        self.assertEqual("never_ran", fetch["state"])
        self.assertIsNone(fetch["outcome"])

    def test_the_key_set_is_the_same_in_every_state(self) -> None:
        # A field-by-field diff between two downloads of the same issue is only
        # meaningful if the key set is stable: a block that drops keys when it has
        # nothing to say turns "this got worse" into "this file is shaped differently".
        blocks = {
            "recorded": self._fetch({"outcome": "ok", "count": 0}),
            "never_ran": self._fetch(None),
            "client_absent": self._dump()["last_fetch"],
            "unreadable": self._dump(_RaisingFetchClient())["last_fetch"],
        }
        for state, block in blocks.items():
            with self.subTest(state=state):
                self.assertEqual(state, block["state"])
        key_sets = {frozenset(block) for block in blocks.values()}
        self.assertEqual(1, len(key_sets), key_sets)
        self.assertEqual(set(diagnostics._FETCH_STATES), set(blocks))
        # The empty halves are built PER CALL, not spread from a module constant.
        # `{**CONST}` is a shallow copy, so a constant would hand every block in the
        # process the same two dicts -- aliased to the constant itself -- and one
        # in-place write anywhere downstream would then appear in every later dump of
        # every config entry. The key set is what the constant existed to stabilise,
        # and it is asserted above, so nothing is lost by unsharing the values.
        for left, right in (("never_ran", "client_absent"), ("client_absent", "unreadable")):
            for key in ("skipped", "degraded"):
                with self.subTest(pair=(left, right), key=key):
                    self.assertIsNot(blocks[left][key], blocks[right][key])
        blocks["never_ran"]["skipped"]["written_by_a_downstream_caller"] = 1
        self.assertEqual({}, self._fetch(None)["skipped"])

    def test_an_empty_payload_and_a_schema_drift_no_longer_look_alike(self) -> None:
        # THE acceptance test. Both dumps carry `"appliances": []` and a null
        # `last_error` -- the exact file the field report arrived as -- and until now
        # nothing else in the document differed at all.
        empty = self._fetch({"status": 200, "outcome": "ok", "count": 0})
        drift = self._fetch(
            {
                "status": 200,
                "outcome": "missing_key",
                "stopped_at": "payload",
                "node_type": "dict",
                "siblings": 2,
                "count": None,
            }
        )
        self.assertNotEqual(empty, drift)
        # The cloud answered, and it answered with nothing: the bug is not ours.
        self.assertEqual("ok", empty["outcome"])
        self.assertEqual(0, empty["count"])
        # The cloud answered with something our walk could not follow, and the block
        # says at which level to look.
        self.assertEqual("missing_key", drift["outcome"])
        self.assertEqual("payload", drift["stopped_at"])
        self.assertEqual(2, drift["siblings"])
        self.assertIsNone(drift["count"])

    def test_the_census_the_probe_builds_survives_into_the_dump(self) -> None:
        # Built by the WRITER rather than hand-written, so this pins the whole path:
        # every key `probe_appliance_list` emits is a key this reader looks up, and a
        # rename on either side would silently turn the block's main field into
        # `null` while both halves kept their own tests green.
        from custom_components.addhon.client.transport.parse import probe_appliance_list

        # The envelope the field report actually carried: `result` keys
        # ['executionTime', 'modules', 'success'], `modules` keys ['applianceList'].
        # What sat under `applianceList` is exactly what no dump could say, which is
        # why the walk has to report the level it stopped at.
        drifted = {
            "executionTime": 1.2,
            "modules": {"applianceList": {"AA:BB:CC:DD:EE:FF": 1}},
            "success": True,
        }
        empty = {"modules": {"applianceList": {"payload": {"appliances": []}}}}
        blocks = [
            self._fetch(
                {"at": FROZEN, "status": 200, "code": None, **probe_appliance_list(body)}
            )
            for body in (drifted, empty)
        ]
        self.assertNotEqual(blocks[0], blocks[1])
        self.assertEqual("missing_key", blocks[0]["outcome"])
        self.assertEqual("payload", blocks[0]["stopped_at"])
        self.assertEqual(1, blocks[0]["siblings"])
        self.assertIsNone(blocks[0]["count"])
        self.assertEqual("ok", blocks[1]["outcome"])
        self.assertEqual(0, blocks[1]["count"])
        # The sibling key beside the level we stopped at was chosen by the cloud.
        self.assertNotIn("AA:BB:CC:DD:EE:FF", json.dumps(blocks))

    def test_a_failed_module_is_not_an_empty_account(self) -> None:
        """The second acceptance test, on the level above the walk.

        The field report reached us as an empty list under a 200 with a well-formed
        envelope, and `outcome: "ok", count: 0` already reads that as "the cloud sent
        nothing, so the bug is not ours". That reading is only sound if the cloud also
        DECLARED the call a success -- and the module envelope carries its own
        `success` beside the payload, which neither this integration nor the official
        app has ever read. With it false the app shows zero appliances too, and every
        other field in this block is identical to a legitimately empty account.
        """
        empty = self._fetch(
            {"status": 200, "outcome": "ok", "count": 0,
             "envelope_ok": True, "module_ok": True, "auth_keys": 0,
             "account": "no_appliances"}
        )
        failed = self._fetch(
            {"status": 200, "outcome": "ok", "count": 0,
             "envelope_ok": True, "module_ok": False, "auth_keys": 0,
             "account": "no_appliances"}
        )
        self.assertNotEqual(empty, failed)
        self.assertIs(True, empty["module_ok"])
        self.assertIs(False, failed["module_ok"])
        # The rest of the block is word for word the same, which is the point: nothing
        # else in the document could have separated these two dumps.
        for key in ("outcome", "count", "status", "envelope_ok", "account"):
            with self.subTest(key=key):
                self.assertEqual(empty[key], failed[key])

    def test_a_session_that_resolved_to_another_account_is_visible(self) -> None:
        # The third state the block could not name. All three of these carry
        # `outcome: "ok"`, and two of them carry `count: 0`; before `account` the dump
        # answered "the cloud sent an empty list" to all three and stopped.
        empty = self._fetch({"outcome": "ok", "count": 0, "account": "no_appliances"})
        blind = self._fetch({"outcome": "ok", "count": 0, "account": "no_claim"})
        theirs = self._fetch({"outcome": "ok", "count": 2, "account": "mismatch"})
        self.assertEqual("no_appliances", empty["account"])
        self.assertEqual("no_claim", blind["account"])
        self.assertEqual("mismatch", theirs["account"])
        self.assertNotEqual(empty, blind)

    def test_a_success_that_is_not_a_boolean_is_never_coerced(self) -> None:
        # `_closed_bool` is the mirror of `_bounded_int`: that one refuses a bool
        # because isinstance(True, int) is True, this one refuses an INT because `1` is
        # not the same statement as `true`. Coercing with `bool(value)` would let the
        # field answer the very question it exists to establish -- and would render
        # every non-empty string the cloud sent as a declared success.
        for junk in ("true", "false", "", 1, 0, [], {}, [1], object()):
            with self.subTest(junk=type(junk).__name__):
                fetch = self._fetch({"envelope_ok": junk, "module_ok": junk})
                self.assertIsNone(fetch["envelope_ok"])
                self.assertIsNone(fetch["module_ok"])
        # ...and a real boolean still comes through, both ways, so the assertions above
        # are not passing merely because the fields are always null.
        for value in (True, False):
            with self.subTest(value=value):
                fetch = self._fetch({"envelope_ok": value, "module_ok": value})
                self.assertIs(value, fetch["envelope_ok"])
                self.assertIs(value, fetch["module_ok"])

    def test_an_auth_key_count_is_a_count_and_nothing_else(self) -> None:
        # `auth_keys` is the size of an object whose values include a bearer token, so
        # the count is the entire permitted output. A string that fell through would be
        # a key name or a token, i.e. the one thing this field is designed not to be.
        for junk in ("2", True, -1, diagnostics._FETCH_MAX_INT + 1,
                     ["cognitoTokenNew"], {"cognitoTokenNew": "SECRET"}):
            with self.subTest(junk=type(junk).__name__):
                self.assertIsNone(self._fetch({"auth_keys": junk})["auth_keys"])
        # 0 is the healthy baseline and must survive as a value, not be treated as
        # "nothing to report": it is what makes any other number a signal.
        self.assertEqual(0, self._fetch({"outcome": "ok", "auth_keys": 0})["auth_keys"])
        self.assertEqual(3, self._fetch({"outcome": "ok", "auth_keys": 3})["auth_keys"])

    def test_an_unknown_account_verdict_collapses_to_other(self) -> None:
        # An identifier is exactly the shape a wrong writer would put here, so the
        # reader keeps the finding and loses the value -- the same rule `outcome` and
        # `node_type` already follow.
        for junk in ("ACCOUNT-0012345", "user@example.com", "3C:71:BF:AA:BB:CC"):
            with self.subTest(junk=junk):
                self.assertEqual("other", self._fetch({"account": junk})["account"])
        blob = json.dumps(self._dump(_FetchClient({
            "account": "3C:71:BF:AA:BB:CC", "auth_keys": "cognitoTokenNew",
            "envelope_ok": "user@example.com",
        })))
        for leak in ("3C:71:BF:AA:BB:CC", "cognitoTokenNew", "user@example.com"):
            self.assertNotIn(leak, blob, leak)

    def test_the_healthy_and_the_reporter_envelope_are_told_apart(self) -> None:
        """The two responses of the investigation, through the real writer.

        `healthy()` is the live 2026-08-24 capture and `reporter()` is the ADDHON-210
        envelope: identical at both levels their log records, which is what closed the
        "the API changed" hypothesis. Everything the dump could say about them used to
        be identical too. Built by the WRITER rather than hand-written, so the pin
        covers the whole path and not this file's idea of it.
        """
        from custom_components.addhon.client.transport.parse import probe_appliance_list
        from _envelopes import healthy, reporter

        blocks = {
            name: self._fetch(
                {"at": FROZEN, "status": 200, "code": None,
                 **probe_appliance_list(body), "account": account}
            )
            for name, body, account in (
                ("healthy", healthy(), "match"),
                ("reporter", reporter(), "no_appliances"),
            )
        }
        self.assertNotEqual(blocks["healthy"], blocks["reporter"])
        # The calibration: every field of the healthy capture, as observed on the wire.
        self.assertEqual("ok", blocks["healthy"]["outcome"])
        self.assertEqual(1, blocks["healthy"]["count"])
        self.assertIs(True, blocks["healthy"]["envelope_ok"])
        self.assertIs(True, blocks["healthy"]["module_ok"])
        self.assertEqual(0, blocks["healthy"]["auth_keys"])
        self.assertEqual("match", blocks["healthy"]["account"])
        # ...and the reporter's, which is now a finished diagnosis rather than a
        # shrug: the cloud declared success at both levels, the session resolved to
        # the account we authenticated as, and that account owns nothing.
        self.assertEqual(0, blocks["reporter"]["count"])
        self.assertIs(True, blocks["reporter"]["module_ok"])
        self.assertEqual("no_appliances", blocks["reporter"]["account"])
        # The capture carries a MAC, a serial and a nickname on its one appliance.
        blob = json.dumps(blocks)
        for leak in ("AA:BB:CC:DD:EE:FF", "PLAINTEXT-SERIAL", "Kitchen Fridge",
                     "ACCOUNT-OURS", "user#eu-west-1"):
            self.assertNotIn(leak, blob, leak)

    def test_a_dropped_inventory_is_not_an_empty_account(self) -> None:
        # State (D): the cloud sent two appliances and this setup dropped both at the
        # empty-MAC guard, which is the one drop in session.py that does not even log.
        fetch = self._fetch(
            {"status": 200, "outcome": "ok", "count": 2},
            setup_expanded=2,
            appliance_count=0,
            setup_drops={"mac_empty": 2},
        )
        self.assertEqual(2, fetch["count"])
        self.assertEqual(2, fetch["expanded"])
        self.assertEqual(0, fetch["built"])
        self.assertEqual({"mac_empty": 2}, fetch["skipped"])

    def test_a_moved_endpoint_is_visible_as_a_status(self) -> None:
        # State (C): a 4xx with a JSON body is delivered as a success by
        # connection.py, so without the status it is indistinguishable from drift.
        fetch = self._fetch(
            {"status": 404, "outcome": "missing_key", "stopped_at": "modules"}
        )
        self.assertEqual(404, fetch["status"])
        self.assertEqual("modules", fetch["stopped_at"])

    def test_a_raised_fetch_is_visible_as_an_outcome(self) -> None:
        # State (E): the POST never reached a body. Recorded on the error path, which
        # is the half a census written after the call could never carry.
        fetch = self._fetch(
            {"status": None, "code": "ADDHON-470", "outcome": "raised"}
        )
        self.assertEqual("recorded", fetch["state"])
        self.assertEqual("raised", fetch["outcome"])
        self.assertEqual("ADDHON-470", fetch["code"])
        self.assertIsNone(fetch["status"])

    def test_a_zoned_account_reconciles(self) -> None:
        # State (H): one cloud entry with `zone: 2` is expanded into three appliance
        # objects, so `expanded > count` is a SANE reading and the block must be able
        # to show it rather than look like an inconsistency.
        fetch = self._fetch(
            {"status": 200, "outcome": "ok", "count": 1},
            setup_expanded=3,
            appliance_count=3,
        )
        self.assertEqual(1, fetch["count"])
        self.assertEqual(3, fetch["expanded"])
        self.assertEqual(3, fetch["built"])

    def test_unknown_tokens_collapse_to_other(self) -> None:
        # The leak test on the reader side. Every token field is looked up in a
        # frozenset written in diagnostics.py; a value from anywhere else is reported
        # as "other", which keeps the finding ("the writer said something this build
        # does not know") and loses the value.
        fetch = self._fetch(
            {
                "outcome": "3C:71:BF:AA:BB:CC",
                "stopped_at": "user@example.com",
                "node_type": "Kitchen Washer",
            }
        )
        self.assertEqual("other", fetch["outcome"])
        self.assertEqual("other", fetch["stopped_at"])
        self.assertEqual("other", fetch["node_type"])
        blob = json.dumps(self._dump(_FetchClient({
            "outcome": "3C:71:BF:AA:BB:CC",
            "stopped_at": "user@example.com",
            "node_type": "Kitchen Washer",
        })))
        for leak in ("3C:71:BF:AA:BB:CC", "user@example.com", "Kitchen Washer"):
            self.assertNotIn(leak, blob, leak)

    def test_a_str_subclass_token_cannot_smuggle_text(self) -> None:
        # `isinstance` admits subclasses, and a str subclass may define __eq__/__hash__
        # so that it IS "ok" for every membership test while its characters are a MAC.
        # `_closed_token` compares with `type(value) is str` for exactly this, and the
        # JSON encoder writes characters, not equality classes.
        class Tok(str):
            def __eq__(self, other):
                return other == "ok" or str.__eq__(self, other)

            def __ne__(self, other):
                return not self.__eq__(other)

            def __hash__(self):
                return hash("ok")

        smuggler = Tok("AA:BB:CC:DD:EE:FF")
        # The premise of the test: this object passes the membership test the naive
        # implementation would have used.
        self.assertIn(smuggler, diagnostics._FETCH_OUTCOMES)
        result = self._dump(_FetchClient({"outcome": smuggler}))
        self.assertEqual("other", result["last_fetch"]["outcome"])
        self.assertNotIn("AA:BB:CC:DD:EE:FF", json.dumps(result))

    def test_an_unknown_code_label_is_dropped(self) -> None:
        # A catalog label cannot be enumerated here without duplicating error_codes.py,
        # so it is bounded by shape. Anything that is not "ADDHON-" plus exactly three
        # digits is not a label this build can vouch for.
        for junk in ("not-a-label", "ADDHON-99", "ADDHON-4700"):
            with self.subTest(code=junk):
                self.assertIsNone(self._fetch({"code": junk})["code"])

    def test_unknown_skip_reasons_are_not_copied(self) -> None:
        # `_skip_census` iterates the ALLOWLIST, never the mapping it received: a key
        # chosen by the writer is a key `_redact` would publish, because `_redact`
        # masks by key name and has never heard of this one.
        fetch = self._fetch(
            {"outcome": "ok", "count": 2},
            setup_drops={"AA:BB:CC:DD:EE:FF": 1, "mac_empty": 2},
        )
        self.assertEqual({"mac_empty": 2}, fetch["skipped"])
        self.assertNotIn("AA:BB:CC:DD:EE:FF", json.dumps(fetch))

    def test_unknown_degraded_keys_are_not_copied(self) -> None:
        # The session keys its degradation ledger by f"{mac}#{zone}"; the reduction to
        # labels happens on the session side, and this is the second wall: a key that
        # is not a label is dropped whole, value included.
        fetch = self._fetch(
            {"outcome": "ok", "count": 2},
            degraded_census={"AA:BB:CC#0": 3, "ADDHON-230": 2},
        )
        self.assertEqual({"ADDHON-230": 2}, fetch["degraded"])
        self.assertNotIn("AA:BB:CC#0", json.dumps(fetch))

    def test_a_skip_count_that_is_not_a_count_is_dropped(self) -> None:
        # The two tests above put their hostile string in the KEY, which the allowlist
        # refuses without ever looking at the value. This is the other half: a KNOWN
        # key whose VALUE is text. `_skip_census` runs `_bounded_int` on it for
        # exactly this reason -- the census arrives through a `getattr` on
        # `_hon_instance`, which in a test, or beside a session double, is any object
        # at all -- and without this the check is invisible to the suite and the next
        # simplification writes `out[reason] = raw.get(reason)`.
        fetch = self._fetch(
            {"outcome": "ok", "count": 2},
            setup_drops={"mac_empty": "AA:BB:CC:DD:EE:FF"},
        )
        self.assertEqual({}, fetch["skipped"])
        self.assertNotIn("AA:BB:CC:DD:EE:FF", json.dumps(fetch))
        # A bool is refused too: isinstance(True, int) is True, and `"mac_empty": true`
        # is a reader's problem forever.
        boolean = self._fetch({"outcome": "ok"}, setup_drops={"mac_empty": True})
        self.assertEqual({}, boolean["skipped"])

    def test_a_degraded_count_that_is_not_a_count_drops_the_whole_row(self) -> None:
        # Keys and values move together or not at all: a valid label with a text count
        # takes the label out with it, rather than publishing the label beside a
        # value this reader could not vouch for.
        fetch = self._fetch(
            {"outcome": "ok", "count": 2},
            degraded_census={"ADDHON-230": "AA:BB:CC:DD:EE:FF"},
        )
        self.assertEqual({}, fetch["degraded"])
        self.assertNotIn("AA:BB:CC:DD:EE:FF", json.dumps(fetch))

    def test_the_three_setup_counters_are_range_checked_like_the_others(self) -> None:
        # `count` and `status` have junk-value tests; `siblings`, `expanded` and
        # `built` had none, so their `_bounded_int` calls could be deleted with the
        # suite green -- and three of the block's fourteen fields would then publish
        # whatever the writer returned. Same table, same three refusals.
        self.assertIsNone(self._fetch({"outcome": "ok", "siblings": True})["siblings"])
        self.assertIsNone(self._fetch({"outcome": "ok", "siblings": "2"})["siblings"])
        self.assertIsNone(self._fetch({"outcome": "ok", "siblings": -1})["siblings"])
        for junk in ("3", True, -1, diagnostics._FETCH_MAX_INT + 1):
            with self.subTest(junk=junk):
                self.assertIsNone(
                    self._fetch({"outcome": "ok"}, setup_expanded=junk)["expanded"]
                )
                self.assertIsNone(
                    self._fetch({"outcome": "ok"}, appliance_count=junk)["built"]
                )
        # ...and a legitimate value still passes, so the assertions above are not
        # passing merely because the fields are always null.
        healthy = self._fetch({"outcome": "ok"}, setup_expanded=3, appliance_count=2)
        self.assertEqual(3, healthy["expanded"])
        self.assertEqual(2, healthy["built"])

    def test_a_huge_degraded_map_is_capped(self) -> None:
        # 20 as a LITERAL, not as `diagnostics._FETCH_MAX_LABEL_ROWS`: an expected
        # value borrowed from the code under test pins the existence of a cap and
        # never its value, so widening the constant to 1000 would leave this green
        # while the block grew fifty times. The input is 100 valid labels, so the
        # answer is exact and the number a reviewer wants to see is in the test.
        census = {f"ADDHON-{200 + i}": 1 for i in range(100)}
        fetch = self._fetch({"outcome": "ok", "count": 1}, degraded_census=census)
        self.assertEqual(20, len(fetch["degraded"]))
        self.assertEqual(20, diagnostics._FETCH_MAX_LABEL_ROWS)

    def test_out_of_range_status_is_refused(self) -> None:
        # `True` is in the list on purpose: isinstance(True, int) is True, and a
        # `"status": true` in the document would be a reader's problem forever.
        for junk in (99, 600, "200", True):
            with self.subTest(status=junk):
                self.assertIsNone(self._fetch({"status": junk})["status"])

    def test_a_boolean_count_is_not_an_int(self) -> None:
        self.assertIsNone(self._fetch({"outcome": "ok", "count": True})["count"])

    def test_a_string_instant_is_never_echoed(self) -> None:
        # `_stamp_text` promises its output was built by datetime.isoformat. A cloud
        # string that merely looks like an instant would break that promise while
        # looking identical in the file.
        fetch = self._fetch({"at": "2026-08-21T04:11:07"})
        self.assertIsNone(fetch["at"])
        self.assertIsNone(fetch["age_s"])
        self.assertNotIn("2026-08-21T04:11:07", json.dumps(fetch))

    def test_a_naive_instant_yields_neither_a_stamp_nor_an_age(self) -> None:
        # `_as_utc(..., assume_naive_utc=False)` returns None for a naive datetime, so
        # both halves blank together. The writer always stamps
        # `datetime.now(timezone.utc)`, so a naive value can only come from a foreign
        # object, and refusing it is the right answer.
        fetch = self._fetch({"at": datetime(2026, 8, 21, 4, 11, 7)})
        self.assertIsNone(fetch["at"])
        self.assertIsNone(fetch["age_s"])

    def test_at_and_age_move_together(self) -> None:
        # Both halves hang off ONE `moment`, so an instant `_as_utc` refuses blanks
        # BOTH: that is the coupling that matters, because it is what stops a value
        # this reader could not validate from being half-published.
        with _FrozenClock(FROZEN):
            for at in (
                FROZEN - timedelta(seconds=30),
                datetime(2026, 8, 21, 4, 11, 7),
                "2026-08-21T04:11:07",
                object(),
            ):
                with self.subTest(at=type(at).__name__):
                    fetch = self._fetch({"at": at})
                    self.assertEqual(fetch["at"] is None, fetch["age_s"] is None)

    def test_an_out_of_range_age_keeps_the_instant_that_explains_it(self) -> None:
        # The FIRST of the two ways the halves part, and the reason the coupling is
        # not stated as an invariant. A host that stamped the fetch before NTP
        # corrected its clock -- Home Assistant on a Pi with no RTC, a container
        # started before sync -- writes an instant `_as_utc` accepts and `_stamp_text`
        # renders, whose age then falls outside +/-10 years and is refused.
        #
        # `at` MUST survive that: it is the only field in the document that explains
        # why every other age in the same dump looks absurd. Blanking it to satisfy a
        # tidier-sounding invariant would delete the finding.
        before_ntp = datetime(1970, 1, 1, 0, 0, 11, tzinfo=timezone.utc)
        with _FrozenClock(FROZEN):
            skewed = self._fetch({"outcome": "ok", "count": 0, "at": before_ntp})
        self.assertEqual("1970-01-01T00:00:11+00:00", skewed["at"])
        self.assertEqual(before_ntp.isoformat(), skewed["at"])
        self.assertIsNone(skewed["age_s"])

    def test_a_hostile_isoformat_loses_the_stamp_and_keeps_the_age(self) -> None:
        # The SECOND way they part, and the test that pins `at` to `_stamp_text`
        # rather than to `datetime.isoformat`. `_as_utc` accepts a datetime SUBCLASS
        # (it is a datetime), `_age_seconds` computes a real age from it, and
        # `_stamp_text` is the only thing standing between an overridden
        # `isoformat()` and the file: it re-validates the rendered text against
        # `_ISO_RE` and a 40-character cap.
        #
        # Every other test in this class is satisfied by a bare `moment.isoformat()`
        # -- the string cases never reach the render at all, because `_as_utc`
        # refused them first -- so without this one the validator is dead weight as
        # far as the suite can tell, and the next simplification deletes it.
        class _HostileStamp(datetime):
            def isoformat(self, *args, **kwargs):
                return "AA:BB:CC:DD:EE:FF"

        moment = _HostileStamp(2026, 8, 13, 7, 9, 10, tzinfo=timezone.utc)
        with _FrozenClock(FROZEN):
            result = self._dump(_FetchClient({"outcome": "ok", "count": 0, "at": moment}))
        fetch = result["last_fetch"]
        self.assertIsNone(fetch["at"])
        self.assertNotIn("AA:BB:CC:DD:EE:FF", json.dumps(result))
        # The age survives because it is COMPUTED, not echoed, and `generated_at` in
        # the same document lets a reader place it.
        self.assertEqual(30, fetch["age_s"])

    def test_an_absurd_age_is_refused(self) -> None:
        with _FrozenClock(FROZEN):
            ancient = self._fetch({"at": FROZEN - timedelta(days=365 * 50)})
            recent = self._fetch({"at": FROZEN - timedelta(days=365 * 5)})
            ahead = self._fetch({"at": FROZEN + timedelta(days=365 * 5)})
        self.assertIsNone(ancient["age_s"])
        self.assertGreater(recent["age_s"], 0)
        self.assertLessEqual(recent["age_s"], diagnostics._FETCH_MAX_AGE_S)
        # A NEGATIVE age is a finding, not nonsense: it says the reporter's clock and
        # the one that stamped the fetch disagree. The bound must not erase it.
        self.assertLess(ahead["age_s"], 0)

    def test_last_fetch_does_not_read_the_clock_again(self) -> None:
        # `now` is passed in, like every other age in the document, so the whole dump
        # still describes one instant.
        moment = FROZEN - timedelta(seconds=51_840)
        with _FrozenClock(FROZEN) as clock:
            fetch = self._fetch({"at": moment})
        self.assertEqual(1, clock.calls)
        self.assertEqual(moment.isoformat(), fetch["at"])
        self.assertEqual(51_840, fetch["age_s"])

    def test_last_fetch_sits_between_last_poll_and_appliances(self) -> None:
        # The placement IS the content of the change: the two blocks answer the same
        # family of question ("what did the cloud give us") and a reader who found
        # `last_poll` must fall over this one without being told it exists.
        result = self._dump(_FetchClient({"outcome": "ok", "count": 0}))
        self.assertEqual(["last_poll", "last_fetch", "appliances"], list(result)[-3:])
        # And it did not take over the first key, which `generated_at` owns.
        self.assertEqual("generated_at", next(iter(result)))

    def test_a_populated_fetch_block_carries_no_identity(self) -> None:
        # The end-to-end leak check, over the FINISHED document rather than the block:
        # `last_fetch` skips `_redact` by design, exactly like `last_poll` beside it,
        # so what protects it has to be visible here.
        census = {
            "at": FROZEN,
            "status": 200,
            "code": "decode error for Kitchen Washer at AA:BB:CC:DD:EE:FF",
            "outcome": "ok",
            "stopped_at": "PLAINTEXT-SERIAL",
            "node_type": "user@example.com",
            "count": 2,
        }
        with _FrozenClock(FROZEN):
            result = self._dump(
                _FetchClient(
                    census,
                    setup_expanded=2,
                    appliance_count=0,
                    setup_drops={"AA:BB:CC:DD:EE:FF": 1, "mac_empty": 2},
                    degraded_census={"AA:BB:CC:DD:EE:FF#0": 1, "ADDHON-230": 2},
                ),
                _build_coordinator(),
            )
        blob = json.dumps(result)
        for leak in (
            "Kitchen Washer",
            "AA:BB:CC:DD:EE:FF",
            "PLAINTEXT-SERIAL",
            "user@example.com",
            "decode error for",
        ):
            self.assertNotIn(leak, blob, leak)
        # ...and the block still said everything it was asked to say.
        self.assertEqual(2, result["last_fetch"]["count"])
        self.assertEqual({"mac_empty": 2}, result["last_fetch"]["skipped"])
        self.assertEqual({"ADDHON-230": 2}, result["last_fetch"]["degraded"])

    def test_a_foreign_client_object_yields_never_ran(self) -> None:
        # Everything that is not a Mapping is the same state: nothing was recorded on
        # this client. The reader does not try to interpret it further.
        for census in ("x", [], None, 3):
            with self.subTest(census=census):
                self.assertEqual("never_ran", self._fetch(census)["state"])

    def test_a_raising_client_property_degrades_the_block_not_the_dump(self) -> None:
        # setup() runs on the dedicated hOn loop thread while a Download Diagnostics
        # runs on HA's event loop, so any accessor here can raise on a session being
        # torn down. A convenience field must never take the whole download with it --
        # the download is the artefact the whole exercise exists to make producible.
        result = self._dump(_RaisingFetchClient(), _build_coordinator())
        self.assertEqual("unreadable", result["last_fetch"]["state"])
        self.assertEqual(2, len(result["appliances"]))
        self.assertIn("generated_at", result)
        self.assertIn("platforms", result)


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
        # The four AP controls that are hardcoded entity classes with no description
        # table, so they contribute nothing to the walk above: a fifth one -- the
        # SHAPE these already use -- would otherwise land with no guard at all.
        declared |= {"machMode", "onOffStatus"}      # fan.HonAirPurifierFan
        declared |= {"aromaStatus", "lightStatus"}   # select.HonAirPurifier{Aroma,PanelLight}Select
        self.assertTrue(declared)
        # assertEqual, not a subset: coverage SUBTRACTS this set from both axes, so a
        # name left here after its entity is removed silently deletes that parameter
        # from the gold signal and the dump reports full coverage of a control the
        # integration does not have. Adding a bogus name left the whole suite green.
        self.assertEqual(declared, set(AP_ENTITY_PARAMS))


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

    def test_a_stopped_purifier_is_not_a_future_capability(self) -> None:
        """The ordinary case, which the exact-dict test above was positioned never to
        see: machMode=0 is the documented off-state sentinel (air_purifier.py's own
        comment, and fan.py: "never a preset"), so an idle purifier must not be
        reported as running a mode nobody handles.

        `AP_HANDLED_VALUES[machMode]` reused AP_WRITABLE_MODES {1,2,4}, and writable
        is not the same as handled -- so every dump from a switched-off purifier
        announced a phantom firmware capability. The real AP capture
        (tests/fixtures/ap/schema.json) declares only ["1","2","4"], which makes 0 the
        ONLY value that ever reaches this branch on the hardware this repo has seen.
        """
        future = _ap_block({"machMode": "0"})["future_capabilities"]
        self.assertEqual({}, future["state_values_unhandled"])

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
        """Identity must be masked BEFORE the value is cut to _FUTURE_MAX_VALUE_CHARS.

        The fixture's mac/serial/model live under keys this section never reads, so
        searching the default block for them asserts that a section containing no
        identity contains no identity. Put the identity where the section actually
        looks -- a live state value -- and put one ACROSS the cap, because a MAC
        sliced through the middle no longer matches `_MAC_RE` and a mask applied
        after the slice publishes the OUI verbatim.
        """
        import json

        mac = "AA:BB:CC:DD:EE:FF"
        straddling = "x" * (diagnostics._FUTURE_MAX_VALUE_CHARS - 10) + mac
        future = _ap_block({"machMode": straddling})["future_capabilities"]
        encoded = json.dumps(future)
        self.assertNotIn(mac, encoded, encoded)
        # a MAC cut by the cap is still a MAC: the OUI alone identifies the vendor
        self.assertNotIn("AA:BB:CC", encoded, encoded)
        for identity in ("AP-PLAINTEXT-SERIAL", "SYNTHETIC-AP", "ap-unique"):
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

    def test_future_capability_row_count_is_bounded(self) -> None:
        """The OTHER bound. `_enum_deltas` caps VALUES per parameter
        (_FUTURE_MAX_VALUES) and ROWS overall (_FUTURE_MAX_ENTRIES); the test above
        drives only the first. The row cap is the one that matters for a device with
        many PARAMETERS rather than many values on one -- the AC in
        diagnostics/live-2026-06-22/ carries 74 shadow parameters, so a type whose
        registry listed them all would cross 40 rows on real hardware while never
        approaching 20 values on any single parameter. Deleting the row cap left the
        entire suite green.
        """
        from custom_components.addhon import air_purifier

        coord = _build_ap_coordinator()
        params = coord.data[AP_ID]["appliance"].commands["settings"].parameters
        overflow = diagnostics._FUTURE_MAX_ENTRIES + 10
        for n in range(overflow):
            params[f"futureParam{n}"] = FakeParam(
                value="0", typology="enum", values=["0", "9"]
            )
        patched = dict(air_purifier.AP_HANDLED_VALUES)
        patched.update({f"futureParam{n}": frozenset({"0"}) for n in range(overflow)})
        original = air_purifier.AP_HANDLED_VALUES
        air_purifier.AP_HANDLED_VALUES = patched
        try:
            result = _run(
                diagnostics.async_get_config_entry_diagnostics(
                    FakeHass(coord), FakeEntry()
                )
            )
        finally:
            air_purifier.AP_HANDLED_VALUES = original
        future = result["appliances"][0]["future_capabilities"]
        self.assertLessEqual(
            len(future["enum_deltas"]), diagnostics._FUTURE_MAX_ENTRIES
        )
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
    def test_a_registry_flag_is_read_through_its_enum_value(self):
        """`_enum_text` unwraps `.value` before stringifying, and nothing pinned it.

        Redundant on current Home Assistant, whose `RegistryEntryDisabler` is a
        `StrEnum` -- `str()` already yields the token. It stops being redundant the
        moment the flag is a plain Enum or a mixin enum, which is what the helper
        was written for: `str()` on those yields `ClassName.MEMBER`, and the dump
        would report a disabled entity as disabled by `RegistryEntryDisabler.USER`.
        Every fixture in this file passes a bare string, so the unwrap could be
        deleted with the suite green. This is the test that stops that.
        """
        import enum

        class _PlainDisabler(enum.Enum):
            USER = "user"

        self.assertEqual("user", diagnostics._enum_text(_PlainDisabler.USER))
        self.assertEqual("user", diagnostics._enum_text("user"))
        self.assertIsNone(diagnostics._enum_text(None))

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
        # A disabler that is NOT "user": the distinction this section exists to make
        # is a deliberate user choice (harmless) versus the integration disabling its
        # own entity (a bug). Echoing the fixture's "user" back cannot see the value
        # path die -- hardcoding the literal survives the whole suite -- and "user" is
        # the only disabler any fixture in the repo has ever fed.
        rows = self._rows(AP_ROWS[:1], disabled_by="integration") + self._rows(AP_ROWS[1:])
        live = FakeStates(live=[eid for _uid, eid in AP_ROWS[1:]])
        _result, entities = self._dump(rows, states=live)
        self.assertEqual({"switch.child_lock": "integration"}, entities["disabled"])
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
        # Positively pin the totals the contrast depends on. The three assertions
        # above are all absence checks over a fixture built by DELETING exactly what
        # they assert absent, so a build where appliance_totals is permanently empty
        # -- or double-counts -- satisfies them unchanged.
        self.assertEqual(
            {"fan": 1, "select": 1}, result["platforms"]["appliance_totals"]
        )

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

    def test_a_shorter_appliance_id_does_not_swallow_a_longer_one(self) -> None:
        """The `_` separator is MANDATORY in prefix attribution, and longest-first
        ordering does not stand in for it when the longer id is absent from the poll.

        Realistic, not theoretical: when the cloud reports the placeholder MAC, the
        engine builds the id as f"{type}_{applianceModelId}"
        (client/engine/appliance.py:100-104), so two appliances of one type with
        model ids 123 and 1234 get `wm_123` and `wm_1234` -- a bare prefix with no
        separator between them. Without the mandatory `_`, the absent appliance's
        surviving registry rows are attributed to its shorter-id sibling and emitted
        under the mangled key `switch._child_lock`.
        """
        inventory, platforms = diagnostics._entity_inventory(
            [FakeRegistryEntry("wm_1234_child_lock", "switch.b_child_lock")],
            ["wm_123"],
            "e1",
            None,
        )
        self.assertEqual({}, inventory["wm_123"]["by_domain"])
        self.assertEqual(1, platforms["unattributed"])

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


# --- step 1: the shared clock, the shared text bound, the coverage mirror ----
#
# `_FrozenClock`, `FROZEN` and `FROZEN_TEXT` below are MODULE-LEVEL and SHARED:
# the freshness section further down reuses all three. A second `class
# _FrozenClock` or a second `FROZEN =` later in this file would silently shadow
# these with no import error and quietly stop patching what the tests above it
# think it patches, which is the same failure Part 2 describes for `REF_ID`.


class _FrozenClock:
    """Freeze `diagnostics._utcnow` for the duration of a `with` block.

    `_utcnow` exists to be exactly this seam, so an age assertion never has to be
    written against the wall clock. It also counts its calls, which is how the
    "one instant for the whole document" claim below is checked rather than
    assumed. Later sections that assert an age patch the same seam.
    """

    def __init__(self, moment):
        self.moment = moment
        self.calls = 0
        self._saved = None

    def __enter__(self):
        self._saved = diagnostics._utcnow

        def _fake():
            self.calls += 1
            return self.moment

        diagnostics._utcnow = _fake
        return self

    def __exit__(self, *exc_info):
        diagnostics._utcnow = self._saved
        return False


FROZEN = datetime(2026, 8, 13, 7, 9, 40, tzinfo=timezone.utc)
FROZEN_TEXT = "2026-08-13T07:09:40+00:00"


class BoundedTextTest(unittest.TestCase):
    """`_bounded_text` masks BEFORE it cuts, which is the whole point of it."""

    def test_a_mac_straddling_the_cap_is_still_masked(self):
        # THE regression this helper exists for, and the one shape every previous
        # "the MAC is masked" test structurally could not catch: the address does
        # not start at offset 0, it starts at 70 with the cap at 80. A helper that
        # cut first would hand `_MAC_RE` ten characters of a seventeen-character
        # address -- three and a half octets, no longer matching the six-group
        # pattern -- and ship them to a public issue in cleartext.
        value = "x" * 70 + "3c:71:bf:bd:32:2c"
        self.assertEqual("x" * 70 + "***", diagnostics._bounded_text(value, 80))
        # Not vacuous: cutting first really does leak, on this exact input.
        self.assertIn("3c:71:bf:b", value[:80])

    def test_no_cut_position_leaks_a_fragment(self):
        # The one offset above could be a lucky alignment; sweep the whole window
        # in which the address straddles the boundary.
        for offset in range(60, 85):
            value = "x" * offset + "3c:71:bf:bd:32:2c"
            bounded = diagnostics._bounded_text(value, 80)
            self.assertNotIn(":", bounded, f"leaked at offset {offset}")

    def test_the_bound_is_still_a_bound(self):
        self.assertEqual("abcde", diagnostics._bounded_text("abcdefghij", 5))
        self.assertEqual("abc", diagnostics._bounded_text("abc", 40))

    def test_a_container_is_refused_rather_than_flattened(self):
        # Not redundant with `_scalar_text`'s own container check: that one runs
        # AFTER `_jsonable`, which turns a dict into its repr, so it never fires
        # for a real Mapping. Without the guard in `_bounded_text` the whole
        # envelope would be flattened into one string under the caller's key,
        # permanently out of reach of key-based redaction.
        conn_event = {"macAddress": "AA:BB:CC:DD:EE:FF", "category": "CONNECTED"}
        self.assertEqual(
            "{'macAddress': '***', 'category': 'CONNECTED'}",
            diagnostics._scalar_text(conn_event),
        )
        self.assertIsNone(diagnostics._bounded_text(conn_event, 40))
        self.assertIsNone(diagnostics._bounded_text(["a"], 40))

        # A bare Mapping is caught by EITHER of the two container guards, so the
        # assertions above cannot tell them apart and the outer one can be
        # deleted with the file still green. This envelope also exposes `.value`,
        # so the inner guard reads a scalar and only the outer guard can refuse it.
        class _Envelope(dict):
            value = "CONNECTED"

        self.assertIsNone(diagnostics._bounded_text(_Envelope(conn_event), 40))
        self.assertIsNone(diagnostics._bounded_text(None, 40))

    def test_a_container_inside_a_wrapper_is_refused_too(self):
        # The shape an outer-only isinstance check cannot see: `_jsonable`
        # unwraps `.value` before stringifying, and the merged attributes
        # mapping is made almost entirely of such wrappers, so the guard has to
        # look through one level as well as at the object itself. Measured with
        # the outer guard alone, this returned the whole envelope as a string
        # with `mobileId` in cleartext beside a masked MAC.
        conn_event = {
            "macAddress": "AA:BB:CC:DD:EE:FF",
            "category": "CONNECTED",
            "mobileId": "phone-install-xyz",
        }
        self.assertIsNone(diagnostics._bounded_text(FakeWrapper(conn_event), 200))
        self.assertIsNone(diagnostics._bounded_text(FakeWrapper(["a"]), 200))
        self.assertIsNone(diagnostics._bounded_text({"a", "b"}, 200))

    def test_a_wrapper_is_unwrapped_like_any_other_scalar(self):
        # It routes through _scalar_text, so a HonAttribute-shaped object is
        # unwrapped rather than stringified as "<object at 0x...>".
        self.assertEqual("7", diagnostics._bounded_text(FakeWrapper(7), 40))

    def test_a_raising_wrapper_is_refused_rather_than_propagated(self):
        class Exploding:
            @property
            def value(self):
                raise RuntimeError("boom")

        self.assertIsNone(diagnostics._bounded_text(Exploding(), 40))


class StampTextTest(unittest.TestCase):
    """The single datetime -> ISO text conversion, and what it refuses."""

    def test_an_aware_instant_is_normalised_to_utc(self):
        moment = datetime(2026, 4, 9, 12, 34, 56, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual("2026-04-09T10:34:56+00:00", diagnostics._stamp_text(moment))

    def test_utc_normalisation_is_what_saves_the_field_from_the_mac_mask(self):
        # A sub-minute offset makes an ISO instant look exactly like a MAC address
        # to `_MAC_RE`, which runs over every string in the block. This is the
        # measured reason `astimezone` is correctness and not tidiness.
        raw = "2026-04-09T12:34:56-05:30:15"
        self.assertEqual("2026-04-09T***", debug_utils._MAC_RE.sub("***", raw))
        odd = timezone(-timedelta(hours=5, minutes=30, seconds=15))
        moment = datetime(2026, 4, 9, 12, 34, 56, tzinfo=odd)
        text = diagnostics._stamp_text(moment)
        self.assertEqual("2026-04-09T18:05:11+00:00", text)
        self.assertEqual(text, debug_utils._MAC_RE.sub("***", text))

    def test_a_naive_instant_is_emitted_as_it_stands(self):
        # Never shifted onto the host's zone: a reader can reason about an instant
        # with no offset, but not about one that was quietly moved by however far
        # the reporter's machine happens to be from UTC.
        moment = datetime(2020, 9, 29, 10, 1, 26)
        self.assertEqual("2020-09-29T10:01:26", diagnostics._stamp_text(moment))

    def test_a_tzinfo_with_no_offset_is_treated_as_naive(self):
        # CPython does NOT raise here: it falls back to the host's local zone and
        # then labels the result "+00:00". A wrong time wearing an authoritative
        # offset is worse than a time with no offset at all.
        class NoOffset(tzinfo):
            def utcoffset(self, dt):
                return None

            def tzname(self, dt):
                return None

            def dst(self, dt):
                return None

        moment = datetime(2020, 9, 29, 10, 1, 26, tzinfo=NoOffset())
        self.assertEqual("2020-09-29T10:01:26", diagnostics._stamp_text(moment))

    def test_a_tzinfo_that_raises_yields_nothing(self):
        class Exploding(tzinfo):
            def utcoffset(self, dt):
                raise RuntimeError("boom")

        moment = datetime(2020, 9, 29, 10, 1, 26, tzinfo=Exploding())
        self.assertIsNone(diagnostics._stamp_text(moment))

    def test_a_datetime_subclass_cannot_smuggle_a_string(self):
        # `isinstance` admits subclasses and a subclass may override isoformat, so
        # the OUTPUT is validated too. Without that, this string would be copied
        # verbatim into a document about to be attached to a public issue.
        class Smuggler(datetime):
            def isoformat(self, *args, **kwargs):
                return "user@example.com"

        self.assertIsNone(diagnostics._stamp_text(Smuggler(2026, 4, 9)))

    def test_an_over_long_output_is_refused(self):
        class Verbose(datetime):
            def isoformat(self, *args, **kwargs):
                return "2026-04-09T05:34:16+00:00" + "!" * 40

        self.assertIsNone(diagnostics._stamp_text(Verbose(2026, 4, 9)))

    def test_a_digits_only_shape_is_refused_too(self):
        # `_ISO_RE` is narrow on purpose: it requires the 'T' separator, which a
        # real `isoformat()` always produces, so the only shapes it admits are
        # the ones a datetime can actually make. A subclass returning something
        # that merely looks date-ish does not get through.
        class Loose(datetime):
            def isoformat(self, *args, **kwargs):
                return "1234-56-78 90:12:34"

        self.assertIsNone(diagnostics._stamp_text(Loose(2026, 4, 9)))

    def test_a_cloud_string_instant_is_refused_by_design(self):
        # `lastConnEvent.instantTime` is a cloud-chosen STRING. This helper's whole
        # promise is that its output came out of datetime.isoformat; echoing a
        # cloud string here would break that promise and look identical in the dump.
        self.assertIsNone(diagnostics._stamp_text("2026-04-09T05:34:16Z"))
        self.assertIsNone(diagnostics._stamp_text(1775712856741))
        self.assertIsNone(diagnostics._stamp_text(None))


class AsUtcTest(unittest.TestCase):
    """`_as_utc` is the one place awareness is decided, so ages cannot raise."""

    def test_an_aware_instant_is_converted(self):
        # Aware datetimes compare by ABSOLUTE INSTANT, so an assertEqual against
        # the converted value is satisfied whether or not the conversion happened.
        # The representation is what has to be asserted: tzinfo and isoformat are
        # the only observations that can tell `astimezone(utc)` from `return value`.
        moment = datetime(2026, 4, 9, 12, 0, tzinfo=timezone(timedelta(hours=2)))
        converted = diagnostics._as_utc(moment, False)
        self.assertEqual(datetime(2026, 4, 9, 10, 0, tzinfo=timezone.utc), converted)
        self.assertEqual(timezone.utc, converted.tzinfo)
        self.assertEqual("2026-04-09T10:00:00+00:00", converted.isoformat())

    def test_a_tzinfo_with_no_offset_is_treated_as_naive(self):
        # The half of the documented policy nothing exercised. A tzinfo whose
        # `utcoffset()` returns None does NOT raise: CPython falls back to the
        # HOST zone, so guarding on `tzinfo is not None` would move the instant
        # by however far the reporter is from UTC and then label it '+00:00'.
        class NoOffset(tzinfo):
            def utcoffset(self, dt):
                return None

            def tzname(self, dt):
                return None

            def dst(self, dt):
                return None

        moment = datetime(2020, 9, 29, 10, 1, 26, tzinfo=NoOffset())
        self.assertIsNone(diagnostics._as_utc(moment, False))
        self.assertEqual(
            datetime(2020, 9, 29, 10, 1, 26, tzinfo=timezone.utc),
            diagnostics._as_utc(moment, True),
        )

    def test_a_naive_instant_follows_the_stated_policy(self):
        naive = datetime(2026, 4, 9, 12, 0)
        self.assertIsNone(diagnostics._as_utc(naive, False))
        self.assertEqual(
            datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
            diagnostics._as_utc(naive, True),
        )

    def test_a_non_datetime_is_refused(self):
        self.assertIsNone(diagnostics._as_utc("2026-04-09T12:00:00Z", True))
        self.assertIsNone(diagnostics._as_utc(None, True))
        self.assertIsNone(diagnostics._as_utc(1775712856741, True))

    def test_a_subclass_that_refuses_to_be_normalised_is_refused(self):
        # Everything that touches the value is inside the one guard, including
        # the `replace` on the naive branch: the callers downstream feed this
        # helper cloud-supplied objects, and an unguarded call there does not
        # produce a wrong age, it produces no dump at all.
        class Hostile(datetime):
            def replace(self, *args, **kwargs):
                raise RuntimeError("boom")

        self.assertIsNone(diagnostics._as_utc(Hostile(2026, 4, 9), True))

    def test_the_result_can_always_be_subtracted_from_now(self):
        # The property the helper exists for: whatever it returns is aware, so the
        # subtraction every age is built on cannot raise TypeError.
        for value in (
            datetime(2026, 4, 9, 12, 0),
            datetime(2026, 4, 9, 12, 0, tzinfo=timezone(timedelta(hours=-3))),
        ):
            converted = diagnostics._as_utc(value, True)
            self.assertIsNotNone(converted)
            (FROZEN - converted).total_seconds()  # must not raise


class DumpTimestampTest(unittest.TestCase):
    """Every document this module produces says when it was taken."""

    def test_the_entry_dump_is_dated_and_dated_first(self):
        with _FrozenClock(FROZEN):
            result, _ = _entry_diag()
        self.assertEqual("generated_at", next(iter(result)))
        self.assertEqual(FROZEN_TEXT, result["generated_at"])

    def test_the_stamp_is_aware_utc(self):
        result, _ = _entry_diag()
        self.assertTrue(result["generated_at"].endswith("+00:00"))

    def test_the_whole_document_shares_one_instant(self):
        # Two appliances, one clock read. A per-block read would let two ages in
        # one document disagree by however long the dump took to build.
        with _FrozenClock(FROZEN) as clock:
            result, blocks = _entry_diag()
        self.assertEqual(2, len(blocks))
        self.assertEqual(1, clock.calls)

    def test_the_device_dump_is_dated_and_dated_first(self):
        coord = _build_coordinator()
        hass = FakeHass(coord)
        device = FakeDevice(identifiers={(DOMAIN, WD_ID)})
        with _FrozenClock(FROZEN):
            result = _run(
                diagnostics.async_get_device_diagnostics(hass, FakeEntry(), device)
            )
        self.assertEqual(["generated_at", "appliance"], list(result))
        self.assertEqual(FROZEN_TEXT, result["generated_at"])

    def test_a_degraded_device_dump_is_still_dated(self):
        # The dump most likely to be pasted into an issue as "the download gave me
        # nothing" used to be the only undated one in the integration.
        coord = _build_coordinator()
        hass = FakeHass(coord)
        device = FakeDevice(identifiers={(DOMAIN, "does-not-exist")})
        with _FrozenClock(FROZEN):
            result = _run(
                diagnostics.async_get_device_diagnostics(hass, FakeEntry(), device)
            )
        self.assertEqual({"generated_at": FROZEN_TEXT}, result)

    def test_a_device_dump_with_no_coordinator_data_is_still_dated(self):
        # The second early return: the appliance id resolves but the coordinator
        # holds nothing for it.
        hass = FakeHass(FakeCoordinator({WD_ID: "not-a-mapping"}))
        device = FakeDevice(identifiers={(DOMAIN, WD_ID)})
        with _FrozenClock(FROZEN):
            result = _run(
                diagnostics.async_get_device_diagnostics(hass, FakeEntry(), device)
            )
        self.assertEqual({"generated_at": FROZEN_TEXT}, result)

    def test_no_identity_reaches_a_degraded_dump(self):
        # A dated degraded dump must still be a degraded dump: the identifier that
        # failed to resolve is not echoed back as evidence of what was looked for.
        coord = _build_coordinator()
        hass = FakeHass(coord)
        device = FakeDevice(identifiers={(DOMAIN, "PLAINTEXT-SERIAL")})
        result = _run(
            diagnostics.async_get_device_diagnostics(hass, FakeEntry(), device)
        )
        self.assertNotIn("PLAINTEXT-SERIAL", json.dumps(result))

    def test_a_block_built_with_no_clock_is_still_dated_downstream(self):
        # The two-positional-argument call site still works, and falls back to the
        # seam rather than to a naive value.
        with _FrozenClock(FROZEN) as clock:
            diagnostics._appliance_block(
                "id1",
                {"appliance": None, "type": "AC", "attributes": {}, "statistics": {}},
            )
        self.assertEqual(1, clock.calls)

    def test_a_naive_clock_is_promoted_not_refused(self):
        # A caller handing in a naive datetime gets a block, not a TypeError:
        # `_appliance_block` normalises at its boundary. Regression against the
        # aware/naive subtraction that would take the whole dump down.
        block = diagnostics._appliance_block(
            "id1",
            {"appliance": None, "type": "AC", "attributes": {}, "statistics": {}},
            None,
            datetime(2026, 8, 13, 7, 9, 40),
        )
        self.assertEqual("AC", block["type"])

    def test_a_junk_clock_does_not_break_the_block(self):
        for junk in ("2026-08-13", 0, object()):
            block = diagnostics._appliance_block(
                "id1",
                {"appliance": None, "type": "AC", "attributes": {}, "statistics": {}},
                None,
                junk,
            )
            self.assertEqual("AC", block["type"])


class HobDerivedCoverageTest(unittest.TestCase):
    """The hob's derived per-zone timers, which no description table can see.

    `_mapped_sets` walks the platform tables, and a sensor built from TWO
    attributes has no row in them. Without an explicit entry the coverage block
    calls twelve live readings unmapped while `entities.sources` in the same
    document names the sensors that read them -- the self-contradiction the
    custom-entity section exists to remove.
    """

    def test_both_halves_of_every_zone_timer_are_mapped(self) -> None:
        mapped_attrs, _params, sources, _ = diagnostics._mapped_sets("IH")
        for zone in range(1, 7):
            for unit in ("HH", "MM"):
                self.assertIn(f"remainingTime{unit}Z{zone}", mapped_attrs)
            self.assertIn(f"sensor.remaining_time_zone{zone}", sources)

    def test_the_source_row_names_both_halves(self) -> None:
        _attrs, _params, sources, _ = diagnostics._mapped_sets("IH")
        row = sources["sensor.remaining_time_zone1"]
        self.assertIn("remainingTimeHHZ1", row["read"])
        self.assertIn("remainingTimeMMZ1", row["read"])
        # A read-only entity omits the write half rather than emitting an empty
        # one, so a reader can see at a glance that it writes nothing.
        self.assertNotIn("write", row)

    def test_the_hob_alias_is_covered_identically(self) -> None:
        self.assertEqual(diagnostics._mapped_sets("IH"), diagnostics._mapped_sets("HOB"))

    def test_another_type_did_not_inherit_the_zone_timers(self) -> None:
        mapped_attrs, _params, sources, _ = diagnostics._mapped_sets("OV")
        self.assertNotIn("remainingTimeHHZ1", mapped_attrs)
        self.assertNotIn("sensor.remaining_time_zone1", sources)


class CoverageExpectedAbsentTest(unittest.TestCase):
    """The mirror axis: what this code maps and the device does not have."""

    def test_a_mapped_attribute_the_device_omits_is_named(self):
        # The WD fixture publishes machMode but neither bare key the mean-water
        # consumption sensor reads, so the sensor exists and can never have a
        # value. That is the "why is the control I expected not there" question.
        _, blocks = _entry_diag()
        absent = blocks["WD"]["coverage"]["attributes_expected_absent"]
        self.assertIn("totalWashCycle", absent)
        self.assertIn("totalWaterUsed", absent)
        self.assertNotIn("machMode", absent)

    def test_a_mapped_command_param_the_device_omits_is_named(self):
        _, blocks = _entry_diag()
        absent = blocks["WD"]["coverage"]["command_params_expected_absent"]
        self.assertIn("prCode", absent)
        self.assertNotIn("program", absent)

    def test_the_lists_are_sorted_and_are_plain_strings(self):
        _, blocks = _entry_diag()
        for block in blocks.values():
            for key in ("attributes_expected_absent", "command_params_expected_absent"):
                names = block["coverage"][key]
                self.assertIsInstance(names, list)
                self.assertEqual(sorted(names), names)
                for name in names:
                    self.assertIsInstance(name, str)

    def test_the_mirror_never_names_something_the_device_publishes(self):
        # The invariant in one assertion, over every appliance in the fixture: a
        # name in either list must not be findable anywhere else in the same
        # block. A reader who can disprove a finding by scrolling down stops
        # trusting the section. Note this one is a DRIFT GUARD rather than a
        # behaviour test -- it restates the set difference the code performs, so
        # it cannot fail while the implementation is that difference. The test
        # below it is the one that discriminates.
        _, blocks = _entry_diag()
        for app_type, block in blocks.items():
            published = set(block["attributes"])
            bare = {k for k in published if "." not in k}
            # The attribute axis does NOT subtract bare keys, it subtracts
            # `reachable`: bare keys PLUS every mapped name whose multi-link read
            # chain reaches a DOTTED spelling the device publishes. Filtering
            # every dotted key out of the comparison is exactly where the false
            # positives live -- the live TD publishes `tumblingStatus` only as
            # `settings.tumblingStatus`/`startProgram.tumblingStatus`, the live
            # WM `delayTime` only as `startProgram.delayTime`.
            #
            # `settings.<name>` is excluded: it is the COMMAND-parameter mirror,
            # which no entity reads and which the attribute axis subtracts on
            # purpose (a writable param is absent from the READ axis even when
            # its settings twin is printed -- `spinSpeed` on the WD fixture).
            dotted_tails = {
                k.split(".", 1)[1]
                for k in published
                if "." in k and not k.startswith("settings.")
            }
            declared = set()
            for params in block["commands"].values():
                declared.update(params)
            for name in block["coverage"]["attributes_expected_absent"]:
                self.assertNotIn(name, bare, f"{app_type}: {name} is published")
                self.assertNotIn(
                    name,
                    dotted_tails,
                    f"{app_type}: {name} is published as a dotted spelling",
                )
            for name in block["coverage"]["command_params_expected_absent"]:
                self.assertNotIn(name, declared, f"{app_type}: {name} is declared")

    def test_a_mapped_name_published_only_under_a_dotted_spelling_is_not_absent(self):
        # The behaviour test behind the `reachable` rule, which nothing pinned.
        # On the live 2026-06-22 TD `tumblingStatus` exists ONLY as
        # `settings.tumblingStatus` / `startProgram.tumblingStatus`, and the
        # option switch reads it through that chain, so it is present, not
        # absent. A single-link chain keeps the strict bare test.
        block = diagnostics._appliance_block(
            "id1",
            {
                "appliance": FakeApplianceNoModel(commands={}),
                "type": "TD",
                "attributes": {"startProgram.tumblingStatus": "1"},
                "statistics": {},
            },
        )
        absent = block["coverage"]["attributes_expected_absent"]
        self.assertNotIn("tumblingStatus", absent)
        self.assertTrue(absent, "the empty case would be vacuous")

    def test_a_param_under_a_non_settings_command_is_not_reported_absent(self):
        # The specific shape the invariant above exists to catch, and the ONLY
        # test in this class that discriminates: measured against the settings
        # params alone, the mirror named `program` and `prCode` on every wash
        # appliance and `onOffStatus` on the air purifier, while the same dump
        # printed them under `commands` a few lines below. Do not delete this as
        # redundant with the invariant above -- the shared fixture has no
        # `startProgram` command, so the invariant passes even when the mirror
        # is measured against the wrong universe.
        appliance = FakeAppliance(
            commands={
                "settings": FakeCommand({"spinSpeed": FakeParam(value="1000")}),
                "startProgram": FakeCommand({
                    "program": FakeParam(value="9"),
                    "prCode": FakeParam(value="14"),
                }),
            }
        )
        block = diagnostics._appliance_block(
            "id1",
            {
                "appliance": appliance,
                "type": "WD",
                "attributes": {},
                "statistics": {},
            },
        )
        absent = block["coverage"]["command_params_expected_absent"]
        # The three names this test exists for. The list is no longer empty:
        # the program options this WD maps and this fake appliance does not
        # declare are genuinely absent, which is what the axis is for.
        self.assertNotIn("program", absent)
        self.assertNotIn("prCode", absent)
        self.assertNotIn("spinSpeed", absent)

    def test_a_device_that_publishes_everything_it_maps_reports_nothing(self):
        # The empty case, derived rather than hand-listed so it cannot rot when a
        # table gains a row: ask the block what it expected, hand exactly that
        # back as the device's shadow AND as its command schema, and the mirror
        # must fall silent on both axes.
        #
        # Both halves are fed now. Until the hood gained writable controls it
        # mapped no command parameter at all, so the parameter half of this test
        # was passing on an empty universe -- it asserted [] == [] and would have
        # gone on doing so however wrong the mirror got.
        def _block(attributes, params=()):
            commands = (
                {"settings": FakeCommand({n: FakeParam(value="0") for n in params})}
                if params
                else {}
            )
            return diagnostics._appliance_block(
                "id1",
                {
                    "appliance": FakeApplianceNoModel(commands=commands),
                    "type": "HO",
                    "attributes": attributes,
                    "statistics": {},
                },
            )

        empty = _block({})["coverage"]
        expected_attrs = empty["attributes_expected_absent"]
        expected_params = empty["command_params_expected_absent"]
        self.assertTrue(expected_attrs, "the attribute case would be vacuous")
        self.assertTrue(expected_params, "the parameter case would be vacuous")
        complete = _block(
            {name: "0" for name in expected_attrs}, params=expected_params
        )
        self.assertEqual([], complete["coverage"]["attributes_expected_absent"])
        self.assertEqual([], complete["coverage"]["command_params_expected_absent"])

    def test_the_mirror_survives_an_appliance_with_no_commands_at_all(self):
        # A dump must degrade, never raise: a foreign appliance object with no
        # `commands` surface still produces both lists.
        block = diagnostics._appliance_block(
            "id1",
            {"appliance": object(), "type": "WD", "attributes": {}, "statistics": {}},
        )
        cov = block["coverage"]
        self.assertIn("prCode", cov["command_params_expected_absent"])
        self.assertIn("program", cov["command_params_expected_absent"])

    def test_a_raising_parameters_property_is_skipped_not_propagated(self):
        # `_command_param_names` walks commands that nothing else in the dump
        # reads first, so it is asserted here directly rather than through a
        # block.
        #
        # Scope note, verified on HEAD: this guard is a BACKSTOP, not the dump's
        # protection. `_command_schema` reads `.parameters` on EVERY command --
        # settings and startProgram alike -- above `_coverage` in
        # `_appliance_block`, so through the real call path a raising property
        # kills the dump before this helper runs. Confirmed on this same
        # appliance: `_appliance_block` propagates RuntimeError. That exposure
        # predates this change and is not fixed here; what the guard buys is
        # that the helper cannot ADD a way to fail.
        class Exploding:
            @property
            def parameters(self):
                raise RuntimeError("boom")

        appliance = FakeAppliance(
            commands={
                "startProgram": Exploding(),
                "settings": FakeCommand({"spinSpeed": FakeParam(value="1")}),
            }
        )
        self.assertEqual({"spinSpeed"}, diagnostics._command_param_names(appliance))

    def test_a_commands_mapping_whose_values_view_raises_is_survived(self):
        # The one way this helper could have ADDED a crash: `_command_schema`
        # walks the same mapping with `.items()` and `_settings_param_names`
        # with `.get(name)`, so an object that only breaks on `.values()` gets
        # past both of them and would have died here.
        class RaisingValues(dict):
            def values(self):
                raise RuntimeError("boom")

        appliance = FakeAppliance(
            commands=RaisingValues({"settings": FakeCommand({"spinSpeed": FakeParam(value="1")})})
        )
        self.assertEqual(set(), diagnostics._command_param_names(appliance))
        block = diagnostics._appliance_block(
            "id1",
            {"appliance": appliance, "type": "WD", "attributes": {}, "statistics": {}},
        )
        self.assertEqual("WD", block["type"])

    def test_the_mirror_carries_no_identity(self):
        # Both lists are differences taken FROM the static tables, so nothing the
        # cloud chose can reach them. Pinned, because a future edit that started
        # listing device-side names would be a silent privacy change.
        _, blocks = _entry_diag()
        for block in blocks.values():
            encoded = json.dumps(
                {
                    k: block["coverage"][k]
                    for k in (
                        "attributes_expected_absent",
                        "command_params_expected_absent",
                    )
                }
            )
            self.assertNotIn("PLAINTEXT", encoded)
            self.assertNotIn("AA:BB:CC:DD:EE:FF", encoded)
            for name in json.loads(encoded)["attributes_expected_absent"]:
                self.assertRegex(name, r"^[A-Za-z][A-Za-z0-9_]*$")


# --- step 2: the entity source map (issue #75) ------------------------------


REF_ID = "ref-unique"

# One registry row per entity the fridge fixture below produces, in the same
# (unique_id, entity_id) shape as AP_ROWS so the existing `_rows()` idiom works
# unchanged. The entity_ids carry a NICKNAME-derived object_id on purpose: the
# identity test at the end of this section proves it never reaches the dump.
REF_ROWS = (
    (f"{REF_ID}_temp_zone3", "sensor.mario_fridge_temp_zone3"),
    (f"{REF_ID}_target_temp_zone3", "number.mario_fridge_target_temp_zone3"),
    (f"{REF_ID}_target_temp_zone4", "number.mario_fridge_target_temp_zone4"),
    (f"{REF_ID}_ref_program", "select.mario_fridge_ref_program"),
    (f"{REF_ID}_door_zone1", "binary_sensor.mario_fridge_door_zone1"),
    (f"{REF_ID}_connectivity", "binary_sensor.mario_fridge_connectivity"),
)


def _build_ref_coordinator() -> FakeCoordinator:
    """The issue-75 fridge: four zone setpoints declared, three zones reported.

    Its `settings` command carries tempSelZ1/Z2/Z3/Z4 while the shadow publishes
    tempZ1/Z2/Z3 only, which is the exact asymmetry the reporter spent two dumps
    and a decompiler pass establishing by hand.

    DO NOT REDEFINE THIS FUNCTION LOWER IN THE FILE. A second `def` of the same
    name silently shadows this one at import time -- no error, no warning -- and
    every assertion above it starts testing the other fixture. The deferred
    zone-map work needs a variant whose tempSelZ3 is not writable; it must add a
    KEYWORD-ONLY parameter here (`def _build_ref_coordinator(*, writable_z3: bool
    = True)`) and branch inside, so there stays exactly one definition.
    """
    ref = FakeAppliance(
        commands={
            "settings": FakeCommand({
                "tempSelZ1": FakeParam(value="4", typology="range", rng=(2, 8, 1)),
                "tempSelZ2": FakeParam(
                    value="-18", typology="enum", values=["-24", "-18"]
                ),
                "tempSelZ3": FakeParam(
                    value="5", typology="enum", values=["0", "2", "5"]
                ),
                "tempSelZ4": FakeParam(
                    value="5", typology="enum", values=["0", "2", "5"]
                ),
            }),
            # A fridge program select resolves its parameter off THIS command at
            # runtime, which is why `select.ref_program` has to read as null.
            "startProgram": FakeCommand({
                "program": FakeParam(value="1", typology="enum", values=["1", "2"]),
            }),
            "stopProgram": FakeCommand({
                "onOffStatus": FakeParam(value="0", typology="fixed"),
            }),
        },
        model_attributes={"zones": "fridge|freezer|vtRoom2", "doorNumber": 3},
    )
    return FakeCoordinator({
        REF_ID: {
            "appliance": ref,
            "type": "REF",
            "name": "Mario Fridge",
            "model": "HTF-540",
            "serial": "REF-PLAINTEXT-SERIAL",
            "mac": "AA:BB:CC:DD:EE:FF",
            "attributes": {
                "tempZ1": 4,
                "tempZ2": -18,
                "tempZ3": 5,
                "doorStatusZ1": "0",
                "available": True,
                "tempSelZ1": "4",
                "tempSelZ2": "-18",
                "tempSelZ3": "5",
            },
            "statistics": {},
        },
    })


_ABSENT = object()


class _BrokenModules:
    """Make `from .<name> import ...` fail the way a broken platform module does.

    `sys.modules[name] = None` is what CPython itself leaves behind for a module
    whose import failed, and it makes the next import raise rather than silently
    re-running the module body. Restoring is done against a sentinel, not against
    `sys.modules[name]`: a module the suite has never imported is ABSENT rather
    than None, and putting a None back for it would break every later test.
    """

    def __init__(self, *names):
        self._names = [f"custom_components.addhon.{name}" for name in names]
        self._saved: dict = {}

    def __enter__(self):
        for name in self._names:
            self._saved[name] = sys.modules.get(name, _ABSENT)
            sys.modules[name] = None
        return self

    def __exit__(self, *exc_info):
        for name, saved in self._saved.items():
            if saved is _ABSENT:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved
        return False


# Every platform module `_mapped_sets` imports, in the order it imports them.
# Written out here rather than derived, so that a module dropped from the walk
# leaves a test breaking on a name nothing reads instead of a sweep that quietly
# checks six things.
_PLATFORM_MODULES = (
    "air_purifier",
    "binary_sensor",
    "number",
    "program_options",
    "select",
    "sensor",
    "switch",
)


def _ref_dump(rows=REF_ROWS, states=None, raises=False, entries=None):
    """The fridge's `entities` section, built through the real registry path."""
    if entries is None:
        entries = [FakeRegistryEntry(uid, eid) for uid, eid in rows]
    if states is None:
        states = FakeStates(live=[row.entity_id for row in entries])
    hass = RegistryHass(_build_ref_coordinator(), rows=entries, states=states)
    _install_registry(hass, raises=raises)
    result = _run(diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry()))
    return result, result["appliances"][0]["entities"]


def _static_parameter_names() -> set[str]:
    """Every parameter name the platform tables declare, harvested independently.

    Built here by walking the tables directly instead of asking `_mapped_sets`,
    so the drift assertions compare the emitted section against the tables and
    not against the very walk that produced it.
    """
    from custom_components.addhon.air_purifier import AP_ENTITY_PARAMS
    from custom_components.addhon.binary_sensor import (
        BINARY_SENSORS,
        _CONNECTIVITY,
        _UNIVERSAL_GATED,
    )
    from custom_components.addhon.const import PROGRAM_PARAM_NAMES
    from custom_components.addhon.hob import HOB_ENTITY_PARAMS
    from custom_components.addhon.number import (
        NUMBERS,
        _AP_TIMING_NUMBERS,
        _PROGRAM_OPTION_NUMBERS,
    )
    from custom_components.addhon.select import (
        _AC_DIRECTION_SELECTS,
        _PROGRAM_OPTION_SELECTS,
        HonRefProgramSelect,
    )
    from custom_components.addhon.sensor import HOB_ZONE_TIME_ATTRS, SENSORS
    from custom_components.addhon.switch import (
        _AIR_PURIFIER_SWITCHES,
        _PROGRAM_OPTION_SWITCHES,
        _SETTINGS_SWITCHES,
    )

    # `AP_ENTITY_PARAMS`, `HOB_ENTITY_PARAMS` and `HOB_ZONE_TIME_ATTRS` are the
    # three frozensets a fixed-key entity declares in place of the description row
    # a table walk could find. Every member is a literal of this repository -- the
    # hob's `powerManagement`, and the twelve `remainingTime{HH,MM}Z{N}` the
    # derived per-zone timers read -- so they belong in the allowed set for exactly
    # the reason the table fields do, and NOT because a custom row in
    # `diagnostics.py` names them. `_CUSTOM_ENTITY_SOURCES` stays unharvested.
    # `_REF_ACTIVE_PROGRAM_ATTRS` joins them for the same reason and on the same terms:
    # it is the fridge select's own literal tuple of the shadow fields it reads to
    # recover a running program, and that select is a fixed-key entity with no
    # description row for the walk to find (#93).
    names: set[str] = (
        set(AP_ENTITY_PARAMS)
        | set(HOB_ENTITY_PARAMS)
        | set(HOB_ZONE_TIME_ATTRS)
        | set(PROGRAM_PARAM_NAMES)
        | set(HonRefProgramSelect._REF_ACTIVE_PROGRAM_ATTRS)
    )
    for registry in (SENSORS, BINARY_SENSORS):
        for descriptions in registry.values():
            for desc in descriptions:
                names.add(desc.attr_key)
                names.update(getattr(desc, "attr_fallbacks", ()) or ())
    names.add(_CONNECTIVITY.attr_key)
    for desc in _UNIVERSAL_GATED:
        names.add(desc.attr_key)
    for registry in (NUMBERS, _SETTINGS_SWITCHES):
        for descriptions in registry.values():
            for desc in descriptions:
                names.add(desc.param)
    for table in (
        _AP_TIMING_NUMBERS,
        _AIR_PURIFIER_SWITCHES,
        _PROGRAM_OPTION_NUMBERS,
        _PROGRAM_OPTION_SELECTS,
        _PROGRAM_OPTION_SWITCHES,
        _AC_DIRECTION_SELECTS,
    ):
        for desc in table:
            names.add(desc.param)
            attr = getattr(desc, "attr", None)
            if attr:
                names.add(attr)
    # The two other STATIC tables in diagnostics.py that name parameters. They
    # are harvested, and `_CUSTOM_ENTITY_SOURCES` deliberately is NOT: harvesting
    # the hand-written table into the set that is supposed to police it makes the
    # guard pass for anything anyone ever types into a custom row, which is the
    # one half of this section a platform table cannot vouch for. Measured: with
    # the harvest in place, planting an identity-shaped literal into a custom row
    # left the whole suite green.
    names |= set(diagnostics._AC_CLIMATE_PARAMS)
    for mapped in _CUSTOM_ENTITY_ATTRS.values():
        names |= set(mapped)
    # `pause` is the single custom-row name no other table in the repository
    # carries (switch.pause writes the `pause` parameter of pauseProgram /
    # resumeProgram, and no description table describes those commands). It is
    # spelled out here so that adding a SECOND such name is a deliberate act
    # recorded in this test rather than something the harvest absorbs silently.
    names.add("pause")
    return names


def _stop_button_section(commands: dict | None) -> dict:
    """The `entities` section for a dryer carrying `commands`, or an unread one.

    `commands` maps a command name to the parameter names the DEVICE declares
    under it -- the shape `_declared_command_params` returns. `None` stands for
    "the schema could not be read", which must not be confused with "the device
    declares nothing".
    """
    inventory = {
        "status": "ok",
        "by_domain": {"button": ["stop_program"], "switch": ["pause"]},
    }
    declared = (
        None if commands is None else {name: set(p) for name, p in commands.items()}
    )
    return diagnostics._entity_section(inventory, "TD", None, declared)


class EntitySourceTest(unittest.TestCase):
    """`entities.sources`: which raw parameter each registered entity speaks to."""

    def tearDown(self) -> None:
        _restore_registry()

    def test_a_number_names_the_parameter_it_reads_and_writes(self) -> None:
        _result, entities = _ref_dump()
        self.assertEqual(
            {"read": ["tempSelZ3"], "write": ["tempSelZ3"]},
            entities["sources"]["number.target_temp_zone3"],
        )

    def test_a_sensor_is_visibly_read_only(self) -> None:
        # The empty half is OMITTED, not emitted as []: a reader must be able to
        # tell "this control cannot be written" from "this dump did not look".
        _result, entities = _ref_dump()
        row = entities["sources"]["sensor.temp_zone3"]
        self.assertEqual({"read": ["tempZ3"]}, row)
        self.assertNotIn("write", row)

    def test_the_two_rows_that_answer_issue_75_sit_side_by_side(self) -> None:
        # THE point of the whole section. Establishing that `sensor.temp_zone3`
        # reads `tempZ3` while `number.target_temp_zone4` writes `tempSelZ4` cost
        # the original reporter a round trip through sensor.py and number.py.
        _result, entities = _ref_dump()
        sources = entities["sources"]
        self.assertEqual(["tempZ3"], sources["sensor.temp_zone3"]["read"])
        self.assertEqual(
            ["tempSelZ4"], sources["number.target_temp_zone4"]["write"]
        )

    def test_a_runtime_resolved_parameter_is_null_and_not_guessed(self) -> None:
        # The fridge program select picks whichever of PROGRAM_PARAM_NAMES the DEVICE
        # happens to carry, and it sends whole startProgram/stopProgram commands rather
        # than owning a named field, so no static table can say what it WRITES. The
        # write half stays absent for that reason -- not guessed, not invented.
        #
        # What it READS is static and now stated (#93). Before that the whole row was
        # null while the coverage block beside it accused `programName` of being
        # unmapped: one dump, two sections, opposite claims about the same attribute.
        _result, entities = _ref_dump()
        row = entities["sources"]["select.ref_program"]
        self.assertEqual(
            row["read"],
            [
                "intelligenceMode",
                "holidayMode",
                "quickModeZ1",
                "quickModeZ2",
                "programName",
                "prStr",
                "prCode",
            ],
        )
        self.assertNotIn("write", row)

    def test_every_registered_entity_gets_exactly_one_row(self) -> None:
        # The join contract: `sources` and `by_domain` describe the same set of
        # entities, or the tag stops being usable as a key between the two.
        _result, entities = _ref_dump()
        tagged = {
            f"{domain}.{key}"
            for domain, keys in entities["by_domain"].items()
            for key in keys
        }
        self.assertEqual(tagged, set(entities["sources"]))
        self.assertEqual(len(REF_ROWS), len(entities["sources"]))

    def test_sources_is_appended_after_the_existing_views(self) -> None:
        # Position is a contract of its own: a section that reshuffles its keys
        # between releases is one people stop skimming.
        _result, entities = _ref_dump()
        self.assertEqual("sources", list(entities)[-1])
        self.assertEqual("status", list(entities)[0])

    def test_a_disabled_entity_still_names_its_parameter(self) -> None:
        # A disabled entity is the single most common reason a control is
        # missing, and it is exactly the case where the reader wants to know
        # which parameter they would get back by re-enabling it.
        entries = [FakeRegistryEntry(uid, eid) for uid, eid in REF_ROWS]
        entries[1].disabled_by = "user"
        _result, entities = _ref_dump(entries=entries)
        self.assertEqual(
            {"number.target_temp_zone3": "user"}, entities["disabled"]
        )
        self.assertEqual(
            {"read": ["tempSelZ3"], "write": ["tempSelZ3"]},
            entities["sources"]["number.target_temp_zone3"],
        )

    def test_a_row_from_an_older_release_reads_as_null_not_as_absent(self) -> None:
        # A registry row whose suffix no current table knows must still appear.
        # Dropping it would make `sources` and `by_domain` disagree about how
        # many entities exist, which is the one thing a join key may not do.
        rows = REF_ROWS + ((f"{REF_ID}_retired_thing", "sensor.mario_fridge_x"),)
        _result, entities = _ref_dump(rows=rows)
        self.assertIn("sensor.retired_thing", entities["sources"])
        self.assertIsNone(entities["sources"]["sensor.retired_thing"])

    def test_the_index_is_per_type_and_never_offers_another_type_s_row(self) -> None:
        # Asserted against the INDEX, not against a fridge dump: `sources` is a
        # projection of `by_domain`, so a dump with no climate row could not show
        # `climate.climate` however broken the index was, and the assertion would
        # pass for the wrong reason.
        ref_index = diagnostics._mapped_sets("REF")[2]
        self.assertNotIn("climate.climate", ref_index)
        self.assertNotIn("switch.pause", ref_index)
        self.assertIn("climate.climate", diagnostics._mapped_sets("AC")[2])
        self.assertIn("switch.pause", diagnostics._mapped_sets("WD")[2])
        # Both tags above come from ONE code path -- the `_CUSTOM_ENTITY_SOURCES`
        # type filter. The registry walk builds type-scoped rows through three
        # OTHER mechanisms (the `app_type == APPLIANCE_AC` louver block, the
        # `app_type == APPLIANCE_AP` fixed-key block, and the per-desc `types`
        # filter), and a general-sounding name over a single-path check is how a
        # leak from one of the others ships unnoticed. Assert the property over
        # every type instead of sampling it.
        owners: dict[str, set[str]] = {}
        for app_type in ("REF", "AC", "WD", "AP", "WC", "OV", "HO", "TD", "WM"):
            for tag in diagnostics._mapped_sets(app_type)[2]:
                owners.setdefault(tag, set()).add(app_type)
        self.assertEqual({"AC"}, owners["select.fan_direction_vertical"])
        self.assertEqual({"AC"}, owners["select.fan_direction_horizontal"])
        self.assertEqual({"AC"}, owners["climate.climate"])
        self.assertEqual({"AP"}, owners["fan.purifier"])
        self.assertEqual({"WM", "WD", "TD"}, owners["switch.pause"])

    def test_the_binary_sensor_rows_name_the_parameters_they_read(self) -> None:
        # 63 of the 381 names this section emits are binary_sensor rows, and
        # nothing asserted a single one: the whole walk could be deleted, or
        # `binary_sensor.connectivity` pointed at another parameter, and the
        # suite stayed green. One row from each of the three tables that build
        # them -- the per-type registry, the universal connectivity row, and the
        # gated universal row.
        ref_index = diagnostics._mapped_sets("REF")[2]
        self.assertEqual({"read": ["doorStatusZ1"]}, ref_index["binary_sensor.door_zone1"])
        self.assertEqual({"read": ["available"]}, ref_index["binary_sensor.connectivity"])
        self.assertEqual(
            {"read": ["remoteCtrValid"]},
            diagnostics._mapped_sets("WD")[2]["binary_sensor.remote_control"],
        )

    def test_a_stop_button_names_the_parameter_it_writes(self) -> None:
        # `button.stop_program` fixes onOffStatus="0" on the command it sends,
        # and that write appears in NO other section of the dump. Its sibling
        # fixes nothing, so it stays null rather than being given the same row.
        # This is the TYPE-level claim; whether the device declares the name is
        # decided per appliance, in `_entity_section` -- see the two tests below.
        index = diagnostics._mapped_sets("WD")[2]
        self.assertEqual({"write": ["onOffStatus"]}, index["button.stop_program"])
        self.assertIsNone(index["button.start_program"])

    def test_a_stop_button_keeps_the_write_the_device_declares(self) -> None:
        # The live washer: `stopProgram` carries `onOffStatus`, so the button
        # really does fix it and the row is true as it stands.
        section = _stop_button_section({"stopProgram": ("onOffStatus",)})
        self.assertEqual(
            {"write": ["onOffStatus"]}, section["sources"]["button.stop_program"]
        )

    def test_a_stop_button_claims_no_write_the_device_cannot_perform(self) -> None:
        # The live dryer: `stopProgram` carries `returnStandby` and nothing
        # else, and `button.py` applies a fixed parameter only `if name in
        # params`, so the button fixes NOTHING there. `onOffStatus` is published
        # as a bare attribute on that dryer, so a reader chasing the row would
        # find the name and conclude the button works. Null is the same
        # statement `select.program` already makes: the entity exists and this
        # dump cannot name a parameter for it.
        section = _stop_button_section({"stopProgram": ("returnStandby",)})
        self.assertIsNone(section["sources"]["button.stop_program"])
        # The narrowing is surgical: a sibling row on the same appliance is
        # untouched, so this cannot be mistaken for the whole index degrading.
        self.assertEqual(
            {"read": ["machMode"], "write": ["pause"]},
            section["sources"]["switch.pause"],
        )

    def test_a_stop_button_row_is_never_narrowed_on_a_lookup_that_did_not_happen(
        self,
    ) -> None:
        # `declared=None` means the caller could not read the command schema.
        # Narrowing there would turn "not looked" into "the device does not
        # declare it", which is the one substitution every section of this dump
        # refuses to make.
        #
        # The first assertion is the one that makes the rest reach production.
        # `_entity_section` has a single caller (diagnostics.py:1882) and it
        # always passes `_declared_command_params(appliance)`, so the rule is
        # only held if THAT helper can express "unread". While it answered `{}`
        # for an unreadable schema the guard below was dead on the only path
        # that runs: `{}` is a Mapping, so the row was narrowed to null anyway.
        class _Unreadable:  # an appliance whose command schema cannot be read
            commands = "not a mapping"

        self.assertIsNone(diagnostics._declared_command_params(_Unreadable()))
        section = _stop_button_section(None)
        self.assertEqual(
            {"write": ["onOffStatus"]}, section["sources"]["button.stop_program"]
        )
        # ... and the appliance that WAS read and declares nothing still
        # narrows, so the two states stay distinguishable in the dump.
        self.assertIsNone(
            _stop_button_section({"stopProgram": []})["sources"]["button.stop_program"]
        )

    def test_the_purifier_fan_reads_power_without_claiming_to_write_it(self) -> None:
        # Power is a whole-COMMAND action on the AP (startProgram / stopProgram,
        # the latter with no values at all), so the fan chooses a value for
        # machMode and never for onOffStatus. Naming it as a write would send a
        # reader looking for a writable onOffStatus this integration never sets.
        index = diagnostics._mapped_sets("AP")[2]
        self.assertEqual(
            {"read": ["onOffStatus", "machMode"], "write": ["machMode"]},
            index["fan.purifier"],
        )

    def test_a_dotted_read_names_its_bare_fallback(self) -> None:
        # `_get_attr` tries the dotted key and then the bare one, so emitting
        # only the dotted spelling would hide the chain and send a reader looking
        # for a key that is not in `attributes` while the bare one is.
        index = diagnostics._mapped_sets("AC")[2]
        self.assertEqual(
            ["settings.windDirectionVertical", "windDirectionVertical"],
            index["select.fan_direction_vertical"]["read"],
        )
        self.assertEqual(
            ["windDirectionVertical"],
            index["select.fan_direction_vertical"]["write"],
        )
        climate_reads = index["climate.climate"]["read"]
        for dotted in ("settings.tempSel", "settings.machMode"):
            bare = dotted.removeprefix("settings.")
            self.assertIn(dotted, climate_reads)
            self.assertEqual(
                climate_reads.index(dotted) + 1, climate_reads.index(bare)
            )

    def test_a_louver_select_is_also_counted_as_mapped_coverage(self) -> None:
        # The two sections must not contradict each other. Before this section
        # existed `windDirectionHorizontal` sat in `command_params_unmapped`
        # while nothing disproved it; now `sources` names its writer, so the
        # coverage numerator has to agree.
        mapped_attrs, mapped_params, index, _missing = diagnostics._mapped_sets("AC")
        self.assertIn("windDirectionHorizontal", mapped_params)
        self.assertEqual(
            ["windDirectionHorizontal"],
            index["select.fan_direction_horizontal"]["write"],
        )

    def test_a_sensor_fallback_chain_keeps_its_order(self) -> None:
        # `actualWeight` then `weight`: the ORDER is the information, because it
        # is the order the entity tries them in. Sorting it would turn a chain
        # into an unordered set of equally-plausible names.
        index = diagnostics._mapped_sets("WD")[2]
        self.assertEqual(
            ["actualWeight", "weight"], index["sensor.estimated_weight"]["read"]
        )

    def test_a_program_option_names_both_places_it_is_read_from(self) -> None:
        # A buffered option is read bare and then under startProgram, because
        # that command is not mirrored into the shadow the way `settings` is.
        index = diagnostics._mapped_sets("WD")[2]
        self.assertEqual(
            {
                "read": ["prewash", "startProgram.prewash"],
                "write": ["prewash"],
            },
            index["switch.opt_prewash"],
        )

    def test_a_derived_sensor_names_both_attributes_it_needs(self) -> None:
        # The entity most likely to be reported as stuck on unknown: it needs two
        # keys, and a washer publishing one of them produces exactly that.
        index = diagnostics._mapped_sets("WD")[2]
        self.assertEqual(
            {"read": ["totalWaterUsed", "totalWashCycle"]},
            index["sensor.mean_water_consumption"],
        )

    def test_the_pause_switch_reads_and_writes_different_names(self) -> None:
        # The one row whose halves disagree: it reads machMode but writes the
        # `pause` parameter of pauseProgram/resumeProgram. A reader assuming the
        # halves match would go looking for a writable machMode that is not there.
        index = diagnostics._mapped_sets("WD")[2]
        self.assertEqual(
            {"read": ["machMode"], "write": ["pause"]}, index["switch.pause"]
        )

    def test_the_degraded_section_gains_nothing(self) -> None:
        # An unreadable registry yields the marker and NOTHING else. Hanging a
        # `sources` key off it would claim a lookup that never happened.
        result, degraded = _ref_dump(raises=True)
        self.assertEqual({"status": "unavailable"}, degraded)
        self.assertEqual({"status": "unavailable"}, result["platforms"])

    def test_unreadable_registries_give_ONE_null_not_a_null_per_entity(self) -> None:
        # The lazy imports are what fail here, not the entity registry: the
        # section knows WHICH entities exist and cannot say what any of them
        # speaks to. A map of nulls would read as "all six were looked up and
        # none is known", which is a finding; one null is the absence of one.
        # It takes the TOTAL collapse to reach that now. One broken module
        # leaves most of the walk readable, and the honest statement is then per
        # entity -- see the test below, which is the other half of this one.
        with _BrokenModules(*_PLATFORM_MODULES):
            _result, entities = _ref_dump()
        self.assertIsNone(entities["sources"])
        self.assertEqual(6, sum(len(v) for v in entities["by_domain"].values()))

    def test_one_broken_module_names_the_entities_it_did_not_lose(self) -> None:
        # The other half, and the reason the per-import guards exist. With
        # `select` unimportable the fridge's select row is the only one that
        # cannot be named; blanking the map would throw away five rows that were
        # read correctly, and it used to travel with a coverage block accusing
        # the integration of not mapping tempSelZ1/Z2/Z3.
        with _BrokenModules("select"):
            result, entities = _ref_dump()
        sources = entities["sources"]
        self.assertIsNotNone(sources)
        self.assertEqual(
            {"read": ["tempSelZ3"], "write": ["tempSelZ3"]},
            sources["number.target_temp_zone3"],
        )
        self.assertEqual({"read": ["tempZ3"]}, sources["sensor.temp_zone3"])
        coverage = result["appliances"][0]["coverage"]
        self.assertEqual(["select"], coverage["registries_unavailable"])
        for name in ("tempSelZ1", "tempSelZ2", "tempSelZ3"):
            self.assertNotIn(name, coverage["command_params_unmapped"])

    def test_the_two_sections_share_one_degradation_decision(self) -> None:
        # Correction 6 made real. With the walk folded into `_mapped_sets`, a
        # broken platform module degrades coverage AND sources together; two
        # independent import blocks would let one dump report `tempSelZ3` as
        # confidently mapped while the other half claimed nothing was readable.
        # What is shared is the DECISION, not its blast radius: each import
        # fails on its own, so `select` costs the select tag on BOTH sides and
        # leaves `tempSelZ3` mapped on both sides.
        with _BrokenModules("select"):
            result, entities = _ref_dump()
            mapped_attrs, mapped_params, index, missing = diagnostics._mapped_sets(
                "REF"
            )
        self.assertEqual(["select"], missing)
        self.assertIn("tempSelZ3", mapped_params)
        self.assertIn("tempZ3", mapped_attrs)
        # ... and the dump SAYS SO in both halves, which is the actual claim.
        coverage = result["appliances"][0]["coverage"]
        self.assertEqual(["select"], coverage["registries_unavailable"])
        self.assertIsNotNone(entities["sources"])
        # Positive control: the same calls with the module intact carry no
        # marker at all, so the assertions above cannot pass for the wrong
        # reason.
        mapped_attrs, mapped_params, index, missing = diagnostics._mapped_sets("REF")
        self.assertEqual([], missing)
        self.assertIsNotNone(index)
        self.assertIn("tempSelZ3", mapped_params)
        _result, healthy = _ref_dump()
        self.assertNotIn(
            "registries_unavailable", _result["appliances"][0]["coverage"]
        )
        self.assertIsNotNone(healthy["sources"])

    def test_a_device_dump_says_when_the_registries_were_unreadable(self) -> None:
        # The case `entities.sources` structurally cannot cover: there is no
        # inventory to decorate, so the coverage marker is the only thing in the
        # document that stops the reader believing the integration maps nothing.
        # `sensor` is the module broken here, because it is the one that owns
        # `tempZ1`: the marker has to be shown naming a loss that actually moved
        # a number, or the test would pass on a module whose absence is free.
        with _BrokenModules("sensor"):
            block = diagnostics._appliance_block(
                "id1",
                {
                    "appliance": None,
                    "type": "REF",
                    "attributes": {"tempZ1": 4},
                    "statistics": {},
                },
            )
        self.assertIsNone(block["entities"])
        self.assertEqual(["sensor"], block["coverage"]["registries_unavailable"])
        self.assertEqual(["tempZ1"], block["coverage"]["attributes_unmapped"])

    def test_a_foreign_inventory_shape_degrades_instead_of_raising(self) -> None:
        # `_appliance_block` is called with no try/except around it from either
        # entry point, so every one of these must return rather than raise.
        self.assertIsNone(diagnostics._entity_section(None, "REF"))
        self.assertIsNone(diagnostics._entity_section("nonsense", "REF"))
        self.assertEqual(
            {"status": "unavailable"},
            diagnostics._entity_section({"status": "unavailable"}, "REF"),
        )
        self.assertEqual(
            {"status": "ok", "by_domain": "not-a-map"},
            diagnostics._entity_section(
                {"status": "ok", "by_domain": "not-a-map"}, "REF"
            ),
        )
        section = diagnostics._entity_section(
            {"status": "ok", "by_domain": {"sensor": "not-a-list"}}, "REF"
        )
        self.assertEqual({}, section["sources"])
        # An appliance whose type is missing or junk still produces a section:
        # every tag resolves to null rather than raising on a table lookup.
        junk = diagnostics._entity_section(
            {"status": "ok", "by_domain": {"sensor": ["temp_zone3"]}}, None
        )
        self.assertEqual({"sensor.temp_zone3": None}, junk["sources"])

    def test_the_caller_s_inventory_is_not_mutated(self) -> None:
        # It is a copy, like the `dict(entities)` it replaces: the inventory is
        # built once for the whole dump and shared with the entry-level view.
        inventory = {"status": "ok", "by_domain": {"sensor": ["temp_zone3"]}}
        section = diagnostics._entity_section(inventory, "REF")
        self.assertIn("sources", section)
        self.assertNotIn("sources", inventory)


class RegistryDegradationTest(unittest.TestCase):
    """One unimportable platform module costs its own tables and nothing else.

    Before the per-import guards, ANY of the seven brought the whole walk down:
    on the four archived live appliances a single broken module roughly doubled
    or tripled `attributes_unmapped`, and the REF's three declared setpoints
    turned into `command_params_unmapped` entries -- an accusation, on the list
    this dump calls the gold signal, about controls the integration ships and
    the reporter is using. These tests pin the blast radius and not the figures,
    which move whenever a table gains a row and differ again depending on how an
    appliance is rebuilt from an archived dump.

    Five of the seven are leaves and genuinely degrade alone: `binary_sensor`,
    `number`, `select`, `sensor`, `switch`. `air_purifier` is imported by all
    five and `program_options` by three, so breaking either takes its importers
    with it however these guards are arranged -- `setUp` explains why. The
    worked example for what this rescues is `sensor` on a fridge or `select` on
    an AC, not the air purifier.
    """

    def setUp(self) -> None:
        # Each guard in `_mapped_sets` is per-IMPORT, and an import only fails
        # ALONE when the module is already in `sys.modules`. Five of the seven
        # import `air_purifier` and three import `program_options`, so on a cold
        # interpreter breaking either takes its importers down with it. Home
        # Assistant has loaded all seven at platform setup long before anyone
        # downloads a dump, so the cached tree is the state this class measures
        # -- import them HERE instead of inheriting whichever test ran first, or
        # `pytest -k RegistryDegradation` turns green into red on correct code.
        for module in _PLATFORM_MODULES:
            importlib.import_module(f"custom_components.addhon.{module}")

    def tearDown(self) -> None:
        _restore_registry()

    def test_no_single_break_empties_the_walk(self) -> None:
        # The contract in one place, over every module and five types: nothing
        # is invented (each broken set is a subset of the healthy one) and
        # nothing is wiped (an index survives), and the marker names exactly the
        # module that failed.
        for app_type in ("REF", "AC", "WD", "TD", "AP"):
            healthy_attrs, healthy_params, healthy_index, healthy_missing = (
                diagnostics._mapped_sets(app_type)
            )
            self.assertEqual([], healthy_missing)
            for module in _PLATFORM_MODULES:
                with self.subTest(app_type=app_type, module=module):
                    with _BrokenModules(module):
                        attrs, params, index, missing = diagnostics._mapped_sets(
                            app_type
                        )
                    self.assertEqual([module], missing)
                    self.assertIsNotNone(index)
                    self.assertTrue(index)
                    self.assertLessEqual(attrs, healthy_attrs)
                    self.assertLessEqual(params, healthy_params)
                    self.assertLessEqual(set(index), set(healthy_index))

    def test_a_broken_module_stops_accusing_the_fridge_of_its_own_setpoints(
        self,
    ) -> None:
        # The defect this follow-up exists for, on the appliance it was measured
        # on. `NUMBERS["REF"]` is what maps tempSelZ1/Z2/Z3, so `number` losing
        # them is truthful and is asserted as such; the other six modules used to
        # lose them too, and the dump then told the reporter to go and add three
        # controls they were already using.
        for module in _PLATFORM_MODULES:
            with self.subTest(module=module):
                with _BrokenModules(module):
                    result, _entities = _ref_dump()
                coverage = result["appliances"][0]["coverage"]
                self.assertEqual([module], coverage["registries_unavailable"])
                unmapped = coverage["command_params_unmapped"]
                for name in ("tempSelZ1", "tempSelZ2", "tempSelZ3"):
                    if module == "number":
                        self.assertIn(name, unmapped)
                    else:
                        self.assertNotIn(name, unmapped)

    def test_only_a_total_collapse_blanks_the_whole_map(self) -> None:
        # `_CUSTOM_ENTITY_SOURCES` is a literal in diagnostics.py and its rows
        # survive any import failure, so the None decision cannot be taken on
        # the size of the finished map -- REF keeps `select.ref_program` even
        # with all seven modules gone. It is taken on whether any PLATFORM table
        # produced a row, which is the question the null answers.
        with _BrokenModules(*_PLATFORM_MODULES):
            attrs, params, index, missing = diagnostics._mapped_sets("REF")
        self.assertIsNone(index)
        self.assertEqual(list(_PLATFORM_MODULES), missing)
        # REF carries no static seed, so every mapped name it has comes from a
        # walk and a total collapse leaves nothing.
        self.assertEqual(set(), attrs)
        self.assertEqual(set(), params)
        # The general sentence "no seed survives a collapse" is FALSE, and
        # measuring it on REF alone cannot show that: `mapped_params |=
        # _AC_CLIMATE_PARAMS` and `mapped_params.update(PROGRAM_PARAM_NAMES)`
        # are unconditional, so AC and the wash group keep names with all seven
        # modules gone. That is deliberate, and the rule this pins is "nothing
        # BEYOND the declared seeds" -- an invented one would subtract names
        # from `command_params_unmapped` in the same block whose
        # `registries_unavailable` says nothing could be read.
        for app_type, seed in (
            ("AC", set(diagnostics._AC_CLIMATE_PARAMS)),
            ("WM", set(diagnostics.PROGRAM_PARAM_NAMES)),
            ("TD", set(diagnostics.PROGRAM_PARAM_NAMES)),
            ("WD", set(diagnostics.PROGRAM_PARAM_NAMES)),
        ):
            with self.subTest(app_type=app_type):
                with _BrokenModules(*_PLATFORM_MODULES):
                    seeded_attrs, seeded_params, seeded_index, _m = (
                        diagnostics._mapped_sets(app_type)
                    )
                self.assertIsNone(seeded_index)
                self.assertEqual(set(), seeded_attrs)
                self.assertEqual(seed, seeded_params)

    def test_a_lost_table_gives_its_entities_one_null_each(self) -> None:
        # The AC louvers are the case that shows the difference: `select` gone,
        # and only the two select rows lose their names while the climate row
        # beside them keeps every one of its reads.
        inventory = {
            "status": "ok",
            "by_domain": {
                "select": ["fan_direction_vertical", "fan_direction_horizontal"],
                "climate": ["climate"],
            },
        }
        with _BrokenModules("select"):
            section = diagnostics._entity_section(inventory, "AC")
        self.assertEqual(
            {
                "select.fan_direction_vertical": None,
                "select.fan_direction_horizontal": None,
            },
            {k: v for k, v in section["sources"].items() if k.startswith("select.")},
        )
        self.assertTrue(section["sources"]["climate.climate"]["read"])
        # Positive control on the same inventory: healthy, both selects are named.
        healthy = diagnostics._entity_section(inventory, "AC")
        self.assertEqual(
            ["windDirectionVertical"],
            healthy["sources"]["select.fan_direction_vertical"]["write"],
        )

    def test_a_lost_startprogram_name_never_invents_an_absence(self) -> None:
        # `program_options` owns one string in this walk: the command the
        # buffered options are read under. Emitting the read chain without it
        # would leave a one-link chain, and `_coverage` would then report every
        # option the device publishes only under `startProgram` as a control the
        # device does not have -- while `entities.sources` named its writer two
        # sections below. Measured on the live TD, that is `tumblingStatus`.
        # Skipping the loop costs coverage and states nothing false, and the
        # discriminator is that the option's TAG is gone from the index too.
        appliance = FakeAppliance(
            commands={
                "settings": FakeCommand({"tumblingStatus": FakeParam(value="1")}),
                "startProgram": FakeCommand(
                    {"tumblingStatus": FakeParam(value="1")}
                ),
            }
        )
        entry = {
            "appliance": appliance,
            "type": "TD",
            "attributes": {"startProgram.tumblingStatus": "1"},
            "statistics": {},
        }
        with _BrokenModules("program_options"):
            index = diagnostics._mapped_sets("TD")[2]
            coverage = diagnostics._appliance_block("id1", entry)["coverage"]
        self.assertEqual(["program_options"], coverage["registries_unavailable"])
        self.assertNotIn("switch.opt_tumbling", index)
        self.assertNotIn("tumblingStatus", coverage["attributes_expected_absent"])
        # Positive control: healthy, the tag is there and the name is mapped, so
        # the two assertions above cannot be passing on an empty walk.
        self.assertIn("switch.opt_tumbling", diagnostics._mapped_sets("TD")[2])
        healthy = diagnostics._appliance_block("id1", entry)["coverage"]
        self.assertNotIn("registries_unavailable", healthy)
        self.assertNotIn("tumblingStatus", healthy["attributes_expected_absent"])
        self.assertNotIn("tumblingStatus", healthy["command_params_unmapped"])

    def test_the_walk_degrades_and_never_raises(self) -> None:
        # `_appliance_block` is called with no try/except around it from either
        # entry point, and seven guards are seven more places to raise from --
        # `_CONNECTIVITY` in particular is the one table entry with no type gate
        # and nothing to be empty, so it is a None and not a `()`. Every
        # one-missing and the all-missing case, on a real block.
        entry = {
            "appliance": FakeAppliance(commands={}),
            "type": "WD",
            "attributes": {"machMode": "1"},
            "statistics": {},
        }
        cases = [(module,) for module in _PLATFORM_MODULES]
        cases.append(_PLATFORM_MODULES)
        for modules in cases:
            with self.subTest(modules=modules):
                with _BrokenModules(*modules):
                    block = diagnostics._appliance_block("id1", entry)
                self.assertEqual(
                    list(modules), block["coverage"]["registries_unavailable"]
                )


class EntitySourceDriftGuardTest(unittest.TestCase):
    """Nothing the cloud chose may reach this section, in either half."""

    def tearDown(self) -> None:
        _restore_registry()

    def test_every_emitted_name_is_a_static_table_field(self) -> None:
        # The value axis has NO redactor behind it: `read`/`write` are not in
        # `_TO_REDACT`, and `_MAC_RE` only catches MAC shapes. What makes the
        # section safe is that every name is copied out of a table in this
        # repository, so this asserts exactly that, over every type -- and the
        # allowed set is harvested from the PLATFORM tables, never from
        # `_CUSTOM_ENTITY_SOURCES`, so a name typed into a custom row has to be
        # justified by some other table or by the one-word allowlist.
        #
        # "Over every type" was a claim this loop did not honour: the list below
        # was written when nine types had rows and stayed at nine when the induction
        # hob gained its own (#84). `select.power_limit` and the twelve
        # `sensor.remaining_time_zone*` rows therefore shipped as the only names in
        # `entities.sources` that no guard had ever looked at -- the one section of
        # the dump whose safety rests entirely on this test.
        allowed = _static_parameter_names()
        self.assertIn("tempSelZ3", allowed)  # not vacuous
        seen = 0
        swept_domains: dict[str, set[str]] = {}
        for app_type in (
            "REF", "AC", "WD", "AP", "WC", "OV", "HO", "IH", "HOB", "TD", "WM"
        ):
            index = diagnostics._mapped_sets(app_type)[2]
            self.assertIsNotNone(index)
            for tag, row in index.items():
                swept_domains.setdefault(app_type, set()).add(tag.split(".")[0])
                for name in (row or {}).get("read", []) + (row or {}).get("write", []):
                    seen += 1
                    bare = name
                    for prefix in ("settings.", "startProgram."):
                        if name.startswith(prefix):
                            bare = name[len(prefix):]
                    self.assertTrue(
                        name in allowed or bare in allowed,
                        f"{app_type} {tag}: {name} is in no static table",
                    )
        # A slack floor lets an ENTIRE table stop being walked unnoticed: the
        # sensor.* rows alone are over a hundred names, so deleting one table
        # would still leave the count high. The floor is therefore kept TIGHT
        # against the real count (562 across the eleven types), and it has to be
        # raised deliberately whenever a type or a table joins the sweep. A union
        # floor is also blind to a table that stops being walked for ONE type,
        # because the other ten keep the domain in the union: pin the SHAPE of the
        # sweep, per type, and not only a number.
        self.assertGreater(
            seen,
            # Measured 562 today (555 before the fridge program select declared its
            # seven read names, #93). Raised deliberately, as the comment above
            # requires -- and re-measured rather than incremented, because the 549 this
            # line was pinned against had already drifted six names behind reality.
            553,
            f"the sweep shrank to {seen} names",
        )
        self.assertEqual(
            {
                "REF": {"binary_sensor", "number", "select", "sensor"},
                "AC": {"binary_sensor", "climate", "select", "sensor", "switch"},
                "WD": {"binary_sensor", "button", "number", "select", "sensor", "switch"},
                "AP": {"binary_sensor", "fan", "number", "select", "sensor", "switch"},
                "WC": {"binary_sensor", "number", "sensor", "switch"},
                "OV": {"binary_sensor", "number", "sensor"},
                # The hood gained its fan, light/timer switches and delay number
                # in 5.18.0 (#83); before that it was read-only like the oven.
                "HO": {"binary_sensor", "fan", "number", "sensor", "switch"},
                # The two spellings of an induction hob. Both were missing from
                # this sweep when their custom rows shipped (#84), so the intake
                # limit and the twelve per-zone timers were the only names in
                # `entities.sources` no drift guard had ever looked at. The
                # `select` domain is the intake limit; the timers land under
                # `sensor` beside the description rows.
                "IH": {"binary_sensor", "select", "sensor"},
                "HOB": {"binary_sensor", "select", "sensor"},
                "TD": {"binary_sensor", "button", "number", "select", "sensor", "switch"},
                "WM": {"binary_sensor", "button", "number", "select", "sensor", "switch"},
            },
            swept_domains,
            "a table stopped being walked for some appliance type",
        )

    def test_every_emitted_name_matches_the_closed_shape(self) -> None:
        # A second, cheaper net under the first: whatever a future edit starts
        # emitting must at least look like a parameter identifier and not like a
        # value, a serial or a sentence. A LEADING DIGIT is allowed on purpose --
        # the air conditioner really does carry `10degreeHeatingStatus` -- so the
        # pattern is deliberately one character wider than it looks.
        name_re = r"^[A-Za-z0-9][A-Za-z0-9_]*(\.[A-Za-z0-9][A-Za-z0-9_]*)?$"
        tag_re = r"^[a-z_]+\.[A-Za-z0-9_]+$"
        for app_type in ("REF", "AC", "WD", "AP"):
            for tag, row in diagnostics._mapped_sets(app_type)[2].items():
                self.assertRegex(tag, tag_re)
                for name in (row or {}).get("read", []) + (row or {}).get("write", []):
                    self.assertRegex(name, name_re)

    def test_the_climate_write_half_is_the_coverage_list(self) -> None:
        # Two INDEPENDENT statements of the same fact: `_AC_CLIMATE_PARAMS` is
        # what coverage counts as mapped, and the literal in
        # `_CUSTOM_ENTITY_SOURCES` is what the dump tells the reader the climate
        # entity writes. They are written out separately on purpose, so that this
        # comparison is a drift guard rather than a value asserted against itself.
        row = next(
            entry
            for entry in diagnostics._CUSTOM_ENTITY_SOURCES
            if entry["tag"] == "climate.climate"
        )
        self.assertEqual(
            sorted(diagnostics._AC_CLIMATE_PARAMS), sorted(row["write"])
        )

    def test_sources_carry_no_identity(self) -> None:
        # Anti-vacuity FIRST: a leak scan over an empty or absent section passes
        # for the wrong reason, and that is how this exact test class has failed
        # to catch things elsewhere.
        result, entities = _ref_dump()
        sources = entities["sources"]
        self.assertIsNotNone(sources)
        self.assertTrue(any(sources.values()))
        encoded = json.dumps(sources)
        self.assertNotIn(REF_ID, encoded)
        self.assertNotIn("mario_fridge", encoded)
        self.assertNotIn("REF-PLAINTEXT-SERIAL", encoded)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", encoded)
        # And the same over the WHOLE document, which is what the reporter
        # actually attaches to the issue.
        self.assertNotIn(REF_ID, json.dumps(result))
        self.assertNotIn("REF-PLAINTEXT-SERIAL", json.dumps(result))

    def test_no_device_value_can_reach_the_row(self) -> None:
        # The section describes the SCHEMA, never the reading. A fridge whose
        # shadow is full of identity-shaped junk must produce byte-identical
        # rows to one whose shadow is empty.
        coord = _build_ref_coordinator()
        clean = diagnostics._appliance_block(REF_ID, coord.data[REF_ID], {
            "status": "ok", "by_domain": {"sensor": ["temp_zone3"]},
        })
        poisoned_data = dict(coord.data[REF_ID])
        poisoned_data["attributes"] = {
            "tempZ3": "AA:BB:CC:DD:EE:FF",
            "serialNumber": "REF-PLAINTEXT-SERIAL",
        }
        poisoned = diagnostics._appliance_block(REF_ID, poisoned_data, {
            "status": "ok", "by_domain": {"sensor": ["temp_zone3"]},
        })
        self.assertEqual(clean["entities"], poisoned["entities"])
        self.assertEqual(
            {"read": ["tempZ3"]}, clean["entities"]["sources"]["sensor.temp_zone3"]
        )

    def test_a_custom_row_with_a_broken_tag_is_skipped_not_raised(self) -> None:
        # The custom-table loop sits OUTSIDE the lazy-import try, and
        # `_appliance_block` has no try/except around it from either entry point,
        # so a malformed row added later would cost the whole dump rather than
        # one section. Each guard is exercised on its own shape.
        broken = (
            {"tag": "no-dot-here", "types": ("REF",), "read": ("tempZ3",)},
            {"tag": ".leading", "types": ("REF",), "read": ("tempZ3",)},
            {"tag": "trailing.", "types": ("REF",), "read": ("tempZ3",)},
            {"tag": "sensor.orphan"},
            {"read": ("tempZ3",), "types": ("REF",)},
            {"tag": "sensor.no_types", "read": ("tempZ3",)},
            {"tag": "sensor.null_types", "types": None, "read": ("tempZ3",)},
        )
        saved = diagnostics._CUSTOM_ENTITY_SOURCES
        diagnostics._CUSTOM_ENTITY_SOURCES = broken
        try:
            index = diagnostics._mapped_sets("REF")[2]
        finally:
            diagnostics._CUSTOM_ENTITY_SOURCES = saved
        self.assertNotIn("no-dot-here", index)
        self.assertNotIn("sensor.no_types", index)
        self.assertNotIn("sensor.null_types", index)
        # Seven broken shapes go in; assert all of them, not three. The tag guard
        # is a TWO-clause condition (`not domain or not suffix`) and only the
        # suffix half was covered -- `if not domain` could be deleted and nothing
        # in the suite noticed, so a row tagged '.leading' shipped with an empty
        # domain that joins with no `by_domain` entry.
        self.assertNotIn(".leading", index)
        self.assertNotIn("trailing.", index)
        self.assertNotIn("sensor.orphan", index)
        # The real table is back and still works, so the swap above cannot leave
        # the rest of the suite testing a stub.
        self.assertIn("select.ref_program", diagnostics._mapped_sets("REF")[2])

    def test_the_custom_table_names_only_entities_that_can_exist(self) -> None:
        # A row for a tag no platform ever creates would be dead weight the
        # reader can never see, and a row whose domain is not a real platform
        # would never join with `by_domain`.
        domains = {"sensor", "binary_sensor", "number", "select", "switch",
                   "button", "climate", "fan"}
        for entry in diagnostics._CUSTOM_ENTITY_SOURCES:
            domain, _dot, suffix = entry["tag"].partition(".")
            self.assertIn(domain, domains, entry["tag"])
            self.assertTrue(suffix, entry["tag"])
            # TUPLES, not bare strings. `app_type not in "REF"` is a SUBSTRING
            # test that quietly matches "R", and the same trap turns a one-name
            # `read` into a row naming every letter of the parameter.
            self.assertIsInstance(entry.get("types"), tuple, entry["tag"])
            self.assertTrue(entry["types"], entry["tag"])
            for half in ("read", "write"):
                if half in entry:
                    self.assertIsInstance(entry[half], tuple, entry["tag"])


# --- the walk is complete: no platform table is invisible to it -------------


# The integration modules whose description tables `_mapped_sets` is expected to
# account for. This IS a whitelist, and a whitelist is the defect this section
# exists to close -- so it is not trusted:
# `test_no_other_module_grew_a_description_table` below derives the same set from
# the SOURCE of every file under custom_components/addhon and fails if the two
# disagree. The chain therefore bottoms out in something no contributor has to
# remember to update.
_TABLE_MODULES = ("binary_sensor", "number", "select", "sensor", "switch")

# Tables that deliberately contribute no parameter the dump should account for.
#
# EMPTY, and that is the honest state: every description table in the five
# modules above is reached by the walk today. An entry here is a promise that a
# reader of a dump is not misled by the absence of these parameters from
# `coverage` and `entities.sources`, so the reason must say what the dump prints
# for them INSTEAD and why that is right -- not that the table is new, internal,
# experimental or special. `test_every_exemption_is_still_needed` deletes the
# entry for you the day it stops being true, by failing.
_TABLES_OUTSIDE_COVERAGE: dict[str, str] = {}

# Planted into a clone of every table at once, then looked for in the tags
# `_mapped_sets` emits. The counter appended to it is fixed width, so no probe
# key can be a suffix of another; the prefix is spelled unlike any real
# translation key so a leak into a real dump would be unmistakable.
_PROBE_KEY = "zz_completeness_probe_"


def _is_entity_description(obj) -> bool:
    """True for a description INSTANCE of any of the ten description classes.

    Duck-typed rather than isinstance-checked against a base: the ten classes
    have no common ancestor. Four extend Home Assistant's `EntityDescription`
    (itself stubbed differently by conftest and by this file) and six --
    `HonSettingsSwitchDescription`, `HonProgramOptionSelectDescription` and their
    siblings -- are plain local dataclasses that extend nothing. What all ten
    share, and what the walk actually reads, is that they are dataclass instances
    carrying a `key`.
    """
    return (
        dataclasses.is_dataclass(obj)
        and not isinstance(obj, type)
        and any(field.name == "key" for field in dataclasses.fields(obj))
    )


def _descriptions_in(value, seen=None) -> list:
    """Every description reachable from one module-level binding.

    The real tables are not one shape. `SENSORS` and `_SETTINGS_SWITCHES` are
    dicts keyed by appliance type; `_PROGRAM_OPTION_SWITCHES` and `_WASH_EXTRA`
    are flat tuples; `_CONNECTIVITY` is a single description with no container at
    all. Recursing over containers instead of matching a shape means a table
    added in a shape nobody anticipated is still found, and the `seen` set means
    a table that appears twice (`_REMOTE_CONTROL` is also inside
    `_UNIVERSAL_GATED`) cannot loop or double-count.
    """
    seen = set() if seen is None else seen
    if id(value) in seen:
        return []
    seen.add(id(value))
    if _is_entity_description(value):
        return [value]
    found: list = []
    if isinstance(value, Mapping):
        for item in value.values():
            found.extend(_descriptions_in(item, seen))
    elif isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            found.extend(_descriptions_in(item, seen))
    return found


def _with_probe_row(value, probe):
    """`value` plus one extra description, or None if it cannot be built.

    Returning None rather than guessing is deliberate: a table shape this cannot
    augment is a table whose reachability cannot be MEASURED, and the guard says
    so out loud instead of reporting the silence as a pass.
    """
    if _is_entity_description(value):
        augmented = probe
    elif isinstance(value, Mapping):
        augmented = dict(value)
        for key, item in list(augmented.items()):
            if isinstance(item, tuple):
                augmented[key] = item + (probe,)
            elif isinstance(item, list):
                augmented[key] = [*item, probe]
            elif _is_entity_description(item):
                augmented[key] = probe
    elif isinstance(value, tuple):
        augmented = value + (probe,)
    elif isinstance(value, list):
        augmented = [*value, probe]
    elif isinstance(value, frozenset):
        augmented = value | {probe}
    elif isinstance(value, set):
        augmented = set(value) | {probe}
    else:
        return None
    # Prove the probe really landed. A Mapping of some future shape could come
    # back unchanged, and an unchanged clone would report a walked table as
    # unwalked -- a failure the maintainer could not act on.
    if not any(item is probe for item in _descriptions_in(augmented)):
        return None
    return augmented


def _appliance_types() -> tuple[str, ...]:
    """Every appliance type the integration knows, harvested from `const`.

    Read off the module rather than listed here so a type added next year is
    probed the day it is added.
    """
    from custom_components.addhon import const

    return tuple(sorted({
        value
        for name, value in vars(const).items()
        if name.startswith("APPLIANCE_") and isinstance(value, str)
    }))


def _description_tables() -> dict:
    """{"<module>.<NAME>": (module, attr, value, {id of each description})}."""
    tables: dict = {}
    for name in _TABLE_MODULES:
        try:
            module = importlib.import_module(f"custom_components.addhon.{name}")
        except Exception as err:  # pragma: no cover - a missing stub, not drift
            raise AssertionError(
                f"custom_components/addhon/{name}.py could not be imported under "
                f"this file's Home Assistant stubs ({err!r}), so its description "
                "tables cannot be checked against the diagnostics walk. Add what "
                "it needs to `_install_stubs` at the top of this file."
            ) from err
        for attr, value in list(vars(module).items()):
            if attr.startswith("__"):
                continue
            rows = _descriptions_in(value)
            if rows:
                tables[f"{name}.{attr}"] = (module, attr, value, {id(r) for r in rows})
    return tables


# Marks a table the probe could not be planted in, so `_completeness_failure`
# can say "not measured" instead of "not walked". The distinction is not
# pedantry: a per-zone `dict[str, dict[str, Description]]` wired correctly into
# `_mapped_sets` is unprobeable, and reporting it as invisible to the dump would
# print a falsehood and then ask the contributor to write it down.
_UNMEASURED = "unmeasured: "


def _unwalked_description_tables() -> dict:
    """{"<module>.<NAME>": why} for every table the dump cannot see.

    MEASURED, not read off a list: one extra description with a unique key is
    planted in a clone of every table, `_mapped_sets` is run for every appliance
    type, and a table counts as walked when its planted key comes back as an
    `entities.sources` tag. Asking the walk what it read -- instead of listing
    what it is supposed to read -- is the whole point; a second hand-kept list
    would have exactly the blind spot of the first.

    A table whose probe never surfaces still passes when every description in it
    is also inside a table that did surface. That is not a loophole, it is the
    normal case: `_WASH_CONSUMPTION` and the other thirty per-type tuples are
    concatenated into `SENSORS` at import, so replacing the tuple afterwards
    cannot reach the walk, while the descriptions themselves are read every time.
    Containment is by object identity, so a tuple that only LOOKS like part of
    `SENSORS` is not excused.
    """
    tables = _description_tables()
    order = sorted(tables)
    probes: dict[str, str] = {}
    unprobeable: dict[str, str] = {}
    restore = [(tables[qname][0], tables[qname][1], tables[qname][2])
               for qname in order]
    try:
        for position, qname in enumerate(order):
            module, attr, value, _ids = tables[qname]
            key = f"{_PROBE_KEY}{position:03d}"
            try:
                probe = dataclasses.replace(_descriptions_in(value)[0], key=key)
                augmented = _with_probe_row(value, probe)
            except Exception as err:
                augmented = None
                unprobeable[qname] = (
                    _UNMEASURED + f"it could not be cloned to probe it: {err!r}"
                )
            if augmented is None:
                unprobeable.setdefault(
                    qname,
                    _UNMEASURED + "`_with_probe_row` has no rule for its shape",
                )
                continue
            probes[key] = qname
            setattr(module, attr, augmented)
        walked: set[str] = set()
        for app_type in _appliance_types():
            for tag in diagnostics._mapped_sets(app_type)[2] or {}:
                _before, marker, position = str(tag).partition(_PROBE_KEY)
                planted = probes.get(marker + position) if marker else None
                if planted:
                    walked.add(planted)
    finally:
        for module, attr, value in restore:
            setattr(module, attr, value)

    if probes and not walked:
        # Restore first, then complain. `_mapped_sets` returns a None index when
        # any of the platform modules it lazily imports fails to import, and on
        # that path EVERY table would be reported unwalked at once -- sixty-odd
        # lines of consequence hiding one cause.
        raise AssertionError(
            "`_mapped_sets` produced no `entities.sources` index for any of the "
            f"{len(_appliance_types())} appliance types, so nothing could be "
            "measured. That is its documented degraded path: one of the platform "
            "modules it imports lazily raised on import. Fix that first."
        )

    reached: set[int] = set()
    for qname in walked:
        if qname in tables:
            reached |= tables[qname][3]
    unwalked = dict(unprobeable)
    for qname, (_module, _attr, _value, ids) in tables.items():
        if qname in walked or qname in unwalked or ids <= reached:
            continue
        unwalked[qname] = (
            f"{len(ids)} description(s), and no table the walk reads contains them"
        )
    return unwalked


def _modules_declaring_descriptions() -> set[str]:
    """Every integration module whose SOURCE constructs an entity description.

    Parsed, never imported: this is the check that has to keep working for a
    module the stubs in this file cannot import (climate.py needs a
    `homeassistant.components.climate` nobody stubs today), which is exactly the
    module a table could be added to without any of this noticing.
    """
    package = REPO_ROOT / "custom_components" / "addhon"
    found: set[str] = set()
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if isinstance(name, str) and name.endswith("Description"):
                found.add(path.relative_to(package).with_suffix("").as_posix())
                break
    return found


def _completeness_failure(unwalked: dict) -> str:
    """The whole value of this guard is that whoever trips it knows what to do.

    Two populations, and telling them apart is the point. A table the probe
    reached and the walk did not is PROVEN invisible to the dump. A table the
    probe could not be planted in is merely UNMEASURED -- the walk may well read
    it. Rendering the second under the first's headline would print a false
    statement and then offer, as remedy, writing that false statement down as an
    exemption. A dict-of-dict per-zone table wired correctly into `_mapped_sets`
    is exactly that case, and it is the shape the worked example uses.
    """
    if not unwalked:
        return ""
    unmeasured = {
        q: why[len(_UNMEASURED):]
        for q, why in unwalked.items()
        if str(why).startswith(_UNMEASURED)
    }
    unwalked = {q: why for q, why in unwalked.items() if q not in unmeasured}
    lines: list[str] = []
    if unmeasured:
        lines += ["", "Entity description tables this guard could not MEASURE:", ""]
        for qname in sorted(unmeasured):
            module, _dot, attr = qname.rpartition(".")
            lines.append(
                f"    custom_components/addhon/{module}.py :: {attr}"
                f"   ({unmeasured[qname]})"
            )
        lines += [
            "",
            "This guard plants one extra description row in a clone of every",
            "table and looks for it in the tags `_mapped_sets` emits. It says",
            "nothing about whether the walk reads these -- it may well. Teach",
            "`_with_probe_row` the shape (about four lines per container kind)",
            "rather than exempting the table: an exemption here would record a",
            "promise about the dump that is probably false.",
        ]
    if not unwalked:
        return "\n".join(lines)
    lines += ["", "Entity description tables the diagnostics dump cannot see:", ""]
    for qname in sorted(unwalked):
        module, _dot, attr = qname.rpartition(".")
        lines.append(
            f"    custom_components/addhon/{module}.py :: {attr}"
            f"   ({unwalked[qname]})"
        )
    example = sorted(unwalked)[0]
    lines += [
        "",
        "`diagnostics._mapped_sets` never reads them, so on an appliance that has",
        "these entities the dump makes two statements the reader can see",
        "contradict each other, a few lines apart: `entities.by_domain` lists the",
        "entity, and then `coverage` reports the parameter it reads and writes as",
        "unmapped, with no row in `entities.sources` to disprove it. The walk and",
        "the test-side harvest are both whitelists, which is why a table neither",
        "was told about is invisible to both.",
        "",
        "Do ONE of these:",
        "",
        "1. Have the walk read it. Add the name to the lazy-import block in",
        "   `_mapped_sets` (custom_components/addhon/diagnostics.py), give it a",
        "   loop that puts its parameters into `mapped_attrs` and/or",
        "   `mapped_params` and writes an `entities.sources` row, and add it to",
        "   `_static_parameter_names` in this file -- the drift guard over the",
        "   emitted names, which fails until it is told about the new table.",
        "",
        "2. If the table genuinely contributes no parameter the dump should",
        "   account for, record why, next to this test:",
        "",
        "       _TABLES_OUTSIDE_COVERAGE = {",
        f'           "{example}":',
        '               "<what the dump prints for these parameters instead,',
        '                and why that is right>",',
        "       }",
        "",
        "   'It is new', 'it is internal' and 'it is experimental' are not",
        "   reasons: a dump is read by whoever is debugging the appliance, and",
        "   none of those three changes what it says about the parameter.",
    ]
    return "\n".join(lines)


class DescriptionTableCompletenessTest(unittest.TestCase):
    """Every platform description table is either walked, or excused in writing.

    `_mapped_sets` names twelve description tables in an explicit import list of
    fourteen names -- the other two, `AP_ENTITY_PARAMS` and
    `STARTPROGRAM_COMMAND`, are a set of parameter names and a string, not
    tables -- and `_static_parameter_names` re-lists the same twelve by hand.
    Both are
    whitelists, so neither can notice a table nobody added to it: a contributor
    who adds `_REF_ZONE_SELECTS` to select.py gets a green suite and a dump that
    accuses the integration of not mapping the parameter while `by_domain` lists
    the entity two sections above -- the dump lying by omission, silently, on the
    one section a maintainer reads to decide whether to write an entity that
    already exists.

    The two whitelists do police EACH OTHER in one direction: a table added to
    the walk but not to the harvest fails
    `test_every_emitted_name_is_a_static_table_field`, because the walk starts
    emitting names the harvest has never heard of. The direction neither covers
    is a table added to NEITHER, and that is the direction this class measures.
    """

    def test_no_other_module_grew_a_description_table(self) -> None:
        # `_TABLE_MODULES` is itself a whitelist; this is what stops it being one
        # more thing to remember. Source, not imports, so a module the stubs here
        # cannot load is still checked.
        declared = _modules_declaring_descriptions()
        scanned = set(_TABLE_MODULES)
        self.assertEqual(
            scanned,
            declared,
            "\nModules that construct entity descriptions: "
            f"{sorted(declared)}\nModules this guard checks:              "
            f"{sorted(scanned)}\n"
            "\nA module in the first list and not the second has description "
            "tables nothing compares against the diagnostics walk: add its name "
            "to `_TABLE_MODULES` above (and whatever stub it needs to "
            "`_install_stubs`). A module in the second and not the first no "
            "longer has any: drop it, so this guard keeps meaning what it says.",
        )

    def test_every_description_table_reaches_the_dump(self) -> None:
        unwalked = _unwalked_description_tables()
        surprises = {
            qname: why
            for qname, why in unwalked.items()
            if qname not in _TABLES_OUTSIDE_COVERAGE
        }
        self.assertEqual({}, surprises, _completeness_failure(surprises))

    def test_every_exemption_is_still_needed(self) -> None:
        # An exemption that has stopped being true is worse than none: it is a
        # written promise about a dump nobody re-reads. The day the table is
        # deleted, or wired into the walk, this fails and the entry goes.
        #
        # Skipped rather than silently green while there is nothing to check.
        # With an empty whitelist the loop below never runs and NEITHER
        # assertion is ever evaluated, so the suite reported an enforced rule
        # that had never executed -- and still paid `_unwalked_description_tables()`,
        # which plants probes over 62 module globals and runs `_mapped_sets` 17
        # times, purely to discard the result. The skip is what arms this the
        # day an entry is added.
        if not _TABLES_OUTSIDE_COVERAGE:
            self.skipTest(
                "`_TABLES_OUTSIDE_COVERAGE` is empty: no exemption to re-check."
            )
        unwalked = _unwalked_description_tables()
        for qname, reason in _TABLES_OUTSIDE_COVERAGE.items():
            self.assertIn(
                qname,
                unwalked,
                f"`{qname}` is exempted from the coverage walk but is now either "
                "walked or gone. Delete the entry from `_TABLES_OUTSIDE_COVERAGE`.",
            )
            self.assertGreaterEqual(
                len(reason.split()),
                8,
                f"`{qname}`'s exemption is not a reason: {reason!r}. It has to say "
                "what the dump prints for these parameters instead and why that "
                "is right, in a sentence the next reader can check.",
            )

    def test_the_guard_notices_a_table_nobody_walks(self) -> None:
        # A completeness guard that cannot fail is a completeness guard that is
        # not there. `select` is the module the worked example uses, and the
        # table is planted on the module OBJECT rather than in the file so this
        # proves the measurement rather than a fixture. The name is one no
        # platform would ever use, and it is restored through `_ABSENT` rather
        # than deleted, so that planting it on a module that later grows a real
        # attribute of the same name cannot delete the real one.
        from custom_components.addhon import select as select_module

        name = "_COMPLETENESS_SELF_TEST_TABLE"
        planted = (
            dataclasses.replace(
                select_module._PROGRAM_OPTION_SELECTS[0],
                key="zone_temperature",
                param="tempSelZ9",
            ),
        )
        before = _unwalked_description_tables()
        previous = getattr(select_module, name, _ABSENT)
        setattr(select_module, name, planted)
        try:
            unwalked = _unwalked_description_tables()
        finally:
            if previous is _ABSENT:
                delattr(select_module, name)
            else:
                setattr(select_module, name, previous)
        # Differential, not absolute: the one thing that changed is the table
        # this test invented. Asserting an empty result instead would turn one
        # real unwalked table into TWO failures, the second of them accusing
        # this test of leaking when it had not.
        self.assertEqual({f"select.{name}"}, set(unwalked) - set(before))
        message = _completeness_failure(unwalked)
        self.assertIn(f"select.py :: {name}", message)
        self.assertIn("_static_parameter_names", message)
        self.assertIn("_TABLES_OUTSIDE_COVERAGE", message)
        self.assertEqual(
            before, _unwalked_description_tables(), "the plant leaked past this test"
        )


# --- step 3: the per-parameter cloud instants (P1) ---------------------------


class _Raising:
    """A shadow value whose `last_update` PROPERTY raises when it is read.

    Not a contrived shape: `last_update` is a property on the real
    `HonAttribute`, and a foreign or half-migrated appliance implementation is
    free to compute it. `_appliance_block` is called with no try/except around
    it from either entry point, so this is the object that decides whether one
    bad parameter costs one row or the whole document.
    """

    def __init__(self):
        self.value = "1"

    @property
    def last_update(self):
        raise RuntimeError("boom")


class _BrokenNested(Mapping):
    """A Mapping that is NOT a dict and refuses one of the keys it enumerates.

    Shaped like what `hon_client.py:192-196` would see, but NO producer builds
    one: `_attribute_timestamps`' docstring traces every write of that key and
    finds a plain `dict` or a non-Mapping, and `dict()` refuses neither. So
    this is a witness that the guards degrade instead of raising, not evidence
    that the degraded flatten happens in the field. See
    `TheNestedParametersContainerIsAlwaysAPlainDictTest` at the end of this
    file, which fails if that stops being true.
    """

    def __init__(self, rows, broken):
        self._rows = dict(rows)
        self._broken = broken

    def __getitem__(self, key):
        if key == self._broken:
            raise RuntimeError("boom")
        return self._rows[key]

    def __iter__(self):
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)


def _stamp_block(attributes, app_type="AC"):
    """A single appliance block built from an ad-hoc attributes mapping.

    Built here rather than by bolting keys onto `_build_coordinator`: that
    fixture's AC and WD attribute dicts are hand-counted by
    `test_coverage_totals` (`attributes_total == 6`), so a section that needs an
    extra key builds its own appliance instead of perturbing the shared one.
    """
    return diagnostics._appliance_block(
        "id1",
        {
            "appliance": FakeApplianceNoModel(commands={}),
            "type": app_type,
            "attributes": attributes,
            "statistics": {},
        },
    )


class AttributeTimestampTest(unittest.TestCase):
    """The per-parameter cloud instants `_jsonable` used to throw away."""

    def test_the_map_sits_beside_the_values_it_describes(self):
        # Placement is the feature: a reader scrolling `attributes` finds the
        # instants in the next section, not after `future_capabilities`.
        _, blocks = _entry_diag()
        keys = list(blocks["AC"])
        self.assertEqual(
            keys.index("attributes") + 1, keys.index("attributes_last_update")
        )
        self.assertEqual(
            keys.index("attributes_last_update") + 1, keys.index("commands")
        )

    def test_every_shadow_parameter_gets_a_row(self):
        _, blocks = _entry_diag()
        self.assertEqual(
            {
                "machMode": None,
                "tempIndoor": "2026-04-09T05:31:02+00:00",
                "tempSel": "2026-04-09T05:34:16+00:00",
            },
            blocks["AC"]["attributes_last_update"],
        )

    def test_a_value_with_no_last_update_surface_gets_no_row(self):
        # The map answers "which keys are shadow parameters", so a plain scalar,
        # a bare `.value` wrapper, an opaque object and an envelope dict must all
        # be absent -- not present with a null, which would claim they are shadow
        # parameters that never carried an instant.
        _, blocks = _entry_diag()
        rows = blocks["AC"]["attributes_last_update"]
        for name in (
            "weirdAcSensor",
            "available",
            "liveParam",
            "opaqueObj",
            "commandHistory",
            "macAddress",
        ):
            self.assertNotIn(name, rows)

    def test_a_shadow_parameter_with_no_instant_is_null_not_missing(self):
        # machMode is wrapped and carries no instant. Dropping the row would
        # merge "no parseable instant has ever arrived" into "not a shadow
        # parameter", which are two different findings.
        _, blocks = _entry_diag()
        rows = blocks["AC"]["attributes_last_update"]
        self.assertIn("machMode", rows)
        self.assertIsNone(rows["machMode"])

    def test_an_appliance_with_no_shadow_wrappers_emits_an_empty_map(self):
        # The WD fixture is plain scalars throughout. An empty map is the honest
        # answer; it is not omitted, because "this device sent no instants" and
        # "this dump did not look" must stay distinguishable.
        _, blocks = _entry_diag()
        self.assertEqual({}, blocks["WD"]["attributes_last_update"])

    def test_the_rows_follow_the_map_they_are_read_beside(self):
        # The map is never read alone: "when did tempZ3 last move" is asked with
        # `attributes` already open at tempZ3. Sorting this one alphabetically
        # while the sibling keeps the cloud's merge order put the same name at
        # two unrelated ranks -- a mean 42.4 rows apart on the live WM, worst
        # case 100, with 66 backward jumps (diagnostics/live-2026-06-22).
        _, blocks = _entry_diag()
        block = blocks["AC"]
        rows = list(block["attributes_last_update"])
        self.assertEqual([k for k in block["attributes"] if k in set(rows)], rows)
        self.assertNotEqual(
            sorted(rows), rows, "an already-sorted fixture would make this vacuous"
        )

    def test_a_row_the_sibling_cannot_rank_falls_back_to_sorted(self):
        # `sorted` is still what orders the rows `attributes` does not enumerate:
        # the nested-only keys of the degraded merge path. Ordered ONCE and not
        # per source, so they land together AFTER everything the sibling ranks --
        # a map whose second half restarts the alphabet reads as a rendering bug,
        # and so would one that interleaved unrankable names at rank zero.
        mixed, _, _ = diagnostics._attribute_timestamps(
            {
                "zzz": FakeShadowAttribute("1", AC_STAMP_OLD),
                "mmm": FakeShadowAttribute("1", AC_STAMP_OLD),
                "parameters": {
                    "bbb": FakeShadowAttribute("1", AC_STAMP_NEW),
                    "aaa": FakeShadowAttribute("1", AC_STAMP_NEW),
                },
            }
        )
        self.assertEqual(["zzz", "mmm", "aaa", "bbb"], list(mixed))

    def test_a_sibling_that_cannot_be_ranked_costs_the_order_not_the_dump(self):
        # The `order` build is a SECOND enumeration of `attributes`, and the walk
        # above has already proved that a mapping which refuses to list its keys
        # is survivable -- its rows can come entirely from the nested sub-map. An
        # unguarded enumeration here would turn that survivable input into a dump
        # that never reaches the reporter.
        class _Unlistable(dict):
            def __iter__(self):
                raise RuntimeError("keys refused")

        source = _Unlistable()
        old = FakeShadowAttribute("1", AC_STAMP_OLD)
        new = FakeShadowAttribute("1", AC_STAMP_NEW)
        # The bare keys too, pointing at the SAME objects. That is the input the
        # guard is load-bearing on: `_attribute_values` proves the sub-map
        # redundant over `.items()`, which never calls `__iter__`, and hands
        # `_coverage` a plain dict, so the block really does reach the reporter.
        # Without them the fixture is killed by `_coverage`'s own unguarded
        # enumeration on HEAD too, and would prove the guard exists rather than
        # that it is worth anything.
        source["zzz"] = old
        source["aaa"] = new
        source["parameters"] = {"zzz": old, "aaa": new}
        rows, truncated, newest = diagnostics._attribute_timestamps(source)
        block = _stamp_block(source)
        self.assertEqual(["aaa", "zzz"], list(block["attributes_last_update"]))
        # Unranked, so the alphabetical fallback is what is emitted.
        self.assertEqual(["aaa", "zzz"], list(rows))
        self.assertFalse(truncated)
        self.assertEqual(AC_STAMP_NEW, newest)

    def test_every_stamp_is_aware_utc_iso_text(self):
        # The shape guard. `read`/`write`-style free text is not in `_TO_REDACT`
        # and `_MAC_RE` catches only MAC shapes, so the only thing keeping this
        # field safe is that it cannot be anything but an instant.
        result, _ = _entry_diag()
        seen = 0
        for block in result["appliances"]:
            for stamp in block["attributes_last_update"].values():
                if stamp is None:
                    continue
                seen += 1
                self.assertRegex(
                    stamp,
                    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?\+00:00$",
                )
        self.assertEqual(2, seen, "the guard would be vacuous with no stamps")

    def test_the_wrapped_fixture_still_reads_as_a_plain_value(self):
        # The wrappers must be invisible to every other section: `_jsonable`
        # unwraps `.value`, and the coverage denominator is a key count.
        _, blocks = _entry_diag()
        self.assertEqual(22.5, blocks["AC"]["attributes"]["tempIndoor"])
        self.assertEqual("1", blocks["AC"]["attributes"]["machMode"])
        self.assertEqual(6, blocks["AC"]["coverage"]["attributes_total"])


class AttributeTimestampTruncationTest(unittest.TestCase):
    """The row cap announces itself, and never silently drops a row."""

    @staticmethod
    def _many(count):
        rows = {
            "p%03d" % i: FakeShadowAttribute(str(i), AC_STAMP_OLD)
            for i in range(count)
        }
        # Alphabetically LAST, so the cap is guaranteed to drop it. That is what
        # makes the "the newest is computed over the dropped rows too" claim
        # below a real test rather than a restatement.
        rows["zzz_newest"] = FakeShadowAttribute("9", AC_STAMP_NEW)
        return rows

    def test_the_flag_is_absent_when_nothing_was_dropped(self):
        _, blocks = _entry_diag()
        self.assertNotIn("attributes_last_update_truncated", blocks["AC"])
        self.assertNotIn("attributes_last_update_truncated", blocks["WD"])

    def test_the_cap_drops_rows_and_says_so_adjacently(self):
        block = _stamp_block(self._many(250))
        self.assertEqual(200, len(block["attributes_last_update"]))
        self.assertTrue(block["attributes_last_update_truncated"])
        keys = list(block)
        self.assertEqual(
            keys.index("attributes_last_update") + 1,
            keys.index("attributes_last_update_truncated"),
        )

    def test_the_newest_is_computed_over_the_rows_the_cap_dropped(self):
        # The third element exists so a freshness header cannot report the
        # newest SURVIVING instant as the newest instant. Here the newest row is
        # provably not in the emitted map.
        rows = self._many(250)
        stamps, truncated, newest = diagnostics._attribute_timestamps(rows)
        self.assertTrue(truncated)
        self.assertNotIn("zzz_newest", stamps)
        self.assertEqual(AC_STAMP_NEW, newest)

    def test_exactly_at_the_cap_nothing_is_flagged(self):
        rows = {
            "p%03d" % i: FakeShadowAttribute("1", AC_STAMP_OLD)
            for i in range(diagnostics._STAMP_MAX_ROWS)
        }
        stamps, truncated, _ = diagnostics._attribute_timestamps(rows)
        self.assertEqual(diagnostics._STAMP_MAX_ROWS, len(stamps))
        self.assertFalse(truncated)

    def test_the_cap_clears_the_largest_real_device(self):
        # Anti-rot: the cap is a runaway guard, not a filter. The biggest shadow
        # this repository holds a dump from is the WM at 112 parameters
        # (diagnostics/live-2026-06-22/device-WM.json).
        self.assertGreater(diagnostics._STAMP_MAX_ROWS, 112)


class AttributeTimestampNewestTest(unittest.TestCase):
    """The third element: one pass, so nothing downstream can disagree with it."""

    def test_the_newest_is_the_maximum_not_the_last_seen(self):
        stamps, _, newest = diagnostics._attribute_timestamps(
            {
                "a": FakeShadowAttribute("1", AC_STAMP_NEW),
                "b": FakeShadowAttribute("1", AC_STAMP_OLD),
            }
        )
        self.assertEqual(AC_STAMP_NEW, newest)
        self.assertEqual(2, len(stamps))

    def test_the_newest_is_aware_utc_whatever_arrived(self):
        # It is returned for subtraction, so it has to be aware or the age that
        # uses it raises TypeError and takes the whole dump down.
        for value in (
            datetime(2026, 4, 9, 5, 34, 16),
            datetime(2026, 4, 9, 5, 34, 16, tzinfo=timezone(timedelta(hours=3))),
        ):
            _, _, newest = diagnostics._attribute_timestamps(
                {"a": FakeShadowAttribute("1", value)}
            )
            self.assertIsNotNone(newest)
            self.assertIsNotNone(newest.utcoffset())

    def test_the_newest_is_a_plain_datetime_whatever_arrived(self):
        # Rebuilt from the integer fields, so a hostile subclass cannot ride out
        # of this helper into whatever subtracts it next.
        class Hostile(datetime):
            def __sub__(self, other):
                raise RuntimeError("boom")

            def __rsub__(self, other):
                raise RuntimeError("boom")

        _, _, newest = diagnostics._attribute_timestamps(
            {"a": FakeShadowAttribute("1", Hostile(2026, 4, 9, tzinfo=timezone.utc))}
        )
        self.assertIs(type(newest), datetime)
        (FROZEN - newest).total_seconds()  # must not raise

    def test_a_subclass_that_refuses_to_compare_does_not_kill_the_dump(self):
        # A hostile `__gt__` is NOT what this guard catches: production rebuilds
        # the instant into a plain `datetime` from its integer fields one line
        # earlier, so by the comparison both operands are ordinary datetimes and
        # an overridden `__gt__` is never invoked (measured: zero calls). What
        # CAN raise inside the guard is the rebuild itself, so the subclass has
        # to be hostile on a field the rebuild reads.
        class Hostile(datetime):
            @property
            def year(self):
                raise RuntimeError("boom")

        stamps, _, newest = diagnostics._attribute_timestamps(
            {
                "a": FakeShadowAttribute("1", AC_STAMP_OLD),
                "b": FakeShadowAttribute(
                    "1", Hostile(2026, 4, 9, 6, 0, 0, tzinfo=timezone.utc)
                ),
            }
        )
        # The hostile value is skipped for the newest, not fatal ...
        self.assertEqual(AC_STAMP_OLD, newest)
        # ... and its own ROW still prints, because `_stamp_text` is a separate
        # path that never touches the comparison.
        self.assertEqual("2026-04-09T06:00:00+00:00", stamps["b"])
        self.assertEqual("2026-04-09T05:31:02+00:00", stamps["a"])

    def test_the_newest_is_none_when_no_instant_ever_arrived(self):
        stamps, _, newest = diagnostics._attribute_timestamps(
            {"a": FakeShadowAttribute("1", None)}
        )
        self.assertEqual({"a": None}, stamps)
        self.assertIsNone(newest)

    def test_a_shadow_stamped_only_with_the_epoch_sentinel_has_no_newest(self):
        # The route a real appliance actually takes. Every one of the 33
        # `lastUpdate` values in the only REF capture this repository holds is
        # exactly '1970-01-01T00:00:00.0Z', so a real fridge arrives with every
        # shadow parameter carrying a PARSED, aware, non-None datetime. The
        # `instant > _NEVER_STAMPED` clause is the only thing between that and a
        # freshness header reading "56 years stale" on a connected appliance.
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        stamps, _, newest = diagnostics._attribute_timestamps(
            {"a": FakeShadowAttribute("1", epoch), "b": FakeShadowAttribute("2", epoch)}
        )
        # The rows still PRINT what the cloud sent -- the map is an echo ...
        self.assertEqual(
            {"a": "1970-01-01T00:00:00+00:00", "b": "1970-01-01T00:00:00+00:00"},
            stamps,
        )
        # ... but the sentinel is not an instant, so no age is derivable from it.
        self.assertIsNone(newest)
        self.assertIsNone(diagnostics._freshness(None, {}, newest, FROZEN).get("shadow"))

    def test_the_newest_is_never_emitted_from_here(self):
        # `json.dumps` CANNOT see this on its own: `_appliance_block` returns
        # `_redact(block)`, and `_jsonable` stringifies any datetime it meets, so
        # a leaked instant arrives as '2026-04-09 05:31:02+00:00' -- the
        # space-separated shape `_ISO_RE` exists to reject -- without raising.
        # Assert the type and the shape, then keep the serialisation check.
        _, blocks = _entry_diag()

        def _leaves(node):
            if isinstance(node, dict):
                for value in node.values():
                    yield from _leaves(value)
            elif isinstance(node, (list, tuple)):
                for value in node:
                    yield from _leaves(value)
            else:
                yield node

        for stamp in blocks["AC"]["attributes_last_update"].values():
            if stamp is not None:
                self.assertIsNotNone(diagnostics._ISO_RE.fullmatch(stamp), stamp)
        for leaf in _leaves(blocks["AC"]):
            self.assertNotIsInstance(leaf, datetime)
        json.dumps(blocks["AC"])  # must not raise


class AttributeTimestampConversionTest(unittest.TestCase):
    """What reaches the map is `_stamp_text` output and nothing else."""

    def test_a_sub_minute_offset_survives_the_mac_mask(self):
        # End to end, through `_redact`. An aware instant is normalised to UTC
        # first, and that is CORRECTNESS: `_MAC_RE` reads '12:34:56-05:30:15' as
        # six octet groups and eats the field down to '2026-04-09T***'.
        odd = timezone(-timedelta(hours=5, minutes=30, seconds=15))
        block = _stamp_block(
            {"a": FakeShadowAttribute("1", datetime(2026, 4, 9, 12, 34, 56, tzinfo=odd))}
        )
        self.assertEqual(
            "2026-04-09T18:05:11+00:00", block["attributes_last_update"]["a"]
        )
        self.assertNotIn("***", json.dumps(block["attributes_last_update"]))

    def test_a_naive_instant_is_not_shifted_onto_the_host_zone(self):
        block = _stamp_block(
            {"a": FakeShadowAttribute("1", datetime(2020, 9, 29, 10, 1, 26))}
        )
        self.assertEqual("2020-09-29T10:01:26", block["attributes_last_update"]["a"])

    def test_a_tzinfo_whose_offset_is_none_is_treated_as_naive(self):
        # CPython does not raise here: it re-reads the instant in the HOST's zone
        # and labels it '+00:00'. A wrong time wearing an authoritative offset is
        # worse than a time with no offset at all.
        class NoOffset(tzinfo):
            def utcoffset(self, dt):
                return None

            def tzname(self, dt):
                return None

            def dst(self, dt):
                return None

        block = _stamp_block(
            {
                "a": FakeShadowAttribute(
                    "1", datetime(2020, 9, 29, 10, 1, 26, tzinfo=NoOffset())
                )
            }
        )
        self.assertEqual("2020-09-29T10:01:26", block["attributes_last_update"]["a"])

    def test_a_datetime_subclass_cannot_smuggle_a_string(self):
        # `isinstance(x, datetime)` admits subclasses and a subclass may return
        # anything from `isoformat`. The row survives as a null; the string does
        # not reach a document about to be attached to a public issue. This is
        # also the THIRD way a `null` row is reached, and the docstring says so.
        class Smuggler(datetime):
            def isoformat(self, *args, **kwargs):
                return "user@example.com"

        block = _stamp_block({"a": FakeShadowAttribute("1", Smuggler(2026, 4, 9))})
        self.assertEqual({"a": None}, block["attributes_last_update"])
        self.assertNotIn("user@example.com", json.dumps(block))

    def test_a_non_datetime_instant_is_a_null_row(self):
        # A cloud STRING instant is refused by design: the map's promise is that
        # every value in it was built by `datetime.isoformat`.
        block = _stamp_block(
            {
                "a": FakeShadowAttribute("1", "2026-04-09T05:34:16Z"),
                "b": FakeShadowAttribute("1", 1775712856741),
            }
        )
        self.assertEqual({"a": None, "b": None}, block["attributes_last_update"])


class AttributeTimestampEngineSemanticsTest(unittest.TestCase):
    """Pinned against the real `HonAttribute`, not against a fake of it."""

    @staticmethod
    def _attr(payload):
        # Imported inside the test on purpose: the point of these two cases is
        # that the map's `null` semantics match the ENGINE's, so a fake would
        # test nothing. Local, so the module's import block stays untouched.
        from custom_components.addhon.client.engine.attributes import HonAttribute

        return HonAttribute(payload)

    def test_a_stale_stamp_survives_a_stampless_update(self):
        # `attributes.py:73` is a walrus guard, so an update with no
        # `lastUpdate` at all leaves the previous instant standing. This is why
        # the docstring must NOT say a null means "the cloud stopped stamping
        # it", and why a stamp may legitimately be older than the value beside
        # it with nothing wrong anywhere.
        attr = self._attr({"parNewVal": "1", "lastUpdate": "2020-09-29 10:01:26"})
        attr.update({"parNewVal": "2"})
        self.assertEqual("2", str(attr))
        stamps, _, _ = diagnostics._attribute_timestamps({"a": attr})
        self.assertEqual({"a": "2020-09-29T10:01:26"}, stamps)

    def test_an_unparseable_instant_resets_the_row_to_null(self):
        # The other half of the same guard (`attributes.py:75-77`): a
        # present-but-unparseable value is the ONLY thing that clears an instant.
        attr = self._attr({"parNewVal": "1", "lastUpdate": "2020-09-29 10:01:26"})
        attr.update({"parNewVal": "2", "lastUpdate": "not-a-date"})
        stamps, _, newest = diagnostics._attribute_timestamps({"a": attr})
        self.assertEqual({"a": None}, stamps)
        self.assertIsNone(newest)

    def test_a_real_engine_attribute_produces_a_row(self):
        # Anti-vacuity for the two cases above: the engine class really does
        # expose the surface this section keys on.
        attr = self._attr({"parNewVal": "1", "lastUpdate": "2026-04-09T05:34:16"})
        stamps, _, newest = diagnostics._attribute_timestamps({"a": attr})
        self.assertEqual({"a": "2026-04-09T05:34:16"}, stamps)
        self.assertEqual(datetime(2026, 4, 9, 5, 34, 16, tzinfo=timezone.utc), newest)


class AttributeTimestampDegradationTest(unittest.TestCase):
    """A dump degrades; it never raises. `_appliance_block` has no net."""

    def test_a_raising_property_becomes_a_null_row_not_a_missing_one(self):
        stamps, truncated, newest = diagnostics._attribute_timestamps(
            {"boom": _Raising(), "ok": FakeShadowAttribute("1", AC_STAMP_NEW)}
        )
        self.assertEqual(
            {"boom": None, "ok": "2026-04-09T05:34:16+00:00"}, stamps
        )
        self.assertFalse(truncated)
        self.assertEqual(AC_STAMP_NEW, newest)

    def test_a_raising_property_does_not_break_the_dump(self):
        block = _stamp_block({"boom": _Raising()})
        self.assertEqual({"boom": None}, block["attributes_last_update"])

    def test_an_empty_mapping_is_an_empty_map(self):
        self.assertEqual(({}, False, None), diagnostics._attribute_timestamps({}))

    def test_a_non_string_key_still_produces_a_row(self):
        # The dump has to be JSON-native, and a non-string key would make HA's
        # encoder the place the failure surfaces.
        stamps, _, _ = diagnostics._attribute_timestamps(
            {7: FakeShadowAttribute("1", AC_STAMP_NEW)}
        )
        self.assertEqual({"7": "2026-04-09T05:34:16+00:00"}, stamps)
        json.dumps(stamps)  # must not raise

    def test_a_mapping_that_refuses_the_parameters_key_is_survived(self):
        # This helper makes the FIRST read of `attributes` in the block, so an
        # unguarded `.get` here would take the dump down ahead of `_coverage`,
        # which reads the same object a few lines below. `Mapping.get` only
        # swallows KeyError, so a `__getitem__` raising anything else escapes it.
        nested = _BrokenNested(
            {"tempZ1": FakeShadowAttribute("4", AC_STAMP_OLD)}, broken="parameters"
        )
        self.assertEqual(
            ({"tempZ1": "2026-04-09T05:31:02+00:00"}, False, AC_STAMP_OLD),
            diagnostics._attribute_timestamps(nested),
        )


class AttributeTimestampNestedSourceTest(unittest.TestCase):
    """The `parameters` sub-map, and why it is read at all."""

    def test_the_nested_map_is_the_fallback_when_nothing_was_flattened(self):
        # The fallback works. It is also inert against every producer this
        # repository has: no write of `attributes["parameters"]` can yield a
        # Mapping that `dict()` refuses (traced in `_attribute_timestamps`'
        # docstring). So this pins a guard kept as insurance against a future
        # producer, not a path a user can reach today.
        nested = _BrokenNested(
            {
                "tempZ1": FakeShadowAttribute("4", AC_STAMP_OLD),
                "tempZ3": FakeShadowAttribute("-43", AC_STAMP_NEW),
            },
            broken=None,
        )
        stamps, _, newest = diagnostics._attribute_timestamps({"parameters": nested})
        self.assertEqual(
            {
                "tempZ1": "2026-04-09T05:31:02+00:00",
                "tempZ3": "2026-04-09T05:34:16+00:00",
            },
            stamps,
        )
        self.assertEqual(AC_STAMP_NEW, newest)

    def test_a_bare_row_wins_over_the_nested_copy(self):
        # On a healthy device the two agree, so this only matters if they ever
        # stop agreeing: the bare value is what `attributes` prints, so it is
        # the one the instants must describe.
        bare = FakeShadowAttribute("1", AC_STAMP_NEW)
        shadow = FakeShadowAttribute("1", AC_STAMP_OLD)
        stamps, _, _ = diagnostics._attribute_timestamps(
            {"tempZ1": bare, "parameters": {"tempZ1": shadow}}
        )
        self.assertEqual({"tempZ1": "2026-04-09T05:34:16+00:00"}, stamps)

    def test_the_nested_key_itself_never_becomes_a_row(self):
        stamps, _, _ = diagnostics._attribute_timestamps(
            {"parameters": {"tempZ1": FakeShadowAttribute("1", AC_STAMP_NEW)}}
        )
        self.assertNotIn("parameters", stamps)

    def test_a_non_mapping_parameters_value_is_not_walked(self):
        # Deliberate: this runs on the event loop and `len()` is the only bound
        # available before iterating, so an iterator under that key is left
        # alone rather than consumed to rescue a degraded corner. Measured: with
        # the `isinstance(nested, Mapping)` guard relaxed to `hasattr(nested,
        # "__iter__")`, an endless iterator here hangs the test run forever,
        # which on the event loop is a Home Assistant that stops responding.
        #
        # The witness is a FLAG and a finite generator rather than an endless
        # one, on purpose: a test that proves its point by hanging is a test
        # that hangs CI instead of reporting.
        walked = []

        def _rows():
            walked.append(True)
            yield ("x", FakeShadowAttribute("1", AC_STAMP_NEW))

        stamps, _, _ = diagnostics._attribute_timestamps({"parameters": _rows()})
        self.assertEqual({}, stamps)
        self.assertEqual([], walked, "the iterator was consumed")
        self.assertEqual({}, diagnostics._attribute_timestamps({"parameters": []})[0])

    def test_one_refusing_key_costs_one_row_not_the_section(self):
        # Scope note, verified on HEAD: this is asserted on the helper, not on a
        # whole block, because a Mapping like this one already takes the entire
        # dump down at `_redact(block)` -- which walks it with `.items()` and has
        # no guard of its own. That exposure predates this section and is not
        # fixed here; what IS fixed is that the new section is not the thing
        # that dies, and that the instants it can read still get through.
        nested = _BrokenNested(
            {
                "bad": FakeShadowAttribute("1", AC_STAMP_OLD),
                "good": FakeShadowAttribute("1", AC_STAMP_NEW),
            },
            broken="bad",
        )
        stamps, _, newest = diagnostics._attribute_timestamps({"parameters": nested})
        self.assertEqual({"good": "2026-04-09T05:34:16+00:00"}, stamps)
        self.assertEqual(AC_STAMP_NEW, newest)


class AttributeTimestampPrivacyTest(unittest.TestCase):
    """The map adds no name and no value the dump did not already carry."""

    def test_no_identity_reaches_the_whole_dump(self):
        # The first whole-document identity scan outside `future_capabilities`
        # and the entity inventory. The instants are a new section reading a new
        # surface off cloud objects, so it is worth pinning at the document
        # level rather than field by field.
        result, _ = _entry_diag()
        encoded = json.dumps(result)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", encoded)
        self.assertNotIn("PLAINTEXT-SERIAL", encoded)
        self.assertNotIn("SN-PLAINTEXT", encoded)
        self.assertNotIn("phone-install-xyz", encoded)

    def test_a_stamp_under_a_redacted_key_name_is_still_redacted(self):
        # No re-keying: the row keeps the cloud's own parameter name, so
        # `_redact` -- which matches by EXACT key name -- still reaches it. A map
        # that renamed its rows to something generic would put them permanently
        # out of the redactor's reach, and that is the failure this pins.
        block = _stamp_block(
            {"serialNumber": FakeShadowAttribute("SN-X", AC_STAMP_NEW)}
        )
        self.assertEqual("***", block["attributes_last_update"]["serialNumber"])

    def test_the_row_names_are_names_the_attributes_section_already_prints(self):
        # The map introduces no new string into the document: on the bare path
        # its keys are keys of `attributes`, which is emitted verbatim one
        # section earlier, and on the nested-fallback path they are the keys of
        # the `parameters` sub-map printed one level deeper in that same
        # section. That is why the keys need no cap of their own.
        _, blocks = _entry_diag()
        for block in blocks.values():
            self.assertLessEqual(
                set(block["attributes_last_update"]), set(block["attributes"])
            )
        nested_block = _stamp_block(
            {"parameters": {"tempZ1": FakeShadowAttribute("4", AC_STAMP_OLD)}}
        )
        self.assertEqual(
            set(nested_block["attributes_last_update"]),
            set(nested_block["attributes"]["parameters"]),
        )

    def test_a_stamp_can_only_ever_be_an_instant(self):
        # The values are `_stamp_text` output, validated against `_ISO_RE`
        # before they are returned, so the row cannot carry free text however
        # the cloud object behaves.
        class Sneaky:
            value = "1"
            last_update = "AA:BB:CC:DD:EE:FF"

        block = _stamp_block({"a": Sneaky()})
        self.assertEqual({"a": None}, block["attributes_last_update"])


# --- step 4: the reduced `freshness` block (P2b) -----------------------------
#
# The clock seam, FROZEN and FROZEN_TEXT are step 1's and are REUSED, not
# redefined: a second `class _FrozenClock` further down this file would silently
# shadow the first and quietly stop patching what the earlier tests think it does.
#
# Every appliance below is built HERE. Nothing in this section touches
# `_build_coordinator`: its AC/WD attribute dicts are hand-counted by
# `test_coverage_totals` (`attributes_total == 6`), so bolting a `lastConnEvent`
# onto them to save a fixture would break an assertion three hundred lines away
# for a reason nobody reading it could guess.

# The pinned connection event, and the age that follows from it by hand:
# 07:09:40 (FROZEN) - 07:02:46 = 6m54s = 414s. The epoch spelling is the same
# instant to the millisecond, so a test can tell WHICH of the two fields the code
# read only by making them disagree on purpose (see the preference test).
CONN_STAMP_TEXT = "2026-08-13T07:02:46+00:00"
CONN_STAMP_EPOCH_MS = 1786604566000
CONN_STAMP_ISO = "2026-08-13T07:02:46Z"
CONN_AGE_S = 414
# An OLD instant used only to prove which field wins: years away from the one
# above, so a preference bug cannot hide inside a rounding difference.
OLD_STAMP_ISO = "2020-09-29T10:01:26Z"
OLD_STAMP_TEXT = "2020-09-29T10:01:26+00:00"

MAC_IN_THE_CLEAR = "3c:71:bf:bd:32:2c"


class FakeLinkAppliance:
    """An appliance exposing the engine's `connection` surface.

    `HonAppliance.connection` is a property backed by `_connection`, and the whole
    point of the `connected` field is to print what that property says, so the
    fixture has to have one rather than relying on a bare attribute lookup that
    happens to succeed.
    """

    def __init__(self, connection):
        self.commands = {}
        self.connection = connection


class FakeExplodingLinkAppliance:
    """An appliance whose `connection` property RAISES.

    Not a hypothetical: `connection` is a real property, `_appliance_block` is
    called with no try/except around it from either entry point, and the dumps
    that matter most are the ones taken while something is already broken.
    """

    def __init__(self):
        self.commands = {}

    @property
    def connection(self):
        raise RuntimeError("boom")


class ExplodingMapping(dict):
    """A Mapping whose `.get` raises, i.e. a hostile cloud envelope."""

    def get(self, *args, **kwargs):
        raise RuntimeError("boom")


def _conn_event(**overrides):
    """The live `lastConnEvent` shape, MAC sibling included.

    Verified against diagnostics/live-2026-06-22/device-REF.json: the envelope
    really does carry a `macAddress` next to the `category`, which is the reason
    `category` is type-guarded instead of stringified.
    """
    event = {
        "macAddress": "AA:BB:CC:DD:EE:FF",
        "category": "DISCONNECTED",
        "instantTime": CONN_STAMP_ISO,
        "timestampEvent": CONN_STAMP_EPOCH_MS,
    }
    event.update(overrides)
    return event


def _freshness_of(attributes, appliance=None, now=FROZEN, app_type="REF"):
    """A `freshness` section produced by the REAL block, redaction included.

    Deliberately not a direct `_freshness` call: the section has to survive
    `_redact` and `_jsonable` on the way out, and a helper that skipped them would
    pass while the shipped dump raised on a datetime.
    """
    block = diagnostics._appliance_block(
        "id1",
        {
            "appliance": appliance,
            "type": app_type,
            "attributes": attributes,
            "statistics": {},
        },
        None,
        now,
    )
    return block["freshness"]


def _json_native(value) -> bool:
    """True when `value` is something HA's JSON encoder will not raise on."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_json_native(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(k, str) and _json_native(v) for k, v in value.items()
        )
    return False


def _iter_stamps(section):
    """Every `at` string anywhere in a freshness section."""
    for value in section.values():
        if isinstance(value, dict) and isinstance(value.get("at"), str):
            yield value["at"]


class FreshnessPlacementTest(unittest.TestCase):
    """Where the section sits, which is most of what it buys."""

    def test_freshness_sits_between_the_identity_keys_and_the_model(self):
        # The section's whole claim is prominence: `available` was already in the
        # dump as one of sixty attribute keys. If it drifts below `attributes` it
        # stops answering the question before the values arrive and becomes a
        # duplicate of a scalar that was always there.
        _, blocks = _entry_diag()
        for app_type, block in blocks.items():
            keys = list(block)
            self.assertIn("freshness", keys, app_type)
            self.assertEqual("mac", keys[keys.index("freshness") - 1], app_type)
            self.assertEqual(
                "model_attributes", keys[keys.index("freshness") + 1], app_type
            )

    def test_every_appliance_block_carries_the_section(self):
        # Including the one with nothing to say. An absent section would read as
        # "this dump does not do freshness"; an empty-ish one reads as "nothing
        # here reported a connection state", which is the honest finding.
        _, blocks = _entry_diag()
        for app_type, block in blocks.items():
            self.assertIsInstance(block["freshness"], dict, app_type)

    def test_the_device_dump_carries_it_too(self):
        coord = _build_coordinator()
        hass = FakeHass(coord)
        device = FakeDevice(identifiers={(DOMAIN, AC_ID)})
        with _FrozenClock(FROZEN):
            result = _run(
                diagnostics.async_get_device_diagnostics(hass, FakeEntry(), device)
            )
        self.assertIn("freshness", result["appliance"])


class FreshnessShippedFieldsTest(unittest.TestCase):
    """The reduced shape, pinned so the dropped fields cannot creep back."""

    def _full_section(self):
        """Every field this block can emit, all four present at once.

        Asserting a key set against a thin fixture only proves what happened to be
        missing; this one has a connection state, an availability flag, a conn
        event and a shadow, so the assertions below are about what IS shipped.
        """
        return diagnostics._freshness(
            FakeLinkAppliance(False),
            {"available": False, "lastConnEvent": _conn_event()},
            AC_STAMP_NEW,
            FROZEN,
        )

    def test_the_dropped_connectivity_fields_never_appear(self):
        # `decided_by`, `realtime` and `realtime_ttl_s` were designed for this
        # block and dropped for three independent reasons, all of which make them
        # print a claim the same dump disproves a line later. The sharpest:
        # `mark_realtime_disconnected` sets connection False and CLEARS both
        # realtime marks while leaving the cached lastConnEvent at its last REST
        # value, so the mirror would print `connected: false` beside a CONNECTED
        # category and two empty marks -- every printed input saying online. This
        # test is here so reintroducing them is a deliberate act with a failing
        # test attached, not a merge that looked harmless.
        section = self._full_section()
        for dropped in ("decided_by", "realtime", "realtime_ttl_s"):
            self.assertNotIn(dropped, section)
        encoded = json.dumps(section)
        for dropped in ("decided_by", "realtime", "rest_connected"):
            self.assertNotIn(dropped, encoded)

    def test_poll_is_absent_and_that_is_deliberate(self):
        # There is no honest source for it from inside `_appliance_block`. The
        # coordinator entry carries no fetch instant, the coordinator itself is not
        # passed in and must not be, and the engine's `_last_update` is private,
        # NAIVE and in the host's zone (so read as UTC it prints a negative age for
        # every reporter east of Greenwich), and only advances on the HTTP path, so
        # on a realtime snapshot it would call the freshest data the stalest.
        self.assertNotIn("poll", self._full_section())

    def test_the_key_set_and_its_order_are_exactly_the_shipped_ones(self):
        full = self._full_section()
        self.assertEqual(
            ["connected", "available", "last_conn_event", "shadow"], list(full)
        )
        self.assertEqual(["category", "at", "age_s"], list(full["last_conn_event"]))
        self.assertEqual(["at", "age_s"], list(full["shadow"]))
        # `_full_section` calls `_freshness` DIRECTLY, so the order it measures is
        # the one the helper returns, not the one the document prints. Order is
        # decided by the LAST writer of the dict -- the block assembly and
        # `_redact` -- so assert it again on the shipped path or a reordering
        # introduced downstream leaves this test green.
        shipped = _freshness_of(
            {"available": False, "lastConnEvent": _conn_event()},
            FakeLinkAppliance(False),
        )
        self.assertEqual(["connected", "available", "last_conn_event"], list(shipped))
        self.assertEqual(["category", "at", "age_s"], list(shipped["last_conn_event"]))
        _, blocks = _entry_diag()
        self.assertEqual("shadow", list(blocks["AC"]["freshness"])[-1])

    def test_a_thin_appliance_emits_a_subset_and_never_a_new_name(self):
        # The conditional keys drop out; nothing else appears in their place.
        thin = _freshness_of({})
        self.assertLessEqual(
            set(thin), {"connected", "available", "last_conn_event", "shadow"}
        )


class FreshnessConnectivityTest(unittest.TestCase):
    """`connected` and `available`, guarded as booleans or reported as nothing."""

    def test_a_disconnected_appliance_says_so_at_the_top_of_its_block(self):
        # The one thing P2 was chartered to deliver, and it survives the reduction
        # intact: "this dump was taken while the appliance was offline".
        section = _freshness_of(
            {"available": False, "lastConnEvent": _conn_event()},
            FakeLinkAppliance(False),
        )
        self.assertIs(False, section["connected"])
        self.assertIs(False, section["available"])
        self.assertEqual("DISCONNECTED", section["last_conn_event"]["category"])

    def test_a_connected_appliance_says_that_too(self):
        section = _freshness_of({"available": True}, FakeLinkAppliance(True))
        self.assertIs(True, section["connected"])
        self.assertIs(True, section["available"])

    def test_a_non_boolean_connection_is_reported_as_null(self):
        # The engine writes real bools into both surfaces. A "true" or a 1 arriving
        # here means something upstream is no longer what this section thinks it
        # is, and printing it anyway would launder that into an unquestionable fact.
        for junk in ("true", 1, 0, "", [], {"a": 1}):
            with self.subTest(junk=junk):
                section = _freshness_of({}, FakeLinkAppliance(junk))
                self.assertIsNone(section["connected"])

    def test_an_appliance_with_no_connection_surface_gives_null(self):
        section = _freshness_of({}, FakeApplianceNoModel(commands={}))
        self.assertIsNone(section["connected"])

    def test_a_raising_connection_property_does_not_break_the_dump(self):
        # `_appliance_block` has no try/except around it from either entry point,
        # so an unguarded read here does not produce a wrong field, it produces no
        # dump at all.
        section = _freshness_of({"available": True}, FakeExplodingLinkAppliance())
        self.assertIsNone(section["connected"])
        self.assertIs(True, section["available"])

    def test_available_is_read_as_a_boolean_or_not_at_all(self):
        self.assertIsNone(_freshness_of({})["available"])
        self.assertIsNone(_freshness_of({"available": "false"})["available"])
        self.assertIsNone(_freshness_of({"available": FakeWrapper(True)})["available"])
        self.assertIs(False, _freshness_of({"available": False})["available"])


class FreshnessConnEventTest(unittest.TestCase):
    """The `last_conn_event` row: what the cloud last said, and how long ago."""

    def test_the_row_carries_category_at_and_age(self):
        row = _freshness_of({"lastConnEvent": _conn_event()})["last_conn_event"]
        self.assertEqual(
            {"category": "DISCONNECTED", "at": CONN_STAMP_TEXT, "age_s": CONN_AGE_S},
            row,
        )

    def test_the_epoch_field_is_preferred_over_the_iso_string(self):
        # Not a coin toss: it is the same order, on the same two keys, that
        # `HonAppliance.load_attributes` uses when deciding whether a REST
        # disconnect is newer than the last realtime traffic. A dump that quietly
        # preferred the other key would, on a payload where the two disagree,
        # explain a decision the engine did not make. Made observable by pointing
        # the two fields at instants six years apart.
        row = _freshness_of(
            {"lastConnEvent": _conn_event(instantTime=OLD_STAMP_ISO)}
        )["last_conn_event"]
        self.assertEqual(CONN_STAMP_TEXT, row["at"])
        self.assertNotEqual(OLD_STAMP_TEXT, row["at"])

    def test_an_unparseable_epoch_falls_back_to_the_iso_string(self):
        # The engine falls back on PARSE failure, not on absence; mirrored here.
        row = _freshness_of(
            {"lastConnEvent": _conn_event(timestampEvent="not-a-time")}
        )["last_conn_event"]
        self.assertEqual(CONN_STAMP_TEXT, row["at"])

    def test_a_null_epoch_falls_back_to_the_iso_string(self):
        row = _freshness_of(
            {"lastConnEvent": _conn_event(timestampEvent=None)}
        )["last_conn_event"]
        self.assertEqual(CONN_STAMP_TEXT, row["at"])

    def test_a_row_with_no_usable_instant_still_reports_its_category(self):
        # `at` and `age_s` appear together or not at all: an `at` with no age makes
        # the reader do the arithmetic this section exists to have already done,
        # and an age with no `at` is a number with nothing to check it against.
        row = _freshness_of(
            {
                "lastConnEvent": _conn_event(
                    timestampEvent="garbage", instantTime="also-garbage"
                )
            }
        )["last_conn_event"]
        self.assertEqual({"category": "DISCONNECTED"}, row)

    def test_the_shared_fixture_has_no_conn_event_and_still_builds(self):
        # ABSENT, not present-and-null. Missing means the appliance carries no such
        # envelope; present with a null category means it carries one and the field
        # inside was not usable text. Folding them together would report "never
        # reported a connection event" and "malformed payload" as one finding.
        _, blocks = _entry_diag()
        for app_type, block in blocks.items():
            self.assertNotIn("last_conn_event", block["freshness"], app_type)

    def test_a_non_mapping_conn_event_is_ignored(self):
        for junk in ("DISCONNECTED", 1, ["DISCONNECTED"], None):
            with self.subTest(junk=junk):
                self.assertNotIn(
                    "last_conn_event", _freshness_of({"lastConnEvent": junk})
                )

    def test_a_raising_conn_event_mapping_does_not_break_the_dump(self):
        # A cloud-supplied Mapping is free to have a `.get` that raises, and from
        # here that would abort the whole dump rather than one row of it.
        row = _freshness_of({"lastConnEvent": ExplodingMapping()})["last_conn_event"]
        self.assertEqual({"category": None}, row)

    def test_a_raising_attributes_mapping_does_not_break_the_dump(self):
        # The same shape one level up. `_coverage` reaches `.get` only while it
        # still has unmapped bare keys to classify, so on an appliance whose bare
        # keys are all mapped this section makes the FIRST such call in the block
        # and an unguarded read here would be a brand new way to lose the dump.
        self.assertEqual(
            {"connected": None, "available": None},
            _freshness_of(ExplodingMapping({"tempIndoor": 1}), app_type="AC"),
        )

    def test_a_non_string_category_is_reported_as_null(self):
        for junk in (1, True, None, ["DISCONNECTED"], FakeWrapper("DISCONNECTED")):
            with self.subTest(junk=junk):
                row = _freshness_of({"lastConnEvent": _conn_event(category=junk)})
                self.assertIsNone(row["last_conn_event"]["category"])

    def test_an_empty_category_reads_as_null_and_the_docstring_says_why(self):
        # An empty string IS a string, and `_bounded_text` refuses it along with
        # every other empty scalar -- so this prints null where the ENGINE saw a
        # decision (`lce.get("category", "") != "DISCONNECTED"` is True for "").
        # Pinned rather than fixed: `connected` on the line above already carries
        # the finding, and a bare "" in a dump reads as a rendering bug.
        row = _freshness_of({"lastConnEvent": _conn_event(category="")})
        self.assertIsNone(row["last_conn_event"]["category"])
        # A category that is only whitespace is not empty and survives, which is
        # what makes the line above a statement about `or None` and not about str.
        spaced = _freshness_of({"lastConnEvent": _conn_event(category="   ")})
        self.assertEqual("   ", spaced["last_conn_event"]["category"])


class FreshnessShadowAgeTest(unittest.TestCase):
    """`shadow`: the newest instant the cloud stamped on any parameter."""

    def test_the_shadow_is_the_newest_instant_the_cloud_stamped(self):
        # Pinned against the wrapped AC fixture: tempSel carries AC_STAMP_NEW,
        # tempIndoor the older one, machMode none at all. The age is derived from
        # the two pinned constants rather than typed out, so the assertion says
        # "the code did this subtraction" and not "the code returned 10892124".
        with _FrozenClock(FROZEN):
            _, blocks = _entry_diag()
        self.assertEqual(
            {
                "at": "2026-04-09T05:34:16+00:00",
                "age_s": int((FROZEN - AC_STAMP_NEW).total_seconds()),
            },
            blocks["AC"]["freshness"]["shadow"],
        )

    def test_the_shadow_matches_the_printed_timestamp_map(self):
        # The Part 2 requirement made checkable: the two sections are one pass, so
        # they cannot disagree. If a future edit re-derives the shadow from a
        # second walk, this is what catches it.
        with _FrozenClock(FROZEN):
            _, blocks = _entry_diag()
        block = blocks["AC"]
        printed = [v for v in block["attributes_last_update"].values() if v]
        self.assertTrue(printed, "the fixture would make this vacuous")
        self.assertEqual(max(printed), block["freshness"]["shadow"]["at"])

    def test_the_shadow_is_absent_when_no_instant_has_ever_arrived(self):
        # The WD fixture wraps nothing, so no parameter exposes a `last_update`.
        with _FrozenClock(FROZEN):
            _, blocks = _entry_diag()
        self.assertNotIn("shadow", blocks["WD"]["freshness"])

    def test_the_shadow_is_the_instant_it_was_HANDED_not_one_it_recomputes(self):
        # The invariant that survives a truncated map: `_freshness` is given the
        # newest instant by the same pass that built the printed rows, including
        # rows the row cap dropped. Handing it an instant that appears NOWHERE in
        # `attributes` proves it does not go looking for its own.
        section = diagnostics._freshness(
            None,
            {"tempZ1": FakeShadowAttribute("3", AC_STAMP_OLD)},
            AC_STAMP_NEW,
            FROZEN,
        )
        self.assertEqual("2026-04-09T05:34:16+00:00", section["shadow"]["at"])

    def test_a_naive_shadow_instant_is_read_as_utc_and_still_paired(self):
        # `at` and `age_s` appear together or not at all. Handing the section a
        # naive instant used to print an offset-less `at` beside a null age --
        # the exact shape this block refuses everywhere else -- so awareness is
        # decided here rather than trusted from the caller.
        section = diagnostics._freshness(
            None, {}, datetime(2026, 8, 13, 7, 0, 0), FROZEN
        )
        self.assertEqual(
            {"at": "2026-08-13T07:00:00+00:00", "age_s": 580}, section["shadow"]
        )

    def test_a_future_instant_gives_a_negative_age_rather_than_zero(self):
        # Not clamped, on purpose. The instants here are stamped by the CLOUD while
        # `now` is stamped by the reporter's host, so a negative age is the dump
        # saying the two clocks disagree -- itself a finding, and one that explains
        # connectivity decisions the engine makes by ordering those timestamps.
        ahead = FROZEN + timedelta(seconds=90)
        section = diagnostics._freshness(None, {}, ahead, FROZEN)
        self.assertEqual(-90, section["shadow"]["age_s"])

    def test_a_junk_newest_stamp_produces_no_shadow(self):
        for junk in ("2026-04-09T05:34:16Z", 1775712856741, object(), True):
            with self.subTest(junk=junk):
                self.assertNotIn(
                    "shadow", diagnostics._freshness(None, {}, junk, FROZEN)
                )

    def test_an_uncomputable_age_degrades_to_null(self):
        # The only reason `_age_seconds` carries a guard at all: both its operands
        # are aware UTC by construction, but a foreign datetime subclass with an
        # opinion about subtraction would otherwise take the whole dump down from
        # inside a field that is never more than a convenience.
        class HostileMoment:
            def __rsub__(self, other):
                raise RuntimeError("boom")

        self.assertIsNone(diagnostics._age_seconds(HostileMoment(), FROZEN))

    def test_an_age_is_a_whole_number_of_seconds(self):
        moment = FROZEN - timedelta(seconds=9, milliseconds=900)
        section = diagnostics._freshness(None, {}, moment, FROZEN)
        self.assertEqual(9, section["shadow"]["age_s"])
        self.assertIsInstance(section["shadow"]["age_s"], int)


class FreshnessPrivacyTest(unittest.TestCase):
    """Nothing identity-shaped may reach the section, by any route."""

    def test_the_conn_event_mac_never_reaches_the_section(self):
        section = _freshness_of({"lastConnEvent": _conn_event()})
        encoded = json.dumps(section)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", encoded)
        self.assertNotIn("macAddress", encoded)

    def test_a_mac_straddling_the_category_cap_is_masked(self):
        # THE regression the shared `_bounded_text` exists for, exercised through
        # THIS field: the address starts at offset 30 with the cap at 40, so a
        # helper that cut before masking would hand `_MAC_RE` ten characters of a
        # seventeen-character address -- no longer six groups, no longer a match --
        # and ship the remains to a public issue.
        category = "x" * 30 + MAC_IN_THE_CLEAR
        section = _freshness_of({"lastConnEvent": _conn_event(category=category)})
        self.assertEqual("x" * 30 + "***", section["last_conn_event"]["category"])
        # Not vacuous: cutting first really does leak on this exact input, and it
        # leaks the plan's measured fragment -- three and a half octets.
        self.assertEqual("x" * 30 + "3c:71:bf:b", category[:40])

    def test_a_category_that_is_the_envelope_itself_is_refused(self):
        # `str()` on the Mapping would flatten the sibling `macAddress` into one
        # string filed under `category`, where `_redact` -- which matches by exact
        # key NAME -- can never reach it again.
        event = _conn_event()
        event["category"] = dict(event)
        section = _freshness_of({"lastConnEvent": event})
        self.assertIsNone(section["last_conn_event"]["category"])
        encoded = json.dumps(section)
        self.assertNotIn("macAddress", encoded)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", encoded)

    def test_a_cloud_string_instant_is_never_echoed(self):
        # `instantTime` is cloud-chosen text. The only way it reaches the dump is
        # by being PARSED into a datetime and re-rendered from that datetime's own
        # fields, so a payload smuggled into the slot yields no `at` at all.
        section = _freshness_of(
            {
                "lastConnEvent": _conn_event(
                    timestampEvent=None, instantTime="user@example.com"
                )
            }
        )
        self.assertNotIn("at", section["last_conn_event"])
        self.assertNotIn("user@example.com", json.dumps(section))

    def test_the_category_is_bounded(self):
        section = _freshness_of({"lastConnEvent": _conn_event(category="z" * 400)})
        self.assertEqual(40, len(section["last_conn_event"]["category"]))

    def test_the_whole_section_carries_no_identity(self):
        # A whole-dump scan over the shared fixture, which carries a plaintext
        # serial, a plaintext MAC and a nested commandHistory, plus an ad-hoc
        # appliance whose conn event carries a MAC of its own.
        _, blocks = _entry_diag()
        ad_hoc = _freshness_of({"lastConnEvent": _conn_event()})
        # Anti-vacuity FIRST: a scan over a section that lost its conn event
        # passes for the wrong reason, which is how leak tests rot.
        self.assertEqual("DISCONNECTED", ad_hoc["last_conn_event"]["category"])
        sections = [json.dumps(b["freshness"]) for b in blocks.values()]
        sections.append(json.dumps(ad_hoc))
        for encoded in sections:
            self.assertNotIn("PLAINTEXT", encoded)
            self.assertNotIn("AA:BB:CC:DD:EE:FF", encoded)
            self.assertNotIn("11:22:33:44:55:66", encoded)
            self.assertNotIn("SN-", encoded)

    def test_an_emitted_instant_cannot_look_like_a_mac(self):
        # A sub-minute offset makes an ISO instant match `_MAC_RE`. Everything this
        # section emits goes through `_stamp_text`, which normalises to UTC first,
        # so the offset is always "+00:00" and the field survives the mask intact.
        for section in (
            _freshness_of({"lastConnEvent": _conn_event()}),
            diagnostics._freshness(None, {}, AC_STAMP_NEW, FROZEN),
        ):
            stamps = list(_iter_stamps(section))
            self.assertTrue(stamps, "the fixture would make this vacuous")
            for text in stamps:
                self.assertTrue(text.endswith("+00:00"), text)
                self.assertEqual(text, debug_utils._MAC_RE.sub("***", text))
        # Both fixtures above are ALREADY UTC, so neither can observe the
        # normalisation the comment credits. Straddle the hazard: a sub-minute
        # offset is what makes an ISO instant match `_MAC_RE`, and the contract
        # is that the instant is SHIFTED to UTC, not dropped.
        odd = timezone(timedelta(hours=-5, minutes=-30, seconds=-15))
        skewed = diagnostics._freshness(
            None, {}, datetime(2026, 4, 9, 12, 34, 56, tzinfo=odd), FROZEN
        )
        self.assertEqual("2026-04-09T18:05:11+00:00", skewed["shadow"]["at"])


class FreshnessDegradationTest(unittest.TestCase):
    """A dump degrades, never raises. `_appliance_block` has no net under it."""

    def test_a_foreign_appliance_object_still_produces_a_section(self):
        section = _freshness_of({}, object())
        self.assertEqual({"connected": None, "available": None}, section)

    def test_an_appliance_with_no_attributes_at_all_still_produces_a_section(self):
        block = diagnostics._appliance_block(
            "id1",
            {"appliance": None, "type": "REF", "attributes": None, "statistics": None},
            None,
            FROZEN,
        )
        self.assertEqual({"connected": None, "available": None}, block["freshness"])

    def test_the_section_survives_every_malformed_conn_event_shape(self):
        shapes = (
            {},
            {"category": None},
            {"category": {}},
            {"category": "OK", "timestampEvent": {}},
            {"category": "OK", "timestampEvent": [1]},
            {"category": "OK", "timestampEvent": True},
            {"category": "OK", "timestampEvent": 10 ** 400},
            {"category": "OK", "instantTime": ""},
            {"category": "OK", "instantTime": "2026-13-45T99:99:99Z"},
            {"instantTime": CONN_STAMP_ISO},
            ExplodingMapping(),
        )
        for shape in shapes:
            with self.subTest(shape=repr(shape)[:40]):
                section = _freshness_of({"lastConnEvent": shape})
                self.assertIsInstance(section["last_conn_event"], dict)
                self.assertTrue(_json_native(section))

    def test_a_raising_instant_read_does_not_discard_a_category_already_read(self):
        # Two guards rather than one: the category is read first and kept, so a
        # `.get` that only breaks on the instant keys costs the instant and not
        # the finding the reader actually came for.
        class HalfBroken(dict):
            def get(self, key, *args):
                if key == "category":
                    return "DISCONNECTED"
                raise RuntimeError("boom")

        row = _freshness_of({"lastConnEvent": HalfBroken()})["last_conn_event"]
        self.assertEqual({"category": "DISCONNECTED"}, row)

    def test_every_emitted_value_is_json_native(self):
        # HA's encoder raises on a datetime, and this is the one section of the
        # dump that handles datetimes at all.
        section = _freshness_of(
            {"available": False, "lastConnEvent": _conn_event()},
            FakeLinkAppliance(False),
        )
        self.assertTrue(_json_native(section))
        _, blocks = _entry_diag()
        for block in blocks.values():
            self.assertTrue(_json_native(block["freshness"]))
            json.dumps(block)  # must not raise
        # Everything above is observed AFTER `_redact`/`_jsonable`, which
        # stringify any datetime they meet -- so the defect this test is named
        # for is erased by the pipeline used to look for it. Assert it on the
        # UNREDACTED return value too, which is the only place it is visible.
        raw = diagnostics._freshness(
            FakeLinkAppliance(False),
            {"available": False, "lastConnEvent": _conn_event()},
            AC_STAMP_NEW,
            FROZEN,
        )
        self.assertTrue(_json_native(raw))

    def test_a_naive_dump_instant_never_reaches_a_subtraction(self):
        # The TypeError that would take the whole dump down. `_appliance_block`
        # normalises at its boundary, so a caller handing in a naive datetime gets
        # the same age as one handing in an aware one.
        naive = _freshness_of(
            {"lastConnEvent": _conn_event()},
            now=datetime(2026, 8, 13, 7, 9, 40),
        )
        self.assertEqual(CONN_AGE_S, naive["last_conn_event"]["age_s"])

    def test_a_block_built_with_no_instant_at_all_still_dates_its_ages(self):
        # The two-positional-argument call site: `now` falls back to the seam.
        with _FrozenClock(FROZEN):
            block = diagnostics._appliance_block(
                "id1",
                {
                    "appliance": None,
                    "type": "REF",
                    "attributes": {"lastConnEvent": _conn_event()},
                    "statistics": {},
                },
            )
        self.assertEqual(CONN_AGE_S, block["freshness"]["last_conn_event"]["age_s"])

    def test_an_unusable_cloud_parser_degrades_to_no_instant(self):
        # The lazy import mirrors `_mapped_sets`: on any import hiccup the row
        # loses its instant instead of the dump losing everything.
        with _BrokenModules("client.helpers"):
            row = _freshness_of({"lastConnEvent": _conn_event()})["last_conn_event"]
        self.assertEqual({"category": "DISCONNECTED"}, row)


# --- step 3b (optional): dropping the duplicated `parameters` sub-map --------


class AttributeValuesDedupTest(unittest.TestCase):
    """The nested `parameters` sub-map goes only when it is provably a copy."""

    def test_a_redundant_nested_map_is_dropped(self):
        shadow = FakeShadowAttribute("4", AC_STAMP_OLD)
        block = _stamp_block({"tempZ1": shadow, "parameters": {"tempZ1": shadow}})
        self.assertNotIn("parameters", block["attributes"])
        self.assertEqual("4", block["attributes"]["tempZ1"])
        # The one bit the sub-map carried -- which keys are shadow parameters --
        # is still in the document, now with the instant as well.
        self.assertEqual(
            {"tempZ1": "2026-04-09T05:31:02+00:00"},
            block["attributes_last_update"],
        )

    def test_coverage_and_the_echo_describe_the_same_mapping(self):
        # The dedupe runs above EVERY consumer, so `attributes_unmapped_meta`
        # cannot name a key the `attributes` section no longer prints. On all
        # four live appliances that key is exactly `parameters`.
        shadow = FakeShadowAttribute("4", AC_STAMP_OLD)
        block = _stamp_block({"tempZ1": shadow, "parameters": {"tempZ1": shadow}})
        self.assertNotIn(
            "parameters", block["coverage"]["attributes_unmapped_meta"]
        )
        for name in block["coverage"]["attributes_unmapped_meta"]:
            self.assertIn(name, block["attributes"])

    def test_a_nested_map_that_is_the_only_copy_is_kept(self):
        # A sub-map whose names are absent from the top level is kept because
        # it cannot be SHOWN to be duplication, and dropping it would delete
        # data. The reachable version of that input is a cloud payload whose
        # own top-level `parameters` replaced the shadow container; the
        # "`dict(params)` raised" story this comment used to tell is retracted
        # in `_attribute_values`' docstring.
        block = _stamp_block(
            {"parameters": {"tempZ1": FakeShadowAttribute("4", AC_STAMP_OLD)}}
        )
        self.assertEqual({"tempZ1": "4"}, block["attributes"]["parameters"])

    def test_a_none_valued_nested_row_is_not_mistaken_for_a_flattened_one(self):
        # `dict.get` answers None for "absent" and for "present and None" alike,
        # so the lookup uses the module sentinel instead. Nothing here was
        # flattened: the sub-map is the only copy and both names would otherwise
        # have vanished from the document entirely.
        block = _stamp_block({"parameters": {"tempZ1": None, "tempZ3": None}})
        self.assertIn("parameters", block["attributes"])

    def test_an_empty_nested_map_is_kept(self):
        # `all([])` is True. "The shadow container was empty" and "there is no
        # shadow container" are different statements and stay different.
        block = _stamp_block({"x": 1, "parameters": {}})
        self.assertEqual({}, block["attributes"]["parameters"])

    def test_a_partially_flattened_map_is_kept_whole(self):
        # One missing bare key is enough: the section never guesses which half
        # of a half-merged shadow is the real one.
        shadow = FakeShadowAttribute("4", AC_STAMP_OLD)
        other = FakeShadowAttribute("5", AC_STAMP_NEW)
        block = _stamp_block(
            {"tempZ1": shadow, "parameters": {"tempZ1": shadow, "tempZ3": other}}
        )
        self.assertIn("parameters", block["attributes"])

    def test_a_bare_key_rebound_to_another_object_keeps_the_map(self):
        # Identity, not equality: a bare key that no longer IS the nested object
        # means the merge this optimisation relies on did not happen the way it
        # is documented, so the copy stays. Equality would also mean calling an
        # `__eq__` this module does not control.
        #
        # The fixture has to be an object for which `is` and `==` DISAGREE.
        # `FakeShadowAttribute` inherits `object.__eq__`, so on it `==` degenerates
        # to `is` and the assertion below cannot tell the two operators apart.
        class _AlwaysEqual:
            def __init__(self, value):
                self.value = value
                self.last_update = AC_STAMP_OLD

            def __eq__(self, other):
                return True

            __hash__ = None

        block = _stamp_block(
            {
                "tempZ1": _AlwaysEqual("4"),
                "parameters": {"tempZ1": _AlwaysEqual("4")},
            }
        )
        self.assertIn("parameters", block["attributes"])

    def test_the_redundancy_check_never_calls_a_foreign_eq(self):
        # The other half of the docstring's reason for `is`: an `__eq__` this
        # module does not control is free to raise, and the dedupe must not be
        # the thing that costs the reporter the file.
        # The call is RECORDED, not inferred from the outcome. Inferring it
        # does not work: a check that consults `__eq__` and swallows the raise
        # as "equal" drops the map too, so the assertion below would pass with
        # the foreign `__eq__` entered on every key -- which is the whole thing
        # this test exists to forbid, and is also the exact kill the sibling
        # above already makes.
        calls = []

        class _HostileEq:
            def __init__(self, value):
                self.value = value
                self.last_update = AC_STAMP_OLD

            def __eq__(self, other):
                calls.append(other)
                raise RuntimeError("boom")

            __hash__ = None

        shared = _HostileEq("4")
        block = _stamp_block({"tempZ1": shared, "parameters": {"tempZ1": shared}})
        self.assertEqual([], calls)
        # Same object bare and nested -> redundant -> dropped.
        self.assertNotIn("parameters", block["attributes"])

    def test_the_live_merge_binds_the_same_object_bare_and_nested(self):
        # The cross-module premise the whole optimisation rests on, stated in the
        # production docstring and, until now, in no test. `_attribute_values`
        # decides redundancy with `is`; that is only ever True because
        # `_get_attributes` merges the shadow with `attributes.update(params)`,
        # binding the SAME objects bare and nested. Rewrite that merge to copy and
        # the dedupe silently stops firing on every real appliance.
        from custom_components.addhon.hon_client import _get_attributes

        shadow = FakeShadowAttribute("4", AC_STAMP_OLD)

        class _App:
            attributes = {"parameters": {"tempZ1": shadow}}

        merged = _get_attributes(_App())
        self.assertIs(shadow, merged["tempZ1"])
        self.assertIs(shadow, merged["parameters"]["tempZ1"])
        self.assertNotIn("parameters", diagnostics._attribute_values(merged))

    def test_a_non_mapping_parameters_value_is_left_alone(self):
        block = _stamp_block({"parameters": "not-a-map", "x": 1})
        self.assertEqual("not-a-map", block["attributes"]["parameters"])

    def test_a_truncated_instant_map_keeps_the_sub_map(self):
        # The justification for deleting the copy is that
        # `attributes_last_update` now carries the shadow-parameter set. Once
        # the row cap has fired it does not carry it for the dropped rows, so
        # the copy stays and the dump keeps saying which keys were shadow.
        shadows = {
            "p%03d" % i: FakeShadowAttribute(str(i), AC_STAMP_OLD)
            for i in range(250)
        }
        attributes = dict(shadows)
        attributes["parameters"] = shadows
        block = _stamp_block(attributes)
        self.assertTrue(block["attributes_last_update_truncated"])
        self.assertIn("parameters", block["attributes"])

    def test_an_uncomparable_nested_map_is_kept(self):
        # On the helper rather than on a whole block, and for a reason worth
        # recording: verified on HEAD, a Mapping whose `__getitem__` raises
        # already takes the entire dump down at `_redact(block)`, which walks it
        # with `.items()` and has no guard. This change neither creates that
        # exposure nor fixes it -- it only guarantees that a sub-map which
        # cannot be shown redundant is never deleted.
        nested = _BrokenNested({"a": FakeShadowAttribute("1", AC_STAMP_OLD)}, broken="a")
        kept = diagnostics._attribute_values({"parameters": nested})
        self.assertIs(nested, kept["parameters"])

    def test_an_appliance_with_no_nested_map_is_untouched(self):
        _, blocks = _entry_diag()
        self.assertEqual(6, blocks["AC"]["coverage"]["attributes_total"])
        self.assertEqual(22.5, blocks["AC"]["attributes"]["tempIndoor"])


class TheNestedParametersContainerIsAlwaysAPlainDictTest(unittest.TestCase):
    """The invariant the two nested-`parameters` docstrings now rest on.

    `_attribute_timestamps` walks that sub-map as a fallback source and
    `_attribute_values` refuses to delete it unless it is provably a copy; both
    now state that no producer can put a Mapping there that `dict()` would
    refuse, which is what makes the fallback inert. A docstring that says so
    goes stale in silence, so the claim is pinned here instead. It drives the
    REAL engine appliance over the REAL fridge capture on purpose: the claim is
    about what the engine builds, not about what a fake can be made to build.
    """

    _INFO = {
        "applianceTypeName": "REF",
        "applianceModelId": "10136",
        "macAddress": "11-22-33-44-55-66",
        "modelName": "HDPW5620CNPK",
        "brand": "haier",
        "nickName": "Frigo",
        "code": "ABC123",
        "serialNumber": "0123456789",
    }

    @staticmethod
    def _capture(name):
        path = REPO_ROOT / "tests" / "fixtures" / "ref_10136" / name
        return json.loads(path.read_text(encoding="utf-8"))

    def _appliance(self, app_type="REF", top_level=_ABSENT):
        from custom_components.addhon.client.engine.appliance import HonAppliance

        capture = self._capture

        class _Api:
            async def load_commands(self, appliance):
                return capture("commands.json")

            async def load_favourites(self, appliance):
                return []

            async def load_command_history(self, appliance):
                return capture("command_history.json")

            async def load_attributes(self, appliance):
                payload = capture("attributes.json")
                if top_level is not _ABSENT:
                    payload["parameters"] = top_level
                return payload

        appliance = HonAppliance(
            _Api(), dict(self._INFO, applianceTypeName=app_type)
        )
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(appliance.load_commands())
            try:
                loop.run_until_complete(appliance.load_attributes())
            except (KeyError, ValueError, IndexError, TypeError, AttributeError):
                # Exactly client/session.py::_APPLIANCE_BUILD_ERRORS, which KEEPS
                # the half-loaded appliance instead of dropping it -- so whatever
                # it is left holding still reaches a dump through
                # `build_realtime_snapshot`, and the invariant has to hold on the
                # failed path too, not only on the happy one.
                pass
        finally:
            loop.close()
        return appliance

    def test_the_engine_builds_the_shadow_container_as_a_plain_dict(self):
        # `client/engine/appliance.py:279` creates it with a literal `{}`. Not
        # `isinstance`: a dict SUBCLASS would pass that and still be the thing
        # the docstrings say cannot arrive, because `dict()` takes the
        # `PyDict_Merge` fast path on subclasses and would keep flattening.
        params = self._appliance().attributes["parameters"]
        self.assertIs(type(params), dict)

    def test_no_cloud_payload_can_put_a_refusing_mapping_under_parameters(self):
        # `client/engine/appliance.py:280` merges the context payload wholesale,
        # so the TYPE of this key is cloud-controlled -- but the payload comes
        # from `response.json()`, and JSON decodes to plain containers only.
        # Every shape it can produce is either exactly `dict` (which `dict()`
        # cannot refuse, so nothing degrades) or not a Mapping at all (which
        # both helpers decline to walk). AC is used because it has no per-type
        # layer in `client/engine/appliances/registry.py`, so nothing rejects
        # the value before it lands: it is the most permissive path there is.
        # The values come FROM a decode, not from literals. Written as literals
        # the six non-dict subcases could not fail whatever the engine did with
        # them -- `not isinstance(landed, Mapping)` was already true of the
        # tuple this test wrote, and the engine hands the key straight back by
        # identity -- so they measured the fixture and not the code. Decoding
        # them here pins the premise the claim actually rests on.
        for text in ('[]', '"abc"', '5', '1.5', 'true', 'null', '{}', '{"x": "1"}'):
            with self.subTest(text=text):
                decoded = json.loads(text)
                self.assertTrue(
                    type(decoded) is dict or not isinstance(decoded, Mapping),
                    f"json.loads produced a {type(decoded).__name__}",
                )
                landed = self._appliance("AC", decoded).attributes.get("parameters")
                self.assertTrue(
                    type(landed) is dict or not isinstance(landed, Mapping),
                    f"a {type(landed).__name__} reached the dead fallback",
                )


class _FailedCode:
    """The `error_codes` singleton `HonClient.last_error_code` holds after a failure."""

    label = "ADDHON-400"
    reason_en = "Validation failed"


class _FailedClient:
    """A client as `_store_setup_failure` finds it: the session already gone (so the
    live census reads None) and the snapshot `setup_sync` took in its place."""

    last_error_code = _FailedCode
    last_error_phase = "load_appliances/auth"
    last_phase_ledger = [{"phase": "auth", "seconds": 2.1, "outcome": "error"}]
    last_appliance_fetch = None
    last_setup_fetch = {"outcome": "raised", "code": "ADDHON-470", "status": None}
    setup_expanded = 0
    appliance_count = 0
    setup_drops = {}
    degraded_census = {}


def _addhon_init():
    """The integration package, which owns the writer half of the record."""
    return importlib.import_module("custom_components.addhon")


class SetupFailureRecordTest(unittest.TestCase):
    """A dump taken after a FAILED setup says why it failed.

    Until the record existed both failure branches of `async_setup_entry` popped the
    entry bucket whole, so `last_error` read `{"status": "client_absent"}` -- the same
    answer a dump gives while Home Assistant is still retrying -- and `last_fetch` read
    `client_absent` too. Everything built to explain a failed setup (the per-phase
    ledger of #76, the appliance-list census) was unreachable in the one dump a
    reporter can produce for it.
    """

    def _hass_with_record(self, record):
        hass = FakeHass(_build_coordinator())
        hass.data[DOMAIN]["e1"] = {"setup_failure": record}
        return hass

    def _dump_record(self, record):
        return _run(
            diagnostics.async_get_config_entry_diagnostics(
                self._hass_with_record(record), FakeEntry()
            )
        )

    def _record(self, **over):
        record = {
            "code": "ADDHON-400",
            "phase": "load_appliances/auth",
            "phase_ledger": [{"phase": "auth", "seconds": 2.1, "outcome": "error"}],
            "fetch": None,
            "at": datetime(2026, 8, 24, 9, 0, 0, tzinfo=timezone.utc),
        }
        record.update(over)
        return record

    # -- the writer -------------------------------------------------------------

    def test_a_failed_setup_replaces_the_bucket_instead_of_popping_it(self) -> None:
        # REPLACES, not merges: a coordinator and a client the setup could not finish
        # handing over must not survive it. The record is the only key left.
        init = _addhon_init()
        hass = FakeHass(_build_coordinator())
        hass.data[DOMAIN]["e1"]["client"] = _FailedClient()
        init._store_setup_failure(hass, FakeEntry(), _FailedClient())
        bucket = hass.data[DOMAIN]["e1"]
        self.assertEqual({"setup_failure"}, set(bucket))
        self.assertEqual("ADDHON-400", bucket["setup_failure"]["code"])
        self.assertEqual("load_appliances/auth", bucket["setup_failure"]["phase"])

    def test_the_record_stores_the_label_and_never_the_code_object(self) -> None:
        # The reason is resolved from the catalog at READ time, so the sentence in the
        # dump comes from error_codes.py in the running version. A record that carried
        # the object (or its text) would put a value from another process's catalog --
        # or from whatever a future writer decides to store -- into the document.
        init = _addhon_init()
        hass = FakeHass(_build_coordinator())
        init._store_setup_failure(hass, FakeEntry(), _FailedClient())
        record = hass.data[DOMAIN]["e1"]["setup_failure"]
        self.assertEqual("ADDHON-400", record["code"])
        self.assertNotIn("reason", record)
        self.assertIsInstance(record["at"], datetime)
        self.assertIsNotNone(record["at"].tzinfo)

    def test_the_record_falls_back_to_the_snapshot_when_the_session_is_gone(self) -> None:
        # THE reason `last_setup_fetch` exists. A failure raised inside setup_sync has
        # already closed the session there, so the live property reads None and the
        # census of the call would be gone -- with it, the only evidence of a POST that
        # never reached a body.
        init = _addhon_init()
        hass = FakeHass(_build_coordinator())
        init._store_setup_failure(hass, FakeEntry(), _FailedClient())
        fetch = hass.data[DOMAIN]["e1"]["setup_failure"]["fetch"]
        self.assertEqual("raised", fetch["outcome"])
        self.assertEqual("ADDHON-470", fetch["code"])
        # The four counters are flattened in beside it, so the reader has one source.
        for key in ("expanded", "built", "skipped", "degraded"):
            self.assertIn(key, fetch)

    def test_a_live_census_wins_over_the_snapshot(self) -> None:
        # A setup that failed AFTER the client came up (the first coordinator refresh
        # raising ConfigEntryNotReady) still has its session: the live census describes
        # the same call and is the fresher of the two.
        init = _addhon_init()
        client = _FailedClient()
        client.last_appliance_fetch = {"outcome": "ok", "count": 2}
        hass = FakeHass(_build_coordinator())
        init._store_setup_failure(hass, FakeEntry(), client)
        self.assertEqual(
            "ok", hass.data[DOMAIN]["e1"]["setup_failure"]["fetch"]["outcome"]
        )

    def test_a_raising_property_does_not_keep_the_setup_from_closing(self) -> None:
        # `getattr(x, name, default)` swallows a MISSING attribute, never an exception
        # raised INSIDE a property body -- and `setup_drops` ends in
        # `dict(self._setup_drops)` on a session setup may still be appending to from
        # the hOn loop thread (client/session.py:625-632, the same hazard
        # `_RaisingFetchClient` above stands in for). Unguarded, that raise propagates
        # out of the `except` handler in async_setup_entry, `_async_close_client` never
        # runs, and the dedicated loop thread and the aiohttp session leak.
        init = _addhon_init()

        class Hostile(_FailedClient):
            @property
            def setup_drops(self):
                raise RuntimeError("session torn down on the hOn loop thread")

        hass = FakeHass(_build_coordinator())
        hass.data[DOMAIN]["e1"]["client"] = object()
        init._store_setup_failure(hass, FakeEntry(), Hostile())
        # No raise -- and the bucket is still CLEARED, because a coordinator or a
        # client that outlived a failed setup is the one outcome worse than a missing
        # record. Empty, not half-filled: `last_error` then answers `client_absent`,
        # which is exactly true.
        self.assertEqual({}, hass.data[DOMAIN]["e1"])
        result = _run(
            diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry())
        )
        self.assertEqual({"status": "client_absent"}, result["last_error"])

    def test_removing_the_entry_drops_the_record(self) -> None:
        # The only hook that can: an entry whose setup never succeeded is never
        # unloaded, so without async_remove_entry the record outlives the entry and
        # keeps hass.data[DOMAIN] non-empty -- which is what async_unload_entry reads
        # to decide the global debug services can go.
        init = _addhon_init()
        hass = self._hass_with_record(self._record())
        _run(init.async_remove_entry(hass, FakeEntry()))
        self.assertNotIn("e1", hass.data[DOMAIN])

    def test_both_failure_branches_record_before_closing_the_client(self) -> None:
        # Source-level, because async_setup_entry is not behaviourally testable in this
        # harness (it runs the executor login, the first refresh and the platform
        # forwarding). Order is the whole point: _async_close_client nulls the session
        # `last_appliance_fetch` reads through, so a record built after it would have
        # lost the live census in exactly the case that has one.
        source = (
            Path(diagnostics.__file__).resolve().parent / "__init__.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        setup = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry"
        )
        handlers = [h for h in ast.walk(setup) if isinstance(h, ast.ExceptHandler)]
        recording = [
            h for h in handlers
            if any(
                isinstance(call.func, ast.Name) and call.func.id == "_store_setup_failure"
                for call in ast.walk(h) if isinstance(call, ast.Call)
            )
        ]
        self.assertEqual(2, len(recording), "both failure branches must record")
        for handler in recording:
            # By LINE, not by the order ast.walk happens to yield: walk is breadth
            # first, so an index into its output says nothing about which call runs
            # first, and an assertion built on it passes whichever way the two are
            # written. Verified by mutation: swapping the two statements leaves a
            # walk-order assertion green and fails this one.
            lines: dict[str, list[int]] = {"_store_setup_failure": [], "_async_close_client": []}
            for node in ast.walk(handler):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in lines:
                        lines[node.func.id].append(node.lineno)
            # max/min, not a single lineno each: ast.walk is breadth first, so a name
            # appearing twice in one handler would resolve to whichever the traversal
            # yielded last. This asserts the strongest reading -- EVERY record build
            # precedes EVERY close -- and cannot be satisfied by traversal luck.
            self.assertLess(
                max(lines["_store_setup_failure"]),
                min(lines["_async_close_client"]),
                "the record must be built while the session is still there",
            )
            # And the pop it replaced must be gone: a pop after the write would delete
            # the record, a pop before it would be dead code that reads as a live rule.
            self.assertNotIn(
                "pop",
                [
                    node.func.attr
                    for node in ast.walk(handler)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                ],
            )

    # -- the reader -------------------------------------------------------------

    def test_last_error_reports_the_failure_instead_of_client_absent(self) -> None:
        result = self._dump_record(self._record())["last_error"]
        self.assertEqual("setup_failed", result["status"])
        self.assertEqual("ADDHON-400", result["code"])
        self.assertEqual("2026-08-24T09:00:00+00:00", result["at"])
        self.assertEqual(
            [{"phase": "auth", "seconds": 2.1, "outcome": "error"}],
            result["phase_ledger"],
        )

    def test_the_reason_is_looked_up_and_never_read_from_the_record(self) -> None:
        # The record is written by us, but it has been through hass.data: a writer in a
        # future version (or a test double, or an older record left by an install that
        # has since been upgraded) is not a source this file publishes text from.
        from custom_components.addhon import error_codes as ec

        result = self._dump_record(
            self._record(reason="3C:71:BF:AA:BB:CC belongs to user@example.com")
        )["last_error"]
        self.assertEqual(ec.NETWORK_TIMEOUT.reason_en, result["reason"])
        self.assertNotIn("3C:71", json.dumps(result))

    def test_an_unknown_label_reports_no_reason_rather_than_a_guess(self) -> None:
        # 998 is registered by nothing (999 IS: it is UNKNOWN), so this is a label
        # whose shape passes and whose meaning this version does not have -- a record
        # written by a newer install, read after a downgrade.
        result = self._dump_record(self._record(code="ADDHON-998"))["last_error"]
        self.assertEqual("ADDHON-998", result["code"])
        self.assertIsNone(result["reason"])

    def test_a_hostile_label_and_a_hostile_phase_are_refused(self) -> None:
        result = self._dump_record(
            self._record(code="3C:71:BF:AA:BB:CC", phase="user@example.com")
        )["last_error"]
        self.assertIsNone(result["code"])
        self.assertIsNone(result["reason"])
        self.assertIsNone(result["phase"])
        self.assertNotIn("3C:71", json.dumps(result))
        self.assertNotIn("example.com", json.dumps(result))

    def test_a_str_subclass_cannot_smuggle_text_through_the_phase(self) -> None:
        # The B1 guard, on the field this change adds: `isinstance` admits a str
        # SUBCLASS, which can match the regex and still render something else.
        class Sneaky(str):
            def __str__(self) -> str:  # pragma: no cover - only reached if the guard fails
                return "3C:71:BF:AA:BB:CC"

        result = self._dump_record(self._record(phase=Sneaky("authenticate")))["last_error"]
        self.assertIsNone(result["phase"])

    def test_a_naive_stamp_is_refused(self) -> None:
        result = self._dump_record(
            self._record(at=datetime(2026, 8, 24, 9, 0, 0))
        )["last_error"]
        self.assertIsNone(result["at"])

    def test_a_foreign_ledger_shape_is_dropped_not_published(self) -> None:
        result = self._dump_record(self._record(phase_ledger="user@example.com"))["last_error"]
        self.assertNotIn("phase_ledger", result)

    def test_last_fetch_reads_the_recorded_census(self) -> None:
        # THE acceptance test of this change, and the state the previous one wrote but
        # could not show: a POST that never reached a body. The census used to die with
        # the session the raise tore down.
        block = self._dump_record(
            self._record(
                fetch={
                    "outcome": "raised",
                    "code": "ADDHON-470",
                    "status": None,
                    "expanded": 0,
                    "built": 0,
                    "skipped": {},
                    "degraded": {},
                }
            )
        )["last_fetch"]
        self.assertEqual("recorded", block["state"])
        self.assertEqual("raised", block["outcome"])
        self.assertEqual("ADDHON-470", block["code"])

    def test_the_counters_of_a_failed_setup_survive_it(self) -> None:
        # The inventory arrived, setup dropped all of it, and then setup failed. Before
        # the record this dump was indistinguishable from an account owning nothing.
        block = self._dump_record(
            self._record(
                fetch={
                    "outcome": "ok",
                    "count": 2,
                    "status": 200,
                    "expanded": 2,
                    "built": 0,
                    "skipped": {"mac_empty": 2},
                    "degraded": {},
                }
            )
        )["last_fetch"]
        self.assertEqual((2, 2, 0), (block["count"], block["expanded"], block["built"]))
        self.assertEqual({"mac_empty": 2}, block["skipped"])

    def test_the_envelope_and_the_identity_check_survive_a_failed_setup(self) -> None:
        """A failed setup must not be LESS diagnosable than a successful one.

        `_setup_failure_record` flattens the census with `**dict(census)`, so a field
        added to the writer reaches hass.data without a second edit -- which is a
        property worth asserting rather than assuming: the day someone replaces that
        spread with an explicit key list (the shape the four counters beside it are
        already written in) the new fields vanish from exactly the dump that is hardest
        to reproduce. The record path is also the one a reporter downloads most often,
        because Home Assistant is usually still retrying when they go looking.
        """
        init = _addhon_init()
        client = _FailedClient()
        client.last_appliance_fetch = {
            "outcome": "ok", "count": 0, "status": 200,
            "envelope_ok": True, "module_ok": False, "auth_keys": 2,
            "account": "mismatch",
        }
        hass = FakeHass(_build_coordinator())
        init._store_setup_failure(hass, FakeEntry(), client)
        stored = hass.data[DOMAIN]["e1"]["setup_failure"]["fetch"]
        for key in ("envelope_ok", "module_ok", "auth_keys", "account"):
            with self.subTest(key=key):
                self.assertIn(key, stored)
        block = _run(
            diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry())
        )["last_fetch"]
        self.assertEqual("recorded", block["state"])
        self.assertIs(True, block["envelope_ok"])
        self.assertIs(False, block["module_ok"])
        self.assertEqual(2, block["auth_keys"])
        self.assertEqual("mismatch", block["account"])

    def test_the_recorded_new_fields_are_validated_like_the_live_ones(self) -> None:
        # The record has been through hass.data, so it is the LESS trusted of the two
        # sources and must not be the one that gets the shorter check. Same hostile
        # values, same refusals as on the live path.
        block = self._dump_record(
            self._record(
                fetch={
                    "outcome": "ok",
                    "envelope_ok": "true",
                    "module_ok": 1,
                    "auth_keys": "cognitoTokenNew",
                    "account": "ACCOUNT-0012345",
                }
            )
        )["last_fetch"]
        self.assertIsNone(block["envelope_ok"])
        self.assertIsNone(block["module_ok"])
        self.assertIsNone(block["auth_keys"])
        # A token the reader does not know keeps the finding and loses the value.
        self.assertEqual("other", block["account"])
        self.assertNotIn("ACCOUNT-0012345", json.dumps(block))
        self.assertNotIn("cognitoTokenNew", json.dumps(block))

    def test_a_record_without_a_census_still_reads_client_absent(self) -> None:
        # The failure happened before the call, so there is nothing to say about it.
        # `last_error` speaks for that dump; this block must not invent a census.
        block = self._dump_record(self._record())["last_fetch"]
        self.assertEqual("client_absent", block["state"])
        self.assertIsNone(block["outcome"])

    def test_the_recorded_block_keeps_the_key_set(self) -> None:
        # Same rule as the four states above it: a field-by-field diff between two
        # downloads of the same issue is only meaningful if the key set is stable.
        from_record = self._dump_record(
            self._record(fetch={"outcome": "ok", "count": 0})
        )["last_fetch"]
        hass = FakeHass(_build_coordinator())
        hass.data[DOMAIN]["e1"]["client"] = _FetchClient({"outcome": "ok", "count": 0})
        from_client = _run(
            diagnostics.async_get_config_entry_diagnostics(hass, FakeEntry())
        )["last_fetch"]
        self.assertEqual(set(from_client), set(from_record))
        self.assertEqual("recorded", from_record["state"])

    def test_the_record_is_validated_like_a_live_client_not_trusted(self) -> None:
        # The record has been through hass.data, so it is the LESS trusted of the two
        # sources -- it must not be the one that gets the shorter check. Every hostile
        # value below is refused by the same guard the live path uses.
        block = self._dump_record(
            self._record(
                fetch={
                    "outcome": "user@example.com",
                    "stopped_at": "3C:71:BF:AA:BB:CC",
                    "node_type": "Kitchen_Washer_3C71BFAABBCC",
                    "code": "not-a-label",
                    "status": 99999,
                    "count": -1,
                    "skipped": {"3C:71:BF:AA:BB:CC": 1},
                    "degraded": {"user@example.com": 1},
                }
            )
        )["last_fetch"]
        # Two refusals, deliberately different. A token this reader does not know
        # renders as "other" -- the finding survives, the value does not (_closed_token)
        # -- while a label, a status and a count that fail their check go to null.
        for key in ("outcome", "stopped_at", "node_type"):
            with self.subTest(key=key):
                self.assertEqual("other", block[key])
        for key in ("code", "status", "count"):
            with self.subTest(key=key):
                self.assertIsNone(block[key])
        self.assertEqual({}, block["skipped"])
        self.assertEqual({}, block["degraded"])
        self.assertNotIn("3C:71", json.dumps(block))
        self.assertNotIn("example.com", json.dumps(block))


if __name__ == "__main__":
    unittest.main()
