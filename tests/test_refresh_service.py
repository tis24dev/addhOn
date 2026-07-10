# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the domain-wide ``addhon.refresh`` service.

The service forces an immediate cloud poll on every loaded config entry: the
automation-callable equivalent of the per-device "Refresh now" button (asked for
in discussion #34). It is global to the domain (registered once, removed on the
last unload), takes no fields and no target, isolates per-entry failures and must
never re-raise to the caller.

Like test_debug_panel, this uses stdlib unittest with hand-rolled HA stubs so no
real Home Assistant is required. ``FakeHass`` here gains a minimal services
registry (the debug-panel one has none): it records the registered handlers keyed
by ``(domain, name)`` and supports ``has_service`` / ``async_register`` /
``async_remove`` -- exactly what ``_async_register_services`` and
``async_unload_entry`` touch. A source-level wiring guard mirrors
test_mqtt_log_level.
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
    """Minimal HA stubs needed to import custom_components.addhon (the package).

    Tolerant ``getattr`` defaults mean the first test module that wins the shared
    ``sys.modules`` race keeps its richer stub; we only fill the gaps so importing
    ``__init__`` works under this module in isolation too.
    """
    ha = _mod("homeassistant")

    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )

    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    core.ServiceCall = getattr(core, "ServiceCall", type("ServiceCall", (), {}))
    if not hasattr(core, "callback"):
        core.callback = lambda func: func

    exceptions = _mod("homeassistant.exceptions")
    base_err = getattr(
        exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {})
    )
    exceptions.HomeAssistantError = base_err
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {})
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {})
    )

    helpers = _mod("homeassistant.helpers")
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = getattr(
        uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {})
    )
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    helpers.update_coordinator = uc

    # voluptuous is imported lazily inside _async_register_services (to build the
    # level schema of the OTHER two services). It is not a test dependency, so
    # stub it when absent -- the refresh service itself takes no schema.
    vol = sys.modules.get("voluptuous")
    if vol is None or not hasattr(vol, "Marker"):
        vol = _mod("voluptuous")
        vol.Schema = lambda schema=None, **kwargs: schema

        class _Marker:
            def __init__(self, key, *args, **kwargs):
                self.key = key
                self.default = kwargs.get("default")

        vol.Required = _Marker
        vol.Optional = _Marker
        vol.In = lambda container=None, *args, **kwargs: container


_install_stubs()

from custom_components.addhon import _async_register_services  # noqa: E402
from custom_components.addhon.const import (  # noqa: E402
    DOMAIN,
    SERVICE_REFRESH,
    SERVICE_SET_LOG_LEVEL,
    SERVICE_SET_MQTT_LOG_LEVEL,
)

COMPONENT = REPO_ROOT / "custom_components" / "addhon"
INIT = COMPONENT / "__init__.py"
CONST = COMPONENT / "const.py"
SERVICES = COMPONENT / "services.yaml"


class FakeServices:
    """Minimal HA services registry: records handlers keyed by (domain, name)."""

    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], object] = {}

    def has_service(self, domain: str, name: str) -> bool:
        return (domain, name) in self.handlers

    def async_register(self, domain, name, handler, schema=None) -> None:
        self.handlers[(domain, name)] = handler

    def async_remove(self, domain, name) -> None:
        self.handlers.pop((domain, name), None)


class FakeHass:
    def __init__(self, data=None) -> None:
        self.data = data or {}
        self.services = FakeServices()


class FakeCoordinator:
    def __init__(self) -> None:
        self.refreshes = 0

    async def async_request_refresh(self) -> None:
        self.refreshes += 1


class RaisingCoordinator:
    def __init__(self) -> None:
        self.refreshes = 0

    async def async_request_refresh(self) -> None:
        self.refreshes += 1
        raise RuntimeError("boom")


class SyncRaisingCoordinator:
    """async_request_refresh raises SYNCHRONOUSLY (not async) when called -- i.e.
    before yielding a coroutine. The handler wraps the call inside a coroutine so even
    this is captured by gather(return_exceptions=True) rather than escaping to the
    caller and aborting the other refreshes."""

    def async_request_refresh(self):
        raise RuntimeError("sync boom")


class FakeServiceCall:
    """A trivial ServiceCall: the refresh handler ignores ``data`` entirely."""

    def __init__(self) -> None:
        self.data: dict = {}


def _entry_data(coordinator, entry_id: str) -> dict:
    return {
        entry_id: {
            "coordinator": coordinator,
            "client": None,
            "integration_version": "9.9.9",
        }
    }


class RefreshServiceRegistrationTest(unittest.TestCase):
    def test_register_adds_refresh_service_without_schema(self) -> None:
        hass = FakeHass()
        _async_register_services(hass)
        self.assertTrue(hass.services.has_service(DOMAIN, SERVICE_REFRESH))
        # All three domain-wide services land in the same registry.
        self.assertTrue(hass.services.has_service(DOMAIN, SERVICE_SET_LOG_LEVEL))
        self.assertTrue(hass.services.has_service(DOMAIN, SERVICE_SET_MQTT_LOG_LEVEL))

    def test_registration_is_idempotent(self) -> None:
        hass = FakeHass()
        _async_register_services(hass)
        first = hass.services.handlers[(DOMAIN, SERVICE_REFRESH)]
        # A second call (e.g. a second entry's setup) must not re-register / replace.
        _async_register_services(hass)
        self.assertIs(first, hass.services.handlers[(DOMAIN, SERVICE_REFRESH)])

    def test_registers_refresh_when_only_it_is_missing(self) -> None:
        # The combined early-return guard must still register refresh if the other
        # two already exist (e.g. an upgrade from a build that lacked refresh).
        hass = FakeHass()
        hass.services.handlers[(DOMAIN, SERVICE_SET_MQTT_LOG_LEVEL)] = object()
        hass.services.handlers[(DOMAIN, SERVICE_SET_LOG_LEVEL)] = object()
        _async_register_services(hass)
        self.assertTrue(hass.services.has_service(DOMAIN, SERVICE_REFRESH))


class RefreshServiceBehaviorTest(unittest.IsolatedAsyncioTestCase):
    def _registered_handler(self, hass: FakeHass):
        return hass.services.handlers[(DOMAIN, SERVICE_REFRESH)]

    async def test_refreshes_every_loaded_coordinator(self) -> None:
        hass = FakeHass()
        _async_register_services(hass)
        coord_a, coord_b = FakeCoordinator(), FakeCoordinator()
        hass.data[DOMAIN] = {}
        hass.data[DOMAIN].update(_entry_data(coord_a, "entry-a"))
        hass.data[DOMAIN].update(_entry_data(coord_b, "entry-b"))

        await self._registered_handler(hass)(FakeServiceCall())

        self.assertEqual(1, coord_a.refreshes)
        self.assertEqual(1, coord_b.refreshes)

    async def test_per_entry_failure_is_isolated(self) -> None:
        hass = FakeHass()
        _async_register_services(hass)
        good, bad = FakeCoordinator(), RaisingCoordinator()
        hass.data[DOMAIN] = {}
        hass.data[DOMAIN].update(_entry_data(bad, "entry-bad"))
        hass.data[DOMAIN].update(_entry_data(good, "entry-good"))

        # Must NOT raise even though one coordinator blows up.
        await self._registered_handler(hass)(FakeServiceCall())

        self.assertEqual(1, bad.refreshes)
        self.assertEqual(1, good.refreshes)

    async def test_synchronous_raise_is_isolated(self) -> None:
        # A coordinator whose async_request_refresh raises SYNCHRONOUSLY (before
        # returning a coroutine) must not abort the others nor reach the caller: the
        # handler wraps each call in a coroutine so gather(return_exceptions=True)
        # captures it.
        hass = FakeHass()
        _async_register_services(hass)
        good, bad = FakeCoordinator(), SyncRaisingCoordinator()
        hass.data[DOMAIN] = {}
        hass.data[DOMAIN].update(_entry_data(bad, "entry-bad"))
        hass.data[DOMAIN].update(_entry_data(good, "entry-good"))

        # Must NOT raise even though one coordinator raises synchronously.
        await self._registered_handler(hass)(FakeServiceCall())

        self.assertEqual(1, good.refreshes)

    async def test_skips_none_coordinators_and_no_data(self) -> None:
        hass = FakeHass()
        _async_register_services(hass)
        handler = self._registered_handler(hass)

        # No DOMAIN bucket at all: a no-op, no raise.
        await handler(FakeServiceCall())

        # An entry mid-setup may have a None coordinator; it must be skipped.
        good = FakeCoordinator()
        hass.data[DOMAIN] = {
            "entry-partial": {"coordinator": None, "client": None},
            "entry-good": {"coordinator": good, "client": None},
        }
        await handler(FakeServiceCall())
        self.assertEqual(1, good.refreshes)

    async def test_reads_hass_data_live_at_call_time(self) -> None:
        # The handler captures ``hass`` but must enumerate coordinators at call
        # time, so an entry added AFTER registration is still refreshed.
        hass = FakeHass()
        _async_register_services(hass)
        handler = self._registered_handler(hass)

        late = FakeCoordinator()
        hass.data[DOMAIN] = _entry_data(late, "entry-late")
        await handler(FakeServiceCall())
        self.assertEqual(1, late.refreshes)


class RefreshServiceWiringTest(unittest.TestCase):
    """Source-level guards: the service must stay declared and wired."""

    def test_const_declares_service_name(self) -> None:
        self.assertIn(
            'SERVICE_REFRESH = "refresh"',
            CONST.read_text(encoding="utf-8"),
        )

    def test_services_yaml_declares_refresh(self) -> None:
        self.assertIn("refresh:", SERVICES.read_text(encoding="utf-8"))

    def test_init_registers_and_unregisters_refresh(self) -> None:
        src = INIT.read_text(encoding="utf-8")
        self.assertIn("SERVICE_REFRESH", src)
        self.assertIn("async_request_refresh", src)
        # Debounced refresh like the button, not a forced async_refresh.
        self.assertNotIn("async_refresh(", src)
        # Per-entry isolation: gather with return_exceptions, never re-raise.
        self.assertIn("return_exceptions=True", src)


if __name__ == "__main__":
    unittest.main()
