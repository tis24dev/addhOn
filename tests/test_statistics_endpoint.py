# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The statistics endpoint: `/commands/v2/statistics` first, `/commands/v1/statistics` as the fallback.

The hOn app 2.30.7 reads the statistics of every laundry appliance (washer, washer-dryer,
dryer, dishwasher, laundry centre) from `GET commands/v2/statistics?macAddress&applianceType`
(apk2 decomp.txt @3333896-3333916) and keeps `statistics/v1/retrieve` for hobs and air
purifiers only. addhOn read v1 for every type. No v2 answer has ever been seen, so v2 is
tried first on the laundry types and v1 answers when v2 fails or answers empty; after one
fallback the appliance stays on v1 until the integration is reloaded (the poll's call
budget has no room for a retry every minute). The dump says which version answered.

A v2 answer is not always the statistics. On 2026-11-08 a beta5 dump of an HQD washer
showed the cloud answering `GET /commands/v2/statistics` with a 200 whose body mixed an
error (`errorMessage`, `errorType`, `requestId`) with stale statistics (`programsCounter`
"37" against the "39" of the good answer, `loadingPercentage` rows dated at midnight, no
`resultCode`, no cleaning maps) at every other poll for two hours: the washes counter
swung 39<->37 every minute, the cleaning binaries went unknown at alternate polls and the
cycle log filled with the swings. Such a body is discarded, the last good statistics kept,
the version unchanged (no v1 fallback) and the cycle log told (`rejected`).

The cleaning counters come from a second read, `GET /commands/v1/maintenance-cycle`, merged
over the statistics. On 2026-11-11 a v5.29.0 dump of an HQD washer showed it answering a
200 whose payload was `{"resultCode": "1", "errorCode": "1003", "errorDescription": "SQL
error: "}`, without the three cleaning maps, at alternate polls: merged as it came, the
cleaning binaries went unknown and the error keys entered the statistics. The app never
checks it and is saved by its store, which merges (apk2 decomp.txt @1346088-1346113;
`apk2/analysis/issue138-beta3/21`). Such an answer is discarded and the last good
maintenance answer merged in its place.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import install_stubs  # noqa: E402

install_stubs()

from custom_components.addhon.client import factory  # noqa: E402
from custom_components.addhon.client.engine import appliance as appliance_mod  # noqa: E402
from custom_components.addhon.client.transport.api import HonApi  # noqa: E402
from custom_components.addhon import diagnostics  # noqa: E402
from test_transport_api import FakeAppliance, FakeConnection  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _Api:
    """An engine-facing API double: v2 and v1 answer what the test sets, calls are kept."""

    def __init__(
        self, v2=None, v1=None, v2_error: Exception | None = None, maintenance=None
    ) -> None:
        self.v2, self.v1, self.v2_error = v2, v1, v2_error
        self.maintenance = {} if maintenance is None else maintenance
        self.calls: list[str] = []

    async def load_statistics_v2(self, appliance):
        self.calls.append("v2")
        if self.v2_error is not None:
            raise self.v2_error
        return self.v2

    async def load_statistics(self, appliance):
        self.calls.append("v1")
        return self.v1

    async def load_maintenance(self, appliance):
        self.calls.append("maintenance")
        # A new map each poll, as the cloud's.
        return dict(self.maintenance)


class _V1OnlyApi(_Api):
    """A double without the v2 method: every older test double looks like this."""

    load_statistics_v2 = None


def _good_answer(counter="39"):
    """A v2 answer as the cloud gives it when it works (beta5 dump of 2026-11-08)."""
    return {
        "resultCode": "0",
        "programsCounter": counter,
        "loadingPercentage": [
            {"current": "5", "max": "9", "date": "2026-11-06T13:26:06+00:00"},
        ],
        "mostUsedPrograms": [{"programName": "cottons", "count": "12"}],
        "temperatureUsage": {"temp40": counter},
        "filterCleaning": {"tot": "100", "current": "40"},
        "drumCleaning": {"tot": "50", "current": "3"},
    }


def _error_body():
    """The 200 the cloud answered at alternate polls: an error mixed with stale
    statistics, without `resultCode` and the cleaning maps."""
    return {
        "errorMessage": "'OperationalError' object has no attribute 'cursor'",
        "errorType": "AttributeError",
        "requestId": "0f3c2a9e-1111-4bbb-8ccc-0123456789ab",
        "programsCounter": "37",
        "loadingPercentage": [
            {"current": "5", "max": "9", "date": "2026-11-06T00:00:00+00:00"},
        ],
        "mostUsedPrograms": [{"programName": "cottons", "count": "12"}],
        "temperatureUsage": {"temp40": "37"},
    }


_REJECTED = {"version": "v2", "fallback": None, "rejected": "AttributeError"}


def _statistics_only(counter=41):
    """A v2 answer without the cleaning maps, which come from `/maintenance-cycle`. It
    carries a `resultCode` of its own: the first key of the merged map in the dump of
    2026-11-11, where the maintenance one overwrote it."""
    return {
        "resultCode": "0",
        "programsCounter": counter,
        "loadingPercentage": [
            {"current": 5, "max": 9, "date": "2026-11-09T16:05:30Z"},
        ],
        "temperatureUsage": {"tempUsageStatistics": {"temp40": 56}},
    }


_CLEANING = {"tot": "100", "count": "40", "remaining": "60", "percentage": "40"}
_CLEANING_MAPS = ("filterCleaning", "drumCleaning", "drawerCleaning")


def _good_maintenance():
    """A `/maintenance-cycle` answer as the cloud gives it when it works (v5.29.0 dump
    of 2026-11-11)."""
    return {
        "resultCode": "0",
        **{name: dict(_CLEANING) for name in _CLEANING_MAPS},
        "lastCheckup": None,
    }


def _maintenance_error():
    """The 200 `/maintenance-cycle` answered at alternate polls on 2026-11-11."""
    return {"resultCode": "1", "errorCode": "1003", "errorDescription": "SQL error: "}


_MAINTENANCE_REJECTED = {
    "version": "v2", "fallback": None, "maintenance_rejected": "maintenance_1003",
}


def _appliance(api, kind="WM"):
    return factory._native_engine_appliance_cls()(
        api, {"applianceTypeName": kind, "applianceModelId": 1, "macAddress": "aa"}, zone=0
    )


class TransportTest(unittest.TestCase):
    def test_v2_asks_the_app_endpoint_with_the_app_parameters(self) -> None:
        conn = FakeConnection({"payload": {"programsCounter": 7}})
        got = _run(HonApi(conn).load_statistics_v2(FakeAppliance()))
        self.assertEqual({"programsCounter": 7}, got)
        method, url, kwargs = conn.calls[0][:3]
        self.assertEqual("GET", method)
        self.assertTrue(url.endswith("/commands/v2/statistics"), url)
        self.assertEqual(
            {"macAddress": "AA:BB:CC:DD:EE:FF", "applianceType": "REF"}, kwargs["params"]
        )

    def test_v2_answers_an_empty_map_on_a_body_without_a_payload_map(self) -> None:
        for body in ({}, {"payload": "x"}, None, [1]):
            with self.subTest(body=body):
                self.assertEqual({}, _run(HonApi(FakeConnection(body)).load_statistics_v2(FakeAppliance())))


class EngineChoiceTest(unittest.TestCase):
    def test_a_laundry_type_reads_v2_and_says_so(self) -> None:
        for kind in ("WM", "WD", "TD", "DW"):
            with self.subTest(kind=kind):
                api = _Api(v2={"programsCounter": 3}, v1={"programsCounter": 99})
                app = _appliance(api, kind)
                _run(app.load_statistics())
                self.assertEqual(["v2", "maintenance"], api.calls)
                self.assertEqual(3, app.statistics["programsCounter"])
                self.assertEqual({"version": "v2", "fallback": None}, app.statistics_source)

    def test_the_other_types_read_v1_as_before(self) -> None:
        for kind in ("AC", "REF", "OV", "HW", "LC"):
            with self.subTest(kind=kind):
                api = _Api(v2={"programsCounter": 3}, v1={"programsCounter": 99})
                app = _appliance(api, kind)
                _run(app.load_statistics())
                self.assertEqual(["v1", "maintenance"], api.calls)
                self.assertEqual(99, app.statistics["programsCounter"])
                self.assertEqual({"version": "v1", "fallback": None}, app.statistics_source)

    def test_a_failing_v2_falls_back_to_v1_and_names_the_error(self) -> None:
        api = _Api(v2_error=RuntimeError("boom"), v1={"programsCounter": 99})
        app = _appliance(api)
        _run(app.load_statistics())
        self.assertEqual(["v2", "v1", "maintenance"], api.calls)
        self.assertEqual(99, app.statistics["programsCounter"])
        self.assertEqual({"version": "v1", "fallback": "RuntimeError"}, app.statistics_source)

    def test_an_empty_v2_answer_falls_back_to_v1(self) -> None:
        for empty in ({}, None, "x"):
            with self.subTest(empty=empty):
                api = _Api(v2=empty, v1={"programsCounter": 99})
                app = _appliance(api)
                _run(app.load_statistics())
                self.assertEqual(["v2", "v1", "maintenance"], api.calls)
                self.assertEqual({"version": "v1", "fallback": "empty"}, app.statistics_source)

    def test_a_v2_answer_of_another_shape_falls_back_to_v1(self) -> None:
        api = _Api(v2={"resultCode": "0", "foo": {"bar": 1}}, v1={"programsCounter": 99})
        app = _appliance(api)
        _run(app.load_statistics())
        self.assertEqual(["v2", "v1", "maintenance"], api.calls)
        self.assertEqual(99, app.statistics["programsCounter"])
        self.assertEqual({"version": "v1", "fallback": "shape"}, app.statistics_source)

    def test_a_known_key_without_a_value_does_not_count(self) -> None:
        api = _Api(v2={"loadingPercentage": None, "resultCode": "0"}, v1={"programsCounter": 99})
        app = _appliance(api)
        _run(app.load_statistics())
        self.assertEqual(["v2", "v1", "maintenance"], api.calls)
        self.assertEqual({"version": "v1", "fallback": "shape"}, app.statistics_source)

    def test_a_known_key_of_another_shape_does_not_count(self) -> None:
        for answer in (
            {"loadingPercentage": "x"},
            {"loadingPercentage": {"rows": []}},
            {"temperatureUsage": [30, 20, 50]},
            {"programsCounter": {}},
            {"programsCounter": "many"},
            {"programsCounter": True},
            {"mostUsedPrograms": "none"},
        ):
            with self.subTest(answer=answer):
                api = _Api(v2=answer, v1={"programsCounter": 99})
                app = _appliance(api)
                _run(app.load_statistics())
                self.assertEqual(["v2", "v1", "maintenance"], api.calls)
                self.assertEqual({"version": "v1", "fallback": "shape"}, app.statistics_source)

    def test_a_numeric_counter_as_text_counts(self) -> None:
        api = _Api(v2={"programsCounter": "84"}, v1={"programsCounter": 99})
        app = _appliance(api)
        _run(app.load_statistics())
        self.assertEqual(["v2", "maintenance"], api.calls)
        self.assertEqual({"version": "v2", "fallback": None}, app.statistics_source)

    def test_one_known_key_is_enough_for_v2(self) -> None:
        shaped = {
            "programsCounter": 7, "mostUsedPrograms": [], "temperatureUsage": {}, "loadingPercentage": [],
        }
        for key in sorted(appliance_mod.STATISTICS_KEYS):
            with self.subTest(key=key):
                api = _Api(v2={key: shaped[key]}, v1={"programsCounter": 99})
                app = _appliance(api)
                _run(app.load_statistics())
                self.assertEqual(["v2", "maintenance"], api.calls)
                self.assertEqual({"version": "v2", "fallback": None}, app.statistics_source)

    def test_a_v2_error_body_is_discarded_and_the_last_statistics_kept(self) -> None:
        api = _Api(v2=_good_answer(), v1={"programsCounter": 99})
        app = _appliance(api)
        _run(app.load_statistics())
        api.v2 = _error_body()
        _run(app.load_statistics())
        self.assertEqual(["v2", "maintenance", "v2", "maintenance"], api.calls)
        self.assertEqual("39", app.statistics["programsCounter"])
        self.assertEqual({"tot": "100", "current": "40"}, app.statistics["filterCleaning"])
        self.assertEqual("2026-11-06T13:26:06+00:00", app.statistics["loadingPercentage"][0]["date"])
        self.assertNotIn("errorMessage", app.statistics)
        self.assertNotIn("errorType", app.statistics)
        self.assertEqual(_REJECTED, app.statistics_source)
        self.assertFalse(app._statistics_v1_only)

    def test_either_error_key_alone_marks_an_error_body(self) -> None:
        for body, reason in (
            ({"errorType": "AttributeError", "programsCounter": "37"}, "AttributeError"),
            ({"errorMessage": "boom", "programsCounter": "37"}, "error"),
            ({"errorType": "", "errorMessage": "boom", "programsCounter": "37"}, "error"),
            ({"errorType": "Internal Server Error", "programsCounter": "37"}, "error"),
        ):
            with self.subTest(body=body):
                api = _Api(v2=_good_answer(), v1={"programsCounter": 99})
                app = _appliance(api)
                _run(app.load_statistics())
                api.v2 = body
                _run(app.load_statistics())
                self.assertEqual("39", app.statistics["programsCounter"])
                self.assertEqual(
                    {"version": "v2", "fallback": None, "rejected": reason}, app.statistics_source
                )
                self.assertNotIn("v1", api.calls)

    def test_error_keys_without_a_value_are_not_an_error(self) -> None:
        # Null, empty or blank error fields say nothing (PR #146 review, Sourcery): a good
        # answer carrying them must not be discarded for stale statistics.
        for body in (
            {"errorMessage": None, "errorType": None, "programsCounter": "37"},
            {"errorMessage": "", "errorType": "", "programsCounter": "37"},
            {"errorType": "   ", "programsCounter": "37"},
        ):
            with self.subTest(body=body):
                api = _Api(v2=body)
                app = _appliance(api)
                _run(app.load_statistics())
                self.assertEqual("37", app.statistics["programsCounter"])
                self.assertEqual({"version": "v2", "fallback": None}, app.statistics_source)

    def test_an_error_type_longer_than_the_dump_prints_reads_as_error(self) -> None:
        # The dump prints a reason of at most 64 characters (`_statistics_reason`); a
        # longer `errorType` is reported as `error` on both paths (PR #146 review,
        # CodeRabbit).
        api = _Api(v2=_good_answer(), v1={"programsCounter": 99})
        app = _appliance(api)
        _run(app.load_statistics())
        api.v2 = {"errorType": "E" * 65, "programsCounter": "37"}
        _run(app.load_statistics())
        self.assertEqual(
            {"version": "v2", "fallback": None, "rejected": "error"}, app.statistics_source
        )
        api.v2 = {"errorType": "E" * 64, "programsCounter": "37"}
        _run(app.load_statistics())
        self.assertEqual("E" * 64, app.statistics_source["rejected"])

    def test_the_next_good_v2_answer_clears_the_rejection(self) -> None:
        api = _Api(v2=_good_answer(), v1={"programsCounter": 99})
        app = _appliance(api)
        _run(app.load_statistics())
        api.v2 = _error_body()
        _run(app.load_statistics())
        api.v2 = _good_answer("40")
        _run(app.load_statistics())
        self.assertEqual(["v2", "maintenance"] * 3, api.calls)
        self.assertEqual("40", app.statistics["programsCounter"])
        self.assertEqual({"version": "v2", "fallback": None}, app.statistics_source)
        self.assertFalse(app._statistics_v1_only)

    def test_an_error_body_at_the_first_read_leaves_the_statistics_empty(self) -> None:
        api = _Api(v2=_error_body(), v1={"programsCounter": 99})
        app = _appliance(api)
        _run(app.load_statistics())
        self.assertEqual(["v2", "maintenance"], api.calls)
        self.assertEqual({}, app.statistics)
        self.assertEqual(_REJECTED, app.statistics_source)
        self.assertFalse(app._statistics_v1_only)
        api.v2 = _good_answer()
        _run(app.load_statistics())
        self.assertEqual("39", app.statistics["programsCounter"])
        self.assertEqual({"version": "v2", "fallback": None}, app.statistics_source)

    def test_the_kept_statistics_are_a_copy_the_merge_cannot_spoil(self) -> None:
        # The maintenance merge writes into the answer returned; a rejection must hand
        # back the good answer as it came, not the copy the last merge wrote into.
        api = _Api(v2=_good_answer())
        app = _appliance(api)
        _run(app.load_statistics())
        app.statistics["spoiled"] = True
        api.v2 = _error_body()
        _run(app.load_statistics())
        self.assertNotIn("spoiled", app.statistics)

    def test_after_one_fallback_the_appliance_stays_on_v1(self) -> None:
        api = _Api(v2={}, v1={"programsCounter": 99})
        app = _appliance(api)
        _run(app.load_statistics())
        _run(app.load_statistics())
        self.assertEqual(["v2", "v1", "maintenance", "v1", "maintenance"], api.calls)
        self.assertEqual({"version": "v1", "fallback": "empty"}, app.statistics_source)

    def test_an_api_without_v2_reads_v1(self) -> None:
        api = _V1OnlyApi(v1={"programsCounter": 99})
        app = _appliance(api)
        _run(app.load_statistics())
        self.assertEqual(["v1", "maintenance"], api.calls)
        self.assertEqual({"version": "v1", "fallback": None}, app.statistics_source)

    def test_before_the_first_read_the_source_is_unknown(self) -> None:
        self.assertIsNone(_appliance(_Api()).statistics_source)

    def test_the_laundry_types_are_the_followed_ones(self) -> None:
        self.assertEqual(appliance_mod.FOLLOWED_TYPES, appliance_mod.STATISTICS_V2_TYPES)


class CycleLogTest(unittest.TestCase):
    """Each poll's statistics are compared with the last: a measured load per cycle and
    the counters that moved reach the cycle log (issue #138); the first read only seeds."""

    def setUp(self) -> None:
        from custom_components.addhon.client.engine import cycle_log

        cycle_log.forget_all()

    @staticmethod
    def _payload(counter, *loads):
        return {
            "programsCounter": counter,
            "loadingPercentage": [
                {"current": current, "max": 1, "date": date} for current, date in loads
            ],
            "temperatureUsage": {"temp0to30": 1, "temp40": counter, "temp60to90": 0},
        }

    def _events(self, app):
        return [e for e in app.cycle_log.snapshot()["events"] if e["kind"] == "statistics"]

    def test_the_first_read_seeds_then_a_new_cycle_is_logged(self) -> None:
        api = _Api(v2=self._payload(4, (0.9, "2026-10-20")))
        app = _appliance(api)
        _run(app.load_statistics())
        self.assertEqual([], self._events(app))
        api.v2 = self._payload(5, (0.9, "2026-10-20"), (0.4, "2026-10-29"))
        _run(app.load_statistics())
        _run(app.load_statistics())
        [event] = self._events(app)
        self.assertEqual([{"current": "0.4", "max": "1", "date": "2026-10-29"}],
                         event["new_rows"])
        self.assertEqual(
            {"programsCounter": {"before": "4", "after": "5"},
             "temperatureUsage.temp40": {"before": "4", "after": "5"}},
            event["changed"],
        )

    def test_an_empty_answer_is_not_a_change(self) -> None:
        api = _Api(v2=self._payload(4), v1={})
        app = _appliance(api)
        _run(app.load_statistics())
        api.v2 = {}
        _run(app.load_statistics())
        api.v1 = self._payload(4)
        _run(app.load_statistics())
        self.assertEqual([], self._events(app))

    def test_an_empty_maintenance_answer_is_not_a_change(self) -> None:
        # A one-off `{}` from `/maintenance-cycle` while the statistics answer: the
        # maintenance keys last seen are kept for the comparison, so neither the gap
        # nor their return is an event.
        api = _Api()
        maintenance = [{"drumCleaning": {"cycles": 12}}, {}, {"drumCleaning": {"cycles": 12}}]

        async def load_statistics_v2(appliance):
            # A new answer each poll, as the cloud's: the merge works on it in place.
            return self._payload(4)

        async def load_maintenance(appliance):
            return maintenance.pop(0)

        api.load_statistics_v2 = load_statistics_v2
        api.load_maintenance = load_maintenance
        app = _appliance(api)
        for _ in range(3):
            _run(app.load_statistics())
        self.assertEqual([], self._events(app))
        self.assertEqual({"cycles": 12}, app.statistics["drumCleaning"])

    def test_an_empty_statistics_half_is_not_a_change(self) -> None:
        # The mirror: `/statistics` answers `{}` once while `/maintenance-cycle` does
        # not; the statistics keys last seen stay in the comparison.
        api = _V1OnlyApi()
        answers = [self._payload(4), {}, self._payload(4)]

        async def load_statistics(appliance):
            return answers.pop(0)

        async def load_maintenance(appliance):
            return {"drumCleaning": {"cycles": 12}}

        api.load_statistics = load_statistics
        api.load_maintenance = load_maintenance
        app = _appliance(api)
        _run(app.load_statistics())
        _run(app.load_statistics())
        self.assertEqual({"drumCleaning": {"cycles": 12}}, app.statistics)
        _run(app.load_statistics())
        self.assertEqual([], self._events(app))

    def test_a_key_that_disappears_is_logged_as_null(self) -> None:
        api = _Api(v2=dict(self._payload(4), totalWashCycle=3))
        app = _appliance(api)
        _run(app.load_statistics())
        api.v2 = self._payload(4)
        _run(app.load_statistics())
        [event] = self._events(app)
        self.assertEqual({"totalWashCycle": {"before": "3", "after": None}}, event["changed"])
        self.assertNotIn("new_rows", event)

    def test_a_discarded_answer_is_logged_as_rejected_and_not_as_a_change(self) -> None:
        api = _Api(v2=_good_answer())
        app = _appliance(api)
        _run(app.load_statistics())
        api.v2 = _good_answer("40")
        _run(app.load_statistics())
        api.v2 = _error_body()
        _run(app.load_statistics())
        api.v2 = _good_answer("40")
        _run(app.load_statistics())
        changed, rejected = self._events(app)
        self.assertEqual({"before": "39", "after": "40"}, changed["changed"]["programsCounter"])
        self.assertNotIn("rejected", changed)
        self.assertEqual("AttributeError", rejected["rejected"])
        self.assertNotIn("changed", rejected)
        self.assertNotIn("new_rows", rejected)
        self.assertNotIn("requestId", json.dumps(rejected))

    def test_a_raising_log_costs_nothing_to_the_poll(self) -> None:
        api = _Api(v2=self._payload(4))
        app = _appliance(api)
        _run(app.load_statistics())

        def boom(**kwargs):
            raise RuntimeError("broken log")

        app.cycle_log.statistics = boom
        api.v2 = self._payload(5)
        with self.assertLogs(appliance_mod._LOGGER, "DEBUG") as logs:
            _run(app.load_statistics())
        self.assertEqual(5, app.statistics["programsCounter"])
        self.assertTrue(any("Cycle log failed" in line for line in logs.output))

    def test_the_types_not_followed_keep_no_log(self) -> None:
        app = _appliance(_Api(v1=self._payload(4)), "AC")
        _run(app.load_statistics())
        self.assertIsNone(app.cycle_log)


class DumpTest(unittest.TestCase):
    def _block(self, source):
        api = _Api(v2={"programsCounter": 3}, v1={"programsCounter": 99})
        app = _appliance(api)
        if source is not None:
            _run(app.load_statistics())
        data = {"type": "WM", "appliance": app, "name": "W", "attributes": {}, "statistics": app.statistics}
        return diagnostics._appliance_block("wm1", data)

    def test_the_source_follows_statistics(self) -> None:
        block = self._block("v2")
        keys = list(block)
        self.assertEqual("statistics_source", keys[keys.index("statistics") + 1])
        self.assertEqual({"version": "v2", "fallback": None}, block["statistics_source"])
        json.dumps(block)

    def test_no_read_yet_prints_no_source(self) -> None:
        self.assertNotIn("statistics_source", self._block(None))

    def test_a_rejected_answer_is_printed(self) -> None:
        api = _Api(v2=_good_answer(), v1={"programsCounter": 99})
        app = _appliance(api)
        _run(app.load_statistics())
        api.v2 = _error_body()
        _run(app.load_statistics())
        data = {"type": "WM", "appliance": app, "name": "W", "attributes": {}, "statistics": app.statistics}
        block = diagnostics._appliance_block("wm1", data)
        self.assertEqual(_REJECTED, block["statistics_source"])
        [event] = [e for e in block["cycle_log"]["events"] if e["kind"] == "statistics"]
        self.assertEqual("AttributeError", event["rejected"])
        self.assertNotIn("requestId", json.dumps(block["cycle_log"]))
        json.dumps(block)

    def test_a_rejection_outside_the_vocabulary_prints_null(self) -> None:
        api = _Api(v2={"programsCounter": 3})
        app = _appliance(api)
        app._statistics_source = {"version": "v2", "fallback": None, "rejected": "<script>"}
        data = {"type": "WM", "appliance": app, "name": "W", "attributes": {}, "statistics": {}}
        block = diagnostics._appliance_block("wm1", data)
        self.assertEqual({"version": "v2", "fallback": None, "rejected": None}, block["statistics_source"])

    def test_a_source_outside_the_vocabulary_is_dropped(self) -> None:
        api = _Api(v2={"programsCounter": 3})
        app = _appliance(api)
        app._statistics_source = {"version": "v9", "fallback": "<script>"}
        data = {"type": "WM", "appliance": app, "name": "W", "attributes": {}, "statistics": {}}
        block = diagnostics._appliance_block("wm1", data)
        self.assertEqual({"version": None, "fallback": None}, block["statistics_source"])


class MaintenanceTest(unittest.TestCase):
    """A `/maintenance-cycle` answer carrying an error is discarded and the last good one
    merged in its place (decision of 2026-11-11, `apk2/analysis/issue138-beta3/21`)."""

    def setUp(self) -> None:
        from custom_components.addhon.client.engine import cycle_log

        cycle_log.forget_all()
        # The log is per appliance id and every test here uses the same one.
        self.addCleanup(cycle_log.forget_all)

    @staticmethod
    def _events(app):
        return [e for e in app.cycle_log.snapshot()["events"] if e["kind"] == "statistics"]

    def test_a_maintenance_error_is_discarded_and_the_last_cleaning_counters_kept(self) -> None:
        api = _Api(v2=_statistics_only(), maintenance=_good_maintenance())
        app = _appliance(api)
        _run(app.load_statistics())
        self.assertEqual({"version": "v2", "fallback": None}, app.statistics_source)
        api.maintenance = _maintenance_error()
        _run(app.load_statistics())
        self.assertEqual(["v2", "maintenance"] * 2, api.calls)
        for name in _CLEANING_MAPS:
            self.assertEqual(_CLEANING, app.statistics[name])
        self.assertNotIn("errorCode", app.statistics)
        self.assertNotIn("errorDescription", app.statistics)
        # "0" twice over: the v2 answer carries one, the last good maintenance another.
        self.assertEqual("0", app.statistics["resultCode"])
        self.assertEqual(41, app.statistics["programsCounter"])
        self.assertEqual(_MAINTENANCE_REJECTED, app.statistics_source)
        [event] = self._events(app)
        self.assertEqual("maintenance_1003", event["rejected"])
        self.assertNotIn("changed", event)
        self.assertNotIn("new_rows", event)

    def test_the_next_good_maintenance_clears_the_rejection(self) -> None:
        api = _Api(v2=_statistics_only(), maintenance=_good_maintenance())
        app = _appliance(api)
        _run(app.load_statistics())
        api.maintenance = _maintenance_error()
        _run(app.load_statistics())
        api.maintenance = dict(_good_maintenance(), drumCleaning=dict(_CLEANING, count="41"))
        _run(app.load_statistics())
        self.assertEqual({"version": "v2", "fallback": None}, app.statistics_source)
        self.assertEqual("41", app.statistics["drumCleaning"]["count"])
        rejected, changed = self._events(app)
        self.assertEqual("maintenance_1003", rejected["rejected"])
        self.assertEqual(
            {"drumCleaning.count": {"before": "40", "after": "41"}}, changed["changed"]
        )

    def test_a_maintenance_error_at_the_first_read_merges_nothing(self) -> None:
        api = _Api(v2=_statistics_only(), maintenance=_maintenance_error())
        app = _appliance(api)
        _run(app.load_statistics())
        self.assertEqual(_statistics_only(), app.statistics)
        self.assertEqual(_MAINTENANCE_REJECTED, app.statistics_source)
        [event] = self._events(app)
        self.assertEqual("maintenance_1003", event["rejected"])

    def test_a_zero_result_code_with_blank_error_fields_is_a_good_answer(self) -> None:
        for extra in (
            {},
            {"errorCode": None},
            {"errorCode": "", "errorDescription": "  "},
            {"resultCode": 0},
            {"resultCode": "0.0", "errorMessage": None, "errorType": ""},
            {"resultCode": None},
            {"resultCode": " "},
        ):
            with self.subTest(extra=extra):
                answer = _good_maintenance() | extra
                api = _Api(v2=_statistics_only(), maintenance=answer)
                app = _appliance(api)
                _run(app.load_statistics())
                self.assertEqual(_CLEANING, app.statistics["filterCleaning"])
                self.assertEqual({"version": "v2", "fallback": None}, app.statistics_source)
                self.assertEqual([], self._events(app))

    def test_each_sign_of_an_error_alone_discards_the_answer(self) -> None:
        for extra, reason in (
            ({"resultCode": "1"}, "maintenance"),
            ({"resultCode": 1}, "maintenance"),
            ({"resultCode": "KO"}, "maintenance"),
            ({"errorCode": "1003"}, "maintenance_1003"),
            ({"errorCode": 1003}, "maintenance_1003"),
            ({"errorCode": "E_SQL"}, "maintenance_E_SQL"),
            ({"errorCode": "SQL error!"}, "maintenance"),
            ({"errorCode": "9" * 64}, "maintenance"),
            ({"errorDescription": "SQL error: "}, "maintenance"),
            ({"errorMessage": "boom"}, "maintenance"),
            ({"errorType": "AttributeError"}, "maintenance"),
        ):
            with self.subTest(extra=extra):
                api = _Api(v2=_statistics_only(), maintenance=_good_maintenance())
                app = _appliance(api)
                _run(app.load_statistics())
                api.maintenance = dict(
                    _good_maintenance() | extra, drumCleaning=dict(_CLEANING, count="99")
                )
                _run(app.load_statistics())
                self.assertEqual("40", app.statistics["drumCleaning"]["count"])
                self.assertEqual(reason, app.statistics_source["maintenance_rejected"])
                for key in extra:
                    if key != "resultCode":
                        self.assertNotIn(key, app.statistics)

    def test_an_empty_maintenance_answer_neither_sets_nor_clears_the_rejection(self) -> None:
        api = _Api(v2=_statistics_only())
        app = _appliance(api)
        _run(app.load_statistics())
        self.assertEqual({"version": "v2", "fallback": None}, app.statistics_source)
        api.maintenance = _maintenance_error()
        _run(app.load_statistics())
        api.maintenance = {}
        _run(app.load_statistics())
        self.assertEqual(_MAINTENANCE_REJECTED, app.statistics_source)
        self.assertNotIn("filterCleaning", app.statistics)

    def test_the_rejection_stands_beside_a_statistics_one_and_on_v1(self) -> None:
        api = _Api(v2=_good_answer(), maintenance=_maintenance_error())
        app = _appliance(api)
        _run(app.load_statistics())
        api.v2 = _error_body()
        _run(app.load_statistics())
        self.assertEqual(
            dict(_REJECTED, maintenance_rejected="maintenance_1003"), app.statistics_source
        )
        api = _Api(v1=_statistics_only(), maintenance=_maintenance_error())
        app = _appliance(api, "AC")
        _run(app.load_statistics())
        _run(app.load_statistics())
        self.assertEqual(
            {"version": "v1", "fallback": None, "maintenance_rejected": "maintenance_1003"},
            app.statistics_source,
        )
        self.assertNotIn("errorCode", app.statistics)
        api.maintenance = _good_maintenance()
        _run(app.load_statistics())
        self.assertEqual({"version": "v1", "fallback": None}, app.statistics_source)

    def test_a_rejected_maintenance_is_printed_in_the_dump(self) -> None:
        api = _Api(v2=_statistics_only(), maintenance=_good_maintenance())
        app = _appliance(api)
        _run(app.load_statistics())
        api.maintenance = _maintenance_error()
        _run(app.load_statistics())
        data = {"type": "WM", "appliance": app, "name": "W", "attributes": {}, "statistics": app.statistics}
        block = diagnostics._appliance_block("wm1", data)
        self.assertEqual(_MAINTENANCE_REJECTED, block["statistics_source"])
        self.assertEqual(_CLEANING, block["statistics"]["drumCleaning"])
        [event] = [e for e in block["cycle_log"]["events"] if e["kind"] == "statistics"]
        self.assertEqual("maintenance_1003", event["rejected"])
        json.dumps(block)

    def test_a_maintenance_rejection_outside_the_vocabulary_prints_null(self) -> None:
        app = _appliance(_Api(v2={"programsCounter": 3}))
        app._statistics_source = {
            "version": "v2", "fallback": None, "maintenance_rejected": "<script>",
        }
        data = {"type": "WM", "appliance": app, "name": "W", "attributes": {}, "statistics": {}}
        block = diagnostics._appliance_block("wm1", data)
        self.assertEqual(
            {"version": "v2", "fallback": None, "maintenance_rejected": None},
            block["statistics_source"],
        )


if __name__ == "__main__":
    unittest.main()
