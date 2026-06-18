"""Golden + behavioral test del layer per-tipo nativo (Fase 4). Era differential vs
pyhОn; con `_vendor/` cancellato è golden (output nativo provato == pyhОn al checkpoint
5a) + pin dei FIX app-priority (modeZ/pause/wh-active per valore; dryLevel '0'/'11';
`available`) e del registry.
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
    """Pin esplicito dei FIX app-priority (codice nuovo, niente bug pyhОn)."""

    def test_ref_modes_by_value(self) -> None:
        s = _native_snapshot()
        self.assertEqual(s["ref_holiday"]["modeZ1"], "holiday")
        self.assertEqual(s["ref_freeze"]["modeZ2"], "super_freeze")
        self.assertEqual((s["ref_autoset"]["modeZ1"], s["ref_autoset"]["modeZ2"]), ("auto_set", "auto_set"))
        self.assertEqual(s["ref_both_z1"]["modeZ1"], "holiday")           # priorità Z1
        self.assertEqual(s["ref_freeze_vs_autoset"]["modeZ2"], "super_freeze")  # priorità Z2
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
        # niente più zeroing offline: machMode mantiene l'ultimo valore anche disconnesso
        # (la disponibilità è gestita da base_entity via `available`).
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


# --- programName end-to-end (full appliance, sintetico con prCode) ---

class DictApi:
    def __init__(self, commands, attributes) -> None:
        self._c, self._a = commands, attributes

    async def load_commands(self, a):
        return json.loads(json.dumps(self._c))

    async def load_favourites(self, a):
        return []

    async def load_command_history(self, a):
        return []

    async def load_attributes(self, a):
        return json.loads(json.dumps(self._a))

    async def load_statistics(self, a):
        return {}

    async def load_maintenance(self, a):
        return {}


def _prog(pr):
    return {"description": "d", "protocolType": "MQTT",
            "parameters": {"prCode": {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": pr}}}


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
        from custom_components.addhon.client import pyhon_adapter
        app = pyhon_adapter._native_engine_appliance_cls()(DictApi(_PN_COMMANDS, attrs), dict(_PN_INFO), zone=0)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(app.load_commands())
        loop.run_until_complete(app.load_attributes())
        return app

    def test_prcode5_is_super_freeze(self) -> None:
        self.assertEqual(self._build(_shadow("5")).attributes["programName"], "super_freeze")

    def test_prcode0_is_no_program(self) -> None:
        # 0 è falsy -> "No Program" anche se esiste un programma id-0
        self.assertEqual(self._build(_shadow("0")).attributes["programName"], "No Program")


if __name__ == "__main__":
    unittest.main()
