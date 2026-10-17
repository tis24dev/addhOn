# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Issue #133: a washer, washer-dryer or tumble-dryer start as the app's new screen sends it.

The hOn app 2.30.7 builds a `startProgram` from its new programme screen in three steps
(apk2 decomp.txt): `_normalize` drops every parameter without a value
(@2632008-2632067, called @2632296-2632304); the builder adds a tumble dryer's
estimated duration as `remainingTimeMM`/`dryTimeMM` and sends `energyLabel` "0"
(@1362586-1362668, @1363028, call @2632501); then, unless the programme is a drying-only
one, `checkDryAndAutodoseOptions` turns a level plus a time into `dryTime "0"` and
`dryProgFlag "1"` (@1363771-1363818, gate @2632516-2632523).

The dryer categories come from tests/fixtures/td_duration (Andre0512/hon-test-data,
Candy ROE H9A2TCE-80, platform CHG); the others are small raw schemas.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from pathlib import Path
from typing import Any, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tests._golden import install_stubs  # noqa: E402

install_stubs()

from custom_components.addhon.client.engine.commands import HonCommand  # noqa: E402
from test_engine_cluster import _WD_WASH_DRY_REAL  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "td_duration" / "catalogs.json"
_ROE = json.loads(FIXTURE.read_text(encoding="utf-8"))["models"]["ROE H9A2TCE-80"]
_ROE_ATTRIBUTES: dict = _ROE["model_attributes"]


class _Category(NamedTuple):
    """A raw catalog category, the key it is filed under and the type it belongs to."""

    name: str
    schema: dict
    appliance_type: str


def _copy(node: Any) -> Any:
    return json.loads(json.dumps(node))


def _roe(name: str, *, without: str = "") -> _Category:
    schema = _copy(_ROE["categories"][name])
    schema.pop(without, None)
    return _Category(name, schema, "TD")


def _wd(**changes: dict) -> dict:
    schema = _copy(_WD_WASH_DRY_REAL)
    for group, nodes in changes.items():
        schema[group].update(nodes)
    return schema


MIX = _roe("PROGRAMS.TD.MIX_AND_DRY")
RAPID_30 = _roe("PROGRAMS.TD.RAPID_30")
# Ruling R4: the same category without its `remainingTimes` group.
MIX_WITHOUT_TABLE = _roe("PROGRAMS.TD.MIX_AND_DRY", without="remainingTimes")

WD_WASH_DRY_REAL = _Category(
    "PROGRAMS.WM_WD.IOT_WASH_RESISTANT_COLORED", _wd(), "WD"
)
WD_WITH_FLAG = _Category(
    "PROGRAMS.WM_WD.IOT_WASH_RESISTANT_COLORED",
    _wd(parameters={"dryProgFlag": {"typology": "fixed", "category": "command",
                                    "mandatory": 0, "fixedValue": "0"}}),
    "WD",
)
WD_DRY_ONLY = _Category(
    "PROGRAMS.WM_WD.IOT_DRY_SYNTHETIC",
    _wd(ancillaryParameters={"programType": {"typology": "fixed", "category": "general",
                                             "mandatory": 1, "fixedValue": "D"}}),
    "WD",
)
WM_WITH_PRSTR_VALUE = _Category(
    "PROGRAMS.WM_WD.COTTONS",
    {
        "parameters": {
            "prStr": {"typology": "fixed", "category": "command", "mandatory": 1,
                      "fixedValue": "X"},
            "temp": {"typology": "enum", "category": "command", "mandatory": 1,
                     "defaultValue": "40", "enumValues": [0, 20, 30, 40, 60, 90]},
        },
        "ancillaryParameters": {
            "programType": {"typology": "fixed", "category": "general", "mandatory": 1,
                            "fixedValue": "W"},
        },
    },
    "WM",
)
DW_WITH_PRSTR = _Category(
    "PROGRAMS.DW.ECO",
    {
        "parameters": {
            "prStr": {"typology": "fixed", "category": "command", "mandatory": 1},
            "dryLevel": {"typology": "range", "category": "command", "mandatory": 0,
                         "defaultValue": "0", "minimumValue": "0", "maximumValue": "3",
                         "incrementValue": "1"},
            "dryTime": {"typology": "range", "category": "command", "mandatory": 0,
                        "defaultValue": "1", "minimumValue": "1", "maximumValue": "4",
                        "incrementValue": "1"},
            "halfLoad": {"typology": "range", "category": "command", "mandatory": 0,
                         "minimumValue": "0", "maximumValue": "1", "incrementValue": "1"},
        },
        "ancillaryParameters": {
            "energyLabel": {"typology": "range", "category": "general", "mandatory": 1,
                            "defaultValue": "3", "minimumValue": "1", "maximumValue": "5",
                            "incrementValue": "1"},
        },
    },
    "DW",
)

_ROE_DEFAULT = object()


class _Api:
    """The cloud boundary: every send recorded as the command handed it over."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_command(
        self, appliance, name, params, ancillary, category, *, wire_command=None,
        energy_label=True, program_label=None,
    ):
        self.sent.append({
            "name": name,
            "params": dict(params),
            "ancillary": dict(ancillary),
            "category": category,
            "energy_label": energy_label,
            "program_label": program_label,
        })
        return True


class LaundryStartAlignmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.api = _Api()

    def command(
        self,
        category: _Category,
        *,
        appliance_type: str | None = None,
        model_attributes: Any = _ROE_DEFAULT,
    ) -> HonCommand:
        """`startProgram` of `category` on an appliance of its own type (or the one given).

        The model attributes default to the ROE H9A2TCE-80's (platform CHG).
        """
        appliance = types.SimpleNamespace(
            appliance_type=appliance_type or category.appliance_type,
            model_attributes=(
                dict(_ROE_ATTRIBUTES)
                if model_attributes is _ROE_DEFAULT
                else model_attributes
            ),
            zone=0,
            options={},
            commands={},
            api=self.api,
            sync_payload_to_params=lambda params: None,
            sync_params_to_command=lambda name: None,
        )
        return HonCommand(
            "startProgram", _copy(category.schema), appliance, category_name=category.name
        )

    def start(
        self,
        category_or_command: _Category | HonCommand,
        *,
        appliance_type: str | None = None,
        model_attributes: Any = _ROE_DEFAULT,
        **values: str,
    ) -> tuple[dict, dict]:
        """Write each option, press Start; the `parameters` and ancillaries that went out."""
        command = (
            category_or_command
            if isinstance(category_or_command, HonCommand)
            else self.command(
                category_or_command,
                appliance_type=appliance_type,
                model_attributes=model_attributes,
            )
        )
        for key, value in values.items():
            command.parameters[key].value = value
        asyncio.run(command.send())
        sent = self.api.sent[-1]
        return sent["params"], sent["ancillary"]

    def test_mix_and_dry_extra_dry_goes_out_as_the_apps(self):
        params, ancillary = self.start(MIX, dryLevel="4")
        self.assertEqual("4", params["dryLevel"])
        self.assertEqual("0", params["dryTime"])
        self.assertEqual("1", params["dryProgFlag"])
        self.assertEqual("136", params["dryTimeMM"])
        self.assertEqual("136", params["remainingTimeMM"])
        self.assertNotIn("prStr", params)
        self.assertEqual("0", ancillary["energyLabel"])
        self.assertEqual("70", ancillary["dryTimeMM"])
        self.assertNotIn("programRules", ancillary)

    def test_a_second_start_recomputes_what_the_first_left(self):
        command = self.command(MIX)
        for key, value in (("remainingTimeMM", "136"), ("prStr", "PROGRAMS.TD.MIX_AND_DRY")):
            command.parameters[key].value = value          # what the history recovery writes
        params, _ = self.start(command, dryLevel="2")
        self.assertEqual("114", params["remainingTimeMM"])
        self.assertEqual("114", params["dryTimeMM"])
        self.assertNotIn("prStr", params)

    def test_a_recovered_dry_prog_flag_does_not_ride_a_timed_start(self):
        # WD category whose schema declares dryProgFlag fixed "0" (as the HQD washers of #112).
        command = self.command(WD_WITH_FLAG)
        command.parameters["dryProgFlag"].value = "1"
        params, _ = self.start(command, dryLevel="0", dryTime="2")
        self.assertEqual("0", params["dryProgFlag"])
        self.assertEqual("2", params["dryTime"])

    def test_a_timed_dryer_programme_keeps_its_time(self):
        params, _ = self.start(RAPID_30)
        self.assertEqual("1", params["dryTime"])
        self.assertNotIn("dryProgFlag", params)
        self.assertEqual("30", params["remainingTimeMM"])

    def test_a_dryer_without_a_table_sends_no_duration(self):
        params, _ = self.start(MIX_WITHOUT_TABLE, dryLevel="4")
        self.assertEqual(("0", "1"), (params["dryTime"], params["dryProgFlag"]))
        self.assertNotIn("remainingTimeMM", params)
        self.assertNotIn("dryTimeMM", params)

    def test_a_dryer_off_hqd_and_chg_sends_no_duration(self):
        for attributes in ({"platform": "XYZ"}, {}, None):
            with self.subTest(attributes=attributes):
                params, _ = self.start(MIX, dryLevel="4", model_attributes=attributes)
                self.assertNotIn("remainingTimeMM", params)
                self.assertEqual("1", params["dryProgFlag"])

    def test_the_washer_dryer_of_issue_99_omits_an_unset_dry_time(self):
        params, _ = self.start(WD_WASH_DRY_REAL)          # dryLevel default "1", dryTime range without default
        self.assertNotIn("dryTime", params)
        self.assertNotIn("dryProgFlag", params)

    def test_a_washer_dryer_with_level_and_time_takes_the_flag(self):
        params, _ = self.start(WD_WASH_DRY_REAL, dryTime="2")
        self.assertEqual(("0", "1"), (params["dryTime"], params["dryProgFlag"]))

    def test_a_drying_only_programme_keeps_level_and_time(self):
        params, _ = self.start(WD_DRY_ONLY, dryTime="2")   # programType fixed "D"
        self.assertEqual("2", params["dryTime"])
        self.assertNotIn("dryProgFlag", params)

    def test_a_prstr_with_a_real_value_goes_as_it_is(self):
        params, _ = self.start(WM_WITH_PRSTR_VALUE)       # prStr fixedValue "X"
        self.assertEqual("X", params["prStr"])

    def test_other_types_keep_their_body(self):
        # DW: prStr still rewritten to the category key, an unset range still sent as its min,
        # no dryProgFlag, no energyLabel change. (REF is pinned by tests/test_engine_cluster.py:641.)
        params, ancillary = self.start(DW_WITH_PRSTR, appliance_type="DW", dryLevel="1", dryTime="2")
        self.assertEqual("PROGRAMS.DW.ECO", params["prStr"])
        self.assertEqual("2", params["dryTime"])
        self.assertIn("halfLoad", params)                  # range without default, today's min
        self.assertNotIn("dryProgFlag", params)
        self.assertEqual("3", ancillary["energyLabel"])    # the schema's own value

    def test_a_dryer_send_without_the_label_keeps_the_schemas(self):
        # energy_label=False is a send that keeps the label out of the attributes: G5
        # adds no "0" then, and the ancillaries carry the schema's own value, or none.
        command = self.command(MIX)
        asyncio.run(command.send_exact(
            command.canonical_exact_payload({"dryLevel": "4", "dryTime": "4"}),
            energy_label=False,
        ))
        self.assertFalse(self.api.sent[-1]["energy_label"])
        self.assertEqual("2", self.api.sent[-1]["ancillary"]["energyLabel"])
        schema = _copy(MIX.schema)
        del schema["ancillaryParameters"]["energyLabel"]
        command = self.command(MIX._replace(schema=schema))
        asyncio.run(command.send_exact(
            command.canonical_exact_payload({"dryLevel": "4", "dryTime": "4"}),
            energy_label=False,
        ))
        self.assertNotIn("energyLabel", self.api.sent[-1]["ancillary"])

    def test_the_callers_payload_is_left_untouched(self):
        command = self.command(MIX)
        payload = command.canonical_exact_payload({"dryLevel": "4", "dryTime": "4", "prStr": "0"})
        snapshot = dict(payload)
        asyncio.run(command.send_exact(payload))
        self.assertEqual(snapshot, dict(payload))


if __name__ == "__main__":
    unittest.main()
