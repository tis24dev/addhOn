# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the error-code surfacing in the config flow (issue #30).

A coded CannotConnect/InvalidAuth from validate_input must drive both the form
error key and the shown ADDHON-NNN (via description_placeholders["error_code"]):
- a UI code -> its precise slug key + label;
- a non-UI (runtime-only) code -> the generic bucket + the bare label;
- a legacy code-less exception -> the bucket with no code (back-compat).

Reuses the HA-stub style of test_config_flow_reauth.py (no real HA needed).
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


def _install_stubs() -> None:
    ha = _mod("homeassistant")
    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(config_entries, "ConfigEntry", type("ConfigEntry", (), {}))

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    config_entries.ConfigFlow = getattr(config_entries, "ConfigFlow", ConfigFlow)
    config_entries.OptionsFlow = getattr(config_entries, "OptionsFlow", type("OptionsFlow", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    data_entry_flow = _mod("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = getattr(data_entry_flow, "FlowResult", dict)
    exceptions = _mod("homeassistant.exceptions")
    base_err = getattr(exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exceptions.HomeAssistantError = base_err
    exceptions.ConfigEntryNotReady = getattr(exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {}))
    exceptions.ConfigEntryAuthFailed = getattr(exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {}))
    helpers = _mod("homeassistant.helpers")
    update_coordinator = _mod("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = getattr(update_coordinator, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    update_coordinator.UpdateFailed = getattr(update_coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {}))
    ha.config_entries, ha.core, ha.exceptions, ha.helpers = config_entries, core, exceptions, helpers
    helpers.update_coordinator = update_coordinator
    if "voluptuous" not in sys.modules:
        vol = _mod("voluptuous")
        vol.Schema = lambda schema=None, **kwargs: schema

        class Required:
            def __init__(self, key, *args, **kwargs):
                self.key = key

        vol.Required = Required
        vol.Optional = Required


_install_stubs()

from custom_components.addhon import config_flow as cf  # noqa: E402
from custom_components.addhon import error_codes as ec  # noqa: E402


class _FakeEntry:
    def __init__(self):
        self.entry_id = "entry-1"
        self.data = {"email": "person@example.com", "password": "old"}
        self.unique_id = "person@example.com"


def _user_flow():
    flow = cf.ConfigFlow()
    flow.hass = object()
    flow.context = {"source": "user"}
    flow.unique_id = None

    async def _set_unique_id(unique_id):
        flow.unique_id = unique_id

    flow.async_set_unique_id = _set_unique_id
    flow._abort_if_unique_id_configured = lambda: None
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_create_entry = lambda *, title, data: {"type": "create_entry", "title": title}
    return flow


def _reauth_flow():
    entry = _FakeEntry()
    flow = cf.ConfigFlow()

    class _CE:
        def async_get_entry(self, _id):
            return entry

    flow.hass = types.SimpleNamespace(config_entries=_CE())
    flow.context = {"source": "reauth", "entry_id": entry.entry_id}
    flow.unique_id = None
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    return flow


def _patch_validate(test, fn) -> None:
    original = cf.validate_input
    cf.validate_input = fn
    test.addCleanup(setattr, cf, "validate_input", original)


class UserStepErrorCodeTest(unittest.IsolatedAsyncioTestCase):
    async def _run_user(self, raiser):
        async def _v(hass, data):
            raise raiser

        _patch_validate(self, _v)
        flow = _user_flow()
        return await flow.async_step_user({"email": "p@example.com", "password": "x"})

    async def test_ui_code_drives_slug_and_label(self) -> None:
        res = await self._run_user(cf.CannotConnect(ec.NETWORK_TIMEOUT))
        self.assertEqual(res["errors"]["base"], "network_timeout")
        self.assertEqual(res["description_placeholders"]["error_code"], "ADDHON-400")

    async def test_invalid_auth_code(self) -> None:
        res = await self._run_user(cf.InvalidAuth(ec.INVALID_CREDENTIALS))
        self.assertEqual(res["errors"]["base"], "invalid_credentials")
        self.assertEqual(res["description_placeholders"]["error_code"], "ADDHON-100")

    async def test_non_ui_code_falls_back_to_bucket_but_keeps_label(self) -> None:
        # MQTT_SUBSCRIBE_TIMEOUT is ui=False (runtime only); if it ever reaches the
        # form it must degrade to the generic bucket while still showing the code.
        res = await self._run_user(cf.CannotConnect(ec.MQTT_SUBSCRIBE_TIMEOUT))
        self.assertEqual(res["errors"]["base"], "cannot_connect")
        self.assertEqual(res["description_placeholders"]["error_code"], "ADDHON-320")

    async def test_legacy_string_exception_has_no_code(self) -> None:
        res = await self._run_user(cf.CannotConnect("offline"))
        self.assertEqual(res["errors"]["base"], "cannot_connect")
        self.assertEqual(res["description_placeholders"]["error_code"], "")

    async def test_clean_form_has_empty_error_code(self) -> None:
        flow = _user_flow()
        res = await flow.async_step_user(None)
        self.assertEqual(res["errors"], {})
        self.assertEqual(res["description_placeholders"]["error_code"], "")

    async def test_unexpected_error_maps_to_unknown_code(self) -> None:
        async def _v(hass, data):
            raise RuntimeError("weird")

        _patch_validate(self, _v)
        flow = _user_flow()
        with self.assertLogs(cf._LOGGER.name, level="ERROR"):
            res = await flow.async_step_user({"email": "p@example.com", "password": "x"})
        self.assertEqual(res["errors"]["base"], "unknown")
        self.assertEqual(res["description_placeholders"]["error_code"], ec.UNKNOWN.label)


class ReauthStepErrorCodeTest(unittest.IsolatedAsyncioTestCase):
    async def test_reauth_coded_error_surfaces(self) -> None:
        async def _v(hass, data):
            raise cf.CannotConnect(ec.LOOP_TIMEOUT)

        _patch_validate(self, _v)
        flow = _reauth_flow()
        res = await flow.async_step_reauth_confirm({"password": "x"})
        self.assertEqual(res["errors"]["base"], "loop_timeout")
        self.assertEqual(res["description_placeholders"]["error_code"], "ADDHON-460")
        self.assertEqual(res["description_placeholders"]["email"], "person@example.com")


if __name__ == "__main__":
    unittest.main()
