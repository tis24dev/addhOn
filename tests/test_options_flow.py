"""Tests for the Options flow debug toggles (enable_debug, enable_mqtt_debug).

Two independent, persisted toggles on the integration's Configure screen:
- enable_debug      -> integration loggers to DEBUG (NOTSET when off).
- enable_mqtt_debug -> MQTT realtime logger to DEBUG (WARNING/silence when off).

Both reuse logging_utils helpers and are applied live (an options update listener,
no reload). MQTT level is applied AFTER the integration level so the MQTT child's
explicit level wins (enabling integration DEBUG does not flood MQTT).

stdlib unittest with HA + voluptuous stubs (mirrors test_config_flow_reauth), and
logging_utils loaded by file path (mirrors test_mqtt_log_level), so no real HA.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

COMPONENT = REPO_ROOT / "custom_components" / "addhon"
LOGGING_UTILS_PATH = COMPONENT / "logging_utils.py"
INIT = COMPONENT / "__init__.py"


def _mod(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_stubs() -> None:
    ha = _mod("homeassistant")

    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    config_entries.ConfigFlow = getattr(config_entries, "ConfigFlow", ConfigFlow)
    config_entries.OptionsFlow = getattr(
        config_entries, "OptionsFlow", type("OptionsFlow", (), {})
    )

    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))

    data_entry_flow = _mod("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = getattr(data_entry_flow, "FlowResult", dict)

    exceptions = _mod("homeassistant.exceptions")
    base_err = getattr(exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exceptions.HomeAssistantError = base_err
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {})
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {})
    )

    helpers = _mod("homeassistant.helpers")
    update_coordinator = _mod("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = getattr(
        update_coordinator, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {})
    )
    update_coordinator.UpdateFailed = getattr(
        update_coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {})
    )

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    helpers.update_coordinator = update_coordinator

    # voluptuous stub that captures `default` (the reauth stub does not). Only
    # install when voluptuous is absent or is a stub (real voluptuous exposes Marker).
    vol = sys.modules.get("voluptuous")
    if vol is None or not hasattr(vol, "Marker"):
        vol = _mod("voluptuous")
        vol.Schema = lambda schema=None, **kwargs: schema

        class Required:
            def __init__(self, key, *args, **kwargs):
                self.key = key
                self.default = kwargs.get("default")

        vol.Required = Required
        vol._addhon_capturing = True


_install_stubs()

from custom_components.addhon.const import (  # noqa: E402
    CONF_ENABLE_DEBUG,
    CONF_ENABLE_MQTT_DEBUG,
)


def _load_logging_utils():
    spec = importlib.util.spec_from_file_location(
        "addhon_logging_utils_optionsflow", LOGGING_UTILS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lu = _load_logging_utils()


class _FakeEntry:
    def __init__(self, options=None) -> None:
        self.entry_id = "entry-1"
        self.options = options or {}


def _make_options_flow(entry):
    from custom_components.addhon.config_flow import OptionsFlowHandler

    flow = OptionsFlowHandler()
    flow.config_entry = entry  # in HA 2024.11+ this is auto-provided; set it for tests
    flow.calls = {}

    def _show_form(*, step_id, data_schema=None, **kwargs):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema}

    def _create_entry(*, title, data):
        flow.calls["create"] = {"title": title, "data": data}
        return {"type": "create_entry", "title": title, "data": data}

    flow.async_show_form = _show_form
    flow.async_create_entry = _create_entry
    return flow


class OptionsFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_init_shows_form_with_both_toggles(self) -> None:
        flow = _make_options_flow(_FakeEntry())
        result = await flow.async_step_init(None)
        self.assertEqual("form", result["type"])
        self.assertEqual("init", result["step_id"])
        self.assertIsNotNone(result["data_schema"])

    async def test_defaults_reflect_current_options(self) -> None:
        vol = sys.modules["voluptuous"]
        if not getattr(vol, "_addhon_capturing", False):
            self.skipTest("real voluptuous: schema default not introspected")
        flow = _make_options_flow(
            _FakeEntry({CONF_ENABLE_DEBUG: True, CONF_ENABLE_MQTT_DEBUG: False})
        )
        result = await flow.async_step_init(None)
        defaults = {req.key: req.default for req in result["data_schema"]}
        self.assertEqual(defaults[CONF_ENABLE_DEBUG], True)
        self.assertEqual(defaults[CONF_ENABLE_MQTT_DEBUG], False)

    async def test_submit_stores_both_booleans(self) -> None:
        flow = _make_options_flow(_FakeEntry())
        result = await flow.async_step_init(
            {CONF_ENABLE_DEBUG: True, CONF_ENABLE_MQTT_DEBUG: False}
        )
        self.assertEqual("create_entry", result["type"])
        self.assertEqual("", result["title"])
        self.assertEqual(
            {CONF_ENABLE_DEBUG: True, CONF_ENABLE_MQTT_DEBUG: False},
            flow.calls["create"]["data"],
        )

    async def test_submit_missing_keys_default_false(self) -> None:
        flow = _make_options_flow(_FakeEntry())
        await flow.async_step_init({})
        self.assertEqual(
            {CONF_ENABLE_DEBUG: False, CONF_ENABLE_MQTT_DEBUG: False},
            flow.calls["create"]["data"],
        )

    def test_config_flow_exposes_options_flow(self) -> None:
        from custom_components.addhon.config_flow import (
            ConfigFlow,
            OptionsFlowHandler,
        )

        handler = ConfigFlow.async_get_options_flow(_FakeEntry())
        self.assertIsInstance(handler, OptionsFlowHandler)


class ApplyDebugOptionsTest(unittest.TestCase):
    """Behavioral test of the shared _apply_debug_options on the real loggers."""

    def setUp(self) -> None:
        names = ("custom_components.addhon", "custom_components.addhon.client.transport.mqtt")
        self._saved = {n: logging.getLogger(n).level for n in names}

    def tearDown(self) -> None:
        for name, level in self._saved.items():
            logging.getLogger(name).setLevel(level)

    def _apply(self, entry):
        from custom_components.addhon import _apply_debug_options

        _apply_debug_options(entry)

    def test_integration_toggle_on_sets_debug(self) -> None:
        self._apply(_FakeEntry({CONF_ENABLE_DEBUG: True}))
        self.assertEqual(
            logging.getLogger("custom_components.addhon").level, logging.DEBUG
        )

    def test_integration_toggle_off_resets_to_notset(self) -> None:
        logging.getLogger("custom_components.addhon").setLevel(logging.DEBUG)
        self._apply(_FakeEntry({CONF_ENABLE_DEBUG: False}))
        self.assertEqual(
            logging.getLogger("custom_components.addhon").level, logging.NOTSET
        )

    def test_mqtt_toggle_independent_and_stays_quiet_under_integration_debug(self) -> None:
        # Integration DEBUG ON but MQTT toggle OFF: the parent integration logger
        # goes to DEBUG WHILE the MQTT child stays WARNING (its explicit level wins
        # over the parent cascade). Asserting BOTH pins the real invariant and keeps
        # the enable_debug=True half load-bearing.
        self._apply(_FakeEntry({CONF_ENABLE_DEBUG: True, CONF_ENABLE_MQTT_DEBUG: False}))
        self.assertEqual(
            logging.getLogger("custom_components.addhon").level, logging.DEBUG
        )
        self.assertEqual(
            logging.getLogger("custom_components.addhon.client.transport.mqtt").level,
            logging.WARNING,
        )

    def test_mqtt_toggle_on_sets_debug(self) -> None:
        self._apply(_FakeEntry({CONF_ENABLE_MQTT_DEBUG: True}))
        self.assertEqual(
            logging.getLogger("custom_components.addhon.client.transport.mqtt").level,
            logging.DEBUG,
        )

    def test_options_update_listener_reapplies_live(self) -> None:
        # The update listener payload must re-apply levels live (no reload). Call it
        # directly: it only touches loggers via _apply_debug_options.
        import asyncio

        from custom_components.addhon import _async_options_updated

        logging.getLogger("custom_components.addhon").setLevel(logging.DEBUG)
        asyncio.run(
            _async_options_updated(None, _FakeEntry({CONF_ENABLE_DEBUG: False}))
        )
        self.assertEqual(
            logging.getLogger("custom_components.addhon").level, logging.NOTSET
        )


class ResetHelperTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {
            n: logging.getLogger(n).level for n in lu.INTEGRATION_DEBUG_LOGGERS
        }

    def tearDown(self) -> None:
        for name, level in self._saved.items():
            logging.getLogger(name).setLevel(level)

    def test_reset_integration_log_level_sets_notset(self) -> None:
        for name in lu.INTEGRATION_DEBUG_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        lu.reset_integration_log_level()
        for name in lu.INTEGRATION_DEBUG_LOGGERS:
            self.assertEqual(logging.getLogger(name).level, logging.NOTSET)


class WiringTest(unittest.TestCase):
    """Source-level guards for the live-apply listener wiring in __init__.py."""

    def test_init_registers_options_listener_and_applies(self) -> None:
        src = INIT.read_text(encoding="utf-8")
        self.assertIn("_apply_debug_options", src)
        self.assertIn("add_update_listener", src)
        self.assertIn("async_on_unload", src)
        self.assertIn("reset_integration_log_level", src)
        self.assertIn("CONF_ENABLE_DEBUG", src)
        self.assertIn("CONF_ENABLE_MQTT_DEBUG", src)

    def test_hacs_declares_min_ha_for_options_flow(self) -> None:
        # The OptionsFlow relies on HA auto-injecting self.config_entry, which only
        # exists since HA 2024.12.0 (a bare OptionsFlow has no config_entry before
        # that). Pin the declared minimum so it is not lowered below that.
        import json

        hacs = json.loads((REPO_ROOT / "hacs.json").read_text(encoding="utf-8"))
        parts = tuple(int(p) for p in hacs["homeassistant"].split("."))
        self.assertGreaterEqual(parts, (2024, 12, 0))


if __name__ == "__main__":
    unittest.main()
