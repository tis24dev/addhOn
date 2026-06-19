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

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    ha.components = components
    ha.const = const
    helpers.entity = entity
    helpers.entity_platform = entity_platform
    helpers.update_coordinator = update_coordinator
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


if __name__ == "__main__":
    unittest.main()
