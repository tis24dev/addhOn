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
