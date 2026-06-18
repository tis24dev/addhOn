"""Golden + behavioral test del CLUSTER motore nativo (commands/command_loader/rules/
program). Era differential vs pyhОn; con `_vendor/` cancellato è golden (output nativo
provato == pyhОn al checkpoint 5a) + pin comportamentali.

Copre: load_commands + sync sul dump reale; send-path (prStr/programRules); dataset
sintetico ricco (favourites, multi-programma+ids, recover, zone>0, send_specific,
selezione programma); rules su fixture sintetiche (incl. .triggers); Protocol.
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
_DUMP = REPO / "apk" / "dump" / "ref_10136"

from custom_components.addhon.client import pyhon_adapter  # noqa: E402
from custom_components.addhon.client.engine.commands import HonCommand as NaCommand  # noqa: E402
from custom_components.addhon.client import interfaces  # noqa: E402

NaAppliance = pyhon_adapter._native_engine_appliance_cls()


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
    return asyncio.new_event_loop().run_until_complete(coro)


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


# --- dataset sintetico ricco ---
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
    # end-to-end sul dump reale + sync
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
    # dataset ricco
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
    # selezione programma a runtime
    ap = _build(NaAppliance, DictApi(_RICH_COMMANDS))
    ap.commands["startProgram"].parameters["program"].value = "super_freeze"
    out["program_selection_category"] = ap.commands["startProgram"].category
    # ids ordinati + prStr upper
    aio_ = _build(NaAppliance, DictApi(_IDS_ORDER_COMMANDS))
    out["ids_order"] = list(aio_.commands["startProgram"].parameters["program"].ids.items())
    smix = DictApi(_MIXEDCASE_COMMANDS)
    amix = _build(NaAppliance, smix)
    _run(amix.commands["startProgram"].send())
    out["prstr_mixed"] = smix.sent[0][1]["prStr"]
    # rules
    out["rules"] = _rules_snapshot()
    return out


# --- rules: fixture sintetiche ---
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


# Struttura REALE dell'AC (anonimizzata, da apk/dump/ac_live IOT_COOL): condizione-extra
# annidata ecoMode + machMode. Valida il fix di `_extra_rules_matches` sui dati dell'app.
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
    # AC reale: ecoMode=1 (con machMode fisso a 1) DEVE vincolare tempSel/windDir/windSpeed
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

    def test_ac_eco_nested_rule_fires(self) -> None:
        # struttura REALE dell'AC (apk/dump/ac_live): ecoMode=1 con machMode fisso=1
        # deve vincolare tempSel a 26 e le wind-direction (condizione-extra annidata).
        # Pin del fix `_extra_rules_matches` validato live: pyhОn lasciava tempSel a 22.
        c = NaCommand("c", json.loads(json.dumps(_AC_IOT_COOL)), FakeAppliance(),
                      category_name="PROGRAMS.AC.IOT_COOL")
        self.assertEqual(c.parameters["tempSel"].value, 22)
        c.parameters["ecoMode"].value = "1"
        self.assertEqual(c.parameters["tempSel"].value, 26)
        self.assertEqual(c.parameters["windDirectionHorizontal"].value, "4")
        self.assertEqual(c.parameters["windDirectionVertical"].value, "3")


class NativeEnumEdgeBehaviorTest(unittest.TestCase):
    """Pin del fix BABYCARE che il cluster espone su favourites/recover/rule-default:
    il nativo accetta un enum ri-castato e ne tiene il grezzo in intern_value."""

    def test_cased_enum_value_accepted(self) -> None:
        from custom_components.addhon.client.engine.parameter.enum import HonParameterEnum as NaEnum
        data = {"category": "command", "typology": "enum", "mandatory": 0,
                "defaultValue": "[dashboard]", "enumValues": ["dashboard"]}
        na = NaEnum("pf", dict(data), "ancillaryParameters")
        na.value = "DASHBOARD"
        self.assertEqual(na.value, "dashboard")
        self.assertEqual(na.intern_value, "DASHBOARD")


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
