"""Regression tests for the restored program select and power cleanup.

Copre la fix Opzione B (regressione comparsa nella release pubblica 2.4.0,
l'ultima funzionante era la 2.2):
- il select "Programma" torna a essere creato/disponibile anche quando il
  programma è esposto solo via startProgram;
- selezionare un programma NON avvia il ciclo (imposta e basta);
- il pulsante "Avvia programma" applica il programma scelto e svuota la scelta;
- lo switch legacy "Alimentazione" (_power) viene rimosso dal registry.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import enum
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
    # Default no-op; i test che servono li sovrascrivono.
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
    switch_module = _ensure_module("homeassistant.components.switch")
    select_module = _ensure_module("homeassistant.components.select")

    class SwitchEntity:
        pass

    class ButtonEntity:
        pass

    class SelectEntity:
        pass

    button_module.ButtonEntity = getattr(button_module, "ButtonEntity", ButtonEntity)
    switch_module.SwitchEntity = getattr(switch_module, "SwitchEntity", SwitchEntity)
    select_module.SelectEntity = getattr(select_module, "SelectEntity", SelectEntity)

    homeassistant.config_entries = config_entries
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    homeassistant.helpers = helpers
    homeassistant.components = components
    helpers.entity = entity
    helpers.entity_platform = entity_platform
    helpers.entity_registry = entity_registry
    helpers.update_coordinator = update_coordinator
    components.button = button_module
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


class ProgramSelectTest(unittest.IsolatedAsyncioTestCase):
    def _attach(self, entity) -> None:
        entity.hass = FakeHass()

    async def test_select_created_for_start_program_only_appliance(self) -> None:
        from custom_components.haier_hon.const import DOMAIN
        from custom_components.haier_hon import select

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
        self.assertEqual("Washer - Programma", added[0]._attr_name)
        self.assertEqual(["Cotone", "Sintetici"], added[0]._attr_options)

    async def test_select_option_records_pending_without_starting(self) -> None:
        from custom_components.haier_hon.select import HonProgramSelect

        start = RecordingCommand({"program": Param(values={"1": "Cotone", "2": "Sintetici"})})
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        entity = HonProgramSelect(coordinator, "washer-1", FakeClient())
        self._attach(entity)

        await entity.async_select_option("Sintetici")

        # Nessun comando inviato: selezionare NON avvia l'elettrodomestico.
        self.assertEqual(0, start.send_calls)
        self.assertEqual(0, coordinator.refreshes)
        # Il parametro del comando NON è stato toccato in fase di selezione.
        self.assertIsNone(start.parameters["program"].value)
        # La scelta è memorizzata e riflessa subito da current_option.
        self.assertEqual({"washer-1": "2"}, coordinator.pending_programs)
        self.assertEqual("Sintetici", entity.current_option)

    async def test_start_button_applies_pending_and_clears_it(self) -> None:
        from custom_components.haier_hon.select import HonProgramSelect
        from custom_components.haier_hon.button import HonProgramCommandButton

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
            name_suffix="Avvia programma",
            icon="mdi:play-circle",
        )
        self._attach(button)

        await select_entity.async_select_option("Sintetici")
        await button.async_press()

        # Il programma scelto è stato applicato a startProgram ed effettivamente avviato.
        self.assertEqual("2", start.parameters["program"].value)
        self.assertEqual(1, start.send_calls)
        # La scelta in attesa è stata svuotata: il select torna allo stato device.
        self.assertEqual({}, coordinator.pending_programs)
        self.assertEqual(1, coordinator.refreshes)

    async def test_stop_button_ignores_pending_program(self) -> None:
        from custom_components.haier_hon.button import HonProgramCommandButton

        stop = RecordingCommand({"onOffStatus": Param("1")})
        coordinator = FakeCoordinator(_washer({"stopProgram": stop}))
        coordinator.pending_programs = {"washer-1": "2"}
        button = HonProgramCommandButton(
            coordinator,
            "washer-1",
            FakeClient(),
            command_name="stopProgram",
            unique_suffix="stop_program",
            name_suffix="Ferma programma",
            icon="mdi:stop-circle",
            command_parameters={"onOffStatus": "0"},
        )
        self._attach(button)

        await button.async_press()

        self.assertEqual(1, stop.send_calls)
        # Lo stop non deve consumare né usare la scelta programma in attesa.
        self.assertEqual({"washer-1": "2"}, coordinator.pending_programs)

    async def test_current_option_reads_device_state_when_no_pending(self) -> None:
        from custom_components.haier_hon.select import HonProgramSelect

        start = RecordingCommand({"program": Param(values={"1": "Cotone", "2": "Sintetici"})})
        coordinator = FakeCoordinator(_washer({"startProgram": start}, attributes={"prCode": "1"}))
        entity = HonProgramSelect(coordinator, "washer-1", FakeClient())
        self._attach(entity)

        self.assertEqual("Cotone", entity.current_option)

    async def test_current_option_matches_human_label_from_device(self) -> None:
        from custom_components.haier_hon.select import HonProgramSelect

        start = RecordingCommand({"program": Param(values={"1": "Cotone", "2": "Sintetici"})})
        # Il device espone direttamente il NOME del programma, non il codice.
        coordinator = FakeCoordinator(
            _washer({"startProgram": start}, attributes={"programName": "Sintetici"})
        )
        entity = HonProgramSelect(coordinator, "washer-1", FakeClient())
        self._attach(entity)

        self.assertEqual("Sintetici", entity.current_option)

    async def test_start_button_keeps_pending_when_program_not_applicable(self) -> None:
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.haier_hon.button import HonProgramCommandButton

        # startProgram senza parametro programma: il programma scelto non è
        # applicabile, quindi NON si avvia e la scelta in attesa resta.
        start = RecordingCommand({"onOffStatus": Param("1")})
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        coordinator.pending_programs = {"washer-1": "2"}
        button = HonProgramCommandButton(
            coordinator,
            "washer-1",
            FakeClient(),
            command_name="startProgram",
            unique_suffix="start_program",
            name_suffix="Avvia programma",
            icon="mdi:play-circle",
        )
        self._attach(button)

        with self.assertRaisesRegex(HomeAssistantError, "non applicabile"):
            await button.async_press()

        self.assertEqual(0, start.send_calls)
        self.assertEqual({"washer-1": "2"}, coordinator.pending_programs)

    async def test_unknown_option_raises(self) -> None:
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.haier_hon.select import HonProgramSelect

        start = RecordingCommand({"program": Param(values={"1": "Cotone"})})
        coordinator = FakeCoordinator(_washer({"startProgram": start}))
        entity = HonProgramSelect(coordinator, "washer-1", FakeClient())
        self._attach(entity)

        with self.assertRaisesRegex(HomeAssistantError, "non trovato"):
            await entity.async_select_option("Inesistente")


class LegacyPowerCleanupTest(unittest.TestCase):
    def test_removes_only_power_entities(self) -> None:
        from homeassistant.helpers import entity_registry as er
        from custom_components.haier_hon import _remove_legacy_entities

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
        # Patch limitata al test: ripristina le funzioni globali del registry
        # anche in caso di fallimento, per non sporcare gli altri test.
        self.addCleanup(setattr, er, "async_get", er.async_get)
        self.addCleanup(
            setattr, er, "async_entries_for_config_entry", er.async_entries_for_config_entry
        )
        er.async_get = lambda hass: registry
        er.async_entries_for_config_entry = lambda reg, entry_id: list(reg._entries)

        _remove_legacy_entities(FakeHass(), FakeEntry())

        # Solo l'entità "Alimentazione" (_power) viene rimossa; le altre restano.
        self.assertEqual(["switch.washer_alimentazione"], registry.removed)


if __name__ == "__main__":
    unittest.main()
