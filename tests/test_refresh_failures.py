"""Regression tests for refresh failures that must not look successful."""
from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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
    update_coordinator = _ensure_module("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        pass

    class UpdateFailed(Exception):
        pass

    update_coordinator.DataUpdateCoordinator = getattr(
        update_coordinator, "DataUpdateCoordinator", DataUpdateCoordinator
    )
    update_coordinator.UpdateFailed = getattr(update_coordinator, "UpdateFailed", UpdateFailed)

    homeassistant.config_entries = config_entries
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    homeassistant.helpers = helpers
    helpers.update_coordinator = update_coordinator


_install_homeassistant_stubs()


class BrokenApi:
    @property
    def appliances(self):
        raise RuntimeError("appliance list unavailable")


class InlineExecutorLoop:
    async def run_in_executor(self, executor, func, *args):
        return func(*args)


class RefreshFailureTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_api_raises_instead_of_zero_appliances(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        client = HonClient("user@example.com", "secret")

        with self.assertRaisesRegex(RuntimeError, "hOn session"):
            await client.async_get_appliances()

    async def test_appliance_list_error_raises_instead_of_zero_appliances(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        client = HonClient("user@example.com", "secret")
        client._api = BrokenApi()

        with self.assertRaisesRegex(RuntimeError, "appliance list unavailable"):
            await client.async_get_appliances()

    async def test_empty_appliance_list_is_successful_zero_appliances(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        client = HonClient("user@example.com", "secret")
        client._api = types.SimpleNamespace(appliances=[])

        self.assertEqual([], await client.async_get_appliances())
        self.assertEqual({}, await client.async_get_appliances_data())

    async def test_partial_appliance_update_failure_raises(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        client = HonClient("user@example.com", "secret")
        good = types.SimpleNamespace(
            unique_id="good",
            attributes={},
            settings={},
            nick_name="Good washer",
            appliance_type="WM",
            model_name="Model",
        )
        bad = types.SimpleNamespace(
            unique_id="bad",
            attributes={},
            settings={},
            nick_name="Bad washer",
            appliance_type="WM",
            model_name="Model",
        )
        client._api = types.SimpleNamespace(appliances=[good, bad])

        def update_or_fail(appliance) -> None:
            if appliance is bad:
                raise RuntimeError("update failed for bad")

        client._update_appliance_sync = update_or_fail

        with (
            patch(
                "custom_components.haier_hon.hon_client.asyncio.get_running_loop",
                return_value=InlineExecutorLoop(),
            ),
            self.assertRaisesRegex(RuntimeError, "update failed for bad"),
        ):
            await client.async_get_appliances_data()

    def test_partial_fallback_update_failure_raises(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        class PartiallyLoadedAppliance:
            unique_id = "partial"
            attributes = {}
            settings = {}
            nick_name = "Partial washer"
            appliance_type = "WM"
            model_name = "Model"

            async def update(self) -> None:
                raise RuntimeError("primary update failed")

            async def load_attributes(self) -> None:
                self.attributes["machMode"] = 1

            async def load_commands(self) -> None:
                raise RuntimeError("commands load failed")

        client = HonClient("user@example.com", "secret")
        client._run_on_hon_loop = lambda coro: asyncio.run(coro)

        with self.assertRaisesRegex(RuntimeError, "commands load failed"):
            client._update_appliance_sync(PartiallyLoadedAppliance())

    def test_empty_update_result_uses_fallback_loaders(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        class EmptyUpdateAppliance:
            attributes = {}
            settings = {}

            async def update(self) -> None:
                return None

            async def load_attributes(self) -> None:
                self.attributes["machMode"] = "1"

        appliance = EmptyUpdateAppliance()
        client = HonClient("user@example.com", "secret")
        client._run_on_hon_loop = lambda coro: asyncio.run(coro)

        client._update_appliance_sync(appliance)

        self.assertEqual({"machMode": "1"}, appliance.attributes)

    def test_auth_update_error_is_not_masked_by_successful_fallback(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        class AuthFailingUpdateAppliance:
            attributes = {}
            settings = {}

            async def update(self) -> None:
                raise RuntimeError("401 unauthorized token expired")

            async def load_attributes(self) -> None:
                self.attributes["machMode"] = "1"

        appliance = AuthFailingUpdateAppliance()
        client = HonClient("user@example.com", "secret")
        client._run_on_hon_loop = lambda coro: asyncio.run(coro)

        with self.assertRaisesRegex(RuntimeError, "401 unauthorized"):
            client._update_appliance_sync(appliance)

        self.assertEqual({}, appliance.attributes)

    def test_auth_update_error_is_not_replaced_by_missing_fallback_error(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        class AuthFailingUpdateAppliance:
            attributes = {}
            settings = {}

            async def update(self) -> None:
                raise RuntimeError("401 unauthorized token expired")

        client = HonClient("user@example.com", "secret")
        client._run_on_hon_loop = lambda coro: asyncio.run(coro)

        with self.assertRaisesRegex(RuntimeError, "401 unauthorized"):
            client._update_appliance_sync(AuthFailingUpdateAppliance())

    def test_missing_session_update_error_is_not_masked_by_successful_fallback(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        class MissingSessionUpdateAppliance:
            attributes = {}
            settings = {}

            async def update(self) -> None:
                raise RuntimeError("session unavailable")

            async def load_attributes(self) -> None:
                self.attributes["machMode"] = "1"

        appliance = MissingSessionUpdateAppliance()
        client = HonClient("user@example.com", "secret")
        client._run_on_hon_loop = lambda coro: asyncio.run(coro)

        with self.assertRaisesRegex(RuntimeError, "session unavailable"):
            client._update_appliance_sync(appliance)

        self.assertEqual({}, appliance.attributes)

    async def test_auth_update_failure_raises_when_reauth_fails(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        appliance = types.SimpleNamespace(
            unique_id="auth-failure",
            attributes={},
            settings={},
            nick_name="Auth washer",
            appliance_type="WM",
            model_name="Model",
        )
        client = HonClient("user@example.com", "secret")
        client._api = types.SimpleNamespace(appliances=[appliance])
        client._update_appliance_sync = lambda appliance: (_ for _ in ()).throw(
            RuntimeError("auth failed")
        )

        async def reauth_failed() -> bool:
            return False

        client._async_reauth = reauth_failed

        with (
            patch(
                "custom_components.haier_hon.hon_client.asyncio.get_running_loop",
                return_value=InlineExecutorLoop(),
            ),
            self.assertRaisesRegex(RuntimeError, "auth failed"),
        ):
            await client.async_get_appliances_data()

    async def test_successful_reauth_retries_failed_refresh(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        appliance = types.SimpleNamespace(
            unique_id="reauth-retry",
            attributes={},
            settings={},
            nick_name="Retry washer",
            appliance_type="WM",
            model_name="Model",
        )
        client = HonClient("user@example.com", "secret")
        client._api = types.SimpleNamespace(appliances=[appliance])
        update_calls = 0

        def update_then_succeed(appliance) -> None:
            nonlocal update_calls
            update_calls += 1
            if update_calls == 1:
                raise RuntimeError("auth failed")

        async def reauth_succeeded() -> bool:
            return True

        client._update_appliance_sync = update_then_succeed
        client._async_reauth = reauth_succeeded

        with patch(
            "custom_components.haier_hon.hon_client.asyncio.get_running_loop",
            return_value=InlineExecutorLoop(),
        ):
            data = await client.async_get_appliances_data()

        self.assertEqual(2, update_calls)
        self.assertIn("reauth-retry", data)

    async def test_auth_error_reauths_without_server_retry_delay(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        appliance = types.SimpleNamespace(
            unique_id="auth-classification",
            attributes={},
            settings={},
            nick_name="Auth classified washer",
            appliance_type="WM",
            model_name="Model",
        )
        client = HonClient("user@example.com", "secret")
        client._api = types.SimpleNamespace(appliances=[appliance])
        update_calls = 0
        reauth_calls = 0
        sleep_calls = []
        reauthed = False

        def update_after_reauth(appliance) -> None:
            nonlocal update_calls
            update_calls += 1
            if not reauthed:
                raise RuntimeError("401 unauthorized token expired")

        async def reauth_succeeded() -> bool:
            nonlocal reauth_calls, reauthed
            reauth_calls += 1
            reauthed = True
            return True

        async def fake_sleep(seconds) -> None:
            sleep_calls.append(seconds)

        client._update_appliance_sync = update_after_reauth
        client._async_reauth = reauth_succeeded

        with (
            patch(
                "custom_components.haier_hon.hon_client.asyncio.get_running_loop",
                return_value=InlineExecutorLoop(),
            ),
            patch("custom_components.haier_hon.hon_client.asyncio.sleep", fake_sleep),
        ):
            data = await client.async_get_appliances_data()

        self.assertEqual(1, reauth_calls)
        self.assertEqual(2, update_calls)
        self.assertEqual([], sleep_calls)
        self.assertIn("auth-classification", data)

    async def test_server_error_retries_without_reauth(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        appliance = types.SimpleNamespace(
            unique_id="server-retry",
            attributes={},
            settings={},
            nick_name="Server retry washer",
            appliance_type="WM",
            model_name="Model",
        )
        client = HonClient("user@example.com", "secret")
        client._api = types.SimpleNamespace(appliances=[appliance])
        update_calls = 0
        reauth_calls = 0
        sleep_calls = []

        def update_after_server_retries(appliance) -> None:
            nonlocal update_calls
            update_calls += 1
            if update_calls < 3:
                raise RuntimeError("503 internal server error")

        async def unexpected_reauth() -> bool:
            nonlocal reauth_calls
            reauth_calls += 1
            return True

        async def fake_sleep(seconds) -> None:
            sleep_calls.append(seconds)

        client._update_appliance_sync = update_after_server_retries
        client._async_reauth = unexpected_reauth

        with (
            patch(
                "custom_components.haier_hon.hon_client.asyncio.get_running_loop",
                return_value=InlineExecutorLoop(),
            ),
            patch("custom_components.haier_hon.hon_client.asyncio.sleep", fake_sleep),
        ):
            data = await client.async_get_appliances_data()

        self.assertEqual(0, reauth_calls)
        self.assertEqual(3, update_calls)
        self.assertEqual([5, 10], sleep_calls)
        self.assertIn("server-retry", data)

    async def test_server_error_with_auth_text_retries_without_reauth(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        appliance = types.SimpleNamespace(
            unique_id="server-auth-overlap",
            attributes={},
            settings={},
            nick_name="Server auth overlap washer",
            appliance_type="WM",
            model_name="Model",
        )
        client = HonClient("user@example.com", "secret")
        client._api = types.SimpleNamespace(appliances=[appliance])
        update_calls = 0
        reauth_calls = 0
        sleep_calls = []

        def update_after_server_retries(appliance) -> None:
            nonlocal update_calls
            update_calls += 1
            if update_calls < 3:
                raise RuntimeError("503 auth service temporarily unavailable")

        async def unexpected_reauth() -> bool:
            nonlocal reauth_calls
            reauth_calls += 1
            return True

        async def fake_sleep(seconds) -> None:
            sleep_calls.append(seconds)

        client._update_appliance_sync = update_after_server_retries
        client._async_reauth = unexpected_reauth

        with (
            patch(
                "custom_components.haier_hon.hon_client.asyncio.get_running_loop",
                return_value=InlineExecutorLoop(),
            ),
            patch("custom_components.haier_hon.hon_client.asyncio.sleep", fake_sleep),
        ):
            data = await client.async_get_appliances_data()

        self.assertEqual(0, reauth_calls)
        self.assertEqual(3, update_calls)
        self.assertEqual([5, 10], sleep_calls)
        self.assertIn("server-auth-overlap", data)

    async def test_timeout_error_retries_without_reauth(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        appliance = types.SimpleNamespace(
            unique_id="timeout-retry",
            attributes={},
            settings={},
            nick_name="Timeout retry washer",
            appliance_type="WM",
            model_name="Model",
        )
        client = HonClient("user@example.com", "secret")
        client._api = types.SimpleNamespace(appliances=[appliance])
        update_calls = 0
        reauth_calls = 0
        sleep_calls = []

        def update_after_timeouts(appliance) -> None:
            nonlocal update_calls
            update_calls += 1
            if update_calls < 3:
                raise concurrent.futures.TimeoutError()

        async def unexpected_reauth() -> bool:
            nonlocal reauth_calls
            reauth_calls += 1
            return True

        async def fake_sleep(seconds) -> None:
            sleep_calls.append(seconds)

        client._update_appliance_sync = update_after_timeouts
        client._async_reauth = unexpected_reauth

        with (
            patch(
                "custom_components.haier_hon.hon_client.asyncio.get_running_loop",
                return_value=InlineExecutorLoop(),
            ),
            patch("custom_components.haier_hon.hon_client.asyncio.sleep", fake_sleep),
        ):
            data = await client.async_get_appliances_data()

        self.assertEqual(0, reauth_calls)
        self.assertEqual(3, update_calls)
        self.assertEqual([5, 10], sleep_calls)
        self.assertIn("timeout-retry", data)

    async def test_update_missing_session_error_reauths_and_retries(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        appliance = types.SimpleNamespace(
            unique_id="update-missing-session",
            attributes={},
            settings={},
            nick_name="Update missing session washer",
            appliance_type="WM",
            model_name="Model",
        )
        client = HonClient("user@example.com", "secret")
        client._api = types.SimpleNamespace(appliances=[appliance])
        update_calls = 0
        reauth_calls = 0
        reauthed = False

        def update_after_reauth(appliance) -> None:
            nonlocal update_calls
            update_calls += 1
            if not reauthed:
                raise RuntimeError("session unavailable")

        async def reauth_succeeded() -> bool:
            nonlocal reauth_calls, reauthed
            reauth_calls += 1
            reauthed = True
            return True

        client._update_appliance_sync = update_after_reauth
        client._async_reauth = reauth_succeeded

        with patch(
            "custom_components.haier_hon.hon_client.asyncio.get_running_loop",
            return_value=InlineExecutorLoop(),
        ):
            data = await client.async_get_appliances_data()

        self.assertEqual(1, reauth_calls)
        self.assertEqual(2, update_calls)
        self.assertIn("update-missing-session", data)

    async def test_auth_appliance_list_error_reauths_and_retries(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        appliance = types.SimpleNamespace(
            unique_id="list-reauth",
            attributes={"machMode": "1"},
            settings={},
            nick_name="List reauth washer",
            appliance_type="WM",
            model_name="Model",
        )

        class AuthFailedApi:
            @property
            def appliances(self):
                raise RuntimeError("401 unauthorized token expired")

        client = HonClient("user@example.com", "secret")
        client._api = AuthFailedApi()
        reauth_calls = 0

        async def reauth_succeeded() -> bool:
            nonlocal reauth_calls
            reauth_calls += 1
            client._api = types.SimpleNamespace(appliances=[appliance])
            return True

        client._async_reauth = reauth_succeeded
        client._update_appliance_sync = lambda appliance: None

        with patch(
            "custom_components.haier_hon.hon_client.asyncio.get_running_loop",
            return_value=InlineExecutorLoop(),
        ):
            data = await client.async_get_appliances_data()

        self.assertEqual(1, reauth_calls)
        self.assertIn("list-reauth", data)

    async def test_missing_session_after_failed_reauth_can_reauth_next_refresh(self) -> None:
        from custom_components.haier_hon.hon_client import HonClient

        appliance = types.SimpleNamespace(
            unique_id="reauth-after-missing-session",
            attributes={},
            settings={},
            nick_name="Reauth after missing session washer",
            appliance_type="WM",
            model_name="Model",
        )
        client = HonClient("user@example.com", "secret")
        client._api = types.SimpleNamespace(appliances=[appliance])
        reauth_calls = 0

        def auth_update_failure(appliance) -> None:
            raise RuntimeError("401 unauthorized token expired")

        async def failed_reauth_clears_session() -> bool:
            nonlocal reauth_calls
            reauth_calls += 1
            client._api = None
            return False

        client._update_appliance_sync = auth_update_failure
        client._async_reauth = failed_reauth_clears_session

        with (
            patch(
                "custom_components.haier_hon.hon_client.asyncio.get_running_loop",
                return_value=InlineExecutorLoop(),
            ),
            self.assertRaisesRegex(RuntimeError, "401 unauthorized"),
        ):
            await client.async_get_appliances_data()

        def update_success(appliance) -> None:
            return None

        async def successful_reauth_restores_session() -> bool:
            nonlocal reauth_calls
            reauth_calls += 1
            client._api = types.SimpleNamespace(appliances=[appliance])
            return True

        client._update_appliance_sync = update_success
        client._async_reauth = successful_reauth_restores_session

        with patch(
            "custom_components.haier_hon.hon_client.asyncio.get_running_loop",
            return_value=InlineExecutorLoop(),
        ):
            data = await client.async_get_appliances_data()

        self.assertEqual(2, reauth_calls)
        self.assertIn("reauth-after-missing-session", data)


if __name__ == "__main__":
    unittest.main()
