"""Offline test of the native MQTT (NativeMqttClient, Phase 3 piece 4b).

awscrt/awsiot are stubbed in sys.modules (the module imports them at the top): so
we dry-test OUR logic - `stop()` (cancels+awaits the watchdog, stops the client)
and `_on_publish_received` (updates parameters/connection + notify, with the
defensive branches) - without network or native dependencies. The parts that use
the awscrt API (`_start`/`_subscribe`) are validated live.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
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
    # homeassistant (imported by custom_components/addhon/__init__.py)
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
    # awscrt.mqtt5 + awsiot.mqtt5_client_builder: the names are enough for the import.
    awscrt = _mod("awscrt")
    awscrt.mqtt5 = _mod("awscrt.mqtt5")
    awsiot = _mod("awsiot")
    awsiot.mqtt5_client_builder = _mod("awsiot.mqtt5_client_builder")


_install_stubs()

from custom_components.addhon.client.transport.mqtt import NativeMqttClient  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class FakeParam:
    def __init__(self) -> None:
        self.updated = None

    def update(self, value) -> None:
        self.updated = value


class FakeAppliance:
    def __init__(self, topic: str) -> None:
        self.info = {"topics": {"subscribe": [topic]}}
        self.attributes = {"parameters": {"temp": FakeParam()}}
        self.nick_name = "Nick"
        self.connection = True
        self.synced = []

    def sync_params_to_command(self, name: str) -> None:
        self.synced.append(name)


class FakeHon:
    def __init__(self, appliances) -> None:
        self.api = object()
        self.appliances = appliances
        self.notified = 0

    def notify(self) -> None:
        self.notified += 1


def _packet(topic: str, payload: dict):
    return types.SimpleNamespace(
        publish_packet=types.SimpleNamespace(
            topic=topic, payload=json.dumps(payload).encode()
        )
    )


class StopTest(unittest.TestCase):
    def test_stop_cancels_watchdog_and_stops_client(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

        async def body():
            m = NativeMqttClient(FakeHon([]), "MID")

            async def _forever():
                while True:
                    await asyncio.sleep(3600)

            m._watchdog_task = asyncio.ensure_future(_forever())
            await asyncio.sleep(0)  # let the watchdog start
            client = FakeClient()
            m._client = client
            await m.stop()
            return m, client

        m, client = _run(body())
        self.assertTrue(client.stopped)
        self.assertIsNone(m._client)
        self.assertIsNone(m._watchdog_task)

    def test_stop_idempotent_no_client_no_task(self) -> None:
        m = NativeMqttClient(FakeHon([]), "MID")
        _run(m.stop())  # no client/watchdog -> no-op, does not raise
        _run(m.stop())


class CreatePathTest(unittest.TestCase):
    """Drives the REAL path create()->_start->_subscribe->watchdog with richer awscrt
    stubs: catches a wiring error (builder/subscribe) invisible to the other tests
    (which mock or skip _start)."""

    def test_create_builds_client_and_subscribes(self) -> None:
        import awscrt
        import awsiot

        import concurrent.futures

        calls = {}

        class FakeClient:
            def __init__(self) -> None:
                self.started = False
                self.subscribed = []
                self.stopped = False

            def start(self) -> None:
                self.started = True

            def subscribe(self, packet):
                # awscrt subscribe() returns a concurrent.futures.Future; the code now
                # awaits it via asyncio.wrap_future, so a resolved Future is required.
                self.subscribed.append(packet)
                fut: concurrent.futures.Future = concurrent.futures.Future()
                fut.set_result(None)
                return fut

            def stop(self) -> None:
                self.stopped = True

        fake_client = FakeClient()

        def fake_builder(**kwargs):
            calls["builder"] = kwargs
            return fake_client

        # runtime stub of the awscrt API used by _start/_subscribe
        awsiot.mqtt5_client_builder.websockets_with_custom_authorizer = fake_builder
        awscrt.mqtt5.SubscribePacket = lambda subs: ("pkt", subs)
        awscrt.mqtt5.Subscription = lambda topic: ("sub", topic)

        class FakeAuth:
            id_token = "IDT"

        class FakeApi:
            auth = FakeAuth()

            async def load_aws_token(self):
                return "SIGNED"

        app = FakeAppliance("haier/MAC/appliancestatus")
        hon = FakeHon([app])
        hon.api = FakeApi()

        async def body():
            # create + stop in the SAME loop (the watchdog task is bound to the loop)
            m = NativeMqttClient(hon, "MID")
            await m.create()
            had_watchdog = m._watchdog_task is not None
            await m.stop()
            return m, had_watchdog

        m, had_watchdog = _run(body())
        # builder called with the expected args
        b = calls["builder"]
        self.assertEqual(b["auth_authorizer_signature"], "SIGNED")
        self.assertEqual(b["auth_token_value"], "IDT")
        self.assertEqual(b["auth_token_key_name"], "token")
        self.assertTrue(b["client_id"].startswith("MID_"))
        # client started + subscribe for each topic + watchdog created and then stopped
        self.assertTrue(fake_client.started)
        self.assertEqual(len(fake_client.subscribed), 1)
        self.assertTrue(had_watchdog)
        self.assertTrue(fake_client.stopped)
        self.assertIsNone(m._watchdog_task)


class PublishReceivedTest(unittest.TestCase):
    def _client(self, appliance):
        hon = FakeHon([appliance])
        return NativeMqttClient(hon, "MID"), hon

    def test_appliancestatus_updates_params_and_notifies(self) -> None:
        topic = "haier/things/MAC/event/appliancestatus/update"
        app = FakeAppliance(topic)
        m, hon = self._client(app)
        m._on_publish_received(_packet(topic, {"parameters": [
            {"parName": "temp", "parValue": "5"},
            {"parName": "ignota", "parValue": "x"},  # not in parameters -> skip (defensive)
        ]}))
        self.assertEqual(app.attributes["parameters"]["temp"].updated,
                         {"parName": "temp", "parValue": "5"})
        self.assertEqual(app.synced, ["settings"])
        self.assertEqual(hon.notified, 1)

    _LOGGER_NAME = "custom_components.addhon.client.transport.mqtt"

    def test_non_dict_parameter_skipped_valid_applied(self) -> None:
        # A null/garbage element must be skipped; the valid params in the SAME batch
        # are still applied and notify still fires (no whole-message drop).
        topic = "haier/things/MAC/event/appliancestatus/update"
        app = FakeAppliance(topic)
        m, hon = self._client(app)
        m._on_publish_received(_packet(topic, {"parameters": [
            None,
            {"parName": "temp", "parValue": "5"},
            "garbage",
        ]}))
        self.assertEqual(app.attributes["parameters"]["temp"].updated,
                         {"parName": "temp", "parValue": "5"})
        self.assertEqual(app.synced, ["settings"])
        self.assertEqual(hon.notified, 1)

    def test_parameters_not_a_list_does_not_drop(self) -> None:
        topic = "haier/things/MAC/event/appliancestatus/update"
        app = FakeAppliance(topic)
        m, hon = self._client(app)
        m._on_publish_received(_packet(topic, {"parameters": None}))
        m._on_publish_received(_packet(topic, {"parameters": {"x": 1}}))
        self.assertEqual(hon.notified, 2)  # both still processed + notified

    def test_non_dict_parameter_no_warning(self) -> None:
        # Dirty cloud data is DEBUG, not WARNING (WARNING is reserved for real bugs).
        topic = "haier/things/MAC/event/appliancestatus/update"
        app = FakeAppliance(topic)
        m, _ = self._client(app)
        with self.assertNoLogs(self._LOGGER_NAME, level="WARNING"):
            m._on_publish_received(_packet(topic, {"parameters": [None, "x"]}))

    def test_appliance_topics_null_does_not_block_others(self) -> None:
        # An appliance whose info["topics"] is null must not break the topic lookup
        # for the OTHER appliances.
        good = FakeAppliance("haier/X/appliancestatus")
        bad = FakeAppliance("haier/Y/appliancestatus")
        bad.info = {"topics": None}
        hon = FakeHon([bad, good])
        m = NativeMqttClient(hon, "MID")
        m._on_publish_received(_packet("haier/X/appliancestatus",
                                       {"parameters": [{"parName": "temp", "parValue": "9"}]}))
        self.assertEqual(good.attributes["parameters"]["temp"].updated,
                         {"parName": "temp", "parValue": "9"})
        self.assertEqual(hon.notified, 1)

    def test_disconnected_sets_connection_false(self) -> None:
        topic = "haier/things/MAC/event/disconnected"
        app = FakeAppliance(topic)
        m, hon = self._client(app)
        m._on_publish_received(_packet(topic, {"disconnectReason": "x"}))
        self.assertFalse(app.connection)
        self.assertEqual(hon.notified, 1)

    def test_connected_sets_connection_true(self) -> None:
        topic = "haier/things/MAC/event/connected"
        app = FakeAppliance(topic)
        app.connection = False
        m, _ = self._client(app)
        m._on_publish_received(_packet(topic, {}))
        self.assertTrue(app.connection)

    def test_unknown_topic_no_crash_no_notify(self) -> None:
        # topic that matches no appliance -> exits without crashing (defensive:
        # pyhOn did next(...) -> StopIteration).
        app = FakeAppliance("haier/known/appliancestatus")
        m, hon = self._client(app)
        m._on_publish_received(_packet("haier/UNKNOWN/topic", {"parameters": []}))
        self.assertEqual(hon.notified, 0)

    def test_empty_payload_ignored(self) -> None:
        app = FakeAppliance("t/appliancestatus")
        m, hon = self._client(app)
        m._on_publish_received(types.SimpleNamespace(publish_packet=None))
        self.assertEqual(hon.notified, 0)

    def test_non_json_payload_skipped_no_crash(self) -> None:
        # Undecodable / non-JSON bytes -> skipped, not raised (the callback runs on
        # an awscrt thread, a raise there would silence every later push).
        app = FakeAppliance("t/appliancestatus")
        m, hon = self._client(app)
        bad = types.SimpleNamespace(
            publish_packet=types.SimpleNamespace(
                topic="t/appliancestatus", payload=b"not json {"
            )
        )
        m._on_publish_received(bad)  # must not raise
        self.assertEqual(hon.notified, 0)

    def test_valid_json_but_non_object_skipped_no_crash(self) -> None:
        # Valid JSON that is NOT an object (a bare list): json.loads succeeds, but the
        # later payload.get(...) would raise AttributeError -> must be skipped.
        app = FakeAppliance("t/appliancestatus")
        m, hon = self._client(app)
        pkt = types.SimpleNamespace(
            publish_packet=types.SimpleNamespace(
                topic="t/appliancestatus", payload=json.dumps([1, 2, 3]).encode()
            )
        )
        m._on_publish_received(pkt)  # must not raise
        self.assertEqual(hon.notified, 0)


class StartReconnectTest(unittest.TestCase):
    """Covers the watchdog leak fix: a second _start() (reconnect) must stop the
    previous awscrt client before building a new one, instead of leaking it."""

    def test_start_stops_previous_client_before_rebuild(self) -> None:
        import awscrt
        import awsiot

        built: list = []

        class FakeClient:
            def __init__(self) -> None:
                self.started = False
                self.stopped = False

            def start(self) -> None:
                self.started = True

            def stop(self) -> None:
                self.stopped = True

        def fake_builder(**kwargs):
            client = FakeClient()
            built.append(client)
            return client

        awsiot.mqtt5_client_builder.websockets_with_custom_authorizer = fake_builder

        class FakeAuth:
            id_token = "IDT"

        class FakeApi:
            auth = FakeAuth()

            async def load_aws_token(self):
                return "SIGNED"

        hon = FakeHon([])
        hon.api = FakeApi()

        async def body():
            m = NativeMqttClient(hon, "MID")
            await m._start()  # first client
            await m._start()  # reconnect: must stop the first
            return m

        m = _run(body())
        self.assertEqual(len(built), 2)
        self.assertTrue(built[0].stopped)  # previous client stopped
        self.assertFalse(built[1].stopped)  # current client still alive
        self.assertIs(m._client, built[1])


class WatchdogThresholdTest(unittest.TestCase):
    """Covers the watchdog's sustained-downtime threshold: it must force a rebuild ONLY
    after _RECONNECT_AFTER_FAILED_TICKS consecutive down ticks, and reset the counter as
    soon as the connection comes back (an off-by-one or a missing reset would otherwise
    slip past the leak-fix tests, which only check _start in isolation)."""

    def _rebuilds_for(self, states):
        # Drive _watchdog over a scripted sequence of self._connection values (one per
        # tick) and count how many times it forces a rebuild (_start). asyncio.sleep is
        # stubbed to advance the script and to end the loop when the script runs out.
        import custom_components.addhon.client.transport.mqtt as mod

        m = NativeMqttClient(FakeHon([]), "MID")
        starts = []
        seq = list(states)

        async def fake_start():
            starts.append(True)

        async def fake_sleep(_interval):
            if not seq:
                raise asyncio.CancelledError
            m._connection = seq.pop(0)

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_appliances = lambda: None  # type: ignore[assignment]
        real_sleep = mod.asyncio.sleep
        mod.asyncio.sleep = fake_sleep
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(m._watchdog())
            except asyncio.CancelledError:
                pass
            finally:
                loop.close()
        finally:
            mod.asyncio.sleep = real_sleep
        return len(starts)

    def test_no_rebuild_before_threshold(self) -> None:
        # Two consecutive down ticks (< _RECONNECT_AFTER_FAILED_TICKS=3) -> no rebuild.
        self.assertEqual(self._rebuilds_for([False, False]), 0)

    def test_rebuild_after_sustained_downtime(self) -> None:
        # Exactly _RECONNECT_AFTER_FAILED_TICKS down ticks -> a single rebuild.
        self.assertEqual(self._rebuilds_for([False, False, False]), 1)

    def test_reconnect_resets_failed_ticks(self) -> None:
        # A healthy tick before the threshold resets the counter, so two down ticks on
        # either side of it never reach three-in-a-row -> no rebuild.
        self.assertEqual(self._rebuilds_for([False, False, True, False, False]), 0)


class StaleLifecycleCallbackTest(unittest.TestCase):
    """Covers the generation guard: after a rebuild, a late lifecycle event from the
    previous (stopped) client must NOT flip self._connection on the new one (awscrt
    stop() is async, so the old client can emit a disconnection after the new one
    connected)."""

    def test_stale_callback_does_not_flip_connection(self) -> None:
        m = NativeMqttClient(FakeHon([]), "MID")
        # Pretend we are on the 2nd client generation.
        m._generation = 2
        # The current generation's success marks the connection up.
        m._on_lifecycle_connection_success(None, generation=2)
        self.assertTrue(m._connection)
        # A late disconnection/failure from the OLD generation (1) is ignored.
        m._on_lifecycle_disconnection(None, generation=1)
        self.assertTrue(m._connection)
        m._on_lifecycle_connection_failure(None, generation=1)
        self.assertTrue(m._connection)
        # The current generation's disconnection still takes effect.
        m._on_lifecycle_disconnection(None, generation=2)
        self.assertFalse(m._connection)


class PublishReceivedRobustnessTest(unittest.TestCase):
    """ITEM B: a VALID dict payload that makes the engine raise (param.update,
    sync_params_to_command, or notify) must NOT propagate out of _on_publish_received
    (it runs on the awscrt callback thread, where a raise would silence every later
    push), and the failure must be logged at WARNING so it stays diagnosable."""

    _LOGGER_NAME = "custom_components.addhon.client.transport.mqtt"

    def test_raising_param_update_is_swallowed_and_warned(self) -> None:
        topic = "haier/things/MAC/event/appliancestatus/update"
        app = FakeAppliance(topic)

        def boom(_value) -> None:
            raise RuntimeError("engine update failed")

        app.attributes["parameters"]["temp"].update = boom
        hon = FakeHon([app])
        m = NativeMqttClient(hon, "MID")
        with self.assertLogs(self._LOGGER_NAME, level="WARNING") as cm:
            m._on_publish_received(_packet(topic, {"parameters": [
                {"parName": "temp", "parValue": "5"},
            ]}))  # must NOT raise
        self.assertEqual(hon.notified, 0)  # raised before notify()
        self.assertTrue(any("handler failed" in r.getMessage() for r in cm.records))

    def test_raising_notify_is_swallowed_and_warned(self) -> None:
        topic = "haier/things/MAC/event/connected"
        app = FakeAppliance(topic)
        hon = FakeHon([app])

        def boom() -> None:
            raise RuntimeError("notify failed")

        hon.notify = boom
        m = NativeMqttClient(hon, "MID")
        with self.assertLogs(self._LOGGER_NAME, level="WARNING") as cm:
            m._on_publish_received(_packet(topic, {}))  # must NOT raise
        self.assertTrue(any("handler failed" in r.getMessage() for r in cm.records))


class StartGenerationWiringTest(unittest.TestCase):
    """Covers the _start <-> guard wiring: _start must bind the state-mutating lifecycle
    callbacks to the CURRENT generation via functools.partial, so awscrt (which calls
    them with a single positional `data`) supplies the right generation and a callback
    captured from a previous client is ignored after a rebuild. Without this test a
    regression on the partial wiring (or the functools import) would pass every other
    test (the guard logic test sets the generation by hand, not through _start)."""

    def _make_client(self):
        import awsiot

        builds: list = []

        class FakeClient:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

        def fake_builder(**kwargs):
            builds.append(kwargs)
            return FakeClient()

        awsiot.mqtt5_client_builder.websockets_with_custom_authorizer = fake_builder

        class FakeAuth:
            id_token = "IDT"

        class FakeApi:
            auth = FakeAuth()

            async def load_aws_token(self):
                return "SIGNED"

        hon = FakeHon([])
        hon.api = FakeApi()
        return NativeMqttClient(hon, "MID"), builds

    def test_start_binds_callbacks_to_current_generation(self) -> None:
        m, builds = self._make_client()

        async def body():
            await m._start()  # generation 1
            await m._start()  # rebuild -> generation 2 (stops the first)

        _run(body())
        self.assertEqual(len(builds), 2)
        gen1, gen2 = builds[0], builds[1]
        # awscrt invokes the registered callback with a SINGLE positional `data`; the
        # partial must supply the generation it was bound to (a bare method would raise
        # TypeError here for the missing generation arg).
        gen2["on_lifecycle_connection_success"](None)  # current generation -> up
        self.assertTrue(m._connection)
        # A late disconnection from the FIRST client (old generation) must be ignored.
        gen1["on_lifecycle_disconnection"](None)
        self.assertTrue(m._connection)
        # The current client's disconnection still takes effect.
        gen2["on_lifecycle_disconnection"](None)
        self.assertFalse(m._connection)


class WatchdogResilienceTest(unittest.TestCase):
    """#3: the watchdog must survive a transient rebuild error (instead of dying and
    leaving realtime dead until a reload), re-raise CancelledError (else shutdown
    deadlocks), and back off (capped, reset on recovery) on repeated failures."""

    def _drive(self, states, start_fn):
        import custom_components.addhon.client.transport.mqtt as mod
        m = NativeMqttClient(FakeHon([]), "MID")
        intervals: list = []
        seq = list(states)

        async def fake_sleep(interval):
            intervals.append(interval)
            if not seq:
                raise asyncio.CancelledError
            m._connection = seq.pop(0)

        async def noop_sub():
            return None

        m._start = start_fn  # type: ignore[assignment]
        m._subscribe_appliances = noop_sub  # type: ignore[assignment]
        real = mod.asyncio.sleep
        mod.asyncio.sleep = fake_sleep
        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(m._watchdog())
            except asyncio.CancelledError:
                pass
            finally:
                loop.close()
        finally:
            mod.asyncio.sleep = real
        return intervals

    def test_watchdog_survives_start_raising(self) -> None:
        starts: list = []

        async def boom():
            starts.append(True)
            raise RuntimeError("load_aws_token 5xx")

        self._drive([False] * 7, boom)  # two rebuild windows
        # Pre-fix the first raise would end the task -> starts == 1. Surviving -> >= 2.
        self.assertGreaterEqual(len(starts), 2)

    def test_watchdog_cancelled_propagates(self) -> None:
        import custom_components.addhon.client.transport.mqtt as mod
        m = NativeMqttClient(FakeHon([]), "MID")

        async def fake_sleep(_i):
            raise asyncio.CancelledError

        async def noop_start():
            return None

        m._start = noop_start  # type: ignore[assignment]
        real = mod.asyncio.sleep
        mod.asyncio.sleep = fake_sleep
        try:
            loop = asyncio.new_event_loop()
            try:
                with self.assertRaises(asyncio.CancelledError):
                    loop.run_until_complete(m._watchdog())
            finally:
                loop.close()
        finally:
            mod.asyncio.sleep = real

    def test_watchdog_backoff_grows_and_resets(self) -> None:
        from custom_components.addhon.client.transport.mqtt import _WATCHDOG_INTERVAL

        async def boom():
            raise RuntimeError("x")

        intervals = self._drive([False] * 9 + [True], boom)
        base = _WATCHDOG_INTERVAL
        self.assertIn(base * 2, intervals)   # grew after the 1st rebuild failure
        self.assertIn(base * 3, intervals)   # grew again
        self.assertEqual(intervals[-1], base)  # reset after the recovery tick

    def test_watchdog_backoff_resets_after_successful_rebuild(self) -> None:
        # The backoff must reset when a rebuild SUCCEEDS (distinct from the
        # connection-up branch): _start may return OK while self._connection is
        # still False (the awscrt connection-success callback fires later).
        from custom_components.addhon.client.transport.mqtt import _WATCHDOG_INTERVAL
        calls = {"n": 0}

        async def flaky_start():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError("transient")

        intervals = self._drive([False] * 14, flaky_start)  # connection never comes up
        base = _WATCHDOG_INTERVAL
        self.assertIn(base * 3, intervals)     # backoff grew during the failures
        self.assertEqual(intervals[-1], base)  # ...and reset after the successful rebuild

    def test_watchdog_backoff_caps(self) -> None:
        from custom_components.addhon.client.transport.mqtt import (
            _WATCHDOG_INTERVAL,
            _RECONNECT_BACKOFF_CAP,
        )

        async def boom():
            raise RuntimeError("x")

        intervals = self._drive([False] * 60, boom)
        self.assertLessEqual(max(intervals), _WATCHDOG_INTERVAL + _RECONNECT_BACKOFF_CAP)


class SubscribeNonBlockingTest(unittest.TestCase):
    """#13: _subscribe must await the awscrt future (yield the loop) instead of a
    blocking .result(), preserving topic order and the timeout bound."""

    def _client(self, topics):
        import awscrt
        awscrt.mqtt5.SubscribePacket = lambda subs: ("pkt", subs)
        awscrt.mqtt5.Subscription = lambda topic: topic
        m = NativeMqttClient(FakeHon([]), "MID")
        app = FakeAppliance("t")
        app.info = {"topics": {"subscribe": topics}}
        return m, app

    def test_subscribe_order_preserved(self) -> None:
        order: list = []

        class C:
            def subscribe(self, packet):
                order.append(packet[1])  # ("pkt", [topic])
                fut: concurrent.futures.Future = concurrent.futures.Future()
                fut.set_result(None)
                return fut

        m, app = self._client(["t1", "t2", "t3"])
        m._client = C()
        _run(m._subscribe(app))
        self.assertEqual(order, [["t1"], ["t2"], ["t3"]])

    def test_subscribe_yields_loop(self) -> None:
        fut: concurrent.futures.Future = concurrent.futures.Future()

        class C:
            def subscribe(self, packet):
                return fut  # not resolved yet

        m, app = self._client(["t1"])
        m._client = C()
        progressed: list = []

        async def resolver():
            progressed.append(True)
            fut.set_result(None)

        async def body():
            await asyncio.gather(m._subscribe(app), resolver())

        _run(body())
        # If _subscribe blocked (old .result()), resolver could not run first.
        self.assertEqual(progressed, [True])

    def test_subscribe_timeout_bound(self) -> None:
        import custom_components.addhon.client.transport.mqtt as mod

        class C:
            def subscribe(self, packet):
                return concurrent.futures.Future()  # never resolves

        m, app = self._client(["t1"])
        m._client = C()
        orig = mod._SUBSCRIBE_TIMEOUT
        mod._SUBSCRIBE_TIMEOUT = 0.01
        try:
            with self.assertRaises(asyncio.TimeoutError):
                _run(m._subscribe(app))
        finally:
            mod._SUBSCRIBE_TIMEOUT = orig


if __name__ == "__main__":
    unittest.main()
