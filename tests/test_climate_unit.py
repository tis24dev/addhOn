# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression guard: climate must use the HA temperature-unit constant.

Covers the M4 cleanup (replace the hardcoded "°C" literal with
UnitOfTemperature.CELSIUS). Behaviour is unchanged (CELSIUS == "°C"); this test
only prevents the literal from creeping back in. Pure source read, no Home
Assistant import required.
"""
from __future__ import annotations

import unittest
from pathlib import Path

CLIMATE = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "addhon"
    / "climate.py"
)


class ClimateTemperatureUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = CLIMATE.read_text(encoding="utf-8")

    def test_imports_unit_of_temperature(self) -> None:
        self.assertIn(
            "from homeassistant.const import UnitOfTemperature",
            self.source,
        )

    def test_uses_celsius_constant(self) -> None:
        self.assertIn(
            "self._attr_temperature_unit = UnitOfTemperature.CELSIUS",
            self.source,
        )

    def test_does_not_hardcode_celsius_literal(self) -> None:
        self.assertNotIn(
            'self._attr_temperature_unit = "°C"',
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
