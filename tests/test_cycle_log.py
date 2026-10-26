# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The cycle log: what a washer did over its last cycles, for one dump (issue #138).

A dump is a photograph: the last value of each key, the last read a push asked for. The
beta3 protocol asks for three cycles and one dump, which must show over time what the
machine pushed -- the rows the 10 s shield of our own write refused too, since the echo
of a start's `stainType`/`antiCreaseStatus` arrives 3.5-5 s after it -- what each read of
the context returned, what was sent and how the cloud answered, and the reads at the
cycle's edges. The decisions of 2026-10-26: every raw push row kept and marked (R1), one
ring of about 2000 events, what it drops counted by kind (R2); always on for the types
whose cycles are followed, in memory, across a reload (D6).
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_running_cycle_name import _Harness, _activity  # noqa: E402  (stubs first)

from custom_components.addhon.client.engine import cycle_log  # noqa: E402
from custom_components.addhon.client.engine.attributes import HonAttribute  # noqa: E402
from custom_components.addhon.client.engine.cycle_log import (  # noqa: E402
    EVENT_LIMIT,
    TEXT_LIMIT,
    CycleLog,
    ShadowRow,
    context_summary,
)

_T0 = "2026-10-26T08:00:00Z"
_T1 = "2026-10-26T08:05:00Z"


class _Clock:
    """Home Assistant's clock, moved by the test."""

    def __init__(self) -> None:
        self.now = 1_793_000_000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float = 1.0) -> float:
        self.now += seconds
        return self.now


def _events(log, kind=None):
    return [e for e in log.snapshot()["events"] if kind is None or e["kind"] == kind]


def _rows(event):
    return event["rows"]


class _LogTest(unittest.TestCase):
    def setUp(self) -> None:
        cycle_log.forget_all()
        self.clock = _Clock()
        self.log = CycleLog(self.clock)


class ShadowRowsTest(_LogTest):
    """Which rows of a push, a read and a write of ours are kept, and how marked."""

    def test_every_push_row_is_kept_and_marked(self) -> None:
        self.log.shadow("mqtt", [
            ShadowRow("machMode", "1", "2", True),
            # The start's echo, inside the 10 s shield of our own write.
            ShadowRow("stainType", "19", "19", False),
            ShadowRow("temp", "40", "40", True),
            # The cloud's old value is not the one held: a push was missed.
            ShadowRow("prPhase", "0", "1", True, cloud_old="3"),
        ], cloud_at="2026-10-26T08:00:03.5Z", event_id=42)
        [event] = _events(self.log, "shadow")
        self.assertEqual("mqtt", event["road"])
        self.assertEqual("2026-10-26T08:00:03.5Z", event["cloud_at"])
        self.assertEqual("42", event["event_id"])
        self.assertEqual(
            [
                {"key": "machMode", "before": "1", "after": "2"},
                {"key": "stainType", "before": "19", "after": "19", "refused": True},
                {"key": "temp", "before": "40", "after": "40", "same": True},
                {"key": "prPhase", "before": "0", "after": "1", "cloud_from": "3"},
            ],
            _rows(event),
        )

    def test_a_held_number_is_logged_as_its_text_and_a_null_as_none(self) -> None:
        # `HonAttribute.__str__` raises on a value that is not a string.
        self.assertEqual("2", cycle_log.value_text(HonAttribute({"parNewVal": 2})))
        self.assertEqual("40", cycle_log.value_text(HonAttribute({"parNewVal": "40"})))
        self.assertIsNone(cycle_log.value_text(HonAttribute({"parNewVal": None})))
        self.assertEqual("x", cycle_log.value_text("x"))
        self.assertIsNone(cycle_log.value_text(None))

    def test_rest_rows_only_when_value_or_stamp_moved(self) -> None:
        self.log.shadow("rest", [
            ShadowRow("temp", "40", "40", True),
            ShadowRow("machMode", "1", "2", True, stamp=_T1),
            ShadowRow("prCode", "115", "115", True, stamp=_T1),  # only lastUpdate moved
            ShadowRow("stainType", "19", "0", False),
        ])
        [event] = _events(self.log, "shadow")
        self.assertEqual(
            [
                {"key": "machMode", "before": "1", "after": "2", "stamp": _T1},
                {"key": "prCode", "before": "115", "after": "115", "same": True, "stamp": _T1},
                {"key": "stainType", "before": "19", "after": "0", "refused": True},
            ],
            _rows(event),
        )
        self.log.shadow("rest", [ShadowRow("temp", "40", "40", True)])
        self.assertEqual(1, len(_events(self.log, "shadow")))

    def test_local_rows_only_when_changed(self) -> None:
        self.log.shadow("local", [
            ShadowRow("stainType", "0", "19", True),
            ShadowRow("temp", "40", "40", True),
        ])
        [event] = _events(self.log, "shadow")
        self.assertEqual("local", event["road"])
        self.assertEqual([{"key": "stainType", "before": "0", "after": "19"}], _rows(event))
        self.log.shadow("local", [ShadowRow("temp", "40", "40", True)])
        self.assertEqual(1, len(_events(self.log, "shadow")))

    def test_an_unseeded_key_is_listed(self) -> None:
        self.log.shadow("mqtt", [], unseeded=["newKey"])
        self.log.shadow("mqtt", [ShadowRow("temp", "40", "30", True)], unseeded=["other"])
        first, second = _events(self.log, "shadow")
        self.assertEqual(["newKey"], first["unseeded"])
        self.assertEqual([], first["rows"])
        self.assertEqual(["other"], second["unseeded"])
        self.assertEqual([{"key": "temp", "before": "40", "after": "30"}], _rows(second))

    def test_values_are_cut_and_mac_masked(self) -> None:
        # Masked first and only then cut: a MAC straddling the cut leaves no fragment.
        straddling = "x" * 55 + "AA:BB:CC:DD:EE:FF"
        self.log.shadow("mqtt", [
            ShadowRow("serialish", straddling, "y" * 200, True),
            ShadowRow("k" * 100, "1", "2", True),
        ])
        [event] = _events(self.log, "shadow")
        first, second = event["rows"]
        self.assertEqual("x" * 55 + "***", first["before"])
        self.assertEqual("y" * TEXT_LIMIT, first["after"])
        self.assertEqual("k" * TEXT_LIMIT, second["key"])
        self.assertNotIn("EE:FF", json.dumps(self.log.snapshot()))

    def test_rows_beyond_the_limit_are_cut_and_counted(self) -> None:
        rows = [ShadowRow(f"k{i}", "0", "1", True) for i in range(cycle_log.ROW_LIMIT + 5)]
        self.log.shadow("mqtt", rows)
        [event] = _events(self.log, "shadow")
        self.assertEqual(cycle_log.ROW_LIMIT, len(event["rows"]))
        self.assertEqual(5, event["truncated"])


class CounterRunTest(_LogTest):
    """Pushes that only step the countdown and consumption counters are one event."""

    def _push(self, *rows):
        self.clock.tick(60)
        self.log.shadow("mqtt", list(rows))

    def test_counter_steps_merge_into_one_run(self) -> None:
        for value in range(60, 55, -1):
            self._push(ShadowRow("remainingTimeMM", str(value), str(value - 1), True))
        [event] = _events(self.log, "shadow")
        self.assertEqual(5, event["steps"])
        self.assertEqual(self.clock.now, event["until"])
        self.assertEqual(self.clock.now - 240, event["at"])
        self.assertEqual([{"key": "remainingTimeMM", "before": "60", "after": "55"}], _rows(event))

    def test_consumption_and_cycle_time_step_together(self) -> None:
        self._push(ShadowRow("currentWaterUsed", "10", "12.5", True),
                   ShadowRow("currentCycleTime", "3", "4", True))
        self._push(ShadowRow("currentCycleTime", "4", "5", True),
                   ShadowRow("currentWaterUsed", "12.5", "13", True))
        [event] = _events(self.log, "shadow")
        self.assertEqual(2, event["steps"])
        self.assertEqual(
            [{"key": "currentWaterUsed", "before": "10", "after": "13"},
             {"key": "currentCycleTime", "before": "3", "after": "5"}],
            _rows(event),
        )

    def test_a_marked_row_breaks_the_run(self) -> None:
        self._push(ShadowRow("remainingTimeMM", "60", "59", True))
        self._push(ShadowRow("remainingTimeMM", "59", "58", True))
        # The same step with a refused row: an event of its own, and the run ends.
        self._push(ShadowRow("remainingTimeMM", "58", "57", True),
                   ShadowRow("stainType", "19", "19", False))
        self._push(ShadowRow("remainingTimeMM", "57", "56", True))
        # A jump is not a step; the step after it starts a new run.
        self._push(ShadowRow("remainingTimeMM", "56", "36", True))
        self._push(ShadowRow("remainingTimeMM", "36", "35", True))
        self._push(ShadowRow("remainingTimeMM", "35", "34", True))
        events = _events(self.log, "shadow")
        self.assertEqual([2, None, None, None, 2], [e.get("steps") for e in events])
        self.assertEqual("34", events[-1]["rows"][0]["after"])

    def test_any_other_event_between_breaks_the_run(self) -> None:
        self._push(ShadowRow("remainingTimeMM", "60", "59", True))
        self.log.command(name="pauseProgram", programme=None, sent=_T0, outcome="accepted")
        self._push(ShadowRow("remainingTimeMM", "59", "58", True))
        self.assertEqual(2, len(_events(self.log, "shadow")))


class RingTest(_LogTest):
    def test_the_ring_keeps_2000_and_counts_what_it_drops(self) -> None:
        for i in range(2100):
            self.clock.tick()
            if i % 2:
                self.log.command(name="settings", programme=None, sent=_T0, outcome="accepted")
            else:
                self.log.shadow("local", [ShadowRow("temp", str(i), str(i + 1), True)])
        snapshot = self.log.snapshot()
        self.assertEqual(EVENT_LIMIT, len(snapshot["events"]))
        self.assertEqual({"shadow": 50, "command": 50}, snapshot["dropped"])
        self.assertEqual(101, snapshot["events"][0]["seq"])
        self.assertEqual(2100, snapshot["events"][-1]["seq"])
        self.assertEqual(
            {"events": EVENT_LIMIT, "rows": cycle_log.ROW_LIMIT, "text": TEXT_LIMIT},
            snapshot["limits"],
        )
        json.dumps(snapshot)

    def test_concurrent_writers(self) -> None:
        errors: list[BaseException] = []
        log = CycleLog()

        def write(worker: int) -> None:
            try:
                for i in range(1000):
                    log.shadow("mqtt", [ShadowRow(f"w{worker}", str(i), str(i + 1), True)])
                    if i % 100 == 0:
                        json.dumps(log.snapshot())
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        snapshot = log.snapshot()
        seqs = [event["seq"] for event in snapshot["events"]]
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertEqual(4000, snapshot["events"][-1]["seq"])
        self.assertEqual(4000 - EVENT_LIMIT, sum(snapshot["dropped"].values()))


class ContextTest(_LogTest):
    """Reads of the context: an event when something moved or the answer was dropped."""

    _IDLE = {"lastConnEvent": {"category": "CONNECTED"}, "commandHistory": None}

    def _read(self, order, activity, reason="poll", applied=True):
        self.clock.tick(60)
        self.log.context(
            reason=reason, order=order, asked=self.clock.now - 1, applied=applied,
            summary=context_summary(self._IDLE, activity) if applied else None,
        )

    def test_unchanged_context_reads_are_only_counted(self) -> None:
        self._read(1, {}, reason="load")
        self._read(2, {})
        self._read(3, {}, reason="cycle_read")
        [event] = _events(self.log, "context")
        self.assertEqual("load", event["reason"])
        self.assertEqual(2, event["unchanged"])
        self.assertEqual(self.clock.now, event["last_at"])
        self.assertEqual(
            {"activity": {}, "commandHistory": None, "connection": "CONNECTED"},
            event["summary"],
        )

    def test_a_changed_activity_is_an_event(self) -> None:
        self._read(1, {})
        opened = {"activityStarted": _T0, "attributes": {"programName": "HQD_COTTONS",
                                                         "stainType": "19"}}
        self._read(2, opened, reason="cycle_read")
        self._read(3, opened)
        first, second = _events(self.log, "context")
        self.assertEqual(("poll", 1, 0), (first["reason"], first["order"], first["unchanged"]))
        self.assertEqual(("cycle_read", 2, 1), (second["reason"], second["order"],
                                                second["unchanged"]))
        self.assertEqual(
            {"activityStarted": _T0, "activityEnded": None, "completed": None,
             "attributes": {"programName": "HQD_COTTONS", "stainType": "19"}},
            second["summary"]["activity"],
        )

    def test_a_stale_answer_is_an_event(self) -> None:
        self._read(2, {})
        self._read(1, None, applied=False)
        self._read(3, {})
        first, stale = _events(self.log, "context")
        self.assertFalse(stale["applied"])
        self.assertEqual(1, stale["order"])
        self.assertNotIn("summary", stale)
        self.assertEqual(1, first["unchanged"])

    def test_a_context_without_an_activity_says_so(self) -> None:
        self.assertIsNone(context_summary(self._IDLE, None)["activity"])

    def test_identity_keys_never_enter_the_log(self) -> None:
        activity = {
            "activityStarted": _T0, "macAddress": "AA:BB:CC:00:11:22",
            "attributes": {
                "programName": "IOT_WASH_DARK", "principalUserId": "acct-0000-test",
                "secondaryUserId": "acct-0001-test", "prStrDisp": "Fav%20title",
                "prStr": "Fav title", "mobileId": "phone-0000", "mobileOs": "osname",
                "osVersion": "osver-99", "appVersion": "appver-99", "deviceModel": "devmodel-99",
                "macAddress": "AA:BB:CC:00:11:22",
            },
        }
        slot = {
            "timestampAccepted": _T0,
            "command": {
                "commandName": "startProgram", "programName": "PROGRAMS.WM_WD.IOT_WASH_DARK",
                "macAddress": "AA:BB:CC:00:11:22", "transactionId": "txn-mac-0000",
                "device": {"mobileId": "phone-0000"},
                "attributes": {"prStr": "Fav title"},
            },
        }
        self.log.context(reason="poll", order=1, asked=self.clock.now, applied=True,
                         summary=context_summary({"commandHistory": slot}, activity))
        blob = json.dumps(self.log.snapshot())
        for key in cycle_log.IDENTITY_KEYS:
            with self.subTest(key=key):
                self.assertNotIn(f'"{key}"', blob)
        for value in ("AA:BB:CC", "acct-", "Fav", "phone-0000", "osname", "osver-99",
                      "appver-99", "devmodel-99", "txn-mac"):
            with self.subTest(value=value):
                self.assertNotIn(value, blob)
        self.assertIn("IOT_WASH_DARK", blob)
        self.assertIn('"commandName": "startProgram"', blob)


class CommandTest(_LogTest):
    def test_command_outcomes(self) -> None:
        self.log.command(name="startProgram", programme="PROGRAMS.WM_WD.HQD_COTTONS",
                         sent="2026-10-26T08:00:00.000Z", outcome="accepted")
        self.log.command(name="settings", programme=None, sent=_T0, outcome="rejected",
                         code="7", error="Runtime.ImportModuleError")
        self.log.command(name="startProgram", programme=None, sent=_T0, outcome="raised",
                         error="RuntimeError")
        accepted, rejected, raised = _events(self.log, "command")
        self.assertEqual(
            {"name": "startProgram", "programme": "PROGRAMS.WM_WD.HQD_COTTONS",
             "sent": "2026-10-26T08:00:00.000Z", "outcome": "accepted"},
            {k: accepted[k] for k in ("name", "programme", "sent", "outcome")},
        )
        self.assertNotIn("result_code", accepted)
        self.assertEqual(("rejected", "7", "Runtime.ImportModuleError"),
                         (rejected["outcome"], rejected["result_code"], rejected["error"]))
        self.assertEqual(("raised", "RuntimeError"), (raised["outcome"], raised["error"]))

    def test_a_cycle_read_is_shown_as_the_transport_filled_it_in(self) -> None:
        record = {"edge": "start", "asked_at": "2026-10-26T08:00:04+00:00", "reads": []}
        self.log.cycle_read(record)
        record["reads"].append({"at": "2026-10-26T08:00:07+00:00", "settled": True})
        record["outcome"] = "settled"
        [event] = _events(self.log, "cycle_read")
        self.assertEqual(
            {"edge": "start", "asked_at": "2026-10-26T08:00:04+00:00",
             "reads": [{"at": "2026-10-26T08:00:07+00:00", "settled": True}],
             "outcome": "settled"},
            {k: event[k] for k in ("edge", "asked_at", "reads", "outcome")},
        )


class SessionTest(unittest.TestCase):
    """The log per appliance id at module level: a reload adds to it."""

    def setUp(self) -> None:
        cycle_log.forget_all()

    def test_attach_twice_is_one_log_with_two_sessions(self) -> None:
        first = cycle_log.attach("wm-1")
        second = cycle_log.attach("wm-1")
        self.assertIs(first, second)
        self.assertIsNot(first, cycle_log.attach("wm-2"))
        snapshot = first.snapshot()
        self.assertEqual(2, snapshot["sessions"])
        self.assertEqual([("session", 1), ("session", 2)],
                         [(e["kind"], e["session"]) for e in snapshot["events"]])

    def test_a_later_session_logs_only_what_differs(self) -> None:
        log = cycle_log.attach("wm-1")
        log.seed({"machMode": "1", "temp": "40", "prCode": "115"})
        self.assertEqual([], _events(log, "shadow"))  # the first session: silent
        log.shadow("mqtt", [ShadowRow("temp", "40", "30", True)])
        log.shadow("mqtt", [ShadowRow("prCode", "115", "136", False)])  # refused
        cycle_log.attach("wm-1")  # a reload
        log.seed({"machMode": "2", "temp": "30", "prCode": "115", "prPhase": "1"})
        resumed = _events(log, "shadow")[-1]
        self.assertTrue(resumed["resumed"])
        self.assertEqual("rest", resumed["road"])
        self.assertEqual(
            [{"key": "machMode", "before": "1", "after": "2"},
             {"key": "prPhase", "before": None, "after": "1"}],
            _rows(resumed),
        )

    def test_forget_all_starts_over(self) -> None:
        log = cycle_log.attach("wm-1")
        cycle_log.forget_all()
        self.assertIsNot(log, cycle_log.attach("wm-1"))


def _context(mach_mode="1", stamp=_T0, activity=None, **values):
    parameters = {"prCode": "115", "machMode": mach_mode, "stainType": "0", **values}
    return {
        "shadow": {"parameters": {
            key: {"parNewVal": value, "lastUpdate": stamp} for key, value in parameters.items()
        }},
        "activity": {} if activity is None else activity,
        "commandHistory": None,
        "lastConnEvent": {"category": "CONNECTED"},
    }


class EngineHooksTest(_Harness):
    """`HonAppliance`: the reads, our writes and the edge records reach the log."""

    def setUp(self) -> None:
        super().setUp()
        cycle_log.forget_all()

    def _washer(self, kind="WM"):
        self.TYPE = kind
        return self._appliance([], _context())

    def test_each_followed_type_has_a_log_and_the_others_none(self) -> None:
        for kind in ("WM", "WD", "TD", "DW"):
            with self.subTest(kind=kind):
                self.assertIsInstance(self._washer(kind).cycle_log, CycleLog)
        for kind in ("AC", "REF", "OV"):
            with self.subTest(kind=kind):
                self.assertIsNone(self._washer(kind).cycle_log)

    def test_load_attributes_logs_rest_rows_with_the_reason(self) -> None:
        app = self._washer()
        log = app.cycle_log
        # The first read of a session seeds the log silently.
        self.assertEqual(["session", "context"], [e["kind"] for e in _events(log)])
        # A poll: machMode moves with its stamp, prCode's stamp moves alone.
        self.cloud.context = _context("2", activity=_activity("HQD_COTTONS", "appliance"))
        self.cloud.context["shadow"]["parameters"]["machMode"]["lastUpdate"] = _T1
        self.cloud.context["shadow"]["parameters"]["prCode"]["lastUpdate"] = _T1
        self.loop.run_until_complete(app.update())
        [rest] = _events(log, "shadow")
        self.assertEqual("rest", rest["road"])
        self.assertEqual(
            [{"key": "prCode", "before": "115", "after": "115", "same": True, "stamp": _T1},
             {"key": "machMode", "before": "1", "after": "2", "stamp": _T1}],
            _rows(rest),
        )
        # The read a cycle edge asked for: the activity has closed.
        self.cloud.context["activity"] = {}
        self.loop.run_until_complete(app.update(force=True))
        self.assertEqual(["load", "poll", "cycle_read"],
                         [e["reason"] for e in _events(log, "context")])

    def test_a_dropped_answer_is_logged(self) -> None:
        app = self._washer()
        gate = asyncio.Event()
        answers = [_context("1"), _context("2")]

        async def load_attributes(appliance):
            answer = answers.pop(0)
            if answer["shadow"]["parameters"]["machMode"]["parNewVal"] == "1":
                await gate.wait()
            return answer

        self.cloud.load_attributes = load_attributes

        async def overlap():
            first = asyncio.ensure_future(app.load_attributes(reason="poll"))
            await asyncio.sleep(0)
            await app.load_attributes(reason="cycle_read")
            gate.set()
            await first

        self.loop.run_until_complete(overlap())
        stale = _events(app.cycle_log, "context")[-1]
        self.assertEqual(("poll", False), (stale["reason"], stale["applied"]))

    def test_our_write_after_a_command_is_logged_local(self) -> None:
        app = self._washer()
        app.sync_payload_to_params({"stainType": "19", "prCode": "115", "absent": "1"})
        [local] = _events(app.cycle_log, "shadow")
        self.assertEqual("local", local["road"])
        self.assertEqual([{"key": "stainType", "before": "0", "after": "19"}], _rows(local))

    def test_the_edge_record_is_logged(self) -> None:
        app = self._washer()
        app.note_cycle_read({"edge": "end", "asked_at": _T0, "reads": []})
        [read] = _events(app.cycle_log, "cycle_read")
        self.assertEqual("end", read["edge"])

    def test_a_dirty_held_value_does_not_stop_the_poll(self) -> None:
        # `parNewVal: null` or a number leaves a value whose str() raises
        # (tests/test_engine_attributes.py): the read must still apply every row, and
        # the next one must still replace it. (`machMode` is read by the washer's
        # derivation, which takes a number but not a null: the null rides `stainType`.)
        app = self._appliance([], _context(temp="40"))
        self.cloud.context = _context("1", temp="30")
        self.cloud.context["shadow"]["parameters"]["machMode"]["parNewVal"] = 2
        self.cloud.context["shadow"]["parameters"]["stainType"]["parNewVal"] = None
        self.assertTrue(self.loop.run_until_complete(app.load_attributes(reason="poll")))
        self.assertEqual("30", str(app.attributes["parameters"]["temp"]))  # a later row
        self.cloud.context = _context("2", temp="40")
        self.assertTrue(self.loop.run_until_complete(app.load_attributes(reason="poll")))
        self.assertEqual("2", str(app.attributes["parameters"]["machMode"]))
        self.assertEqual("0", str(app.attributes["parameters"]["stainType"]))
        first, second = _events(app.cycle_log, "shadow")
        self.assertEqual(
            [{"key": "machMode", "before": "1", "after": "2"},
             {"key": "stainType", "before": "0", "after": None},
             {"key": "temp", "before": "40", "after": "30"}],
            _rows(first),
        )
        self.assertEqual(
            [{"key": "stainType", "before": None, "after": "0"},
             {"key": "temp", "before": "30", "after": "40"}],
            _rows(second),
        )
        # A first read with a dirty value still seeds the log of a new object.
        cycle_log.forget_all()
        self.cloud.context["shadow"]["parameters"]["stainType"]["parNewVal"] = None
        fresh = self._appliance([], self.cloud.context)
        self.assertEqual(["session", "context"], [e["kind"] for e in _events(fresh.cycle_log)])

    def test_a_dirty_held_value_does_not_stop_our_write(self) -> None:
        app = self._washer()
        app.attributes["parameters"]["machMode"] = HonAttribute({"parNewVal": None})
        app.sync_payload_to_params({"machMode": "2", "stainType": "19"})
        self.assertEqual("2", str(app.attributes["parameters"]["machMode"]))
        self.assertEqual("19", str(app.attributes["parameters"]["stainType"]))
        [local] = _events(app.cycle_log, "shadow")
        self.assertEqual(
            [{"key": "machMode", "before": None, "after": "2"},
             {"key": "stainType", "before": "0", "after": "19"}],
            _rows(local),
        )

    def test_a_raising_log_costs_neither_the_read_nor_the_write(self) -> None:
        app = self._washer()

        class _Broken:
            def __getattr__(self, name):
                raise RuntimeError("broken")

        app._cycle_log = _Broken()
        self.cloud.context = _context("2")
        self.assertTrue(self.loop.run_until_complete(app.load_attributes(reason="poll")))
        self.assertEqual("2", str(app.attributes["parameters"]["machMode"]))
        app.sync_payload_to_params({"stainType": "19"})
        self.assertEqual("19", str(app.attributes["parameters"]["stainType"]))
        app.note_cycle_read({"edge": "start"})
        self.assertEqual({"edge": "start"}, app.cycle_read)


if __name__ == "__main__":
    unittest.main()
