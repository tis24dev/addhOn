"""Behavioural tests for the writable program options (discussion #35).

Covers the design-of-record:
- the GATE (>= 2 reachable values / not fixed; range checked before enum; dryLevel
  sentinel-only -> off);
- BUFFER write: an option entity writes the coordinator store and sends NOTHING;
- READ precedence: pending value else live device value, None when absent;
- APPLY-on-start: the buffered options land on the POST-SWAP startProgram command, are
  sent once, and both pending stores are cleared on success;
- an option absent from the selected program is skipped (no error);
- stopProgram ignores the option buffer;
- CAPABILITY gate: a fixed/single-value param creates NO entity (anti-"No disponible");
- TYPE gate: dryLevel uses the WM/WD label map vs the TD one.

Stdlib unittest + inline Home Assistant stubs (the platforms pull the entity stack).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
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
    dr.DeviceEntryType = getattr(dr, "DeviceEntryType", type("DeviceEntryType", (), {"SERVICE": "service"}))
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
            self.hass = getattr(coordinator, "hass", None)

        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

    uc.CoordinatorEntity = getattr(uc, "CoordinatorEntity", CoordinatorEntity)
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

    const = _mod("homeassistant.const")
    for unit_cls in ("UnitOfTemperature", "UnitOfTime"):
        if not hasattr(const, unit_cls):
            setattr(const, unit_cls, type(unit_cls, (), {"CELSIUS": "C", "MINUTES": "min", "SECONDS": "s"}))
    const.EntityCategory = getattr(
        const, "EntityCategory", type("EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"})
    )

    components = _mod("homeassistant.components")
    _mod("homeassistant.components.switch").SwitchEntity = type("SwitchEntity", (), {})
    _mod("homeassistant.components.select").SelectEntity = type("SelectEntity", (), {})
    _mod("homeassistant.components.button").ButtonEntity = type("ButtonEntity", (), {})
    number_mod = _mod("homeassistant.components.number")
    import dataclasses

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


_install_stubs()


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


class Param:
    """A settable command parameter (value assignment recorded)."""

    def __init__(self, value=None, values=None) -> None:
        self.value = value
        self.values = values


class RecordingCommand:
    def __init__(self, parameters=None) -> None:
        self.parameters = parameters or {}
        self.send_calls = 0

    async def send(self) -> None:
        self.send_calls += 1


class RangeParam:
    """Duck-types HonParameterRange (min/max/step) for the gate + option_choices."""

    def __init__(self, mn: float, mx: float, step: float, value=None) -> None:
        self.min = mn
        self.max = mx
        self.step = step
        self.value = value if value is not None else mn

    @property
    def values(self) -> list[str]:
        out: list[str] = []
        i = self.min
        while i <= self.max:
            out.append(str(i))
            i += self.step
        return out


class SetParam:
    """Duck-types HonParameterEnum/Fixed (values only, no range)."""

    def __init__(self, values, value=None) -> None:
        self._values = list(values)
        self.value = value if value is not None else (self._values[0] if self._values else None)

    @property
    def values(self) -> list[str]:
        return list(self._values)


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id


def _washer(commands: dict, attributes: dict | None = None, app_type: str = "WM") -> dict:
    return {
        "washer-1": {
            "type": app_type,
            "name": "Washer",
            "appliance": types.SimpleNamespace(commands=commands),
            "attributes": attributes or {},
            "settings": {},
        }
    }


class GateTest(unittest.TestCase):
    def test_range_two_or_more_is_settable(self) -> None:
        from custom_components.addhon.program_options import is_settable_option

        self.assertTrue(is_settable_option(RangeParam(0, 1, 1)))
        self.assertTrue(is_settable_option(RangeParam(12, 14, 1)))
        self.assertTrue(is_settable_option(RangeParam(0, 360, 360)))

    def test_range_single_value_not_settable(self) -> None:
        from custom_components.addhon.program_options import is_settable_option

        # max == min -> only one reachable value -> off.
        self.assertFalse(is_settable_option(RangeParam(5, 5, 1)))

    def test_enum_two_or_more_is_settable(self) -> None:
        from custom_components.addhon.program_options import is_settable_option

        self.assertTrue(is_settable_option(SetParam(["0", "400", "800"])))

    def test_enum_single_value_not_settable(self) -> None:
        from custom_components.addhon.program_options import is_settable_option

        self.assertFalse(is_settable_option(SetParam(["0"])))

    def test_fixed_param_not_settable(self) -> None:
        from custom_components.addhon.program_options import is_settable_option

        # A fixed param duck-types as a single-value set (and no range).
        self.assertFalse(is_settable_option(SetParam(["0"])))

    def test_drylevel_sentinel_only_not_settable(self) -> None:
        from custom_components.addhon.const import DRY_LEVEL_SENTINELS
        from custom_components.addhon.program_options import is_settable_option

        # An enum exposing only the unselectable sentinels -> nothing to pick -> off.
        self.assertFalse(is_settable_option(SetParam(["0", "11"]), DRY_LEVEL_SENTINELS))

    def test_option_value_set_empty_for_range(self) -> None:
        # CRITICAL: a range must never be counted via .values (it enumerates min..max).
        from custom_components.addhon.program_options import option_value_set

        self.assertEqual(option_value_set(RangeParam(0, 1410, 30)), [])

    def test_option_choices_materializes_range(self) -> None:
        from custom_components.addhon.program_options import option_choices

        self.assertEqual(option_choices(RangeParam(12, 14, 1)), ["12", "13", "14"])
        self.assertEqual(option_choices(RangeParam(0, 360, 360)), ["0", "360"])

    def test_option_choices_drops_sentinels(self) -> None:
        from custom_components.addhon.const import DRY_LEVEL_SENTINELS
        from custom_components.addhon.program_options import option_choices

        self.assertEqual(
            option_choices(SetParam(["0", "1", "2", "11"]), DRY_LEVEL_SENTINELS),
            ["1", "2"],
        )

    def test_range_with_sentinels_counts_non_sentinel_members(self) -> None:
        # The correctness graft: when `drop` is non-empty a RANGE is gated on its NON-
        # sentinel reachable members (>= 2), not the cheap max>min -- so a sentinel-only
        # range gates OFF even though max>min. Range is still measured WITHOUT .values.
        from custom_components.addhon.const import DRY_LEVEL_SENTINELS
        from custom_components.addhon.program_options import is_settable_option

        # 0..11 step 11 -> {0, 11}, both sentinels -> 0 real values -> off (max>min is True!).
        self.assertFalse(is_settable_option(RangeParam(0, 11, 11), DRY_LEVEL_SENTINELS))
        # 12..14 step 1 -> {12, 13, 14}, no sentinels -> settable.
        self.assertTrue(is_settable_option(RangeParam(12, 14, 1), DRY_LEVEL_SENTINELS))
        # 0..13 step 1 minus {0, 11} -> 12 real members -> settable.
        self.assertTrue(is_settable_option(RangeParam(0, 13, 1), DRY_LEVEL_SENTINELS))

    def test_none_param_not_settable(self) -> None:
        from custom_components.addhon.program_options import is_settable_option

        self.assertFalse(is_settable_option(None))

    def test_normalize_code_cleans_numeric_repr(self) -> None:
        # The token normalizer: float/int/str numerics collapse to a clean code so a
        # device reading ("13.0", 360) matches the schema codes; non-numeric passes through.
        from custom_components.addhon.program_options import normalize_code

        self.assertEqual("13", normalize_code("13.0"))
        self.assertEqual("360", normalize_code(360))
        self.assertEqual("0", normalize_code("0"))
        self.assertEqual("iot_smart", normalize_code("iot_smart"))
        self.assertIsNone(normalize_code(None))


class BufferWriteTest(unittest.IsolatedAsyncioTestCase):
    def _attach(self, entity) -> None:
        entity.hass = FakeHass()

    async def test_switch_buffers_without_sending(self) -> None:
        from custom_components.addhon import switch

        start = RecordingCommand({"extraRinse1": RangeParam(0, 1, 1)})
        coordinator = FakeCoordinator(
            _washer({"startProgram": start}, attributes={"extraRinse1": "0"})
        )
        desc = switch.HonProgramOptionSwitchDescription(
            key="extra_rinse_1", param="extraRinse1", types=("WM", "WD")
        )
        entity = switch.HonProgramOptionSwitch(coordinator, "washer-1", desc, FakeClient())
        self._attach(entity)

        self.assertFalse(entity.is_on)  # live value "0"
        await entity.async_turn_on()

        # Buffered only: no command sent, no refresh.
        self.assertEqual({"washer-1": {"extraRinse1": "1"}}, coordinator.pending_options)
        self.assertEqual(0, start.send_calls)
        self.assertEqual(0, coordinator.refreshes)
        self.assertTrue(entity.is_on)  # pending now wins

    async def test_select_buffers_raw_value(self) -> None:
        from custom_components.addhon import select

        start = RecordingCommand({"spinSpeed": SetParam(["0", "400", "800", "1000", "1200"])})
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        desc = select.HonProgramOptionSelectDescription(
            key="spin_speed", param="spinSpeed", translation_key="spin_speed", types=("WM", "WD")
        )
        entity = select.HonProgramOptionSelect(coordinator, "washer-1", desc, FakeClient())
        self._attach(entity)

        self.assertEqual(["0", "400", "800", "1000", "1200"], entity._attr_options)
        await entity.async_select_option("800")

        self.assertEqual({"washer-1": {"spinSpeed": "800"}}, coordinator.pending_options)
        self.assertEqual(0, start.send_calls)
        self.assertEqual("800", entity.current_option)

    async def test_number_buffers_and_rejects_off_grid(self) -> None:
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.addhon import number

        start = RecordingCommand({"delayTime": RangeParam(0, 1410, 30)})
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        desc = number.HonProgramOptionNumberDescription(
            key="delay_time", param="delayTime", translation_key="delay_time",
            types=("WM", "WD", "TD"),
        )
        entity = number.HonProgramOptionNumber(coordinator, "washer-1", desc, FakeClient())
        self._attach(entity)

        self.assertEqual(0, entity.native_min_value)
        self.assertEqual(1410, entity.native_max_value)
        self.assertEqual(30, entity.native_step)

        await entity.async_set_native_value(240)
        self.assertEqual({"washer-1": {"delayTime": "240"}}, coordinator.pending_options)
        self.assertEqual(0, start.send_calls)

        # Off-grid value rejected up front (clean error, no buffer change).
        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_set_native_value(45)
        self.assertEqual("invalid_setpoint", ctx.exception.translation_key)

    async def test_anti_crease_time_switch_uses_schema_values(self) -> None:
        # antiCreaseTime is a 0/360 range: on must be the schema's "360", not a hardcoded
        # "1". The on/off tokens are derived from the device schema (TD dryer).
        from custom_components.addhon import switch

        start = RecordingCommand({"antiCreaseTime": RangeParam(0, 360, 360)})
        coordinator = FakeCoordinator(_washer({"startProgram": start}, app_type="TD"))
        desc = switch.HonProgramOptionSwitchDescription(
            key="anti_crease_time", param="antiCreaseTime", types=("TD",)
        )
        entity = switch.HonProgramOptionSwitch(coordinator, "washer-1", desc, FakeClient())
        self._attach(entity)

        await entity.async_turn_on()
        self.assertEqual({"washer-1": {"antiCreaseTime": "360"}}, coordinator.pending_options)
        self.assertTrue(entity.is_on)
        await entity.async_turn_off()
        self.assertEqual("0", coordinator.pending_options["washer-1"]["antiCreaseTime"])
        self.assertFalse(entity.is_on)
        self.assertEqual(0, start.send_calls)


class ReadPrecedenceTest(unittest.IsolatedAsyncioTestCase):
    def _attach(self, entity) -> None:
        entity.hass = FakeHass()

    def _make(self, attributes):
        from custom_components.addhon import switch

        start = RecordingCommand({"extraRinse1": RangeParam(0, 1, 1)})
        coordinator = FakeCoordinator(_washer({"startProgram": start}, attributes=attributes))
        desc = switch.HonProgramOptionSwitchDescription(
            key="extra_rinse_1", param="extraRinse1", types=("WM", "WD")
        )
        entity = switch.HonProgramOptionSwitch(coordinator, "washer-1", desc, FakeClient())
        self._attach(entity)
        return entity, coordinator

    async def test_live_value_when_no_pending(self) -> None:
        entity, _ = self._make({"extraRinse1": "1"})
        self.assertTrue(entity.is_on)

    async def test_pending_overrides_live(self) -> None:
        entity, coordinator = self._make({"extraRinse1": "1"})
        coordinator.pending_options = {"washer-1": {"extraRinse1": "0"}}
        self.assertFalse(entity.is_on)

    async def test_none_when_absent(self) -> None:
        entity, _ = self._make({})
        self.assertIsNone(entity.is_on)


class ApplyOnStartTest(unittest.IsolatedAsyncioTestCase):
    def _attach(self, entity) -> None:
        entity.hass = FakeHass()

    def _swap_appliance(self, new_cmd):
        """Build a startProgram whose 'program' setter swaps the active command (like
        HonParameterProgram), returning (appliance, old_cmd)."""
        appliance = types.SimpleNamespace(commands={})

        class ProgramSwapParam:
            def __init__(self) -> None:
                self._value = None

            @property
            def value(self):
                return self._value

            @value.setter
            def value(self, v) -> None:
                self._value = v
                appliance.commands["startProgram"] = new_cmd  # category swap

        old_cmd = RecordingCommand({"program": ProgramSwapParam()})
        appliance.commands["startProgram"] = old_cmd
        return appliance, old_cmd

    async def test_options_land_on_post_swap_command_and_clear(self) -> None:
        from custom_components.addhon.button import HonProgramCommandButton

        new_cmd = RecordingCommand({"spinSpeed": Param("0"), "prStr": Param("X")})
        appliance, old_cmd = self._swap_appliance(new_cmd)
        coordinator = FakeCoordinator(
            {"washer-1": {"type": "WM", "name": "W", "appliance": appliance,
                          "attributes": {}, "settings": {}}}
        )
        coordinator.pending_programs = {"washer-1": "2"}
        coordinator.pending_options = {"washer-1": {"spinSpeed": "1200"}}

        button = HonProgramCommandButton(
            coordinator, "washer-1", FakeClient(),
            command_name="startProgram", unique_suffix="start_program",
            translation_key="start_program", icon="mdi:play-circle",
            command_parameters={"prStr": "Y"},
        )
        self._attach(button)

        await button.async_press()

        # The selected (swapped) command was sent once; option landed on the NEW command.
        self.assertEqual(1, new_cmd.send_calls)
        self.assertEqual(0, old_cmd.send_calls)
        self.assertEqual("1200", new_cmd.parameters["spinSpeed"].value)
        self.assertEqual("Y", new_cmd.parameters["prStr"].value)  # fixed params too
        # Both buffers cleared on success.
        self.assertEqual({}, coordinator.pending_programs)
        self.assertEqual({}, coordinator.pending_options)
        self.assertEqual(1, coordinator.refreshes)

    async def test_options_cleared_even_without_program_reselection(self) -> None:
        # Only options changed (no pending program): the active command is sent and the
        # option buffer is still cleared (gated on the command name, not pending_program).
        from custom_components.addhon.button import HonProgramCommandButton

        start = RecordingCommand({"spinSpeed": Param("0")})
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        coordinator.pending_options = {"washer-1": {"spinSpeed": "1000"}}

        button = HonProgramCommandButton(
            coordinator, "washer-1", FakeClient(),
            command_name="startProgram", unique_suffix="start_program",
            translation_key="start_program", icon="mdi:play-circle",
        )
        self._attach(button)

        await button.async_press()

        self.assertEqual(1, start.send_calls)
        self.assertEqual("1000", start.parameters["spinSpeed"].value)
        self.assertEqual({}, coordinator.pending_options)

    async def test_option_absent_for_selected_program_is_skipped(self) -> None:
        # The buffered option is not a parameter of the selected program's command: it is
        # skipped (debug), the command still starts, no error.
        from custom_components.addhon.button import HonProgramCommandButton

        start = RecordingCommand({"onOffStatus": Param("1")})  # no spinSpeed param here
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        coordinator.pending_options = {"washer-1": {"spinSpeed": "1200"}}

        button = HonProgramCommandButton(
            coordinator, "washer-1", FakeClient(),
            command_name="startProgram", unique_suffix="start_program",
            translation_key="start_program", icon="mdi:play-circle",
        )
        self._attach(button)

        await button.async_press()

        self.assertEqual(1, start.send_calls)
        self.assertEqual({}, coordinator.pending_options)

    async def test_stop_ignores_options(self) -> None:
        from custom_components.addhon.button import HonProgramCommandButton

        stop = RecordingCommand({"onOffStatus": Param("1")})
        coordinator = FakeCoordinator(_washer({"stopProgram": stop}))
        coordinator.pending_options = {"washer-1": {"spinSpeed": "1200"}}

        button = HonProgramCommandButton(
            coordinator, "washer-1", FakeClient(),
            command_name="stopProgram", unique_suffix="stop_program",
            translation_key="stop_program", icon="mdi:stop-circle",
            command_parameters={"onOffStatus": "0"},
        )
        self._attach(button)

        await button.async_press()

        self.assertEqual(1, stop.send_calls)
        # The stop must neither consume nor apply the option buffer.
        self.assertEqual({"washer-1": {"spinSpeed": "1200"}}, coordinator.pending_options)
        self.assertEqual("0", stop.parameters["onOffStatus"].value)

    async def test_failed_start_keeps_options(self) -> None:
        # On a send failure the option buffer is KEPT (the clear is past the send, inside
        # the try): the user can retry without re-entering the options.
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.addhon.button import HonProgramCommandButton

        class FailingCommand(RecordingCommand):
            async def send(self) -> None:
                raise RuntimeError("cloud rejected")

        start = FailingCommand({"spinSpeed": Param("0")})
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        coordinator.pending_options = {"washer-1": {"spinSpeed": "800"}}

        button = HonProgramCommandButton(
            coordinator, "washer-1", FakeClient(),
            command_name="startProgram", unique_suffix="start_program",
            translation_key="start_program", icon="mdi:play-circle",
        )
        self._attach(button)

        with self.assertRaises(HomeAssistantError):
            await button.async_press()
        self.assertEqual({"washer-1": {"spinSpeed": "800"}}, coordinator.pending_options)


class CapabilityGateSetupTest(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_param_creates_no_select(self) -> None:
        # The anti-"No disponible" assertion: a fixed/single-value option creates NO
        # entity, while a genuinely settable one does.
        from custom_components.addhon import select
        from custom_components.addhon.const import DOMAIN

        start = RecordingCommand({
            "program": Param(values={"1": "Cotone", "2": "Sintetici"}),
            "spinSpeed": SetParam(["0", "400", "800"]),  # settable -> entity
            "dirtyLevel": SetParam(["0"]),               # fixed/single -> NO entity
        })
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}})
        added: list = []

        await select.async_setup_entry(hass, FakeEntry(), added.extend)

        added = [e for e in added if not getattr(e, "_addhon_account", False)]
        tks = {getattr(e, "_attr_translation_key", None) for e in added}
        self.assertIn("spin_speed", tks)
        self.assertNotIn("dirty_level", tks)
        self.assertIn("program", tks)  # the program select is unaffected

    async def test_no_option_switch_for_fixed_param(self) -> None:
        from custom_components.addhon import switch
        from custom_components.addhon.const import DOMAIN

        start = RecordingCommand({
            "acquaplus": RangeParam(0, 1, 1),  # settable -> switch
            "prewash": SetParam(["0"]),        # fixed -> NO switch
        })
        commands = {
            "startProgram": start,
            "pauseProgram": RecordingCommand(),
            "resumeProgram": RecordingCommand(),
        }
        coordinator = FakeCoordinator(_washer(commands))
        hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}})
        added: list = []

        await switch.async_setup_entry(hass, FakeEntry(), added.extend)

        added = [e for e in added if not getattr(e, "_addhon_account", False)]
        tks = {getattr(e, "_attr_translation_key", None) for e in added}
        self.assertIn("acquaplus", tks)
        self.assertNotIn("prewash", tks)
        self.assertIn("pause", tks)


class TypeGateDryLevelTest(unittest.IsolatedAsyncioTestCase):
    async def _dry_level_select(self, app_type, param):
        from custom_components.addhon import select
        from custom_components.addhon.const import DOMAIN

        start = RecordingCommand({
            "program": Param(values={"1": "A", "2": "B"}),
            "dryLevel": param,
        })
        coordinator = FakeCoordinator(_washer({"startProgram": start}, app_type=app_type))
        hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}})
        added: list = []
        await select.async_setup_entry(hass, FakeEntry(), added.extend)
        added = [e for e in added if getattr(e, "_attr_translation_key", None) == "dry_level"]
        self.assertEqual(1, len(added))
        return added[0]

    async def test_wm_uses_wm_label_map(self) -> None:
        entity = await self._dry_level_select("WM", RangeParam(1, 3, 1))
        # WM/WD case 300: 1=extra_dry, 2=cupboard, 3=iron_dry.
        self.assertEqual(["extra_dry", "cupboard", "iron_dry"], entity._attr_options)

    async def test_td_uses_td_label_map(self) -> None:
        entity = await self._dry_level_select("TD", RangeParam(12, 14, 1))
        # TD case 53: 12=iron_dry, 13=ready_to_wear, 14=cupboard.
        self.assertEqual(["iron_dry", "ready_to_wear", "cupboard"], entity._attr_options)


if __name__ == "__main__":
    unittest.main()
