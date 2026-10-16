# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Issue #131: programme duration on HQD, and the delayed start built on it.

The hOn app 2.30.7 evaluates the model's `remainingTime*` formulas for a washer on the
HQD platform (`getCommonCycleDuration` -> `actualDuration`, apk2 decomp.txt:1365720-1366807,
behind `getProgramDuration` @2624910). With `delayType: end` it counts `delayTime` to the
END of the cycle and adds that duration to the start the user picks (@4077209-4077270).

The expected durations in tests/fixtures/wm_hqd_duration are `getProgramDuration` of the
decompiled app executed in Node on the real starts of the published dumps (Haier
HW80-B14959TU1-S of #112/#131, HW90-B14387TU1-S of #112, Candy BP 49SBL8-FR of #112);
the port matched it on all 54 and on 37,000 random cases
(apk2/analysis/issue131-wm-delay/2-motore-durata-hqd.md, strumenti/duration/).
"""
from __future__ import annotations

import json
import types
import unittest
from pathlib import Path

from tests._golden import install_stubs

install_stubs()

from custom_components.addhon.client.engine import hqd_duration  # noqa: E402
from custom_components.addhon.client.engine.hqd_duration import (  # noqa: E402
    delay_counts_to_end,
    delay_to_send,
    programme_duration,
    started_programme_duration,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "wm_hqd_duration" / "cases.json"
MODELS: dict = json.loads(FIXTURE.read_text(encoding="utf-8"))["models"]
HW80 = "HW80-B14959TU1-S"


def _appliance(model: str = HW80, **overrides) -> types.SimpleNamespace:
    data = MODELS[model]
    fields = dict(
        appliance_type=data["type"],
        model_attributes=dict(data["model_attributes"]),
        brand="Haier",
        command_history=[],
        commands={},
    )
    fields.update(overrides)
    return types.SimpleNamespace(**fields)


def _category(model: str, program_name: str) -> types.SimpleNamespace:
    node = MODELS[model]["categories"][program_name]
    return types.SimpleNamespace(
        ancillary_group=node["ancillaryParameters"], remaining_times=node["remainingTimes"]
    )


def _start(model: str, index: int) -> dict:
    return MODELS[model]["starts"][index]


class ProgrammeDurationTest(unittest.TestCase):
    def test_real_starts_last_what_the_app_computes(self) -> None:
        for model, data in MODELS.items():
            for start in data["starts"]:
                with self.subTest(model=model, programme=start["programName"],
                                  expected=start["expected"]):
                    got = programme_duration(
                        _appliance(model),
                        _category(model, start["programName"]),
                        start["parameters"],
                        start["ancillaryParameters"],
                    )
                    self.assertEqual(start["expected"], got)

    def test_the_delayed_start_of_issue_131_lasts_108_minutes(self) -> None:
        # IOT_WASH_DARK, prCode 116, soak prewash on, main wash 15, three rinses:
        # 20 + 45 + 37 + 5 + 1, where the catalogue says a fixed 115.
        start = _start(HW80, 0)
        self.assertEqual("570", start["parameters"]["delayTime"])
        got = programme_duration(
            _appliance(), _category(HW80, start["programName"]),
            start["parameters"], start["ancillaryParameters"],
        )
        self.assertEqual(108.0, got)

    def test_without_formulas_the_catalogue_remaining_time_is_used(self) -> None:
        # validateResult @1366541-1366592: nothing to evaluate -> remainingTime.fixedValue.
        appliance = _appliance(model_attributes={"platform": "HQD"})
        category = types.SimpleNamespace(
            ancillary_group={},
            remaining_times={"remainingTime": {"typology": "fixed", "fixedValue": "63"}},
        )
        self.assertEqual(63.0, programme_duration(appliance, category, {"prCode": "1"}))

    def test_no_formula_and_no_catalogue_value_is_no_duration(self) -> None:
        appliance = _appliance(model_attributes={"platform": "HQD"})
        category = types.SimpleNamespace(ancillary_group={}, remaining_times=None)
        self.assertIsNone(programme_duration(appliance, category, {"prCode": "1"}))

    def test_off_hqd_there_is_no_duration(self) -> None:
        start = _start(HW80, 0)
        attributes = dict(MODELS[HW80]["model_attributes"], platform="CHG")
        self.assertIsNone(
            programme_duration(
                _appliance(model_attributes=attributes),
                _category(HW80, start["programName"]),
                start["parameters"],
                start["ancillaryParameters"],
            )
        )

    def test_a_zero_or_negative_result_is_no_duration(self) -> None:
        # Decision H4 of 2026-10-16: the app would use a negative one, we do not; a
        # zero falls back to the catalogue, here absent.
        category = types.SimpleNamespace(ancillary_group={}, remaining_times=None)
        for formula in ("2-9", "3-3"):
            with self.subTest(formula=formula):
                appliance = _appliance(
                    model_attributes={"platform": "HQD", "remainingTime": formula}
                )
                self.assertIsNone(programme_duration(appliance, category, {"prCode": "1"}))

    def test_a_single_positive_number_is_the_result_as_in_the_app(self) -> None:
        # checkValuesInLex @1366594-1366641: "0-5" is 5, not -5.
        category = types.SimpleNamespace(ancillary_group={}, remaining_times=None)
        appliance = _appliance(model_attributes={"platform": "HQD", "remainingTime": "0-5"})
        self.assertEqual(5.0, programme_duration(appliance, category, {"prCode": "1"}))

    def test_formulas_that_cite_themselves_are_no_duration(self) -> None:
        # Decision H2: the JavaScript stack overflow, whose result depends on its depth.
        # The shape the fuzz found: `#finalSpinDuration` inside finalSpinDuration.
        loop = '{"prCode":{"default":"#finalSpinDuration+2"}}'
        appliance = _appliance(
            model_attributes={
                "platform": "HQD",
                "remainingTime": "remainingTimeFinalSpin",
                "remainingTimeFinalSpin": loop,
                "finalSpinDuration": loop,
            }
        )
        category = types.SimpleNamespace(
            ancillary_group={},
            remaining_times={"remainingTime": {"typology": "fixed", "fixedValue": "63"}},
        )
        self.assertIsNone(programme_duration(appliance, category, {"prCode": "1"}))

    def test_no_category_or_no_model_is_no_duration(self) -> None:
        start = _start(HW80, 0)
        self.assertIsNone(programme_duration(_appliance(), None, start["parameters"]))
        self.assertIsNone(
            programme_duration(
                _appliance(model_attributes=None),
                _category(HW80, start["programName"]),
                start["parameters"],
            )
        )


class LexerTest(unittest.TestCase):
    def _value(self, expression: str, values: dict) -> float:
        tokens = hqd_duration._lex(expression, lambda token: values.get(token))
        return hqd_duration._evaluate(tokens)

    def test_the_rinse_expression_of_the_hw80(self) -> None:
        # (rinseIterationTime+2)*(rinseIterations-1)+rinseIterationTime, 9 and 3: 31.
        self.assertEqual(
            31.0,
            self._value(
                "(rinseIterationTime+2)*(rinseIterations-1)+rinseIterationTime",
                {"rinseIterationTime": 9.0, "rinseIterations": 3.0},
            ),
        )

    def test_a_decimal_comma_and_unary_minus(self) -> None:
        self.assertEqual(-5.0, self._value("-2*2.5", {}))

    def test_a_single_positive_number_wins_without_evaluating(self) -> None:
        # checkValuesInLex @1366594-1366641: "a-b" with a=0, b=10 gives 10.
        engine = hqd_duration._HqdEngine({}, {}, {}, None)
        self.assertEqual(10.0, engine._single_value([0.0, 10.0, "-"]))


class DelayCountsToEndTest(unittest.TestCase):
    def test_hqd_with_an_end_delay(self) -> None:
        self.assertTrue(delay_counts_to_end(_appliance()))

    def test_an_hqd_model_without_delay_type_counts_to_the_end_too(self) -> None:
        # The app tests `delayType !== 'start'` (@4077209-4077227).
        attributes = {"platform": "HQD"}
        self.assertTrue(delay_counts_to_end(_appliance(model_attributes=attributes)))

    def test_the_other_models_count_to_the_start(self) -> None:
        cases = (
            dict(model_attributes={"platform": "HQD", "delayType": "start"}),
            dict(model_attributes={"platform": "CHG", "delayType": "end"}),
            dict(model_attributes={}),
            dict(model_attributes=None),
            dict(brand="Hoover"),
            dict(appliance_type="TD"),
            dict(appliance_type="DW"),
        )
        for overrides in cases:
            with self.subTest(**overrides):
                self.assertFalse(delay_counts_to_end(_appliance(**overrides)))

    def test_the_candy_of_issue_112_counts_to_the_start(self) -> None:
        self.assertFalse(delay_counts_to_end(_appliance("BP 49SBL8-FR", brand="Candy")))

    def test_a_washer_dryer_counts_as_a_washer(self) -> None:
        self.assertTrue(delay_counts_to_end(_appliance(appliance_type="WD")))


class DelayToSendTest(unittest.TestCase):
    def test_rounded_to_the_half_hour_as_the_app(self) -> None:
        # getRightDelayTime @2646628: truncated, at least 30, Math.round to 30.
        cases = (
            (570, 108.0, 690, False),  # 678 -> 690, the start 12 minutes late
            (570, 105.0, 690, False),  # 675: a half rounds up
            (570, 104.9, 660, False),  # 674 truncated, then down
            (10, 5.0, 30, False),  # below 30 -> 30
            (1200, 108.0, 1320, False),
            (1350, 108.0, 1410, True),  # 1470 above the schema's bound
        )
        for chosen, duration, expected, capped in cases:
            with self.subTest(chosen=chosen, duration=duration):
                self.assertEqual((expected, capped), delay_to_send(chosen, duration))


class StartedProgrammeDurationTest(unittest.TestCase):
    def _waiting(self, *history_rows, categories=None) -> types.SimpleNamespace:
        start = _start(HW80, 0)
        category = _category(HW80, start["programName"])
        command = types.SimpleNamespace(
            categories=categories if categories is not None else {"iot_wash_dark": category}
        )
        return _appliance(command_history=list(history_rows), commands={"startProgram": command})

    @staticmethod
    def _row(command_name="startProgram", **command) -> dict:
        start = _start(HW80, 0)
        body = {
            "commandName": command_name,
            "programName": start["programName"],
            "parameters": dict(start["parameters"]),
            "ancillaryParameters": dict(start["ancillaryParameters"]),
        }
        body.update(command)
        return {"command": body}

    def test_the_newest_start_with_the_shadows_programme(self) -> None:
        appliance = self._waiting(self._row(command_name="setParameters"), self._row())
        self.assertEqual(108.0, started_programme_duration(appliance, "116"))
        self.assertEqual(108.0, started_programme_duration(appliance, 116))

    def test_a_newest_start_of_another_programme_is_not_used(self) -> None:
        # A start from the panel never reaches /history: the newest row is older.
        appliance = self._waiting(self._row())
        self.assertIsNone(started_programme_duration(appliance, "115"))

    def test_no_start_in_the_history(self) -> None:
        self.assertIsNone(started_programme_duration(self._waiting(), "116"))
        appliance = self._waiting(self._row(command_name="stopProgram"))
        self.assertIsNone(started_programme_duration(appliance, "116"))

    def test_a_programme_missing_from_the_catalogue(self) -> None:
        appliance = self._waiting(self._row(), categories={})
        self.assertIsNone(started_programme_duration(appliance, "116"))


if __name__ == "__main__":
    unittest.main()
