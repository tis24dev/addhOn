"""Tests for the MQTT realtime wiring (#4): HonClient.subscribe_updates /
build_realtime_snapshot, plus source-guards for the async_setup_entry wiring.

A full async_setup_entry behavioural test is infeasible with the stub harness
(it runs the executor login, first refresh and platform forwarding), so the
cross-thread wiring in __init__.py is covered by source-guards (same approach as
test_coordinator_config_entry.py).
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


class FakeSession:
    """Stands in for the NativeHon session held as HonClient._hon_instance."""

    def __init__(self, appliances) -> None:
        self.appliances = appliances
        self._notify_function = None

    def subscribe_updates(self, fn) -> None:
        self._notify_function = fn

    def notify(self) -> None:
        if self._notify_function:
            self._notify_function(None)


class FakeAppliance:
    def __init__(self, uid: str) -> None:
        self.unique_id = uid
        self.attributes = {"parameters": {}}
        self.settings = {"s": 1}
        self.statistics = {}
        self.nick_name = "Nick"


def _client(appliances=None):
    c = HonClient(email="e@x", password="p")
    c._hon_instance = FakeSession(appliances or [])
    return c


class HonClientRealtimeTest(unittest.TestCase):
    def test_subscribe_updates_forwarded(self) -> None:
        c = _client()
        cb = lambda _arg: None  # noqa: E731
        c.subscribe_updates(cb)
        self.assertIs(c._hon_instance._notify_function, cb)

    def test_subscribe_updates_before_setup_raises(self) -> None:
        c = HonClient(email="e@x", password="p")  # no _hon_instance yet
        with self.assertRaises(RuntimeError):
            c.subscribe_updates(lambda _a: None)

    def test_subscribe_updates_detach_with_none(self) -> None:
        c = _client()
        c.subscribe_updates(lambda _a: None)
        c.subscribe_updates(None)
        self.assertIsNone(c._hon_instance._notify_function)

    def test_build_realtime_snapshot_from_memory(self) -> None:
        app = FakeAppliance("ac-1")
        c = _client([app])
        snap = c.build_realtime_snapshot()
        self.assertIn("ac-1", snap)
        self.assertIs(snap["ac-1"]["appliance"], app)
        self.assertEqual(snap["ac-1"]["settings"], {"s": 1})

    def test_build_realtime_snapshot_empty_without_session(self) -> None:
        c = HonClient(email="e@x", password="p")  # _hon_instance is None
        self.assertEqual(c.build_realtime_snapshot(), {})

    def test_build_realtime_snapshot_skips_raising_appliance(self) -> None:
        # Runs on the awscrt thread: one appliance raising must be skipped, never
        # take down the whole snapshot.
        class BadAppliance:
            unique_id = "bad"
            nick_name = "x"
            statistics = {}

            @property
            def attributes(self):
                raise RuntimeError("boom")

        good = FakeAppliance("good")
        c = _client([BadAppliance(), good])
        snap = c.build_realtime_snapshot()
        self.assertIn("good", snap)
        self.assertNotIn("bad", snap)

    def test_build_appliance_entry_has_all_keys(self) -> None:
        # Pin the shared shape so the realtime snapshot and the HTTP poll never
        # diverge (the reason _build_appliance_entry was extracted).
        entry = HonClient._build_appliance_entry(FakeAppliance("x"))
        self.assertEqual(
            set(entry),
            {"appliance", "type", "name", "model", "serial", "mac",
             "attributes", "statistics", "settings"},
        )

    def test_notify_roundtrip_invokes_callback(self) -> None:
        # subscribe_updates registers on the session; session.notify() (called by the
        # MQTT push) must invoke the callback.
        c = _client()
        calls = []
        c.subscribe_updates(lambda _arg: calls.append(True))
        c._hon_instance.notify()
        self.assertEqual(calls, [True])


class RealtimeWiringSourceGuard(unittest.TestCase):
    """The cross-thread wiring in async_setup_entry can't be exercised by the stub
    harness; guard its essential pieces at the source level."""

    _COMPONENT = REPO / "custom_components" / "addhon"

    def test_init_wires_push_via_call_soon_threadsafe(self) -> None:
        src = (self._COMPONENT / "__init__.py").read_text(encoding="utf-8")
        # push wired to the client...
        self.assertIn("subscribe_updates(", src)
        # ...and marshalled onto the HA loop (NOT a direct coordinator call from the
        # awscrt thread, which would be unsafe).
        self.assertIn("call_soon_threadsafe", src)
        self.assertIn("async_set_updated_data", src)
        # detached on unload
        self.assertIn("subscribe_updates(None)", src)

    def test_init_does_not_repoll_on_push(self) -> None:
        src = (self._COMPONENT / "__init__.py").read_text(encoding="utf-8")
        # The realtime publish must use the snapshot, not async_request_refresh
        # (which would re-trigger the slow HTTP poll on every push).
        self.assertIn("build_realtime_snapshot", src)
        self.assertNotIn("async_request_refresh", src)

    def test_hon_client_exposes_realtime_api(self) -> None:
        src = (self._COMPONENT / "hon_client.py").read_text(encoding="utf-8")
        self.assertIn("def subscribe_updates", src)
        self.assertIn("def build_realtime_snapshot", src)


if __name__ == "__main__":
    unittest.main()
