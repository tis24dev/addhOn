"""Tests for HonBaseEntity's per-appliance `available` override.

Covers the fix that makes an entity go unavailable when its appliance is no
longer in coordinator.data, while still honoring the coordinator's overall
last_update_success.

Stdlib unittest with inline Home Assistant stubs; no real HA needed. The stub
CoordinatorEntity intentionally defines `available` (returning
coordinator.last_update_success, like the real one) and is force-assigned so
super().available resolves regardless of test-suite order.
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


def _install_homeassistant_stubs() -> None:
    ha = _mod("homeassistant")

    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )

    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))

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
    entity = _mod("homeassistant.helpers.entity")
    entity.DeviceInfo = getattr(entity, "DeviceInfo", dict)

    update_coordinator = _mod("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
            self.hass = getattr(coordinator, "hass", None)

        @property
        def available(self) -> bool:
            return self.coordinator.last_update_success

        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

    # Force-assign (not getattr-default): another test module may have already
    # registered a CoordinatorEntity WITHOUT `available`, and super().available
    # must resolve no matter the suite order. This stub is a superset of the one
    # in test_program_select (it also provides async_write_ha_state) so taking
    # over the shared class does not break other modules' tests.
    update_coordinator.CoordinatorEntity = CoordinatorEntity
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
    helpers.entity = entity
    helpers.update_coordinator = update_coordinator


_install_homeassistant_stubs()


class FakeCoordinator:
    def __init__(self, data, last_update_success: bool = True) -> None:
        self.data = data
        self.hass = None
        self.last_update_success = last_update_success
        self.last_exception = None
        self.hon_client = None


def _appliance_data() -> dict:
    return {"washer-1": {"type": "WM", "name": "Washer", "attributes": {}, "settings": {}}}


class AvailabilityTest(unittest.TestCase):
    def _make_entity(self, coordinator):
        from custom_components.addhon.base_entity import HonBaseEntity

        class _Concrete(HonBaseEntity):
            pass

        return _Concrete(coordinator, "washer-1", client=None)

    def test_available_when_present_and_coordinator_ok(self) -> None:
        entity = self._make_entity(FakeCoordinator(_appliance_data()))
        self.assertTrue(entity.available)

    def test_unavailable_when_appliance_missing(self) -> None:
        # Refresh succeeded but this appliance is gone from the data.
        entity = self._make_entity(FakeCoordinator({"other-2": {}}))
        self.assertFalse(entity.available)

    def test_unavailable_when_coordinator_failed_even_if_present(self) -> None:
        entity = self._make_entity(
            FakeCoordinator(_appliance_data(), last_update_success=False)
        )
        self.assertFalse(entity.available)

    def test_unavailable_when_data_is_not_a_dict(self) -> None:
        # Defensive: data None/unset must not raise, just be unavailable.
        entity = self._make_entity(FakeCoordinator(None))
        self.assertFalse(entity.available)

    def test_unavailable_when_device_disconnected(self) -> None:
        # App model: device offline (available=False from lastConnEvent) -> entity
        # unavailable instead of stale values (replaces the old zeroing).
        data = {"washer-1": {"type": "WM", "name": "Washer",
                             "attributes": {"available": False}, "settings": {}}}
        entity = self._make_entity(FakeCoordinator(data))
        self.assertFalse(entity.available)

    def test_available_when_device_connected(self) -> None:
        data = {"washer-1": {"type": "WM", "name": "Washer",
                             "attributes": {"available": True}, "settings": {}}}
        entity = self._make_entity(FakeCoordinator(data))
        self.assertTrue(entity.available)

    def test_available_when_flag_absent_defaults_true(self) -> None:
        # `available` absent (e.g. load failed) -> do not hide it without reason.
        entity = self._make_entity(FakeCoordinator(_appliance_data()))
        self.assertTrue(entity.available)


if __name__ == "__main__":
    unittest.main()
