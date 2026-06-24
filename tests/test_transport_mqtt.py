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

    def test_create_stops_client_when_subscribe_fails(self) -> None:
        # #21: _start() already started the awscrt client; if a later step raises,
        # create() must stop it before re-raising (otherwise NativeHon never gets a
        # reference and the client leaks).
        class StoppableClient:
            def __init__(self) -> None:
                self.stops = 0

            def stop(self) -> None:
                self.stops += 1

        m = NativeMqttClient(FakeHon([]), "MID")
        client = StoppableClient()

        async def fake_start():
            m._client = client  # _start started the awscrt client

        async def boom_subscribe():
            raise asyncio.TimeoutError("subscribe timeout")

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_appliances = boom_subscribe  # type: ignore[assignment]

        with self.assertRaises(asyncio.TimeoutError):
            _run(m.create())
        self.assertEqual(client.stops, 1)   # stopped -> no leak
        self.assertIsNone(m._client)        # stop() cleared the reference

    def test_create_stops_client_when_watchdog_start_fails(self) -> None:
        # Same guarantee if the failure is in _start_watchdog (after subscribe).
        class StoppableClient:
            def __init__(self) -> None:
                self.stops = 0

            def stop(self) -> None:
                self.stops += 1

        m = NativeMqttClient(FakeHon([]), "MID")
        client = StoppableClient()

        async def fake_start():
            m._client = client

        async def ok_subscribe():
            return None

        async def boom_watchdog():
            raise RuntimeError("watchdog boom")

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_appliances = ok_subscribe  # type: ignore[assignment]
        m._start_watchdog = boom_watchdog  # type: ignore[assignment]

        with self.assertRaises(RuntimeError):
            _run(m.create())
        self.assertEqual(client.stops, 1)
        self.assertIsNone(m._client)

    def test_create_stops_client_on_cancellation(self) -> None:
        # The cleanup must catch BaseException (not just Exception): a setup cancelled
        # mid-subscribe (CancelledError) must still stop the started client.
        class StoppableClient:
            def __init__(self) -> None:
                self.stops = 0

            def stop(self) -> None:
                self.stops += 1

        m = NativeMqttClient(FakeHon([]), "MID")
        client = StoppableClient()

        async def fake_start():
            m._client = client

        async def cancelled_subscribe():
            raise asyncio.CancelledError

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_appliances = cancelled_subscribe  # type: ignore[assignment]

        with self.assertRaises(asyncio.CancelledError):
            _run(m.create())
        self.assertEqual(client.stops, 1)  # stopped despite cancellation
        self.assertIsNone(m._client)

    def test_create_stops_client_on_keyboardinterrupt(self) -> None:
        # Pins the `except BaseException` (vs a narrower (Exception, CancelledError)):
        # a non-Exception, non-CancelledError BaseException must still clean up.
        class StoppableClient:
            def __init__(self) -> None:
                self.stops = 0

            def stop(self) -> None:
                self.stops += 1

        m = NativeMqttClient(FakeHon([]), "MID")
        client = StoppableClient()

        async def fake_start():
            m._client = client

        async def boom_subscribe():
            raise KeyboardInterrupt

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_appliances = boom_subscribe  # type: ignore[assignment]

        with self.assertRaises(KeyboardInterrupt):
            _run(m.create())
        self.assertEqual(client.stops, 1)
        self.assertIsNone(m._client)

    def test_create_stops_client_when_start_method_raises(self) -> None:
        # Real _start path: the builder returns a client whose start() raises AFTER
        # _start assigned self._client; create() must stop it (covers the real
        # self.client.start() line, not just a stubbed _start).
        import awscrt
        import awsiot

        class FakeClient:
            def __init__(self) -> None:
                self.stops = 0

            def start(self) -> None:
                raise RuntimeError("native start boom")

            def stop(self) -> None:
                self.stops += 1

        fake_client = FakeClient()
        awsiot.mqtt5_client_builder.websockets_with_custom_authorizer = lambda **kw: fake_client
        awscrt.mqtt5.SubscribePacket = lambda subs: ("pkt", subs)
        awscrt.mqtt5.Subscription = lambda topic: topic

        class FakeAuth:
            id_token = "IDT"

        class FakeApi:
            auth = FakeAuth()

            async def load_aws_token(self):
                return "SIGNED"

        hon = FakeHon([])
        hon.api = FakeApi()
        m = NativeMqttClient(hon, "MID")
        with self.assertRaises(RuntimeError):
            _run(m.create())
        self.assertEqual(fake_client.stops, 1)
        self.assertIsNone(m._client)

    def test_double_stop_after_failed_create(self) -> None:
        # After create() self-cleaned, an extra stop() (e.g. from a later close())
        # is idempotent: the client is not stopped twice and stays cleared.
        class StoppableClient:
            def __init__(self) -> None:
                self.stops = 0

            def stop(self) -> None:
                self.stops += 1

        m = NativeMqttClient(FakeHon([]), "MID")
        client = StoppableClient()

        async def fake_start():
            m._client = client

        async def boom_subscribe():
            raise asyncio.TimeoutError

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_appliances = boom_subscribe  # type: ignore[assignment]

        with self.assertRaises(asyncio.TimeoutError):
            _run(m.create())
        self.assertEqual(client.stops, 1)
        _run(m.stop())  # second teardown
        self.assertEqual(client.stops, 1)  # not stopped again
        self.assertIsNone(m._client)


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

    def test_non_dict_parameter_does_not_leak_mac_value(self) -> None:
        # CR#4: a bare MAC-shaped element must NOT reach the log. The skip line logs
        # only the element TYPE, and the INFO summary masks the MAC leaf inside the
        # parameters list via redact_identity (key-based redaction can't reach a bare
        # list scalar, so redact_identity now masks MAC-shaped string leaves too).
        topic = "haier/things/MAC/event/appliancestatus/update"
        app = FakeAppliance(topic)
        m, hon = self._client(app)
        mac = "AA:BB:CC:DD:EE:FF"
        with self.assertLogs(self._LOGGER_NAME, level="DEBUG") as cm:
            m._on_publish_received(_packet(topic, {"parameters": [
                mac,
                {"parName": "temp", "parValue": "5"},
            ]}))
        blob = "\n".join(cm.output)
        self.assertNotIn(mac, blob)      # raw MAC never logged (skip line + INFO summary)
        self.assertIn("type=str", blob)  # skip line logs the element TYPE, not the value
        # behaviour unchanged: the valid param in the same batch is still applied
        self.assertEqual(app.attributes["parameters"]["temp"].updated,
                         {"parName": "temp", "parValue": "5"})
        self.assertEqual(hon.notified, 1)

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


class PublishReceivedRedactionTest(unittest.TestCase):
    """#32/#35: the MQTT handler must not log raw device identity (payload echoing
    macAddress/serial, nick_name) even when the MQTT logger is raised to INFO/DEBUG
    for troubleshooting."""

    _LOGGER_NAME = "custom_components.addhon.client.transport.mqtt"

    def _client(self, appliance):
        return NativeMqttClient(FakeHon([appliance]), "MID")

    def test_appliancestatus_payload_redacted_at_info(self) -> None:
        # #32: the generic INFO line logs the whole payload; a macAddress echoed in it
        # must be masked.
        topic = "haier/things/MAC/event/appliancestatus/update"
        app = FakeAppliance(topic)
        m = self._client(app)
        mac = "AA:BB:CC:DD:EE:FF"
        with self.assertLogs(self._LOGGER_NAME, level="INFO") as cm:
            m._on_publish_received(
                _packet(
                    topic,
                    {
                        "macAddress": mac,
                        "parameters": [{"parName": "temp", "parValue": "5"}],
                    },
                )
            )
        blob = "\n".join(cm.output)
        self.assertNotIn(mac, blob)
        self.assertIn("***", blob)

    def test_disconnected_nick_name_redacted_at_info(self) -> None:
        # #35: lifecycle logs nick_name at INFO -> must be masked.
        topic = "haier/things/MAC/event/disconnected"
        app = FakeAppliance(topic)
        app.nick_name = "MySecretNick"
        m = self._client(app)
        with self.assertLogs(self._LOGGER_NAME, level="INFO") as cm:
            m._on_publish_received(_packet(topic, {"disconnectReason": "x"}))
        blob = "\n".join(cm.output)
        self.assertNotIn("MySecretNick", blob)
        self.assertIn("Disconnected ***", blob)

    def test_connected_nick_name_redacted_at_info(self) -> None:
        topic = "haier/things/MAC/event/connected"
        app = FakeAppliance(topic)
        app.nick_name = "MySecretNick"
        m = self._client(app)
        with self.assertLogs(self._LOGGER_NAME, level="INFO") as cm:
            m._on_publish_received(_packet(topic, {}))
        blob = "\n".join(cm.output)
        self.assertNotIn("MySecretNick", blob)
        self.assertIn("Connected ***", blob)

    def test_topic_mac_redacted_at_info(self) -> None:
        # #32 (refuter gap): the Haier topic embeds the device MAC
        # (haier/things/<MAC>/...) and is logged at INFO right next to the payload.
        mac = "3c-71-bf-bd-32-2c"
        topic = f"haier/things/{mac}/event/appliancestatus/update"
        app = FakeAppliance(topic)
        m = self._client(app)
        with self.assertLogs(self._LOGGER_NAME, level="INFO") as cm:
            m._on_publish_received(
                _packet(topic, {"parameters": [{"parName": "temp", "parValue": "5"}]})
            )
        blob = "\n".join(cm.output)
        self.assertNotIn(mac, blob)
        self.assertIn("haier/things/***/event/appliancestatus/update", blob)

    def test_non_object_payload_logs_type_only(self) -> None:
        # #35 (refuter gap): a bare scalar JSON payload (e.g. a string that is a MAC)
        # must NOT be echoed; only its type is logged, and the topic MAC is masked.
        mac = "AA:BB:CC:DD:EE:FF"
        topic = f"haier/things/{mac}/event/appliancestatus"
        app = FakeAppliance(topic)
        m = self._client(app)
        pkt = types.SimpleNamespace(
            publish_packet=types.SimpleNamespace(
                topic=topic, payload=json.dumps(mac).encode()  # bare JSON string
            )
        )
        with self.assertLogs(self._LOGGER_NAME, level="DEBUG") as cm:
            m._on_publish_received(pkt)
        blob = "\n".join(cm.output)
        self.assertNotIn(mac, blob)
        self.assertIn("type=str", blob)


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
            # A scripted state is "healthy" (connected + subscribed) or fully "down":
            # the watchdog now requires BOTH flags to consider a tick healthy, so set
            # them together to keep this test about the failed-tick/rebuild threshold.
            state = seq.pop(0)
            m._connection = state
            m._subscribed = state

        async def noop_sub():
            return None

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_appliances = noop_sub  # type: ignore[assignment]
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
            # Healthy = connected AND subscribed (the watchdog now requires both); a
            # scripted state drives the pair together so this test stays focused on the
            # rebuild backoff, not the re-subscribe recovery path.
            state = seq.pop(0)
            m._connection = state
            m._subscribed = state

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
        from custom_components.addhon.error_codes import (
            MQTT_SUBSCRIBE_TIMEOUT,
            HonCodedError,
        )

        class C:
            def subscribe(self, packet):
                return concurrent.futures.Future()  # never resolves

        m, app = self._client(["t1"])
        m._client = C()
        orig = mod._SUBSCRIBE_TIMEOUT
        mod._SUBSCRIBE_TIMEOUT = 0.01
        try:
            # The bound still holds, but the stall is now surfaced as a coded error
            # (its cause is the underlying asyncio.TimeoutError).
            with self.assertRaises(HonCodedError) as ctx:
                _run(m._subscribe(app))
            self.assertIs(ctx.exception.error_code, MQTT_SUBSCRIBE_TIMEOUT)
            self.assertIsInstance(ctx.exception.__cause__, asyncio.TimeoutError)
        finally:
            mod._SUBSCRIBE_TIMEOUT = orig


class WatchdogSubscribedRecoveryTest(unittest.TestCase):
    """Greptile P1: a single `self._connection` flag conflated 'transport connected'
    with 'appliance topics subscribed'. The connection-success callback sets
    _connection=True WITHOUT subscribing, so a rebuild whose _subscribe_appliances()
    times out (or awscrt's own auto-reconnect on a clean session) left the client
    connected-but-unsubscribed; the watchdog's `if self._connection:` check then treated
    it as healthy and never recovered -> realtime pushes silently dead. The fix adds a
    separate self._subscribed flag and a re-subscribe recovery branch. These tests fail
    against the pre-fix one-flag watchdog and pass with the fix."""

    def _drive(self, scripts, sub_fn=None):
        """Drive the REAL _watchdog over a scripted sequence. Each script entry is a
        (connection, subscribed) pair applied to the instance at the START of a tick
        (i.e. it is the state the awscrt callbacks would have produced before this
        tick's health check). asyncio.sleep is stubbed to advance the script and end
        the loop (CancelledError) when it runs out. Returns (resub_calls, rebuilds,
        m), where `rebuilds` holds the 1-based tick index of each full _start()
        rebuild (so a test can pin WHEN the rebuild fired, not just that it did)."""
        import custom_components.addhon.client.transport.mqtt as mod

        m = NativeMqttClient(FakeHon([]), "MID")
        seq = list(scripts)
        rebuilds = []
        resub_calls = []
        tick = {"n": 0}

        async def fake_start():
            rebuilds.append(tick["n"])
            # The real _start() clears _subscribed (fresh client). Mirror that so a
            # rebuild that does not re-subscribe is correctly seen as unsubscribed.
            m._subscribed = False

        async def counted_sub():
            resub_calls.append(True)
            if sub_fn is not None:
                await sub_fn()

        async def fake_sleep(_interval):
            if not seq:
                raise asyncio.CancelledError
            tick["n"] += 1
            conn, sub = seq.pop(0)
            m._connection = conn
            m._subscribed = sub

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_appliances = counted_sub  # type: ignore[assignment]
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
        return resub_calls, rebuilds, m

    def test_connected_but_unsubscribed_triggers_resubscribe_not_healthy(self) -> None:
        # Connected but NOT subscribed for two ticks (the subscribe-timeout aftermath):
        # the watchdog must re-subscribe each tick (recovery), NOT treat it as healthy.
        # Pre-fix: `if self._connection:` -> healthy -> 0 re-subscribes (the bug).
        resub, rebuilds, m = self._drive([(True, False), (True, False)])
        self.assertEqual(len(resub), 2)        # re-subscribed on each tick
        self.assertEqual(len(rebuilds), 0)     # no full rebuild: transport is up
        self.assertTrue(m._subscribed)         # recovered to healthy

    def test_resubscribe_failure_retries_next_tick_no_stuck_healthy(self) -> None:
        # If the in-place re-subscribe RAISES (e.g. MQTT_SUBSCRIBE_TIMEOUT), the
        # watchdog must NOT mark _subscribed True (no stuck "healthy") and must retry on
        # the next tick. Pre-fix this scenario could never arise (it was always healthy).
        from custom_components.addhon.error_codes import (
            HonCodedError,
            MQTT_SUBSCRIBE_TIMEOUT,
        )

        attempts = []

        async def flaky_sub():
            attempts.append(True)
            if len(attempts) == 1:
                raise HonCodedError(MQTT_SUBSCRIBE_TIMEOUT, "stall")

        # Tick 1: connected/unsubscribed -> re-subscribe raises -> backoff, stays
        # unsubscribed. Tick 2: still connected/unsubscribed -> re-subscribe succeeds.
        resub, rebuilds, m = self._drive(
            [(True, False), (True, False)], sub_fn=flaky_sub
        )
        self.assertEqual(len(resub), 2)     # retried after the first failure
        self.assertEqual(len(rebuilds), 0)  # never escalated to a full rebuild
        self.assertTrue(m._subscribed)      # recovered on the 2nd attempt

    def test_healthy_when_connected_and_subscribed(self) -> None:
        # Both flags set -> healthy -> neither re-subscribe nor rebuild.
        resub, rebuilds, _ = self._drive([(True, True), (True, True)])
        self.assertEqual(len(resub), 0)
        self.assertEqual(len(rebuilds), 0)

    def test_not_connected_rebuilds_not_resubscribes(self) -> None:
        # Sustained not-connected -> the rebuild path (NOT the in-place re-subscribe),
        # preserving the _RECONNECT_AFTER_FAILED_TICKS threshold semantics.
        from custom_components.addhon.client.transport.mqtt import (
            _RECONNECT_AFTER_FAILED_TICKS,
        )

        resub, rebuilds, _ = self._drive(
            [(False, False)] * _RECONNECT_AFTER_FAILED_TICKS
        )
        self.assertEqual(len(rebuilds), 1)   # one full rebuild after the threshold
        # The rebuild path re-subscribes once as part of the rebuild.
        self.assertEqual(len(resub), 1)

    def test_perpetual_resubscribe_failure_escalates_to_rebuild(self) -> None:
        # Issue 1: connected but _subscribe_appliances() ALWAYS raises (half-open socket
        # / stale custom-authorizer session that awscrt still reports connected and that
        # never fires a disconnection callback). The in-place re-subscribe branch must
        # NOT loop forever: after _MAX_RESUBSCRIBE_FAILURES it must escalate to a full
        # _start() rebuild (which is what refreshes the AWS token + tears down the dead
        # client).
        #
        # This test PINS THE TICK the rebuild fires on -- that is what actually exercises
        # the escalation branch. Two weaker designs would be vacuous: without the cap
        # gate the in-place re-subscribe loops forever and never rebuilds; with the gate
        # but without the prompt escalation-skip, the rebuild is merely DELAYED to the
        # slower failed_ticks fallback (tick _MAX_RESUBSCRIBE_FAILURES +
        # _RECONNECT_AFTER_FAILED_TICKS). Asserting `len(rebuilds) > 0` alone passes
        # against BOTH (the fallback still rebuilds inside a generous budget), so we
        # assert the FIRST rebuild lands on the very next tick after the cap is hit
        # (tick _MAX_RESUBSCRIBE_FAILURES + 1) -- only the escalation produces that.
        from custom_components.addhon.client.transport.mqtt import (
            _MAX_RESUBSCRIBE_FAILURES,
        )
        from custom_components.addhon.error_codes import (
            HonCodedError,
            MQTT_SUBSCRIBE_TIMEOUT,
        )

        async def always_fail():
            raise HonCodedError(MQTT_SUBSCRIBE_TIMEOUT, "stall")

        # Stay connected-but-unsubscribed for more than enough ticks to exceed the
        # threshold AND to give a (mutated) failed_ticks fallback room to fire later.
        ticks = _MAX_RESUBSCRIBE_FAILURES + 3
        resub, rebuilds, _ = self._drive(
            [(True, False)] * ticks, sub_fn=always_fail
        )
        # It only escalated AFTER exhausting the in-place re-subscribe attempts.
        self.assertGreaterEqual(len(resub), _MAX_RESUBSCRIBE_FAILURES)
        # Escalation fired (with escalation disabled, or the cap gate removed, this is 0).
        self.assertGreaterEqual(len(rebuilds), 1)
        # ...and PROMPTLY, on the first tick past the cap -- not via the slower
        # failed_ticks fallback. (1-based tick index recorded by _drive.)
        self.assertEqual(rebuilds[0], _MAX_RESUBSCRIBE_FAILURES + 1)

    def test_resubscribe_failure_counter_resets_on_success(self) -> None:
        # Issue 1 corollary: the escalation counter must reset on EVERY successful
        # subscribe, so an INTERMITTENT (not perpetual) re-subscribe failure never
        # accumulates to a needless rebuild. Drive an alternating fail/succeed pattern
        # whose TOTAL failures exceed _MAX_RESUBSCRIBE_FAILURES but where each failure is
        # followed by a success (which must reset the counter to 0). With the reset: 0
        # rebuilds. WITHOUT the reset: the failures accumulate past the threshold and the
        # watchdog wrongly escalates to a full rebuild.
        from custom_components.addhon.client.transport.mqtt import (
            _MAX_RESUBSCRIBE_FAILURES,
        )
        from custom_components.addhon.error_codes import (
            HonCodedError,
            MQTT_SUBSCRIBE_TIMEOUT,
        )

        attempts = {"n": 0}

        async def alternating():
            attempts["n"] += 1
            # Odd attempts fail, even attempts succeed: a fail is always followed by a
            # success that re-subscribes (and must reset the failure counter).
            if attempts["n"] % 2 == 1:
                raise HonCodedError(MQTT_SUBSCRIBE_TIMEOUT, "stall")

        # Enough fail/succeed pairs that the cumulative odd-attempt failures comfortably
        # exceed _MAX_RESUBSCRIBE_FAILURES (without the reset, escalation would fire).
        pairs = _MAX_RESUBSCRIBE_FAILURES + 2
        script = [(True, False)] * (pairs * 2)
        resub, rebuilds, _ = self._drive(script, sub_fn=alternating)
        self.assertEqual(len(rebuilds), 0)  # reset prevents intermittent escalation

    def test_failed_ticks_symmetry_resubscribe_recovery_resets_outage(self) -> None:
        # Issue 2: a successful in-place re-subscribe must reset failed_ticks, exactly
        # like the healthy branch. A flapping connection
        #   [down, up-but-unsubscribed->resubscribe-OK, down, down]
        # must NOT force a full rebuild, because the recovered tick resets the outage
        # counter (only _RECONNECT_AFTER_FAILED_TICKS *consecutive* down ticks rebuild).
        resub, rebuilds, _ = self._drive(
            [(False, False), (True, False), (False, False), (False, False)]
        )
        self.assertEqual(len(rebuilds), 0)  # recovered tick reset the outage counter
        self.assertEqual(len(resub), 1)     # the up-but-unsubscribed tick re-subscribed

    def test_failed_ticks_symmetry_contrast_full_outage_rebuilds(self) -> None:
        # Contrast to the symmetry test: an UNINTERRUPTED not-connected run of the same
        # length (no recovery tick to reset failed_ticks) DOES rebuild at the threshold.
        from custom_components.addhon.client.transport.mqtt import (
            _RECONNECT_AFTER_FAILED_TICKS,
        )

        resub, rebuilds, _ = self._drive(
            [(False, False)] * (_RECONNECT_AFTER_FAILED_TICKS + 1)
        )
        self.assertGreaterEqual(len(rebuilds), 1)  # no reset -> threshold reached

    def test_lost_update_disconnect_mid_resubscribe_not_marked_healthy(self) -> None:
        # Issue 3 at the re-subscribe site: a disconnection callback lands DURING the
        # in-place re-subscribe await (flips _connection=False on the awscrt thread).
        # The post-await assignment must re-check connection (and generation) instead of
        # an unconditional True, so _subscribed ends up False (not clobbered) and the
        # next tick is NOT treated as healthy.
        def make_dropper(m):
            async def dropping_sub():
                # Simulate the awscrt disconnection callback landing mid-subscribe.
                m._connection = False
            return dropping_sub

        import custom_components.addhon.client.transport.mqtt as mod

        m = NativeMqttClient(FakeHon([]), "MID")
        # One tick: connected/unsubscribed -> re-subscribe runs, but the connection drops
        # during the await. Then a second scripted tick is NOT applied (loop ends), so we
        # observe the state the re-subscribe left.
        seq = [(True, False)]
        rebuilds = []
        resub_calls = []

        async def fake_start():
            rebuilds.append(True)
            m._subscribed = False

        dropping = make_dropper(m)

        async def counted_sub():
            resub_calls.append(True)
            await dropping()

        async def fake_sleep(_interval):
            if not seq:
                raise asyncio.CancelledError
            conn, sub = seq.pop(0)
            m._connection = conn
            m._subscribed = sub

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_appliances = counted_sub  # type: ignore[assignment]
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

        self.assertEqual(len(resub_calls), 1)   # re-subscribe attempted
        self.assertFalse(m._subscribed)         # NOT clobbered back to True
        self.assertFalse(m._connection)         # the disconnect stuck
        self.assertEqual(len(rebuilds), 0)      # one tick only: no rebuild yet

    def test_lost_update_disconnect_mid_rebuild_subscribe_not_marked_healthy(self) -> None:
        # Issue 3 at the REBUILD site: after a full _start() (which models the awscrt
        # connection-success flipping _connection=True), the rebuild's subscribe is
        # interrupted by a disconnection callback (flips _connection=False mid-await).
        # The post-await assignment must re-check connection (and generation), so the
        # rebuild does NOT leave a stuck "healthy on a dropped session".
        import custom_components.addhon.client.transport.mqtt as mod
        from custom_components.addhon.client.transport.mqtt import (
            _RECONNECT_AFTER_FAILED_TICKS,
        )

        m = NativeMqttClient(FakeHon([]), "MID")
        # Sustained not-connected long enough to trip the rebuild threshold; the rebuild's
        # _start() then brings the transport up, but the subscribe drops it again.
        seq = [(False, False)] * _RECONNECT_AFTER_FAILED_TICKS
        rebuilds = []
        resub_calls = []

        async def fake_start():
            rebuilds.append(True)
            m._subscribed = False
            # _start() built a fresh client; model the awscrt success callback bringing
            # the transport up before the subscribe runs.
            m._connection = True

        async def counted_sub():
            resub_calls.append(True)
            # The disconnection callback lands on the awscrt thread mid-subscribe.
            m._connection = False

        async def fake_sleep(_interval):
            if not seq:
                raise asyncio.CancelledError
            conn, sub = seq.pop(0)
            m._connection = conn
            m._subscribed = sub

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_appliances = counted_sub  # type: ignore[assignment]
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

        self.assertEqual(len(rebuilds), 1)   # the rebuild ran
        self.assertEqual(len(resub_calls), 1)
        self.assertFalse(m._subscribed)      # NOT clobbered back to True
        self.assertFalse(m._connection)      # the mid-subscribe disconnect stuck


class SubscribedFlagLifecycleTest(unittest.TestCase):
    """The companion of WatchdogSubscribedRecoveryTest at the flag level: _subscribed
    must be reset by the disconnection/failure callbacks (respecting the generation
    guard) and by _start(), and set only after _subscribe_appliances() succeeds."""

    def test_disconnection_resets_subscribed(self) -> None:
        m = NativeMqttClient(FakeHon([]), "MID")
        m._generation = 1
        m._connection = True
        m._subscribed = True
        m._on_lifecycle_disconnection(None, generation=1)
        self.assertFalse(m._connection)
        self.assertFalse(m._subscribed)

    def test_connection_failure_resets_subscribed(self) -> None:
        m = NativeMqttClient(FakeHon([]), "MID")
        m._generation = 1
        m._connection = True
        m._subscribed = True
        m._on_lifecycle_connection_failure(None, generation=1)
        self.assertFalse(m._connection)
        self.assertFalse(m._subscribed)

    def test_connection_success_does_not_set_subscribed(self) -> None:
        # The success callback only flips _connection: it does NOT subscribe, so
        # _subscribed must stay False (this is exactly the connected-but-unsubscribed
        # window the watchdog must recover, incl. awscrt's clean-session auto-reconnect).
        m = NativeMqttClient(FakeHon([]), "MID")
        m._generation = 1
        m._on_lifecycle_connection_success(None, generation=1)
        self.assertTrue(m._connection)
        self.assertFalse(m._subscribed)

    def test_stale_disconnection_does_not_reset_subscribed(self) -> None:
        # A late event from an OLD generation must not clear _subscribed on the current
        # healthy client (same guard that protects _connection).
        m = NativeMqttClient(FakeHon([]), "MID")
        m._generation = 2
        m._connection = True
        m._subscribed = True
        m._on_lifecycle_disconnection(None, generation=1)  # stale
        self.assertTrue(m._connection)
        self.assertTrue(m._subscribed)

    def test_start_resets_subscribed(self) -> None:
        import awscrt
        import awsiot

        class FakeClient:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

        awsiot.mqtt5_client_builder.websockets_with_custom_authorizer = (
            lambda **kw: FakeClient()
        )

        class FakeAuth:
            id_token = "IDT"

        class FakeApi:
            auth = FakeAuth()

            async def load_aws_token(self):
                return "SIGNED"

        hon = FakeHon([])
        hon.api = FakeApi()
        m = NativeMqttClient(hon, "MID")
        m._subscribed = True  # pretend a previous client was subscribed

        _run(m._start())
        self.assertFalse(m._subscribed)  # fresh client -> no subscriptions

    def test_create_sets_subscribed_true_on_success(self) -> None:
        m = NativeMqttClient(FakeHon([]), "MID")

        async def fake_start():
            m._client = object()
            # The real _start() builds the client; the awscrt connection-success
            # callback then flips _connection=True before subscribe completes. create()'s
            # lost-update guard (Issue 3) only marks _subscribed when _connection is up,
            # so model the connected transport here.
            m._connection = True

        async def ok_sub():
            return None

        async def ok_watchdog():
            return None

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_appliances = ok_sub  # type: ignore[assignment]
        m._start_watchdog = ok_watchdog  # type: ignore[assignment]
        _run(m.create())
        self.assertTrue(m._subscribed)

    def test_create_does_not_clobber_subscribed_when_disconnect_lands_mid_subscribe(
        self,
    ) -> None:
        # Issue 3 at the create() site: if a disconnection callback lands DURING the
        # initial subscribe (flips _connection=False), create() must NOT clobber
        # _subscribed back to True with an unconditional assignment.
        m = NativeMqttClient(FakeHon([]), "MID")

        async def fake_start():
            m._client = object()
            m._connection = True

        async def dropping_sub():
            # The awscrt thread fires a disconnection mid-subscribe.
            m._connection = False
            m._subscribed = False

        async def ok_watchdog():
            return None

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_appliances = dropping_sub  # type: ignore[assignment]
        m._start_watchdog = ok_watchdog  # type: ignore[assignment]
        _run(m.create())
        self.assertFalse(m._subscribed)  # not clobbered back to healthy


if __name__ == "__main__":
    unittest.main()
