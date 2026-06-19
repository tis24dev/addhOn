"""Regression tests for the restored program select and power cleanup.

Covers the Option B fix (regression that appeared in the public release 2.4.0,
the last working one was 2.2):
- the "Programma" select is created/available again even when the program is
  exposed only via startProgram;
- selecting a program does NOT start the cycle (it only sets it);
- the "Avvia programma" button applies the chosen program and clears the choice;
- the legacy "Alimentazione" switch (_power) is removed from the registry.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import enum
import logging
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _ensure_module(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_homeassistant_stubs() -> None:
    homeassistant = _ensure_module("homeassistant")

    config_entries = _ensure_module("homeassistant.config_entries")

    class ConfigEntry:
        pass

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    config_entries.ConfigEntry = getattr(config_entries, "ConfigEntry", ConfigEntry)
    config_entries.ConfigFlow = getattr(config_entries, "ConfigFlow", ConfigFlow)

    core = _ensure_module("homeassistant.core")

    class HomeAssistant:
        pass

    core.HomeAssistant = getattr(core, "HomeAssistant", HomeAssistant)

    exceptions = _ensure_module("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    class ConfigEntryNotReady(HomeAssistantError):
        pass

    class ConfigEntryAuthFailed(HomeAssistantError):
        pass

    exceptions.HomeAssistantError = getattr(exceptions, "HomeAssistantError", HomeAssistantError)
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", ConfigEntryNotReady
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions, "ConfigEntryAuthFailed", ConfigEntryAuthFailed
    )

    helpers = _ensure_module("homeassistant.helpers")
    entity = _ensure_module("homeassistant.helpers.entity")
    entity.DeviceInfo = getattr(entity, "DeviceInfo", dict)

    entity_platform = _ensure_module("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = getattr(
        entity_platform, "AddEntitiesCallback", object
    )

    entity_registry = _ensure_module("homeassistant.helpers.entity_registry")
    # Default no-op; the tests that need them override them.
    entity_registry.async_get = getattr(
        entity_registry, "async_get", lambda hass: None
    )
    entity_registry.async_entries_for_config_entry = getattr(
        entity_registry, "async_entries_for_config_entry", lambda registry, entry_id: []
    )

    update_coordinator = _ensure_module("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
            self.hass = getattr(coordinator, "hass", None)

        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

    class DataUpdateCoordinator:
        pass

    class UpdateFailed(Exception):
        pass

    update_coordinator.CoordinatorEntity = getattr(
        update_coordinator, "CoordinatorEntity", CoordinatorEntity
    )
    update_coordinator.DataUpdateCoordinator = getattr(
        update_coordinator, "DataUpdateCoordinator", DataUpdateCoordinator
    )
    update_coordinator.UpdateFailed = getattr(update_coordinator, "UpdateFailed", UpdateFailed)

    components = _ensure_module("homeassistant.components")
    button_module = _ensure_module("homeassistant.components.button")
    climate_module = _ensure_module("homeassistant.components.climate")
    climate_const = _ensure_module("homeassistant.components.climate.const")
    switch_module = _ensure_module("homeassistant.components.switch")
    select_module = _ensure_module("homeassistant.components.select")

    class SwitchEntity:
        pass

    class ButtonEntity:
        pass

    class ClimateEntity:
        pass

    class ClimateEntityFeature(enum.IntFlag):
        TARGET_TEMPERATURE = 1
        FAN_MODE = 2
        TURN_ON = 4
        TURN_OFF = 8

    class HVACMode(str, enum.Enum):
        OFF = "off"
        AUTO = "auto"
        COOL = "cool"
        DRY = "dry"
        HEAT = "heat"
        FAN_ONLY = "fan_only"

    class SelectEntity:
        pass

    button_module.ButtonEntity = getattr(button_module, "ButtonEntity", ButtonEntity)
    climate_module.ClimateEntity = getattr(climate_module, "ClimateEntity", ClimateEntity)
    climate_const.ClimateEntityFeature = getattr(
        climate_const, "ClimateEntityFeature", ClimateEntityFeature
    )
    climate_const.HVACMode = getattr(climate_const, "HVACMode", HVACMode)
    switch_module.SwitchEntity = getattr(switch_module, "SwitchEntity", SwitchEntity)
    select_module.SelectEntity = getattr(select_module, "SelectEntity", SelectEntity)

    const_module = _ensure_module("homeassistant.const")

    class UnitOfTemperature:
        CELSIUS = "°C"

    const_module.UnitOfTemperature = getattr(
        const_module, "UnitOfTemperature", UnitOfTemperature
    )

    homeassistant.config_entries = config_entries
    homeassistant.const = const_module
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    homeassistant.helpers = helpers
    homeassistant.components = components
    helpers.entity = entity
    helpers.entity_platform = entity_platform
    helpers.entity_registry = entity_registry
    helpers.update_coordinator = update_coordinator
    components.button = button_module
    components.climate = climate_module
    components.switch = switch_module
    components.select = select_module


_install_homeassistant_stubs()


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
    def __init__(self, value=None, values=None) -> None:
        self.value = value
        self.values = values


class RecordingCommand:
    def __init__(self, parameters=None) -> None:
        self.parameters = parameters or {}
        self.send_calls = 0

    async def send(self) -> None:
        self.send_calls += 1


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id


def _washer(commands: dict, attributes: dict | None = None) -> dict:
    return {
        "washer-1": {
            "type": "WM",
            "name": "Washer",
            "appliance": types.SimpleNamespace(commands=commands),
            "attributes": attributes or {},
            "settings": {},
        }
    }


def _ac(commands: dict, attributes: dict | None = None) -> dict:
    return {
        "ac-1": {
            "type": "AC",
            "name": "AC",
            "appliance": types.SimpleNamespace(commands=commands),
            "attributes": attributes or {},
            "settings": {},
        }
    }


class ProgramSelectTest(unittest.IsolatedAsyncioTestCase):
    def _attach(self, entity) -> None:
        entity.hass = FakeHass()

    async def test_select_created_for_start_program_only_appliance(self) -> None:
        from custom_components.addhon.const import DOMAIN
        from custom_components.addhon import select

        commands = {
            "startProgram": RecordingCommand(
                {"program": Param(values={"1": "Cotone", "2": "Sintetici"})}
            )
        }
        coordinator = FakeCoordinator(_washer(commands))
        hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}})
        added: list = []

        await select.async_setup_entry(hass, FakeEntry(), added.extend)

        self.assertEqual(1, len(added))
        self.assertEqual("program", added[0]._attr_translation_key)
        self.assertEqual(["Cotone", "Sintetici"], added[0]._attr_options)

    async def test_select_option_records_pending_without_starting(self) -> None:
        from custom_components.addhon.select import HonProgramSelect

        start = RecordingCommand({"program": Param(values={"1": "Cotone", "2": "Sintetici"})})
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        entity = HonProgramSelect(coordinator, "washer-1", FakeClient())
        self._attach(entity)

        await entity.async_select_option("Sintetici")

        # No command sent: selecting does NOT start the appliance.
        self.assertEqual(0, start.send_calls)
        self.assertEqual(0, coordinator.refreshes)
        # The command parameter is NOT touched during selection.
        self.assertIsNone(start.parameters["program"].value)
        # The choice is stored and reflected right away by current_option.
        self.assertEqual({"washer-1": "2"}, coordinator.pending_programs)
        self.assertEqual("Sintetici", entity.current_option)

    async def test_start_button_applies_pending_and_clears_it(self) -> None:
        from custom_components.addhon.select import HonProgramSelect
        from custom_components.addhon.button import HonProgramCommandButton

        start = RecordingCommand({"program": Param(values={"1": "Cotone", "2": "Sintetici"})})
        coordinator = FakeCoordinator(_washer({"startProgram": start}))

        select_entity = HonProgramSelect(coordinator, "washer-1", FakeClient())
        self._attach(select_entity)
        button = HonProgramCommandButton(
            coordinator,
            "washer-1",
            FakeClient(),
            command_name="startProgram",
            unique_suffix="start_program",
            translation_key="start_program",
            icon="mdi:play-circle",
        )
        self._attach(button)

        await select_entity.async_select_option("Sintetici")
        await button.async_press()

        # The chosen program was applied to startProgram and actually started.
        self.assertEqual("2", start.parameters["program"].value)
        self.assertEqual(1, start.send_calls)
        # The pending choice was cleared: the select goes back to the device state.
        self.assertEqual({}, coordinator.pending_programs)
        self.assertEqual(1, coordinator.refreshes)

    async def test_stop_button_ignores_pending_program(self) -> None:
        from custom_components.addhon.button import HonProgramCommandButton

        stop = RecordingCommand({"onOffStatus": Param("1")})
        coordinator = FakeCoordinator(_washer({"stopProgram": stop}))
        coordinator.pending_programs = {"washer-1": "2"}
        button = HonProgramCommandButton(
            coordinator,
            "washer-1",
            FakeClient(),
            command_name="stopProgram",
            unique_suffix="stop_program",
            translation_key="stop_program",
            icon="mdi:stop-circle",
            command_parameters={"onOffStatus": "0"},
        )
        self._attach(button)

        await button.async_press()

        self.assertEqual(1, stop.send_calls)
        # The stop must not consume or use the pending program choice.
        self.assertEqual({"washer-1": "2"}, coordinator.pending_programs)

    async def test_current_option_reads_device_state_when_no_pending(self) -> None:
        from custom_components.addhon.select import HonProgramSelect

        start = RecordingCommand({"program": Param(values={"1": "Cotone", "2": "Sintetici"})})
        coordinator = FakeCoordinator(_washer({"startProgram": start}, attributes={"prCode": "1"}))
        entity = HonProgramSelect(coordinator, "washer-1", FakeClient())
        self._attach(entity)

        self.assertEqual("Cotone", entity.current_option)

    async def test_current_option_matches_human_label_from_device(self) -> None:
        from custom_components.addhon.select import HonProgramSelect

        start = RecordingCommand({"program": Param(values={"1": "Cotone", "2": "Sintetici"})})
        # The device exposes the program NAME directly, not the code.
        coordinator = FakeCoordinator(
            _washer({"startProgram": start}, attributes={"programName": "Sintetici"})
        )
        entity = HonProgramSelect(coordinator, "washer-1", FakeClient())
        self._attach(entity)

        self.assertEqual("Sintetici", entity.current_option)

    async def test_current_option_resolves_name_without_unmappable_prcode_noise(self) -> None:
        """Real case (production washer/dryer): the options list is built from a
        LIST of names (name->name map), the device publishes a NUMERIC,
        unmappable settings.prCode while the correct name is available via
        startProgram.program. current_option must resolve the name WITHOUT
        stopping or logging 'not mapped' for the numeric code.

        Regression: previously the prCode keys were tried before
        startProgram.program, generating hundreds of misleading DEBUG lines.
        """
        from custom_components.addhon import select
        from custom_components.addhon.select import HonProgramSelect

        start = RecordingCommand({"program": Param(values=["hqd_smart", "hqd_eco"])})
        # The program name is reachable ONLY via startProgram (not as a direct
        # 'program' attribute, nor via settings.program); prCode is numeric.
        data = {
            "washer-1": {
                "type": "WM",
                "name": "Washer",
                "appliance": types.SimpleNamespace(commands={"startProgram": start}),
                "attributes": {"prCode": "124"},
                "settings": {},
                "startProgram": {"program": "hqd_smart"},
            }
        }
        coordinator = FakeCoordinator(data)
        entity = HonProgramSelect(coordinator, "washer-1", FakeClient())
        self._attach(entity)

        with self.assertLogs(select._LOGGER.name, level="DEBUG") as logs:
            result = entity.current_option

        self.assertEqual("hqd_smart", result)
        self.assertFalse(
            any("not mapped" in line for line in logs.output),
            msg=f"current_option must not reach the numeric prCode: {logs.output}",
        )

    async def test_start_button_keeps_pending_when_program_not_applicable(self) -> None:
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.addhon.button import HonProgramCommandButton

        # startProgram without a program parameter: the chosen program is not
        # applicable, so it does NOT start and the pending choice stays.
        start = RecordingCommand({"onOffStatus": Param("1")})
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        coordinator.pending_programs = {"washer-1": "2"}
        button = HonProgramCommandButton(
            coordinator,
            "washer-1",
            FakeClient(),
            command_name="startProgram",
            unique_suffix="start_program",
            translation_key="start_program",
            icon="mdi:play-circle",
        )
        self._attach(button)

        with self.assertRaisesRegex(HomeAssistantError, "not applicable"):
            await button.async_press()

        self.assertEqual(0, start.send_calls)
        self.assertEqual({"washer-1": "2"}, coordinator.pending_programs)

    async def test_unknown_option_raises(self) -> None:
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.addhon.select import HonProgramSelect

        start = RecordingCommand({"program": Param(values={"1": "Cotone"})})
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        entity = HonProgramSelect(coordinator, "washer-1", FakeClient())
        self._attach(entity)

        with self.assertRaisesRegex(HomeAssistantError, "non trovato"):
            await entity.async_select_option("Inesistente")


class LegacyPowerCleanupTest(unittest.TestCase):
    def test_removes_only_power_entities(self) -> None:
        from homeassistant.helpers import entity_registry as er
        from custom_components.addhon import _remove_legacy_entities

        class RegEntry:
            def __init__(self, entity_id, unique_id):
                self.entity_id = entity_id
                self.unique_id = unique_id

        class FakeRegistry:
            def __init__(self, entries):
                self._entries = list(entries)
                self.removed: list = []

            def async_remove(self, entity_id):
                self.removed.append(entity_id)

        registry = FakeRegistry([
            RegEntry("switch.washer_alimentazione", "washer-1_power"),
            RegEntry("switch.washer_pausa", "washer-1_pause"),
            RegEntry("select.washer_programma", "washer-1_program"),
            RegEntry("sensor.washer_energia_totale", "washer-1_total_energy"),
        ])
        # Test-scoped patch: restores the registry's global functions even on
        # failure, so the other tests are not dirtied.
        self.addCleanup(setattr, er, "async_get", er.async_get)
        self.addCleanup(
            setattr, er, "async_entries_for_config_entry", er.async_entries_for_config_entry
        )
        er.async_get = lambda hass: registry
        er.async_entries_for_config_entry = lambda reg, entry_id: list(reg._entries)

        _remove_legacy_entities(FakeHass(), FakeEntry())

        # Only the "Alimentazione" entity (_power) is removed; the others stay.
        self.assertEqual(["switch.washer_alimentazione"], registry.removed)


class GetAttributesStatisticsTest(unittest.TestCase):
    def test_statistics_are_merged_into_attributes(self) -> None:
        from custom_components.addhon.hon_client import _get_attributes

        appliance = types.SimpleNamespace(
            attributes={"parameters": {"machMode": "1"}, "lastConnEvent": "x"},
            settings={"tempSel": "40"},
            statistics={
                "totalElectricityUsed": "123.4",
                "totalWaterUsed": "5000",
                "totalWashCycle": "42",
            },
        )

        attrs = _get_attributes(appliance)

        # The consumption counters from the statistics container are now visible.
        self.assertEqual("123.4", attrs["totalElectricityUsed"])
        self.assertEqual("5000", attrs["totalWaterUsed"])
        self.assertEqual("42", attrs["totalWashCycle"])
        # Real-time attributes and settings keep working.
        self.assertEqual("1", attrs["machMode"])
        self.assertEqual("40", attrs["tempSel"])

    def test_realtime_attributes_win_over_statistics_on_conflict(self) -> None:
        from custom_components.addhon.hon_client import _get_attributes

        appliance = types.SimpleNamespace(
            attributes={"parameters": {"totalElectricityUsed": "999"}},
            settings={},
            statistics={"totalElectricityUsed": "1"},
        )

        attrs = _get_attributes(appliance)

        # On a conflicting key the real-time value wins, not statistics.
        self.assertEqual("999", attrs["totalElectricityUsed"])

    def test_missing_statistics_is_tolerated(self) -> None:
        from custom_components.addhon.hon_client import _get_attributes

        appliance = types.SimpleNamespace(
            attributes={"parameters": {"machMode": "2"}},
            settings={},
        )

        attrs = _get_attributes(appliance)

        self.assertEqual("2", attrs["machMode"])

    def test_update_refreshes_statistics_even_when_attributes_exist(self) -> None:
        from custom_components.addhon.hon_client import HonClient, _get_attributes

        class Appliance:
            nick_name = "Dryer"
            appliance_type = "TD"
            unique_id = "td-1"
            commands = {}
            settings = {}
            statistics = {}
            attributes = {}

            def __init__(self) -> None:
                self.update_calls = 0
                self.statistics_calls = 0

            async def update(self) -> None:
                self.update_calls += 1
                self.attributes = {"parameters": {"machMode": "1"}}

            async def load_statistics(self) -> None:
                self.statistics_calls += 1
                self.statistics = {"programsCounter": "27"}

        appliance = Appliance()
        client = HonClient(email="user@example.com", password="secret")
        client._run_on_hon_loop = lambda coro: asyncio.run(coro)

        client._update_appliance_sync(appliance)

        self.assertEqual(1, appliance.update_calls)
        self.assertEqual(1, appliance.statistics_calls)
        self.assertEqual("27", _get_attributes(appliance)["programsCounter"])


class DebugUtilsTest(unittest.TestCase):
    def test_debug_utils_exports_key_sample_and_rich_param_snapshot(self) -> None:
        from custom_components.addhon.debug_utils import (
            DEBUG_KEY_SAMPLE_LIMIT,
            command_names,
            debug_key_sample,
            param_snapshot,
        )

        values = {f"k{i:03d}": i for i in range(DEBUG_KEY_SAMPLE_LIMIT + 2)}
        self.assertEqual(
            [*sorted(values.keys())[:DEBUG_KEY_SAMPLE_LIMIT], "... (+2)"],
            debug_key_sample(values),
        )

        appliance = types.SimpleNamespace(commands={"z": object(), "a": object()})
        self.assertEqual(["a", "z"], command_names(appliance))
        self.assertEqual([], command_names(types.SimpleNamespace(commands=[])))
        self.assertEqual(
            {"program": {"value": "1", "has_values": True, "values_count": 2}},
            param_snapshot({"program": Param("1", {"1": "Cotone", "2": "Sintetici"})}),
        )
        self.assertEqual({"<non-dict>": "list"}, param_snapshot([]))


class DiagnosticsTest(unittest.IsolatedAsyncioTestCase):
    async def test_config_entry_diagnostics_redacts_email_from_title(self) -> None:
        from custom_components.addhon.const import DOMAIN
        from custom_components.addhon.diagnostics import (
            async_get_config_entry_diagnostics,
        )

        entry = types.SimpleNamespace(
            entry_id="entry-1",
            title="Haier (person@example.com)",
            data={"email": "person@example.com", "password": "secret"},
            options={"scan_interval": 60},
        )
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": FakeCoordinator({})}}}
        )

        diagnostics = await async_get_config_entry_diagnostics(hass, entry)

        self.assertEqual("Haier (***@example.com)", diagnostics["entry"]["title"])
        self.assertNotIn("person", diagnostics["entry"]["title"])


class SwitchLoggingTest(unittest.IsolatedAsyncioTestCase):
    async def test_added_switch_log_only_emitted_when_entity_is_created(self) -> None:
        from custom_components.addhon import switch
        from custom_components.addhon.const import DOMAIN

        missing_resume = {"pauseProgram": RecordingCommand()}
        coordinator = FakeCoordinator(_washer(missing_resume))
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}}
        )
        added: list = []

        with self.assertNoLogs(switch._LOGGER.name, level="INFO"):
            await switch.async_setup_entry(hass, FakeEntry(), added.extend)

        self.assertEqual([], added)

        complete_commands = {
            "pauseProgram": RecordingCommand(),
            "resumeProgram": RecordingCommand(),
        }
        coordinator = FakeCoordinator(_washer(complete_commands))
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}}
        )
        added = []

        with self.assertLogs(switch._LOGGER.name, level="INFO") as logs:
            await switch.async_setup_entry(hass, FakeEntry(), added.extend)

        self.assertEqual(1, len(added))
        self.assertIn("Added switch: Washer", "\n".join(logs.output))


class DebugLoggingGuardTest(unittest.IsolatedAsyncioTestCase):
    def _attach(self, entity) -> None:
        entity.hass = FakeHass()

    def _force_info_logging(self, logger: logging.Logger) -> None:
        original_level = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.setLevel, original_level)

    def _replace_snapshot_with_failure(self, module) -> None:
        # The AC settings send moved to ac_command, which builds no param_snapshot,
        # so the climate module may not expose param_snapshot/_param_snapshot at all.
        # When neither exists there is nothing to guard -> no-op (the send is then
        # only checked for correctness below).
        name = "param_snapshot" if hasattr(module, "param_snapshot") else "_param_snapshot"
        if not hasattr(module, name):
            return
        original = getattr(module, name)

        def fail_if_called(params):
            raise AssertionError("param snapshot should not be built outside DEBUG")

        setattr(module, name, fail_if_called)
        self.addCleanup(setattr, module, name, original)

    async def test_button_does_not_build_param_snapshot_when_debug_is_disabled(self) -> None:
        from custom_components.addhon import button

        start = RecordingCommand({"program": Param(values={"1": "Cotone"})})
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        entity = button.HonProgramCommandButton(
            coordinator,
            "washer-1",
            FakeClient(),
            command_name="startProgram",
            unique_suffix="start_program",
            translation_key="start_program",
            icon="mdi:play-circle",
        )
        self._attach(entity)
        self._force_info_logging(button._LOGGER)
        self._replace_snapshot_with_failure(button)

        await entity.async_press()

        self.assertEqual(1, start.send_calls)

    async def test_climate_does_not_build_param_snapshot_when_debug_is_disabled(self) -> None:
        from custom_components.addhon import climate

        settings = RecordingCommand({"tempSel": Param("20")})
        appliance = types.SimpleNamespace(commands={"settings": settings})
        coordinator = FakeCoordinator(_ac({"settings": settings}))
        entity = climate.HaierClimateEntity(coordinator, "ac-1", FakeClient())
        self._attach(entity)
        self._force_info_logging(climate._LOGGER)
        self._replace_snapshot_with_failure(climate)

        await entity._send_command_in_executor(
            FakeClient(), appliance, {"tempSel": "21"}
        )

        self.assertEqual("21", settings.parameters["tempSel"].value)
        self.assertEqual(1, settings.send_calls)


if __name__ == "__main__":
    unittest.main()
