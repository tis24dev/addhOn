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
        # Mirrors HonAppliance.mark_realtime_seen book-keeping so the transport tests
        # can assert the liveness time the handler records.
        self.realtime_seen = []
        self.disconnected_calls = 0

    def sync_params_to_command(self, name: str) -> None:
        self.synced.append(name)

    def mark_realtime_seen(self, timestamp=None) -> None:
        # Positive-only, mirroring the engine: realtime traffic -> connected, and the
        # cloud timestamp is remembered for the test to inspect.
        self.connection = True
        self.realtime_seen.append(timestamp)

    def mark_realtime_disconnected(self) -> None:
        # Mirrors the engine: explicit negative evidence clears the liveness marks.
        self.connection = False
        self.disconnected_calls += 1


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
        m._subscribe_missing = boom_subscribe  # type: ignore[assignment]

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
        m._subscribe_missing = ok_subscribe  # type: ignore[assignment]
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
        m._subscribe_missing = cancelled_subscribe  # type: ignore[assignment]

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
        m._subscribe_missing = boom_subscribe  # type: ignore[assignment]

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
        m._subscribe_missing = boom_subscribe  # type: ignore[assignment]

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

    def test_appliancestatus_marks_realtime_seen_with_payload_timestamp(self) -> None:
        # Realtime traffic is authoritative connectivity evidence: the handler must mark
        # the appliance live and pass the CLOUD payload `timestamp` (not wall-clock) to
        # mark_realtime_seen, so the engine can reconcile it against a stale lastConnEvent.
        topic = "haier/things/MAC/event/appliancestatus/update"
        app = FakeAppliance(topic)
        app.connection = False  # previously reported offline (stale REST disconnect)
        m, hon = self._client(app)
        ts = "2026-06-25T16:04:21.1Z"
        m._on_publish_received(_packet(topic, {
            "timestamp": ts,
            "parameters": [{"parName": "temp", "parValue": "5"}],
        }))
        self.assertEqual(app.realtime_seen, [ts])   # cloud timestamp forwarded
        self.assertTrue(app.connection)             # marked live again
        self.assertEqual(hon.notified, 1)

    def test_appliancestatus_marks_realtime_seen_even_without_timestamp(self) -> None:
        # No payload timestamp: the message itself is still evidence -> mark_realtime_seen
        # is called (with None) so connection is set live; the engine then declines the
        # stale-disconnect protection for lack of an orderable time.
        topic = "haier/things/MAC/event/appliancestatus/update"
        app = FakeAppliance(topic)
        m, _ = self._client(app)
        m._on_publish_received(_packet(topic, {
            "parameters": [{"parName": "temp", "parValue": "5"}],
        }))
        self.assertEqual(app.realtime_seen, [None])

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

    def test_session_presence_connected_does_not_touch_connectivity(self) -> None:
        # `$aws/events/presence/connected/<clientId>` is OUR client's session presence, not
        # the appliance's connectivity: it must NOT arm liveness NOR even set connection.
        topic = "$aws/events/presence/connected/myclient"
        app = FakeAppliance(topic)
        app.connection = False  # genuinely offline
        m, _ = self._client(app)
        m._on_publish_received(_packet(topic, {"clientId": "myclient", "eventType": "connected"}))
        self.assertFalse(app.connection)        # not pinned online by our session
        self.assertEqual(app.realtime_seen, [])  # liveness not armed

    def test_session_presence_disconnected_does_not_clobber_live_appliance(self) -> None:
        # `$aws/events/presence/disconnected/<clientId>` is OUR client's session blip; it
        # must NOT clear the appliance's liveness marks (which would knock a live, streaming
        # appliance offline non-self-correcting while the cloud lastConnEvent stays stale).
        topic = "$aws/events/presence/disconnected/myclient"
        app = FakeAppliance(topic)
        app.connection = True  # appliance is live (streaming appliancestatus)
        m, _ = self._client(app)
        m._on_publish_received(_packet(topic, {"clientId": "myclient", "eventType": "disconnected"}))
        self.assertTrue(app.connection)          # still live
        self.assertEqual(app.disconnected_calls, 0)  # marks NOT cleared

    def test_connected_does_not_arm_realtime_liveness(self) -> None:
        # The presence "connected" event is OUR client's AWS-IoT session presence, not the
        # appliance's connectivity. It must set connection True (transient, self-corrected
        # at the next poll) but must NOT record realtime liveness -- otherwise periodic
        # client reconnects would pin a genuinely offline appliance online indefinitely.
        topic = "haier/things/MAC/event/connected"
        app = FakeAppliance(topic)
        app.connection = False
        m, _ = self._client(app)
        m._on_publish_received(_packet(topic, {"timestamp": "2026-06-25T16:04:16Z"}))
        self.assertTrue(app.connection)
        self.assertEqual(app.realtime_seen, [])  # liveness NOT armed by presence

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
            # FakeHon([]) has no appliances, so _all_topics() is empty: with the set
            # model "fully subscribed" reduces to "connected" (no missing topics). A
            # scripted True is healthy, a scripted False routes to the rebuild path --
            # which keeps this test about the failed-tick/rebuild threshold.
            state = seq.pop(0)
            m._connection = state

        async def noop_sub():
            return None

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_missing = noop_sub  # type: ignore[assignment]
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
            # FakeHon([]) -> _all_topics() empty -> "fully subscribed" reduces to
            # "connected": a scripted True is healthy, a False routes to the rebuild
            # path. Keeps this test focused on the rebuild backoff, not the re-subscribe
            # recovery path.
            state = seq.pop(0)
            m._connection = state

        async def noop_sub():
            return None

        m._start = start_fn  # type: ignore[assignment]
        m._subscribe_missing = noop_sub  # type: ignore[assignment]
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


_RECOVERY_TOPIC = "haier/things/MAC/event/appliancestatus/update"


class WatchdogSubscribedRecoveryTest(unittest.TestCase):
    """Greptile P1 + H1: a single `self._connection` flag conflated 'transport
    connected' with 'appliance topics subscribed'. The connection-success callback sets
    _connection=True WITHOUT subscribing, so a rebuild whose subscribe times out (or
    awscrt's own auto-reconnect on a clean session) left the client connected-but-
    unsubscribed; the watchdog's `if self._connection:` check then treated it as healthy
    and never recovered -> realtime pushes silently dead. The fix replaces the binary
    flag with a SET of subscribed topics: the watchdog re-subscribes only the MISSING
    topics in place, and escalates to a rebuild ONLY on a total blackout (nothing
    subscribed = dead connection), never on a partial miss (one bad appliance). These
    tests fail against the pre-fix one-flag watchdog and pass with the fix."""

    def _drive(self, scripts, sub_fn=None):
        """Drive the REAL _watchdog over a scripted sequence. Each script entry is a
        (connection, subscribed) pair applied to the instance at the START of a tick
        (i.e. it is the state the awscrt callbacks would have produced before this
        tick's health check). `subscribed` True means the single target topic is in the
        set, False means it is missing. asyncio.sleep is stubbed to advance the script
        and end the loop (CancelledError) when it runs out. Returns (resub_calls,
        rebuilds, m), where `rebuilds` holds the 1-based tick index of each full _start()
        rebuild (so a test can pin WHEN the rebuild fired, not just that it did)."""
        import custom_components.addhon.client.transport.mqtt as mod

        # ONE appliance/topic: _all_topics() == {_RECOVERY_TOPIC}, so "fully subscribed"
        # is exactly "the topic is in the set". With a single topic, a subscribe failure
        # is a TOTAL blackout (the escalation trigger); a success fully subscribes.
        m = NativeMqttClient(FakeHon([FakeAppliance(_RECOVERY_TOPIC)]), "MID")
        seq = list(scripts)
        rebuilds = []
        resub_calls = []
        tick = {"n": 0}

        async def fake_start():
            rebuilds.append(tick["n"])
            # The real _start() clears the set (fresh client). Mirror that so a rebuild
            # that does not re-subscribe is correctly seen as unsubscribed.
            m._subscribed_topics_set = set()

        async def counted_sub():
            # Replaces the real _subscribe_missing(): record the call, then either fail
            # (sub_fn raises -> leave the topic out of the set = blackout, mirroring the
            # real method which SWALLOWS a per-topic HonCodedError without adding it and
            # returns normally) or succeed (add the single target topic).
            from custom_components.addhon.error_codes import HonCodedError

            resub_calls.append(True)
            if sub_fn is not None:
                try:
                    await sub_fn()
                except HonCodedError:
                    return  # per-topic failure swallowed, topic stays missing
            m._subscribed_topics_set = m._subscribed_topics_set | {_RECOVERY_TOPIC}

        async def fake_sleep(_interval):
            if not seq:
                raise asyncio.CancelledError
            tick["n"] += 1
            conn, sub = seq.pop(0)
            m._connection = conn
            m._subscribed_topics_set = {_RECOVERY_TOPIC} if sub else set()

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_missing = counted_sub  # type: ignore[assignment]
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
        # recovered to fully subscribed (the single target topic is in the set)
        self.assertEqual(m._subscribed_topics_set, {_RECOVERY_TOPIC})

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
        # recovered on the 2nd attempt (the target topic is now in the set)
        self.assertEqual(m._subscribed_topics_set, {_RECOVERY_TOPIC})

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
        # The post-await commit must re-check connection (and generation) instead of
        # trusting the set, so the set ends EMPTY (not committed) and the next tick is
        # NOT treated as healthy.
        import custom_components.addhon.client.transport.mqtt as mod

        m = NativeMqttClient(FakeHon([FakeAppliance(_RECOVERY_TOPIC)]), "MID")
        # One tick: connected/unsubscribed -> re-subscribe runs, but the connection drops
        # during the await. Then a second scripted tick is NOT applied (loop ends), so we
        # observe the state the re-subscribe left.
        seq = [(True, False)]
        rebuilds = []
        resub_calls = []

        async def fake_start():
            rebuilds.append(True)
            m._subscribed_topics_set = set()

        async def counted_sub():
            resub_calls.append(True)
            # The subscribe itself succeeds (topic added), but the awscrt disconnection
            # callback lands mid-await and flips _connection=False. The watchdog's
            # post-await guard must then DISCARD the committed set.
            m._subscribed_topics_set = m._subscribed_topics_set | {_RECOVERY_TOPIC}
            m._connection = False

        async def fake_sleep(_interval):
            if not seq:
                raise asyncio.CancelledError
            conn, sub = seq.pop(0)
            m._connection = conn
            m._subscribed_topics_set = {_RECOVERY_TOPIC} if sub else set()

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_missing = counted_sub  # type: ignore[assignment]
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
        self.assertFalse(m._subscribed_topics_set)  # set NOT committed (empty)
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

        m = NativeMqttClient(FakeHon([FakeAppliance(_RECOVERY_TOPIC)]), "MID")
        # Sustained not-connected long enough to trip the rebuild threshold; the rebuild's
        # _start() then brings the transport up, but the subscribe drops it again.
        seq = [(False, False)] * _RECONNECT_AFTER_FAILED_TICKS
        rebuilds = []
        resub_calls = []

        async def fake_start():
            rebuilds.append(True)
            m._subscribed_topics_set = set()
            # _start() built a fresh client; model the awscrt success callback bringing
            # the transport up before the subscribe runs.
            m._connection = True

        async def counted_sub():
            resub_calls.append(True)
            # The subscribe succeeds (topic added), but the awscrt disconnection callback
            # lands mid-await and flips _connection=False; the post-await guard must then
            # DISCARD the committed set.
            m._subscribed_topics_set = m._subscribed_topics_set | {_RECOVERY_TOPIC}
            m._connection = False

        async def fake_sleep(_interval):
            if not seq:
                raise asyncio.CancelledError
            conn, sub = seq.pop(0)
            m._connection = conn
            m._subscribed_topics_set = {_RECOVERY_TOPIC} if sub else set()

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_missing = counted_sub  # type: ignore[assignment]
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
        self.assertFalse(m._subscribed_topics_set)  # set NOT committed (empty)
        self.assertFalse(m._connection)      # the mid-subscribe disconnect stuck


class SubscribedFlagLifecycleTest(unittest.TestCase):
    """The companion of WatchdogSubscribedRecoveryTest at the state level: the subscribed
    SET must be cleared (rebound to empty) by the disconnection/failure callbacks
    (respecting the generation guard) and by _start(), and populated only after the
    subscribe succeeds."""

    def test_disconnection_resets_subscribed(self) -> None:
        m = NativeMqttClient(FakeHon([]), "MID")
        m._generation = 1
        m._connection = True
        m._subscribed_topics_set = {"t"}
        m._on_lifecycle_disconnection(None, generation=1)
        self.assertFalse(m._connection)
        self.assertEqual(m._subscribed_topics_set, set())

    def test_connection_failure_resets_subscribed(self) -> None:
        m = NativeMqttClient(FakeHon([]), "MID")
        m._generation = 1
        m._connection = True
        m._subscribed_topics_set = {"t"}
        m._on_lifecycle_connection_failure(None, generation=1)
        self.assertFalse(m._connection)
        self.assertEqual(m._subscribed_topics_set, set())

    def test_connection_success_does_not_set_subscribed(self) -> None:
        # The success callback only flips _connection: it does NOT subscribe, so the set
        # must stay empty (this is exactly the connected-but-unsubscribed window the
        # watchdog must recover, incl. awscrt's clean-session auto-reconnect).
        m = NativeMqttClient(FakeHon([]), "MID")
        m._generation = 1
        m._on_lifecycle_connection_success(None, generation=1)
        self.assertTrue(m._connection)
        self.assertEqual(m._subscribed_topics_set, set())

    def test_stale_disconnection_does_not_reset_subscribed(self) -> None:
        # A late event from an OLD generation must not clear the set on the current
        # healthy client (same guard that protects _connection).
        m = NativeMqttClient(FakeHon([]), "MID")
        m._generation = 2
        m._connection = True
        m._subscribed_topics_set = {"t"}
        m._on_lifecycle_disconnection(None, generation=1)  # stale
        self.assertTrue(m._connection)
        self.assertEqual(m._subscribed_topics_set, {"t"})

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
        m._subscribed_topics_set = {"t"}  # pretend a previous client was subscribed

        _run(m._start())
        self.assertEqual(m._subscribed_topics_set, set())  # fresh client -> none

    def test_start_resets_connection(self) -> None:
        # CR#1: _start() must also reset _connection (symmetric with the set): the
        # new client is not up until ITS connection-success callback fires. An escalation
        # rebuild (connected-but-unsubscribed) reaches _start() with _connection=True
        # carried over from the just-stopped client; leaving it True would make a rebuild
        # whose subscribe then fails look connected-but-unsubscribed on a client not
        # actually up yet, sending the next watchdog tick down the in-place re-subscribe
        # path against a dead client instead of rebuilding.
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
        m._connection = True  # pretend the just-stopped client was connected

        _run(m._start())
        self.assertFalse(m._connection)  # fresh client -> not up until its success cb

    def test_create_sets_subscribed_true_on_success(self) -> None:
        m = NativeMqttClient(FakeHon([FakeAppliance("t")]), "MID")

        async def fake_start():
            m._client = object()
            # The real _start() builds the client; the awscrt connection-success
            # callback then flips _connection=True before subscribe completes. create()'s
            # lost-update guard (Issue 3) only commits the set when _connection is up,
            # so model the connected transport here.
            m._connection = True

        async def ok_sub():
            # Model _subscribe_missing(): the topic subscribes -> goes into the set.
            m._subscribed_topics_set = {"t"}

        async def ok_watchdog():
            return None

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_missing = ok_sub  # type: ignore[assignment]
        m._start_watchdog = ok_watchdog  # type: ignore[assignment]
        _run(m.create())
        self.assertEqual(m._subscribed_topics_set, {"t"})  # fully subscribed

    def test_create_does_not_clobber_subscribed_when_disconnect_lands_mid_subscribe(
        self,
    ) -> None:
        # Issue 3 at the create() site: if a disconnection callback lands DURING the
        # initial subscribe (flips _connection=False), create() must DISCARD the set
        # (not commit a "healthy on a dropped session").
        m = NativeMqttClient(FakeHon([FakeAppliance("t")]), "MID")

        async def fake_start():
            m._client = object()
            m._connection = True

        async def dropping_sub():
            # The subscribe adds the topic, but the awscrt thread fires a disconnection
            # mid-subscribe; create()'s post-await guard must then DISCARD the set.
            m._subscribed_topics_set = {"t"}
            m._connection = False

        async def ok_watchdog():
            return None

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_missing = dropping_sub  # type: ignore[assignment]
        m._start_watchdog = ok_watchdog  # type: ignore[assignment]
        _run(m.create())
        self.assertFalse(m._subscribed_topics_set)  # set discarded, not committed


class MultiTopicAppliance:
    """An appliance that subscribes SEVERAL topics, to exercise the per-topic isolation
    of _subscribe_missing (the binary _subscribed flag could not represent a partial
    success)."""

    def __init__(self, topics) -> None:
        self.info = {"topics": {"subscribe": list(topics)}}


class _PerTopicFakeClient:
    """A fake awscrt mqtt5 client whose subscribe() resolves or stalls PER TOPIC.

    `bad_topics` never SUBACK (their Future never resolves -> _SUBSCRIBE_TIMEOUT fires);
    every other topic resolves immediately. Requires the test to set
    awscrt.mqtt5.SubscribePacket/Subscription so the topic is recoverable from the
    packet the production code builds."""

    def __init__(self, bad_topics) -> None:
        self.bad_topics = set(bad_topics)
        self.subscribed: list = []

    def subscribe(self, packet):
        # SubscribePacket is stubbed to ("pkt", [topic]); pull the single topic out.
        topic = packet[1][0]
        self.subscribed.append(topic)
        fut: concurrent.futures.Future = concurrent.futures.Future()
        if topic not in self.bad_topics:
            fut.set_result(None)
        # else: leave it unresolved so asyncio.wait_for raises TimeoutError.
        return fut


class _RaisingTopicFakeClient:
    """A fake awscrt mqtt5 client whose subscribe() future RAISES a NON-timeout exception
    for `raise_topics` (modelling an awscrt/transport error surfaced on the future), to
    prove _subscribe_missing isolates more than just the HonCodedError timeout."""

    def __init__(self, raise_topics) -> None:
        self.raise_topics = set(raise_topics)
        self.subscribed: list = []

    def subscribe(self, packet):
        topic = packet[1][0]
        self.subscribed.append(topic)
        fut: concurrent.futures.Future = concurrent.futures.Future()
        if topic in self.raise_topics:
            fut.set_exception(RuntimeError("awscrt subscribe failure"))
        else:
            fut.set_result(None)
        return fut


class SubscribeMissingIsolationTest(unittest.TestCase):
    """H1 direct guard: _subscribe_missing must subscribe every topic it CAN, isolating a
    single failing topic instead of aborting the whole pass (the old sequential
    _subscribe raised on the first timeout and starved every appliance after it)."""

    def setUp(self) -> None:
        import awscrt

        awscrt.mqtt5.SubscribePacket = lambda subs: ("pkt", subs)
        awscrt.mqtt5.Subscription = lambda topic: topic
        # Keep the per-topic timeout tiny so the stalled topic fails fast.
        import custom_components.addhon.client.transport.mqtt as mod

        self._mod = mod
        self._orig_timeout = mod._SUBSCRIBE_TIMEOUT
        mod._SUBSCRIBE_TIMEOUT = 0.01

    def tearDown(self) -> None:
        self._mod._SUBSCRIBE_TIMEOUT = self._orig_timeout

    def test_one_bad_topic_does_not_block_the_others(self) -> None:
        topics = ["t/good1", "t/bad", "t/good2", "t/good3"]
        app = MultiTopicAppliance(topics)
        m = NativeMqttClient(FakeHon([app]), "MID")
        m._client = _PerTopicFakeClient(bad_topics={"t/bad"})

        _run(m._subscribe_missing())

        # Every topic EXCEPT the broken one ends up subscribed (H1: the bad topic does
        # not starve the rest).
        self.assertEqual(m._subscribed_topics_set, {"t/good1", "t/good2", "t/good3"})
        self.assertNotIn("t/bad", m._subscribed_topics_set)
        # All four were attempted (the failing one did not short-circuit the loop).
        self.assertEqual(set(m._client.subscribed), set(topics))

    def test_already_subscribed_topic_is_skipped(self) -> None:
        # _subscribe_missing only attempts the topics NOT already in the set: an
        # already-subscribed topic is not re-issued (idempotent retries).
        topics = ["t/a", "t/b"]
        app = MultiTopicAppliance(topics)
        m = NativeMqttClient(FakeHon([app]), "MID")
        m._client = _PerTopicFakeClient(bad_topics=set())
        m._subscribed_topics_set = {"t/a"}  # already subscribed

        _run(m._subscribe_missing())

        self.assertEqual(m._subscribed_topics_set, {"t/a", "t/b"})
        self.assertEqual(m._client.subscribed, ["t/b"])  # only the missing one issued

    def test_recovered_topic_empties_missing_on_a_later_pass(self) -> None:
        # First pass: the bad topic stalls -> stays missing. Second pass with the topic
        # now healthy: the remaining miss is filled -> fully subscribed.
        topics = ["t/good", "t/flaky"]
        app = MultiTopicAppliance(topics)
        m = NativeMqttClient(FakeHon([app]), "MID")

        m._client = _PerTopicFakeClient(bad_topics={"t/flaky"})
        _run(m._subscribe_missing())
        self.assertEqual(m._subscribed_topics_set, {"t/good"})  # flaky still missing

        # The flaky topic recovers; the next pass only needs to fill the miss.
        m._client = _PerTopicFakeClient(bad_topics=set())
        _run(m._subscribe_missing())
        self.assertEqual(m._subscribed_topics_set, {"t/good", "t/flaky"})  # complete
        self.assertEqual(m._client.subscribed, ["t/flaky"])  # only the miss re-issued

    def test_one_bad_topic_logs_warning_and_continues(self) -> None:
        app = MultiTopicAppliance(["t/good", "t/bad"])
        m = NativeMqttClient(FakeHon([app]), "MID")
        m._client = _PerTopicFakeClient(bad_topics={"t/bad"})
        name = "custom_components.addhon.client.transport.mqtt"
        with self.assertLogs(name, level="WARNING") as cm:
            _run(m._subscribe_missing())
        blob = "\n".join(cm.output)
        self.assertIn("subscribe failed for one topic, continuing", blob)
        self.assertEqual(m._subscribed_topics_set, {"t/good"})

    def test_non_timeout_exception_is_isolated(self) -> None:
        # A subscribe failure that is NOT the HonCodedError timeout (e.g. an awscrt
        # transport error surfaced on the future) must ALSO be isolated, not abort the
        # pass: otherwise H1 starvation reopens for any non-timeout failure. With the
        # old `except HonCodedError`-only handler this RuntimeError escaped and starved
        # t/good2.
        topics = ["t/good1", "t/raises", "t/good2"]
        app = MultiTopicAppliance(topics)
        m = NativeMqttClient(FakeHon([app]), "MID")
        m._client = _RaisingTopicFakeClient(raise_topics={"t/raises"})
        name = "custom_components.addhon.client.transport.mqtt"
        with self.assertLogs(name, level="WARNING") as cm:
            _run(m._subscribe_missing())  # must NOT raise out of the loop
        self.assertIn(
            "subscribe failed for one topic, continuing", "\n".join(cm.output)
        )
        # The other topics still got subscribed; all three were attempted.
        self.assertEqual(m._subscribed_topics_set, {"t/good1", "t/good2"})
        self.assertEqual(set(m._client.subscribed), set(topics))

    def test_cancelled_error_propagates(self) -> None:
        # A CancelledError (stop() cancels the watchdog) must NOT be swallowed as a
        # per-topic failure: it has to propagate so shutdown is not deadlocked.
        app = MultiTopicAppliance(["t/x"])
        m = NativeMqttClient(FakeHon([app]), "MID")

        async def _cancel(_topic):
            raise asyncio.CancelledError

        m._subscribe_topic = _cancel  # type: ignore[assignment]
        with self.assertRaises(asyncio.CancelledError):
            _run(m._subscribe_missing())

        # Forward-protection: today CancelledError propagates because it is
        # BaseException-derived and `except Exception` cannot catch it, so the behavioral
        # check above passes with OR without the explicit re-raise. The REAL risk is a
        # future refactor to `except BaseException`, which WOULD swallow the cancellation
        # and deadlock stop(). Pin the isolation handler against that: it must catch
        # `Exception` (not `BaseException`) and keep the explicit CancelledError re-raise.
        import inspect

        src = inspect.getsource(NativeMqttClient._subscribe_missing)
        self.assertIn("except asyncio.CancelledError", src)
        self.assertNotIn("except BaseException", src)


class WatchdogPartialMissEscalationTest(unittest.TestCase):
    """H1 watchdog discrimination: ONE chronically broken appliance topic (transport
    alive, other topics subscribed) is appliance-specific and must NOT escalate to a full
    _start() rebuild (which would drop the healthy subscriptions). A TOTAL blackout
    (nothing subscribes) IS a dead connection and DOES escalate after the cap."""

    def _drive(self, *, all_topics, healthy_topics, ticks, connected=True):
        """Drive the REAL _watchdog. _subscribe_missing is replaced with a stub that adds
        only `healthy_topics` to the set (the rest stay chronically missing). The
        instance starts connected-but-unsubscribed. Returns (resub_calls, rebuilds, m).
        """
        import custom_components.addhon.client.transport.mqtt as mod

        appliance = MultiTopicAppliance(all_topics)
        m = NativeMqttClient(FakeHon([appliance]), "MID")
        m._connection = connected
        seq_ticks = {"left": ticks}
        rebuilds = []
        resub_calls = []

        async def fake_start():
            rebuilds.append(True)
            m._subscribed_topics_set = set()
            # Model the awscrt success callback bringing the (new) transport up.
            m._connection = True

        async def fake_subscribe_missing():
            resub_calls.append(True)
            # Per-topic isolation: only the healthy topics make it into the set; the
            # broken ones stay missing pass after pass.
            m._subscribed_topics_set = m._subscribed_topics_set | set(healthy_topics)

        async def fake_sleep(_interval):
            if seq_ticks["left"] <= 0:
                raise asyncio.CancelledError
            seq_ticks["left"] -= 1
            # The state at the start of each tick is connected (the awscrt callback keeps
            # reporting up) but with the broken topic missing.
            m._connection = True

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_missing = fake_subscribe_missing  # type: ignore[assignment]
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

    def test_chronic_bad_topic_does_not_escalate_to_rebuild(self) -> None:
        from custom_components.addhon.client.transport.mqtt import (
            _MAX_RESUBSCRIBE_FAILURES,
        )

        # 3 topics; one is chronically broken. Run many ticks (well past the blackout
        # cap) -- the watchdog must keep re-subscribing the missing topic and NEVER
        # escalate, because some topics ARE subscribed (the connection is alive).
        resub, rebuilds, m = self._drive(
            all_topics=["t/good1", "t/good2", "t/bad"],
            healthy_topics=["t/good1", "t/good2"],
            ticks=_MAX_RESUBSCRIBE_FAILURES + 5,
        )
        self.assertEqual(len(rebuilds), 0)  # never escalated: healthy subs preserved
        self.assertGreaterEqual(len(resub), 1)  # kept retrying the missing topic
        # The healthy subscriptions survived every tick (never dropped by a rebuild).
        self.assertEqual(m._subscribed_topics_set, {"t/good1", "t/good2"})

    def test_total_blackout_escalates_after_cap(self) -> None:
        from custom_components.addhon.client.transport.mqtt import (
            _MAX_RESUBSCRIBE_FAILURES,
        )

        # NOTHING ever subscribes (healthy_topics empty) = dead connection: after
        # _MAX_RESUBSCRIBE_FAILURES blackout ticks the watchdog DOES escalate to a full
        # rebuild (today's behavior preserved). Give it enough ticks for the escalation.
        resub, rebuilds, _ = self._drive(
            all_topics=["t/a", "t/b"],
            healthy_topics=[],
            ticks=_MAX_RESUBSCRIBE_FAILURES + 2,
        )
        self.assertGreaterEqual(len(rebuilds), 1)  # blackout escalated to a rebuild
        # The blackout re-subscribe was attempted up to the cap before escalating.
        self.assertGreaterEqual(len(resub), _MAX_RESUBSCRIBE_FAILURES)


class WatchdogDisconnectMidSubscribeClearsSetTest(unittest.TestCase):
    """A disconnect landing mid-subscribe must clear the set (no 'healthy on a dropped
    session'), driven through the REAL lifecycle callback rather than a hand-set flag."""

    def test_disconnect_callback_mid_subscribe_clears_committed_set(self) -> None:
        import custom_components.addhon.client.transport.mqtt as mod

        app = MultiTopicAppliance(["t/x"])
        m = NativeMqttClient(FakeHon([app]), "MID")
        m._generation = 1
        m._connection = True
        seq = [True]  # one tick: connected, topic missing
        rebuilds = []

        async def fake_start():
            rebuilds.append(True)
            m._subscribed_topics_set = set()
            m._connection = True

        async def subscribe_then_disconnect():
            # A REAL awscrt disconnection callback fires mid-await on the current
            # generation (clears _connection and rebinds the set to empty), then a LATE
            # SUBACK lands and re-adds the topic AFTER the rebind. The set is now non-empty
            # on a DROPPED session, so the ONLY thing that can discard it is the watchdog's
            # post-await guard (the _connection/generation re-check). If that guard is
            # removed, the stale topic survives -> "healthy on a dropped session". This
            # ordering makes the assertion genuinely depend on the guard (the callback
            # alone leaving the set empty would pass vacuously).
            m._on_lifecycle_disconnection(None, generation=1)
            m._subscribed_topics_set.add("t/x")

        async def fake_sleep(_interval):
            if not seq:
                raise asyncio.CancelledError
            seq.pop(0)
            m._connection = True
            m._subscribed_topics_set = set()

        m._start = fake_start  # type: ignore[assignment]
        m._subscribe_missing = subscribe_then_disconnect  # type: ignore[assignment]
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

        self.assertFalse(m._connection)             # the disconnect stuck
        self.assertEqual(m._subscribed_topics_set, set())  # set cleared, not trusted
        self.assertEqual(len(rebuilds), 0)          # one tick: no rebuild yet


if __name__ == "__main__":
    unittest.main()
