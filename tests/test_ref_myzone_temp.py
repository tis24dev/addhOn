# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the My Zone temperature correction of issue #75.

Two fridges publish the measured temperature of their My Zone drawer 38 °C below
reality while the drawer's setpoint stays correct:

    HFW7918ENMP   tempZ3 = -43   tempSelZ4 = -5    (range -5..5)
    HCW58F18EWMP  tempZ4 = -56   tempSelZ4 = -18   (range -18..5)

The other zones of the same appliances are exact, so the correction must reach the
My Zone and nothing else, and it must not reach a fridge whose dictionary is sound.

The shapes below are those two appliances plus the healthy counter-examples. Stubs
come from conftest (shared across the suite); only the appliance/coordinator fakes
are local, because this platform needs the write COMMAND and the shared
`_build_sensors` helpers do not carry one.
"""
from __future__ import annotations

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


def _install_package_stubs() -> None:
    """The few `homeassistant` modules conftest does not already provide.

    Importing `custom_components.addhon.sensor` runs the package __init__, which
    needs config_entries/core/exceptions. Everything else (the sensor platform,
    the coordinator entity, const) comes from conftest. getattr-guarded, so the
    first stub in the shared pytest process keeps winning.
    """
    ha = _mod("homeassistant")

    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )

    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))

    exceptions = _mod("homeassistant.exceptions")
    base_err = getattr(
        exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {})
    )
    exceptions.HomeAssistantError = base_err
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {})
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {})
    )

    # conftest builds the sensor platform stub but not SensorEntity, and its const
    # stub carries only the units its own modules needed.
    components = _mod("homeassistant.components")
    sensor_mod = _mod("homeassistant.components.sensor")
    sensor_mod.SensorEntity = getattr(
        sensor_mod, "SensorEntity", type("SensorEntity", (), {})
    )
    components.sensor = sensor_mod

    const = _mod("homeassistant.const")
    const.UnitOfEnergy = getattr(
        const, "UnitOfEnergy", type("UnitOfEnergy", (), {"KILO_WATT_HOUR": "kWh"})
    )
    const.UnitOfVolume = getattr(
        const, "UnitOfVolume", type("UnitOfVolume", (), {"LITERS": "L"})
    )
    const.UnitOfTime = getattr(
        const, "UnitOfTime", type("UnitOfTime", (), {"MINUTES": "min", "SECONDS": "s"})
    )
    const.UnitOfTemperature = getattr(
        const, "UnitOfTemperature", type("UnitOfTemperature", (), {"CELSIUS": "°C"})
    )
    const.UnitOfMass = getattr(
        const, "UnitOfMass", type("UnitOfMass", (), {"GRAMS": "g", "KILOGRAMS": "kg"})
    )
    const.EntityCategory = getattr(
        const,
        "EntityCategory",
        type("EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"}),
    )

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.components = components
    ha.const = const


_install_package_stubs()


class RangeParam:
    """Mimics HonParameterRange: min/max/step, as the cloud declares a setpoint."""

    def __init__(self, value, mn, mx, step=1) -> None:
        self.value = value
        self.min = mn
        self.max = mx
        self.step = step


class EnumParam:
    """Mimics HonParameterEnum: a discrete allowed set and NO min/max/step.

    The app's own HTW7720ENMP fixture declares a My Zone setpoint this way
    (tempSelZ3 enumValues ["0", "2", "5"]), so an enum has to bound the drawer too.
    """

    def __init__(self, values) -> None:
        self.values = [str(v) for v in values]
        self.value = self.values[0] if self.values else ""


class MalformedRangeParam(RangeParam):
    """A range whose bounds are inconsistent (max below min).

    `param_range` refuses it -- the same None it returns for an enum -- so this is
    the case that tells the two apart. Reading `.values` off a range materialises
    the whole grid, so here it raises: a test then fails loudly instead of hanging
    if the enum fallback ever reaches a range parameter.
    """

    @property
    def values(self):
        raise AssertionError("materialised the grid of a range parameter")


class FakeCommand:
    def __init__(self, parameters) -> None:
        self.parameters = parameters


class FakeAppliance:
    def __init__(self, settings_parameters) -> None:
        self.commands = {"settings": FakeCommand(settings_parameters)}


class FakeCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.hass = None
        self.last_update_success = True


class FakeHass:
    def __init__(self, data: dict) -> None:
        self.data = data


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id
        self.options: dict = {}


async def _build(app_type: str, attributes: dict, setpoints: dict | None = None) -> list:
    """Build the sensors of one appliance, with a real write command behind it."""
    from custom_components.addhon import sensor
    from custom_components.addhon.const import DOMAIN

    entry: dict = {
        "type": app_type,
        "name": "Dev",
        "attributes": attributes,
        "settings": {},
    }
    if setpoints is not None:
        entry["appliance"] = FakeAppliance(setpoints)
    coordinator = FakeCoordinator({"x-1": entry})
    hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
    added: list = []
    await sensor.async_setup_entry(hass, FakeEntry(), added.extend)
    return [e for e in added if not getattr(e, "_addhon_account", False)]


def _by_key(entities: list, key: str):
    return next(e for e in entities if e._attr_unique_id == f"x-1_{key}")


def _set_attr(entity, name: str, value) -> None:
    """Replace one shadow value under the entity, as a refresh would."""
    entity.coordinator.data["x-1"]["attributes"][name] = value


# The drawer of HFW7918ENMP: door and measure on index 3, setpoint on index 4.
# Shadow values as its debug log of 25/08/2026 reported them -- tempZ1/tempZ2 are
# exact there, only the drawer is not -- and setpoint bounds exactly as its own
# diagnostics dump declares them under commands/settings (tempSelZ1 1..9,
# tempSelZ2 -24..-14, tempSelZ4 -5..5, and NO writable tempSelZ3 at all).
def _hfw7918_attributes(temp_z3: str = "-43") -> dict:
    return {
        "available": True,
        "tempZ1": "5",
        "tempSelZ1": "5",
        "tempZ2": "-18",
        "tempSelZ2": "-18",
        "tempZ3": temp_z3,
        "tempSelZ4": "-5",
        "tempEnv": "29",
    }


def _hfw7918_setpoints() -> dict:
    return {
        "tempSelZ1": RangeParam(5, 1, 9),
        "tempSelZ2": RangeParam(-18, -24, -14),
        "tempSelZ4": RangeParam(-5, -5, 5),
    }


class MyZoneBiasTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_impossible_drawer_reading_is_corrected(self) -> None:
        added = await _build("REF", _hfw7918_attributes(), _hfw7918_setpoints())
        self.assertEqual(_by_key(added, "temp_zone3").native_value, -5.0)

    async def test_the_fridge_and_the_freezer_are_left_alone(self) -> None:
        added = await _build("REF", _hfw7918_attributes(), _hfw7918_setpoints())
        self.assertEqual(_by_key(added, "temp_zone1").native_value, 5.0)
        self.assertEqual(_by_key(added, "temp_zone2").native_value, -18.0)
        self.assertEqual(_by_key(added, "temp_ambient").native_value, 29.0)

    async def test_the_whole_recorded_pull_down_reads_as_a_pull_down(self) -> None:
        # The trace of #75: setpoint already at -5, the drawer walking down one
        # degree every seven minutes. Corrected, it leaves the +2 it was resting at
        # and heads for the -5 it was just set to.
        added = await _build("REF", _hfw7918_attributes("-36"), _hfw7918_setpoints())
        drawer = _by_key(added, "temp_zone3")
        seen = []
        for raw in ("-36", "-37", "-38", "-39", "-40"):
            _set_attr(drawer, "tempZ3", raw)
            seen.append(drawer.native_value)
        self.assertEqual(seen, [2.0, 1.0, 0.0, -1.0, -2.0])

    async def test_a_sound_fridge_is_never_touched(self) -> None:
        attributes = {
            "available": True,
            "tempZ1": "5",
            "tempZ3": "-4",
            "tempSelZ3": "-5",
        }
        setpoints = {
            "tempSelZ1": RangeParam(5, 1, 9),
            "tempSelZ3": RangeParam(-5, -5, 5),
        }
        added = await _build("REF", attributes, setpoints)
        self.assertEqual(_by_key(added, "temp_zone3").native_value, -4.0)

    async def test_a_drawer_merely_overshooting_its_setpoint_is_not_biased(self) -> None:
        # Super-freeze pull-down: 14 °C under the floor is cold, not impossible.
        attributes = {"available": True, "tempZ3": "-19", "tempSelZ3": "-5"}
        setpoints = {"tempSelZ3": RangeParam(-5, -5, 5)}
        added = await _build("REF", attributes, setpoints)
        self.assertEqual(_by_key(added, "temp_zone3").native_value, -19.0)

    async def test_the_verdict_survives_the_drawer_warming_back_up(self) -> None:
        # The point of the latch: once corrected, a reading that climbs back inside
        # the plausible band keeps the correction instead of jumping 38 °C.
        added = await _build("REF", _hfw7918_attributes(), _hfw7918_setpoints())
        drawer = _by_key(added, "temp_zone3")
        self.assertEqual(drawer.native_value, -5.0)
        _set_attr(drawer, "tempZ3", "-9")
        self.assertEqual(drawer.native_value, 29.0)

    async def test_the_verdict_is_not_reached_before_a_reading_proves_it(self) -> None:
        # Same appliance, opened at a plausible value: nothing is corrected until
        # an impossible one actually arrives.
        added = await _build("REF", _hfw7918_attributes("-9"), _hfw7918_setpoints())
        drawer = _by_key(added, "temp_zone3")
        self.assertEqual(drawer.native_value, -9.0)
        _set_attr(drawer, "tempZ3", "-43")
        self.assertEqual(drawer.native_value, -5.0)

    async def test_the_second_appliance_is_corrected_on_its_own_index(self) -> None:
        # HCW58F18EWMP: the measure is on Z4, and so is the setpoint.
        attributes = {"available": True, "tempZ4": "-56", "tempSelZ4": "-18"}
        setpoints = {"tempSelZ4": RangeParam(-18, -18, 5)}
        added = await _build("REF", attributes, setpoints)
        self.assertEqual(_by_key(added, "temp_zone4").native_value, -18.0)

    async def test_the_wide_drawer_is_corrected_over_its_whole_range(self) -> None:
        # HCW58F18EWMP's drawer spans -18..+5, a band narrower than the bias itself.
        # A test that only asked "is this below the floor" would recognise the same
        # -38 °C error with the drawer set to -5 and miss it with the drawer set to
        # +2, and the state would step ~35 °C when the user crossed zero.
        for setpoint in (5, 4, 2, 0, -1, -5, -12, -18):
            attributes = {
                "available": True,
                "tempZ4": str(setpoint - 38),
                "tempSelZ4": str(setpoint),
            }
            setpoints = {"tempSelZ4": RangeParam(setpoint, -18, 5)}
            added = await _build("REF", attributes, setpoints)
            with self.subTest(setpoint=setpoint):
                self.assertEqual(
                    _by_key(added, "temp_zone4").native_value, float(setpoint)
                )

    async def test_a_reading_the_bias_cannot_explain_is_left_alone(self) -> None:
        # A dead probe pinned at a sentinel: impossible, but +38 does not make it a
        # drawer temperature either, so shifting it would only make it look sane.
        attributes = {"available": True, "tempZ3": "-128", "tempSelZ4": "-5"}
        added = await _build("REF", attributes, _hfw7918_setpoints())
        self.assertEqual(_by_key(added, "temp_zone3").native_value, -128.0)

    async def test_a_corrected_reading_far_above_the_drawer_is_left_alone(self) -> None:
        # -22 is past the floor by more than a probe overshoots, so the first half of
        # the test is satisfied -- but +38 would put it at +16, warmer than the
        # drawer's ceiling by more than a door left open explains. Neither reading is
        # a drawer temperature, so what arrived is what gets reported.
        attributes = {"available": True, "tempZ3": "-22", "tempSelZ4": "-5"}
        setpoints = {"tempSelZ4": RangeParam(-5, -5, 5)}
        added = await _build("REF", attributes, setpoints)
        self.assertEqual(_by_key(added, "temp_zone3").native_value, -22.0)

    async def test_an_enum_setpoint_bounds_the_drawer_too(self) -> None:
        attributes = {"available": True, "tempZ4": "-56", "tempSelZ4": "-18"}
        setpoints = {"tempSelZ4": EnumParam(["-18", "-5", "0", "5"])}
        added = await _build("REF", attributes, setpoints)
        self.assertEqual(_by_key(added, "temp_zone4").native_value, -18.0)

    async def test_without_a_writable_setpoint_nothing_is_judged(self) -> None:
        # No command at all: the reading is wrong and stays wrong, visibly, rather
        # than being corrected by a rule nothing on this device supports.
        added = await _build("REF", _hfw7918_attributes())
        self.assertEqual(_by_key(added, "temp_zone3").native_value, -43.0)

    async def test_a_setpoint_with_no_declared_bounds_judges_nothing(self) -> None:
        attributes = {"available": True, "tempZ3": "-43", "tempSelZ4": "-5"}
        setpoints = {"tempSelZ4": EnumParam([])}
        added = await _build("REF", attributes, setpoints)
        self.assertEqual(_by_key(added, "temp_zone3").native_value, -43.0)

    async def test_a_malformed_range_is_refused_without_reading_its_grid(self) -> None:
        attributes = {"available": True, "tempZ3": "-43", "tempSelZ4": "-5"}
        setpoints = {"tempSelZ4": MalformedRangeParam(-5, 5, -5)}
        added = await _build("REF", attributes, setpoints)
        self.assertEqual(_by_key(added, "temp_zone3").native_value, -43.0)

    async def test_an_unreadable_value_stays_unknown(self) -> None:
        attributes = {"available": True, "tempZ3": "n/a", "tempSelZ4": "-5"}
        added = await _build("REF", attributes, _hfw7918_setpoints())
        self.assertIsNone(_by_key(added, "temp_zone3").native_value)


class MyZoneScopeTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_drawer_zones_get_the_correcting_class(self) -> None:
        from custom_components.addhon.sensor import HonMyZoneTempSensor, HonSensor

        attributes = dict(_hfw7918_attributes(), tempZ4="-56")
        added = await _build("REF", attributes, _hfw7918_setpoints())
        for key in ("temp_zone3", "temp_zone4"):
            self.assertIsInstance(_by_key(added, key), HonMyZoneTempSensor)
        for key in ("temp_zone1", "temp_zone2", "temp_ambient"):
            entity = _by_key(added, key)
            self.assertIsInstance(entity, HonSensor)
            self.assertNotIsInstance(entity, HonMyZoneTempSensor)

    async def test_the_freezer_types_share_the_correction(self) -> None:
        from custom_components.addhon.sensor import HonMyZoneTempSensor

        for app_type in ("FR", "FRE"):
            added = await _build(app_type, _hfw7918_attributes(), _hfw7918_setpoints())
            self.assertIsInstance(_by_key(added, "temp_zone3"), HonMyZoneTempSensor)

    async def test_a_hob_reporting_the_same_parameter_names_is_untouched(self) -> None:
        # The hob publishes `tempZ{N}` too (issue #84). Nothing about a cooking zone
        # is a fridge drawer, and it must never reach the correction.
        from custom_components.addhon.sensor import HonMyZoneTempSensor

        added = await _build("HOB", {"available": True, "tempZ3": "-43", "sensorTempZ3": "-43"})
        for entity in added:
            self.assertNotIsInstance(entity, HonMyZoneTempSensor)
        self.assertEqual(_by_key(added, "temp_zone3").native_value, -43.0)


if __name__ == "__main__":
    unittest.main()
