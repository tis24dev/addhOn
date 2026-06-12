"""Regression tests for hOn client lifecycle cleanup."""
from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_dependency_stubs() -> None:
    """Install minimal stubs for optional Home Assistant test dependencies."""

    voluptuous = types.ModuleType("voluptuous")
    voluptuous.Required = lambda key: key
    voluptuous.Schema = lambda schema: schema

    homeassistant = types.ModuleType("homeassistant")

    config_entries = types.ModuleType("homeassistant.config_entries")

    class ConfigEntry:
        pass

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlow = ConfigFlow

    core = types.ModuleType("homeassistant.core")

    class HomeAssistant:
        pass

    core.HomeAssistant = HomeAssistant

    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict

    exceptions = types.ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    class ConfigEntryNotReady(HomeAssistantError):
        pass

    class ConfigEntryAuthFailed(HomeAssistantError):
        pass

    exceptions.HomeAssistantError = HomeAssistantError
    exceptions.ConfigEntryNotReady = ConfigEntryNotReady
    exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed

    helpers = types.ModuleType("homeassistant.helpers")
    update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        pass

    class UpdateFailed(Exception):
        pass

    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed

    homeassistant.config_entries = config_entries
    homeassistant.core = core
    homeassistant.data_entry_flow = data_entry_flow
    homeassistant.exceptions = exceptions
    homeassistant.helpers = helpers
    helpers.update_coordinator = update_coordinator

    sys.modules.setdefault("voluptuous", voluptuous)
    sys.modules.setdefault("homeassistant", homeassistant)
    sys.modules.setdefault("homeassistant.config_entries", config_entries)
    sys.modules.setdefault("homeassistant.core", core)
    sys.modules.setdefault("homeassistant.data_entry_flow", data_entry_flow)
    sys.modules.setdefault("homeassistant.exceptions", exceptions)
    sys.modules.setdefault("homeassistant.helpers", helpers)
    sys.modules.setdefault("homeassistant.helpers.update_coordinator", update_coordinator)


_install_dependency_stubs()


def _pyhon_stub(hon_cls: type) -> types.ModuleType:
    pyhon = types.ModuleType("pyhon")
    pyhon.Hon = hon_cls
    return pyhon


class HonClientLifecycleTest(unittest.TestCase):
    def test_setup_sync_cleans_loop_when_hon_constructor_fails(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        class FailingHon:
            def __init__(self, email: str, password: str) -> None:
                raise RuntimeError("constructor failed")

        client = HonClient("user@example.com", "secret")
        self.addCleanup(lambda: asyncio.run(client.async_close()))

        with patch.dict(sys.modules, {"pyhon": _pyhon_stub(FailingHon)}):
            with self.assertRaises(RuntimeError):
                client.setup_sync()

        self.assertIsNone(client._hon_loop)
        self.assertIsNone(client._hon_thread)
        self.assertIsNone(client._hon_instance)
        self.assertIsNone(client._api)

    def test_setup_sync_cleans_loop_when_login_fails(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        class FailingHon:
            def __init__(self, email: str, password: str) -> None:
                self.email = email
                self.password = password

            async def __aenter__(self):
                raise RuntimeError("login failed")

            async def __aexit__(self, exc_type, exc, tb):
                return None

        client = HonClient("user@example.com", "secret")
        self.addCleanup(lambda: asyncio.run(client.async_close()))

        with patch.dict(sys.modules, {"pyhon": _pyhon_stub(FailingHon)}):
            with self.assertRaises(RuntimeError):
                client.setup_sync()

        self.assertIsNone(client._hon_loop)
        self.assertIsNone(client._hon_thread)

    def test_async_close_closes_dedicated_loop_object(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        class SuccessfulHon:
            def __init__(self, email: str, password: str) -> None:
                self.email = email
                self.password = password

            async def __aenter__(self):
                return types.SimpleNamespace(appliances=[])

            async def __aexit__(self, exc_type, exc, tb):
                return None

        client = HonClient("user@example.com", "secret")

        with patch.dict(sys.modules, {"pyhon": _pyhon_stub(SuccessfulHon)}):
            client.setup_sync()

        loop = client._hon_loop
        thread = client._hon_thread
        self.assertIsNotNone(loop)
        self.assertIsNotNone(thread)

        try:
            asyncio.run(client.async_close())
            self.assertFalse(thread.is_alive())
            self.assertTrue(loop.is_closed())
        finally:
            if loop is not None and not loop.is_closed():
                loop.close()

    def test_run_on_hon_loop_cancels_task_on_timeout(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        cancelled = threading.Event()
        finished = threading.Event()

        async def slow_coro():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                finished.set()

        client = HonClient("user@example.com", "secret")
        client._RUN_TIMEOUT = 0.01
        client._CANCEL_TIMEOUT = 1
        client._start_hon_loop()

        try:
            with self.assertRaises(concurrent.futures.TimeoutError):
                client._run_on_hon_loop(slow_coro())
            self.assertTrue(cancelled.wait(1))
            self.assertTrue(finished.wait(1))
        finally:
            asyncio.run(client.async_close())

    def test_close_waits_for_in_progress_setup_sync(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        enter_started = threading.Event()
        enter_release = threading.Event()
        close_done = threading.Event()
        exit_called = threading.Event()
        errors: list[BaseException] = []

        class SlowHon:
            def __init__(self, email: str, password: str) -> None:
                self.email = email
                self.password = password

            async def __aenter__(self):
                enter_started.set()
                while not enter_release.is_set():
                    await asyncio.sleep(0.01)
                return types.SimpleNamespace(appliances=[])

            async def __aexit__(self, exc_type, exc, tb):
                exit_called.set()
                return None

        client = HonClient("user@example.com", "secret")

        def run_setup() -> None:
            try:
                client.setup_sync()
            except BaseException as err:
                errors.append(err)

        def run_close() -> None:
            try:
                client._close_sync()
            except BaseException as err:
                errors.append(err)
            finally:
                close_done.set()

        with patch.dict(sys.modules, {"pyhon": _pyhon_stub(SlowHon)}):
            setup_thread = threading.Thread(target=run_setup)
            setup_thread.start()
            self.assertTrue(enter_started.wait(1))

            close_thread = threading.Thread(target=run_close)
            close_thread.start()
            self.assertFalse(close_done.wait(0.05))

            enter_release.set()
            setup_thread.join(timeout=2)
            close_thread.join(timeout=2)

        try:
            self.assertFalse(setup_thread.is_alive())
            self.assertFalse(close_thread.is_alive())
            self.assertEqual([], errors)
            self.assertTrue(close_done.is_set())
            self.assertTrue(exit_called.is_set())
            self.assertIsNone(client._hon_loop)
            self.assertIsNone(client._hon_thread)
        finally:
            asyncio.run(client.async_close())


class ConfigFlowLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_validate_input_closes_client_when_setup_is_cancelled(self) -> None:
        config_flow = importlib.import_module("custom_components.haier_hon.config_flow")

        class FakeHass:
            async def async_add_executor_job(self, func, *args):
                raise asyncio.CancelledError

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                return None

            async def async_complete_setup(self) -> None:
                return None

            async def async_get_appliances(self) -> list:
                return []

            async def async_close(self) -> None:
                self.closed = True

        with patch.object(config_flow, "HonClient", FakeClient):
            with self.assertRaises(asyncio.CancelledError):
                await config_flow.validate_input(
                    FakeHass(), {"email": "user@example.com", "password": "secret"}
                )

        self.assertTrue(FakeClient.instances[0].closed)

    async def test_validate_input_closes_client_when_pyhon_import_fails(self) -> None:
        config_flow = importlib.import_module("custom_components.haier_hon.config_flow")

        class FakeHass:
            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                raise ImportError("missing pyhon")

            async def async_complete_setup(self) -> None:
                return None

            async def async_get_appliances(self) -> list:
                return []

            async def async_close(self) -> None:
                self.closed = True

        with patch.object(config_flow, "HonClient", FakeClient):
            with self.assertRaises(config_flow.CannotConnect):
                await config_flow.validate_input(
                    FakeHass(), {"email": "user@example.com", "password": "secret"}
                )

        self.assertTrue(FakeClient.instances[0].closed)

    async def test_validate_input_closes_client_when_setup_fails(self) -> None:
        config_flow = importlib.import_module("custom_components.haier_hon.config_flow")

        class FakeHass:
            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                raise RuntimeError("bad credentials")

            async def async_complete_setup(self) -> None:
                return None

            async def async_get_appliances(self) -> list:
                return []

            async def async_close(self) -> None:
                self.closed = True

        with patch.object(config_flow, "HonClient", FakeClient):
            with self.assertRaises(config_flow.InvalidAuth):
                await config_flow.validate_input(
                    FakeHass(), {"email": "user@example.com", "password": "secret"}
                )

        self.assertTrue(FakeClient.instances[0].closed)

    async def test_validate_input_closes_client_when_complete_setup_fails(self) -> None:
        config_flow = importlib.import_module("custom_components.haier_hon.config_flow")

        class FakeHass:
            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                return None

            async def async_complete_setup(self) -> None:
                raise RuntimeError("incomplete setup")

            async def async_get_appliances(self) -> list:
                return []

            async def async_close(self) -> None:
                self.closed = True

        with patch.object(config_flow, "HonClient", FakeClient):
            with self.assertRaises(config_flow.CannotConnect):
                await config_flow.validate_input(
                    FakeHass(), {"email": "user@example.com", "password": "secret"}
                )

        self.assertTrue(FakeClient.instances[0].closed)

    async def test_validate_input_maps_appliance_list_auth_failure(self) -> None:
        config_flow = importlib.import_module("custom_components.haier_hon.config_flow")

        class FakeHass:
            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                return None

            async def async_complete_setup(self) -> None:
                return None

            async def async_get_appliances(self) -> list:
                raise RuntimeError("401 unauthorized token expired")

            async def async_close(self) -> None:
                self.closed = True

        with patch.object(config_flow, "HonClient", FakeClient):
            with self.assertRaises(config_flow.InvalidAuth):
                await config_flow.validate_input(
                    FakeHass(), {"email": "user@example.com", "password": "secret"}
                )

        self.assertTrue(FakeClient.instances[0].closed)

    async def test_validate_input_maps_appliance_list_server_failure(self) -> None:
        config_flow = importlib.import_module("custom_components.haier_hon.config_flow")

        class FakeHass:
            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                return None

            async def async_complete_setup(self) -> None:
                return None

            async def async_get_appliances(self) -> list:
                raise RuntimeError("503 internal server error")

            async def async_close(self) -> None:
                self.closed = True

        with patch.object(config_flow, "HonClient", FakeClient):
            with self.assertRaises(config_flow.CannotConnect):
                await config_flow.validate_input(
                    FakeHass(), {"email": "user@example.com", "password": "secret"}
                )

        self.assertTrue(FakeClient.instances[0].closed)


class SetupEntryLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_setup_entry_forwards_button_platform(self) -> None:
        integration = importlib.import_module("custom_components.haier_hon")
        hon_client_module = importlib.import_module("custom_components.haier_hon.hon_client")

        class FakeConfigEntries:
            def __init__(self) -> None:
                self.forwarded_platforms = None

            async def async_forward_entry_setups(self, entry, platforms) -> None:
                self.forwarded_platforms = list(platforms)

        class FakeHass:
            def __init__(self) -> None:
                self.data = {}
                self.config_entries = FakeConfigEntries()

            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeEntry:
            entry_id = "entry-1"
            data = {"email": "user@example.com", "password": "secret"}

        class FakeClient:
            def __init__(self, email: str, password: str) -> None:
                pass

            def setup_sync(self) -> None:
                return None

            async def async_get_appliances_data(self) -> dict:
                return {}

            async def async_close(self) -> None:
                return None

        class SuccessfulCoordinator:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def async_config_entry_first_refresh(self) -> None:
                return None

        hass = FakeHass()

        with (
            patch.object(hon_client_module, "HonClient", FakeClient),
            patch.object(integration, "DataUpdateCoordinator", SuccessfulCoordinator),
        ):
            self.assertTrue(await integration.async_setup_entry(hass, FakeEntry()))

        self.assertEqual(list(integration.PLATFORMS), hass.config_entries.forwarded_platforms)
        self.assertIn("button", hass.config_entries.forwarded_platforms)

    async def test_setup_entry_closes_client_when_setup_is_cancelled(self) -> None:
        integration = importlib.import_module("custom_components.haier_hon")
        hon_client_module = importlib.import_module("custom_components.haier_hon.hon_client")

        class FakeHass:
            def __init__(self) -> None:
                self.data = {}
                self.config_entries = types.SimpleNamespace()

            async def async_add_executor_job(self, func, *args):
                raise asyncio.CancelledError

        class FakeEntry:
            entry_id = "entry-1"
            data = {"email": "user@example.com", "password": "secret"}

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                return None

            async def async_close(self) -> None:
                self.closed = True

        with patch.object(hon_client_module, "HonClient", FakeClient):
            with self.assertRaises(asyncio.CancelledError):
                await integration.async_setup_entry(FakeHass(), FakeEntry())

        self.assertTrue(FakeClient.instances[0].closed)

    async def test_setup_entry_closes_client_when_setup_sync_fails(self) -> None:
        integration = importlib.import_module("custom_components.haier_hon")
        hon_client_module = importlib.import_module("custom_components.haier_hon.hon_client")

        class FakeHass:
            def __init__(self) -> None:
                self.data = {}
                self.config_entries = types.SimpleNamespace()

            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeEntry:
            entry_id = "entry-1"
            data = {"email": "user@example.com", "password": "secret"}

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                raise RuntimeError("connect failed")

            async def async_close(self) -> None:
                self.closed = True

        with patch.object(hon_client_module, "HonClient", FakeClient):
            with self.assertRaises(integration.ConfigEntryNotReady):
                await integration.async_setup_entry(FakeHass(), FakeEntry())

        self.assertTrue(FakeClient.instances[0].closed)

    async def test_setup_entry_raises_auth_failed_when_setup_sync_auth_fails(self) -> None:
        integration = importlib.import_module("custom_components.haier_hon")
        hon_client_module = importlib.import_module("custom_components.haier_hon.hon_client")

        class FakeHass:
            def __init__(self) -> None:
                self.data = {}
                self.config_entries = types.SimpleNamespace()

            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeEntry:
            entry_id = "entry-1"
            data = {"email": "user@example.com", "password": "secret"}

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                raise RuntimeError("401 unauthorized token expired")

            async def async_close(self) -> None:
                self.closed = True

        with patch.object(hon_client_module, "HonClient", FakeClient):
            with self.assertRaises(integration.ConfigEntryAuthFailed):
                await integration.async_setup_entry(FakeHass(), FakeEntry())

        self.assertTrue(FakeClient.instances[0].closed)

    async def test_setup_entry_closes_client_when_first_refresh_is_cancelled(self) -> None:
        integration = importlib.import_module("custom_components.haier_hon")
        hon_client_module = importlib.import_module("custom_components.haier_hon.hon_client")

        class FakeHass:
            def __init__(self) -> None:
                self.data = {}
                self.config_entries = types.SimpleNamespace()

            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeEntry:
            entry_id = "entry-1"
            data = {"email": "user@example.com", "password": "secret"}

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                return None

            async def async_get_appliances_data(self) -> dict:
                return {}

            async def async_close(self) -> None:
                self.closed = True

        class CancellingCoordinator:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def async_config_entry_first_refresh(self) -> None:
                raise asyncio.CancelledError

        hass = FakeHass()
        entry = FakeEntry()

        with (
            patch.object(hon_client_module, "HonClient", FakeClient),
            patch.object(integration, "DataUpdateCoordinator", CancellingCoordinator),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await integration.async_setup_entry(hass, entry)

        self.assertTrue(FakeClient.instances[0].closed)
        self.assertNotIn(entry.entry_id, hass.data.get(integration.DOMAIN, {}))

    async def test_setup_entry_closes_client_when_first_refresh_fails(self) -> None:
        integration = importlib.import_module("custom_components.haier_hon")
        hon_client_module = importlib.import_module("custom_components.haier_hon.hon_client")

        class FakeHass:
            def __init__(self) -> None:
                self.data = {}
                self.config_entries = types.SimpleNamespace()

            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeEntry:
            entry_id = "entry-1"
            data = {"email": "user@example.com", "password": "secret"}

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                return None

            async def async_get_appliances_data(self) -> dict:
                return {}

            async def async_close(self) -> None:
                self.closed = True

        class FailingCoordinator:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def async_config_entry_first_refresh(self) -> None:
                raise RuntimeError("refresh failed")

        with (
            patch.object(hon_client_module, "HonClient", FakeClient),
            patch.object(integration, "DataUpdateCoordinator", FailingCoordinator),
        ):
            with self.assertRaises(RuntimeError):
                await integration.async_setup_entry(FakeHass(), FakeEntry())

        self.assertTrue(FakeClient.instances[0].closed)

    async def test_setup_entry_raises_auth_failed_when_first_refresh_auth_fails(self) -> None:
        integration = importlib.import_module("custom_components.haier_hon")
        hon_client_module = importlib.import_module("custom_components.haier_hon.hon_client")

        class FakeHass:
            def __init__(self) -> None:
                self.data = {}
                self.config_entries = types.SimpleNamespace()

            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeEntry:
            entry_id = "entry-1"
            data = {"email": "user@example.com", "password": "secret"}

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                return None

            async def async_get_appliances_data(self) -> dict:
                raise RuntimeError("401 unauthorized token expired")

            async def async_close(self) -> None:
                self.closed = True

        class RefreshingCoordinator:
            def __init__(self, *args, **kwargs) -> None:
                self.update_method = kwargs["update_method"]

            async def async_config_entry_first_refresh(self) -> None:
                await self.update_method()

        with (
            patch.object(hon_client_module, "HonClient", FakeClient),
            patch.object(integration, "DataUpdateCoordinator", RefreshingCoordinator),
        ):
            with self.assertRaises(integration.ConfigEntryAuthFailed):
                await integration.async_setup_entry(FakeHass(), FakeEntry())

        self.assertTrue(FakeClient.instances[0].closed)

    async def test_setup_entry_closes_and_unstores_client_when_forwarding_is_cancelled(self) -> None:
        integration = importlib.import_module("custom_components.haier_hon")
        hon_client_module = importlib.import_module("custom_components.haier_hon.hon_client")

        class FakeConfigEntries:
            def __init__(self) -> None:
                self.unloaded = False

            async def async_forward_entry_setups(self, entry, platforms) -> None:
                raise asyncio.CancelledError

            async def async_unload_platforms(self, entry, platforms) -> bool:
                self.unloaded = True
                return True

        class FakeHass:
            def __init__(self) -> None:
                self.data = {}
                self.config_entries = FakeConfigEntries()

            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeEntry:
            entry_id = "entry-1"
            data = {"email": "user@example.com", "password": "secret"}

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                return None

            async def async_get_appliances_data(self) -> dict:
                return {}

            async def async_close(self) -> None:
                self.closed = True

        class SuccessfulCoordinator:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def async_config_entry_first_refresh(self) -> None:
                return None

        hass = FakeHass()
        entry = FakeEntry()

        with (
            patch.object(hon_client_module, "HonClient", FakeClient),
            patch.object(integration, "DataUpdateCoordinator", SuccessfulCoordinator),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await integration.async_setup_entry(hass, entry)

        self.assertTrue(FakeClient.instances[0].closed)
        self.assertTrue(hass.config_entries.unloaded)
        self.assertNotIn(entry.entry_id, hass.data.get(integration.DOMAIN, {}))

    async def test_setup_entry_closes_and_unstores_client_when_forwarding_fails(self) -> None:
        integration = importlib.import_module("custom_components.haier_hon")
        hon_client_module = importlib.import_module("custom_components.haier_hon.hon_client")

        class FakeConfigEntries:
            async def async_forward_entry_setups(self, entry, platforms) -> None:
                raise RuntimeError("forward failed")

        class FakeHass:
            def __init__(self) -> None:
                self.data = {}
                self.config_entries = FakeConfigEntries()

            async def async_add_executor_job(self, func, *args):
                return func(*args)

        class FakeEntry:
            entry_id = "entry-1"
            data = {"email": "user@example.com", "password": "secret"}

        class FakeClient:
            instances = []

            def __init__(self, email: str, password: str) -> None:
                self.closed = False
                self.__class__.instances.append(self)

            def setup_sync(self) -> None:
                return None

            async def async_get_appliances_data(self) -> dict:
                return {}

            async def async_close(self) -> None:
                self.closed = True

        class SuccessfulCoordinator:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def async_config_entry_first_refresh(self) -> None:
                return None

        hass = FakeHass()
        entry = FakeEntry()

        with (
            patch.object(hon_client_module, "HonClient", FakeClient),
            patch.object(integration, "DataUpdateCoordinator", SuccessfulCoordinator),
        ):
            with self.assertRaises(RuntimeError):
                await integration.async_setup_entry(hass, entry)

        self.assertTrue(FakeClient.instances[0].closed)
        self.assertNotIn(entry.entry_id, hass.data.get(integration.DOMAIN, {}))


if __name__ == "__main__":
    unittest.main()
