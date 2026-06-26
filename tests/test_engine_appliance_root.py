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


class RealtimeReconciliationTest(unittest.TestCase):
    """Realtime MQTT traffic is authoritative connectivity evidence (the hOn app trusts
    it): a realtime message marks the appliance available, and the 60s REST poll must NOT
    clobber that back offline with a STALE `lastConnEvent.category=DISCONNECTED` (older
    than the realtime traffic). A genuinely NEWER disconnect DOES set it offline
    (self-correcting). Mirrors the user's washer: disconnect@13:35 < traffic@16:04.
    """

    # Cloud-stamped times from the real diagnostics dump (washer WM): the REST
    # lastConnEvent disconnect is ~2.5h OLDER than the realtime stream.
    _DISCONNECT_ISO = "2026-06-25T13:35:13Z"
    _DISCONNECT_MS = 1782394513133  # same instant, epoch ms (timestampEvent)
    _REALTIME_ISO = "2026-06-25T16:04:21.1Z"  # newer; note the single fractional digit

    def _build(self, payloads, type_name="WM"):
        class _SeqApi(FakeApi):
            def __init__(self, seq) -> None:
                self._payloads = list(seq)
                self._i = 0

            async def load_attributes(self, a):
                payload = self._payloads[min(self._i, len(self._payloads) - 1)]
                self._i += 1
                return payload

        info = dict(_INFO)
        info["applianceTypeName"] = type_name
        app = NaRoot(_SeqApi(payloads), json.loads(json.dumps(info)), zone=0)
        _run(app.load_commands())
        return app

    @staticmethod
    def _lce(category, *, ts_ms=None, iso=None):
        lce = {"category": category}
        if ts_ms is not None:
            lce["timestampEvent"] = ts_ms
        if iso is not None:
            lce["instantTime"] = iso
        return {"shadow": {"parameters": {}}, "lastConnEvent": lce}

    def test_realtime_message_marks_available(self) -> None:
        # (a) A realtime appliancestatus marks a previously-disconnected appliance
        # available (the engine entry point the MQTT transport calls).
        app = self._build([self._lce("DISCONNECTED", ts_ms=self._DISCONNECT_MS)])
        _run(app.load_attributes())
        self.assertFalse(app.connection)  # stale REST disconnect, no realtime yet
        app.mark_realtime_seen(self._REALTIME_ISO)
        self.assertTrue(app.connection)

    def test_realtime_marks_available_attr_immediately(self) -> None:
        # Regression guard: the connectivity binary_sensor (attr_key="available") and the
        # availability gate read the RAW `available` attribute, NOT `connection` directly,
        # and `attributes` returns the cached dict without recomputing. So the realtime
        # mark must flip `available` AT ONCE, not only at the next 60s poll.
        app = self._build([self._lce("DISCONNECTED", ts_ms=self._DISCONNECT_MS)])
        _run(app.load_attributes())
        self.assertFalse(app.attributes["available"])
        app.mark_realtime_seen(self._REALTIME_ISO)
        self.assertTrue(app.attributes["available"])  # instant, no poll in between

    def test_stale_disconnect_does_not_clobber_realtime(self) -> None:
        # (b) THE bug: realtime says online (16:04); a subsequent REST poll whose
        # lastConnEvent is a STALE DISCONNECTED (13:35) must NOT force it offline.
        app = self._build([self._lce("DISCONNECTED", ts_ms=self._DISCONNECT_MS)])
        app.mark_realtime_seen(self._REALTIME_ISO)
        self.assertTrue(app.connection)
        _run(app.load_attributes())  # stale disconnect@13:35 < traffic@16:04
        self.assertTrue(app.connection)  # stays online
        self.assertTrue(app.attributes["available"])

    def test_newer_disconnect_does_set_offline(self) -> None:
        # (c) A genuinely NEWER disconnect (after the last realtime traffic) DOES win ->
        # offline. Self-correcting: a real later disconnect is honored.
        # Pick a disconnect strictly AFTER the realtime instant.
        newer_iso = "2026-06-25T16:10:00Z"
        app = self._build([self._lce("DISCONNECTED", iso=newer_iso)])
        app.mark_realtime_seen(self._REALTIME_ISO)  # 16:04
        self.assertTrue(app.connection)
        _run(app.load_attributes())  # disconnect@16:10 > traffic@16:04 -> offline
        self.assertFalse(app.connection)
        self.assertFalse(app.attributes["available"])

    def test_connected_event_still_online_regardless_of_realtime(self) -> None:
        # A non-DISCONNECTED REST event is itself positive evidence -> online (unchanged).
        app = self._build([self._lce("CONNECTED", ts_ms=self._DISCONNECT_MS)])
        _run(app.load_attributes())
        self.assertTrue(app.connection)

    def test_missing_disconnect_timestamp_falls_back_to_rest(self) -> None:
        # Defensive fallback: if the disconnect carries NO usable timestamp we cannot
        # order it against the realtime traffic, so honor the DISCONNECTED (prior
        # REST-only behavior) rather than trusting possibly-stale realtime.
        app = self._build([self._lce("DISCONNECTED")])  # no timestampEvent/instantTime
        app.mark_realtime_seen(self._REALTIME_ISO)
        self.assertTrue(app.connection)
        _run(app.load_attributes())  # no ordering possible -> honor DISCONNECTED
        self.assertFalse(app.connection)

    def test_realtime_without_timestamp_still_marks_connected_but_no_protection(self) -> None:
        # A realtime message with a missing/garbage timestamp still marks connected (the
        # message IS the evidence) but does not record a liveness time, so a later REST
        # disconnect is honored (cannot assert a bogus ordering).
        app = self._build([self._lce("DISCONNECTED", ts_ms=self._DISCONNECT_MS)])
        app.mark_realtime_seen(None)  # no usable time
        self.assertTrue(app.connection)
        self.assertIsNone(app._last_realtime_ts)
        _run(app.load_attributes())
        self.assertFalse(app.connection)  # no protection without a timestamp

    def test_realtime_timestamp_iso_vs_epoch_ms_comparable(self) -> None:
        # The realtime payload time (ISO) and the lastConnEvent time (epoch ms) are the
        # SAME clock: a realtime ISO newer than an epoch-ms disconnect protects it.
        app = self._build([self._lce("DISCONNECTED", ts_ms=self._DISCONNECT_MS)])
        app.mark_realtime_seen(self._REALTIME_ISO)  # ISO, newer
        _run(app.load_attributes())  # epoch-ms disconnect, older
        self.assertTrue(app.connection)

    def test_realtime_ts_only_moves_forward(self) -> None:
        # An out-of-order OLDER realtime message must not rewind the recorded liveness.
        app = self._build([self._lce("DISCONNECTED", iso="2026-06-25T15:00:00Z")])
        app.mark_realtime_seen("2026-06-25T16:04:00Z")  # newer first
        app.mark_realtime_seen("2026-06-25T15:30:00Z")  # older, out of order
        self.assertEqual(
            app._last_realtime_ts.isoformat(), "2026-06-25T16:04:00+00:00"
        )
        _run(app.load_attributes())  # disconnect@15:00 < kept 16:04 -> online
        self.assertTrue(app.connection)

    def test_type_agnostic_ac_without_extra(self) -> None:
        # The fix is not WM-specific: an AC (no per-type layer) is protected too.
        app = self._build(
            [self._lce("DISCONNECTED", ts_ms=self._DISCONNECT_MS)], type_name="AC"
        )
        self.assertIsNone(app._extra)
        app.mark_realtime_seen(self._REALTIME_ISO)
        _run(app.load_attributes())
        self.assertTrue(app.connection)
        self.assertTrue(app.attributes["available"])

    def test_silently_dead_appliance_goes_offline_after_ttl(self) -> None:
        # Stuck-online guard: an appliance that streamed realtime then went SILENT must
        # NOT stay online forever while the cloud's lastConnEvent is frozen at an old
        # disconnect. Once the last realtime message ages past _REALTIME_LIVENESS_TTL, the
        # stale disconnect is honored -> offline (recovers a dead device within the window).
        from time import monotonic

        app = self._build(
            [self._lce("DISCONNECTED", ts_ms=self._DISCONNECT_MS)] * 2
        )
        app.mark_realtime_seen(self._REALTIME_ISO)  # cloud ts 16:04 (newer than 13:35)
        _run(app.load_attributes())
        self.assertTrue(app.connection)  # still fresh -> online
        # Age the MONOTONIC receipt time past the freshness window (cloud ts unchanged,
        # still newer than the disconnect): realtime is no longer recent enough to trust.
        app._last_realtime_local = monotonic() - (app._REALTIME_LIVENESS_TTL + 60)
        _run(app.load_attributes())
        self.assertFalse(app.connection)  # stale realtime -> defer to REST -> offline
        self.assertFalse(app.attributes["available"])

    def test_explicit_disconnect_clears_liveness_no_resurrection(self) -> None:
        # An explicit MQTT `disconnected` clears the realtime marks so a subsequent STALE
        # REST disconnect (older than the prior traffic) cannot resurrect the appliance.
        app = self._build([self._lce("DISCONNECTED", ts_ms=self._DISCONNECT_MS)])
        app.mark_realtime_seen(self._REALTIME_ISO)
        self.assertTrue(app.connection)
        app.mark_realtime_disconnected()
        self.assertFalse(app.connection)
        self.assertFalse(app.attributes["available"])
        self.assertIsNone(app._last_realtime_ts)
        self.assertIsNone(app._last_realtime_local)
        _run(app.load_attributes())  # stale disconnect@13:35, no liveness left
        self.assertFalse(app.connection)  # NOT resurrected


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
