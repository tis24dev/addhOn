# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Characterization of str_to_float (client/helpers.py).

It used to be a differential test vs pyhOn's str_to_float (_vendor/pyhon/helper.py);
with `_vendor/` deleted, the NATIVE characterization remains: "pinned" values that
fix the behavior (including the int() truncation quirk), proven == pyhOn during the
migration. Loaded in isolation (importlib, no package __init__, no aiohttp).
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OUR_HELPER = _ROOT / "custom_components" / "addhon" / "client" / "helpers.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load the module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParseCloudTimestampTest(unittest.TestCase):
    """parse_cloud_timestamp must turn the two cloud time shapes into the SAME
    comparable tz-aware UTC datetime and degrade gracefully (None) on bad input -- it
    runs in connectivity reconciliation and on the awscrt callback thread, neither of
    which may raise. The real cloud emits a VARIABLE number of fractional digits."""

    def setUp(self) -> None:
        self.parse = _load(_OUR_HELPER, "addhon_client_helpers2").parse_cloud_timestamp

    def test_iso_and_epoch_ms_same_instant(self) -> None:
        # The real diagnostics lastConnEvent: instantTime and timestampEvent are the SAME
        # instant in the two shapes -> must parse equal.
        a = self.parse("2026-06-25T13:35:13Z")
        b = self.parse(1782394513133)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        # within a second (timestampEvent has ms precision, instantTime is to the second)
        self.assertLess(abs((a - b).total_seconds()), 1.0)

    def test_variable_fractional_digits(self) -> None:
        # The realtime payload timestamp has a SINGLE fractional digit ("...21.1Z").
        ts = self.parse("2026-06-25T16:04:21.1Z")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.isoformat(), "2026-06-25T16:04:21.100000+00:00")

    def test_numeric_string_epoch(self) -> None:
        self.assertEqual(
            self.parse("1782394513133"), self.parse(1782394513133)
        )

    def test_result_is_tz_aware_utc(self) -> None:
        self.assertIsNotNone(self.parse("2026-06-25T13:35:13Z").tzinfo)
        # A naive ISO (no offset) is assumed UTC (the cloud stamps UTC).
        self.assertEqual(
            self.parse("2026-06-25T13:35:13"),
            self.parse("2026-06-25T13:35:13Z"),
        )

    def test_ordering_iso_vs_epoch(self) -> None:
        older = self.parse(1782394513133)            # 13:35:13
        newer = self.parse("2026-06-25T16:04:21.1Z")  # 16:04
        self.assertLess(older, newer)

    def test_bad_input_returns_none(self) -> None:
        for bad in (None, "", "   ", "garbage", "not-a-date", [], {}, True, False, object()):
            self.assertIsNone(self.parse(bad), f"expected None for {bad!r}")

    def test_total_never_raises_on_extreme_numbers(self) -> None:
        # Totality contract: even an arbitrary-precision int (e.g. from json.loads) or an
        # out-of-range/NaN float must return None, never raise -- the function runs on the
        # awscrt callback thread and in the per-appliance setup loop, where an OverflowError
        # (ArithmeticError, not ValueError) would escape the boundary catch.
        for extreme in (10 ** 400, -(10 ** 400), 1e308, -1e308, float("inf"),
                        float("-inf"), float("nan")):
            self.assertIsNone(self.parse(extreme), f"expected None for {extreme!r}")


class StrToFloatCharacterizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ours = _load(_OUR_HELPER, "addhon_client_helpers").str_to_float

    def test_pinned_characterization(self) -> None:
        self.assertEqual(self.ours("5"), 5)
        self.assertEqual(self.ours("0"), 0)
        self.assertEqual(self.ours("-16"), -16)
        self.assertEqual(self.ours("5.5"), 5.5)
        self.assertEqual(self.ours("5,5"), 5.5)       # decimal comma
        self.assertEqual(self.ours("-16.5"), -16.5)
        self.assertEqual(self.ours("0.0"), 0.0)
        self.assertEqual(self.ours(5), 5)
        self.assertEqual(self.ours(5.5), 5)           # QUIRK: float truncated by int()
        self.assertEqual(self.ours("  3 "), 3)        # int() tolerates spaces
        with self.assertRaises(ValueError):
            self.ours("abc")
        with self.assertRaises(ValueError):
            self.ours("1.2.3")
        with self.assertRaises(TypeError):
            self.ours(None)


if __name__ == "__main__":
    unittest.main()
