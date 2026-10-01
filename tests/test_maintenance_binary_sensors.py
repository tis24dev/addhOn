# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Drum- and filter-cleaning binaries read the maintenance-cycle counter.

`drumCleaning` / `filterCleaning` are not 0/1 flags: the statistics endpoint gives
`{tot, count, remaining, percentage}` (HW80-B14959TU1-S: `{90, 43, 47, 48}`), so the
default `str(raw) == "1"` could never turn on. The hOn app writes "maintenance needed"
when `remaining <= 0` and raises its health warning when `count >= tot`, the same moment
(apk2 decomp.txt:3191560-3191720, 3172170-3172235).
"""
from __future__ import annotations

import unittest

from tests.test_hpwh import _install_package_stubs  # same stubs, one place

_install_package_stubs()


class MaintenanceBinaryTest(unittest.TestCase):
    def _read(self, raw):
        from custom_components.addhon import binary_sensor

        results = set()
        for description in (binary_sensor._DRUM_CLEAN, binary_sensor._FILTER_CLEAN):
            self.assertIsNotNone(description.value_fn, description.key)
            results.add(description.value_fn(raw))
        self.assertEqual(1, len(results), "drum and filter must read a counter alike")
        return results.pop()

    def test_counter_with_cycles_left_is_off(self) -> None:
        self.assertIs(False, self._read({"tot": 90, "count": 43, "remaining": 47, "percentage": 48}))

    def test_counter_at_zero_remaining_is_on(self) -> None:
        self.assertIs(True, self._read({"tot": 90, "count": 90, "remaining": 0, "percentage": 100}))

    def test_overdue_counter_is_on(self) -> None:
        self.assertIs(True, self._read({"tot": 90, "count": 95, "remaining": -5}))

    def test_counter_without_remaining_compares_count_to_tot(self) -> None:
        self.assertIs(True, self._read({"tot": 90, "count": 90}))
        self.assertIs(False, self._read({"tot": 90, "count": 12}))

    def test_unreadable_counter_is_unknown(self) -> None:
        self.assertIsNone(self._read({"percentage": "n/a"}))
        self.assertIsNone(self._read({"remaining": "soon"}))

    def test_flag_shaped_value_keeps_the_old_reading(self) -> None:
        self.assertIs(True, self._read("1"))
        self.assertIs(True, self._read(1))
        self.assertIs(False, self._read("0"))


if __name__ == "__main__":
    unittest.main()
