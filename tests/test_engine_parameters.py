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

from custom_components.addhon.client.engine.parameter.range import (  # noqa: E402
    HonParameterRange as NaRange,
    _MAX_RANGE_VALUES,
)
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


class RangeGridSetterTest(unittest.TestCase):
    """Regression for the x100 modulo grid-check bug: an on-grid setpoint with a
    non-zero min and a decimal step (e.g. 20.1 on 20..25 step 0.1) was wrongly
    rejected with a ValueError, which the write path (climate.py / number.py) reads as
    a failed set and SILENTLY rolls the user's value back. The replacement snap-to-index
    grid-check accepts every real on-grid value while still rejecting off-grid /
    out-of-range ones."""

    def _range(self, lo, hi, step):
        return NaRange("temp", {"category": "command", "typology": "range",
                                "mandatory": 0, "minimumValue": lo, "maximumValue": hi,
                                "incrementValue": step, "defaultValue": lo}, "grp")

    # --- values that USED to be wrongly rejected (the actual bug) ---
    def test_decimal_min_nonzero_accept_string(self) -> None:
        p = self._range("20", "25", "0.1")
        p.value = "20.1"  # on-grid, non-zero min, decimal step -> was ValueError
        self.assertEqual(p.value, 20.1)
        self.assertEqual(p.intern_value, "20.1")

    def test_decimal_min_nonzero_accept_direct_float(self) -> None:
        p = self._range("20", "25", "0.1")
        p.value = 20.1  # fractional float assigned directly, must not truncate to 20
        self.assertEqual(p.value, 20.1)
        self.assertEqual(p.intern_value, "20.1")

    def test_16_30_step_01_accept(self) -> None:
        p = self._range("16", "30", "0.1")
        p.value = "16.3"  # was ValueError
        self.assertEqual(p.value, 16.3)
        self.assertEqual(p.intern_value, "16.3")

    def test_three_decimals_accept(self) -> None:
        p = self._range("0", "1", "0.001")
        p.value = "0.003"  # >2 decimals was ValueError under the x100 trick
        self.assertEqual(p.value, 0.003)
        self.assertEqual(p.intern_value, "0.003")

    # --- genuinely off-grid values must STILL raise ValueError (rollback contract) ---
    def test_off_grid_half_step_string_rejected(self) -> None:
        p = self._range("16", "30", "0.1")
        with self.assertRaises(ValueError):
            p.value = "16.35"

    def test_off_grid_three_decimals_rejected(self) -> None:
        p = self._range("0", "1", "0.001")
        with self.assertRaises(ValueError):
            p.value = "0.0035"

    def test_off_grid_direct_float_rejected_not_truncated(self) -> None:
        # 22.3 is off the 0.5 grid: must raise, not be silently truncated to 22.
        p = self._range("20", "25", "0.5")
        with self.assertRaises(ValueError):
            p.value = 22.3

    # --- out-of-range still rejected, on-grid boundary still accepted ---
    def test_out_of_range_rejected(self) -> None:
        p = self._range("20", "25", "0.5")
        with self.assertRaises(ValueError):
            p.value = 25.5

    def test_boundary_max_on_grid_accepted(self) -> None:
        p = self._range("20", "25", "0.5")
        p.value = "25.0"
        self.assertEqual(p.value, 25.0)

    # --- intern_value invariant: integer-valued inputs stay clean ("24", not "24.0") ---
    def test_integer_inputs_clean_intern(self) -> None:
        for v in ("24", 24, 24.0):
            p = self._range("20", "25", "0.5")
            p.value = v
            self.assertEqual(p.value, 24)
            self.assertEqual(p.intern_value, "24")

    def test_decimal_comma_preserved(self) -> None:
        p = self._range("20", "25", "0.5")
        p.value = "22,5"  # cloud decimal comma
        self.assertEqual(p.value, 22.5)
        self.assertEqual(p.intern_value, "22.5")

    # --- negative-min integer grid ---
    def test_negative_min_integer_grid(self) -> None:
        p = self._range("-24", "-16", "1")
        p.value = -20
        self.assertEqual(p.value, -20)
        self.assertEqual(p.intern_value, "-20")
        p2 = self._range("-24", "-16", "1")
        with self.assertRaises(ValueError):
            p2.value = "-20.5"

    # --- malformed negative step: no ZeroDivisionError, no spurious reject ---
    def test_malformed_negative_step_no_crash(self) -> None:
        p = self._range("0", "10", "-1")
        self.assertEqual(p.step, -1)  # step property keeps a genuine negative
        p.value = "5"  # in-range: accepted via the step<=0 branch, no crash
        self.assertEqual(p.value, 5)

    # --- values(): index-based, no dropped final point, no unbounded loop ---
    def test_values_decimal_endpoints_and_length(self) -> None:
        p = self._range("16", "30", "0.1")
        v = p.values
        self.assertEqual(len(v), 141)
        self.assertEqual(v[0], "16.0")
        self.assertEqual(v[-1], "30.0")  # final point no longer dropped / drifted

    def test_values_half_step_range(self) -> None:
        p = self._range("20", "25", "0.5")
        self.assertEqual(
            p.values,
            ["20.0", "20.5", "21.0", "21.5", "22.0", "22.5",
             "23.0", "23.5", "24.0", "24.5", "25.0"],
        )

    def test_values_bounded_on_malformed_range(self) -> None:
        # tiny step over a huge span must not loop unbounded.
        p = self._range("0", "1000", "0.001")
        self.assertLessEqual(len(p.values), _MAX_RANGE_VALUES)


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
