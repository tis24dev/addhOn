# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Behavioral tests for `_persist_refresh_token` (#1/#2 unified refresh-token persist).

The helper must write a rotated token into entry.data exactly once and only on a real
change (non-empty AND different from the stored one), reading entry.data live each call so
consecutive rotations in one process are all captured. stdlib unittest, HA stubbed.
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


def _install_ha_stubs() -> None:
    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exc = _mod("homeassistant.exceptions")
    base = getattr(exc, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exc.HomeAssistantError = base
    exc.ConfigEntryNotReady = getattr(exc, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base,), {}))
    exc.ConfigEntryAuthFailed = getattr(exc, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base,), {}))
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))
    ha = _mod("homeassistant")
    ha.config_entries, ha.core, ha.exceptions = ce, core, exc
    ha.helpers = _mod("homeassistant.helpers")
    ha.helpers.update_coordinator = uc


_install_ha_stubs()

from custom_components.addhon import _persist_refresh_token  # noqa: E402


class _FakeConfigEntries:
    def __init__(self) -> None:
        self.writes: list[dict] = []

    def async_update_entry(self, entry, data=None) -> None:
        self.writes.append(data)
        entry.data = data  # mirror HA: the next read sees the written value


class _FakeHass:
    def __init__(self) -> None:
        self.config_entries = _FakeConfigEntries()


class _FakeEntry:
    def __init__(self, data: dict) -> None:
        self.data = data


class _FakeClient:
    def __init__(self, token: str) -> None:
        self.refresh_token = token


class PersistRefreshTokenTest(unittest.TestCase):
    def _run(self, stored: str, live: str):
        hass = _FakeHass()
        entry = _FakeEntry({"email": "e", "password": "p", "refresh_token": stored})
        _persist_refresh_token(hass, entry, _FakeClient(live))
        return hass, entry

    def test_unchanged_does_not_write(self) -> None:
        hass, _ = self._run("RT", "RT")
        self.assertEqual([], hass.config_entries.writes)  # no churn on a steady poll

    def test_empty_does_not_wipe(self) -> None:
        hass, entry = self._run("RT", "")
        self.assertEqual([], hass.config_entries.writes)
        self.assertEqual("RT", entry.data["refresh_token"])

    def test_rotation_writes_once_preserving_other_keys(self) -> None:
        hass, _ = self._run("RT_OLD", "RT_NEW")
        self.assertEqual(1, len(hass.config_entries.writes))
        self.assertEqual(
            {"email": "e", "password": "p", "refresh_token": "RT_NEW"},
            hass.config_entries.writes[0],
        )

    def test_two_consecutive_rotations_both_persist(self) -> None:
        # The guard must read entry.data LIVE each call (the exact bug fixed): a second
        # rotation in the same process must still write.
        hass = _FakeHass()
        entry = _FakeEntry({"refresh_token": "RT0"})
        _persist_refresh_token(hass, entry, _FakeClient("RT1"))
        _persist_refresh_token(hass, entry, _FakeClient("RT2"))
        self.assertEqual(2, len(hass.config_entries.writes))
        self.assertEqual("RT2", entry.data["refresh_token"])


if __name__ == "__main__":
    unittest.main()
