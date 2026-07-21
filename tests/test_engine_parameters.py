# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

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
from decimal import Decimal
from fractions import Fraction
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
from custom_components.addhon.client.engine.commands import HonCommand  # noqa: E402

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


class RangeSnapToGridTest(unittest.TestCase):
    """snap_to_grid returns the nearest in-range grid value so an off-grid device shadow
    value (e.g. a measured 17.2 for a step-1 setpoint) can be synced into the command
    without leaving it at the default (discussion #62). The setter still raises on the raw
    off-grid value -- snap is applied explicitly, never implicitly in the setter."""

    def _range(self, lo, hi, step):
        return NaRange("temp", {"category": "command", "typology": "range",
                                "mandatory": 0, "minimumValue": lo, "maximumValue": hi,
                                "incrementValue": step, "defaultValue": lo}, "grp")

    def test_snaps_off_grid_to_nearest(self) -> None:
        p = self._range("5", "20", "1")
        self.assertEqual(p.snap_to_grid("17.2"), 17)   # the #62 case
        self.assertEqual(p.snap_to_grid("12.2"), 12)
        self.assertEqual(p.snap_to_grid(17.6), 18)     # rounds to nearest

    def test_out_of_range_raises_not_clamped(self) -> None:
        # An out-of-range shadow value (stale metadata) must NOT be clamped to a boundary:
        # clamping + a later send would overwrite the command's current (possibly user-set)
        # value. snap raises so sync_params_to_command skips it and keeps the command value.
        p = self._range("5", "20", "1")
        with self.assertRaises(ValueError):
            p.snap_to_grid("2")    # below min
        with self.assertRaises(ValueError):
            p.snap_to_grid("99")   # above max

    def test_non_numeric_raises(self) -> None:
        p = self._range("5", "20", "1")
        with self.assertRaises(ValueError):
            p.snap_to_grid("bad")

    def test_on_grid_value_unchanged(self) -> None:
        p = self._range("16", "30", "0.5")
        self.assertEqual(p.snap_to_grid("22.5"), 22.5)  # already on grid, exact

    def test_off_grid_max_never_exceeds_max(self) -> None:
        # max not on the min/step grid: snapping a near-max value must stay <= max and land
        # on the grid (last valid point), never round UP past max (which the setter would
        # then reject, re-creating the #62 default-fallback). Grid tops out below max.
        p = self._range("5", "22", "3")            # grid: 5,8,11,14,17,20 (23 > 22)
        self.assertEqual(p.snap_to_grid("22"), 20)
        p.value = p.snap_to_grid("22")             # must be setter-accepted
        self.assertEqual(p.value, 20)
        q = self._range("0", "9.6", "1")           # grid: 0..9 (10 > 9.6)
        self.assertEqual(q.snap_to_grid("9.6"), 9)
        q.value = q.snap_to_grid("9.6")
        self.assertEqual(q.value, 9)

    def test_snapped_value_is_accepted_by_setter(self) -> None:
        # the whole point: the snapped result must pass the setter that rejected the raw one
        p = self._range("5", "20", "1")
        with self.assertRaises(ValueError):
            p.value = "17.2"
        p.value = p.snap_to_grid("17.2")
        self.assertEqual(p.value, 17)


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


def _mk_range(lo, hi, step):
    return NaRange("temp", {"category": "command", "typology": "range",
                            "mandatory": 0, "minimumValue": lo, "maximumValue": hi,
                            "incrementValue": step, "defaultValue": lo}, "grp")


def _mk_enum(values, default=None):
    data = {"category": "command", "typology": "enum", "mandatory": 1,
            "enumValues": list(values)}
    if default is not None:
        data["defaultValue"] = default
    return NaEnum("mode", data, "grp")


def _mk_fixed(value):
    return NaFixed("fx", {"category": "command", "typology": "fixed",
                          "mandatory": 0, "fixedValue": value}, "grp")


# (lo, hi, step) plus expected reachable-point count. Covers fractional steps,
# endpoints, integer ranges and the extreme 1e6 magnitude / tiny step case.
_GRID_MATRIX = [
    ("16", "30", "0.1", 141),
    ("20", "25", "0.5", 11),
    ("0", "1", "0.001", 1001),
    ("0", "40", "0.125", 321),
    ("-10", "10", "0.25", 81),
    ("100000", "100010", "0.001", 10001),
    ("2", "8", "1", 7),
    ("-24", "-16", "1", 9),
    ("0", "5", "1", 6),
]


class RangeGridEpsilonF1Test(unittest.TestCase):
    """F1: the snap tolerance is now capped at step/4, so it can never exceed the
    half-step off-grid distance -- not even on a pathological schema where the
    magnitude-scaled 1e-9 term alone would (min 0, max 1e6, step 0.001 -> old eps 1e-3
    > step/2 = 5e-4). The setter's off-grid -> ValueError contract (the rollback depends
    on it) must hold unconditionally, WITHOUT introducing a false negative on any
    realistic on-grid value."""

    def test_extreme_off_grid_rejected(self) -> None:
        # Pre-fix (uncapped eps = 1e-3) accepted this off-grid value; capped eps = 2.5e-4
        # rejects it. Oracle-confirmed the old code accepted 0.0005.
        p = _mk_range("0", "1000000", "0.001")
        with self.assertRaises(ValueError):
            p.value = "0.0005"

    def test_extreme_on_grid_still_accepted(self) -> None:
        # The cap must not turn a real on-grid value into a false negative.
        p = _mk_range("0", "1000000", "0.001")
        p.value = "0.001"
        self.assertEqual(p.value, 0.001)
        p2 = _mk_range("0", "1000000", "0.001")
        p2.value = "999999.999"
        self.assertEqual(p2.value, 999999.999)

    def test_eps_below_half_step_on_every_matrix_range(self) -> None:
        for lo, hi, step, _n in _GRID_MATRIX:
            p = _mk_range(lo, hi, step)
            self.assertLess(p._grid_eps(p.step), p.step / 2)

    def test_fraction_oracle_zero_fn_zero_fp(self) -> None:
        """Exact fractions.Fraction oracle: every exact grid point is accepted and
        every exact half-step midpoint in range is rejected (0 false negatives / 0
        false positives) across the realistic AND extreme space."""
        for lo, hi, step in [(m[0], m[1], m[2]) for m in _GRID_MATRIX] + [
            ("0", "1000000", "0.001")
        ]:
            f_lo, f_hi, f_step = Fraction(lo), Fraction(hi), Fraction(step)
            d_lo, d_step = Decimal(lo), Decimal(step)
            n = int((f_hi - f_lo) / f_step) + 1
            # Sample indices so the 1e6/0.001 case (1e9 points) stays fast, always
            # including the endpoints.
            stride = max(1, n // 60)
            indices = sorted(set(list(range(0, n, stride)) + [n - 1]))
            for i in indices:
                gp = str(d_lo + i * d_step)  # exact on-grid decimal string
                p = _mk_range(lo, hi, step)
                try:
                    p.value = gp
                except ValueError:  # pragma: no cover - would be a false negative
                    self.fail(f"false negative: {lo}..{hi}/{step} rejected on-grid {gp}")
                if i < n - 1:
                    mid = str(d_lo + (Decimal(i) + Decimal("0.5")) * d_step)
                    pm = _mk_range(lo, hi, step)
                    with self.assertRaises(
                        ValueError,
                        msg=f"false positive: {lo}..{hi}/{step} accepted off-grid {mid}",
                    ):
                        pm.value = mid


class RangeOptionCountF2Test(unittest.TestCase):
    """F2: option_count() counts the reachable grid points ARITHMETICALLY (no string
    materialization), and must equal len(values()) in every case -- values() is built
    from range(_grid_count(step)), so they share the exact same capped epsilon and
    _MAX_RANGE_VALUES bound."""

    def test_option_count_equals_len_values_matrix(self) -> None:
        for lo, hi, step, expected in _GRID_MATRIX:
            p = _mk_range(lo, hi, step)
            self.assertEqual(p.option_count(), len(p.values), f"{lo}..{hi}/{step}")
            self.assertEqual(p.option_count(), expected, f"{lo}..{hi}/{step}")

    def test_cap_boundary_matches_and_saturates(self) -> None:
        for lo, hi, step in [("0", "1000", "0.001"), ("0", "100", "0.001")]:
            p = _mk_range(lo, hi, step)
            self.assertEqual(p.option_count(), _MAX_RANGE_VALUES)
            self.assertEqual(len(p.values), _MAX_RANGE_VALUES)

    def test_inverted_range_is_zero(self) -> None:
        p = _mk_range("10", "0", "1")
        self.assertEqual(p.option_count(), 0)
        self.assertEqual(p.values, [])

    def test_single_point_range(self) -> None:
        p = _mk_range("5", "5", "1")
        self.assertEqual(p.option_count(), 1)
        self.assertEqual(p.values, ["5"])

    def test_non_positive_step_no_zero_division(self) -> None:
        p = _mk_range("0", "10", "-1")
        self.assertEqual(p.option_count(), 1)
        self.assertEqual(p.values, ["0"])

    def test_base_option_count_enum_and_fixed(self) -> None:
        enum = _mk_enum(["A", "B", "C"], default="A")
        self.assertEqual(enum.option_count(), 3)
        self.assertEqual(enum.option_count(), len(enum.values))
        fixed = _mk_fixed("cool")
        self.assertEqual(fixed.option_count(), 1)
        self.assertEqual(fixed.option_count(), len(fixed.values))

    def test_more_options_selection_preserved(self) -> None:
        # _more_options must still pick the parameter with MORE options; ties keep first.
        fixed = _mk_fixed("cool")
        range11 = _mk_range("20", "25", "0.5")   # 11 options
        range5 = _mk_range("0", "2", "0.5")       # 5 options
        enum3 = _mk_enum(["A", "B", "C"], default="A")
        self.assertIs(HonCommand._more_options(fixed, range11), range11)
        self.assertIs(HonCommand._more_options(range11, fixed), range11)
        self.assertIs(HonCommand._more_options(enum3, range11), range11)
        self.assertIs(HonCommand._more_options(range11, enum3), range11)
        self.assertIs(HonCommand._more_options(range11, range5), range11)
        enum3b = _mk_enum(["X", "Y", "Z"], default="X")
        self.assertIs(HonCommand._more_options(enum3, enum3b), enum3)  # tie -> first


class RangeValuesDriftF3Test(unittest.TestCase):
    """F3: values() rounds each grid point to the step/min decimal precision before
    str(), so decimal drift ("24.200000000000003") is gone -- WITHOUT touching integer
    ranges (golden) or the setter's stored value / intern_value."""

    def test_no_float_drift_in_labels(self) -> None:
        p = _mk_range("16", "30", "0.1")
        v = p.values
        self.assertEqual(len(v), 141)
        self.assertEqual(v[0], "16.0")
        self.assertEqual(v[-1], "30.0")
        self.assertIn("24.2", v)
        self.assertNotIn("24.200000000000003", v)
        for label in v:
            frac = label.split(".")[1] if "." in label else ""
            self.assertLessEqual(len(frac), 6, label)

    def test_half_step_list_exact(self) -> None:
        p = _mk_range("20", "25", "0.5")
        self.assertEqual(
            p.values,
            ["20.0", "20.5", "21.0", "21.5", "22.0", "22.5",
             "23.0", "23.5", "24.0", "24.5", "25.0"],
        )

    def test_quarter_eighth_step_three_decimals(self) -> None:
        p = _mk_range("0", "1", "0.125")
        self.assertEqual(
            p.values,
            ["0.0", "0.125", "0.25", "0.375", "0.5",
             "0.625", "0.75", "0.875", "1.0"],
        )

    def test_integer_ranges_byte_identical(self) -> None:
        # Integer step -> ndigits 0 -> bare integer strings (never "2.0"); this is what
        # the golden fridge ranges rely on.
        self.assertEqual(_mk_range("2", "8", "1").values,
                         ["2", "3", "4", "5", "6", "7", "8"])
        self.assertEqual(_mk_range("-24", "-16", "1").values,
                         ["-24", "-23", "-22", "-21", "-20", "-19", "-18", "-17", "-16"])
        self.assertEqual(_mk_range("0", "5", "1").values,
                         ["0", "1", "2", "3", "4", "5"])

    def test_min_finer_than_step_not_collapsed(self) -> None:
        # ndigits = max(decimals(min), decimals(step)); a fractional min with an integer
        # step must keep its .5 points (not banker's-round to 16/18).
        p = _mk_range("16.5", "20.5", "1")
        self.assertEqual(p.values, ["16.5", "17.5", "18.5", "19.5", "20.5"])

    def test_setter_intern_value_untouched(self) -> None:
        # F3 must alter ONLY values() output, never the setter's stored value.
        p = _mk_range("20", "25", "0.5")
        p.value = 22.5
        self.assertEqual(p.value, 22.5)
        self.assertEqual(p.intern_value, "22.5")
        for v in ("24", 24, 24.0):
            q = _mk_range("20", "25", "0.5")
            q.value = v
            self.assertEqual(q.value, 24)
            self.assertEqual(q.intern_value, "24")
        r = _mk_range("20", "25", "0.5")
        r.value = "22,5"
        self.assertEqual(r.value, 22.5)

    def test_decimals_derivation_robust(self) -> None:
        self.assertEqual(NaRange._decimals(0.1), 1)
        self.assertEqual(NaRange._decimals(0.125), 3)
        self.assertEqual(NaRange._decimals(0.001), 3)
        self.assertEqual(NaRange._decimals(0.25), 2)
        self.assertEqual(NaRange._decimals(0.5), 1)
        self.assertEqual(NaRange._decimals(1), 0)
        self.assertEqual(NaRange._decimals(5), 0)
        self.assertEqual(NaRange._decimals(16.0), 0)
        # decimal-comma "5,5" schema resolves to the float 5.5 via str_to_float -> 1.
        self.assertEqual(NaRange._decimals(5.5), 1)
        p = _mk_range("0", "22", "5,5")
        self.assertEqual(p.step, 5.5)
        self.assertEqual(p.values, ["0.0", "5.5", "11.0", "16.5", "22.0"])


if __name__ == "__main__":
    unittest.main()
