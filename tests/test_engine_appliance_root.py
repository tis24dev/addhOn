"""Golden test of the native ROOT appliance (Phase 4). Freezes properties + the
end-to-end load on the real fridge dump. It used to be differential vs pyhOn
(slice 5a); with `_vendor/` deleted it is golden (native output proven == pyhOn
at checkpoint 5a, commit 520f036).
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import REPO, frozen, install_stubs, normalize  # noqa: E402

install_stubs()
_DUMP = REPO / "tests" / "fixtures" / "ref_10136"

from custom_components.addhon.client import factory  # noqa: E402

NaRoot = factory._native_engine_appliance_cls()


def _load(name: str):
    return json.loads((_DUMP / name).read_text(encoding="utf-8"))


class FakeApi:
    async def load_commands(self, a):
        return _load("commands.json")

    async def load_favourites(self, a):
        return []

    async def load_command_history(self, a):
        return _load("command_history.json")

    async def load_attributes(self, a):
        return _load("attributes.json")

    async def load_statistics(self, a):
        return _load("statistics.json")

    async def load_maintenance(self, a):
        return _load("maintenance.json")


_INFO = {
    "applianceTypeName": "REF", "applianceModelId": "10136",
    "macAddress": "11-22-33-44-55-66", "modelName": "HDPW5620CNPK", "brand": "haier",
    "nickName": "Frigo", "code": "ABC123", "serialNumber": "0123456789",
    "attributes": [{"parName": "a", "parValue": "1"}, {"parName": "b", "parValue": "2"}],
}

_PROPS = ["appliance_type", "appliance_model_id", "mac_address", "unique_id", "model_name",
          "brand", "nick_name", "code", "model_id", "zone", "connection"]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _snap_param(p):
    s = {"value": p.value, "intern_value": p.intern_value, "values": list(p.values)}
    if hasattr(p, "min"):
        s["min"], s["max"], s["step"] = p.min, p.max, p.step
    return s


def _native_snapshot():
    out = {"props": {}}
    for zone in (0, 1, 2):
        app = NaRoot(FakeApi(), json.loads(json.dumps(_INFO)), zone=zone)
        out["props"][str(zone)] = {p: getattr(app, p) for p in _PROPS}
    app0 = NaRoot(FakeApi(), json.loads(json.dumps(_INFO)), zone=0)
    out["info_attributes"] = app0.info["attributes"]
    # load end-to-end
    app = NaRoot(FakeApi(), json.loads(json.dumps(_INFO)), zone=0)
    _run(app.load_commands())
    _run(app.load_attributes())
    _run(app.load_statistics())
    out["commands"] = sorted(app.commands)
    out["available_settings"] = sorted(app.available_settings)
    out["options"] = app.options
    out["additional_data"] = sorted(app.additional_data)
    out["settings"] = {k: _snap_param(v) for k, v in sorted(app.settings.items())}
    out["statistics"] = app.statistics
    out["attr_param_keys"] = sorted(app.attributes.get("parameters", {}))
    out["programName"] = app.attributes.get("programName")
    out["available"] = app.attributes.get("available")
    out["command_parameters"] = app.command_parameters
    out["data_keys"] = sorted(app.data)
    return out


class _ConnApi(FakeApi):
    def __init__(self, category) -> None:
        self._cat = category

    async def load_attributes(self, a):
        return {"shadow": {"parameters": {}}, "lastConnEvent": {"category": self._cat}}


class ConnectivityTest(unittest.TestCase):
    """`connection`/`available` derived from lastConnEvent.category (app model), accurate
    on polling (before, `connection` was stale-True; validated live: offline TD now clears)."""

    def _build(self, category, type_name="REF"):
        info = dict(_INFO)
        info["applianceTypeName"] = type_name
        app = NaRoot(_ConnApi(category), json.loads(json.dumps(info)), zone=0)
        _run(app.load_commands())
        _run(app.load_attributes())
        return app

    def test_disconnected(self) -> None:
        a = self._build("DISCONNECTED")
        self.assertFalse(a.connection)
        self.assertFalse(a.attributes["available"])

    def test_connected(self) -> None:
        a = self._build("CONNECTED")
        self.assertTrue(a.connection)
        self.assertTrue(a.attributes["available"])

    def test_available_universal_without_extra(self) -> None:
        # a type without a per-type layer (e.g. AC) still gets `available` from the ROOT
        a = self._build("CONNECTED", type_name="AC")
        self.assertIsNone(a._extra)
        self.assertTrue(a.attributes["available"])

    def test_malformed_lastconnevent_no_crash(self) -> None:
        class _BadApi(FakeApi):
            async def load_attributes(self, a):
                return {"shadow": {"parameters": {}}, "lastConnEvent": "OOPS"}

        app = NaRoot(_BadApi(), json.loads(json.dumps(_INFO)), zone=0)
        _run(app.load_commands())
        _run(app.load_attributes())  # must not raise on a non-dict lastConnEvent
        self.assertTrue(app.connection)  # state unchanged (default)
        self.assertTrue(app.attributes["available"])

    def test_dict_without_category_keeps_state(self) -> None:
        # A dict lastConnEvent lacking `category` must NOT force availability True:
        # it keeps the prior (MQTT/previous-poll) state. Regression guard: the old code
        # did `lce.get("category", "") != "DISCONNECTED"` -> "" -> True on every poll.
        class _SeqApi(FakeApi):
            def __init__(self) -> None:
                self._payloads = [
                    {"shadow": {"parameters": {}}, "lastConnEvent": {"category": "DISCONNECTED"}},
                    {"shadow": {"parameters": {}}, "lastConnEvent": {"id": "x"}},  # dict, no category
                ]
                self._i = 0

            async def load_attributes(self, a):
                payload = self._payloads[min(self._i, len(self._payloads) - 1)]
                self._i += 1
                return payload

        app = NaRoot(_SeqApi(), json.loads(json.dumps(_INFO)), zone=0)
        _run(app.load_commands())
        _run(app.load_attributes())  # DISCONNECTED -> False
        self.assertFalse(app.connection)
        _run(app.load_attributes())  # dict without category -> keep False (was forced True)
        self.assertFalse(app.connection)
        self.assertFalse(app.attributes["available"])


class RootGoldenTest(unittest.TestCase):
    def test_native_root_matches_golden(self) -> None:
        snap = _native_snapshot()
        self.assertEqual(normalize(snap), frozen("engine_appliance_root", snap))

    def test_info_attributes_parsed(self) -> None:
        app = NaRoot(FakeApi(), json.loads(json.dumps(_INFO)), zone=0)
        self.assertEqual(app.info["attributes"], {"a": "1", "b": "2"})

    def test_root_module_is_native(self) -> None:
        self.assertEqual(NaRoot.__module__, "custom_components.addhon.client.engine.appliance")


if __name__ == "__main__":
    unittest.main()
