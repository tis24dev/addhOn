# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Golden + behavioral test of the native per-type layer (Phase 4). It used to be
differential vs pyhOn; with `_vendor/` deleted it is golden (native output proven
== pyhOn at checkpoint 5a) + pins for the app-priority FIXES (modeZ/pause/wh-active
by value; dryLevel '0'/'11'; `available`) and for the registry.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import frozen, install_stubs, normalize  # noqa: E402

install_stubs()

from custom_components.addhon.client.engine.attributes import HonAttribute  # noqa: E402
from custom_components.addhon.client.engine.parameter.fixed import HonParameterFixed as NaFixed  # noqa: E402
from custom_components.addhon.client.engine.appliances import registry as native_registry  # noqa: E402


class FakeParent:
    def __init__(self, connection=True, settings=None, appliance_type="TD") -> None:
        self.connection = connection
        self.settings = settings or {}
        self.appliance_type = appliance_type


def _params(d: dict) -> dict:
    return {k: HonAttribute({"parNewVal": v}) for k, v in d.items()}


def _val(params: dict, key: str):
    return params[key].value if key in params else None


def _na(name):
    mod = __import__(
        f"custom_components.addhon.client.engine.appliances.{name}", fromlist=["Appliance"]
    )
    return mod.Appliance


def _run(name, params_dict, *, connection=True, last_conn=None, activity=None):
    parent = FakeParent(connection=connection, appliance_type=name.upper())
    params = _params(params_dict)
    data = {"parameters": params}
    if last_conn is not None:
        data["lastConnEvent"] = {"category": last_conn}
    if activity is not None:
        data["activity"] = activity
    out = _na(name)(parent).attributes(data)
    return out, params


def _snap(name, params_dict, **kw):
    out, params = _run(name, params_dict, **kw)
    return {
        "active": out.get("active"), "pause": out.get("pause"),
        "modeZ1": out.get("modeZ1"), "modeZ2": out.get("modeZ2"),
        "programName": out.get("programName"), "available": out.get("available"),
        "machMode": _val(params, "machMode"), "onOffStatus": _val(params, "onOffStatus"),
        "temp": _val(params, "temp"),
    }


def _native_snapshot():
    return {
        "td_online": _snap("td", {"machMode": "3"}, activity={"x": 1}),
        "td_offline": _snap("td", {"machMode": "5"}, connection=False, activity={}),
        "wm_disc": _snap("wm", {"machMode": "3"}, connection=False, activity={}),
        "dw": _snap("dw", {"machMode": "2"}, activity={"x": 1}),
        "ov_on": _snap("ov", {"onOffStatus": "1", "temp": "50"}),
        "ov_off": _snap("ov", {"onOffStatus": "1", "temp": "50", "remoteCtrValid": "1", "remainingTimeMM": "30"}, connection=False),
        "wh": _snap("wh", {"onOffStatus": "1"}),
        "ref_holiday": _snap("ref", {"holidayMode": "1", "intelligenceMode": "0", "quickModeZ1": "0", "quickModeZ2": "0"}),
        "ref_freeze": _snap("ref", {"holidayMode": "0", "intelligenceMode": "0", "quickModeZ1": "0", "quickModeZ2": "1"}),
        "ref_autoset": _snap("ref", {"holidayMode": "0", "intelligenceMode": "1", "quickModeZ1": "0", "quickModeZ2": "0"}),
        "ref_both_z1": _snap("ref", {"holidayMode": "1", "intelligenceMode": "0", "quickModeZ1": "1", "quickModeZ2": "0"}),
        "ref_freeze_vs_autoset": _snap("ref", {"holidayMode": "0", "intelligenceMode": "1", "quickModeZ1": "0", "quickModeZ2": "1"}),
        "ref_off": _snap("ref", {"holidayMode": "0", "intelligenceMode": "0", "quickModeZ1": "0", "quickModeZ2": "0"}),
    }


class PerTypeGoldenTest(unittest.TestCase):
    def test_native_matches_golden(self) -> None:
        snap = _native_snapshot()
        self.assertEqual(normalize(snap), frozen("engine_appliances", snap))


class NativeFixesPinTest(unittest.TestCase):
    """Explicit pin of the app-priority FIXES (new code, no pyhOn bug)."""

    def test_ref_modes_by_value(self) -> None:
        s = _native_snapshot()
        self.assertEqual(s["ref_holiday"]["modeZ1"], "holiday")
        self.assertEqual(s["ref_freeze"]["modeZ2"], "super_freeze")
        self.assertEqual((s["ref_autoset"]["modeZ1"], s["ref_autoset"]["modeZ2"]), ("auto_set", "auto_set"))
        self.assertEqual(s["ref_both_z1"]["modeZ1"], "holiday")           # Z1 priority
        self.assertEqual(s["ref_freeze_vs_autoset"]["modeZ2"], "super_freeze")  # Z2 priority
        self.assertEqual((s["ref_off"]["modeZ1"], s["ref_off"]["modeZ2"]), ("no_mode", "no_mode"))

    def test_pause_by_value(self) -> None:
        self.assertTrue(_native_snapshot()["td_online"]["pause"])
        self.assertTrue(_run("wm", {"machMode": "3"}, last_conn="CONNECTED", activity={"x": 1})[0]["pause"])

    def test_wh_active_by_value(self) -> None:
        self.assertTrue(_native_snapshot()["wh"]["active"])

    def test_available_tracks_connection(self) -> None:
        self.assertTrue(_run("td", {"machMode": "2"})[0]["available"])
        self.assertFalse(_run("td", {"machMode": "2"}, connection=False)[0]["available"])


class RegistryTest(unittest.TestCase):
    def test_known_types_mapped(self) -> None:
        for t in ("ref", "td", "wm", "wd", "dw", "ov", "wh", "wc"):
            extra = native_registry.get_extra(FakeParent(appliance_type=t.upper()))
            self.assertIsNotNone(extra, t)
            self.assertEqual(extra.__module__.rsplit(".", 1)[-1], t)

    def test_unknown_type_none(self) -> None:
        self.assertIsNone(native_registry.get_extra(FakeParent(appliance_type="XYZ")))

    def test_case_insensitive(self) -> None:
        self.assertIsNotNone(native_registry.get_extra(FakeParent(appliance_type="Ref")))


class SettingsDryLevelTest(unittest.TestCase):
    def _settings(self, value):
        s = {"startProgram.dryLevel": NaFixed("dryLevel", {"fixedValue": value}, "g"), "keep": 1}
        _na("td")(FakeParent(appliance_type="TD")).settings(s)
        return s

    def test_hidden_for_11_and_0(self) -> None:
        self.assertNotIn("startProgram.dryLevel", self._settings("11"))
        self.assertNotIn("startProgram.dryLevel", self._settings("0"))

    def test_kept_for_real_value(self) -> None:
        self.assertIn("startProgram.dryLevel", self._settings("3"))


class EdgeRobustnessTest(unittest.TestCase):
    def test_programname_no_program_param(self) -> None:
        out = _na("wc")(FakeParent(settings={}, appliance_type="WC")).attributes({"parameters": _params({"prCode": "5"})})
        self.assertEqual(out["programName"], "No Program")

    def test_programname_empty_prcode_no_crash(self) -> None:
        out = _na("wc")(FakeParent(settings={}, appliance_type="WC")).attributes({"parameters": _params({"prCode": ""})})
        self.assertEqual(out["programName"], "No Program")

    def test_no_offline_zeroing(self) -> None:
        # no more offline zeroing: machMode keeps the last value even when disconnected
        # (availability is handled by base_entity via `available`).
        _, params = _run("td", {"machMode": "5"}, connection=False, activity={})
        self.assertEqual(params["machMode"].value, 5)
        _, op = _run("ov", {"onOffStatus": "1", "temp": "50"}, connection=False)
        self.assertEqual(op["temp"].value, 50)
        _, wp = _run("wm", {"machMode": "3"}, last_conn="DISCONNECTED", activity={})
        self.assertEqual(wp["machMode"].value, 3)

    def test_missing_machmode_no_crash(self) -> None:
        out, params = _run("td", {"otherParam": "1"}, connection=False, activity={})
        self.assertNotIn("machMode", params)
        self.assertFalse(out["pause"])


# --- programName end-to-end (full appliance, synthetic with prCode) ---

class DictApi:
    def __init__(self, commands, attributes, history=None) -> None:
        self._c, self._a = commands, attributes
        self._h = history or []

    async def load_commands(self, a):
        return json.loads(json.dumps(self._c))

    async def load_favourites(self, a):
        return []

    async def load_command_history(self, a):
        return json.loads(json.dumps(self._h))

    async def load_attributes(self, a):
        return json.loads(json.dumps(self._a))

    async def load_statistics(self, a):
        return {}

    async def load_maintenance(self, a):
        return {}


def _prog(pr, pos=None):
    parameters = {
        "prCode": {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": pr}
    }
    if pos is not None:
        parameters["prPosition"] = {
            "typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": pos
        }
    return {"description": "d", "protocolType": "MQTT", "parameters": parameters}


_PN_COMMANDS = {
    "applianceModel": {"options": {}},
    "settings": {"setParameters": {"description": "d", "protocolType": "MQTT",
                                   "parameters": {"x": {"typology": "fixed", "category": "command", "mandatory": 0, "fixedValue": "1"}}}},
    "startProgram": {
        "PROGRAMS.REF.OFF": _prog("0"),
        "PROGRAMS.REF.SUPER_COOL": _prog("1"),
        "PROGRAMS.REF.SUPER_FREEZE": _prog("5"),
    },
    "dictionaryId": 1,
}
_PN_INFO = {"applianceTypeName": "REF", "applianceModelId": 1, "macAddress": "aa"}


def _shadow(prcode):
    return {"shadow": {"parameters": {
        "prCode": {"parNewVal": prcode, "lastUpdate": "2024-01-01T00:00:00"},
        "holidayMode": {"parNewVal": "0", "lastUpdate": "2024-01-01T00:00:00"},
        "intelligenceMode": {"parNewVal": "0", "lastUpdate": "2024-01-01T00:00:00"},
    }}}


class ProgramNameEndToEndTest(unittest.TestCase):
    def _build(self, attrs):
        from custom_components.addhon.client import factory
        app = factory._native_engine_appliance_cls()(DictApi(_PN_COMMANDS, attrs), dict(_PN_INFO), zone=0)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(app.load_commands())
            loop.run_until_complete(app.load_attributes())
        finally:
            loop.close()
        return app

    def test_prcode5_is_super_freeze(self) -> None:
        self.assertEqual(self._build(_shadow("5")).attributes["programName"], "super_freeze")

    def test_prcode0_is_no_program(self) -> None:
        # 0 is falsy -> "No Program" even if a program with id-0 exists
        self.assertEqual(self._build(_shadow("0")).attributes["programName"], "No Program")


# --- programName under an AMBIGUOUS prCode (the washer case) ---
#
# prCode does not identify a program: on a real HW80, code 115 is claimed by
# HQD_COTTONS, IOT_WASH_PERFECT_WHITE and IOT_WASH_BATHROBE at once (see
# diagnostics/live-2026-06-22/device-WM.json), and the hOn catalog in the APK has codes
# shared by up to 35 categories. The fixture below reproduces exactly that shape.

_WM_COMMANDS = {
    "applianceModel": {"options": {}},
    "settings": {
        "setParameters": {
            "description": "d",
            "protocolType": "MQTT",
            "parameters": {
                "x": {
                    "typology": "fixed",
                    "category": "command",
                    "mandatory": 0,
                    "fixedValue": "1",
                }
            },
        }
    },
    "startProgram": {
        "PROGRAMS.WM_WD.HQD_COTTONS": _prog("115"),
        "PROGRAMS.WM_WD.IOT_WASH_PERFECT_WHITE": _prog("115"),
        "PROGRAMS.WM_WD.IOT_WASH_BATHROBE": _prog("115"),
        "PROGRAMS.WM_WD.HQD_SMART": _prog("124"),
    },
    "dictionaryId": 1,
}
_WM_INFO = {"applianceTypeName": "WM", "applianceModelId": 1, "macAddress": "aa"}


def _wm_shadow(prcode):
    return {
        "shadow": {
            "parameters": {
                "prCode": {"parNewVal": prcode, "lastUpdate": "2024-01-01T00:00:00"},
            }
        }
    }


def _history(program):
    """A startProgram command-history entry naming `program` as the last one started."""
    return [
        {
            "command": {
                "commandName": "startProgram",
                "parameters": {"program": program},
            }
        }
    ]


class AmbiguousProgramCodeTest(unittest.TestCase):
    def _build(self, prcode, history=None, commands=None):
        from custom_components.addhon.client import factory

        app = factory._native_engine_appliance_cls()(
            DictApi(commands or _WM_COMMANDS, _wm_shadow(prcode), history),
            dict(_WM_INFO),
            zone=0,
        )
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(app.load_commands())
            loop.run_until_complete(app.load_attributes())
        finally:
            loop.close()
        return app

    def test_ids_keeps_only_the_canonical_program_of_a_shared_code(self) -> None:
        # The `iot_` skip is disambiguation: without it the map would answer with
        # whichever of the three categories happened to be iterated last.
        app = self._build("115")
        program = app.settings["startProgram.program"]
        self.assertEqual({115: "hqd_cottons", 124: "hqd_smart"}, program.ids)

    def test_a_running_downloaded_program_is_named_precisely(self) -> None:
        # THE fix. The appliance reports code 115 and the last started program was the
        # downloaded "Perfect White"; `ids` alone would answer "hqd_cottons", the base
        # program that merely shares the code.
        app = self._build("115", _history("PROGRAMS.WM_WD.IOT_WASH_PERFECT_WHITE"))
        self.assertEqual("iot_wash_perfect_white", app.attributes["programName"])

    def test_a_stale_active_category_falls_back_to_the_base_program(self) -> None:
        # The last app-started program was HQD_SMART (code 124) but the appliance now
        # reports 115: the cycle was started elsewhere, e.g. from the machine's own dial.
        # The active category is stale, so the unambiguous base program wins -- which is
        # the behaviour that predates the fix, hence never a regression.
        app = self._build("115", _history("PROGRAMS.WM_WD.HQD_SMART"))
        self.assertEqual("hqd_cottons", app.attributes["programName"])

    def test_without_history_the_base_program_is_used(self) -> None:
        self.assertEqual("hqd_cottons", self._build("115").attributes["programName"])

    def test_a_downloaded_category_listed_first_does_not_hijack_the_name(self) -> None:
        # REGRESSION. With no history the active category is merely the schema's FIRST
        # entry, so trusting it because its prCode matches is a coincidence, not
        # evidence: here the downloaded IOT_WASH_PERFECT_WHITE is listed first and shares
        # prCode 115, and it would be reported as running when nothing started it.
        # The previous fixture passed only because it happened to list the base program
        # first -- ordering was never the contract. Only a DELIBERATELY selected category
        # (recovered from history, or set through the program parameter) is evidence.
        commands = dict(_WM_COMMANDS)
        commands["startProgram"] = {
            "PROGRAMS.WM_WD.IOT_WASH_PERFECT_WHITE": _prog("115"),
            "PROGRAMS.WM_WD.HQD_COTTONS": _prog("115"),
            "PROGRAMS.WM_WD.HQD_SMART": _prog("124"),
        }
        app = self._build("115", commands=commands)
        self.assertEqual("hqd_cottons", app.attributes["programName"])

    def test_an_unshared_code_is_unaffected(self) -> None:
        app = self._build("124", _history("PROGRAMS.WM_WD.HQD_SMART"))
        self.assertEqual("hqd_smart", app.attributes["programName"])

    def test_a_code_no_category_declares_is_still_no_program(self) -> None:
        self.assertEqual("No Program", self._build("999").attributes["programName"])


# --- prPosition tie-break (dead code kept on purpose, see program.py) ---
#
# No appliance observed so far reports prPosition in its shadow, so this path never runs
# in production today. It is implemented and pinned anyway because the algorithm is known
# from the app (findCurrentProgramNameFromPrCodeAndPrPosition @ decomp.txt:2705620) and
# because the discovery log is what would ever let us learn that a device started sending
# the field. The fixture reproduces the REAL base-vs-base collision measured in the hOn
# catalog: prCode 4 is claimed by two BASE programs, which prPosition separates.

_POS_COMMANDS = {
    "applianceModel": {"options": {}},
    "startProgram": {
        "PROGRAMS.WM_WD.NIGHT_AND_DAY": _prog("4", "17"),
        "PROGRAMS.WM_WD.SYNTHETIC_AND_COLOURED": _prog("4", "6"),
        # Two downloaded variants riding on NIGHT_AND_DAY's dial slot: they inherit BOTH
        # fields, which is exactly why prPosition cannot separate downloaded from base.
        "PROGRAMS.WM_WD.IOT_WASH_BACKPACKS": _prog("4", "17"),
        "PROGRAMS.WM_WD.IOT_WASH_COLORED": _prog("4", "17"),
    },
    "dictionaryId": 1,
}


def _pos_shadow(prcode, prposition=None):
    parameters = {"prCode": {"parNewVal": prcode, "lastUpdate": "2024-01-01T00:00:00"}}
    if prposition is not None:
        parameters["prPosition"] = {
            "parNewVal": prposition, "lastUpdate": "2024-01-01T00:00:00"
        }
    return {"shadow": {"parameters": parameters}}


class PrPositionTieBreakTest(unittest.TestCase):
    def _build(self, prcode, prposition=None, history=None):
        from custom_components.addhon.client import factory

        app = factory._native_engine_appliance_cls()(
            DictApi(_POS_COMMANDS, _pos_shadow(prcode, prposition), history),
            dict(_WM_INFO),
            zone=0,
        )
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(app.load_commands())
            loop.run_until_complete(app.load_attributes())
        finally:
            loop.close()
        return app

    def test_without_prposition_a_base_collision_is_arbitrary_but_stable(self) -> None:
        # Today's reality: `ids` keeps ONE of the two base programs of code 4.
        name = self._build("4").attributes["programName"]
        self.assertIn(name, {"night_and_day", "synthetic_and_coloured"})

    def test_prposition_separates_two_base_programs(self) -> None:
        # THE case the tie-break exists for: prPosition 6 can only be SYNTHETIC_AND_COLOURED.
        self.assertEqual(
            "synthetic_and_coloured", self._build("4", "6").attributes["programName"]
        )

    def test_prposition_does_not_separate_downloaded_from_base(self) -> None:
        # Slot 17 holds the base program AND two downloaded variants: 3 candidates, so
        # the strict-narrowing rule declines and we fall back to `ids`. The invariant
        # that matters is that the answer is a BASE program -- never a downloaded one
        # that may not be running. Which of the two base programs `ids` keeps is an
        # insertion-order artifact of a prCode claimed by two of them, so it is
        # deliberately not pinned here.
        name = self._build("4", "17").attributes["programName"]
        self.assertIn(name, {"night_and_day", "synthetic_and_coloured"})

    def test_an_unmatched_position_falls_back_instead_of_blanking(self) -> None:
        # A position no category declares must never produce "No Program".
        name = self._build("4", "99").attributes["programName"]
        self.assertIn(name, {"night_and_day", "synthetic_and_coloured"})

    def test_the_active_category_still_wins_over_prposition(self) -> None:
        # The active category is a PRECISE identity (it distinguishes downloaded variants,
        # which prPosition provably cannot), so it must keep priority.
        app = self._build("4", "17", _history("PROGRAMS.WM_WD.IOT_WASH_BACKPACKS"))
        self.assertEqual("iot_wash_backpacks", app.attributes["programName"])

    def test_discovery_is_logged_once_with_the_candidate_counts(self) -> None:
        # The log line IS the deliverable: it is the evidence a future implementation
        # needs. The derivation runs once per POLL (load_attributes), not per property
        # access, so the latch is what keeps a 60s poll from repeating it forever.
        with self.assertLogs(
            "custom_components.addhon.client.engine.parameter.program", level="INFO"
        ) as captured:
            app = self._build("4", "6")
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(app.load_attributes())  # second poll
            finally:
                loop.close()
        self.assertEqual(1, len(captured.records))
        message = captured.records[0].getMessage()
        self.assertIn("prPosition=6", message)
        self.assertIn("prCode=4", message)
        # The counts are the actionable part: 4 candidates by prCode, 1 once prPosition
        # is added. Without them the line would not tell us whether the field is worth
        # anything on that model.
        self.assertIn("alone: 4", message)
        self.assertIn("prPosition: 1", message)

    def test_no_log_when_the_appliance_does_not_report_the_field(self) -> None:
        # Zero noise on every device known today.
        logger = logging.getLogger(
            "custom_components.addhon.client.engine.parameter.program"
        )
        with self.assertNoLogs(logger, level="INFO"):
            self._build("4")


if __name__ == "__main__":
    unittest.main()
