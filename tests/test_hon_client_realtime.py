"""Tests for the MQTT realtime wiring (#4): HonClient.subscribe_updates /
build_realtime_snapshot, plus source-guards for the async_setup_entry wiring.

A full async_setup_entry behavioural test is infeasible with the stub harness
(it runs the executor login, first refresh and platform forwarding), so the
cross-thread wiring in __init__.py is covered by source-guards (same approach as
test_coordinator_config_entry.py).
"""
from __future__ import annotations

import asyncio
import logging
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
from custom_components.addhon.error_codes import HonCodedError  # noqa: E402


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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


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
        self.assertIs(c._notify_function, cb)  # also stored on the client

    def test_subscribe_updates_before_setup_is_stored_not_raised(self) -> None:
        # #28: no raise when there is no session yet; the callback is remembered and
        # applied by setup_sync. (Old contract raised RuntimeError here.)
        c = HonClient(email="e@x", password="p")  # no _hon_instance yet
        cb = lambda _a: None  # noqa: E731
        c.subscribe_updates(cb)  # must NOT raise
        self.assertIs(c._notify_function, cb)

    def test_subscribe_none_after_close_is_noop(self) -> None:
        # #28: the on-unload detach runs subscribe_updates(None) AFTER the client is
        # closed (_hon_instance None) -> must be a clean no-op, not RuntimeError.
        c = HonClient(email="e@x", password="p")  # post-close state
        c.subscribe_updates(None)  # must NOT raise
        self.assertIsNone(c._notify_function)

    def test_subscribe_updates_detach_with_none(self) -> None:
        c = _client()
        c.subscribe_updates(lambda _a: None)
        c.subscribe_updates(None)
        self.assertIsNone(c._hon_instance._notify_function)
        self.assertIsNone(c._notify_function)

    def test_callback_rewired_after_reauth(self) -> None:
        # #20: setup_sync (run at initial setup AND on re-auth, which rebuilds the
        # session) must re-apply the stored notify callback to the NEW session, else
        # the MQTT push dies permanently after a re-auth.
        import custom_components.addhon.client.factory as factory
        new_session = FakeSession([])
        orig_create = getattr(factory, "create_session", None)
        factory.create_session = lambda email, password, **kw: new_session
        try:
            c = HonClient(email="e@x", password="p")
            cb = lambda _a: None  # noqa: E731
            c.subscribe_updates(cb)  # stored on the client (no session yet)
            # run setup_sync offline: stub the dedicated-loop machinery
            c._start_hon_loop = lambda: None  # type: ignore[assignment]
            c._run_on_hon_loop = lambda coro: coro.close()  # type: ignore[assignment]
            c.setup_sync()
            self.assertIs(c._hon_instance, new_session)
            self.assertIs(new_session._notify_function, cb)  # re-applied to new session
        finally:
            if orig_create is not None:
                factory.create_session = orig_create

    def test_setup_sync_without_subscribe_does_not_crash(self) -> None:
        # The constructor MUST init _notify_function: setup_sync runs at initial
        # setup BEFORE subscribe_updates is ever called, and reads it for the
        # re-apply. Without the init this raises AttributeError.
        import custom_components.addhon.client.factory as factory
        new_session = FakeSession([])
        orig_create = getattr(factory, "create_session", None)
        factory.create_session = lambda email, password, **kw: new_session
        try:
            c = HonClient(email="e@x", password="p")  # never subscribed
            c._start_hon_loop = lambda: None  # type: ignore[assignment]
            c._run_on_hon_loop = lambda coro: coro.close()  # type: ignore[assignment]
            c.setup_sync()  # must NOT raise
            self.assertIs(c._hon_instance, new_session)
            self.assertIsNone(new_session._notify_function)  # nothing to apply
        finally:
            if orig_create is not None:
                factory.create_session = orig_create

    def test_setup_sync_failure_closes_and_keeps_callback(self) -> None:
        # On a failed (re)setup the session is closed (_hon_instance cleared) but the
        # stored callback PERSISTS on the client, ready for the next setup (#20).
        import custom_components.addhon.client.factory as factory

        class BoomSession(FakeSession):
            async def __aenter__(self):
                raise RuntimeError("login boom")

        boom = BoomSession([])
        orig_create = getattr(factory, "create_session", None)
        factory.create_session = lambda email, password, **kw: boom
        try:
            c = HonClient(email="e@x", password="p")
            cb = lambda _a: None  # noqa: E731
            c.subscribe_updates(cb)
            c._start_hon_loop = lambda: None  # type: ignore[assignment]
            c._run_on_hon_loop = lambda coro: asyncio.run(coro)  # type: ignore[assignment]
            with self.assertRaises(Exception):
                c.setup_sync()
            self.assertIsNone(c._hon_instance)  # _close_sync ran on failure
            self.assertIs(c._notify_function, cb)  # callback survives for next setup
        finally:
            if orig_create is not None:
                factory.create_session = orig_create

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


class DiscoveryLogRedactionTest(unittest.TestCase):
    """The poll/discovery DEBUG logs (incl. the 'Updated' line) must redact the
    MAC, the appliance_id (= MAC/serial) and the nick_name, never log them raw."""

    def test_discovery_and_updated_logs_redact_identity(self) -> None:
        mac = "AA:BB:CC:DD:EE:FF"
        nick = "NickSecret42"
        app = types.SimpleNamespace(
            mac_address=mac,
            unique_id=mac,
            appliance_type="REF",
            nick_name=nick,
            attributes={},
            settings={},
            statistics={},
        )
        c = HonClient(email="e@x", password="p")

        async def fake_get_appliances():
            return [app]

        c.async_get_appliances = fake_get_appliances  # type: ignore[assignment]
        c._update_appliance_sync = lambda a: None  # type: ignore[assignment]

        logger = "custom_components.addhon.hon_client"
        with self.assertLogs(logger, level="DEBUG") as cm:
            data = asyncio.run(c.async_get_appliances_data())
        blob = "\n".join(cm.output)
        self.assertNotIn(mac, blob)  # neither mac= nor id= leak the MAC
        self.assertNotIn(nick, blob)  # nick_name (= identity) must not leak either
        self.assertIn("***", blob)
        self.assertTrue(any("Updated" in line for line in cm.output))
        # the coordinator DATA still carries the real MAC + nick (data, not a log)
        self.assertIn(mac, data)
        self.assertEqual(data[mac]["name"], nick)

    def test_update_error_warning_redacts_nick_name(self) -> None:
        # The per-appliance error path logs a WARNING with the nick_name and (first
        # poll) re-raises a coded error (CR#6: HonCodedError preserving the cause).
        # Neither the WARNING nor the raised error must leak the nick.
        nick = "NickSecret42"
        app = types.SimpleNamespace(
            mac_address="AA:BB:CC:DD:EE:FF",
            unique_id="AA:BB:CC:DD:EE:FF",
            appliance_type="REF",
            nick_name=nick,
            attributes={},
            settings={},
            statistics={},
        )
        c = HonClient(email="e@x", password="p")

        async def fake_get_appliances():
            return [app]

        def boom(_a):
            raise ValueError("update boom")

        c.async_get_appliances = fake_get_appliances  # type: ignore[assignment]
        c._update_appliance_sync = boom  # type: ignore[assignment]

        logger = "custom_components.addhon.hon_client"
        with self.assertLogs(logger, level="WARNING") as cm:
            with self.assertRaises(HonCodedError) as ctx:
                asyncio.run(c.async_get_appliances_data())
        blob = "\n".join(cm.output)
        self.assertTrue(any("Error updating" in line for line in cm.output))
        self.assertNotIn(nick, blob)  # WARNING must not leak the nick
        self.assertNotIn(nick, str(ctx.exception))  # nor the raised coded error

    def test_steady_state_partial_failure_warning_redacts_nick(self) -> None:
        # Steady state (resilient): one appliance fails, the other succeeds. The
        # 'Partial update' WARNING joins the failed appliances' names -> must redact.
        nick_ok, nick_bad = "OkNick", "BadSecretNick99"
        app_ok = types.SimpleNamespace(
            mac_address="AA:BB:CC:DD:EE:01", unique_id="AA:BB:CC:DD:EE:01",
            appliance_type="REF", nick_name=nick_ok,
            attributes={}, settings={}, statistics={},
        )
        app_bad = types.SimpleNamespace(
            mac_address="AA:BB:CC:DD:EE:02", unique_id="AA:BB:CC:DD:EE:02",
            appliance_type="REF", nick_name=nick_bad,
            attributes={}, settings={}, statistics={},
        )
        c = HonClient(email="e@x", password="p")
        c._first_poll_done = True  # steady state -> skip a failed appliance, keep the rest

        async def fake_get_appliances():
            return [app_ok, app_bad]

        def update(a):
            if a is app_bad:
                raise ValueError("update boom")

        c.async_get_appliances = fake_get_appliances  # type: ignore[assignment]
        c._update_appliance_sync = update  # type: ignore[assignment]

        logger = "custom_components.addhon.hon_client"
        with self.assertLogs(logger, level="WARNING") as cm:
            data = asyncio.run(c.async_get_appliances_data())
        blob = "\n".join(cm.output)
        self.assertTrue(any("Partial update" in line for line in cm.output))
        self.assertNotIn(nick_bad, blob)  # neither the per-appliance nor the joined list
        self.assertIn(app_ok.unique_id, data)  # the healthy appliance survives

    def test_steady_state_total_failure_warning_redacts_nick(self) -> None:
        # Steady state, EVERY appliance fails (CR#6): the all-failed summary WARNING
        # joins the failed names (must redact) and the raised coded error carries no
        # identity in its message.
        nick1, nick2 = "SecretNickA1", "SecretNickB2"
        app1 = types.SimpleNamespace(
            mac_address="AA:BB:CC:DD:EE:11", unique_id="AA:BB:CC:DD:EE:11",
            appliance_type="REF", nick_name=nick1,
            attributes={}, settings={}, statistics={},
        )
        app2 = types.SimpleNamespace(
            mac_address="AA:BB:CC:DD:EE:12", unique_id="AA:BB:CC:DD:EE:12",
            appliance_type="REF", nick_name=nick2,
            attributes={}, settings={}, statistics={},
        )
        c = HonClient(email="e@x", password="p")
        c._first_poll_done = True  # steady state -> all fail -> all-failed branch

        async def fake_get_appliances():
            return [app1, app2]

        c.async_get_appliances = fake_get_appliances  # type: ignore[assignment]
        c._update_appliance_sync = lambda a: (_ for _ in ()).throw(ValueError("update boom"))

        logger = "custom_components.addhon.hon_client"
        with self.assertLogs(logger, level="WARNING") as cm:
            with self.assertRaises(HonCodedError) as ctx:
                asyncio.run(c.async_get_appliances_data())
        blob = "\n".join(cm.output)
        self.assertTrue(any("Update failed for all" in line for line in cm.output))
        self.assertNotIn(nick1, blob)  # summary WARNING must redact the joined names
        self.assertNotIn(nick2, blob)
        self.assertNotIn(nick1, str(ctx.exception))  # nor the raised coded error
        self.assertNotIn(nick2, str(ctx.exception))


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
