# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Issue #133: tumble-dryer programme duration, as the hOn app 2.30.7 computes it.

`getCycleDurationInSelection` sends a dryer to its own branch (apk2 decomp.txt:
1404274-1404392): the HQD formulas first on an HQD model, otherwise, and when they give
nothing, `getHdryCyclesDuration` (@1365630-1365675), whose `actualDuration` reads the
category's `remainingTimes` tables by `dryLevel`, `dryTime`, `remainingTimeMM` or
`dryTimeMM` (@1365423-1365628, apk2/analysis/issue133-td/README.md section 2.3).

The catalogue in tests/fixtures/td_duration is a Candy ROE H9A2TCE-80 (platform CHG) of
Andre0512/hon-test-data; the expected minutes are read by hand off its tables.
"""
from __future__ import annotations

import json
import types
import unittest
from pathlib import Path

from tests._golden import install_stubs

install_stubs()

from custom_components.addhon.client.engine.hqd_duration import (  # noqa: E402
    programme_duration,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "td_duration" / "catalogs.json"
MODELS: dict = json.loads(FIXTURE.read_text(encoding="utf-8"))["models"]
ROE = "ROE H9A2TCE-80"


def _appliance(model: str = ROE, **overrides) -> types.SimpleNamespace:
    data = MODELS[model]
    fields = dict(
        appliance_type=data["type"],
        model_attributes=dict(data["model_attributes"]),
        brand="Candy",
        command_history=[],
        commands={},
    )
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def _category(model: str, program_name: str, **overrides) -> types.SimpleNamespace:
    node = MODELS[model]["categories"][program_name]
    fields = dict(
        ancillary_group=node["ancillaryParameters"], remaining_times=node["remainingTimes"]
    )
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


class DryerDurationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.appliance = _appliance()
        self.mix = _category(ROE, "PROGRAMS.TD.MIX_AND_DRY")
        self.whites = _category(ROE, "PROGRAMS.TD.WHITES")
        self.rapid = _category(ROE, "PROGRAMS.TD.RAPID_30")
        self.mix_without_table = _category(ROE, "PROGRAMS.TD.MIX_AND_DRY", remaining_times=None)

    def test_a_dry_level_reads_the_level_table(self) -> None:
        # MIX_AND_DRY remainingTimes.dryLevel = {1: 83, 2: 114, 3: 125, 4: 136}
        for level, minutes in (("1", 83), ("2", 114), ("3", 125), ("4", 136)):
            with self.subTest(dryLevel=level):
                self.assertEqual(
                    minutes,
                    programme_duration(self.appliance, self.mix, {"dryLevel": level, "dryTime": "4"}),
                )

    def test_another_programme_reads_its_own_table(self) -> None:
        self.assertEqual(
            150, programme_duration(self.appliance, self.whites, {"dryLevel": "2", "dryTime": "4"})
        )

    def test_timed_drying_reads_the_time_table(self) -> None:
        self.assertEqual(
            30, programme_duration(self.appliance, self.rapid, {"dryLevel": "0", "dryTime": "1"})
        )
        self.assertEqual(
            90, programme_duration(self.appliance, self.mix, {"dryLevel": "0", "dryTime": "6"})
        )

    def test_a_remaining_time_in_the_parameters_wins_as_in_the_app(self) -> None:
        # actualDuration step 4 (@1365518-1365522): the reason G6 strips it before a start.
        self.assertEqual(
            136,
            programme_duration(
                self.appliance,
                self.mix,
                {"dryLevel": "2", "dryTime": "4", "remainingTimeMM": "136"},
            ),
        )

    def test_without_a_table_there_is_no_duration(self) -> None:
        self.assertIsNone(
            programme_duration(
                self.appliance, self.mix_without_table, {"dryLevel": "4", "dryTime": "4"}
            )
        )

    def test_any_platform_reaches_the_table(self) -> None:
        # The TD branch only asks HQD for formulas (@1404274-1404360); every other
        # platform reads the table.
        other = _appliance(model_attributes={"platform": "XYZ"})
        self.assertEqual(
            136, programme_duration(other, self.mix, {"dryLevel": "4", "dryTime": "4"})
        )

    def test_unreadable_formulas_are_no_duration(self) -> None:
        # HQD path with a formula that cites itself (as tests/test_hqd_duration.py:133)
        # and no table: the engine gives nothing, the table fallback gives nothing, no
        # exception.
        hqd = _appliance(model_attributes={"platform": "HQD", "remainingTime": "remainingTime+1"})
        self.assertIsNone(
            programme_duration(hqd, self.mix_without_table, {"dryLevel": "4", "dryTime": "4"})
        )


if __name__ == "__main__":
    unittest.main()
