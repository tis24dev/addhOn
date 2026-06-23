"""Per-appliance resilience of the coordinator poll (async_get_appliances_data).

A non-auth failure on ONE appliance (a transient cloud 5xx that outlived the
retries, a malformed payload, ...) must NOT blank EVERY device: the failed
appliance is simply absent from the snapshot while the others stay live. Only a
TOTAL failure (every appliance errored) re-raises so the coordinator marks the
cycle failed instead of publishing an empty snapshot.

Uses the same HA-stub harness as test_hon_client_realtime.py (the real
homeassistant package is not importable in the unit env).
"""
from __future__ import annotations

import asyncio
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


_install_stubs()

from custom_components.addhon.hon_client import HonClient  # noqa: E402


class FakeApi:
    def __init__(self, appliances) -> None:
        self.appliances = appliances


class FakeAppliance:
    def __init__(self, uid: str) -> None:
        self.unique_id = uid
        self.attributes = {"parameters": {}, "available": True}
        self.settings = {"s": 1}
        self.statistics = {}
        self.nick_name = uid


def _client(appliances):
    c = HonClient(email="e@x", password="p")
    c._api = FakeApi(appliances)
    return c


class CoordinatorResilienceTest(unittest.TestCase):
    def test_first_poll_is_strict_one_failure_raises(self) -> None:
        # On the FIRST poll, a per-appliance failure must re-raise (NOT skip): platform
        # setup iterates the first snapshot once with no dynamic discovery, so a skipped
        # appliance would get zero entities until a reload. Raising -> ConfigEntryNotReady
        # -> HA retries setup until the full inventory loads.
        good, bad = FakeAppliance("g1"), FakeAppliance("bad")
        c = _client([good, bad])  # fresh client: _first_poll_done is False

        def _update(appliance):
            if appliance is bad:
                raise RuntimeError("boom")  # non-auth, non-retryable

        c._update_appliance_sync = _update
        with self.assertRaises(RuntimeError):
            asyncio.run(c.async_get_appliances_data())
        self.assertFalse(c._first_poll_done)  # never completed -> still strict

    def test_first_poll_full_success_flips_flag(self) -> None:
        c = _client([FakeAppliance("g1"), FakeAppliance("g2")])
        c._update_appliance_sync = lambda appliance: None
        data = asyncio.run(c.async_get_appliances_data())
        self.assertEqual(set(data), {"g1", "g2"})
        self.assertTrue(c._first_poll_done)  # steady-state resilience now armed

    def test_steady_state_partial_update_keeps_the_others(self) -> None:
        good1, bad, good2 = FakeAppliance("g1"), FakeAppliance("bad"), FakeAppliance("g2")
        c = _client([good1, bad, good2])
        c._first_poll_done = True  # simulate a healthy first poll already happened

        def _update(appliance):
            if appliance is bad:
                raise RuntimeError("boom")  # non-auth, non-retryable

        c._update_appliance_sync = _update
        data = asyncio.run(c.async_get_appliances_data())

        self.assertIn("g1", data)
        self.assertIn("g2", data)
        self.assertNotIn("bad", data)  # the failed appliance is skipped, not fatal

    def test_steady_state_total_failure_raises(self) -> None:
        bad1, bad2 = FakeAppliance("b1"), FakeAppliance("b2")
        c = _client([bad1, bad2])
        c._first_poll_done = True
        c._update_appliance_sync = lambda appliance: (_ for _ in ()).throw(RuntimeError("boom"))
        # Every appliance failed -> surface a failed update (do NOT publish empty data)
        with self.assertRaises(RuntimeError):
            asyncio.run(c.async_get_appliances_data())

    def test_zero_appliances_returns_empty_without_raising(self) -> None:
        c = _client([])
        c._update_appliance_sync = lambda appliance: None
        self.assertEqual(asyncio.run(c.async_get_appliances_data()), {})

    def test_reauth_error_still_triggers_reauth(self) -> None:
        # Regression: an auth/"session unavailable" error must NOT be silently
        # skipped like a generic one -> it still drives re-authentication and a retry.
        app = FakeAppliance("ac")
        c = _client([app])
        calls = {"update": 0, "reauth": 0}

        def _update(appliance):
            calls["update"] += 1
            if calls["update"] == 1:
                raise RuntimeError("hOn session unavailable")  # _requires_reauth -> True

        async def _reauth():
            calls["reauth"] += 1
            return True

        c._update_appliance_sync = _update
        c._async_reauth = _reauth
        data = asyncio.run(c.async_get_appliances_data())

        self.assertEqual(calls["reauth"], 1)       # reauth was attempted
        self.assertGreaterEqual(calls["update"], 2)  # and the poll retried
        self.assertIn("ac", data)                  # recovered after reauth


if __name__ == "__main__":
    unittest.main()
