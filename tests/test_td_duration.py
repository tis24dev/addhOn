# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Issue #133: tumble-dryer programme duration, as the hOn app 2.30.7 computes it.

`getCycleDurationInSelection` sends a dryer to its own branch (apk2 decomp.txt:
1404274-1404392): the HQD formulas first on an HQD model, otherwise, and when they give
nothing, `getHdryCyclesDuration` (@1365630-1365675), whose `actualDuration` reads the
category's `remainingTimes` tables by `dryLevel`, `dryTime`, `remainingTimeMM` or
`dryTimeMM` (@1365423-1365628, apk2/analysis/issue133-td/README.md section 2.3).

The catalogues in tests/fixtures/td_duration come from Andre0512/hon-test-data. For the
Candy ROE H9A2TCE-80 (platform CHG) the expected minutes are read by hand off its tables.
The fixture's `cases` (four Haier HD90 models on HQD, a Hoover NDP4 on CHG) carry the
minutes the app's own decompiled chain gives when run in Node
(apk2/analysis/issue133-td/README.md section 5).
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
_DATA: dict = json.loads(FIXTURE.read_text(encoding="utf-8"))
MODELS: dict = _DATA["models"]
CASES: list = _DATA["cases"]
ROE = "ROE H9A2TCE-80"
HQD_DE = "HD90-A3959 DE"


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
        ancillary_group=node["ancillaryParameters"], remaining_times=node.get("remainingTimes")
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

    def test_a_numeric_string_in_the_table_is_read_as_minutes(self) -> None:
        # moment 2.29.4 takes a numeric string down its number path (@312779-312783,
        # @312912-312923), so the app shows 83 minutes.
        category = _category(ROE, "PROGRAMS.TD.MIX_AND_DRY", remaining_times={"dryLevel": {"1": "83"}})
        self.assertEqual(
            83, programme_duration(self.appliance, category, {"dryLevel": "1", "dryTime": "4"})
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
        # HQD path with an unparseable formula and no table: the engine's JSON.parse of
        # it throws a SyntaxError that remainingTimeGlobal absorbs (@1366726-1366728),
        # validateResult finds no catalogue value (NaN), and the table fallback has no
        # table: None, no exception.
        hqd = _appliance(model_attributes={"platform": "HQD", "remainingTime": "remainingTime+1"})
        self.assertIsNone(
            programme_duration(hqd, self.mix_without_table, {"dryLevel": "4", "dryTime": "4"})
        )

    def test_hqd_dryers_last_what_the_app_computes(self):
        for case in CASES:
            with self.subTest(model=case["model"], program=case["programName"], params=case["parameters"]):
                got = programme_duration(_appliance(case["model"]), _category(case["model"], case["programName"]), case["parameters"])
                self.assertEqual(case["minutes"], got)

    def test_a_drying_programme_hands_the_formulas_dry_prog_flag(self) -> None:
        # On HQD the TD branch adds dryProgFlag '1' before the formulas when the
        # programType is 'SD' or 'D' (@1404307-1404327). No real catalogue formula reads
        # it, so this one is made up; the minutes are the app's, run in Node. A formula
        # like "dryProgFlag*7" would not show the flag: checkValuesInLex takes the one
        # number above zero as the result (@1366594-1366641).
        hqd = _appliance(HQD_DE, model_attributes={"platform": "HQD", "remainingTime": "dryProgFlag*30+20"})
        group = MODELS[HQD_DE]["categories"]["PROGRAMS.TD.HQD_COTTON"]["ancillaryParameters"]
        variants = (
            ("D", group, 50),
            ("SD", dict(group, programType=dict(group["programType"], fixedValue="SD")), 50),
            ("none", {key: node for key, node in group.items() if key != "programType"}, 20),
        )
        for program_type, ancillary_group, minutes in variants:
            with self.subTest(programType=program_type):
                category = _category(HQD_DE, "PROGRAMS.TD.HQD_COTTON", ancillary_group=ancillary_group)
                self.assertEqual(
                    minutes, programme_duration(hqd, category, {"prCode": "62", "dryLevel": "14"})
                )


if __name__ == "__main__":
    unittest.main()
