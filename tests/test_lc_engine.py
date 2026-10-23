# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Laundry Centre (type LC) engine layer: the programme running on each unit (#124).

The LC is a washer (`WM*`) and a dryer (`TD*`) under one MAC. Each unit's code is looked
up on that unit's OWN categories of `startProgram`, never on the active category (P5).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import install_stubs  # noqa: E402

install_stubs()

from custom_components.addhon.client.engine.appliances import lc, registry  # noqa: E402
from custom_components.addhon.client.engine.attributes import HonAttribute  # noqa: E402
from custom_components.addhon.client.engine.parameter.program import (  # noqa: E402
    HonParameterProgram,
)


def _category(key, code, family, favourite=False):
    parameters = {
        key: SimpleNamespace(value=code, values=[str(code)]),
        "programFamily": SimpleNamespace(value=family, values=[family]),
    }
    if favourite:
        parameters["favourite"] = SimpleNamespace(value="1", values=["1"])
    return SimpleNamespace(parameters=parameters)


def _program(iot_wash_44_family="guided_voice"):
    categories = {
        "hqd_wm_smart": _category("WMprCode", 97, "dashboard_smart"),
        "iot_wash_rapid_30": _category("WMprCode", 9, "guided_voice"),
        "iot_wash_rapid_44": _category("WMprCode", 9, iot_wash_44_family),
        "hqd_td_smart_ai": _category(
            "TDprCode", 56, "dashboard_wmsync_guided_voice_care_auto"
        ),
        "iot_dry_mixed": _category("TDprCode", 56, "wmsync_guided_voice_auto"),
        "fav_td": _category("TDprCode", 56, "favourite", favourite=True),
    }
    command = SimpleNamespace(category="PROGRAMS.LC.HQD_WM_SMART", categories=categories)
    return HonParameterProgram("program", command, "custom")


def _params(**values):
    return {k: HonAttribute({"parNewVal": v}) for k, v in values.items()}


class NameForUnitCodeTest(unittest.TestCase):
    def setUp(self):
        self.program = _program()

    def test_a_unique_code_names_its_programme(self):
        self.assertEqual("hqd_wm_smart", self.program.name_for_unit_code(97, "WMprCode"))

    def test_the_base_programme_beats_iot_and_favourites(self):
        self.assertEqual(
            "hqd_td_smart_ai", self.program.name_for_unit_code(56, "TDprCode")
        )

    def test_an_iot_only_code_takes_the_first_candidate(self):
        # P6: the app's washer dashboard; no dashboard family among the two.
        self.assertEqual(
            "iot_wash_rapid_30", self.program.name_for_unit_code(9, "WMprCode")
        )

    def test_an_iot_only_code_prefers_a_dashboard_family(self):
        program = _program(iot_wash_44_family="dashboard")
        self.assertEqual("iot_wash_rapid_44", program.name_for_unit_code(9, "WMprCode"))

    def test_a_code_of_the_other_unit_is_not_matched(self):
        self.assertIsNone(self.program.name_for_unit_code(56, "WMprCode"))
        self.assertIsNone(self.program.name_for_unit_code(12, "WMprCode"))

    def test_the_active_category_is_ignored(self):
        # P5 pending: even a deliberately selected iot_ category does not win.
        self.program._value = "iot_dry_mixed"
        self.program._programs["iot_dry_mixed"].selected_explicitly = True
        self.assertEqual(
            "hqd_td_smart_ai", self.program.name_for_unit_code(56, "TDprCode")
        )


class LcLayerTest(unittest.TestCase):
    def setUp(self):
        self.parent = SimpleNamespace(
            settings={"startProgram.program": _program()},
            connection=True,
            appliance_type="LC",
        )

    def test_the_layer_names_both_units(self):
        data = lc.Appliance(self.parent).attributes(
            {"parameters": _params(WMprCode="97", TDprCode="56")}
        )
        self.assertEqual("hqd_wm_smart", data["WMprogramName"])
        self.assertEqual("hqd_td_smart_ai", data["TDprogramName"])
        self.assertNotIn("programName", data)
        self.assertTrue(data["available"])

    def test_a_zero_or_missing_code_is_no_program(self):
        data = lc.Appliance(self.parent).attributes(
            {"parameters": _params(WMprCode="0", TDprCode="0")}
        )
        self.assertEqual("No Program", data["WMprogramName"])
        self.assertEqual("No Program", data["TDprogramName"])

    def test_an_unmatched_or_unreadable_code_is_no_program(self):
        data = lc.Appliance(self.parent).attributes(
            {"parameters": _params(WMprCode="12", TDprCode="x")}
        )
        self.assertEqual("No Program", data["WMprogramName"])
        self.assertEqual("No Program", data["TDprogramName"])

    def test_a_unit_without_its_code_gets_no_programme_key(self):
        data = lc.Appliance(self.parent).attributes({"parameters": {}})
        self.assertNotIn("WMprogramName", data)
        self.assertNotIn("TDprogramName", data)
        self.assertTrue(data["available"])

    def test_only_the_absent_unit_loses_its_programme_key(self):
        washer_only = lc.Appliance(self.parent).attributes(
            {"parameters": _params(WMprCode="97")}
        )
        self.assertEqual("hqd_wm_smart", washer_only["WMprogramName"])
        self.assertNotIn("TDprogramName", washer_only)
        dryer_only = lc.Appliance(self.parent).attributes(
            {"parameters": _params(TDprCode="56")}
        )
        self.assertNotIn("WMprogramName", dryer_only)
        self.assertEqual("hqd_td_smart_ai", dryer_only["TDprogramName"])

    def test_a_stale_programme_key_does_not_survive_a_missing_code(self):
        data = lc.Appliance(self.parent).attributes(
            {"parameters": {}, "WMprogramName": "stale", "TDprogramName": "stale"}
        )
        self.assertNotIn("WMprogramName", data)
        self.assertNotIn("TDprogramName", data)

    def test_the_registry_knows_the_lc(self):
        self.assertIsInstance(registry.get_extra(self.parent), lc.Appliance)


if __name__ == "__main__":
    unittest.main()
