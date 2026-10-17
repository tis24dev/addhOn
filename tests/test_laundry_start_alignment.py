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

The dryer categories come from tests/fixtures/td_duration (Andre0512/hon-test-data:
Candy ROE H9A2TCE-80, platform CHG; Haier HD90 models, platform HQD); the others are
small raw schemas.
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

from custom_components.addhon.client.engine.command_loader import (  # noqa: E402
    HonCommandLoader,
)
from custom_components.addhon.client.engine.commands import HonCommand  # noqa: E402
from test_engine_cluster import _WD_WASH_DRY_REAL  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "td_duration" / "catalogs.json"
_MODELS: dict = json.loads(FIXTURE.read_text(encoding="utf-8"))["models"]
_ROE = _MODELS["ROE H9A2TCE-80"]
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


def _haier(model: str, name: str) -> tuple[_Category, dict]:
    """A raw category of one of the fixture's Haier dryers, and the model's attributes."""
    data = _MODELS[model]
    return _Category(name, _copy(data["categories"][name]), data["type"]), dict(
        data["model_attributes"]
    )


def _range(default: str, minimum: str, maximum: str) -> dict:
    return {"typology": "range", "category": "command", "mandatory": 1,
            "defaultValue": default, "minimumValue": minimum, "maximumValue": maximum,
            "incrementValue": "1"}


MIX = _roe("PROGRAMS.TD.MIX_AND_DRY")
RAPID_30 = _roe("PROGRAMS.TD.RAPID_30")
WHITES = _roe("PROGRAMS.TD.WHITES")
# Ruling R4: the same category without its `remainingTimes` group.
MIX_WITHOUT_TABLE = _roe("PROGRAMS.TD.MIX_AND_DRY", without="remainingTimes")

# Timed programmes of the HD90 dryers (HQD): the timer is `parameters.dryTimeMM`, an enum
# with a default, and the dry level is fixed "11" (td_1014 and td_1015 of hon-test-data).
HQD_TIMER, HQD_DE_ATTRIBUTES = _haier("HD90-A3959 DE", "PROGRAMS.TD.HQD_TIMER")
HQD_DUVET, HQD_BNL_ATTRIBUTES = _haier("HD90-A3959 BNL", "PROGRAMS.TD.IOT_DRY_DUVET")

# The level-mode nodes of the Haier washer-dryer wd_1889 of hon-test-data (HQD_COTTONS
# and 90 more programmes): a `dryTime` that accepts 0, a dry level and a `dryProgFlag`.
WD_LEVEL_MODE = _Category(
    "PROGRAMS.WM_WD.HQD_COTTONS",
    {
        "parameters": {
            "prCode": {"typology": "fixed", "category": "command", "mandatory": 1,
                       "fixedValue": "1"},
            "dryTime": _range("1", "0", "7"),
            "dryLevel": _range("3", "0", "3"),
            "dryProgFlag": _range("0", "0", "1"),
        },
        "ancillaryParameters": {
            "programType": {"typology": "fixed", "category": "general", "mandatory": 1,
                            "fixedValue": "W+D"},
        },
    },
    "WD",
)
_HQD_WD_ATTRIBUTES = {"platform": "HQD"}
# The same nodes with the ancillary `dryTimeMM` of wd_1889's HQD_SMART (hon-test-data).
WD_WITH_DRY_TIME_MM = WD_LEVEL_MODE._replace(schema={
    **WD_LEVEL_MODE.schema,
    "ancillaryParameters": {
        **WD_LEVEL_MODE.schema["ancillaryParameters"],
        "dryTimeMM": {"typology": "range", "category": "general", "mandatory": 1,
                      "defaultValue": "30", "minimumValue": "0", "maximumValue": "210",
                      "incrementValue": "30"},
    },
})

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
# A computed key the schema gives as an enum with a cased value (made up: no catalog of
# hon-test-data has one); an enum's `value` reads back lower-cased (`clean_value`).
WM_WITH_PRSTR_ENUM = _Category(
    "PROGRAMS.WM_WD.COTTONS",
    {
        "parameters": {
            "prStr": {"typology": "enum", "category": "command", "mandatory": 1,
                      "defaultValue": "COTTONS", "enumValues": ["COTTONS"]},
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

    def favourite(
        self, category: _Category, saved: dict, *, model_attributes: Any = _ROE_DEFAULT
    ) -> HonCommand:
        """The favourite `HonCommandLoader._add_favourites` files for `saved` on `category`."""
        base = self.command(category, model_attributes=model_attributes)
        loader = HonCommandLoader(None, None)
        parent = types.SimpleNamespace(categories={category.name.split(".")[-1].lower(): base})
        loader._commands = {"startProgram": parent}  # noqa: SLF001
        loader._favourites = [{  # noqa: SLF001
            "favouriteName": "Mine",
            "command": {"commandName": "startProgram", "programName": category.name},
            "parameters": dict(saved),
        }]
        loader._add_favourites()  # noqa: SLF001
        return parent.categories["Mine"]

    @staticmethod
    def recover(command: HonCommand, body: dict) -> None:
        """The loader's own recovery of `body` as the last start `/history` lists.

        `_recover_command_state` with `rebuild`, as for a start followed at runtime:
        the programme goes back to its schema, then each key of the body is written.
        The loader is the appliance's own, as `HonAppliance` builds it.
        """
        loader = HonCommandLoader(None, command.appliance)
        loader._command_history = [{"command": {  # noqa: SLF001
            "commandName": "startProgram",
            "programName": command.category,
            "parameters": dict(body),
        }}]
        loader._recover_command_state("startProgram", command, rebuild=True)  # noqa: SLF001

    def test_a_dryer_start_without_a_label_falls_back_to_the_key(self):
        self.start(MIX, dryLevel="4")
        self.assertEqual("PROGRAMS.TD.MIX_AND_DRY", self.api.sent[-1]["program_label"])

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

    def test_a_chosen_dryer_timer_sets_the_duration(self):
        # I1: on the HD90 timed programmes the timer is the user's choice (an enum), so it
        # stays and the duration comes from it: the schema's default, or a favourite's
        # saved value (the history recovery leaves it alone; here a direct write stands
        # in for the favourite). Minutes: the app's decompiled chain run in Node on the
        # category's defaults with these keys (apk2/analysis/issue133-td/strumenti,
        # job 'prod'); the hygiene option adds its 60 minutes.
        cases = (
            (HQD_TIMER, HQD_DE_ATTRIBUTES, {"dryTimeMM": "120"}, "120"),
            (HQD_DUVET, HQD_BNL_ATTRIBUTES, {"dryTimeMM": "105"}, "105"),
            (HQD_DUVET, HQD_BNL_ATTRIBUTES,
             {"dryTimeMM": "105", "sterilizationStatus": "1"}, "165"),
        )
        for category, attributes, values, minutes in cases:
            with self.subTest(programme=category.name, **values):
                params, _ = self.start(category, model_attributes=attributes, **values)
                self.assertEqual(minutes, params["dryTimeMM"])
                self.assertEqual(minutes, params["remainingTimeMM"])

    def test_a_favourite_timer_keeps_its_time(self):
        # I1: a favourite "Timer 90" went out as the default 30. 90 minutes in Node, as above.
        favourite = self.favourite(
            HQD_TIMER, {"dryTimeMM": "90"}, model_attributes=HQD_DE_ATTRIBUTES
        )
        params, _ = self.start(favourite)
        self.assertEqual(("90", "90"), (params["dryTimeMM"], params["remainingTimeMM"]))

    def test_the_level_mode_survives_the_recovery_of_the_first_start(self):
        # I2: the first start goes out in level mode, the history recovery writes its
        # dryTime "0" and dryProgFlag "1" back, and Start pressed again sends the same body.
        command = self.command(WD_LEVEL_MODE, model_attributes=_HQD_WD_ATTRIBUTES)
        first, _ = self.start(command)
        self.recover(command, first)
        self.assertEqual("0", str(command.parameters["dryTime"].value))  # what the row left
        second, _ = self.start(command)
        self.assertEqual(("0", "1"), (first["dryTime"], first["dryProgFlag"]))
        self.assertEqual(first, second)

    def test_a_favourite_saved_in_level_mode_keeps_it(self):
        # I2: the app saves the body it sent, so the favourite holds dryTime "0".
        favourite = self.favourite(
            WD_LEVEL_MODE,
            {"dryLevel": "3", "dryTime": "0", "dryProgFlag": "1"},
            model_attributes=_HQD_WD_ATTRIBUTES,
        )
        params, _ = self.start(favourite)
        self.assertEqual(("0", "1"), (params["dryTime"], params["dryProgFlag"]))

    def test_a_dry_time_of_0_stays_where_the_level_mode_does_not_apply(self):
        # I2 is there for the level mode alone: without a dry level, or on a drying-only
        # programme (programType "D", which the rule skips), a written 0 goes as it is.
        command = self.command(WD_LEVEL_MODE, model_attributes=_HQD_WD_ATTRIBUTES)
        params, _ = self.start(command, dryLevel="0", dryTime="0")
        self.assertEqual(("0", "0"), (params["dryTime"], params["dryProgFlag"]))
        schema = _copy(WD_LEVEL_MODE.schema)
        schema["ancillaryParameters"]["programType"]["fixedValue"] = "D"
        command = self.command(
            WD_LEVEL_MODE._replace(schema=schema), model_attributes=_HQD_WD_ATTRIBUTES
        )
        params, _ = self.start(command, dryTime="0")
        self.assertEqual(("3", "0", "0"),
                         (params["dryLevel"], params["dryTime"], params["dryProgFlag"]))

    def test_a_dryer_timer_with_hygiene_goes_out_the_same_on_every_start(self):
        # A tumble dryer's start carries the duration in `dryTimeMM`, not the timer: 75
        # plus the hygiene's 60 minutes on IOT_DRY_DUVET, 135 in Node as above. Written
        # back by the history recovery, 135 landed on the timer's enum (35..135) and the
        # next start sent 195, then 135 again; the recovery leaves the timer alone and
        # still writes back every other key, the hygiene option included.
        command = self.command(HQD_DUVET, model_attributes=HQD_BNL_ATTRIBUTES)
        command.parameters["sterilizationStatus"].value = "1"
        sent, recovered = [], []
        for _ in range(3):
            params, _ = self.start(command)
            sent.append((params["dryTimeMM"], params["remainingTimeMM"]))
            self.recover(command, params)
            recovered.append(tuple(
                str(command.parameters[key].value)
                for key in ("dryTimeMM", "sterilizationStatus")
            ))
        self.assertEqual([("135", "135")] * 3, sent)
        self.assertEqual([("75", "1")] * 3, recovered)

    def test_a_chg_dryer_keeps_the_catalogs_ancillary_dry_time(self):
        # On the Candy ROE (CHG) the recovery wrote the first start's duration, 180 at
        # level 4 (in Node as above), into the ancillary range `dryTimeMM` (70..220 step
        # 10), and the next start sent 180 there in place of the catalog's 70.
        command = self.command(WHITES)
        first, first_ancillary = self.start(command, dryLevel="4")
        self.recover(command, first)
        self.assertEqual("4", str(command.parameters["dryLevel"].value))  # still recovered
        second, second_ancillary = self.start(command)
        self.assertEqual(("180", "70"), (first["dryTimeMM"], first_ancillary["dryTimeMM"]))
        self.assertEqual(first, second)
        self.assertEqual("70", second_ancillary["dryTimeMM"])

    def test_the_recovery_still_writes_dry_time_mm_off_a_tumble_dryer(self):
        # Only a tumble dryer's start carries a computed `dryTimeMM`: on the washer-dryer
        # the recovery writes a recorded value into the ancillary of that name as before.
        command = self.command(WD_WITH_DRY_TIME_MM, model_attributes=_HQD_WD_ATTRIBUTES)
        self.recover(command, {"prCode": "1", "dryLevel": "2", "dryTimeMM": "90"})
        self.assertEqual(
            ("2", "90"),
            tuple(str(command.parameters[key].value) for key in ("dryLevel", "dryTimeMM")),
        )

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
        # The app compares the platform strictly with 'HQD' and 'CHG' (apk2
        # decomp.txt:1362589-1362599, @1366809-1366816): another case is another platform.
        for attributes in ({"platform": "XYZ"}, {}, None, {"platform": "chg"},
                           {"platform": "hqd"}):
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

    def test_a_computed_key_goes_as_the_schema_writes_it(self):
        params, _ = self.start(WM_WITH_PRSTR_ENUM)        # prStr enum, defaultValue "COTTONS"
        self.assertEqual("COTTONS", params["prStr"])

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
