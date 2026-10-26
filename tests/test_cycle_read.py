# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A push at the start or the end of a cycle asks for a read of the context (issue #130).

The activity that names the running programme comes only with the context, which the
coordinator reads once a minute; a push never carries it. Decisions of 2026-10-25:

- C1: the push that moves `machMode` into a cycle (2 or 5, the dishwasher's 4 too) from
  outside one, or out of one into the end 7 or selection 1, asks for the read; a resume
  from pause (3 -> 2) does not;
- C2: one appliance's context alone, on the session's loop, published like a push;
- C3: 3 s after the push, then once more 10 s later if the activity does not show the
  change yet. A live read on mik's air conditioner (2026-10-25) found the activity
  opened and closed with the shadow, readable 2 s after the change.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_transport_mqtt import FakeAppliance, FakeHon, _packet  # noqa: E402  (stubs first)

from custom_components.addhon.client import factory  # noqa: E402
from custom_components.addhon.client.transport import mqtt as mqtt_module  # noqa: E402
from custom_components.addhon.client.transport.mqtt import NativeMqttClient  # noqa: E402
from test_follow_started_elsewhere import CloudDouble, _programme  # noqa: E402

_TOPIC = "haier/things/MAC/event/appliancestatus/update"
_COMMANDS = {
    "applianceModel": {"options": {}},
    "startProgram": {"PROGRAMS.WM_WD.HQD_COTTONS": _programme("115")},
    "dictionaryId": 1,
}


def _context(mach_mode, activity=None):
    return {
        "shadow": {"parameters": {
            "prCode": {"parNewVal": "115", "lastUpdate": "2026-10-16T12:20:00Z"},
            "machMode": {"parNewVal": mach_mode, "lastUpdate": "2026-10-16T12:20:00Z"},
        }},
        "activity": activity or {},
        "commandHistory": None,
    }


def _open_activity():
    return {"activityStarted": "2026-10-16T12:25:03Z",
            "attributes": {"programName": "HQD_COTTONS", "channel": "appliance"}}


class CycleEdgeTest(unittest.TestCase):
    """C1, on the engine's appliance."""

    def _appliance(self, kind, mach_mode="1"):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        app = factory._native_engine_appliance_cls()(
            CloudDouble(_COMMANDS, [], _context(mach_mode)),
            {"applianceTypeName": kind, "applianceModelId": 1, "macAddress": "aa"},
            zone=0,
        )
        loop.run_until_complete(app.load_commands())
        loop.run_until_complete(app.load_attributes())
        return app

    def _edge(self, app, before, after):
        app.attributes["parameters"]["machMode"].update({"parNewVal": after})
        return app.cycle_edge(before)

    def test_the_washer_s_moves(self) -> None:
        cases = {
            (1, "2"): "start", (0, "2"): "start", (1, "5"): "start", (7, "2"): "start",
            (3, "2"): None,  # a resume
            (5, "2"): None,  # a delayed start that begins
            (2, "3"): None, (2, "2"): None,
            (2, "7"): "end", (3, "1"): "end", (5, "1"): "end",
            (7, "1"): None,  # already ended
            (1, "4"): None,  # 4 is the dishwasher's
        }
        for kind in ("WM", "WD", "TD"):
            app = self._appliance(kind)
            for (before, after), edge in cases.items():
                with self.subTest(kind=kind, before=before, after=after):
                    self.assertEqual(edge, self._edge(app, before, after))

    def test_the_dishwasher_s_moves(self) -> None:
        app = self._appliance("DW")
        cases = {(1, "4"): "start", (4, "2"): None, (3, "4"): None, (4, "7"): "end"}
        for (before, after), edge in cases.items():
            with self.subTest(before=before, after=after):
                self.assertEqual(edge, self._edge(app, before, after))

    def test_the_types_not_followed_ask_for_nothing(self) -> None:
        for kind in ("OV", "REF", "WH"):
            with self.subTest(kind=kind):
                self.assertIsNone(self._edge(self._appliance(kind), 1, "2"))

    def test_settled_reads_the_activity_last_read(self) -> None:
        app = self._appliance("WM")
        self.assertFalse(app.cycle_edge_settled("start"))
        self.assertTrue(app.cycle_edge_settled("end"))
        app.attributes["activity"] = _open_activity()
        self.assertTrue(app.cycle_edge_settled("start"))
        self.assertFalse(app.cycle_edge_settled("end"))
        app.attributes["activity"] = _open_activity() | {"activityEnded": "2026-10-16T13:30:00Z"}
        self.assertTrue(app.cycle_edge_settled("end"))


class _CycleAppliance(FakeAppliance):
    """A pushed appliance whose edge and read the test sets."""

    def __init__(self, edge="start", settled=(True,)) -> None:
        super().__init__(_TOPIC)
        self.mach_mode = 1
        self.edges = []
        self.edge = edge
        self.settled = list(settled)
        self.reads = 0
        self.read_error = None

    def cycle_edge(self, before):
        self.edges.append(before)
        return self.edge

    def cycle_edge_settled(self, edge):
        return self.settled.pop(0) if self.settled else True

    async def update(self, force=False):
        assert force
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error


@patch.object(mqtt_module, "_CYCLE_READ_DELAY", 0)
@patch.object(mqtt_module, "_CYCLE_READ_RETRY", 0)
class CycleReadTest(unittest.TestCase):
    """C2/C3, on the transport."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def _push(self, appliance):
        hon = FakeHon([appliance])
        client = NativeMqttClient(hon, "MID")
        client._loop = self.loop  # what create() records
        client._on_message(_packet(_TOPIC, {"parameters": [
            {"parName": "temp", "parValue": "5"}]}))
        return client, hon

    def _settle(self):
        self.loop.run_until_complete(asyncio.sleep(0.05))

    def test_an_edge_reads_the_context_and_publishes(self) -> None:
        app = _CycleAppliance()
        client, hon = self._push(app)
        self.assertEqual([1], app.edges)  # machMode before the push
        self.assertEqual(1, hon.notified)  # the push itself
        self._settle()
        self.assertEqual(1, app.reads)
        self.assertEqual(2, hon.notified)
        self.assertEqual({}, client._cycle_reads)

    def test_a_read_that_does_not_show_the_edge_is_tried_once_more(self) -> None:
        app = _CycleAppliance(settled=(False, False))
        _, hon = self._push(app)
        self._settle()
        self.assertEqual(2, app.reads)
        self.assertEqual(3, hon.notified)

    def test_no_edge_no_read(self) -> None:
        app = _CycleAppliance(edge=None)
        client, hon = self._push(app)
        self._settle()
        self.assertEqual(0, app.reads)
        self.assertEqual(1, hon.notified)
        self.assertEqual({}, client._cycle_reads)

    def test_a_failed_read_is_left_to_the_poll(self) -> None:
        app = _CycleAppliance(settled=(False,))
        app.read_error = TimeoutError()
        _, hon = self._push(app)
        self._settle()
        self.assertEqual(1, app.reads)
        self.assertEqual(1, hon.notified)

    def test_before_create_nothing_is_read(self) -> None:
        app = _CycleAppliance()
        hon = FakeHon([app])
        NativeMqttClient(hon, "MID")._on_message(
            _packet(_TOPIC, {"parameters": [{"parName": "temp", "parValue": "5"}]}))
        self._settle()
        self.assertEqual(0, app.reads)

    def test_a_newer_edge_replaces_a_read_still_pending(self) -> None:
        app = _CycleAppliance()
        client, _ = self._push(app)
        first = client._cycle_reads[id(app)]
        client._on_message(_packet(_TOPIC, {"parameters": []}))
        self._settle()
        self.assertTrue(first.cancelled())
        self.assertEqual(1, app.reads)

    def test_stop_cancels_a_pending_read(self) -> None:
        app = _CycleAppliance()
        client, _ = self._push(app)
        pending = client._cycle_reads[id(app)]
        self.loop.run_until_complete(client.stop())
        self._settle()
        self.assertTrue(pending.cancelled())
        self.assertEqual(0, app.reads)


@patch.object(mqtt_module, "_CYCLE_READ_DELAY", 0)
@patch.object(mqtt_module, "_CYCLE_READ_RETRY", 0)
class PanelStartEndToEndTest(unittest.TestCase):
    """The engine's appliance behind the transport: a panel start named in seconds."""

    def test_the_push_that_starts_the_cycle_names_it(self) -> None:
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        commands = {
            "applianceModel": {"options": {}},
            "startProgram": {
                "PROGRAMS.WM_WD.HQD_COTTONS": _programme("115"),
                "PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS": _programme("115"),
            },
            "dictionaryId": 1,
        }
        history = [{"command": {
            "commandName": "startProgram", "programName": "PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS",
            "timestamp": "2026-10-15T08:00:00.000Z", "parameters": {"prCode": "115"}}}]
        cloud = CloudDouble(commands, history, _context("1"))
        app = factory._native_engine_appliance_cls()(
            cloud,
            {"applianceTypeName": "WM", "applianceModelId": 1, "macAddress": "aa",
             "topics": {"subscribe": [_TOPIC]}},
            zone=0,
        )
        loop.run_until_complete(app.load_commands())
        loop.run_until_complete(app.load_attributes())
        self.assertEqual("iot_wash_denim_jeans", app.attributes["programName"])
        # Cottons is started on the panel: the cloud opens its activity with the shadow.
        cloud.context = _context("2", _open_activity())
        hon = FakeHon([app])
        client = NativeMqttClient(hon, "MID")
        client._loop = loop
        client._on_message(_packet(_TOPIC, {"parameters": [
            {"parName": "machMode", "parValue": "2", "parNewVal": "2"}]}))
        self.assertEqual("iot_wash_denim_jeans", app.attributes["programName"])  # the push
        loop.run_until_complete(asyncio.sleep(0.05))
        self.assertEqual("hqd_cottons", app.attributes["programName"])  # the read
        self.assertEqual(2, hon.notified)


if __name__ == "__main__":
    unittest.main()
