"""Offline test dell'orchestrazione `Hon` nativa (NativeHon, Fase 3 piece 3).

Verifica la SEQUENZA di setup fedele a pyhОn `Hon.setup` (load_appliances → per
appliance load_commands/attributes/statistics → MQTT per ultimo), la gestione zone,
lo skip su mac vuoto, la tolleranza agli errori per-appliance, il gating MQTT, la
chiusura e la conformità al Protocol `HonSession`.

Il motore pyhОn (HonAppliance) e il MQTT sono mockati via i factory di
`pyhon_adapter` (l'unico ponte verso `_vendor`): nessun import di `_vendor`,
nessuna rete, niente awscrt. aiohttp/yarl/homeassistant sono stubati.
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
    yarl = _mod("yarl")
    if not hasattr(yarl, "URL"):
        yarl.URL = type("URL", (), {"__init__": lambda self, s, encoded=False: None})
    aio = _mod("aiohttp")
    aio.ClientSession = getattr(aio, "ClientSession", type("ClientSession", (), {}))
    aio.ClientResponse = getattr(aio, "ClientResponse", type("ClientResponse", (), {}))
    aio.ContentTypeError = getattr(aio, "ContentTypeError", type("ContentTypeError", (Exception,), {}))


_install_stubs()

from custom_components.addhon.client import pyhon_adapter  # noqa: E402
from custom_components.addhon.client import session as session_mod  # noqa: E402
from custom_components.addhon.client.session import NativeHon  # noqa: E402
from custom_components.addhon.client.interfaces import HonSession  # noqa: E402
from custom_components.addhon.client.transport.auth import NativeAuthError  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class FakeAppliance:
    def __init__(self, api, data, zone, events, fail=False) -> None:
        self._api = api
        self._data = data
        self.zone = zone
        self.events = events
        self.fail = fail
        self.mac_address = data.get("macAddress", "")
        self.appliance_type = data.get("applianceTypeName", "?")

    async def load_commands(self) -> None:
        self.events.append(f"cmd:{self.mac_address}:{self.zone}")
        if self.fail:
            raise KeyError("boom")

    async def load_attributes(self) -> None:
        self.events.append(f"attr:{self.mac_address}:{self.zone}")

    async def load_statistics(self) -> None:
        self.events.append(f"stat:{self.mac_address}:{self.zone}")


class FakeApi:
    def __init__(self, appliances, events) -> None:
        self._appliances = appliances
        self.events = events
        self.closed = False

    async def load_appliances(self):
        self.events.append("load_appliances")
        return [dict(a) for a in self._appliances]

    async def close(self):
        self.closed = True


class _Harness:
    """Patcha i factory di pyhon_adapter + HonConnection/HonApi di session."""

    def __init__(self, test, appliances, fail_macs=()):
        self.test = test
        self.events: list = []
        self.appliances_data = appliances
        self.fail_macs = set(fail_macs)
        self.api = FakeApi(appliances, self.events)
        self.mqtt_calls: list = []
        self.stop_calls: list = []
        self.mqtt_sentinel = object()

    def install(self):
        t = self.test
        events = self.events

        def fake_create_appliance(api, data, zone=0):
            return FakeAppliance(api, data, zone, events, fail=data.get("macAddress") in self.fail_macs)

        async def fake_create_mqtt(hon, mobile_id):
            events.append("mqtt")
            self.mqtt_calls.append((hon, mobile_id))
            return self.mqtt_sentinel

        async def fake_stop_mqtt(mqtt_client):
            self.stop_calls.append(mqtt_client)

        t._patch(pyhon_adapter, "create_appliance", fake_create_appliance)
        t._patch(pyhon_adapter, "create_mqtt", fake_create_mqtt)
        t._patch(pyhon_adapter, "stop_mqtt", fake_stop_mqtt)


class NativeSessionSetupTest(unittest.TestCase):
    def _patch(self, obj, name, value):
        real = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, real))

    def _nh_with_api(self, harness, **kw):
        nh = NativeHon("u@x", "p", **kw)
        nh._api = harness.api  # bypassa la creazione connessione, testa setup()
        return nh

    def test_setup_loads_each_appliance_then_mqtt_last(self) -> None:
        data = [
            {"macAddress": "A", "applianceTypeName": "REF"},
            {"macAddress": "B", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        # 2 appliance costruite + caricate, mqtt per ULTIMO
        self.assertEqual([a.mac_address for a in nh.appliances], ["A", "B"])
        self.assertEqual(h.events[0], "load_appliances")
        self.assertEqual(h.events[-1], "mqtt")
        # per ogni appliance: cmd -> attr -> stat, e tutte PRIMA di mqtt
        self.assertEqual(
            h.events,
            ["load_appliances",
             "cmd:A:0", "attr:A:0", "stat:A:0",
             "cmd:B:0", "attr:B:0", "stat:B:0",
             "mqtt"],
        )

    def test_zone_appliance_split(self) -> None:
        data = [{"macAddress": "Z", "applianceTypeName": "AC", "zone": "2"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        # zones=2 -> zone1, zone2, poi base(zone0) = 3 appliance (come pyhОn)
        self.assertEqual([a.zone for a in nh.appliances], [1, 2, 0])

    def test_zero_appliances_still_creates_mqtt(self) -> None:
        # 0 appliance: load_appliances (=1 POST autenticato che popola i token) avviene
        # comunque, poi MQTT parte lo stesso -> auth pronto anche senza appliance.
        h = _Harness(self, [])
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual(nh.appliances, [])
        self.assertEqual(h.events, ["load_appliances", "mqtt"])
        self.assertEqual(len(h.mqtt_calls), 1)

    def test_mqtt_created_once_across_two_setups(self) -> None:
        # il gate `not self._mqtt_client` impedisce una seconda creazione MQTT.
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        _run(nh.setup())
        self.assertEqual(h.events.count("mqtt"), 1)
        self.assertEqual(len(h.mqtt_calls), 1)

    def test_mixed_zoned_and_normal_ordering(self) -> None:
        # un appliance multi-zona seguito da uno normale: zone1,zone2,base(0) poi normale(0),
        # tutti caricati PRIMA di mqtt (per ultimo).
        data = [
            {"macAddress": "Z", "applianceTypeName": "AC", "zone": "2"},
            {"macAddress": "N", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual(
            [(a.mac_address, a.zone) for a in nh.appliances],
            [("Z", 1), ("Z", 2), ("Z", 0), ("N", 0)],
        )
        self.assertEqual(h.events[0], "load_appliances")
        self.assertEqual(h.events[-1], "mqtt")
        # nessun load dopo mqtt
        self.assertEqual(h.events.index("mqtt"), len(h.events) - 1)

    def test_zone_one_not_split(self) -> None:
        data = [{"macAddress": "Z", "applianceTypeName": "AC", "zone": "1"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual([a.zone for a in nh.appliances], [0])

    def test_empty_mac_skipped(self) -> None:
        data = [
            {"macAddress": "", "applianceTypeName": "GHOST"},
            {"macAddress": "B", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual([a.mac_address for a in nh.appliances], ["B"])
        # l'appliance senza mac non viene caricata
        self.assertNotIn("cmd::0", h.events)

    def test_appliance_load_error_still_appended(self) -> None:
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data, fail_macs={"A"})
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        # load_commands lancia KeyError ma l'appliance resta (stato parziale, come pyhОn)
        self.assertEqual([a.mac_address for a in nh.appliances], ["A"])

    def test_mqtt_disabled(self) -> None:
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h, enable_mqtt=False)
        _run(nh.setup())
        self.assertNotIn("mqtt", h.events)
        self.assertEqual(h.mqtt_calls, [])
        self.assertIsNone(nh._mqtt_client)

    def test_create_builds_connection_api_then_setup(self) -> None:
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)
        h.install()

        created = {}

        class FakeConn:
            def __init__(self, *a, **kw):
                created["args"] = (a, kw)

            async def create(self):
                created["conn_created"] = True
                return self

            async def close(self):
                created["conn_closed"] = True

        def fake_honapi(conn):
            created["api_conn"] = conn
            return h.api

        self._patch(session_mod, "HonConnection", FakeConn)
        self._patch(session_mod, "HonApi", fake_honapi)

        nh = NativeHon("u@x", "p", mobile_id="MID", enable_mqtt=False)
        out = _run(nh.create())
        self.assertIs(out, nh)
        self.assertTrue(created["conn_created"])
        self.assertIsInstance(created["api_conn"], FakeConn)
        self.assertEqual([a.mac_address for a in nh.appliances], ["A"])

    def test_close_closes_api(self) -> None:
        h = _Harness(self, [])
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.close())
        self.assertTrue(h.api.closed)

    def test_close_stops_mqtt_then_api(self) -> None:
        # close() ferma il MQTT (no leak) e poi chiude l'api; _mqtt_client azzerato.
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertIs(nh._mqtt_client, h.mqtt_sentinel)
        _run(nh.close())
        self.assertEqual(h.stop_calls, [h.mqtt_sentinel])
        self.assertIsNone(nh._mqtt_client)
        self.assertTrue(h.api.closed)

    def test_close_without_mqtt_no_stop(self) -> None:
        # enable_mqtt=False: niente mqtt -> close() non chiama stop_mqtt.
        h = _Harness(self, [])
        h.install()
        nh = self._nh_with_api(h, enable_mqtt=False)
        _run(nh.setup())
        _run(nh.close())
        self.assertEqual(h.stop_calls, [])
        self.assertTrue(h.api.closed)

    def test_real_stop_mqtt_cancels_watchdog_and_stops_client(self) -> None:
        # Esercita la stop_mqtt REALE (non mockata): cattura un eventuale rename
        # degli attributi del vendor (_watchdog_task/_client) che la renderebbe no-op.
        class FakeClient:
            def __init__(self) -> None:
                self.stopped = False

            def stop(self) -> None:
                self.stopped = True

        async def body():
            async def _forever():
                while True:
                    await asyncio.sleep(3600)

            task = asyncio.ensure_future(_forever())
            await asyncio.sleep(0)  # lascia partire il watchdog
            client = FakeClient()
            m = types.SimpleNamespace(_watchdog_task=task, _client=client)
            await pyhon_adapter.stop_mqtt(m)
            return task, client

        task, client = _run(body())
        self.assertTrue(task.cancelled())  # cancellato E awaitato
        self.assertTrue(client.stopped)

    def test_real_stop_mqtt_none_and_missing_attrs_no_crash(self) -> None:
        _run(pyhon_adapter.stop_mqtt(None))  # non deve sollevare
        _run(pyhon_adapter.stop_mqtt(types.SimpleNamespace()))  # senza _watchdog_task/_client

    def test_api_property_raises_before_create(self) -> None:
        nh = NativeHon("u@x", "p")
        with self.assertRaises(NativeAuthError):
            _ = nh.api

    def test_subscribe_and_notify(self) -> None:
        nh = NativeHon("u@x", "p")
        got = []
        nh.subscribe_updates(lambda payload: got.append(payload))
        nh.notify()
        self.assertEqual(got, [None])

    def test_notify_noop_without_subscriber(self) -> None:
        nh = NativeHon("u@x", "p")
        nh.notify()  # non deve sollevare

    def test_satisfies_hon_session_protocol(self) -> None:
        nh = NativeHon("u@x", "p")
        self.assertIsInstance(nh, HonSession)
        # i membri che il MQTTClient/integrazione leggono (via dir: la property
        # `api` solleva se valutata prima di create(), che è il comportamento giusto)
        for member in ("api", "appliances", "subscribe_updates", "notify", "close"):
            self.assertIn(member, dir(nh))
        # appliances è leggibile subito (lista vuota), api no (non creata)
        self.assertEqual(nh.appliances, [])


if __name__ == "__main__":
    unittest.main()
