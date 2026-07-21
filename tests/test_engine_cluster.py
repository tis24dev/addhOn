# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Golden + behavioral test of the native engine CLUSTER (commands/command_loader/
rules/program). It used to be differential vs pyhOn; with `_vendor/` deleted it is
golden (native output proven == pyhOn at checkpoint 5a) + behavioral pins.

Covers: load_commands + sync on the real dump; send-path (prStr/programRules); a
rich synthetic dataset (favourites, multi-program+ids, recover, zone>0,
send_specific, program selection); rules on synthetic fixtures (incl. .triggers);
Protocol.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from copy import copy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import REPO, frozen, install_stubs, normalize  # noqa: E402

install_stubs()
_DUMP = REPO / "tests" / "fixtures" / "ref_10136"

from custom_components.addhon.client import factory  # noqa: E402
from custom_components.addhon.client.engine.commands import HonCommand as NaCommand  # noqa: E402
from custom_components.addhon.client.engine.rules import HonRuleSet  # noqa: E402
from custom_components.addhon.client import interfaces  # noqa: E402

NaAppliance = factory._native_engine_appliance_cls()


def _load(name: str):
    return json.loads((_DUMP / name).read_text(encoding="utf-8"))


class FakeApi:
    def __init__(self, *, favourites=None) -> None:
        self.sent: list = []
        self._favourites = favourites or []

    async def load_commands(self, a):
        return _load("commands.json")

    async def load_favourites(self, a):
        return list(self._favourites)

    async def load_command_history(self, a):
        return _load("command_history.json")

    async def load_attributes(self, a):
        return _load("attributes.json")

    async def load_statistics(self, a):
        return _load("statistics.json")

    async def load_maintenance(self, a):
        return _load("maintenance.json")

    async def send_command(self, appliance, name, params, ancillary, category):
        self.sent.append((name, dict(params), dict(ancillary), category))
        return True


class DictApi(FakeApi):
    def __init__(self, commands, history=None, favourites=None, attributes=None) -> None:
        super().__init__(favourites=favourites)
        self._commands = commands
        self._history = history or []
        self._attributes = attributes or {"shadow": {"parameters": {}}}

    async def load_commands(self, a):
        return json.loads(json.dumps(self._commands))

    async def load_command_history(self, a):
        return json.loads(json.dumps(self._history))

    async def load_attributes(self, a):
        return json.loads(json.dumps(self._attributes))

    async def load_statistics(self, a):
        return {}

    async def load_maintenance(self, a):
        return {}


_INFO = {"applianceTypeName": "REF", "applianceModelId": 10136, "macAddress": "aa-bb"}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _snap_param(p) -> dict:
    s = {"key": p.key, "category": p.category, "typology": p.typology,
         "mandatory": p.mandatory, "group": p.group, "value": p.value,
         "intern_value": p.intern_value, "values": list(p.values)}
    if hasattr(p, "min") and hasattr(p, "max") and hasattr(p, "step"):
        s["min"], s["max"], s["step"] = p.min, p.max, p.step
    if hasattr(p, "ids"):
        try:
            s["ids"] = dict(p.ids)
        except Exception as e:
            s["ids"] = f"<{type(e).__name__}>"
    if hasattr(p, "triggers"):
        s["triggers"] = p.triggers
    return s


def _snap_command(c) -> dict:
    return {"name": c.name, "category": c.category, "setting_keys": sorted(c.setting_keys),
            "categories": sorted(c.categories),
            "parameters": {k: _snap_param(p) for k, p in sorted(c.parameters.items())},
            "parameter_value": c.parameter_value, "parameter_groups": c.parameter_groups,
            "mandatory_parameter_groups": c.mandatory_parameter_groups,
            "available_settings": {k: _snap_param(p) for k, p in sorted(c.available_settings.items())},
            "data_keys": sorted(c.data)}


def _snap_appliance(a) -> dict:
    return {"commands": {n: _snap_command(c) for n, c in sorted(a.commands.items())},
            "additional_data_keys": sorted(a.additional_data), "options": a.options,
            "available_settings": sorted(a.available_settings),
            "settings": {k: _snap_param(p) for k, p in sorted(a.settings.items())},
            "command_parameters": a.command_parameters}


# --- rich synthetic dataset ---
def _prog(pr_code: str) -> dict:
    return {"description": "d", "protocolType": "MQTT", "parameters": {
        "prCode": {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": pr_code},
        "prStr": {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": "x"},
        "tempSel": {"typology": "range", "category": "command", "mandatory": 0,
                    "defaultValue": "5", "minimumValue": "2", "maximumValue": "8", "incrementValue": "1"}}}


_RICH_COMMANDS = {
    "applianceModel": {"options": {}},
    "settings": {"setParameters": {"description": "d", "protocolType": "MQTT", "parameters": {
        "tempSel": {"typology": "range", "category": "command", "mandatory": 1,
                    "defaultValue": "5", "minimumValue": "2", "maximumValue": "8", "incrementValue": "1"}}}},
    "startProgram": {
        "PROGRAMS.REF.SUPER_COOL": {**_prog("1"), "ancillaryParameters": {
            "programRules": {"typology": "fixed", "category": "command", "mandatory": 0, "fixedValue": "0"},
            "remoteActionable": {"typology": "fixed", "category": "command", "mandatory": 0, "fixedValue": "1"}}},
        "PROGRAMS.REF.SUPER_FREEZE": _prog("5"),
        "PROGRAMS.REF.iot_auto": {"description": "d", "protocolType": "MQTT", "parameters": {
            "prCode": {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": "9"}}}},
    "stopProgram": {"description": "d", "protocolType": "MQTT",
                    "parameters": {"onOff": {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": "0"}}},
    "dictionaryId": 1,
}
_RICH_COMMANDS["startProgram"]["PROGRAMS.REF.SUPER_COOL"]["parameters"]["speed"] = {
    "typology": "fixed", "category": "command", "mandatory": 0, "fixedValue": "3"}
_RICH_COMMANDS["startProgram"]["PROGRAMS.REF.SUPER_FREEZE"]["parameters"]["speed"] = {
    "typology": "range", "category": "command", "mandatory": 0,
    "defaultValue": "3", "minimumValue": "1", "maximumValue": "5", "incrementValue": "1"}

_IDS_ORDER_COMMANDS = {"applianceModel": {"options": {}},
                       "startProgram": {"PROGRAMS.REF.BIG": _prog("9"), "PROGRAMS.REF.SMALL": _prog("1")},
                       "dictionaryId": 1}
_MIXEDCASE_COMMANDS = {"applianceModel": {"options": {}},
                       "startProgram": {"PROGRAMS.REF.Mixed_Case": _prog("1")}, "dictionaryId": 1}
_RICH_FAVOURITES = [{"favouriteName": "MyFav",
                     "command": {"commandName": "startProgram", "programName": "PROGRAMS.REF.SUPER_COOL"},
                     "parameters": {"tempSel": "7"}}]
_RICH_HISTORY = [{"command": {"commandName": "startProgram",
                              "parameters": {"program": "PROGRAMS.REF.SUPER_FREEZE", "tempSel": "7"}}}]


def _build(cls, api):
    app = cls(api, dict(_INFO), zone=0)
    _run(app.load_commands())
    return app


def _native_snapshot() -> dict:
    out: dict = {}
    # end-to-end on the real dump + sync
    app = _build(NaAppliance, FakeApi())
    out["dump_load"] = _snap_appliance(app)
    app2 = _build(NaAppliance, FakeApi())
    _run(app2.load_attributes())
    app2.sync_params_to_command("settings")
    out["dump_after_sync"] = _snap_appliance(app2)
    # send settings
    sa = FakeApi()
    app3 = _build(NaAppliance, sa)
    _run(app3.load_attributes())
    _run(app3.commands["settings"].send())
    out["send_settings"] = sa.sent
    # rich dataset
    app4 = _build(NaAppliance, DictApi(_RICH_COMMANDS))
    out["rich_load"] = _snap_appliance(app4)
    out["rich_ids"] = dict(app4.commands["startProgram"].parameters["program"].ids)
    af = _build(NaAppliance, DictApi(_RICH_COMMANDS, favourites=_RICH_FAVOURITES))
    out["rich_favourites_categories"] = sorted(af.commands["startProgram"].categories)
    ah = _build(NaAppliance, DictApi(_RICH_COMMANDS, history=_RICH_HISTORY))
    out["rich_recover"] = _snap_appliance(ah)
    az = NaAppliance(DictApi(_RICH_COMMANDS), dict(_INFO), zone=1)
    _run(az.load_commands())
    out["rich_zone1"] = _snap_appliance(az)
    # send startProgram (prStr/programRules) + send_specific + only_mandatory
    for label, fn in (("send_start", lambda c: c.send()),
                      ("send_only_mandatory", lambda c: c.send(only_mandatory=True)),
                      ("send_specific", lambda c: c.send_specific(["tempSel"]))):
        sapi = DictApi(_RICH_COMMANDS)
        ap = _build(NaAppliance, sapi)
        _run(fn(ap.commands["startProgram"]))
        out[label] = sapi.sent
    # runtime program selection
    ap = _build(NaAppliance, DictApi(_RICH_COMMANDS))
    ap.commands["startProgram"].parameters["program"].value = "super_freeze"
    out["program_selection_category"] = ap.commands["startProgram"].category
    # sorted ids + prStr upper
    aio_ = _build(NaAppliance, DictApi(_IDS_ORDER_COMMANDS))
    out["ids_order"] = list(aio_.commands["startProgram"].parameters["program"].ids.items())
    smix = DictApi(_MIXEDCASE_COMMANDS)
    amix = _build(NaAppliance, smix)
    _run(amix.commands["startProgram"].send())
    out["prstr_mixed"] = smix.sent[0][1]["prStr"]
    # rules
    out["rules"] = _rules_snapshot()
    return out


# --- rules: synthetic fixtures ---
class FakeAppliance:
    def __init__(self) -> None:
        self.zone = 0
        self.options: dict = {}
        self.commands: dict = {}


def _enum(default, values):
    return {"typology": "enum", "category": "command", "mandatory": 0, "defaultValue": default, "enumValues": values}


def _range(default="20", lo="10", hi="40", inc="1"):
    return {"typology": "range", "category": "command", "mandatory": 0,
            "defaultValue": default, "minimumValue": lo, "maximumValue": hi, "incrementValue": inc}


def _rule(rule_dict, kind="fixedValue"):
    return {"category": "rule", kind: rule_dict}


# REAL AC structure (anonymized, from apk/dump/ac_live IOT_COOL): nested extra-condition
# ecoMode + machMode. Validates the `_extra_rules_matches` fix against the app data.
_AC_IOT_COOL = {
    "parameters": {
        "machMode": {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": "1"},
        "tempSel": {"typology": "range", "category": "command", "mandatory": 1,
                    "defaultValue": "22", "minimumValue": "16", "maximumValue": "30", "incrementValue": "1"},
        "windSpeed": {"typology": "enum", "category": "command", "mandatory": 1,
                      "defaultValue": "5", "enumValues": [1, 2, 3, 5]},
        "windDirectionHorizontal": {"typology": "enum", "category": "command", "mandatory": 1,
                                    "defaultValue": "0", "enumValues": [0, 3, 4, 5, 6, 7]},
        "windDirectionVertical": {"typology": "enum", "category": "command", "mandatory": 1,
                                  "defaultValue": "5", "enumValues": [2, 4, 5, 6, 8]},
    },
    "ancillaryParameters": {
        "ecoMode": {"typology": "range", "category": "general", "mandatory": 1,
                    "defaultValue": "0", "minimumValue": "0", "maximumValue": "1", "incrementValue": "1"},
        "programRules": {"category": "rule", "mandatory": 0, "typology": "fixed", "fixedValue": {
            "tempSel": {"ecoMode": {"1": {"machMode": {"1": {"fixedValue": "26", "typology": "fixed"},
                                                       "4": {"fixedValue": "20", "typology": "fixed"}}}}},
            "windDirectionHorizontal": {"ecoMode": {"1": {"machMode": {"1|4": {"fixedValue": "4", "typology": "fixed"}}}}},
            "windDirectionVertical": {"ecoMode": {"1": {"machMode": {"1|4": {"fixedValue": "3", "typology": "fixed"}}}}},
            "windSpeed": {"ecoMode": {"1": {"machMode": {"1|4": {"defaultValue": "5", "enumValues": "1|2|3|5", "typology": "enum"}}}}},
        }},
    },
}


_RULES = {
    # real AC: ecoMode=1 (with machMode fixed at 1) MUST constrain tempSel/windDir/windSpeed
    "ac_eco_nested": (_AC_IOT_COOL, [("ecoMode", "1")]),
    "fixed_in_range": ({"parameters": {"mode": _enum("cold", ["cold", "hot"]), "temp": _range()},
                        "rules": {"r": _rule({"temp": {"mode": {"hot": {"typology": "fixed", "fixedValue": "30"}}}})}},
                       [("mode", "hot")]),
    "fixed_expand": ({"parameters": {"mode": _enum("cold", ["cold", "hot"]), "temp": _range()},
                      "rules": {"r": _rule({"temp": {"mode": {"hot": {"typology": "fixed", "fixedValue": "55"}}}})}},
                     [("mode", "hot")]),
    "range_shrink": ({"parameters": {"mode": _enum("cold", ["cold", "hot"]), "temp": _range()},
                     "rules": {"r": _rule({"temp": {"mode": {"hot": {"typology": "fixed", "fixedValue": "5"}}}})}},
                    [("mode", "hot")]),
    "enum_target": ({"parameters": {"mode": _enum("cold", ["cold", "hot"]), "fan": _enum("low", ["low", "mid", "high"])},
                     "rules": {"r": _rule({"fan": {"mode": {"hot": {"typology": "enum", "enumValues": "mid|high", "defaultValue": "high"}}}})}},
                    [("mode", "hot")]),
    "fixed_on_enum": ({"parameters": {"mode": _enum("cold", ["cold", "hot"]), "fan": _enum("low", ["low", "mid", "high"])},
                       "rules": {"r": _rule({"fan": {"mode": {"hot": {"typology": "fixed", "fixedValue": "high"}}}})}},
                      [("mode", "hot")]),
    "pipe_split": ({"parameters": {"mode": _enum("cold", ["cold", "hot"]), "temp": _range()},
                    "rules": {"r": _rule({"temp": {"mode": {"cold|hot": {"typology": "fixed", "fixedValue": "30"}}}})}},
                   [("mode", "hot")]),
    "at_strip": ({"parameters": {"mode": _enum("cold", ["cold", "hot"]), "temp": _range()},
                  "rules": {"r": _rule({"temp": {"@mode": {"hot": {"typology": "fixed", "fixedValue": "30"}}}})}},
                 [("mode", "hot")]),
    "self_ref": ({"parameters": {"mode": _enum("cold", ["cold", "hot"]), "temp": _range()},
                  "rules": {"r": _rule({"temp": {"mode": {"hot": {"typology": "fixed", "fixedValue": "@temp"}}}})}},
                 [("mode", "hot")]),
    "scalar": ({"parameters": {"mode": _enum("cold", ["cold", "hot"]), "temp": _range()},
                "rules": {"r": _rule({"temp": {"mode": {"hot": "30"}}})}},
               [("mode", "hot")]),
    "nested_both": ({"parameters": {"mode": _enum("cold", ["cold", "hot"]), "speed": _enum("lo", ["lo", "hi"]), "temp": _range()},
                     "rules": {"r": _rule({"temp": {"mode": {"hot": {"speed": {"hi": {"typology": "fixed", "fixedValue": "35"}}}}}})}},
                    [("speed", "hi"), ("mode", "hot")]),
    "nested_partial": ({"parameters": {"mode": _enum("cold", ["cold", "hot"]), "speed": _enum("lo", ["lo", "hi"]), "temp": _range()},
                        "rules": {"r": _rule({"temp": {"mode": {"hot": {"speed": {"hi": {"typology": "fixed", "fixedValue": "35"}}}}}})}},
                       [("mode", "hot")]),
}


def _rules_snapshot() -> dict:
    out: dict = {}
    for name, (attrs, actions) in _RULES.items():
        c = NaCommand("c", json.loads(json.dumps(attrs)), FakeAppliance())
        steps = [{k: _snap_param(p) for k, p in sorted(c.parameters.items())}]
        for param, value in actions:
            c.parameters[param].value = value
            steps.append({k: _snap_param(p) for k, p in sorted(c.parameters.items())})
        out[name] = steps
    return out


class ClusterGoldenTest(unittest.TestCase):
    def test_native_cluster_matches_golden(self) -> None:
        snap = _native_snapshot()
        self.assertEqual(normalize(snap), frozen("engine_cluster", snap))


class ClusterBehaviorTest(unittest.TestCase):
    def test_send_prstr_and_programrules(self) -> None:
        snap = _native_snapshot()
        name, params, ancillary, _ = snap["send_start"][0]
        self.assertEqual(params["prStr"], "PROGRAMS.REF.SUPER_COOL")
        self.assertNotIn("programRules", ancillary)

    def test_ids_excludes_iot_and_sorted(self) -> None:
        snap = _native_snapshot()
        self.assertEqual(snap["rich_ids"], {1: "super_cool", 5: "super_freeze"})
        self.assertEqual(snap["ids_order"], [(1, "small"), (9, "big")])

    def test_prstr_uppercased(self) -> None:
        self.assertEqual(_native_snapshot()["prstr_mixed"], "PROGRAMS.REF.MIXED_CASE")

    def test_program_selection(self) -> None:
        self.assertEqual(_native_snapshot()["program_selection_category"], "PROGRAMS.REF.SUPER_FREEZE")

    def test_program_value_invalid_raises(self) -> None:
        ap = _build(NaAppliance, DictApi(_RICH_COMMANDS))
        with self.assertRaises(ValueError):
            ap.commands["startProgram"].parameters["program"].value = "nope"

    def test_favourite_added(self) -> None:
        self.assertIn("MyFav", _native_snapshot()["rich_favourites_categories"])

    def test_favourite_does_not_corrupt_base_program(self) -> None:
        # Regression: `_add_favourites` shallow-copied the base command, sharing its
        # `_parameters` dict AND parameter objects. Applying MyFav (tempSel=7 on
        # SUPER_COOL) then mutated the REAL super_cool program -> it got tempSel=7 and
        # a favourite="1" flag, and `HonParameterProgram.ids` (which drops favourites)
        # hid it entirely. HonCommand.__copy__ now isolates the parameters.
        app = _build(NaAppliance, DictApi(_RICH_COMMANDS, favourites=_RICH_FAVOURITES))
        start = app.commands["startProgram"]
        base = start.categories["super_cool"]  # the real program, not the MyFav copy
        # base keeps its own default, untouched by the favourite's tempSel=7
        self.assertEqual(float(base.parameters["tempSel"].value), 5.0)
        # base is NOT flagged as a favourite...
        self.assertNotIn("favourite", base.parameters)
        # ...so it still appears in the selectable program ids (prCode 1 -> super_cool)
        self.assertEqual(start.parameters["program"].ids.get(1), "super_cool")
        # the favourite itself is a distinct command carrying tempSel=7 + favourite=1
        fav = start.categories["MyFav"]
        self.assertEqual(float(fav.parameters["tempSel"].value), 7.0)
        self.assertEqual(str(fav.parameters["favourite"].value), "1")

    def test_favourite_copy_rules_do_not_corrupt_base(self) -> None:
        # Regression: isolating `_parameters` in __copy__ was not enough. A shallow-copied
        # parameter SHARED its trigger table with the base, and every rule callback closed
        # over the BASE command. Applying a favourite sets values on the copy (see
        # command_loader._update_base_command_with_data), which fires check_trigger -> the
        # rule ran against the BASE program and mutated it. __copy__ now gives the copy its
        # own trigger tables + rule sets bound to itself.
        attrs = {
            "parameters": {
                "mode": _enum("cold", ["cold", "hot"]),
                "temp": _range(default="20", lo="16", hi="30", inc="1"),
            },
            "rules": {"r": _rule({"temp": {"mode": {"hot": {"typology": "fixed", "fixedValue": "28"}}}})},
        }
        base = NaCommand("c", json.loads(json.dumps(attrs)), FakeAppliance())
        fav = copy(base)
        # Applying the favourite fires the rule ON THE COPY: mode=hot -> temp=28.
        fav.parameters["mode"].value = "hot"
        self.assertEqual(fav.parameters["temp"].value, 28)  # the copy IS constrained
        # ...and the base program keeps its own default, uncorrupted.
        self.assertEqual(base.parameters["temp"].value, 20)  # bug: became 28
        self.assertEqual(base.parameters["mode"].value, "cold")
        # White-box: the copy's rule set is a distinct object bound to the copy, and the
        # copied parameter's trigger table is not shared with the base.
        self.assertIsNot(fav._rules[0], base._rules[0])
        self.assertIsNot(fav.parameters["mode"]._triggers, base.parameters["mode"]._triggers)

    def test_malformed_fixed_value_rule_skipped_at_runtime(self) -> None:
        # A rule whose fixedValue is non-numeric for a RANGE target must NOT raise out
        # of the runtime trigger (the range setter rejects "x" with ValueError). The
        # rule is skipped and the parameter keeps its value; setup is not aborted.
        attrs = {
            "parameters": {
                "mode": _enum("cold", ["cold", "hot"]),
                "temp": _range(default="20", lo="16", hi="30", inc="1"),
            },
            "rules": {"r": _rule({"temp": {"mode": {"hot": {"typology": "fixed", "fixedValue": "not-a-number"}}}})},
        }
        cmd = NaCommand("c", json.loads(json.dumps(attrs)), FakeAppliance())
        cmd.parameters["mode"].value = "hot"  # fires the bad rule -> must be swallowed
        self.assertEqual(cmd.parameters["temp"].value, 20)  # unchanged

    def test_malformed_fixed_value_rule_skipped_at_construction(self) -> None:
        # Same malformed rule, but the trigger value equals the param DEFAULT, so the
        # immediate-fire runs during construction (patch()). Building the command must
        # not raise -- the ValueError from _apply_fixed used to abort the whole load.
        attrs = {
            "parameters": {
                "mode": _enum("hot", ["cold", "hot"]),  # default already == trigger
                "temp": _range(default="20", lo="16", hi="30", inc="1"),
            },
            "rules": {"r": _rule({"temp": {"mode": {"hot": {"typology": "fixed", "fixedValue": "not-a-number"}}}})},
        }
        cmd = NaCommand("c", json.loads(json.dumps(attrs)), FakeAppliance())  # must not raise
        self.assertEqual(cmd.parameters["temp"].value, 20)  # unchanged

    def test_copy_rebinds_program_param_backrefs(self) -> None:
        # A HonParameterProgram carries `_command` (the base command); its value-setter
        # does `self._command.category = value` (swapping appliance.commands). A shallow
        # copy shares that back-reference, so a write on the copy's program parameter
        # could reach the base. __copy__ now rebinds `_command`/`_programs` to the copy.
        from custom_components.addhon.client.engine.parameter.program import (
            HonParameterProgram,
        )

        app = _build(NaAppliance, DictApi(_RICH_COMMANDS, favourites=_RICH_FAVOURITES))
        base = app.commands["startProgram"].categories["super_cool"]
        self.assertIsInstance(base.parameters["program"], HonParameterProgram)
        fav = copy(base)
        fav_prog = fav.parameters["program"]
        self.assertIs(fav_prog._command, fav)       # rebound to the copy...
        self.assertIsNot(fav_prog._command, base)   # ...not the base command
        self.assertIs(fav_prog._programs, fav.categories)

    def test_favourites_malformed_do_not_crash(self) -> None:
        # Stale/malformed favourites payloads must not stop the loader.
        bad_favs = [
            {"command": {"commandName": "doesNotExist", "programName": "X"}},  # removed command -> KeyError
            {"favouriteName": "NoCmd"},  # no command key
            {"favouriteName": "BadCmd", "command": "not-a-dict"},  # non-dict command
            {"favouriteName": "BadData",  # non-dict data -> .items() AttributeError
             "command": {"commandName": "startProgram", "programName": "PROGRAMS.REF.SUPER_COOL"},
             "parameters": ["not", "a", "dict"]},
        ]
        app = _build(NaAppliance, DictApi(_RICH_COMMANDS, favourites=bad_favs))
        cats = app.commands["startProgram"].categories
        self.assertIn("BadData", cats)  # the valid-but-with-dirty-data one is added
        self.assertNotIn("doesNotExist", cats)

    def test_nested_rule_extras_not_cross_contaminated(self) -> None:
        # ORACLE: two branches of the same trigger (ecoMode 1 and 2), each with a
        # nested condition on machMode, must stay independent. Bug: `extra` was
        # mutated and shared across iterations -> the ecoMode=1 branch rule got
        # corrupted to ecoMode=2, so setting ecoMode=1 no longer fired.
        attrs = {
            "parameters": {
                "ecoMode": _range(default="0", lo="0", hi="2", inc="1"),
                "machMode": {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": "5"},
                "temp": _range(default="20", lo="16", hi="30", inc="1"),
            },
            "rules": {"r": _rule({"temp": {"ecoMode": {
                "1": {"machMode": {"5": {"typology": "fixed", "fixedValue": "25"}}},
                "2": {"machMode": {"5": {"typology": "fixed", "fixedValue": "28"}}},
            }}})},
        }
        c1 = NaCommand("c", json.loads(json.dumps(attrs)), FakeAppliance())
        c1.parameters["ecoMode"].value = "1"
        self.assertEqual(c1.parameters["temp"].value, 25)  # bug: stayed 20
        c2 = NaCommand("c", json.loads(json.dumps(attrs)), FakeAppliance())
        c2.parameters["ecoMode"].value = "2"
        self.assertEqual(c2.parameters["temp"].value, 28)
        # White-box: the two machMode-trigger rules must have DISTINCT extras, each with
        # the right value for its own branch (pins the per-branch copy, not just end-to-end).
        class _Cmd:
            appliance = FakeAppliance()
        rs = HonRuleSet(_Cmd(), {"temp": {"ecoMode": {
            "1": {"machMode": {"5": {"typology": "fixed", "fixedValue": "25"}}},
            "2": {"machMode": {"5": {"typology": "fixed", "fixedValue": "28"}}},
        }}})
        mach_rules = rs.rules["machMode"]
        self.assertEqual([r.extras for r in mach_rules], [{"ecoMode": "1"}, {"ecoMode": "2"}])
        self.assertIsNot(mach_rules[0].extras, mach_rules[1].extras)

    def test_range_rule_preserves_decimal(self) -> None:
        # ORACLE: a decimal fixedValue rule ("22.5") on a range with a decimal step
        # must set 22.5, not 22. Bug: _apply_fixed passed float(value) to the setter,
        # which does str_to_float(22.5)=int(22.5)=22 -> silently truncates (with step 0.5
        # the truncated 22 also passes the off-step check, so no error).
        attrs = {
            "parameters": {
                "mode": _enum("cold", ["cold", "hot"]),
                "temp": _range(default="20", lo="16", hi="30", inc="0.5"),
            },
            "rules": {"r": _rule({"temp": {"mode": {"hot": {"typology": "fixed", "fixedValue": "22.5"}}}})},
        }
        c = NaCommand("c", json.loads(json.dumps(attrs)), FakeAppliance())
        c.parameters["mode"].value = "hot"
        self.assertEqual(c.parameters["temp"].value, 22.5)

    def test_sync_params_to_command_preserves_half_degree(self) -> None:
        # ORACLE (Bug3 at its call-site): a half-degree setpoint (22.5) coming from the
        # attributes must survive sync_params_to_command into the command settings, not
        # be truncated to 22. The old code assigned float(new.value) to the range setter,
        # and str_to_float(22.5)=int(22.5)=22 truncates silently (step 0.5 makes the
        # truncated 22 pass the off-step check, so no ValueError surfaced). The fix
        # assigns str(new.value). Without this test a regression to float() would still
        # leave all other tests green.
        from custom_components.addhon.client.engine.attributes import HonAttribute

        command = NaCommand(
            "settings",
            {"parameters": {"temp": _range(default="20", lo="16", hi="30", inc="0.5")}},
            FakeAppliance(),
        )
        app = NaAppliance(FakeApi(), dict(_INFO), zone=0)
        app._commands = {"settings": command}
        app._attributes = {"parameters": {"temp": HonAttribute({"parNewVal": "22.5"})}}
        app.sync_params_to_command("settings")
        self.assertEqual(command.settings["temp"].value, 22.5)

    def test_sync_params_to_command_snaps_off_grid_shadow(self) -> None:
        # REGRESSION (discussion #62): a wine cooler reports its setpoint OFF-GRID in the
        # shadow (a measured 17.2 for a step-1 tempSel). The old code let the range setter
        # raise on 17.2 and then SKIPPED the key, leaving the command at its load-time
        # default (min=5). A later full-command send (fired by the light switch) then wrote
        # 5C back to the device, clobbering the real setpoint. The fix snaps the off-grid
        # shadow value onto the grid so the command carries the true setpoint (17), not 5.
        from custom_components.addhon.client.engine.attributes import HonAttribute

        command = NaCommand(
            "settings",
            {"parameters": {"tempSel": _range(default="5", lo="5", hi="20", inc="1")}},
            FakeAppliance(),
        )
        app = NaAppliance(FakeApi(), dict(_INFO), zone=0)
        app._commands = {"settings": command}
        # sanity: without a sync the command sits at the min default, not the real setpoint
        self.assertEqual(command.settings["tempSel"].value, 5)
        app._attributes = {"parameters": {"tempSel": HonAttribute({"parNewVal": "17.2"})}}
        app.sync_params_to_command("settings")
        self.assertEqual(command.settings["tempSel"].value, 17)

    def test_sync_params_out_of_range_shadow_keeps_command_value(self) -> None:
        # GUARD (PR #66 review): an OUT-OF-RANGE shadow value (stale local metadata) must NOT
        # be snapped/clamped into the command, or a later send would overwrite the command's
        # current (here user-set) value with a boundary guess. Sync leaves the command as-is.
        from custom_components.addhon.client.engine.attributes import HonAttribute

        command = NaCommand(
            "settings",
            {"parameters": {"tempSel": _range(default="5", lo="5", hi="20", inc="1")}},
            FakeAppliance(),
        )
        app = NaAppliance(FakeApi(), dict(_INFO), zone=0)
        app._commands = {"settings": command}
        command.settings["tempSel"].value = "18"   # a good, user-set value on the command
        app._attributes = {"parameters": {"tempSel": HonAttribute({"parNewVal": "25"})}}
        app.sync_params_to_command("settings")
        self.assertEqual(command.settings["tempSel"].value, 18)  # preserved, not clamped to 20

    def test_ac_eco_nested_rule_fires(self) -> None:
        # REAL AC structure (apk/dump/ac_live): ecoMode=1 with machMode fixed=1
        # must constrain tempSel to 26 and the wind-direction (nested extra-condition).
        # Pin of the `_extra_rules_matches` fix validated live: pyhOn left tempSel at 22.
        c = NaCommand("c", json.loads(json.dumps(_AC_IOT_COOL)), FakeAppliance(),
                      category_name="PROGRAMS.AC.IOT_COOL")
        self.assertEqual(c.parameters["tempSel"].value, 22)
        c.parameters["ecoMode"].value = "1"
        self.assertEqual(c.parameters["tempSel"].value, 26)
        self.assertEqual(c.parameters["windDirectionHorizontal"].value, "4")
        self.assertEqual(c.parameters["windDirectionVertical"].value, "3")


class NativeEnumEdgeBehaviorTest(unittest.TestCase):
    """Pin of the BABYCARE fix that the cluster exposes on favourites/recover/rule-default:
    the native side accepts a re-cased enum and keeps the raw value in intern_value."""

    def test_cased_enum_value_accepted(self) -> None:
        from custom_components.addhon.client.engine.parameter.enum import HonParameterEnum as NaEnum
        data = {"category": "command", "typology": "enum", "mandatory": 0,
                "defaultValue": "[dashboard]", "enumValues": ["dashboard"]}
        na = NaEnum("pf", dict(data), "ancillaryParameters")
        na.value = "DASHBOARD"
        self.assertEqual(na.value, "dashboard")
        self.assertEqual(na.intern_value, "DASHBOARD")


# REAL `$installationType` rule (AC IOT_SELF_CLEAN, anonymized): static device config.
# Validated on the live AC (unitConfiguration='1to1' -> inert) + app model.
_AC_SELF_CLEAN = {
    "description": "d", "protocolType": "MQTT",
    "ancillaryParameters": {
        "remoteActionable": {"typology": "range", "category": "general", "mandatory": 0,
                             "defaultValue": "1", "minimumValue": "0", "maximumValue": "1", "incrementValue": "1"},
        "remoteVisible": {"typology": "range", "category": "general", "mandatory": 0,
                          "defaultValue": "1", "minimumValue": "0", "maximumValue": "1", "incrementValue": "1"},
        "programRules": {"category": "rule", "typology": "fixed", "mandatory": 0, "fixedValue": {
            "remoteActionable": {"$installationType": {"1toN": {"fixedValue": "0", "typology": "fixed"}}},
            "remoteVisible": {"$installationType": {"1toN": {"fixedValue": "0", "typology": "fixed"}}},
        }},
    },
}


class _ConfigApp:
    zone = 0
    options: dict = {}
    commands: dict = {}

    def __init__(self, unit_config) -> None:
        self.info = {"unitConfiguration": unit_config} if unit_config is not None else {}


class ConfigRuleTest(unittest.TestCase):
    """`$installationType` rules: static device config, app model resolved against
    `appliance.info['unitConfiguration']` (app maps $installationType->unitConfiguration).
    Real AC = 1to1 -> no branch -> inert (correct); 1toN -> the rule fires."""

    def _build(self, unit_config):
        return NaCommand("c", json.loads(json.dumps(_AC_SELF_CLEAN)), _ConfigApp(unit_config),
                         category_name="PROGRAMS.AC.IOT_SELF_CLEAN")

    def test_1to1_inert(self) -> None:
        c = self._build("1to1")  # real device: only the 1toN branch -> no match -> default
        self.assertEqual(c.parameters["remoteActionable"].value, 1)
        self.assertEqual(c.parameters["remoteVisible"].value, 1)

    def test_1toN_fires(self) -> None:
        c = self._build("1toN")  # multi-split: the rule fires
        self.assertEqual(c.parameters["remoteActionable"].value, 0)
        self.assertEqual(c.parameters["remoteVisible"].value, 0)

    def test_missing_unitconfig_inert(self) -> None:
        c = self._build(None)  # field absent -> does not fire (fallback like the app)
        self.assertEqual(c.parameters["remoteActionable"].value, 1)

    def test_malformed_config_rule_skipped_not_aborting_build(self) -> None:
        # A $installationType config rule whose fixedValue is non-numeric for a RANGE
        # target must NOT raise out of HonCommand construction: patch() runs in
        # __init__ and the loader does not wrap it, so an escaping ValueError would drop
        # every command -- and thus every entity -- of the device. The bad rule is
        # skipped per-iteration (like the runtime trigger path) while a sibling good
        # config rule still fires.
        bad = json.loads(json.dumps(_AC_SELF_CLEAN))
        bad["ancillaryParameters"]["programRules"]["fixedValue"] = {
            "remoteActionable": {
                "$installationType": {"1toN": {"fixedValue": "not-a-number",
                                               "typology": "fixed"}}},
            "remoteVisible": {
                "$installationType": {"1toN": {"fixedValue": "0", "typology": "fixed"}}},
        }
        c = NaCommand("c", bad, _ConfigApp("1toN"),
                      category_name="PROGRAMS.AC.IOT_SELF_CLEAN")
        # Construction did not raise; all params built; the malformed rule was skipped
        # (target keeps its default 1) but the sibling good rule still applied (-> 0).
        self.assertEqual(c.parameters["remoteActionable"].value, 1)
        self.assertEqual(c.parameters["remoteVisible"].value, 0)

    def test_malformed_config_rule_rolls_back_partial_mutation(self) -> None:
        # A fixedValue BEYOND max AND off the min/step grid widens the range's max
        # (numeric > max) BEFORE the value setter rejects it as off-grid. Skipping the
        # rule is not enough: without a rollback the param is left with the widened max
        # and the old value, so the entity would expose a wrong range. The guard must
        # restore the pre-apply state; a sibling well-formed rule still fires.
        bad = json.loads(json.dumps(_AC_SELF_CLEAN))
        bad["ancillaryParameters"]["programRules"]["fixedValue"] = {
            "remoteActionable": {
                "$installationType": {"1toN": {"fixedValue": "1.5", "typology": "fixed"}}},
            "remoteVisible": {
                "$installationType": {"1toN": {"fixedValue": "0", "typology": "fixed"}}},
        }
        c = NaCommand("c", bad, _ConfigApp("1toN"),
                      category_name="PROGRAMS.AC.IOT_SELF_CLEAN")
        ra = c.parameters["remoteActionable"]
        # Rolled back: max is the original 1 (NOT the transiently-widened 1.5) and the
        # value is untouched. The sibling well-formed config rule still applied (-> 0).
        self.assertEqual(ra.max, 1)
        self.assertEqual(ra.value, 1)
        self.assertEqual(c.parameters["remoteVisible"].value, 0)


class _HassStub:
    async def async_add_executor_job(self, fn, *a):
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(fn, *a).result(timeout=5)


class _ClientStub:
    def run_command_sync(self, coro) -> None:
        asyncio.run(coro)


class _FailApi:
    async def send_command(self, *a, **k):
        raise RuntimeError("send boom")


class _RuleApp:
    zone = 0

    def __init__(self) -> None:
        self.options: dict = {}
        self.info: dict = {}
        self.api = _FailApi()
        self.commands: dict = {}

    def sync_command_to_params(self, name) -> None:
        pass


class RollbackAfterRuleCascadeTest(unittest.TestCase):
    """If a sent parameter is a rule trigger, the assignment narrows the `.values`
    of a sibling parameter (cascade). A failed send() MUST restore the exact command
    state (value AND values), not just the values via the setter (which would
    re-trigger the rules and leave values narrowed -> corrupted state)."""

    def test_send_failure_restores_full_param_state(self) -> None:
        from custom_components.addhon.hon_commands import async_send_command

        app = _RuleApp()
        cmd = NaCommand("settings", json.loads(json.dumps(_AC_IOT_COOL)), app,
                        category_name="PROGRAMS.AC.IOT_COOL")
        app.commands["settings"] = cmd
        wdh = cmd.parameters["windDirectionHorizontal"]
        eco = cmd.parameters["ecoMode"]
        eco_before = eco.value
        wdh_value_before = wdh.value
        wdh_values_before = list(wdh.values)

        with self.assertRaises(RuntimeError):
            asyncio.run(async_send_command(_HassStub(), _ClientStub(), app, "settings",
                                           {"ecoMode": "1"}))

        # ecoMode=1 had narrowed windDirectionHorizontal to ["4"]; after the rollback
        # value AND values must go back to the initial ones.
        self.assertEqual(eco.value, eco_before)
        self.assertEqual(wdh.value, wdh_value_before)
        self.assertEqual(list(wdh.values), wdh_values_before)


class ProtocolConformanceTest(unittest.TestCase):
    def test_native_objects_satisfy_protocols(self) -> None:
        na = _build(NaAppliance, FakeApi())
        self.assertIsInstance(na, interfaces.Appliance)
        for command in na.commands.values():
            self.assertIsInstance(command, interfaces.Command)
            for param in command.parameters.values():
                self.assertIsInstance(param, interfaces.Parameter)


if __name__ == "__main__":
    unittest.main()
