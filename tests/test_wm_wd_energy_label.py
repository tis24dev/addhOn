# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Issue #99: `energyLabel` of a washer / washer-dryer start outside HQD.

The hOn app 2.30.7 works out the programme's duration from the category's
`remainingTimes` table (`actualDuration`, apk2 decomp.txt:1367613-1368630, behind
`getWashDrySteamCyclesDuration` @1368632-1368677) and turns it into the label
(`calculateEnergyLabel`, @1364133-1364169) that its builder writes in BOTH
`attributes` and `ancillaryParameters` (@1362303-1362323, @1362669-1362736). addhOn
sent the schema's default ('4') and '0' instead.

The 20 cases are the ones worked out by hand from the decompiled code and checked
against it executed in Node (apk2/analysis/issue99-wd/4-actual-duration-energy-label.md
section 2), on the app's own sample catalog: tests/fixtures/wd_hoover_hdpd4149ambc.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
import unittest
from pathlib import Path
from typing import Any

from tests._golden import install_stubs  # the transport's aiohttp / yarl
from tests.test_program_options import _install_stubs as _install_entity_stubs

install_stubs()
_install_entity_stubs()  # the Start button's platform, same stubs as its own tests

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "wd_hoover_hdpd4149ambc"
    / "app_sample_programs.json"
)
SAMPLE = json.loads(FIXTURE.read_text(encoding="utf-8"))
PROGRAMS: dict[str, dict] = SAMPLE["startProgram"]
ECO_KEY = "PROGRAMS.WM_WD.ECO_40_60_NEW_ENERGY_LABEL"
ECO = "eco_40_60_new_energy_label"


def _program(name: str) -> dict:
    return copy.deepcopy(PROGRAMS[f"PROGRAMS.WM_WD.{name}"])


def _screen_values(nodes: dict | None) -> dict[str, Any]:
    """The parameters the app's programme screen sends for an untouched programme.

    The reference harness's own reconstruction (strumenti/energy/cases.py): fixed ->
    fixedValue (suggestedValue when blank), otherwise defaultValue, and the legacy
    screen's `dryTime || '0'`.
    """
    out: dict[str, Any] = {}
    for name, node in (nodes or {}).items():
        if not isinstance(node, dict):
            continue
        if node.get("typology") == "fixed":
            value = node.get("fixedValue")
            if not value and node.get("suggestedValue"):
                value = node["suggestedValue"]
            if value is not None:
                out[name] = value
        elif node.get("defaultValue") is not None:
            out[name] = node["defaultValue"]
        elif name == "dryTime":
            out[name] = "0"
    return out


# id, programme, overrides, dropped, appliance type, minutes, label.
CASES = (
    ("1", "ECO_40_60_NEW_ENERGY_LABEL",
     {"dirtyLevel": "2", "dryLevel": "1", "dryTime": "0", "temp": "40"}, (), "WD", 381, 1),
    ("2", "ECO_40_60_NEW_ENERGY_LABEL",
     {"dirtyLevel": "2", "dryLevel": "0", "dryTime": "0", "temp": "40"}, (), "WD", 151, 3),
    ("3", "ECO_40_60_NEW_ENERGY_LABEL",
     {"dirtyLevel": "2", "dryLevel": "0", "dryTime": "2", "temp": "40"}, (), "WD", 211, 2),
    ("4", "ECO_40_60_NEW_ENERGY_LABEL",
     {"dirtyLevel": "2", "dryLevel": "1", "dryTime": "2", "temp": "40"}, (), "WD", 211, 2),
    ("5", "ECO_40_60_NEW_ENERGY_LABEL",
     {"dirtyLevel": "3", "dryLevel": "0", "temp": "60", "extraRinse1": "1", "spinSpeed": "0"},
     (), "WD", 235, 1),
    ("6", "ECO_40_60_NEW_ENERGY_LABEL",
     {"dirtyLevel": "1", "dryLevel": "0", "temp": "20"}, (), "WD", 116, 5),
    ("7", "IOT_WASH_COTTON", {"dirtyLevel": "3", "dryLevel": "0", "temp": "60"}, (), "WD", 169, 3),
    ("8", "IOT_WASH_COTTON", {"dirtyLevel": "3", "dryLevel": "1", "temp": "60"}, (), "WD", 399, 1),
    ("9", "IOT_WASH_COTTON_STEAM", {"dirtyLevel": "3", "temp": "60"}, (), "WD", 206, 3),
    ("10", "IOT_WASH_DELICATE", {"steamLevel": "2", "temp": "30"}, (), "WD", 88, 3),
    ("11", "IOT_WASH_DELICATE", {"steamLevel": "0", "temp": "30"}, (), "WD", 59, 4),
    ("12", "RAPID_WASH_AND_DRY_59_MIN", {}, (), "WD", 59, 4),
    ("13", "HIGH_DRY", {"dryLevel": "4"}, (), "WD", 220, 0),
    ("14", "HIGH_DRY", {"dryLevel": "1", "dryTime": "2"}, (), "WD", 60, 0),
    ("15", "IOT_WASH_MASKS_REFRESH", {}, (), "WD", 44, 0),
    ("16", "TUMBLING", {}, (), "WD", None, 0),
    ("17", "ECO_40_60_NEW_ENERGY_LABEL",
     {"dirtyLevel": "2", "dryLevel": "1", "temp": "40"}, ("prCode",), "WD", None, 0),
    ("18", "ECO_40_60_NEW_ENERGY_LABEL",
     {"dirtyLevel": "4", "dryLevel": "0", "temp": "40"}, (), "WD", 231, 2),
    ("19", "ECO_40_60_NEW_ENERGY_LABEL",
     {"dirtyLevel": "2", "dryLevel": "2", "temp": "40"}, (), "WD", None, 0),
    ("20", "ECO_40_60_NEW_ENERGY_LABEL",
     {"dirtyLevel": "2", "dryLevel": "1", "temp": "40"}, (), "WM", 151, 3),
)


class DurationAndLabelTest(unittest.TestCase):
    """The port against the 20 reference cases, on the app's sample catalog."""

    def _run(self, programme, overrides, dropped, appliance_type, rt="table"):
        from custom_components.addhon.client.engine import energy_label as el

        program = _program(programme)
        params = _screen_values(program.get("parameters"))
        params.update(overrides)
        for name in dropped:
            params.pop(name, None)
        ancillary = el.ancillary_values(program.get("ancillaryParameters") or {})
        table = program.get("remainingTimes") if rt == "table" else rt
        minutes = el.actual_duration(appliance_type, params, ancillary, table)
        contribution = (program["ancillaryParameters"].get("tempContribution") or {}).get(
            "fixedValue"
        )
        label = el.energy_label(
            minutes, program["parameters"].get("temp"), contribution, params.get("temp")
        )
        return minutes, label

    def test_the_twenty_reference_cases(self) -> None:
        for cid, programme, overrides, dropped, appliance_type, minutes, label in CASES:
            with self.subTest(case=cid, programme=programme):
                got_minutes, got_label = self._run(programme, overrides, dropped, appliance_type)
                self.assertEqual(minutes, got_minutes)
                self.assertEqual(label, got_label)

    def test_no_table_no_duration(self) -> None:
        for table in (None, {}, [], "x", 5):
            with self.subTest(table=table):
                minutes, label = self._run(
                    "ECO_40_60_NEW_ENERGY_LABEL", {"dirtyLevel": "2", "temp": "40"}, (),
                    "WD", rt=table,
                )
                self.assertIsNone(minutes)
                self.assertEqual(0, label)

    def test_only_washers_and_washer_dryers(self) -> None:
        for appliance_type in ("DW", "TD", ""):
            with self.subTest(appliance_type=appliance_type):
                minutes, _ = self._run(
                    "ECO_40_60_NEW_ENERGY_LABEL", {"dirtyLevel": "2"}, (), appliance_type
                )
                self.assertIsNone(minutes)

    def test_dirt_level_default_is_a_string_entry(self) -> None:
        # Case 18's mechanism on its own: '4' is not in the table, `default` ("231",
        # a string) is used and counted as a number.
        minutes, _ = self._run(
            "ECO_40_60_NEW_ENERGY_LABEL", {"dirtyLevel": "4", "dryLevel": "0"}, (), "WD"
        )
        self.assertEqual(231, minutes)

    def test_numeric_strings_follow_the_apps_reading(self) -> None:
        # stringIsNumeric trims blanks for the test, but the table is indexed with the
        # raw value: " 2 " passes, misses the entry "2" (151) and takes `default` (231).
        minutes, _ = self._run(
            "ECO_40_60_NEW_ENERGY_LABEL", {"dirtyLevel": " 2 ", "dryLevel": "0"}, (), "WD"
        )
        self.assertEqual(231, minutes)
        # An integer reads like its string.
        minutes, _ = self._run(
            "ECO_40_60_NEW_ENERGY_LABEL", {"dirtyLevel": 2, "dryLevel": 0}, (), "WD"
        )
        self.assertEqual(151, minutes)
        # A blank string is not numeric at all.
        minutes, _ = self._run(
            "ECO_40_60_NEW_ENERGY_LABEL", {"dirtyLevel": " ", "dryLevel": "0"}, (), "WD"
        )
        self.assertIsNone(minutes)

    def test_a_missing_zero_entry_adds_nothing_a_missing_table_voids(self) -> None:
        program = _program("ECO_40_60_NEW_ENERGY_LABEL")
        params = _screen_values(program["parameters"])
        params.update({"dirtyLevel": "2", "dryLevel": "0", "dryTime": "0", "steamLevel": "0"})
        from custom_components.addhon.client.engine import energy_label as el

        # Washing only with a dry type: the dryLevel table's '+dryTypeC'[0] is added.
        ancillary = el.ancillary_values(program["ancillaryParameters"])
        table = program["remainingTimes"]
        self.assertEqual(151, el.actual_duration("WD", params, ancillary, table))
        table["dryLevel"]["+dryTypeC"].pop("0")
        self.assertEqual(151, el.actual_duration("WD", params, ancillary, table))
        table["dryLevel"]["+dryTypeC"]["0"] = 7
        self.assertEqual(158, el.actual_duration("WD", params, ancillary, table))
        del table["dryLevel"]["+dryTypeC"]
        self.assertIsNone(el.actual_duration("WD", params, ancillary, table))

    def test_negative_entries_are_subtracted(self) -> None:
        # Case 5: the 0-rpm spin is -6 minutes.
        minutes, _ = self._run(
            "ECO_40_60_NEW_ENERGY_LABEL",
            {"dirtyLevel": "3", "dryLevel": "0", "spinSpeed": "0"}, (), "WD",
        )
        self.assertEqual(225, minutes)

    def test_a_malformed_table_gives_no_duration_not_an_error(self) -> None:
        from custom_components.addhon.client.engine import energy_label as el

        program = _program("ECO_40_60_NEW_ENERGY_LABEL")
        params = _screen_values(program["parameters"])
        params.update({"dirtyLevel": "2", "dryLevel": "1"})
        ancillary = el.ancillary_values(program["ancillaryParameters"])
        for key, broken in (
            ("dirtyLevel", None),        # `in` on null: a TypeError in the app
            ("dirtyLevel", 5),
            ("prewash", None),
            ("spinSpeed", {"dryTypeC": None}),
            ("dryLevel", {"+dryTypeC": {"1": "x"}}),
        ):
            with self.subTest(key=key, broken=broken):
                table = copy.deepcopy(program["remainingTimes"])
                table[key] = broken
                self.assertIsNone(el.actual_duration("WD", params, ancillary, table))

    def test_the_label_formula(self) -> None:
        from custom_components.addhon.client.engine import energy_label as el

        temp = {"typology": "enum", "enumValues": ["0", "20", "30", "40", "60"]}
        self.assertEqual(1, el.energy_label(381.0, temp, "1", "40"))
        self.assertEqual(3, el.energy_label(151.0, temp, "1", "40"))
        # Not an enum, no contribution, no duration: the app's '0'.
        self.assertEqual(0, el.energy_label(151.0, {"typology": "fixed"}, "1", "40"))
        self.assertEqual(0, el.energy_label(151.0, None, "1", "40"))
        self.assertEqual(0, el.energy_label(151.0, temp, None, "40"))
        self.assertEqual(0, el.energy_label(151.0, temp, "", "40"))
        self.assertEqual(0, el.energy_label(None, temp, "1", "40"))
        # '0' is truthy in JavaScript: the formula runs with the term zeroed (2.58).
        self.assertEqual(2, el.energy_label(151.0, temp, "0", "40"))
        # Clamped to 1..5.
        self.assertEqual(5, el.energy_label(10.0, temp, "1", "20"))
        self.assertEqual(1, el.energy_label(600.0, temp, "1", "60"))
        # The LAST enum value counts, not the highest: 90 would give 4.
        self.assertEqual(
            2, el.energy_label(151.0, {"typology": "enum", "enumValues": ["90", "60"]}, "1", "60")
        )
        # An unreadable temperature: NaN, then '0'.
        self.assertEqual(0, el.energy_label(151.0, temp, "1", "abc"))

    def test_ancillary_values_map_like_the_app(self) -> None:
        from custom_components.addhon.client.engine import energy_label as el

        nodes = {
            "programType": {"typology": "fixed", "fixedValue": "W+D"},
            "dryType": {"typology": "fixed", "fixedValue": ""},
            "steamType": {"typology": "fixed", "fixedValue": "", "suggestedValue": "S"},
            "energyLabel": {"typology": "range", "defaultValue": "4"},
            "programFamily": {"typology": "enum", "defaultValue": "[dashboard]"},
            "dryOption": {"typology": "range", "minimumValue": "0"},
            "loose": {"category": "general"},
        }
        self.assertEqual(
            {"programType": "W+D", "steamType": "S", "energyLabel": "4",
             "programFamily": "[dashboard]"},
            el.ancillary_values(nodes),
        )


class RemainingTimesKeptTest(unittest.TestCase):
    """HonCommand keeps the raw `remainingTimes` group next to its usual loading."""

    class _Appliance:
        zone = 0
        options: dict = {}
        appliance_type = "WD"

    def _command(self, program: dict):
        from custom_components.addhon.client.engine.commands import HonCommand

        return HonCommand("startProgram", program, self._Appliance(), category_name=ECO_KEY)

    def test_the_group_is_kept_raw(self) -> None:
        program = _program("ECO_40_60_NEW_ENERGY_LABEL")
        expected = copy.deepcopy(program["remainingTimes"])
        command = self._command(program)
        self.assertEqual(expected, command.remaining_times)
        # The usual loading is untouched: the sub-nodes still land flat in `data`.
        for name, node in expected.items():
            self.assertEqual(node, command.data[name])

    def test_no_group_is_none(self) -> None:
        program = _program("ECO_40_60_NEW_ENERGY_LABEL")
        del program["remainingTimes"]
        self.assertIsNone(self._command(program).remaining_times)

    def test_a_favourite_copy_inherits_it(self) -> None:
        command = self._command(_program("ECO_40_60_NEW_ENERGY_LABEL"))
        self.assertIs(command.remaining_times, copy.copy(command).remaining_times)


# ------------------------------------------------------------ end to end, on the wire
class _Response:
    def __init__(self, body: dict) -> None:
        self._body = body
        self.status = 200

    async def json(self, content_type: Any = None) -> dict:
        return copy.deepcopy(self._body)

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _Connection:
    """The cloud boundary: catalog, empty history, optional favourites; POSTs recorded."""

    def __init__(self, catalog: dict, favourites: list | None = None) -> None:
        from custom_components.addhon.client.transport.device import HonDevice

        self._catalog = {"payload": {**copy.deepcopy(catalog), "resultCode": "0"}}
        self._favourites = favourites or []
        self.device = HonDevice("test")
        self.posts: list[dict] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        if url.endswith("/commands/v1/retrieve"):
            return _Response(self._catalog)
        if url.endswith("/history"):
            return _Response({"payload": {"history": []}})
        if url.endswith("/favourite"):
            return _Response({"payload": {"favourites": copy.deepcopy(self._favourites)}})
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url: str, **kwargs: Any) -> _Response:
        if url.endswith("/commands/v1/send"):
            self.posts.append(copy.deepcopy(kwargs.get("json")))
        return _Response({"payload": {"resultCode": "0"}})


class _Coordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.hass = None
        self.last_update_success = True
        self.last_exception = None

    def async_update_listeners(self) -> None:
        pass

    async def async_refresh(self) -> None:
        pass

    async def async_request_refresh(self) -> None:
        pass


class _Hass:
    data: dict = {}

    async def async_add_executor_job(self, func, *args):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(func, *args).result(timeout=20)


class _Client:
    def run_command_sync(self, coro):
        return asyncio.run(coro)


def _catalog(*, platform: str | None, table: bool = True, drop_rows: tuple = ()) -> dict:
    category = _program("ECO_40_60_NEW_ENERGY_LABEL")
    if not table:
        del category["remainingTimes"]
    for row in drop_rows:
        del category["remainingTimes"][row]
    attributes = [] if platform is None else [{"parName": "platform", "parValue": platform}]
    return {"applianceModel": {"attributes": attributes}, "startProgram": {ECO_KEY: category}}


async def _appliance(connection, appliance_type: str = "WD"):
    from custom_components.addhon.client.engine.appliance import HonAppliance
    from custom_components.addhon.client.transport.api import HonApi

    appliance = HonAppliance(HonApi(connection), {
        "applianceTypeName": appliance_type, "applianceModelId": "31010317",
        "macAddress": "aa-bb-cc-dd-ee-99", "code": "31010317",
        "modelName": "HDPD4149AMBC/1-S", "brand": "hoover", "nickName": "WD",
    })
    await appliance.load_commands()
    return appliance


async def _start(connection, *, appliance_type: str = "WD", options: dict | None = None) -> dict:
    """Press Start on Eco 40-60 through the real button, command and API."""
    from custom_components.addhon.button import HonProgramCommandButton

    appliance = await _appliance(connection, appliance_type)
    uid = appliance.unique_id
    coordinator = _Coordinator({uid: {"type": appliance_type, "name": "WD",
                                      "appliance": appliance, "attributes": {},
                                      "settings": {}}})
    coordinator.pending_programs = {uid: ECO}
    coordinator.pending_options = {uid: dict(options or {"dirtyLevel": "2", "temp": "40"})}
    button = HonProgramCommandButton(
        coordinator, uid, _Client(), command_name="startProgram",
        unique_suffix="start_program", translation_key="start_program",
        icon="mdi:play-circle",
    )
    button.hass = _Hass()
    await button.async_press()
    assert len(connection.posts) == 1, connection.posts
    body = connection.posts[0]
    for volatile in ("timestamp", "transactionId"):
        body.pop(volatile)
    return body


class WasherStartEnergyLabelBodyTest(unittest.IsolatedAsyncioTestCase):
    """The whole POSTed body, from the Start button down to HonApi."""

    async def test_eco_40_60_wash_and_dry_carries_the_apps_label(self) -> None:
        # Dirt 2, dryLevel 1 (the schema default), 40 C: 151 + 230 = 381 minutes -> 1.
        for platform in ("CHG", None):
            with self.subTest(platform=platform):
                body = await _start(_Connection(_catalog(platform=platform)))
                self.assertEqual("2", body["parameters"]["dirtyLevel"])
                self.assertEqual("1", body["parameters"]["dryLevel"])
                self.assertEqual("0", body["parameters"]["dryTime"])
                self.assertEqual("40", body["parameters"]["temp"])
                self.assertEqual("1", body["ancillaryParameters"]["energyLabel"])
                self.assertEqual("1", body["attributes"]["energyLabel"])

    async def test_without_the_table_the_body_is_todays(self) -> None:
        with_table = await _start(_Connection(_catalog(platform="CHG")))
        without = await _start(_Connection(_catalog(platform="CHG", table=False)))
        self.assertEqual("4", without["ancillaryParameters"]["energyLabel"])
        self.assertEqual("0", without["attributes"]["energyLabel"])
        # The table changes the two energyLabel values and nothing else.
        with_table["ancillaryParameters"]["energyLabel"] = "4"
        with_table["attributes"]["energyLabel"] = "0"
        self.assertEqual(without, with_table)

    async def test_no_duration_keeps_todays_body(self) -> None:
        # A table without its dirtyLevel row gives the app no duration (label '0'); we
        # keep what we sent before rather than inventing one.
        body = await _start(_Connection(_catalog(platform="CHG", drop_rows=("dirtyLevel",))))
        self.assertEqual("4", body["ancillaryParameters"]["energyLabel"])
        self.assertEqual("0", body["attributes"]["energyLabel"])

    async def test_hqd_keeps_its_own_calculation(self) -> None:
        # The HQD branch reads `remainingTime`, which this category lacks: '0' in both,
        # exactly as before, whatever the table would give.
        body = await _start(_Connection(_catalog(platform="HQD")))
        self.assertEqual("0", body["ancillaryParameters"]["energyLabel"])
        self.assertEqual("0", body["attributes"]["energyLabel"])

    async def test_a_washer_computes_it_too(self) -> None:
        # Case 20: a WM ignores the drying, 151 minutes -> 3. The transport then drops
        # dryLevel from a washer's body, after the label is worked out (as the app does).
        body = await _start(_Connection(_catalog(platform="CHG")), appliance_type="WM")
        self.assertNotIn("dryLevel", body["parameters"])
        self.assertEqual("3", body["ancillaryParameters"]["energyLabel"])
        self.assertEqual("3", body["attributes"]["energyLabel"])

    async def test_a_favourite_inherits_the_table(self) -> None:
        favourite = {"favouriteName": "Mine", "command": {
            "commandName": "startProgram", "programName": ECO_KEY,
            "parameters": {"dirtyLevel": "2", "temp": "40"}}}
        appliance = await _appliance(_Connection(_catalog(platform="CHG"), [favourite]))
        categories = appliance.commands["startProgram"].categories
        self.assertIn("Mine", categories)
        self.assertIsNotNone(categories[ECO].remaining_times)
        self.assertIs(categories[ECO].remaining_times, categories["Mine"].remaining_times)


if __name__ == "__main__":
    unittest.main()
