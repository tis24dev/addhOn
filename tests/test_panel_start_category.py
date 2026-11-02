# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A panel start points the active programme at the cycle's own category (issue #138).

A reporter's HQD washer, 2026-10-31: Cottons (prCode 115, 1400 rpm, 3 rinses) was
started on the panel while the last start Home Assistant knew of, from `/history`, was
Technical fabrics. A panel start never reaches `/history`, so the active category of
`startProgram` stayed on Technical fabrics: the option selects offered its lists
(the spin showed "unknown" at 1400, the rinses 1) and, at the end of the cycle, the
programme select fell back to it while the name sensor still said Cottons.

The app's washer dashboard rebuilds its current card from the shadow's prCode (the
first category of the `dashboard` family on that code) and lays the activity over it
-- `programName` and the start's values -- unless the activity's prCode contradicts
the shadow's (`apk2/analysis/issue138-beta3/15-app-programma-e-liste-dopo-avvio-pannello.md`
section 1, apk2 decomp.txt @2591529-2592418). Decision of 2026-11-02: as the app,
the active category follows the panel's programme, with the schema rebuilt and the
activity's values on it; `history_recovery` records it as `panel`; `/history` is not
read (T1 stands: it would list nothing new).
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import install_stubs  # noqa: E402

install_stubs()

from custom_components.addhon.client import factory  # noqa: E402
from custom_components.addhon.client.engine import command_loader  # noqa: E402
from test_follow_started_elsewhere import CloudDouble, _fixed  # noqa: E402

_STAMP = "2026-10-31T10:00:00Z"
_DENIM_AT = "2026-10-28T08:00:00.000Z"
_PANEL_AT = "2026-10-31T10:09:00Z"


def _enum(default, values):
    return {"typology": "enum", "category": "command", "mandatory": 0,
            "defaultValue": default, "enumValues": list(values)}


def _range(default, minimum, maximum, step="1"):
    return {"typology": "range", "category": "command", "mandatory": 0,
            "defaultValue": default, "minimumValue": minimum, "maximumValue": maximum,
            "incrementValue": step}


def _programme(pr_code, family=None):
    parameters = {
        "prCode": _fixed(pr_code),
        "temp": _range("30", "0", "90", "10"),
        "spinSpeed": _enum("1000", ["0", "400", "800", "1000", "1400"]),
        "delayTime": _range("0", "0", "1440", "30"),
    }
    if family is not None:
        parameters["programFamily"] = _fixed(family)
    return {"description": "d", "protocolType": "MQTT", "parameters": parameters}


# The programmes that matter on the reporter's washer: four on the shared 115 -- the
# dial's Cottons (`dashboard`), a hidden base programme listed after it, two
# downloaded ones -- and one on 124.
_WM_COMMANDS = {
    "applianceModel": {"options": {}},
    "startProgram": {
        "PROGRAMS.WM_WD.HQD_COTTONS": _programme("115", "[dashboard]"),
        "PROGRAMS.WM_WD.HQD_COTTONS_PLUS": _programme("115", "[hidden]"),
        "PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS": _programme("115"),
        "PROGRAMS.WM_WD.IOT_WASH_BED_LINEN": _programme("115"),
        "PROGRAMS.WM_WD.HQD_SMART": _programme("124", "[dashboard]"),
    },
    "dictionaryId": 1,
}


def _start(programme, stamp, pr_code="115", **parameters):
    """One `startProgram` as the app records it in `/history`."""
    return {"command": {
        "commandName": "startProgram",
        "programName": programme,
        "timestamp": stamp,
        "parameters": {"prCode": pr_code, "temp": "40", "spinSpeed": "800", **parameters},
    }}


def _parameter(value):
    return {"parNewVal": value, "lastUpdate": _STAMP}


def _shadow(pr_code, mach_mode):
    return {"shadow": {"parameters": {
        "prCode": _parameter(pr_code), "machMode": _parameter(mach_mode)}}}


def _activity(programme, channel="appliance", *, started=_PANEL_AT, pr_code="115", **extra):
    attributes = {"channel": channel, "origin": "dashboard", "prCode": pr_code, **extra}
    if programme is not None:
        attributes["programName"] = programme
    return {"activityStarted": started, "attributes": attributes}


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def _denim_last_started(self):
        """The app started Denim last; the washer stands idle on 115; one poll done."""
        self.cloud = CloudDouble(
            _WM_COMMANDS,
            [_start("PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS", _DENIM_AT)],
            _shadow("115", "1") | {"activity": {}, "commandHistory": None},
        )
        app = factory._native_engine_appliance_cls()(
            self.cloud,
            {"applianceTypeName": "WM", "applianceModelId": 1, "macAddress": "aa"},
            zone=0,
        )
        self.loop.run_until_complete(app.load_commands())
        self.loop.run_until_complete(app.load_attributes())
        self._poll(app)  # the first activity seen after setup is the baseline
        self.assertEqual("PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS", app.commands["startProgram"].category)
        return app

    def _poll(self, app, times=1, activity=None, **shadow):
        """Coordinator polls, after the activity and shadow values given."""
        if activity is not None:
            self.cloud.context["activity"] = activity
        for key, value in shadow.items():
            self.cloud.context["shadow"]["parameters"][key] = _parameter(value)
        for _ in range(times):
            self.loop.run_until_complete(app.update(force=True))
            self.loop.run_until_complete(asyncio.sleep(0.01))

    @staticmethod
    def _value(app, key):
        return str(app.commands["startProgram"].parameters[key].value)


class PanelStartTest(_Harness):
    """The case of the report: Cottons started on the panel over a Denim start."""

    def test_the_category_follows_the_panel_s_programme(self) -> None:
        app = self._denim_last_started()
        reads = self.cloud.history_reads
        self._poll(app, activity=_activity("HQD_COTTONS", spinSpeed=1400, temp="40", delayTime=30),
                   machMode="2")
        command = app.commands["startProgram"]
        self.assertEqual("PROGRAMS.WM_WD.HQD_COTTONS", command.category)
        self.assertEqual("panel", app.history_recovery["startProgram"])
        self.assertEqual("hqd_cottons", app.attributes["programName"])
        # The activity's values of the start's parameters go on the category...
        self.assertEqual("1400", self._value(app, "spinSpeed"))
        self.assertEqual("40", self._value(app, "temp"))
        # ...with the exclusions of a history recovery: the delay is zeroed.
        self.assertEqual("0", self._value(app, "delayTime"))
        # No read of /history (a panel start never reaches it), no start followed.
        self.assertEqual(reads, self.cloud.history_reads)
        self.assertEqual(0, app.followed_starts)

    def test_the_category_is_rebuilt_from_its_schema_first(self) -> None:
        app = self._denim_last_started()
        cottons = app.commands["startProgram"].categories["hqd_cottons"]
        cottons.parameters["temp"].value = "60"  # an earlier choice on the programme
        self._poll(app, activity=_activity("HQD_COTTONS", spinSpeed="1400"), machMode="2")
        self.assertIs(cottons, app.commands["startProgram"])
        self.assertEqual("1400", self._value(app, "spinSpeed"))
        self.assertEqual("30", self._value(app, "temp"))  # the schema's, as after a load

    def test_a_contradicted_activity_changes_nothing(self) -> None:
        # The app's own guard: the activity's prCode must be the shadow's.
        app = self._denim_last_started()
        self._poll(app, activity=_activity("HQD_COTTONS", pr_code="124", spinSpeed="1400"),
                   machMode="2")
        command = app.commands["startProgram"]
        self.assertEqual("PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS", command.category)
        self.assertEqual("programName", app.history_recovery["startProgram"])
        self.assertEqual("800", self._value(app, "spinSpeed"))

    def test_an_unknown_programme_falls_back_to_the_code_s_dashboard_programme(self) -> None:
        # The base programme of the shadow's prCode, the first of the `dashboard`
        # family (@2591908-2591914): not the hidden one listed after it.
        for programme in (None, "HQD_NOT_IN_THIS_CATALOG"):
            with self.subTest(programme=programme):
                app = self._denim_last_started()
                self._poll(app, activity=_activity(programme, spinSpeed="1400"), machMode="2")
                self.assertEqual("PROGRAMS.WM_WD.HQD_COTTONS", app.commands["startProgram"].category)
                self.assertEqual("panel", app.history_recovery["startProgram"])
                self.assertEqual("1400", self._value(app, "spinSpeed"))

    def test_the_programme_the_activity_names_is_taken_over_the_code_s_base(self) -> None:
        # The activity spells the bare name (`IOT_WASH_BED_LINEN`), the categories are
        # keyed as `_clean_name` cleans a dotted one (`iot_wash_bed_linen`): matched as
        # `HonParameterProgram._category_key` reads an activity's. Bed linen shares 115
        # with Cottons, the code's base programme: the name decides, not the code.
        for programme, expected in (
            ("IOT_WASH_BED_LINEN", "PROGRAMS.WM_WD.IOT_WASH_BED_LINEN"),
            ("HQD_COTTONS_PLUS", "PROGRAMS.WM_WD.HQD_COTTONS_PLUS"),  # hidden, not the base
        ):
            with self.subTest(programme=programme):
                app = self._denim_last_started()
                self._poll(app, activity=_activity(programme, spinSpeed="1400"), machMode="2")
                self.assertEqual(expected, app.commands["startProgram"].category)
                self.assertEqual("panel", app.history_recovery["startProgram"])
                self.assertEqual("1400", self._value(app, "spinSpeed"))

    def test_the_code_s_base_programme_is_taken_by_name_not_by_fallback(self) -> None:
        # Cottons is both what the activity names and the base of 115: the name branch
        # must answer, so a programme the code lookup cannot tell apart still lands.
        # The programme parameter's `ids` is emptied: no fallback is left to hide behind.
        app = self._denim_last_started()
        program = app.commands["startProgram"].parameters["program"]
        with patch.object(type(program), "ids", new_callable=lambda: property(lambda self: {})):
            self._poll(app, activity=_activity("HQD_COTTONS", spinSpeed="1400"), machMode="2")
        self.assertEqual("PROGRAMS.WM_WD.HQD_COTTONS", app.commands["startProgram"].category)
        self.assertEqual("panel", app.history_recovery["startProgram"])

    def test_a_programme_on_another_code_is_not_taken(self) -> None:
        # The name the activity carries is not the shadow's code (decision 4 of
        # 2026-10-25 for the name): the code's own programme is taken instead.
        app = self._denim_last_started()
        self._poll(app, activity=_activity("HQD_SMART", spinSpeed="1400"), machMode="2")
        self.assertEqual("PROGRAMS.WM_WD.HQD_COTTONS", app.commands["startProgram"].category)

    def test_an_activity_read_at_rest_waits_for_the_cycle(self) -> None:
        # Decision 6 of 2026-10-25: the activity counts only while the shadow's
        # machMode says a cycle runs; the next poll that says so applies it.
        app = self._denim_last_started()
        self._poll(app, activity=_activity("HQD_COTTONS", spinSpeed="1400"), machMode="1")
        self.assertEqual("PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS", app.commands["startProgram"].category)
        self._poll(app, machMode="2")
        self.assertEqual("PROGRAMS.WM_WD.HQD_COTTONS", app.commands["startProgram"].category)
        self.assertEqual("1400", self._value(app, "spinSpeed"))

    def test_a_failure_while_pointing_does_not_break_the_poll(self) -> None:
        app = self._denim_last_started()
        with patch.object(
            command_loader.HonCommandLoader, "recover_panel_start", side_effect=RuntimeError("x")
        ):
            self._poll(app, activity=_activity("HQD_COTTONS", spinSpeed="1400"), machMode="2")
        self.assertEqual("PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS", app.commands["startProgram"].category)
        self.assertEqual("2", str(app.attributes["parameters"]["machMode"]))
        self.assertEqual("hqd_cottons", app.attributes["programName"])

    def test_an_app_start_is_still_followed_through_history(self) -> None:
        # `channel mobileApp`: the follow of today, unchanged -- one read of /history,
        # the row's category and values.
        app = self._denim_last_started()
        reads = self.cloud.history_reads
        self.cloud.history.insert(
            0, _start("PROGRAMS.WM_WD.IOT_WASH_BED_LINEN", "2026-10-31T10:08:55.000Z",
                      spinSpeed="400"))
        self._poll(app, 2, activity=_activity("IOT_WASH_BED_LINEN", "mobileApp",
                                              spinSpeed="1400"), machMode="2")
        command = app.commands["startProgram"]
        self.assertEqual("PROGRAMS.WM_WD.IOT_WASH_BED_LINEN", command.category)
        self.assertEqual("programName", app.history_recovery["startProgram"])
        self.assertEqual("400", self._value(app, "spinSpeed"))
        self.assertEqual(reads + 1, self.cloud.history_reads)
        self.assertEqual(1, app.followed_starts)


_APP_AT = "2026-10-31T10:08:56Z"
_BED_LINEN_ROW_AT = "2026-10-31T10:08:55.000Z"
_LATER_PANEL_AT = "2026-10-31T10:12:00Z"


class PanelStartOverPendingFollowTest(_Harness):
    """A panel start while an app start's follow of `/history` is still pending.

    The app started Bed linen, its row was still to be read (or read and not yet
    listed), and Cottons was started on the panel: the panel's cycle is the newest,
    so what the follow brings afterwards must not replace it. The follow ends with
    the panel start, a read still in flight is dropped, and no start is counted.
    """

    def _app_started_bed_linen(self, app):
        self._poll(app, activity=_activity("IOT_WASH_BED_LINEN", "mobileApp", started=_APP_AT,
                                           spinSpeed="1400"), machMode="2")

    def _panel_started_cottons(self, app):
        self._poll(app, activity=_activity("HQD_COTTONS", started=_LATER_PANEL_AT,
                                           spinSpeed="1400"), machMode="2")
        self.assertEqual("PROGRAMS.WM_WD.HQD_COTTONS", app.commands["startProgram"].category)
        self.assertEqual("panel", app.history_recovery["startProgram"])

    def _assert_cottons_stays(self, app):
        self.assertEqual("PROGRAMS.WM_WD.HQD_COTTONS", app.commands["startProgram"].category)
        self.assertEqual("panel", app.history_recovery["startProgram"])
        self.assertEqual("1400", self._value(app, "spinSpeed"))
        self.assertEqual(0, app.followed_starts)

    def test_a_read_in_flight_across_the_panel_start_is_dropped(self) -> None:
        app = self._denim_last_started()
        self.cloud.history.insert(
            0, _start("PROGRAMS.WM_WD.IOT_WASH_BED_LINEN", _BED_LINEN_ROW_AT, spinSpeed="400"))
        self.cloud.history_gate = asyncio.Event()  # the read waits
        self._app_started_bed_linen(app)
        self.assertFalse(app._follow_task.done())
        self._panel_started_cottons(app)
        self.cloud.history_gate.set()
        self.loop.run_until_complete(asyncio.sleep(0.01))
        self._poll(app)
        self._assert_cottons_stays(app)

    def test_a_row_the_cloud_lists_late_is_not_applied(self) -> None:
        app = self._denim_last_started()
        self._app_started_bed_linen(app)  # the list has no row of it yet
        self.assertEqual("IOT_WASH_BED_LINEN", app._awaited_programme)
        self._panel_started_cottons(app)
        reads = self.cloud.history_reads
        self.cloud.history.insert(
            0, _start("PROGRAMS.WM_WD.IOT_WASH_BED_LINEN", _BED_LINEN_ROW_AT, spinSpeed="400"))
        self._poll(app, 2)
        self._assert_cottons_stays(app)
        self.assertEqual(reads, self.cloud.history_reads)  # the follow ended, no more reads

    def test_a_start_the_slot_announced_is_dropped_too(self) -> None:
        app = self._denim_last_started()
        self.cloud.history_gate = asyncio.Event()
        self.cloud.history.insert(
            0, _start("PROGRAMS.WM_WD.IOT_WASH_BED_LINEN", _BED_LINEN_ROW_AT, spinSpeed="400"))
        self.cloud.context["commandHistory"] = {"command": {
            "commandName": "startProgram", "timestamp": _BED_LINEN_ROW_AT,
            "programName": "PROGRAMS.WM_WD.IOT_WASH_BED_LINEN", "parameters": {"prCode": "115"}}}
        self._poll(app, machMode="2")
        self.assertIsNotNone(app._awaited_start)
        self._panel_started_cottons(app)
        self.cloud.history_gate.set()
        self.loop.run_until_complete(asyncio.sleep(0.01))
        self._poll(app)
        self._assert_cottons_stays(app)


if __name__ == "__main__":
    unittest.main()
