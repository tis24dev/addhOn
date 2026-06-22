"""Golden test of the native parameters (Phase 4). Reuses the 67 REAL fridge
parameters (apk/dump/ref_10136/commands.json: range+enum+fixed) and freezes their
construction + setter.

History: it used to be a differential test vs pyhOn+BABYCARE patch; with `_vendor/`
deleted it became golden (the native output was proven == pyhOn at checkpoint 5a).
The BABYCARE fix is native in the enum; the enum-edge divergences stay pinned below.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import REPO, frozen, install_stubs  # noqa: E402

install_stubs()
_DUMP = REPO / "tests" / "fixtures" / "ref_10136" / "commands.json"

from custom_components.addhon.client.engine.parameter.range import HonParameterRange as NaRange  # noqa: E402
from custom_components.addhon.client.engine.parameter.enum import HonParameterEnum as NaEnum  # noqa: E402
from custom_components.addhon.client.engine.parameter.fixed import HonParameterFixed as NaFixed  # noqa: E402

_NA = {"range": NaRange, "enum": NaEnum, "fixed": NaFixed}


def _walk_params(node, out):
    if isinstance(node, dict):
        if node.get("typology") in _NA and "category" in node:
            out.append(node)
            return
        for v in node.values():
            _walk_params(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_params(v, out)


def _load_real_params():
    data = json.loads(_DUMP.read_text(encoding="utf-8"))
    out: list = []
    for key in ("settings", "stopProgram", "startProgram"):
        _walk_params(data.get(key, {}), out)
    return out


def _snap(p, typ):
    s = {
        "key": p.key, "category": p.category, "typology": p.typology,
        "mandatory": p.mandatory, "group": p.group, "value": p.value,
        "intern_value": p.intern_value, "values": list(p.values),
    }
    if typ == "range":
        s["min"], s["max"], s["step"] = p.min, p.max, p.step
    return s


def _native_snapshot():
    params = _load_real_params()
    out = {"by_typ": {}, "items": []}
    for d in params:
        t = d["typology"]
        out["by_typ"][t] = out["by_typ"].get(t, 0) + 1
        item = {"construct": _snap(_NA[t]("k", dict(d), "grp"), t)}
        # setter on the valid values: resulting (value, intern_value)
        na = _NA[t]("k", dict(d), "grp")
        setter = []
        for v in list(na.values):
            na.value = v
            setter.append([na.value, na.intern_value])
        item["setter_valid"] = setter
        # setter on an invalid value
        if t == "fixed":
            item["setter_invalid"] = "n/a"
        else:
            na2 = _NA[t]("k", dict(d), "grp")
            try:
                na2.value = "___definitely_not_allowed___"
                item["setter_invalid"] = "accepted"
            except ValueError:
                item["setter_invalid"] = "ValueError"
        if t == "range":
            # NUMERIC probes of the range setter: out-of-range and off-step. Without
            # these, the only invalid is a non-numeric string that already raises in
            # str_to_float (before the min/max/step checks) -> bound/step regressions
            # would be invisible.
            probes: dict = {}
            nr = _NA[t]("k", dict(d), "grp")
            try:
                nr.value = nr.max + (nr.step or 1) * 1000
                probes["out_of_range"] = "accepted"
            except ValueError:
                probes["out_of_range"] = "ValueError"
            nr2 = _NA[t]("k", dict(d), "grp")
            try:
                nr2.value = str(nr2.min + 0.5)  # string: avoids str_to_float's int truncation
                probes["off_step"] = "accepted"
            except ValueError:
                probes["off_step"] = "ValueError"
            item["range_probes"] = probes
        out["items"].append(item)
    return out


class ParameterGoldenTest(unittest.TestCase):
    def test_dump_has_all_typologies(self) -> None:
        snap = _native_snapshot()
        self.assertTrue(snap["items"])
        for t in ("range", "enum", "fixed"):
            self.assertIn(t, snap["by_typ"])

    def test_native_params_match_golden(self) -> None:
        snap = _native_snapshot()
        self.assertEqual(snap, frozen("engine_parameters", snap))


class NativeEnumEdgeBehaviorTest(unittest.TestCase):
    """Intended NATIVE behavior on the enum edges (BABYCARE fix + pinned divergences)."""

    def test_babycare_cased_value_accepted(self) -> None:
        data = {"category": "command", "typology": "enum", "mandatory": 1,
                "defaultValue": "OFF", "enumValues": ["OFF", "BABYCARE", "ECO"]}
        na = NaEnum("mode", dict(data), "grp")
        # accepts both the cloud casing and the clean one; value normalizes, intern_value stays raw
        na.value = "BABYCARE"
        self.assertEqual(na.value, "babycare")
        self.assertEqual(na.intern_value, "BABYCARE")
        na.value = "eco"
        self.assertEqual(na.value, "eco")

    def test_trigger_fires_on_cased_accepted_value(self) -> None:
        data = {"category": "command", "typology": "enum", "mandatory": 1,
                "defaultValue": "OFF", "enumValues": ["OFF", "BABYCARE"]}
        na = NaEnum("mode", dict(data), "grp")
        fired = []
        na.add_trigger("babycare", lambda d: fired.append(d), object())
        na.value = "BABYCARE"
        self.assertEqual(len(fired), 1)

    def test_string_enumvalues_normalized_to_list(self) -> None:
        # enumValues as the string "cold|hot" + default outside the list: previously
        # `.append` on a str raised AttributeError during construction. Now it is
        # normalized to a list.
        data = {"category": "command", "typology": "enum", "mandatory": 1,
                "defaultValue": "warm", "enumValues": "cold|hot"}
        na = NaEnum("mode", dict(data), "grp")
        self.assertEqual(na.values, ["cold", "hot", "warm"])
        na.value = "cold"
        self.assertEqual(na.value, "cold")

    def test_pipe_string_enum_native_rejects_substring(self) -> None:
        data = {"category": "command", "typology": "enum", "mandatory": 1,
                "defaultValue": "", "enumValues": "A|B|C"}
        na = NaEnum("k", dict(data), "grp")
        with self.assertRaises(ValueError):
            na.value = "A|B|C"


class RangeSetterHardeningTest(unittest.TestCase):
    """ITEM A: a fractional float assigned DIRECTLY to the range setter must not be
    truncated. The setter delegated to str_to_float, whose int()-first quirk turned a
    raw 22.5 into 22 silently (the golden never hit this: range.values yields strings).
    Integer-valued inputs must stay int so intern_value is clean ("24", never "24.0")."""

    def _range(self, lo="20", hi="25", step="0.5"):
        return NaRange("temp", {"category": "command", "typology": "range",
                                "mandatory": 0, "minimumValue": lo, "maximumValue": hi,
                                "incrementValue": step, "defaultValue": lo}, "grp")

    def test_fractional_float_not_truncated(self) -> None:
        p = self._range()
        p.value = 22.5  # FLOAT passed directly, not the documented string
        self.assertEqual(p.value, 22.5)
        self.assertEqual(p.intern_value, "22.5")

    def test_integer_valued_inputs_stay_int_and_clean(self) -> None:
        # str "24", int 24 and float 24.0 must all store int 24 -> intern "24", no "24.0".
        for v in ("24", 24, 24.0):
            p = self._range()
            p.value = v
            self.assertEqual(p.value, 24)
            self.assertEqual(p.intern_value, "24")

    def test_off_grid_float_raises_instead_of_truncating(self) -> None:
        # 22.3 is off the 0.5 grid: it must raise, not be truncated to 22 and accepted.
        p = self._range()
        with self.assertRaises(ValueError):
            p.value = 22.3

    def test_decimal_comma_string_still_preserved(self) -> None:
        p = self._range()
        p.value = "22,5"  # cloud decimal comma -> 22.5 (string path unchanged)
        self.assertEqual(p.value, 22.5)


if __name__ == "__main__":
    unittest.main()
