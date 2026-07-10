# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Tests for the Tier 3 number platform (writable temperature setpoints).

Modeled on the REAL fridge schema (pyhOn dump, REF HDPW5620CNPK): a `settings`
command with the range parameters tempSelZ1[2..8], tempSelZ2[-24..-16],
tempSelZ3[0..5]; no Z4/UZ/LZ. Verifies:
- capability-gating: only the setpoints present as writable parameters are created;
- range (min/max/step) read from the REAL parameter at runtime, not hardcoded;
- native_value read from the shadow (attributes);
- async_set_native_value sends the `settings` command setting the parameter
  (as int when the value is an int), via the generic hon_commands sender.

Stdlib unittest with inline Home Assistant stubs (no HA install required). The
stubs are getattr-guarded so they coexist with the other test modules in the
pytest process.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
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

        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

    update_coordinator.CoordinatorEntity = getattr(update_coordinator, "CoordinatorEntity", CoordinatorEntity)
    update_coordinator.DataUpdateCoordinator = getattr(update_coordinator, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    update_coordinator.UpdateFailed = getattr(update_coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

    components = _mod("homeassistant.components")
    number_mod = _mod("homeassistant.components.number")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class NumberEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        device_class: object | None = None
        native_unit_of_measurement: str | None = None
        native_min_value: float | None = None
        native_max_value: float | None = None
        native_step: float | None = None
        mode: object | None = None

    class NumberEntity:
        pass

    class NumberDeviceClass:
        TEMPERATURE = "temperature"

    class NumberMode:
        AUTO = "auto"
        BOX = "box"
        SLIDER = "slider"

    number_mod.NumberEntityDescription = getattr(number_mod, "NumberEntityDescription", NumberEntityDescription)
    number_mod.NumberEntity = getattr(number_mod, "NumberEntity", NumberEntity)
    number_mod.NumberDeviceClass = getattr(number_mod, "NumberDeviceClass", NumberDeviceClass)
    number_mod.NumberMode = getattr(number_mod, "NumberMode", NumberMode)

    const = _mod("homeassistant.const")

    class UnitOfTemperature:
        CELSIUS = "°C"

    const.UnitOfTemperature = getattr(const, "UnitOfTemperature", UnitOfTemperature)
    const.EntityCategory = getattr(
        const, "EntityCategory", type("EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"})
    )

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    ha.components = components
    ha.const = const
    helpers.entity = entity
    helpers.entity_platform = entity_platform
    helpers.update_coordinator = update_coordinator
    helpers.device_registry = device_registry
    components.number = number_mod


_install_homeassistant_stubs()


class RangeParam:
    """Mimics HonParameterRange: min/max/step + a value that applies pyhOn's
    str_to_float (int() first, catches only ValueError -> a fractional float would
    be truncated; a string "5.5" stays 5.5). Used to test the truncation fix."""

    def __init__(self, value, mn, mx, step) -> None:
        self.min = mn
        self.max = mx
        self.step = step
        self._v = self._coerce(value)

    @staticmethod
    def _coerce(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return float(str(v).replace(",", "."))

    @property
    def value(self):
        return self._v

    @value.setter
    def value(self, v):
        fv = self._coerce(v)
        # Same validation as HonParameterRange: out of range or off-grid
        # (step) -> ValueError, so the fail-closed path is actually exercised.
        if not (self.min <= fv <= self.max) or ((fv - self.min) * 100) % (self.step * 100):
            raise ValueError(f"Allowed: [{self.min}..{self.max}] step {self.step} But was: {fv}")
        self._v = fv


class EnumParam:
    """Mimics HonParameterEnum: a discrete set of allowed (string) values and NO
    min/max/step attribute. The value setter raises ValueError for anything outside
    the set, exactly like enum.py. Used to test the enum-setpoint handling (#26)."""

    def __init__(self, values) -> None:
        self._values = [str(v) for v in values]
        self._value = self._values[0] if self._values else ""

    @staticmethod
    def _clean(v):
        return str(v).strip("[]").replace("|", "_").lower()

    @property
    def values(self):
        return [self._clean(v) for v in self._values]

    @property
    def value(self):
        return self._clean(self._value)

    @value.setter
    def value(self, v):
        if self._clean(v) in self.values:
            self._value = v
        else:
            raise ValueError(f"Allowed values: {self._values} But was: {v}")


class RecordingCommand:
    def __init__(self, parameters) -> None:
        self.parameters = parameters
        self.send_calls = 0
        self.sent = None

    async def send(self) -> None:
        self.send_calls += 1
        self.sent = {k: p.value for k, p in self.parameters.items()}


class FakeAppliance:
    def __init__(self, commands) -> None:
        self.commands = commands


class FakeClient:
    def run_command_sync(self, coro) -> None:
        asyncio.run(coro)


class FakeCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.hass = None
        self.refreshes = 0
        self.last_update_success = True
        self.last_exception = None

    async def async_refresh(self) -> None:
        self.refreshes += 1

    async def async_request_refresh(self) -> None:
        self.refreshes += 1


class FakeHass:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}

    async def async_add_executor_job(self, func, *args):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(func, *args).result(timeout=5)


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id


def _fridge_commands() -> dict:
    """`settings` command as in the real dump: only Z1/Z2/Z3."""
    return {
        "settings": RecordingCommand(
            {
                "tempSelZ1": RangeParam(5, 2, 8, 1),
                "tempSelZ2": RangeParam(-18, -24, -16, 1),
                "tempSelZ3": RangeParam(1, 0, 5, 1),
            }
        )
    }


async def _build(app_type: str, appliance, attributes: dict, client=None) -> list:
    from custom_components.addhon import number
    from custom_components.addhon.const import DOMAIN

    data = {
        "x-1": {
            "type": app_type,
            "name": "Frigo",
            "attributes": attributes,
            "appliance": appliance,
        }
    }
    coordinator = FakeCoordinator(data)
    hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": client}}})
    added: list = []
    await number.async_setup_entry(hass, FakeEntry(), added.extend)
    for ent in added:
        ent.hass = hass
    return added


class NumberSetpointTest(unittest.TestCase):
    def test_gating_only_present_setpoints(self) -> None:
        app = FakeAppliance(_fridge_commands())
        attrs = {"tempSelZ1": "5", "tempSelZ2": "-18", "tempSelZ3": "1"}
        added = asyncio.run(_build("REF", app, attrs))
        keys = sorted(e.entity_description.key for e in added)
        self.assertEqual(
            keys,
            ["target_temp_zone1", "target_temp_zone2", "target_temp_zone3"],
        )

    def test_range_read_from_runtime_param(self) -> None:
        app = FakeAppliance(_fridge_commands())
        added = asyncio.run(_build("REF", app, {}))
        by_key = {e.entity_description.key: e for e in added}
        z1 = by_key["target_temp_zone1"]
        self.assertEqual((z1.native_min_value, z1.native_max_value, z1.native_step), (2.0, 8.0, 1.0))
        z2 = by_key["target_temp_zone2"]
        self.assertEqual((z2.native_min_value, z2.native_max_value), (-24.0, -16.0))

    def test_native_value_from_shadow(self) -> None:
        app = FakeAppliance(_fridge_commands())
        attrs = {"tempSelZ1": "5", "tempSelZ2": "-18", "tempSelZ3": "1"}
        added = asyncio.run(_build("REF", app, attrs))
        by_key = {e.entity_description.key: e for e in added}
        self.assertEqual(by_key["target_temp_zone1"].native_value, 5.0)
        self.assertEqual(by_key["target_temp_zone2"].native_value, -18.0)

    def test_set_native_value_sends_command_as_int(self) -> None:
        commands = _fridge_commands()
        app = FakeAppliance(commands)
        client = FakeClient()
        attrs = {"tempSelZ1": "5", "tempSelZ2": "-18", "tempSelZ3": "1"}
        added = asyncio.run(_build("REF", app, attrs, client=client))
        z1 = next(e for e in added if e.entity_description.key == "target_temp_zone1")
        asyncio.run(z1.async_set_native_value(4.0))
        settings = commands["settings"]
        self.assertEqual(settings.send_calls, 1)
        # tempSelZ1 set to 4 as an INT (not 4.0); the others unchanged.
        self.assertEqual(settings.parameters["tempSelZ1"].value, 4)
        self.assertEqual(settings.parameters["tempSelZ2"].value, -18)

    def test_fractional_value_not_truncated(self) -> None:
        # Regression on the truncation fix: a device with step 0.5 -> 12.5 stays 12.5.
        commands = {"settings": RecordingCommand({"tempSel": RangeParam(10, 5, 20, 0.5)})}
        app = FakeAppliance(commands)
        client = FakeClient()
        added = asyncio.run(_build("WC", app, {"tempSel": "10"}, client=client))
        ent = next(e for e in added if e.entity_description.key == "target_temp")
        asyncio.run(ent.async_set_native_value(12.5))
        self.assertEqual(commands["settings"].parameters["tempSel"].value, 12.5)
        # And an integer value stays a clean int (no 13.0).
        asyncio.run(ent.async_set_native_value(13.0))
        self.assertEqual(commands["settings"].parameters["tempSel"].value, 13)

    def test_off_grid_value_fails_closed(self) -> None:
        # Load-bearing half of the fix: an off-grid value (step 0.5) must fail
        # cleanly (HomeAssistantError), with rollback and no send.
        from homeassistant.exceptions import HomeAssistantError

        commands = {"settings": RecordingCommand({"tempSel": RangeParam(10, 5, 20, 0.5)})}
        app = FakeAppliance(commands)
        client = FakeClient()
        added = asyncio.run(_build("WC", app, {"tempSel": "10"}, client=client))
        ent = next(e for e in added if e.entity_description.key == "target_temp")
        with self.assertRaises(HomeAssistantError):
            asyncio.run(ent.async_set_native_value(12.3))  # off the 0.5 grid
        self.assertEqual(commands["settings"].parameters["tempSel"].value, 10)  # rollback
        self.assertEqual(commands["settings"].send_calls, 0)

    def test_rollback_on_send_failure(self) -> None:
        # send() used to fail OUTSIDE the try -> the parameters stayed altered. Now the
        # send is inside the try and a failure restores the state.
        from custom_components.addhon.hon_commands import async_send_command

        class _FailSend(RecordingCommand):
            async def send(self) -> None:
                self.send_calls += 1
                raise RuntimeError("send boom")

        cmd = _FailSend({"tempSel": RangeParam(10, 5, 20, 1)})
        app = FakeAppliance({"settings": cmd})
        with self.assertRaises(RuntimeError):
            asyncio.run(async_send_command(FakeHass(), FakeClient(), app, "settings", {"tempSel": 15}))
        self.assertEqual(cmd.parameters["tempSel"].value, 10)  # rollback (was 15)

    def test_rollback_on_presend_failure(self) -> None:
        # pre_send used to run BEFORE the try: if it mutated a parameter and then
        # failed, the mutation stayed. Now pre_send is inside the try and is restored.
        from custom_components.addhon.hon_commands import async_send_command

        cmd = RecordingCommand({"a": RangeParam(1, 0, 5, 1), "b": RangeParam(2, 0, 5, 1)})
        app = FakeAppliance({"settings": cmd})

        def bad_presend(cp) -> None:
            cp["b"].value = 4  # mutate a parameter not in `params`
            raise RuntimeError("presend boom")

        with self.assertRaises(RuntimeError):
            asyncio.run(async_send_command(FakeHass(), FakeClient(), app, "settings",
                                           {"a": 3}, pre_send=bad_presend))
        self.assertEqual(cmd.parameters["b"].value, 2)  # pre_send mutation undone
        self.assertEqual(cmd.parameters["a"].value, 1)  # never changed
        self.assertEqual(cmd.send_calls, 0)

    def test_fourth_zone_appears_when_present(self) -> None:
        commands = _fridge_commands()
        commands["settings"].parameters["tempSelZ4"] = RangeParam(0, -2, 4, 1)
        app = FakeAppliance(commands)
        added = asyncio.run(_build("REF", app, {}))
        keys = sorted(e.entity_description.key for e in added)
        self.assertIn("target_temp_zone4", keys)

    def test_param_range_rejects_negative_step(self) -> None:
        import types as _t

        from custom_components.addhon.hon_commands import param_range
        self.assertIsNone(param_range(_t.SimpleNamespace(min=2, max=8, step=-1)))
        self.assertEqual(param_range(_t.SimpleNamespace(min=2, max=8, step=1)), (2.0, 8.0, 1.0))

    def test_other_types_use_their_own_candidates(self) -> None:
        # Oven: only the generic tempSel (gated).
        app = FakeAppliance({"settings": RecordingCommand({"tempSel": RangeParam(180, 30, 250, 5)})})
        added = asyncio.run(_build("OV", app, {"tempSel": "180"}))
        self.assertEqual([e.entity_description.key for e in added], ["target_temp"])
        self.assertEqual(added[0].native_step, 5.0)

    # --- #26: enum-typed temperature setpoints (e.g. tempSelZ3 = ['0','2','5'] on
    # some multidoor models) must NOT get fabricated 0..100 bounds nor reject valid
    # writes. They get a discrete numeric range + membership validation before send.
    def test_enum_setpoint_uses_discrete_bounds_not_0_100(self) -> None:
        commands = {"settings": RecordingCommand({"tempSelZ3": EnumParam(["0", "2", "5"])})}
        app = FakeAppliance(commands)
        added = asyncio.run(_build("REF", app, {}))
        z3 = next(e for e in added if e.entity_description.key == "target_temp_zone3")
        self.assertEqual(z3.native_min_value, 0.0)
        self.assertEqual(z3.native_max_value, 5.0)  # NOT the fabricated 100
        # gcd of the gaps {2,3} = 1 -> every member (0,2,5) is reachable from min.
        self.assertEqual(z3.native_step, 1.0)

    def test_enum_setpoint_uniform_set_tiles_exactly(self) -> None:
        commands = {"settings": RecordingCommand({"tempSelZ3": EnumParam(["0", "2", "4"])})}
        app = FakeAppliance(commands)
        added = asyncio.run(_build("REF", app, {}))
        z3 = next(e for e in added if e.entity_description.key == "target_temp_zone3")
        self.assertEqual((z3.native_min_value, z3.native_max_value, z3.native_step), (0.0, 4.0, 2.0))

    def test_enum_setpoint_rejects_out_of_set_before_send(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        commands = {"settings": RecordingCommand({"tempSelZ3": EnumParam(["0", "2", "5"])})}
        app = FakeAppliance(commands)
        client = FakeClient()
        added = asyncio.run(_build("REF", app, {}, client=client))
        z3 = next(e for e in added if e.entity_description.key == "target_temp_zone3")
        with self.assertRaises(HomeAssistantError) as ctx:
            asyncio.run(z3.async_set_native_value(3.0))  # in 0..5 range but not in {0,2,5}
        # Distinct, actionable error (not the generic command_error), and NO cloud call.
        self.assertEqual(getattr(ctx.exception, "translation_key", None), "invalid_setpoint")
        self.assertEqual(commands["settings"].send_calls, 0)

    def test_enum_setpoint_in_set_value_sends(self) -> None:
        commands = {"settings": RecordingCommand({"tempSelZ3": EnumParam(["0", "2", "5"])})}
        app = FakeAppliance(commands)
        client = FakeClient()
        added = asyncio.run(_build("REF", app, {}, client=client))
        z3 = next(e for e in added if e.entity_description.key == "target_temp_zone3")
        asyncio.run(z3.async_set_native_value(2.0))
        self.assertEqual(commands["settings"].send_calls, 1)
        self.assertEqual(commands["settings"].parameters["tempSelZ3"].value, "2")

    def test_non_numeric_enum_setpoint_is_gated_off(self) -> None:
        # A mode-style enum (['low','high']) is not a sensible number control -> no
        # entity (rather than a fabricated 0..100 number).
        commands = {"settings": RecordingCommand({"tempSelZ3": EnumParam(["low", "high"])})}
        app = FakeAppliance(commands)
        added = asyncio.run(_build("REF", app, {}))
        keys = [e.entity_description.key for e in added]
        self.assertNotIn("target_temp_zone3", keys)

    # --- #26 CONFUTATORE: numeric/edge attacks on the new helpers (_enum_step,
    # _numeric_enum_set, _value_in_set) + the enum bounds derivation. Each test
    # kills a targeted mutant.

    def test_enum_step_uniform_gap_three(self) -> None:
        # {0,3,6,9} -> all gaps are 3 -> gcd = 3 (kills a gcd->1 mutant).
        from custom_components.addhon import number as N

        self.assertEqual(N._enum_step([0.0, 3.0, 6.0, 9.0]), 3.0)

    def test_enum_step_single_value_is_one_and_min_eq_max(self) -> None:
        # A 1-value enum (len<2) -> step 1.0; the entity has min==max (kills a
        # mutant that drops the len<2 guard and would index diffs[] of an empty list).
        from custom_components.addhon import number as N

        self.assertEqual(N._enum_step([4.0]), 1.0)
        commands = {"settings": RecordingCommand({"tempSelZ3": EnumParam(["4"])})}
        app = FakeAppliance(commands)
        added = asyncio.run(_build("REF", app, {}))
        z3 = next(e for e in added if e.entity_description.key == "target_temp_zone3")
        self.assertEqual(
            (z3.native_min_value, z3.native_max_value, z3.native_step), (4.0, 4.0, 1.0)
        )

    def test_enum_step_empty_is_one(self) -> None:
        # Defensive: an empty list (len<2) -> 1.0, never an IndexError on diffs.
        from custom_components.addhon import number as N

        self.assertEqual(N._enum_step([]), 1.0)

    def test_enum_step_single_float_value_does_not_crash(self) -> None:
        # KILLS the `len(values) < 2`->`< 1` mutant: a single NON-integer value
        # ['4.5'] falls through to the float branch, where min(diffs) on an EMPTY
        # diffs list would raise ValueError. The len<2 guard must short-circuit
        # to 1.0 (a single-int value takes the gcd branch and would NOT expose this).
        from custom_components.addhon import number as N

        self.assertEqual(N._enum_step([4.5]), 1.0)
        commands = {"settings": RecordingCommand({"tempSelZ3": EnumParam(["4.5"])})}
        app = FakeAppliance(commands)
        added = asyncio.run(_build("REF", app, {}))
        z3 = next(e for e in added if e.entity_description.key == "target_temp_zone3")
        self.assertEqual(
            (z3.native_min_value, z3.native_max_value, z3.native_step), (4.5, 4.5, 1.0)
        )

    def test_enum_float_set_step_is_smallest_gap(self) -> None:
        # Fractional set -> float branch -> SMALLEST adjacent diff. Use a set where
        # the smallest gap is NOT first: [10,12,12.5] -> gaps [2.0, 0.5]. min=0.5.
        # This single assert kills BOTH mutants: `min(diffs)`->`max(diffs)` (=2.0)
        # AND `min(diffs)`->`diffs[0]` (=2.0).
        from custom_components.addhon import number as N

        self.assertEqual(N._enum_step([10.0, 12.0, 12.5]), 0.5)  # not 2.0
        # And the simple ascending case still holds.
        self.assertEqual(N._enum_step([10.0, 10.5, 12.0]), 0.5)

    def test_enum_float_setpoint_full_roundtrip_bounds_and_send(self) -> None:
        # ['10','10.5','11'] -> bounds (10,11,0.5); 10.5 passes membership and is
        # sent as "10.5" (NOT truncated to "10"); membership and the sent string
        # must agree with EnumParam's clean_value.
        commands = {"settings": RecordingCommand({"tempSel": EnumParam(["10", "10.5", "11"])})}
        app = FakeAppliance(commands)
        client = FakeClient()
        added = asyncio.run(_build("WC", app, {}, client=client))
        ent = next(e for e in added if e.entity_description.key == "target_temp")
        self.assertEqual(
            (ent.native_min_value, ent.native_max_value, ent.native_step),
            (10.0, 11.0, 0.5),
        )
        asyncio.run(ent.async_set_native_value(10.5))
        self.assertEqual(commands["settings"].send_calls, 1)
        # Sent as the fractional string and accepted by the enum setter.
        self.assertEqual(commands["settings"].parameters["tempSel"].value, "10.5")

    def test_negative_enum_setpoint_bounds_and_membership(self) -> None:
        # Freezer-style negative enum tempSelZ2 = ['-24','-20','-18']:
        # bounds (-24,-18, gcd of {4,2}=2); -20 sends, -19 rejected before send.
        from homeassistant.exceptions import HomeAssistantError

        commands = {"settings": RecordingCommand({"tempSelZ2": EnumParam(["-24", "-20", "-18"])})}
        app = FakeAppliance(commands)
        client = FakeClient()
        added = asyncio.run(_build("REF", app, {}, client=client))
        z2 = next(e for e in added if e.entity_description.key == "target_temp_zone2")
        self.assertEqual(z2.native_min_value, -24.0)
        self.assertEqual(z2.native_max_value, -18.0)
        self.assertEqual(z2.native_step, 2.0)
        asyncio.run(z2.async_set_native_value(-20.0))
        self.assertEqual(commands["settings"].send_calls, 1)
        self.assertEqual(commands["settings"].parameters["tempSelZ2"].value, "-20")
        with self.assertRaises(HomeAssistantError):
            asyncio.run(z2.async_set_native_value(-19.0))  # in range, not in set
        self.assertEqual(commands["settings"].send_calls, 1)  # no extra send

    def test_value_in_set_tolerance_and_non_float(self) -> None:
        # KILLS the 1e-6 tolerance->0 mutant (2.0000001 must pass) and proves a
        # non-float (None) returns False instead of crashing.
        from custom_components.addhon import number as N

        s = [0.0, 2.0, 5.0]
        self.assertTrue(N._value_in_set(2.0, s))
        self.assertTrue(N._value_in_set(2.0000001, s))  # within tolerance
        self.assertFalse(N._value_in_set(2.1, s))
        self.assertFalse(N._value_in_set(None, s))  # non-float -> False, no crash

    def test_within_tolerance_drift_snapped_to_canonical_int_sends(self) -> None:
        # CR#7: a sub-tolerance drift (2.0000001) must be SNAPPED to the canonical
        # member and SENT as "2" -- NOT serialized raw as "2.0000001", which the cloud
        # enum setter rejects, surfacing as a generic command_error instead of applying.
        commands = {"settings": RecordingCommand({"tempSelZ3": EnumParam(["0", "2", "5"])})}
        app = FakeAppliance(commands)
        client = FakeClient()
        added = asyncio.run(_build("REF", app, {}, client=client))
        z3 = next(e for e in added if e.entity_description.key == "target_temp_zone3")
        asyncio.run(z3.async_set_native_value(2.0000001))  # within 1e-6 of 2
        self.assertEqual(commands["settings"].send_calls, 1)
        self.assertEqual(commands["settings"].parameters["tempSelZ3"].value, "2")

    def test_within_tolerance_drift_snapped_to_canonical_fraction_sends(self) -> None:
        # Fractional set (where min+n*step float drift actually bites): 10.5000001 must
        # be sent as "10.5", not "10.5000001".
        commands = {"settings": RecordingCommand({"tempSel": EnumParam(["10", "10.5", "11"])})}
        app = FakeAppliance(commands)
        client = FakeClient()
        added = asyncio.run(_build("WC", app, {}, client=client))
        ent = next(e for e in added if e.entity_description.key == "target_temp")
        asyncio.run(ent.async_set_native_value(10.5000001))  # within 1e-6 of 10.5
        self.assertEqual(commands["settings"].send_calls, 1)
        self.assertEqual(commands["settings"].parameters["tempSel"].value, "10.5")

    def test_snap_to_set_returns_canonical_member_or_none(self) -> None:
        # Unit: snap returns the CANONICAL member (not the drifted input) or None.
        from custom_components.addhon import number as N

        s = [0.0, 2.0, 5.0]
        self.assertEqual(N._snap_to_set(2.0000001, s), 2.0)  # canonical, not the input
        self.assertEqual(N._snap_to_set(2.0, s), 2.0)
        self.assertIsNone(N._snap_to_set(2.0 + 5e-6, s))     # just beyond tolerance (1e-6)
        self.assertIsNone(N._snap_to_set(2.1, s))            # well beyond tolerance
        self.assertIsNone(N._snap_to_set(None, s))           # non-float, no crash

    def test_numeric_enum_set_skips_when_any_value_non_numeric(self) -> None:
        # KILLS the `return None`->`continue` mutant in _numeric_enum_set: a single
        # non-numeric member among numbers (['0','low','2']) must gate the WHOLE
        # entity off (return None), not silently drop the bad value and keep [0,2].
        from custom_components.addhon import number as N

        self.assertIsNone(N._numeric_enum_set(EnumParam(["0", "low", "2"])))
        commands = {"settings": RecordingCommand({"tempSelZ3": EnumParam(["0", "low", "2"])})}
        app = FakeAppliance(commands)
        added = asyncio.run(_build("REF", app, {}))
        self.assertNotIn(
            "target_temp_zone3", [e.entity_description.key for e in added]
        )

    def test_disordered_enum_set_is_sorted_for_bounds(self) -> None:
        # KILLS the `sorted(set(out))`->`out` mutant: an enum given out of order
        # ['5','0','2'] must still yield min=0, max=5 (NOT min=5).
        from custom_components.addhon import number as N

        self.assertEqual(N._numeric_enum_set(EnumParam(["5", "0", "2"])), [0.0, 2.0, 5.0])
        commands = {"settings": RecordingCommand({"tempSelZ3": EnumParam(["5", "0", "2"])})}
        app = FakeAppliance(commands)
        added = asyncio.run(_build("REF", app, {}))
        z3 = next(e for e in added if e.entity_description.key == "target_temp_zone3")
        self.assertEqual(z3.native_min_value, 0.0)  # NOT 5.0
        self.assertEqual(z3.native_max_value, 5.0)

    def test_empty_enum_set_is_gated_off(self) -> None:
        # An enum with no values -> _numeric_enum_set returns None -> no entity
        # (covers the `if not out: return None` line).
        from custom_components.addhon import number as N

        self.assertIsNone(N._numeric_enum_set(EnumParam([])))
        commands = {"settings": RecordingCommand({"tempSelZ3": EnumParam([])})}
        app = FakeAppliance(commands)
        added = asyncio.run(_build("REF", app, {}))
        self.assertNotIn(
            "target_temp_zone3", [e.entity_description.key for e in added]
        )


if __name__ == "__main__":
    unittest.main()
