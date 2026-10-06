# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A programme started elsewhere is followed while Home Assistant runs (issue #112).

infy1995's washer: the official app started `iot_wash_dark` (prCode 116, shared by 21
programmes) while the integration ran. Until a reload, Home Assistant showed the base
programme of that code and the options of the last start it had read at setup, because
`/history` was read only at catalog load. The decisions of 2026-10-06 (analysis in
`apk2/analysis/issue112-wm/7-dump-infy1995-beta10.md` §4):

- D1 c: when a new cycle shows up, read `/history` again and redo the recovery;
- D2: the options follow too, as after a reload;
- D3: the new cycle shows up as a new `startProgram` in the context's `commandHistory`
  or as a new `activityStarted`;
- D4: washers, washer-dryers, dryers and dishwashers;
- D5: a recovered programme is trusted even when the shadow's prPosition disagrees;
- T1: a read with no newer start (a cycle started on the panel) changes nothing;
- T2: one read per cycle, whichever signals fire;
- T3: no read scheduled after our own start or stop any more;
- T4: pause, stop and a `commandHistory` the cloud nulled are not new cycles;
- the read runs in the background and is applied at the next poll, up to 3 attempts;
- only `startProgram` is recovered.
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


def _fixed(value):
    return {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": value}


def _temp(default="30"):
    return {
        "typology": "range", "category": "command", "mandatory": 0,
        "defaultValue": default, "minimumValue": "0", "maximumValue": "90",
        "incrementValue": "10",
    }


def _programme(pr_code, position=None):
    parameters = {"prCode": _fixed(pr_code), "temp": _temp()}
    if position is not None:
        parameters["prPosition"] = _fixed(position)
    return {"description": "d", "protocolType": "MQTT", "parameters": parameters}


# infy1995's three programmes that matter: the base of 116, the downloaded programme the
# app started on the same code, and the spin of the start read at setup.
_WM_COMMANDS = {
    "applianceModel": {"options": {}},
    "settings": {"setParameters": {"description": "d", "protocolType": "MQTT", "parameters": {
        "childLock": {"typology": "enum", "category": "command", "mandatory": 0,
                      "defaultValue": "0", "enumValues": ["0", "1"]}}}},
    "startProgram": {
        "PROGRAMS.WM_WD.HQD_SYNTHETIC_AND_COLOURED": _programme("116"),
        "PROGRAMS.WM_WD.IOT_WASH_DARK": _programme("116"),
        "PROGRAMS.WM_WD.HQD_SPIN": _programme("74"),
    },
    "dictionaryId": 1,
}

# bbosson's dishwasher: prCode 8 shared by eco and a downloaded programme on the same
# dial slot, and a shadow that reports another prPosition (2 dumps out of 2).
_DW_COMMANDS = {
    "applianceModel": {"options": {}},
    "startProgram": {
        "PROGRAMS.DW.ECO": _programme("8", "3"),
        "PROGRAMS.DW.IOT_HAPPY_HOUR": _programme("8", "3"),
        "PROGRAMS.DW.RAPID_20": _programme("28", "12"),
    },
    "dictionaryId": 1,
}

_SPIN_AT = "2026-10-02T23:54:15.402Z"
_DARK_AT = "2026-10-06T13:46:15.231Z"
_ACTIVITY_AT = "2026-10-06T13:46:19Z"


def _start(programme, stamp, temp="0", pr_code=None):
    """One `startProgram` as the app records it: no `program` key, a top-level name."""
    code = pr_code or {"HQD_SPIN": "74"}.get(programme.rsplit(".", 1)[-1], "116")
    return {"command": {
        "commandName": "startProgram",
        "programName": programme,
        "timestamp": stamp,
        "parameters": {"prCode": code, "temp": temp},
    }}


def _shadow(pr_code, position=None):
    parameters = {"prCode": {"parNewVal": pr_code, "lastUpdate": "2026-10-06T13:46:19Z"}}
    if position is not None:
        parameters["prPosition"] = {"parNewVal": position, "lastUpdate": "2026-10-06T13:46:19Z"}
    return {"shadow": {"parameters": parameters}}


class CloudDouble:
    """The cloud as the appliance sees it, changed by the test between polls."""

    def __init__(self, commands, history, context) -> None:
        self.commands = commands
        self.history = history
        self.context = context
        self.history_reads = 0
        self.history_error: Exception | None = None
        # Set to an unset asyncio.Event to hold a read in flight across polls.
        self.history_gate: asyncio.Event | None = None

    async def load_commands(self, appliance):
        return json.loads(json.dumps(self.commands))

    async def load_favourites(self, appliance):
        return []

    async def load_command_history(self, appliance):
        self.history_reads += 1
        listed = json.loads(json.dumps(self.history))
        if self.history_gate is not None:
            await self.history_gate.wait()
        if self.history_error is not None:
            raise self.history_error
        return listed

    async def load_attributes(self, appliance):
        return json.loads(json.dumps(self.context))

    async def load_statistics(self, appliance):
        return {}

    async def load_maintenance(self, appliance):
        return {}

    async def send_command(self, appliance, name, params, ancillary, category, **kwargs):
        return True


class _Harness(unittest.TestCase):
    TYPE = "WM"
    COMMANDS = _WM_COMMANDS

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def _appliance(self, history, context):
        self.cloud = CloudDouble(self.COMMANDS, history, context)
        app = factory._native_engine_appliance_cls()(
            self.cloud,
            {"applianceTypeName": self.TYPE, "applianceModelId": 1, "macAddress": "aa"},
            zone=0,
        )
        self.loop.run_until_complete(app.load_commands())
        self.loop.run_until_complete(app.load_attributes())
        return app

    def _poll(self, app, times=1):
        """One coordinator poll each, then let the background read finish."""
        for _ in range(times):
            self.loop.run_until_complete(app.update(force=True))
            self.loop.run_until_complete(asyncio.sleep(0.01))

    @staticmethod
    def _start_command(app):
        return app.commands["startProgram"]


class FollowAStartMadeElsewhereTest(_Harness):
    """infy1995's case, then the signals one by one."""

    def _at_setup(self):
        # Setup read a spin; the washer now runs a 116 nobody told us about.
        context = _shadow("116") | {"activity": {}, "commandHistory": _start(
            "PROGRAMS.WM_WD.HQD_SPIN", _SPIN_AT)}
        return self._appliance([_start("PROGRAMS.WM_WD.HQD_SPIN", _SPIN_AT)], context)

    def _app_starts_dark(self, *, command=True, activity=True):
        self.cloud.history = [
            _start("PROGRAMS.WM_WD.IOT_WASH_DARK", _DARK_AT, temp="40"),
            _start("PROGRAMS.WM_WD.HQD_SPIN", _SPIN_AT),
        ]
        if command:
            self.cloud.context["commandHistory"] = _start("PROGRAMS.WM_WD.IOT_WASH_DARK", _DARK_AT, "40")
        if activity:
            self.cloud.context["activity"] = {
                "activityStarted": _ACTIVITY_AT,
                "attributes": {"programName": "IOT_WASH_DARK", "prCode": "116"},
            }

    def test_setup_shows_the_base_programme_of_the_shared_code(self) -> None:
        # The starting point of the report: the spin read at setup does not match 116.
        app = self._at_setup()
        self.assertEqual("hqd_synthetic_and_coloured", app.attributes["programName"])

    def test_a_start_from_the_app_is_followed_at_the_next_poll(self) -> None:
        app = self._at_setup()
        self._app_starts_dark()
        self._poll(app)  # sees the new cycle, reads /history in the background
        self._poll(app)  # applies what the read brought
        self.assertEqual("iot_wash_dark", app.attributes["programName"])
        command = self._start_command(app)
        self.assertEqual("PROGRAMS.WM_WD.IOT_WASH_DARK", command.category)
        self.assertEqual("40", str(command.parameters["temp"].value))  # D2
        self.assertEqual("programName", app.history_recovery["startProgram"])

    def test_the_read_list_is_the_one_a_dump_prints(self) -> None:
        app = self._at_setup()
        self._app_starts_dark()
        self._poll(app, 2)
        self.assertEqual(2, len(app.command_history))
        self.assertEqual("ok", app.command_history_refresh)

    def test_the_activity_alone_is_enough(self) -> None:
        # The cloud may have nulled `commandHistory` (issue #112).
        app = self._at_setup()
        self.cloud.context["commandHistory"] = None
        self._poll(app)
        self._app_starts_dark(command=False)
        self._poll(app, 2)
        self.assertEqual("iot_wash_dark", app.attributes["programName"])

    def test_the_command_history_alone_is_enough(self) -> None:
        app = self._at_setup()
        self._app_starts_dark(activity=False)
        self._poll(app, 2)
        self.assertEqual("iot_wash_dark", app.attributes["programName"])

    def test_both_signals_in_one_poll_cost_one_read(self) -> None:  # T2
        app = self._at_setup()
        reads = self.cloud.history_reads
        self._app_starts_dark()
        self._poll(app, 4)
        self.assertEqual(reads + 1, self.cloud.history_reads)

    def test_both_signals_in_two_polls_cost_one_read(self) -> None:  # T2
        app = self._at_setup()
        self.cloud.context["activity"] = {"activityStarted": "2026-10-02T23:54:20Z",
                                          "attributes": {}}
        self._poll(app)
        reads = self.cloud.history_reads
        self._app_starts_dark(activity=False)
        self._poll(app, 2)
        self.cloud.context["activity"] = {"activityStarted": _ACTIVITY_AT, "attributes": {}}
        self._poll(app, 3)
        self.assertEqual(reads + 1, self.cloud.history_reads)
        self.assertEqual("iot_wash_dark", app.attributes["programName"])

    def test_nothing_is_read_while_nothing_starts(self) -> None:
        app = self._at_setup()
        reads = self.cloud.history_reads
        self._poll(app, 3)
        self.assertEqual(reads, self.cloud.history_reads)

    def test_the_first_activity_seen_after_setup_is_not_a_new_cycle(self) -> None:
        # Setup has just read /history: the cycle already running when HA started is
        # the one that list describes, or one it cannot describe.
        context = _shadow("116") | {"commandHistory": None, "activity": {
            "activityStarted": _ACTIVITY_AT, "attributes": {}}}
        app = self._appliance([_start("PROGRAMS.WM_WD.HQD_SPIN", _SPIN_AT)], context)
        reads = self.cloud.history_reads
        self._poll(app, 3)
        self.assertEqual(reads, self.cloud.history_reads)

    def test_a_panel_start_changes_nothing(self) -> None:  # T1
        # A cycle started on the panel never reaches /history: the list read again
        # still ends with the spin, and the choices made since are kept.
        app = self._at_setup()
        command = self._start_command(app)
        command.parameters["temp"].value = "60"
        self.cloud.context["activity"] = {"activityStarted": _ACTIVITY_AT, "attributes": {}}
        self._poll(app)
        self.cloud.context["activity"] = {"activityStarted": "2026-10-06T15:00:00Z",
                                          "attributes": {}}
        self._poll(app, 3)
        self.assertIs(command, self._start_command(app))
        self.assertEqual("60", str(command.parameters["temp"].value))
        self.assertEqual("hqd_synthetic_and_coloured", app.attributes["programName"])

    def test_pause_stop_and_a_nulled_slot_are_not_new_cycles(self) -> None:  # T4
        for slot in (
            {"command": {"commandName": "pauseProgram", "timestamp": _DARK_AT,
                         "parameters": {"pause": "1"}}},
            {"command": {"commandName": "stopProgram", "timestamp": _DARK_AT,
                         "parameters": {}}},
            None,
            {},
        ):
            with self.subTest(slot=slot):
                app = self._at_setup()
                reads = self.cloud.history_reads
                self.cloud.context["commandHistory"] = slot
                self._poll(app, 2)
                self.assertEqual(reads, self.cloud.history_reads)

    def test_a_slot_restored_after_a_null_is_not_a_new_cycle(self) -> None:  # T4
        app = self._at_setup()
        self._app_starts_dark()
        self._poll(app, 2)
        reads = self.cloud.history_reads
        self.cloud.context["commandHistory"] = None
        self._poll(app)
        self.cloud.context["commandHistory"] = _start("PROGRAMS.WM_WD.IOT_WASH_DARK", _DARK_AT, "40")
        self._poll(app, 2)
        self.assertEqual(reads, self.cloud.history_reads)

    def test_a_failed_read_is_tried_three_times_in_all(self) -> None:
        app = self._at_setup()
        reads = self.cloud.history_reads
        self.cloud.history_error = TimeoutError()
        self._app_starts_dark()
        self._poll(app, 6)
        self.assertEqual(reads + 3, self.cloud.history_reads)
        self.assertEqual("TimeoutError", app.command_history_refresh)
        self.assertEqual("hqd_synthetic_and_coloured", app.attributes["programName"])

    def test_a_read_that_works_on_a_retry_is_applied(self) -> None:
        app = self._at_setup()
        self.cloud.history_error = TimeoutError()
        self._app_starts_dark()
        self._poll(app)
        self.cloud.history_error = None
        self._poll(app, 2)
        self.assertEqual("iot_wash_dark", app.attributes["programName"])

    def test_an_announced_start_not_listed_yet_is_waited_for(self) -> None:
        app = self._at_setup()
        reads = self.cloud.history_reads
        self._app_starts_dark()
        listed = self.cloud.history
        self.cloud.history = [_start("PROGRAMS.WM_WD.HQD_SPIN", _SPIN_AT)]  # lagging
        self._poll(app, 2)  # two reads, neither lists it
        self.cloud.history = listed
        self._poll(app, 2)  # the third does, the next poll applies it
        self.assertEqual(reads + 3, self.cloud.history_reads)
        self.assertEqual("iot_wash_dark", app.attributes["programName"])

    def test_an_announcement_given_up_is_not_taken_up_again(self) -> None:
        app = self._at_setup()
        reads = self.cloud.history_reads
        self._app_starts_dark()
        self.cloud.history = [_start("PROGRAMS.WM_WD.HQD_SPIN", _SPIN_AT)]  # never listed
        self._poll(app, 8)
        self.assertEqual(reads + 3, self.cloud.history_reads)

    def test_the_next_cycle_is_seen_by_its_activity_alone(self) -> None:
        # Two app starts with the slot nulled: the second activity is a new cycle too.
        app = self._at_setup()
        self.cloud.context["commandHistory"] = None
        self._poll(app)
        self._app_starts_dark(command=False)
        self._poll(app, 2)
        self.cloud.history.insert(0, _start("PROGRAMS.WM_WD.HQD_SPIN", "2026-10-06T16:00:00Z"))
        self.cloud.context["activity"] = {"activityStarted": "2026-10-06T16:00:04Z",
                                          "attributes": {}}
        self._poll(app, 2)
        self.assertEqual("PROGRAMS.WM_WD.HQD_SPIN", self._start_command(app).category)
        self.assertEqual(2, app.followed_starts)

    def test_a_second_start_announced_meanwhile_is_still_awaited(self) -> None:
        # The slot shows a second start while the read of the first is in flight: that
        # read cannot list it, so applying it must not end the wait for the second.
        app = self._at_setup()
        self.cloud.history_gate = asyncio.Event()
        self._app_starts_dark()
        self._poll(app)  # the read starts, listing dark only, and hangs
        second = _start("PROGRAMS.WM_WD.HQD_SPIN", "2026-10-06T13:47:30Z")
        self.cloud.context["commandHistory"] = second
        self._poll(app)  # the spin is announced while the read is in flight
        self.cloud.history.insert(0, second)
        self.cloud.history_gate.set()
        self.loop.run_until_complete(asyncio.sleep(0.01))
        self._poll(app, 3)
        self.assertEqual("PROGRAMS.WM_WD.HQD_SPIN", self._start_command(app).category)
        self.assertEqual(2, app.followed_starts)

    def test_only_the_start_is_recovered(self) -> None:
        # The load recovers every command; a new cycle concerns the start alone.
        app = self._at_setup()
        settings = app.commands["settings"]
        self._app_starts_dark()
        self.cloud.history.insert(0, {"command": {
            "commandName": "setParameters", "timestamp": "2026-10-06T13:47:00Z",
            "parameters": {"childLock": "1"}}})
        self._poll(app, 2)
        self.assertEqual("iot_wash_dark", app.attributes["programName"])
        self.assertIs(settings, app.commands["settings"])
        self.assertEqual("0", str(settings.parameters["childLock"].value))

    def test_each_followed_start_is_counted(self) -> None:
        # What the integration watches to drop the choices made in HA and not started.
        app = self._at_setup()
        self.assertEqual(0, app.followed_starts)
        self._app_starts_dark()
        self._poll(app, 2)
        self.assertEqual(1, app.followed_starts)
        self._poll(app, 2)
        self.assertEqual(1, app.followed_starts)


class OtherTypesTest(_Harness):
    def test_the_dryer_and_the_dishwasher_are_followed(self) -> None:  # D4
        for kind in ("WD", "TD", "DW"):
            with self.subTest(kind=kind):
                self.TYPE = kind
                context = _shadow("116") | {"activity": {}, "commandHistory": None}
                app = self._appliance([_start("PROGRAMS.WM_WD.HQD_SPIN", _SPIN_AT)], context)
                reads = self.cloud.history_reads
                self.cloud.context["commandHistory"] = _start(
                    "PROGRAMS.WM_WD.IOT_WASH_DARK", _DARK_AT, "40")
                self._poll(app)
                self.assertEqual(reads + 1, self.cloud.history_reads)

    def test_the_other_types_are_not(self) -> None:
        for kind in ("REF", "AC", "HW", "OV"):
            with self.subTest(kind=kind):
                self.TYPE = kind
                context = _shadow("116") | {"activity": {}, "commandHistory": None}
                app = self._appliance([_start("PROGRAMS.WM_WD.HQD_SPIN", _SPIN_AT)], context)
                reads = self.cloud.history_reads
                self.cloud.context["commandHistory"] = _start(
                    "PROGRAMS.WM_WD.IOT_WASH_DARK", _DARK_AT, "40")
                self.cloud.context["activity"] = {"activityStarted": _ACTIVITY_AT}
                self._poll(app, 3)
                self.assertEqual(reads, self.cloud.history_reads)


class RecoveredProgrammeAndPrPositionTest(_Harness):
    """D5: the dishwasher's shadow reports a prPosition the catalog does not declare."""

    TYPE = "DW"
    COMMANDS = _DW_COMMANDS

    def _dw(self, history, position="1"):
        context = _shadow("8", position) | {"activity": {}, "commandHistory": None}
        return self._appliance(history, context)

    def test_a_programme_recovered_at_setup_is_named(self) -> None:
        app = self._dw([_start("PROGRAMS.DW.IOT_HAPPY_HOUR", _DARK_AT, pr_code="8")])
        self.assertEqual("iot_happy_hour", app.attributes["programName"])

    def test_a_programme_recovered_at_runtime_is_named(self) -> None:
        app = self._dw([_start("PROGRAMS.DW.RAPID_20", _SPIN_AT, pr_code="28")])
        self.assertEqual("eco", app.attributes["programName"])
        self.cloud.history = [_start("PROGRAMS.DW.IOT_HAPPY_HOUR", _DARK_AT, pr_code="8")]
        self.cloud.context["commandHistory"] = self.cloud.history[0]
        self._poll(app, 2)
        self.assertEqual("iot_happy_hour", app.attributes["programName"])

    def test_a_category_merely_selected_keeps_the_prposition_guard(self) -> None:
        # Only the recovery is trusted past the guard: a category set through the
        # programme parameter, never confirmed by /history, still has to agree.
        app = self._dw([])
        command = self._start_command(app)
        command.category = "iot_happy_hour"
        self.loop.run_until_complete(app.load_attributes())
        self.assertEqual("eco", app.attributes["programName"])

    def test_the_mark_does_not_survive_another_recovery(self) -> None:
        app = self._dw([_start("PROGRAMS.DW.IOT_HAPPY_HOUR", _DARK_AT, pr_code="8")])
        happy_hour = self._start_command(app)
        self.cloud.history = [_start("PROGRAMS.DW.RAPID_20", "2026-10-06T15:00:00Z", pr_code="28")]
        self.cloud.context["commandHistory"] = self.cloud.history[0]
        self._poll(app, 2)
        happy_hour.category = "iot_happy_hour"
        self.loop.run_until_complete(app.load_attributes())
        self.assertEqual("eco", app.attributes["programName"])


class NoReadAfterOurOwnCommandTest(_Harness):
    """T3: the read 10 s after an accepted start or stop is gone (`dd9c7fe`)."""

    def test_an_accepted_start_reads_nothing_by_itself(self) -> None:
        # The new cycle it starts is announced like any other: the poll reads then.
        context = _shadow("116") | {"activity": {}, "commandHistory": None}
        app = self._appliance([_start("PROGRAMS.WM_WD.HQD_SPIN", _SPIN_AT)], context)
        reads = self.cloud.history_reads
        # The removed read waited 10 s; zeroed, a survivor would show within the wait.
        app._HISTORY_REFRESH_DELAY = 0
        command = self._start_command(app)
        self.loop.run_until_complete(command.send())
        self.loop.run_until_complete(asyncio.sleep(0.05))
        self.assertEqual(reads, self.cloud.history_reads)
        self.assertIsNone(app.command_history_refresh)


if __name__ == "__main__":
    unittest.main()
