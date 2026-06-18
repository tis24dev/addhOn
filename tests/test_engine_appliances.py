"""Test del layer per-tipo nativo (Fase 4 slice 4) vs pyhОn.

Le `_extra` per-tipo derivano campi client-side (programName, modi, active/pause,
available) e ritoccano le settings (dryLevel). Oracolo = le `_extra` di pyhОn, MA con
due categorie di differenze VOLUTE (documentate in apk/analysis/per-type-derivations.md):
  - FIX di no-op pyhОn: `HonAttribute == "x"` è sempre False (manca __eq__), quindi
    pyhОn aveva modeZ1/Z2 = sempre no_mode, pause/wh-active = sempre False. Native
    confronta per VALORE (intento app) -> corretto. Campi NON consumati -> divergenza
    inerte, qui PINNATA.
  - MIGLIORIE app: dryLevel nascosto anche per '0' (non solo '11'); `available`
    first-class.
I campi CONSUMATI dall'integrazione (programName, settings dryLevel su param nativi,
zeroing offline, ov-active) restano a PARITA' (o corretti dove pyhОn si romperebbe coi
parametri nativi post-flip).

HA/aiohttp/yarl stubati.
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
_DUMP = REPO / "apk" / "dump" / "ref_10136"


def _mod(name: str) -> types.ModuleType:
    m = sys.modules.get(name)
    if m is None:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return m


def _install_stubs() -> None:
    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exc = _mod("homeassistant.exceptions")
    base = getattr(exc, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exc.HomeAssistantError = base
    exc.ConfigEntryNotReady = getattr(exc, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base,), {}))
    exc.ConfigEntryAuthFailed = getattr(exc, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base,), {}))
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))
    ha = _mod("homeassistant")
    ha.config_entries, ha.core, ha.exceptions = ce, core, exc
    ha.helpers = _mod("homeassistant.helpers")
    ha.helpers.update_coordinator = uc
    yarl = _mod("yarl")
    if not hasattr(yarl, "URL"):
        yarl.URL = type("URL", (), {"__init__": lambda self, s, encoded=False: None})
    aio = _mod("aiohttp")
    aio.ClientSession = getattr(aio, "ClientSession", type("ClientSession", (), {}))
    aio.ClientResponse = getattr(aio, "ClientResponse", type("ClientResponse", (), {}))
    aio.ContentTypeError = getattr(aio, "ContentTypeError", type("ContentTypeError", (Exception,), {}))
    aio.client = _mod("aiohttp.client")
    aio.client._RequestContextManager = type("_RCM", (), {})


_install_stubs()

from custom_components.addhon._vendor.pyhon.attributes import HonAttribute  # noqa: E402
from custom_components.addhon._vendor.pyhon.parameter.fixed import HonParameterFixed as PyFixed  # noqa: E402
from custom_components.addhon.client.engine.parameter.fixed import HonParameterFixed as NaFixed  # noqa: E402
from custom_components.addhon.client.engine.appliances import registry as native_registry  # noqa: E402

# Extra per-tipo: pyhОn (oracolo) e native
import custom_components.addhon._vendor.pyhon.appliances as _py_app  # noqa: E402
import custom_components.addhon.client.engine.appliances as _na_app  # noqa: E402


class FakeParent:
    def __init__(self, connection=True, settings=None, appliance_type="TD") -> None:
        self.connection = connection
        self.settings = settings or {}
        self.appliance_type = appliance_type


def _params(d: dict) -> dict:
    return {k: HonAttribute({"parNewVal": v}) for k, v in d.items()}


def _val(params: dict, key: str):
    return params[key].value if key in params else None


def _run(extra_cls, params_dict, *, connection=True, last_conn=None, activity=None):
    parent = FakeParent(connection=connection)
    params = _params(params_dict)
    data = {"parameters": params}
    if last_conn is not None:
        data["lastConnEvent"] = {"category": last_conn}
    if activity is not None:
        data["activity"] = activity
    out = extra_cls(parent).attributes(data)
    return out, params


def _py(name):
    return getattr(__import__(f"custom_components.addhon._vendor.pyhon.appliances.{name}", fromlist=["Appliance"]), "Appliance")


def _na(name):
    return getattr(__import__(f"custom_components.addhon.client.engine.appliances.{name}", fromlist=["Appliance"]), "Appliance")


class RegistryTest(unittest.TestCase):
    def test_known_types_mapped(self) -> None:
        for t in ("ref", "td", "wm", "wd", "dw", "ov", "wh", "wc"):
            extra = native_registry.get_extra(FakeParent(appliance_type=t.upper()))
            self.assertIsNotNone(extra, f"manca extra per {t}")
            self.assertEqual(extra.__module__.rsplit(".", 1)[-1], t)

    def test_unknown_type_none(self) -> None:
        self.assertIsNone(native_registry.get_extra(FakeParent(appliance_type="XYZ")))

    def test_case_insensitive(self) -> None:
        self.assertIsNotNone(native_registry.get_extra(FakeParent(appliance_type="Ref")))


class ParityFieldsTest(unittest.TestCase):
    """I campi consumati/funzionanti restano a parità native vs pyhОn."""

    def test_active_bool_activity_parity(self) -> None:
        for name in ("td", "wm", "wd", "dw"):
            po, _ = _run(_py(name), {"machMode": "2"}, activity={"x": 1})
            no, _ = _run(_na(name), {"machMode": "2"}, activity={"x": 1})
            self.assertEqual(no["active"], po["active"], name)
            self.assertTrue(no["active"], name)
            # senza activity -> active False su entrambe
            po2, _ = _run(_py(name), {"machMode": "2"})
            no2, _ = _run(_na(name), {"machMode": "2"})
            self.assertEqual(no2["active"], po2["active"], name)

    def test_offline_zeroing_parity(self) -> None:
        # td/wd/dw: machMode -> "0" quando connection=False
        for name in ("td", "wd", "dw"):
            _, pp = _run(_py(name), {"machMode": "5"}, connection=False, activity={})
            _, np = _run(_na(name), {"machMode": "5"}, connection=False, activity={})
            self.assertEqual(_val(np, "machMode"), _val(pp, "machMode"), name)
            self.assertEqual(_val(np, "machMode"), 0, name)
        # wm: machMode -> "0" quando lastConnEvent DISCONNECTED
        _, pp = _run(_py("wm"), {"machMode": "5"}, last_conn="DISCONNECTED", activity={})
        _, np = _run(_na("wm"), {"machMode": "5"}, last_conn="DISCONNECTED", activity={})
        self.assertEqual(_val(np, "machMode"), _val(pp, "machMode"))
        self.assertEqual(_val(np, "machMode"), 0)

    def test_ov_active_and_offline_parity(self) -> None:
        # online, onOffStatus=1 -> active True su entrambe (pyhОn usava .value==1, ok)
        po, _ = _run(_py("ov"), {"onOffStatus": "1", "temp": "50"})
        no, _ = _run(_na("ov"), {"onOffStatus": "1", "temp": "50"})
        self.assertEqual(no["active"], po["active"])
        self.assertTrue(no["active"])
        # offline -> zera temp/onOffStatus, active False su entrambe
        po2, pp = _run(_py("ov"), {"onOffStatus": "1", "temp": "50", "remoteCtrValid": "1", "remainingTimeMM": "30"}, connection=False)
        no2, np = _run(_na("ov"), {"onOffStatus": "1", "temp": "50", "remoteCtrValid": "1", "remainingTimeMM": "30"}, connection=False)
        self.assertEqual(no2["active"], po2["active"])
        self.assertFalse(no2["active"])
        self.assertEqual(_val(np, "temp"), _val(pp, "temp"))
        self.assertEqual(_val(np, "onOffStatus"), _val(pp, "onOffStatus"))

    def test_ref_all_flags_off_parity(self) -> None:
        # tutti i flag a 0: modeZ1/Z2 = no_mode su entrambe (qui coincidono)
        flags = {"holidayMode": "0", "intelligenceMode": "0", "quickModeZ1": "0", "quickModeZ2": "0"}
        po, _ = _run(_py("ref"), flags)
        no, _ = _run(_na("ref"), flags)
        self.assertEqual((no["modeZ1"], no["modeZ2"]), (po["modeZ1"], po["modeZ2"]))
        self.assertEqual(no["modeZ1"], "no_mode")


class NativeFixesTest(unittest.TestCase):
    """Pinna i FIX native dei no-op di pyhОn (campi non consumati): divergenza voluta."""

    def test_ref_modes_fixed_by_value(self) -> None:
        po, _ = _run(_py("ref"), {"holidayMode": "1", "intelligenceMode": "0", "quickModeZ1": "0", "quickModeZ2": "0"})
        no, _ = _run(_na("ref"), {"holidayMode": "1", "intelligenceMode": "0", "quickModeZ1": "0", "quickModeZ2": "0"})
        self.assertEqual(no["modeZ1"], "holiday")          # native corretto
        self.assertEqual(po["modeZ1"], "no_mode")          # pyhОn rotto (no __eq__)
        # super_freeze su Z2
        no2, _ = _run(_na("ref"), {"holidayMode": "0", "intelligenceMode": "0", "quickModeZ1": "0", "quickModeZ2": "1"})
        self.assertEqual(no2["modeZ2"], "super_freeze")
        # auto_set su entrambe le zone
        no3, _ = _run(_na("ref"), {"holidayMode": "0", "intelligenceMode": "1", "quickModeZ1": "0", "quickModeZ2": "0"})
        self.assertEqual((no3["modeZ1"], no3["modeZ2"]), ("auto_set", "auto_set"))

    def test_ref_z1_precedence(self) -> None:
        # holiday vince su super_cool quando ENTRAMBI i flag sono attivi: pinna l'ordine
        # Z1 (scelta documentata, ordine pyhОn; l'app inverte ma il campo è inerte) e
        # copre il ramo super_cool (altrimenti dark).
        no, _ = _run(_na("ref"), {"holidayMode": "1", "quickModeZ1": "1", "intelligenceMode": "0", "quickModeZ2": "0"})
        self.assertEqual(no["modeZ1"], "holiday")
        no2, _ = _run(_na("ref"), {"holidayMode": "0", "intelligenceMode": "0", "quickModeZ1": "1", "quickModeZ2": "0"})
        self.assertEqual(no2["modeZ1"], "super_cool")
        # Z2: super_freeze (quickModeZ2) vince su auto_set (intelligenceMode) se entrambi
        no3, _ = _run(_na("ref"), {"holidayMode": "0", "intelligenceMode": "1", "quickModeZ1": "0", "quickModeZ2": "1"})
        self.assertEqual(no3["modeZ2"], "super_freeze")

    def test_pause_fixed_by_value(self) -> None:
        for name in ("td", "wm", "wd"):
            kw = {"last_conn": "CONNECTED"} if name == "wm" else {}
            po, _ = _run(_py(name), {"machMode": "3"}, activity={"x": 1}, **kw)
            no, _ = _run(_na(name), {"machMode": "3"}, activity={"x": 1}, **kw)
            self.assertTrue(no["pause"], name)             # native corretto
            self.assertFalse(po["pause"], name)            # pyhОn rotto

    def test_wh_active_fixed_by_value(self) -> None:
        po, _ = _run(_py("wh"), {"onOffStatus": "1"})
        no, _ = _run(_na("wh"), {"onOffStatus": "1"})
        self.assertTrue(no["active"])                      # native corretto
        self.assertFalse(po["active"])                     # pyhОn rotto (isinstance HonParameter)

    def test_available_added(self) -> None:
        no, _ = _run(_na("td"), {"machMode": "2"}, connection=True)
        self.assertTrue(no["available"])
        no2, _ = _run(_na("td"), {"machMode": "2"}, connection=False)
        self.assertFalse(no2["available"])
        po, _ = _run(_py("td"), {"machMode": "2"})
        self.assertNotIn("available", po)                  # pyhОn non lo aggiunge

    def test_ref_robust_on_missing_flags(self) -> None:
        # native ref usa .get -> nessun crash se i flag mancano (pyhОn usa [] -> KeyError)
        no, _ = _run(_na("ref"), {"prCode": "0"})
        self.assertEqual(no["modeZ1"], "no_mode")
        self.assertEqual(no["modeZ2"], "no_mode")
        with self.assertRaises(KeyError):
            _run(_py("ref"), {"prCode": "0"})


class EdgeRobustnessTest(unittest.TestCase):
    """Branch difensivi e divergenze di robustezza vs pyhОn (documentate)."""

    def test_programname_no_program_param(self) -> None:
        # prCode != 0 ma nessun param `program` in settings -> "No Program" (ramo
        # isinstance False di base.attributes).
        parent = FakeParent(settings={}, appliance_type="WC")
        out = _na("wc")(parent).attributes({"parameters": _params({"prCode": "5"})})
        self.assertEqual(out["programName"], "No Program")

    def test_programname_empty_prcode_no_crash(self) -> None:
        # prCode vuoto: native -> "No Program" (robusto); pyhОn farebbe int("") -> ValueError.
        parent = FakeParent(settings={}, appliance_type="WC")
        out = _na("wc")(parent).attributes({"parameters": _params({"prCode": ""})})
        self.assertEqual(out["programName"], "No Program")

    def test_set_noop_on_missing_key(self) -> None:
        # td offline ma senza param machMode: native non crasha (pyhОn farebbe KeyError).
        out, params = _run(_na("td"), {"otherParam": "1"}, connection=False, activity={})
        self.assertNotIn("machMode", params)
        self.assertFalse(out["pause"])


class SettingsDryLevelTest(unittest.TestCase):
    def _td_settings(self, fixed_cls, extra_cls, value):
        s = {"startProgram.dryLevel": fixed_cls("dryLevel", {"fixedValue": value}, "g"), "keep": 1}
        extra_cls(FakeParent(appliance_type="TD")).settings(s)
        return s

    def test_drylevel_11_hidden_both(self) -> None:
        py = self._td_settings(PyFixed, _py("td"), "11")
        na = self._td_settings(NaFixed, _na("td"), "11")
        self.assertNotIn("startProgram.dryLevel", py)
        self.assertNotIn("startProgram.dryLevel", na)

    def test_drylevel_0_hidden_only_native(self) -> None:
        py = self._td_settings(PyFixed, _py("td"), "0")
        na = self._td_settings(NaFixed, _na("td"), "0")
        self.assertIn("startProgram.dryLevel", py)         # pyhОn tiene "0"
        self.assertNotIn("startProgram.dryLevel", na)      # native lo nasconde (app)

    def test_drylevel_real_value_kept_both(self) -> None:
        py = self._td_settings(PyFixed, _py("td"), "3")
        na = self._td_settings(NaFixed, _na("td"), "3")
        self.assertIn("startProgram.dryLevel", py)
        self.assertIn("startProgram.dryLevel", na)

    def test_pyhon_td_fails_on_native_param_native_succeeds(self) -> None:
        # Post-flip i param sono NATIVI: la td di pyhОn NON li riconosce (isinstance vs
        # classe pyhОn) -> non poppa = REGRESSIONE. La nostra td sì. È il motivo per cui
        # cluster (slice 3) e per-tipo (slice 4) flippano insieme.
        s_py = {"startProgram.dryLevel": NaFixed("dryLevel", {"fixedValue": "11"}, "g")}
        _py("td")(FakeParent(appliance_type="TD")).settings(s_py)
        self.assertIn("startProgram.dryLevel", s_py)       # pyhОn NON poppa il nativo
        s_na = {"startProgram.dryLevel": NaFixed("dryLevel", {"fixedValue": "11"}, "g")}
        _na("td")(FakeParent(appliance_type="TD")).settings(s_na)
        self.assertNotIn("startProgram.dryLevel", s_na)    # native poppa


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


_PN_COMMANDS = {
    "applianceModel": {"options": {}},
    "settings": {"setParameters": {"description": "d", "protocolType": "MQTT",
                                   "parameters": {"x": {"typology": "fixed", "category": "command", "mandatory": 0, "fixedValue": "1"}}}},
    "startProgram": {
        "PROGRAMS.REF.OFF": {"description": "d", "protocolType": "MQTT",
                             "parameters": {"prCode": {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": "0"}}},
        "PROGRAMS.REF.SUPER_COOL": {"description": "d", "protocolType": "MQTT",
                                    "parameters": {"prCode": {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": "1"}}},
        "PROGRAMS.REF.SUPER_FREEZE": {"description": "d", "protocolType": "MQTT",
                                      "parameters": {"prCode": {"typology": "fixed", "category": "command", "mandatory": 1, "fixedValue": "5"}}},
    },
    "dictionaryId": 1,
}
# shadow con prCode=5 -> programName atteso "super_freeze". holidayMode/intelligenceMode
# inclusi perché la ref.py di pyhОn li legge con [] (KeyError se assenti; la native usa .get).
_PN_ATTRS = {"shadow": {"parameters": {
    "prCode": {"parNewVal": "5", "lastUpdate": "2024-01-01T00:00:00"},
    "holidayMode": {"parNewVal": "0", "lastUpdate": "2024-01-01T00:00:00"},
    "intelligenceMode": {"parNewVal": "0", "lastUpdate": "2024-01-01T00:00:00"},
}}}
_PN_INFO = {"applianceTypeName": "REF", "applianceModelId": 1, "macAddress": "aa"}


# shadow con prCode=0 (programma OFF presente in ids): programName deve restare
# "No Program" perché prCode 0 è falsy (pinna il boundary `if program:` vs `is not None`).
_PN_ATTRS_ZERO = {"shadow": {"parameters": {
    "prCode": {"parNewVal": "0", "lastUpdate": "2024-01-01T00:00:00"},
    "holidayMode": {"parNewVal": "0", "lastUpdate": "2024-01-01T00:00:00"},
    "intelligenceMode": {"parNewVal": "0", "lastUpdate": "2024-01-01T00:00:00"},
}}}


class ProgramNameEndToEndTest(unittest.TestCase):
    def _build(self, cls, attrs=_PN_ATTRS):
        import asyncio
        app = cls(DictApi(_PN_COMMANDS, attrs), dict(_PN_INFO), zone=0)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(app.load_commands())
        loop.run_until_complete(app.load_attributes())
        return app

    def test_programname_parity_and_value(self) -> None:
        from custom_components.addhon._vendor.pyhon.appliance import HonAppliance
        from custom_components.addhon.client import pyhon_adapter
        pyhon_adapter.ensure_enum_patch()
        py = self._build(HonAppliance)
        na = self._build(pyhon_adapter._native_engine_appliance_cls())
        self.assertEqual(na.attributes["programName"], py.attributes["programName"])
        self.assertEqual(na.attributes["programName"], "super_freeze")

    def test_programname_zero_prcode_is_no_program(self) -> None:
        # prCode=0 con un programma id-0 negli ids: deve restare "No Program" (0 falsy),
        # non il programma id-0. Parità con pyhОn.
        from custom_components.addhon._vendor.pyhon.appliance import HonAppliance
        from custom_components.addhon.client import pyhon_adapter
        pyhon_adapter.ensure_enum_patch()
        py = self._build(HonAppliance, _PN_ATTRS_ZERO)
        na = self._build(pyhon_adapter._native_engine_appliance_cls(), _PN_ATTRS_ZERO)
        self.assertEqual(na.attributes["programName"], py.attributes["programName"])
        self.assertEqual(na.attributes["programName"], "No Program")


if __name__ == "__main__":
    unittest.main()
