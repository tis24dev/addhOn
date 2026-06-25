"""Tests for _remove_legacy_entities (#22): the legacy 'power' cleanup must be
scoped to the switch domain, so the legitimate WH `power` and KT `current_power`
SENSORS (which also end in '_power') are not purged on every setup.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _mod(name: str) -> types.ModuleType:
    m = sys.modules.get(name)
    if m is None:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return m


def _install_stubs() -> None:
    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    if not hasattr(core, "callback"):
        core.callback = lambda f: f
    if not hasattr(core, "ServiceCall"):
        core.ServiceCall = object
    exc = _mod("homeassistant.exceptions")
    base = getattr(exc, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exc.HomeAssistantError = base
    exc.ConfigEntryNotReady = getattr(exc, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base,), {}))
    exc.ConfigEntryAuthFailed = getattr(exc, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base,), {}))
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))
    _mod("homeassistant.helpers.entity_registry")  # functions set per-test
    ha = _mod("homeassistant")
    ha.config_entries, ha.core, ha.exceptions = ce, core, exc
    ha.helpers = _mod("homeassistant.helpers")
    ha.helpers.update_coordinator = uc
    ha.helpers.entity_registry = sys.modules["homeassistant.helpers.entity_registry"]


_install_stubs()

import homeassistant.helpers.entity_registry as er  # noqa: E402
from custom_components.addhon import _remove_legacy_entities, DOMAIN  # noqa: E402


class FakeRegEntry:
    def __init__(self, entity_id: str, unique_id: str) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


class FakeRegistry:
    def __init__(self, entries) -> None:
        self._entries = list(entries)
        self.removed: list = []

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)
        self._entries = [e for e in self._entries if e.entity_id != entity_id]


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id


def _run(entries, coord_data=None):
    reg = FakeRegistry(entries)
    er.async_get = lambda hass: reg
    er.async_entries_for_config_entry = lambda registry, entry_id: list(registry._entries)
    entry = FakeEntry()
    coordinator = types.SimpleNamespace(data=coord_data or {})
    hass = types.SimpleNamespace(data={DOMAIN: {entry.entry_id: {"coordinator": coordinator}}})
    _remove_legacy_entities(hass, entry)
    return reg.removed


class LegacyCleanupTest(unittest.TestCase):
    def test_legacy_power_switch_removed(self) -> None:
        removed = _run([FakeRegEntry("switch.foo_power", "ID_power")])
        self.assertEqual(removed, ["switch.foo_power"])

    def test_wh_power_sensor_kept(self) -> None:
        # #22: a sensor with unique_id '<id>_power' must NOT be deleted.
        removed = _run([FakeRegEntry("sensor.foo_power", "ID_power")])
        self.assertEqual(removed, [])

    def test_kt_current_power_sensor_kept(self) -> None:
        removed = _run([FakeRegEntry("sensor.foo_current_power", "ID_current_power")])
        self.assertEqual(removed, [])

    def test_mixed_only_switch_power_removed(self) -> None:
        entries = [
            FakeRegEntry("switch.foo_power", "ID_power"),         # legacy -> remove
            FakeRegEntry("sensor.foo_power", "ID_power"),         # WH power -> keep
            FakeRegEntry("sensor.foo_current_power", "ID_current_power"),  # KT -> keep
            FakeRegEntry("sensor.foo_temperature", "ID_temperature"),     # unrelated -> keep
        ]
        removed = _run(entries)
        self.assertEqual(removed, ["switch.foo_power"])

    def test_td_orphan_consumption_removed(self) -> None:
        # Existing behavior preserved: washer-only consumption sensors on a TD device.
        removed = _run(
            [FakeRegEntry("sensor.td_total_water", "tdid_total_water")],
            coord_data={"tdid": {"type": "TD"}},
        )
        self.assertEqual(removed, ["sensor.td_total_water"])

    def test_td_orphan_not_removed_on_non_td(self) -> None:
        removed = _run(
            [FakeRegEntry("sensor.wm_total_water", "wmid_total_water")],
            coord_data={"wmid": {"type": "WM"}},
        )
        self.assertEqual(removed, [])

    def test_legacy_power_removal_log_redacts_identity(self) -> None:
        # Privacy: the INFO removal log must carry the redacted id, never the
        # entity_id (whose object_id is the nickname slug). INFO is not gated by
        # the debug toggles, so it always reaches home-assistant.log.
        with self.assertLogs("custom_components.addhon", level="INFO") as logs:
            removed = _run([FakeRegEntry("switch.foo_power", "ID_power")])
        self.assertEqual(removed, ["switch.foo_power"])
        blob = "\n".join(logs.output)
        self.assertIn("id=***", blob)
        self.assertNotIn("foo_power", blob)
        self.assertNotIn("ID_power", blob)

    def test_td_orphan_removal_log_redacts_identity(self) -> None:
        with self.assertLogs("custom_components.addhon", level="INFO") as logs:
            removed = _run(
                [FakeRegEntry("sensor.td_total_water", "tdid_total_water")],
                coord_data={"tdid": {"type": "TD"}},
            )
        self.assertEqual(removed, ["sensor.td_total_water"])
        blob = "\n".join(logs.output)
        self.assertIn("id=***", blob)
        self.assertNotIn("td_total_water", blob)
        self.assertNotIn("tdid", blob)


if __name__ == "__main__":
    unittest.main()
