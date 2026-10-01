# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Issue #115 on a heat-pump water heater catalogue the cloud really served (type HW).

The app changes mode with `startProgram {machMode}`: no programName, no prCode. The
command-history recovery used to write that machMode onto the FIRST programme, so on
the M7 catalogues (AUTO first) the water heater sent ECO's "2" for heat_pump.
`test_engine_cluster` pins the rule on synthetic catalogues, AUTO first included;
this pins it on the HP150M8-9 of issue #115 (`hw_hp150m8`, rebuilt from its dump:
VAC first, inferred, and the history `{machMode: "2"}`).

Each catalogue goes through the real loader, and each mode change through the real
dispatcher down to the body handed to the API.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import REPO, install_stubs  # noqa: E402

install_stubs()

from custom_components.addhon.client import factory  # noqa: E402
from custom_components.addhon.command_dispatch import CommandDispatcher  # noqa: E402
from custom_components.addhon.hpwh import HPWH_MODE_CATEGORIES, mode_patch  # noqa: E402
from tests.test_hpwh_water_heater import platform as water_heater  # noqa: E402  (HA stubs)

NaAppliance = factory._native_engine_appliance_cls()
_FIXTURES = REPO / "tests" / "fixtures"
# The machMode each operation must send: the app's HPWHMachMode, 1 AUTO, 2 ECO, 3 ELEC.
_MODES = {"heat_pump": "1", "eco": "2", "electric": "3"}


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name / "catalog.json").read_text(encoding="utf-8"))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Api:
    """The cloud as the fixture recorded it. Sends are kept, never made."""

    def __init__(self, fixture: dict) -> None:
        self._fixture = fixture
        self.sent: list = []

    async def load_commands(self, appliance):
        return json.loads(json.dumps(self._fixture["commands"]))

    async def load_favourites(self, appliance):
        return []

    async def load_command_history(self, appliance):
        return json.loads(json.dumps(self._fixture["command_history"]))

    async def send_command(self, appliance, name, parameters, ancillary, *args, **kwargs):
        self.sent.append((name, dict(parameters)))
        return True


class _RealCatalogue:
    """One real catalogue, loaded as the integration loads it."""

    CASE = ""
    FIRST = ""  # the programme the cloud lists first
    ACTIVE = ""  # the programme the history must select
    EXPLICIT = False  # whether the history selected it
    RECOVERY: dict = {}  # `history_recovery` after the load

    def setUp(self) -> None:
        self.fixture = _fixture(self.CASE)

    def _load(self):
        api = _Api(self.fixture)
        app = NaAppliance(
            api, {"applianceTypeName": "HW", "applianceModelId": 1, "macAddress": "aa-bb"}, zone=0
        )
        _run(app.load_commands())
        return app, api

    def test_the_fixture_is_the_case_it_claims(self) -> None:
        # Anti-vacuity: each case rests on the programme the cloud lists first and on a
        # history start that names no programme. A fixture edited away from either
        # would no longer test the defect.
        start = self.fixture["commands"]["startProgram"]
        self.assertEqual(self.FIRST, next(iter(start)))
        self.assertEqual(
            {"1", "2", "3", "4"},
            {node["parameters"]["machMode"]["fixedValue"] for node in start.values()},
        )
        for row in self.fixture["command_history"]:
            command = row["command"]
            self.assertEqual("startProgram", command["commandName"])
            self.assertNotIn("programName", command)
            self.assertEqual({"machMode"}, set(command["parameters"]))

    def test_the_history_selects_the_programme_of_its_machmode(self) -> None:
        start = self._load()[0].commands["startProgram"]
        self.assertEqual(self.ACTIVE, start.category)
        self.assertEqual(self.EXPLICIT, start.selected_explicitly)

    def test_every_programme_keeps_the_machmode_its_schema_fixes(self) -> None:
        # Issue #115: the recovery wrote ECO's "2" over the first programme's own value.
        start = self._load()[0].commands["startProgram"]
        declared = {
            name.split(".")[-1].lower(): node["parameters"]["machMode"]["fixedValue"]
            for name, node in self.fixture["commands"]["startProgram"].items()
        }
        live = {key: str(c.parameters["machMode"].value) for key, c in start.categories.items()}
        schema = {key: str(c.parameters["machMode"].schema_value)
                  for key, c in start.categories.items()}
        self.assertEqual(declared, schema)
        self.assertEqual(declared, live)

    def test_the_recovery_says_how_it_chose(self) -> None:
        app = self._load()[0]
        self.assertEqual(self.RECOVERY, getattr(app, "history_recovery", None))

    def test_the_water_heater_reads_each_mode_from_its_programme(self) -> None:
        self.assertEqual(_MODES, water_heater._mode_categories(self._load()[0]))

    def test_each_mode_sends_its_own_machmode(self) -> None:
        # What the water heater would put on the wire for each operation: its mode
        # table, then the real dispatcher, on a freshly loaded appliance each time.
        sent = {}
        for operation in _MODES:
            app, api = self._load()
            modes = water_heater._mode_categories(app)
            patch = mode_patch(HPWH_MODE_CATEGORIES[operation], modes[operation])
            self.assertTrue(_run(CommandDispatcher().dispatch(app, patch)))
            sent[operation] = api.sent
        self.assertEqual(
            {operation: [("startProgram", {"machMode": machmode})]
             for operation, machmode in _MODES.items()},
            sent,
        )


class HP150M8Issue115Test(_RealCatalogue, unittest.TestCase):
    """Issue #115: the dump showed vac active carrying ECO's machMode "2"."""

    CASE = "hw_hp150m8"
    FIRST = "PROGRAMS.HW.VAC"
    ACTIVE = "PROGRAMS.HW.ECO"
    EXPLICIT = True
    RECOVERY = {"startProgram": "machMode"}


if __name__ == "__main__":
    unittest.main()
