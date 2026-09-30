# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-type send profile: which keys a sparse patch puts on the wire."""
from __future__ import annotations

from tests._golden import install_stubs

install_stubs()

from custom_components.addhon.client.engine.commands import HonCommand  # noqa: E402
from custom_components.addhon.command_dispatch import (  # noqa: E402
    CommandDispatcher,
    CommandPatch,
)
from custom_components.addhon.send_profiles import (  # noqa: E402
    HPWH,
    LEGACY,
    profile_for,
)


class _Appliance:
    appliance_type = "HW"

    def __init__(self) -> None:
        self.commands: dict = {}
        self.attributes: dict = {"parameters": {}}

    def sync_command_to_params(self, name: str) -> None:
        pass


def _settings(appliance) -> HonCommand:
    """A `settings` command shaped like the HP110M8-9's: two free parameters and
    two of the 22 mandatory fixed ones."""
    attributes = {
        "parameters": {
            "tempSel": {"typology": "range", "minimumValue": "35",
                        "maximumValue": "75", "incrementValue": "1",
                        "defaultValue": "40", "category": "command", "mandatory": 0},
            "onOffStatus": {"typology": "range", "minimumValue": "0",
                            "maximumValue": "1", "incrementValue": "1",
                            "defaultValue": "0", "category": "command", "mandatory": 0},
            "operationName": {"typology": "fixed", "fixedValue": "grTimingPowerOnOff",
                              "category": "command", "mandatory": 1},
            "opp1EcoDays": {"typology": "range", "minimumValue": "0",
                            "maximumValue": "40", "incrementValue": "1",
                            "defaultValue": "0", "category": "command", "mandatory": 1},
        },
        "ancillaryParameters": {},
    }
    command = HonCommand("settings", attributes, appliance, category_name="setParameters")
    appliance.commands["settings"] = command
    return command


def test_every_type_but_hw_keeps_the_legacy_profile() -> None:
    for app_type in ("AC", "WM", "WD", "TD", "DW", "REF", "HO", "AP", "WH", None, ""):
        assert profile_for(app_type) is LEGACY, app_type
    assert profile_for("HW") is HPWH


def test_legacy_backfills_the_mandatory_keys() -> None:
    appliance = _Appliance()
    command = _settings(appliance)
    prepared = CommandDispatcher()._prepare(
        command, CommandPatch("settings", {"tempSel": "45"}, action="t")
    )
    assert set(prepared.payload) == {"tempSel", "operationName", "opp1EcoDays"}


def test_the_hpwh_profile_sends_the_requested_keys_only() -> None:
    appliance = _Appliance()
    command = _settings(appliance)
    prepared = CommandDispatcher()._prepare(
        command, CommandPatch("settings", {"tempSel": "45"}, action="t"), HPWH
    )
    assert dict(prepared.payload) == {"tempSel": "45"}


def test_the_hpwh_profile_keeps_the_requested_order() -> None:
    appliance = _Appliance()
    command = _settings(appliance)
    prepared = CommandDispatcher()._prepare(
        command,
        CommandPatch("settings", {"onOffStatus": "1", "tempSel": "45"}, action="t"),
        HPWH,
    )
    assert list(prepared.payload) == ["onOffStatus", "tempSel"]
