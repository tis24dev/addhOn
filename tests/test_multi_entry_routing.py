"""Regression tests for multi-entry command routing."""
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

    config_entries.ConfigEntry = getattr(config_entries, "ConfigEntry", ConfigEntry)

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

    update_coordinator = _ensure_module("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
            self.hass = getattr(coordinator, "hass", None)

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
    button = _ensure_module("homeassistant.components.button")

    class ButtonEntity:
        pass

    button.ButtonEntity = getattr(button, "ButtonEntity", ButtonEntity)

    homeassistant.config_entries = config_entries
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    homeassistant.helpers = helpers
    helpers.entity = entity
    helpers.entity_platform = entity_platform
    helpers.update_coordinator = update_coordinator
    homeassistant.components = components
    components.button = button


_install_homeassistant_stubs()


class FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def run_command_sync(self, coro) -> None:
        self.calls += 1
        asyncio.run(coro)


class FakeCommand:
    def __init__(self) -> None:
        self.sent = 0

    async def send(self) -> None:
        self.sent += 1


class FakeCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.hass = None
        self.refreshes = 0

    async def async_refresh(self) -> None:
        self.refreshes += 1

    async def async_request_refresh(self) -> None:
        self.refreshes += 1


class FakeHass:
    def __init__(self, data: dict) -> None:
        self.data = data

    async def async_add_executor_job(self, func, *args):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(func, *args).result(timeout=5)


class FakeEntry:
    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id


class MultiEntryRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_button_uses_client_from_own_config_entry(self) -> None:
        from custom_components.haier_hon.const import DOMAIN
        from custom_components.haier_hon import button

        client_a = FakeClient()
        client_b = FakeClient()
        command_b = FakeCommand()
        appliance_b = types.SimpleNamespace(commands={"startProgram": command_b})

        coordinator_a = FakeCoordinator({})
        coordinator_b = FakeCoordinator(
            {
                "washer-b": {
                    "type": "WM",
                    "name": "Washer B",
                    "appliance": appliance_b,
                    "attributes": {"machMode": "0"},
                    "settings": {},
                }
            }
        )

        hass = FakeHass(
            {
                DOMAIN: {
                    "entry-a": {"coordinator": coordinator_a, "client": client_a},
                    "entry-b": {"coordinator": coordinator_b, "client": client_b},
                }
            }
        )
        coordinator_a.hass = hass
        coordinator_b.hass = hass
        added_entities = []

        def add_entities(entities) -> None:
            for entity in entities:
                entity.hass = hass
            added_entities.extend(entities)

        await button.async_setup_entry(hass, FakeEntry("entry-b"), add_entities)
        self.assertEqual(1, len(added_entities))
        self.assertEqual({(DOMAIN, "washer-b")}, added_entities[0].device_info["identifiers"])

        await added_entities[0].async_press()

        self.assertEqual(0, client_a.calls)
        self.assertEqual(1, client_b.calls)
        self.assertEqual(1, command_b.sent)
        self.assertEqual(1, coordinator_b.refreshes)


if __name__ == "__main__":
    unittest.main()
