# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-type send profile: which keys a sparse patch puts on the wire."""
from __future__ import annotations

import asyncio

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


class _RecordingApi:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def send_command(self, appliance, command, parameters, ancillary,
                           program_name="", *, wire_command=None, energy_label=True):
        self.calls.append((command, dict(parameters), program_name,
                           wire_command, energy_label))
        return True


def _dispatch(appliance, command, patch):
    api = _RecordingApi()
    command._api = api
    appliance.sync_payload_to_params = lambda payload: None
    ok = asyncio.run(CommandDispatcher().dispatch(appliance, patch))
    return ok, api.calls


def test_an_hw_write_goes_out_as_setparameters_without_energy_label() -> None:
    appliance = _Appliance()
    command = _settings(appliance)
    ok, calls = _dispatch(
        appliance, command, CommandPatch("settings", {"tempSel": "45"}, action="t")
    )
    assert ok is True
    assert calls == [
        ("settings", {"tempSel": "45"}, "setParameters", "setParameters", False)
    ]


def test_another_type_goes_out_exactly_as_before() -> None:
    appliance = _Appliance()
    appliance.appliance_type = "HO"
    command = _settings(appliance)
    ok, calls = _dispatch(
        appliance, command, CommandPatch("settings", {"tempSel": "45"}, action="t")
    )
    assert ok is True
    name, payload, program_name, wire, label = calls[0]
    assert (name, wire, label) == ("settings", None, True)
    assert set(payload) == {"tempSel", "operationName", "opp1EcoDays"}


def test_hpwh_mode_patch_selects_its_category_without_touching_the_others() -> None:
    """`mode_patch` selects the category in `prepare`, so `machMode` lands on the
    category it belongs to; the fixed `machMode` of every other category, which the
    real HW schema declares, stays as it was."""
    from custom_components.addhon import hpwh

    appliance = _Appliance()
    categories: dict = {}
    for name, mach_mode in (("auto", "1"), ("eco", "2"), ("elec", "3"), ("vac", "4")):
        categories[name] = HonCommand(
            "startProgram",
            {
                "parameters": {
                    "machMode": {"typology": "fixed", "fixedValue": mach_mode,
                                 "category": "command", "mandatory": 1},
                    "onOffStatus": {"typology": "fixed", "fixedValue": "1",
                                    "category": "command", "mandatory": 1},
                },
                "ancillaryParameters": {},
            },
            appliance,
            categories=categories,
            category_name=f"PROGRAMS.HW.{name.upper()}",
        )
    appliance.commands["startProgram"] = categories["auto"]
    api = _RecordingApi()
    for command in categories.values():
        command._api = api
    appliance.sync_payload_to_params = lambda payload: None

    ok = asyncio.run(
        CommandDispatcher().dispatch(appliance, hpwh.mode_patch("eco", "2"))
    )

    assert ok is True
    assert len(api.calls) == 1
    _name, payload, program_name, _wire, _label = api.calls[0]
    assert payload == {"machMode": "2"}
    assert program_name == ""
    assert categories["auto"].parameters["machMode"].value == "1"
