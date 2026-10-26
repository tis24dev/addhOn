"""The statistics endpoint: `/commands/v2/statistics` first, `/commands/v1/statistics` as the fallback.

The hOn app 2.30.7 reads the statistics of every laundry appliance (washer, washer-dryer,
dryer, dishwasher, laundry centre) from `GET commands/v2/statistics?macAddress&applianceType`
(apk2 decomp.txt @3333896-3333916) and keeps `statistics/v1/retrieve` for hobs and air
purifiers only. addhOn read v1 for every type. No v2 answer has ever been seen, so v2 is
tried first on the laundry types and v1 answers when v2 fails or answers empty; after one
fallback the appliance stays on v1 until the integration is reloaded (the poll's call
budget has no room for a retry every minute). The dump says which version answered.
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

    def __init__(self, v2=None, v1=None, v2_error: Exception | None = None) -> None:
        self.v2, self.v1, self.v2_error = v2, v1, v2_error
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
        return {}


class _V1OnlyApi(_Api):
    """A double without the v2 method: every older test double looks like this."""

    load_statistics_v2 = None


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

    def test_one_known_key_is_enough_for_v2(self) -> None:
        for key in sorted(appliance_mod.STATISTICS_KEYS):
            with self.subTest(key=key):
                api = _Api(v2={key: 1}, v1={"programsCounter": 99})
                app = _appliance(api)
                _run(app.load_statistics())
                self.assertEqual(["v2", "maintenance"], api.calls)
                self.assertEqual({"version": "v2", "fallback": None}, app.statistics_source)

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

    def test_a_source_outside_the_vocabulary_is_dropped(self) -> None:
        api = _Api(v2={"programsCounter": 3})
        app = _appliance(api)
        app._statistics_source = {"version": "v9", "fallback": "<script>"}
        data = {"type": "WM", "appliance": app, "name": "W", "attributes": {}, "statistics": {}}
        block = diagnostics._appliance_block("wm1", data)
        self.assertEqual({"version": None, "fallback": None}, block["statistics_source"])


if __name__ == "__main__":
    unittest.main()
