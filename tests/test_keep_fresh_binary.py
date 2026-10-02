# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Keep Fresh phase as a read-only binary, from `freshAirStatus` (#112).

The Keep Fresh switch is a setting for the next Start; while a cycle runs the app shows
the Keep Fresh phase by reading the shadow's `freshAirStatus` (dashboard title
`PROGRAMS.WM_WD.ULTRA_FRESH`, apk2 decomp.txt:2612411-2612546). A reporter's HQD washer
keeps it at 1 for the whole cycle when Keep Fresh is on and at 0 otherwise; other models
do not report it, and like every binary the entity exists only where the shadow does.
"""
from __future__ import annotations

import unittest

from tests.test_hpwh import _install_package_stubs  # same stubs, one place

_install_package_stubs()


class KeepFreshBinaryTest(unittest.TestCase):
    def _description(self, app_type: str):
        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        return next(
            (d for d in BINARY_SENSORS.get(app_type, ()) if d.key == "keep_fresh"), None
        )

    def test_washers_read_fresh_air_status(self) -> None:
        for app_type in ("WM", "WD"):
            with self.subTest(app_type):
                description = self._description(app_type)
                self.assertIsNotNone(description)
                self.assertEqual("freshAirStatus", description.attr_key)
                self.assertEqual("1", description.on_value)
                self.assertIsNone(description.value_fn)

    def test_not_on_dryers(self) -> None:
        self.assertIsNone(self._description("TD"))


if __name__ == "__main__":
    unittest.main()
