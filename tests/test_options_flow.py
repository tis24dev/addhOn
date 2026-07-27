# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the Options flow toggles (debug, MQTT debug, experimental).

Three independent, persisted toggles on the integration's Configure screen:
- enable_debug        -> integration loggers to DEBUG (NOTSET when off).
- enable_mqtt_debug   -> MQTT realtime logger to DEBUG (WARNING/silence when off).
- enable_experimental -> creates the entities whose meaning is inferred from
  incomplete evidence.

The two debug toggles reuse logging_utils helpers and are applied live (an options
update listener, no reload). MQTT level is applied AFTER the integration level so
the MQTT child's explicit level wins (enabling integration DEBUG does not flood
MQTT). The experimental toggle is different in kind: it changes WHICH entities
exist, so it is the only one that reloads the entry.

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
        vol.Optional = Required
        vol.In = lambda container=None, *args, **kwargs: container
        vol._addhon_capturing = True


_install_stubs()

from custom_components.addhon.const import (  # noqa: E402
    CONF_ENABLE_DEBUG,
    CONF_ENABLE_EXPERIMENTAL,
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


class _FakeServices:
    """Minimal hass.services for exercising _async_register_services."""

    def __init__(self) -> None:
        self._registered: set[tuple[str, str]] = set()

    def has_service(self, domain, service) -> bool:
        return (domain, service) in self._registered

    def async_register(self, domain, service, handler, schema=None) -> None:
        self._registered.add((domain, service))


class _FakeHass:
    def __init__(self) -> None:
        self.services = _FakeServices()


def _make_options_flow(entry):
    from custom_components.addhon.config_flow import OptionsFlowHandler

    flow = OptionsFlowHandler()
    flow.config_entry = entry  # in HA 2024.12.0+ this is auto-provided; set it for tests
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

    async def test_submit_stores_all_three_booleans(self) -> None:
        flow = _make_options_flow(_FakeEntry())
        result = await flow.async_step_init(
            {
                CONF_ENABLE_DEBUG: True,
                CONF_ENABLE_MQTT_DEBUG: False,
                CONF_ENABLE_EXPERIMENTAL: True,
            }
        )
        self.assertEqual("create_entry", result["type"])
        self.assertEqual("", result["title"])
        self.assertEqual(
            {
                CONF_ENABLE_DEBUG: True,
                CONF_ENABLE_MQTT_DEBUG: False,
                CONF_ENABLE_EXPERIMENTAL: True,
            },
            flow.calls["create"]["data"],
        )

    async def test_submit_missing_keys_default_false(self) -> None:
        flow = _make_options_flow(_FakeEntry())
        await flow.async_step_init({})
        self.assertEqual(
            {
                CONF_ENABLE_DEBUG: False,
                CONF_ENABLE_MQTT_DEBUG: False,
                CONF_ENABLE_EXPERIMENTAL: False,
            },
            flow.calls["create"]["data"],
        )

    async def test_experimental_defaults_to_false_on_the_form(self) -> None:
        vol = sys.modules["voluptuous"]
        if not getattr(vol, "_addhon_capturing", False):
            self.skipTest("real voluptuous: schema default not introspected")
        flow = _make_options_flow(_FakeEntry())
        result = await flow.async_step_init(None)
        defaults = {req.key: req.default for req in result["data_schema"]}
        self.assertEqual(defaults[CONF_ENABLE_EXPERIMENTAL], False)

    async def test_experimental_default_reflects_a_saved_true(self) -> None:
        vol = sys.modules["voluptuous"]
        if not getattr(vol, "_addhon_capturing", False):
            self.skipTest("real voluptuous: schema default not introspected")
        flow = _make_options_flow(_FakeEntry({CONF_ENABLE_EXPERIMENTAL: True}))
        result = await flow.async_step_init(None)
        defaults = {req.key: req.default for req in result["data_schema"]}
        self.assertEqual(defaults[CONF_ENABLE_EXPERIMENTAL], True)

    async def test_unknown_pre_existing_option_keys_survive_a_submit(self) -> None:
        """The screen writes the WHOLE options mapping, so anything it does not
        render (an option added by a later version, or one this build no longer
        shows) would be silently dropped by a literal three-key payload."""
        flow = _make_options_flow(
            _FakeEntry({"legacy_option": 7, CONF_ENABLE_DEBUG: True})
        )
        await flow.async_step_init({CONF_ENABLE_MQTT_DEBUG: True})
        data = flow.calls["create"]["data"]
        self.assertEqual(7, data["legacy_option"])
        # An unticked box is a real False, not a reason to keep the old value.
        self.assertEqual(False, data[CONF_ENABLE_DEBUG])
        self.assertEqual(True, data[CONF_ENABLE_MQTT_DEBUG])

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

    def test_options_listener_skips_when_toggles_unchanged(self) -> None:
        # HA fires update listeners on ANY entry write (e.g. a refresh-token rotation),
        # not only options changes. With the toggles unchanged the listener must NOT
        # re-apply/reset the levels, so a debug level raised at runtime survives.
        import asyncio

        from custom_components.addhon import (
            _ENTRY_OPTS_KEY,
            _async_options_updated,
            _entry_opts,
        )
        from custom_components.addhon.const import DOMAIN

        entry = _FakeEntry({CONF_ENABLE_DEBUG: False})

        class _Hass:
            data = {DOMAIN: {entry.entry_id: {_ENTRY_OPTS_KEY: _entry_opts(entry)}}}

        logging.getLogger("custom_components.addhon").setLevel(logging.DEBUG)
        asyncio.run(_async_options_updated(_Hass(), entry))
        # unchanged toggles -> level preserved, NOT reset to NOTSET
        self.assertEqual(
            logging.getLogger("custom_components.addhon").level, logging.DEBUG
        )

    def test_options_listener_applies_when_toggles_changed(self) -> None:
        # A genuine options change (baseline debug ON, now OFF) still re-applies live.
        import asyncio

        from custom_components.addhon import _ENTRY_OPTS_KEY, _async_options_updated
        from custom_components.addhon.const import DOMAIN

        entry = _FakeEntry({CONF_ENABLE_DEBUG: False})

        class _Hass:
            data = {DOMAIN: {entry.entry_id: {_ENTRY_OPTS_KEY: (True, False, False)}}}

        logging.getLogger("custom_components.addhon").setLevel(logging.DEBUG)
        asyncio.run(_async_options_updated(_Hass(), entry))
        self.assertEqual(
            logging.getLogger("custom_components.addhon").level, logging.NOTSET
        )


class _FakeConfigEntries:
    def __init__(self) -> None:
        self.reloads: list[str] = []

    async def async_reload(self, entry_id: str) -> None:
        self.reloads.append(entry_id)


class _ListenerHass:
    """hass with a recorded previous options snapshot and a recording reloader."""

    def __init__(self, entry, previous) -> None:
        from custom_components.addhon import _ENTRY_OPTS_KEY
        from custom_components.addhon.const import DOMAIN

        self.entry_data = {_ENTRY_OPTS_KEY: previous}
        self.data = {DOMAIN: {entry.entry_id: self.entry_data}}
        self.config_entries = _FakeConfigEntries()


class ExperimentalReloadTest(unittest.TestCase):
    """The experimental toggle changes WHICH entities exist, so it is the only
    option that needs a reload; the debug toggles must keep being applied live."""

    def setUp(self) -> None:
        self._saved = logging.getLogger("custom_components.addhon").level

    def tearDown(self) -> None:
        logging.getLogger("custom_components.addhon").setLevel(self._saved)

    def _run(self, entry, previous):
        import asyncio

        from custom_components.addhon import _async_options_updated

        hass = _ListenerHass(entry, previous)
        asyncio.run(_async_options_updated(hass, entry))
        return hass

    def test_a_debug_only_change_does_not_reload(self) -> None:
        entry = _FakeEntry({CONF_ENABLE_DEBUG: True})
        hass = self._run(entry, (False, False, False))
        self.assertEqual([], hass.config_entries.reloads)
        self.assertEqual(
            logging.getLogger("custom_components.addhon").level, logging.DEBUG
        )

    def test_an_experimental_change_reloads_exactly_once(self) -> None:
        entry = _FakeEntry({CONF_ENABLE_EXPERIMENTAL: True})
        hass = self._run(entry, (False, False, False))
        self.assertEqual([entry.entry_id], hass.config_entries.reloads)

    def test_turning_experimental_off_reloads_too(self) -> None:
        entry = _FakeEntry({CONF_ENABLE_EXPERIMENTAL: False})
        hass = self._run(entry, (False, False, True))
        self.assertEqual([entry.entry_id], hass.config_entries.reloads)

    def test_an_experimental_change_leaves_a_runtime_debug_level_alone(self) -> None:
        # Only the third value moved, so the loggers must not be re-applied: a
        # DEBUG level raised at runtime via set_log_level has to survive.
        entry = _FakeEntry({CONF_ENABLE_EXPERIMENTAL: True})
        logging.getLogger("custom_components.addhon").setLevel(logging.DEBUG)
        self._run(entry, (False, False, False))
        self.assertEqual(
            logging.getLogger("custom_components.addhon").level, logging.DEBUG
        )

    def test_an_unchanged_entry_write_neither_reloads_nor_reapplies(self) -> None:
        # HA fires the listener on ANY entry write (a token rotation, a title
        # change). Nothing may happen.
        entry = _FakeEntry({CONF_ENABLE_EXPERIMENTAL: True})
        logging.getLogger("custom_components.addhon").setLevel(logging.DEBUG)
        hass = self._run(entry, (False, False, True))
        self.assertEqual([], hass.config_entries.reloads)
        self.assertEqual(
            logging.getLogger("custom_components.addhon").level, logging.DEBUG
        )

    def test_a_combined_change_reloads_and_applies(self) -> None:
        entry = _FakeEntry(
            {CONF_ENABLE_DEBUG: True, CONF_ENABLE_EXPERIMENTAL: True}
        )
        logging.getLogger("custom_components.addhon").setLevel(logging.NOTSET)
        hass = self._run(entry, (False, False, False))
        self.assertEqual([entry.entry_id], hass.config_entries.reloads)
        self.assertEqual(
            logging.getLogger("custom_components.addhon").level, logging.DEBUG
        )

    def test_a_repeated_listener_call_does_not_reload_again(self) -> None:
        # The snapshot must be updated, or every subsequent entry write for the
        # same options would reload the integration again.
        import asyncio

        from custom_components.addhon import _ENTRY_OPTS_KEY, _async_options_updated

        entry = _FakeEntry({CONF_ENABLE_EXPERIMENTAL: True})
        hass = self._run(entry, (False, False, False))
        self.assertEqual((False, False, True), hass.entry_data[_ENTRY_OPTS_KEY])
        asyncio.run(_async_options_updated(hass, entry))
        self.assertEqual([entry.entry_id], hass.config_entries.reloads)

    def test_a_first_listener_call_without_a_snapshot_does_not_reload(self) -> None:
        # No baseline means nothing is known to have CHANGED; reloading on that
        # would risk a reload loop at setup time.
        entry = _FakeEntry({CONF_ENABLE_EXPERIMENTAL: True})
        hass = _ListenerHass(entry, None)
        hass.entry_data.clear()
        import asyncio

        from custom_components.addhon import _async_options_updated

        asyncio.run(_async_options_updated(hass, entry))
        self.assertEqual([], hass.config_entries.reloads)


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


class RegisterThenApplyOrderTest(unittest.TestCase):
    """Helper-composition guard (NOT the production-order guard): proves that
    _async_register_services default-silences the MQTT logger on first
    registration, and that a LATER _apply_debug_options with the MQTT toggle ON
    overrides it back to DEBUG. The PRODUCTION call order inside async_setup_entry
    is pinned separately and behaviorally by SetupEntryOrderingTest and (in
    source) by WiringTest.test_debug_options_applied_before_setup_path.
    """

    def setUp(self) -> None:
        names = ("custom_components.addhon", "custom_components.addhon.client.transport.mqtt")
        self._saved = {n: logging.getLogger(n).level for n in names}

    def tearDown(self) -> None:
        for name, level in self._saved.items():
            logging.getLogger(name).setLevel(level)

    def test_register_silences_then_apply_on_overrides(self) -> None:
        from custom_components.addhon import (
            _apply_debug_options,
            _async_register_services,
        )

        mqtt_logger = logging.getLogger(
            "custom_components.addhon.client.transport.mqtt"
        )
        mqtt_logger.setLevel(logging.DEBUG)  # start dirty to prove the silence ran

        hass = _FakeHass()
        # First registration silences MQTT to WARNING.
        _async_register_services(hass)
        self.assertEqual(mqtt_logger.level, logging.WARNING)

        # A later apply with MQTT debug ON overrides back to DEBUG.
        _apply_debug_options(_FakeEntry({CONF_ENABLE_MQTT_DEBUG: True}))
        self.assertEqual(mqtt_logger.level, logging.DEBUG)


class SetupEntryOrderingTest(unittest.IsolatedAsyncioTestCase):
    """Behavioral guard that drives the REAL async_setup_entry far enough to
    observe production behavior. async_setup_entry bails at the missing-email
    check (returns False) AFTER _async_register_services + the early
    _apply_debug_options run, so HonClient is imported but never constructed and
    no login happens. The real .hon_client is NOT stubbed: importing it is part of
    what this exercises (a stub would mask an import-time regression there).
    """

    def setUp(self) -> None:
        names = ("custom_components.addhon", "custom_components.addhon.client.transport.mqtt")
        self._saved = {n: logging.getLogger(n).level for n in names}

    def tearDown(self) -> None:
        for name, level in self._saved.items():
            logging.getLogger(name).setLevel(level)

    async def test_setup_escalates_toggles_after_default_silence(self) -> None:
        # PRODUCTION-ORDER guard: with both toggles ON, async_setup_entry must end
        # with MQTT at DEBUG. That only holds if register-services (default silence
        # -> WARNING) runs BEFORE the early apply (-> DEBUG). A swapped order would
        # leave MQTT at WARNING. This is exactly what RegisterThenApplyOrderTest
        # CANNOT catch (it sequences the calls itself).
        from custom_components.addhon import async_setup_entry

        mqtt_logger = logging.getLogger("custom_components.addhon.client.transport.mqtt")
        intg_logger = logging.getLogger("custom_components.addhon")
        mqtt_logger.setLevel(logging.NOTSET)
        intg_logger.setLevel(logging.NOTSET)

        entry = _FakeEntry({CONF_ENABLE_DEBUG: True, CONF_ENABLE_MQTT_DEBUG: True})
        entry.data = {}  # no email -> returns False right after the early apply

        result = await async_setup_entry(_FakeHass(), entry)
        self.assertFalse(result)
        self.assertEqual(mqtt_logger.level, logging.DEBUG)
        self.assertEqual(intg_logger.level, logging.DEBUG)

    async def test_setup_off_does_not_clobber_runtime_debug(self) -> None:
        # Regression guard (refuter HIGH): with the persisted toggle OFF, a re-setup
        # (HA retry / reload) must NOT reset a DEBUG level set at runtime via the
        # addhon.set_log_level service. The setup-path apply uses reset_when_off=False
        # so an OFF toggle only leaves the logger alone. The OLD full-reset behavior
        # would clobber it to NOTSET on every retry.
        from custom_components.addhon import async_setup_entry, _async_register_services

        intg_logger = logging.getLogger("custom_components.addhon")

        hass = _FakeHass()
        _async_register_services(hass)  # services already exist (a prior attempt)
        intg_logger.setLevel(logging.DEBUG)  # user ran set_log_level: debug at runtime

        entry = _FakeEntry({CONF_ENABLE_DEBUG: False, CONF_ENABLE_MQTT_DEBUG: False})
        entry.data = {}
        await async_setup_entry(hass, entry)

        self.assertEqual(intg_logger.level, logging.DEBUG)  # survived the re-setup


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

    def test_debug_options_applied_before_setup_path(self) -> None:
        # The persisted toggles must be applied at the TOP of async_setup_entry so
        # DEBUG covers the setup path (login/discovery/first refresh), and AFTER
        # _async_register_services so its default MQTT silence does not clobber the
        # MQTT toggle. Pin both orderings in source. (Match "(entry" without the
        # closing paren so the reset_when_off kwarg does not break the search.)
        src = INIT.read_text(encoding="utf-8")
        setup_idx = src.index("async def async_setup_entry")
        register_idx = src.index("_async_register_services(hass)", setup_idx)
        apply_idx = src.index("_apply_debug_options(entry", setup_idx)
        client_setup_idx = src.index("hon_client.setup_sync", setup_idx)
        self.assertLess(
            register_idx,
            apply_idx,
            "register services (default MQTT silence) must precede applying toggles",
        )
        self.assertLess(
            apply_idx,
            client_setup_idx,
            "debug options must be applied before the hOn setup path",
        )

    def test_setup_applies_debug_options_exactly_once(self) -> None:
        # The move replaced the end-of-setup call; there must be exactly ONE apply
        # inside async_setup_entry (no leftover double-apply). Ignore comment lines
        # so a comment mentioning the call cannot skew the count.
        src = INIT.read_text(encoding="utf-8")
        start = src.index("async def async_setup_entry")
        end = src.index("async def async_unload_entry", start)
        code = "\n".join(
            ln for ln in src[start:end].splitlines() if not ln.lstrip().startswith("#")
        )
        self.assertEqual(code.count("_apply_debug_options("), 1)

    def test_setup_apply_disables_reset_when_off(self) -> None:
        # The setup-path apply must pass reset_when_off=False so an OFF toggle does
        # not clobber a runtime-set debug level across retries (the listener path
        # keeps the default reset semantics). Match the kwarg anywhere in the setup
        # span to stay robust to reformatting (e.g. a trailing comma), but strip
        # comment lines so the explanatory comment mentioning the kwarg cannot make
        # this guard vacuous.
        src = INIT.read_text(encoding="utf-8")
        start = src.index("async def async_setup_entry")
        end = src.index("async def async_unload_entry", start)
        code = "\n".join(
            ln for ln in src[start:end].splitlines() if not ln.lstrip().startswith("#")
        )
        self.assertIn("reset_when_off=False", code)

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
