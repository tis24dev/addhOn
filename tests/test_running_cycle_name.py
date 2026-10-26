# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The running programme is named by the cloud's activity (issue #130).

Lajahome's washer (HW90-B14387TU1-S), 2026-10-16: the app had last started Denim
(`IOT_WASH_DENIM_JEANS`, prCode 115), the dial was turned through nine programmes and
Cottons, also 115, was started on the panel. Home Assistant named Denim for the whole
cycle, though the context's activity said `HQD_COTTONS`, channel `appliance`. A panel
start never reaches `/history` (T1), and prCode 115 is shared by Cottons, Perfect White,
Denim and Bed linen. The decisions of 2026-10-25:

- 2: the activity names the running programme wherever the cycle was started, as the
  app's `mapLastActivity` does (apk2 decomp.txt @1551831), on the four followed types;
  the dishwasher's activity may name it by `functionalId` alone; a selected favourite of
  the programme named keeps its name;
- 4: an activity from the panel that names nothing usable (no name, a name the catalog
  lacks, a programme on another code) leaves the code's own programme, not the category
  the app or Home Assistant started last;
- 6: the activity counts only while the shadow's `machMode` says a cycle runs (2, 3, 5,
  8; the dishwasher 4 too), as the app reads it (@3398666-3398745);
- 8: a programme the activity names skips the prPosition check, as D5;
- S1: when the cycle ends the programme that ran stays named while the shadow keeps its
  code.

The activity shapes are the dumps' (diagnostics/issue112-dumps, issue130-dumps, a live
read on mik's air conditioner on 2026-10-25): `activityStarted` in the cloud's own
format, the programme as the bare name, `channel` and `origin` in `attributes`.
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
from test_follow_started_elsewhere import CloudDouble, _fixed, _programme  # noqa: E402

_STAMP = "2026-10-16T12:20:00Z"
_DENIM_AT = "2026-10-15T08:00:00.000Z"
_COTTONS_AT = "2026-10-16T12:25:03Z"

# Lajahome's programmes that matter: four on the shared 115, one each on 124 and 136.
_WM_COMMANDS = {
    "applianceModel": {"options": {}},
    "startProgram": {
        "PROGRAMS.WM_WD.HQD_COTTONS": _programme("115"),
        "PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS": _programme("115"),
        "PROGRAMS.WM_WD.IOT_WASH_BED_LINEN": _programme("115"),
        "PROGRAMS.WM_WD.IOT_WASH_PERFECT_WHITE": _programme("115"),
        "PROGRAMS.WM_WD.HQD_SMART": _programme("124"),
        "PROGRAMS.WM_WD.HQD_ECO_40_60": _programme("136"),
    },
    "dictionaryId": 1,
}


def _dishwasher_programme(pr_code, position, functional_id):
    category = _programme(pr_code, position)
    category["ancillaryParameters"] = {"functionalId": _fixed(functional_id)}
    return category


# bbosson's dishwasher: eco and a downloaded programme share prCode 8 and dial slot 3.
_DW_COMMANDS = {
    "applianceModel": {"options": {}},
    "startProgram": {
        "PROGRAMS.DW.ECO": _dishwasher_programme("8", "3", "2"),
        "PROGRAMS.DW.IOT_HAPPY_HOUR": _dishwasher_programme("8", "3", "41"),
        "PROGRAMS.DW.RAPID_20": _dishwasher_programme("28", "12", "7"),
    },
    "dictionaryId": 1,
}


def _start(programme, stamp, pr_code="115", label=None):
    """One `startProgram` as the app records it in `/history`."""
    command = {
        "commandName": "startProgram",
        "programName": programme,
        "timestamp": stamp,
        "parameters": {"prCode": pr_code, "temp": "40"},
    }
    if label is not None:
        command["attributes"] = {"prStr": label, "channel": "mobileApp"}
    return {"command": command}


def _parameter(value):
    return {"parNewVal": value, "lastUpdate": _STAMP}


def _shadow(pr_code, mach_mode, position=None):
    parameters = {"prCode": _parameter(pr_code), "machMode": _parameter(mach_mode)}
    if position is not None:
        parameters["prPosition"] = _parameter(position)
    return {"shadow": {"parameters": parameters}}


def _activity(programme, channel, *, started=_COTTONS_AT, pr_code="115", **extra):
    attributes = {"channel": channel, "origin": "dashboard", "prCode": pr_code, **extra}
    if programme is not None:
        attributes["programName"] = programme
    return {"activityStarted": started, "attributes": attributes}


class _FavouritesCloud(CloudDouble):
    def __init__(self, commands, history, context, favourites) -> None:
        super().__init__(commands, history, context)
        self.favourites = favourites

    async def load_favourites(self, appliance):
        return json.loads(json.dumps(self.favourites))


class _Harness(unittest.TestCase):
    TYPE = "WM"
    COMMANDS = _WM_COMMANDS

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def _appliance(self, history, context, favourites=()):
        self.cloud = _FavouritesCloud(self.COMMANDS, history, context, list(favourites))
        app = factory._native_engine_appliance_cls()(
            self.cloud,
            {"applianceTypeName": self.TYPE, "applianceModelId": 1, "macAddress": "aa"},
            zone=0,
        )
        self.loop.run_until_complete(app.load_commands())
        self.loop.run_until_complete(app.load_attributes())
        return app

    def _read(self, app, context_change=None, **shadow):
        """One read of the context, after `context_change` and the shadow values given."""
        if context_change is not None:
            self.cloud.context.update(context_change)
        for key, value in shadow.items():
            self.cloud.context["shadow"]["parameters"][key] = _parameter(value)
        self.loop.run_until_complete(app.load_attributes())
        return app.attributes["programName"]

    @staticmethod
    def _push(app, **shadow):
        """What the MQTT transport does with a push: the parameters, then the name."""
        for key, value in shadow.items():
            app.attributes["parameters"][key].update({"parName": key, "parNewVal": value})
        app.derive_attributes()
        return app.attributes["programName"]

    def _denim_last_started(self, mach_mode="1", **context):
        """The app started Denim last; the washer stands on 115."""
        return self._appliance(
            [_start("PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS", _DENIM_AT)],
            _shadow("115", mach_mode) | {"activity": {}, "commandHistory": None} | context,
        )


class PanelStartOnASharedCodeTest(_Harness):
    """The case of the report."""

    def test_before_the_cycle_the_last_start_is_named(self) -> None:
        self.assertEqual("iot_wash_denim_jeans", self._denim_last_started().attributes["programName"])

    def test_the_panel_cycle_is_named_by_its_activity(self) -> None:
        app = self._denim_last_started()
        name = self._read(app, {"activity": _activity("HQD_COTTONS", "appliance")}, machMode="2")
        self.assertEqual("hqd_cottons", name)

    def test_the_selection_and_its_options_are_left_alone(self) -> None:  # T1
        app = self._denim_last_started()
        command = app.commands["startProgram"]
        self._read(app, {"activity": _activity("HQD_COTTONS", "appliance")}, machMode="2")
        self.assertIs(command, app.commands["startProgram"])
        self.assertEqual("PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS", command.category)
        self.assertEqual("40", str(command.parameters["temp"].value))

    def test_a_push_keeps_the_name_the_read_found(self) -> None:
        app = self._denim_last_started()
        self._read(app, {"activity": _activity("HQD_COTTONS", "appliance")}, machMode="2")
        self.assertEqual("hqd_cottons", self._push(app, machMode="3"))  # paused
        self.assertEqual("hqd_cottons", self._push(app, machMode="2"))

    def test_the_programme_that_ran_stays_named_at_the_end(self) -> None:  # S1
        app = self._denim_last_started()
        self._read(app, {"activity": _activity("HQD_COTTONS", "appliance")}, machMode="2")
        # The end: machMode 7, then the activity empties, the shadow still on 115.
        self.assertEqual("hqd_cottons", self._push(app, machMode="7"))
        self.assertEqual("hqd_cottons", self._read(app, {"activity": {}}))
        # The resting code (136 on Lajahome's washer), first pushed, then read.
        self.assertEqual("hqd_eco_40_60", self._push(app, prCode="136", machMode="1"))
        self.assertEqual("hqd_eco_40_60", self._read(app, prCode="136", machMode="1"))
        self.assertIsNone(app.cycle_activity)
        # Back on 115 at rest: the cycle is gone, the last start names the code again.
        self.assertEqual("iot_wash_denim_jeans", self._read(app, prCode="115"))

    def test_a_push_off_the_code_does_not_drop_the_cycle(self) -> None:
        # Only a read drops it: a push to another code only does not apply it there.
        app = self._denim_last_started()
        self._read(app, {"activity": _activity("HQD_COTTONS", "appliance")}, machMode="2")
        self.assertEqual("hqd_smart", self._push(app, prCode="124"))
        self.assertEqual("hqd_cottons", self._push(app, prCode="115"))

    def test_the_next_cycle_replaces_it(self) -> None:
        app = self._denim_last_started()
        self._read(app, {"activity": _activity("HQD_COTTONS", "appliance")}, machMode="2")
        self._read(app, {"activity": {}}, machMode="7")
        name = self._read(
            app,
            {"activity": _activity(
                "IOT_WASH_PERFECT_WHITE", "mobileApp", started="2026-10-16T14:00:00Z")},
            machMode="2",
        )
        self.assertEqual("iot_wash_perfect_white", name)

    def test_a_context_without_an_activity_key_keeps_the_cycle(self) -> None:
        app = self._denim_last_started()
        self._read(app, {"activity": _activity("HQD_COTTONS", "appliance")}, machMode="2")
        self.cloud.context.pop("activity")
        self.assertEqual("hqd_cottons", self._read(app))


class AppStartNamedByItsActivityTest(_Harness):
    """Decision 2: the app's own starts are named by the activity too."""

    def test_an_app_start_is_named_at_the_first_read(self) -> None:
        # Lajahome, 2026-10-18 on v5.27.1: Bed linen showed as Cottons until the
        # second poll, when the recovery from /history caught up.
        app = self._appliance([], _shadow("115", "1") | {"activity": {}, "commandHistory": None})
        self.assertEqual("hqd_cottons", app.attributes["programName"])
        name = self._read(
            app, {"activity": _activity("IOT_WASH_BED_LINEN", "mobileApp")}, machMode="2"
        )
        self.assertEqual("iot_wash_bed_linen", name)

    def test_a_prefixed_programme_name_is_cut_like_a_category_key(self) -> None:
        app = self._denim_last_started()
        name = self._read(
            app,
            {"activity": _activity("PROGRAMS.WM_WD.IOT_WASH_BED_LINEN", "mobileApp")},
            machMode="5",  # a delayed start
        )
        self.assertEqual("iot_wash_bed_linen", name)

    def test_an_unusable_app_activity_falls_through_to_the_last_start(self) -> None:
        for programme in (None, "IOT_WASH_NOT_IN_THIS_CATALOG", "HQD_SMART"):
            with self.subTest(programme=programme):
                app = self._denim_last_started()
                name = self._read(
                    app, {"activity": _activity(programme, "mobileApp")}, machMode="2"
                )
                self.assertEqual("iot_wash_denim_jeans", name)


class UnusablePanelActivityTest(_Harness):
    """Decision 4: the panel started it, so the last start is not what runs."""

    def test_the_code_s_own_programme_is_named(self) -> None:
        cases = {
            "no programme": None,
            "not in this catalog": "HQD_NOT_IN_THIS_CATALOG",
            "on another code": "HQD_SMART",
        }
        for case, programme in cases.items():
            with self.subTest(case=case):
                app = self._denim_last_started()
                name = self._read(
                    app, {"activity": _activity(programme, "appliance")}, machMode="2"
                )
                self.assertEqual("hqd_cottons", name)


class CycleModesTest(_Harness):
    """Decision 6: the activity counts only while the shadow says a cycle runs."""

    def test_an_activity_read_at_rest_is_not_the_cycle(self) -> None:
        for mode in ("0", "1", "4", "6", "7", "9"):
            with self.subTest(machMode=mode):
                app = self._denim_last_started(mach_mode=mode)
                name = self._read(app, {"activity": _activity("HQD_COTTONS", "appliance")})
                self.assertEqual("iot_wash_denim_jeans", name)
                self.assertIsNone(app.cycle_activity)

    def test_an_ended_activity_is_not_the_cycle(self) -> None:
        app = self._denim_last_started()
        activity = _activity("HQD_COTTONS", "appliance") | {"activityEnded": "2026-10-16T13:30:00Z"}
        self.assertEqual("iot_wash_denim_jeans", self._read(app, {"activity": activity}, machMode="2"))

    def test_without_machmode_nothing_changes(self) -> None:
        app = self._denim_last_started()
        del self.cloud.context["shadow"]["parameters"]["machMode"]
        app.attributes["parameters"].pop("machMode")
        self.assertEqual(
            "iot_wash_denim_jeans",
            self._read(app, {"activity": _activity("HQD_COTTONS", "appliance")}),
        )


class FavouriteOfTheNamedProgrammeTest(_Harness):
    """Decision 2: a selected favourite of the programme the activity names keeps its name."""

    _FAVOURITES = [{
        "favouriteName": "My jeans",
        "command": {
            "commandName": "startProgram",
            "programName": "PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS",
            "parameters": {"temp": "30"},
        },
    }]

    def _favourite_selected(self):
        app = self._appliance(
            [], _shadow("115", "1") | {"activity": {}, "commandHistory": None}, self._FAVOURITES
        )
        app.settings["startProgram.program"].value = "My jeans"
        return app

    def test_the_favourite_keeps_its_name(self) -> None:
        app = self._favourite_selected()
        name = self._read(
            app, {"activity": _activity("IOT_WASH_DENIM_JEANS", "mobileApp")}, machMode="2"
        )
        self.assertEqual("My jeans", name)

    def test_another_programme_is_named_over_it(self) -> None:
        app = self._favourite_selected()
        name = self._read(app, {"activity": _activity("HQD_COTTONS", "appliance")}, machMode="2")
        self.assertEqual("hqd_cottons", name)


class FavouriteRecognisedByItsTitleTest(_Harness):
    """Decision 7: a favourite's start carries its name as the title.

    paps42's favourite starts (diagnostics/issue112-dumps/paps42-beta7-entry-diag.json):
    `/history` records the base programme as `programName` and the favourite's name as
    `attributes.prStr`; the activity echoes it as `prStrDisp`. Home Assistant sends the
    favourite's name as `prStr` too.
    """

    _FAVOURITES = [
        {"favouriteName": "My jeans", "command": {
            "commandName": "startProgram",
            "programName": "PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS",
            "parameters": {"temp": "30"}}},
        # Same name, another programme: not the favourite of Denim.
        {"favouriteName": "Sheets", "command": {
            "commandName": "startProgram",
            "programName": "PROGRAMS.WM_WD.IOT_WASH_BED_LINEN",
            "parameters": {"temp": "60"}}},
    ]

    def _setup_after(self, label):
        return self._appliance(
            [_start("PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS", _DENIM_AT, label=label)],
            _shadow("115", "1") | {"activity": {}, "commandHistory": None},
            self._FAVOURITES,
        )

    def test_a_favourite_start_is_recovered_as_the_favourite(self) -> None:
        app = self._setup_after("My jeans")
        self.assertEqual("My jeans", app.attributes["programName"])
        self.assertEqual("favourite", app.history_recovery["startProgram"])
        # The row's options go on the favourite, as on any recovered start (D2).
        self.assertEqual("40", str(app.commands["startProgram"].parameters["temp"].value))

    def test_a_title_that_names_no_favourite_of_it_recovers_the_programme(self) -> None:
        for label in ("Farmer - Nadrág", "Sheets", "", None):
            with self.subTest(label=label):
                app = self._setup_after(label)
                self.assertEqual("iot_wash_denim_jeans", app.attributes["programName"])
                self.assertEqual("programName", app.history_recovery["startProgram"])

    def test_a_favourite_start_followed_at_runtime_selects_the_favourite(self) -> None:
        app = self._setup_after(None)
        self.cloud.history = [
            _start("PROGRAMS.WM_WD.IOT_WASH_DENIM_JEANS", "2026-10-16T12:25:00.000Z",
                   label="My jeans"),
            *self.cloud.history,
        ]
        self.cloud.context["commandHistory"] = self.cloud.history[0]
        for _ in range(2):  # sees the new start, then applies the list read
            self.loop.run_until_complete(app.update(force=True))
            self.loop.run_until_complete(asyncio.sleep(0.01))
        self.assertEqual("My jeans", app.settings["startProgram.program"].value)
        self.assertEqual("favourite", app.history_recovery["startProgram"])

    def test_the_activity_s_title_names_the_favourite(self) -> None:
        app = self._setup_after(None)
        for title in ("My jeans", "My%20jeans"):  # the app URI-decodes `prStrDisp`
            with self.subTest(title=title):
                name = self._read(
                    app,
                    {"activity": _activity(
                        "IOT_WASH_DENIM_JEANS", "mobileApp", prStrDisp=title)},
                    machMode="2",
                )
                self.assertEqual("My jeans", name)

    def test_the_activity_s_title_of_another_programme_s_favourite_is_ignored(self) -> None:
        app = self._setup_after(None)
        name = self._read(
            app,
            {"activity": _activity("IOT_WASH_DENIM_JEANS", "mobileApp", prStrDisp="Sheets")},
            machMode="2",
        )
        self.assertEqual("iot_wash_denim_jeans", name)


class DishwasherTest(_Harness):
    """The dishwasher: `functionalId`, machMode 4 and its shadow's prPosition."""

    TYPE = "DW"
    COMMANDS = _DW_COMMANDS

    def _eco_at_rest(self):
        # The shadow's prPosition is not the catalog's (2 dumps out of 2, D5).
        return self._appliance(
            [], _shadow("8", "1", position="1") | {"activity": {}, "commandHistory": None}
        )

    def test_a_functional_id_alone_names_the_programme(self) -> None:
        app = self._eco_at_rest()
        self.assertEqual("eco", app.attributes["programName"])
        name = self._read(
            app,
            {"activity": _activity(None, "appliance", pr_code="8", functionalId="41")},
            machMode="2",
        )
        self.assertEqual("iot_happy_hour", name)

    def test_a_programme_name_comes_before_the_functional_id(self) -> None:
        app = self._eco_at_rest()
        name = self._read(
            app,
            {"activity": _activity("IOT_HAPPY_HOUR", "mobileApp", pr_code="8", functionalId="2")},
            machMode="2",
        )
        self.assertEqual("iot_happy_hour", name)

    def test_machmode_4_is_a_cycle_on_the_dishwasher_only(self) -> None:
        app = self._eco_at_rest()
        name = self._read(
            app, {"activity": _activity("IOT_HAPPY_HOUR", "appliance", pr_code="8")}, machMode="4"
        )
        self.assertEqual("iot_happy_hour", name)


class OtherTypesTest(_Harness):
    def test_the_types_not_followed_ignore_the_activity(self) -> None:
        for kind in ("OV", "REF", "WH"):
            with self.subTest(kind=kind):
                self.TYPE = kind
                app = self._denim_last_started()
                name = self._read(app, {"activity": _activity("HQD_COTTONS", "appliance")}, machMode="2")
                self.assertEqual("iot_wash_denim_jeans", name)
                self.assertIsNone(app.cycle_activity)

    def test_a_functional_id_counts_on_the_dishwasher_alone(self) -> None:
        app = self._denim_last_started()
        self._read(
            app, {"activity": _activity(None, "mobileApp", functionalId="41")}, machMode="2"
        )
        self.assertIsNone(app.cycle_activity.functional_id)


if __name__ == "__main__":
    unittest.main()
