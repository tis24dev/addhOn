# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from tests._golden import install_stubs

install_stubs()

from custom_components.addhon.client.engine.commands import HonCommand
from custom_components.addhon.command_dispatch import CommandDispatcher, CommandPatch
from tests.contract_fixtures import load_contract_cases


class _Appliance:
    def __init__(self) -> None:
        self.zone = 0
        self.options: dict[str, str] = {}
        self.commands: dict[str, HonCommand] = {}


def _enum(default: str, values: list[str], *, mandatory: int = 0) -> dict[str, Any]:
    return {
        "typology": "enum",
        "category": "command",
        "mandatory": mandatory,
        "defaultValue": default,
        "enumValues": values,
    }


def _command(*, with_rule: bool = False) -> HonCommand:
    appliance = _Appliance()
    attributes: dict[str, Any] = {
        "parameters": {
            "mode": _enum("1", ["1", "2"]),
            "onOff": {
                "typology": "fixed",
                "category": "command",
                "mandatory": 1,
                "fixedValue": "1",
            },
            "coupled": _enum("0", ["0", "4"]),
            "light": _enum("0", ["0", "1"]),
        }
    }
    if with_rule:
        attributes["ancillaryParameters"] = {
            "programRules": {
                "category": "rule",
                "typology": "fixed",
                "fixedValue": {
                    "coupled": {
                        "mode": {
                            "2": {"typology": "fixed", "fixedValue": "4"}
                        }
                    }
                },
            }
        }
    command = HonCommand("settings", attributes, appliance)
    appliance.commands["settings"] = command
    return command


@pytest.fixture
def dispatcher() -> CommandDispatcher:
    return CommandDispatcher()


@pytest.fixture
def rule_command() -> HonCommand:
    return _command(with_rule=True)


def test_dispatcher_contract_fixture_is_valid() -> None:
    cases = load_contract_cases("dispatcher.json")
    assert {case["id"] for case in cases} == {
        "requested_only",
        "mandatory_included",
        "unrelated_preserved",
    }
    assert all(case["support"] == "confirmed" for case in cases)


def test_patch_copies_values_immutably() -> None:
    values = {"mode": "2"}

    patch = CommandPatch("settings", values, action="set_mode")
    values["mode"] = "4"

    assert dict(patch.values) == {"mode": "2"}
    with pytest.raises(TypeError):
        patch.values["mode"] = "4"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        patch.action = "other"  # type: ignore[misc]


def test_prepare_payload_keeps_only_requested_and_mandatory(
    dispatcher: CommandDispatcher,
) -> None:
    command = _command()

    prepared = dispatcher._prepare(
        command,
        CommandPatch("settings", {"mode": "2"}, action="set_mode"),
    )

    assert prepared.command is command
    assert prepared.payload == {"mode": "2", "onOff": "1"}
    assert prepared.requested_keys == frozenset({"mode"})
    assert prepared.mandatory_keys == frozenset({"onOff"})
    assert prepared.changed_keys == frozenset()
    assert "light" not in prepared.payload
    assert "coupled" not in prepared.payload


def test_prepare_payload_classifies_rule_changed_sibling(
    dispatcher: CommandDispatcher,
    rule_command: HonCommand,
) -> None:
    prepared = dispatcher._prepare(
        rule_command,
        CommandPatch("settings", {"mode": "2"}, action="set_mode"),
    )

    assert prepared.payload == {"mode": "2", "onOff": "1", "coupled": "4"}
    assert prepared.requested_keys == frozenset({"mode"})
    assert prepared.mandatory_keys == frozenset({"onOff"})
    assert prepared.changed_keys == frozenset({"coupled"})


def test_prepare_callback_changes_fields_and_activates_rules(
    dispatcher: CommandDispatcher,
    rule_command: HonCommand,
) -> None:
    def prepare(params: dict[str, Any]) -> None:
        params["mode"].value = "2"

    prepared = dispatcher._prepare(
        rule_command,
        CommandPatch(
            "settings",
            {"light": "1"},
            action="turn_on_light",
            prepare=prepare,
        ),
    )

    assert prepared.payload == {
        "light": "1",
        "onOff": "1",
        "mode": "2",
        "coupled": "4",
    }
    assert prepared.requested_keys == frozenset({"light"})
    assert prepared.mandatory_keys == frozenset({"onOff"})
    assert prepared.changed_keys == frozenset({"mode", "coupled"})


def test_prepare_payload_orders_each_class_without_duplicates(
    dispatcher: CommandDispatcher,
    rule_command: HonCommand,
) -> None:
    prepared = dispatcher._prepare(
        rule_command,
        CommandPatch(
            "settings",
            {"light": "1", "mode": "2", "onOff": "1"},
            action="set_multiple",
        ),
    )

    assert list(prepared.payload) == ["light", "mode", "onOff", "coupled"]
    assert prepared.requested_keys == frozenset({"light", "mode", "onOff"})
    assert prepared.mandatory_keys == frozenset()
    assert prepared.changed_keys == frozenset({"coupled"})


def test_prepare_compares_transmitted_intern_value(
    dispatcher: CommandDispatcher,
) -> None:
    appliance = _Appliance()
    command = HonCommand(
        "settings",
        {
            "parameters": {
                "mode": _enum("dashboard", ["dashboard"]),
                "light": _enum("0", ["0", "1"]),
            }
        },
        appliance,
    )
    appliance.commands["settings"] = command

    prepared = dispatcher._prepare(
        command,
        CommandPatch(
            "settings",
            {"light": "0"},
            action="prepare_mode",
            prepare=lambda params: setattr(params["mode"], "value", "DASHBOARD"),
        ),
    )

    assert command.parameters["mode"].value == "dashboard"
    assert prepared.payload["mode"] == "DASHBOARD"
    assert prepared.changed_keys == frozenset({"mode"})


def test_prepare_returns_command_activated_by_program_parameter(
    dispatcher: CommandDispatcher,
) -> None:
    appliance = _Appliance()
    categories: dict[str, HonCommand] = {}
    first = HonCommand(
        "settings",
        {"parameters": {"mode": _enum("1", ["1", "2"])}},
        appliance,
        categories=categories,
        category_name="first",
    )
    second = HonCommand(
        "settings",
        {"parameters": {"mode": _enum("1", ["1", "2"])}},
        appliance,
        categories=categories,
        category_name="second",
    )
    categories.update({"first": first, "second": second})
    appliance.commands["settings"] = first

    prepared = dispatcher._prepare(
        first,
        CommandPatch("settings", {"category": "second"}, action="set_category"),
    )

    assert prepared.command is second
    assert prepared.payload == {"category": "second"}
    assert prepared.requested_keys == frozenset({"category"})
    assert prepared.mandatory_keys == frozenset()
    assert prepared.changed_keys == frozenset()


@pytest.mark.parametrize(
    "values",
    [
        {"category": "second", "mode": "target"},
        {"mode": "target", "category": "second"},
    ],
    ids=["selector-first", "selector-last"],
)
def test_prepare_applies_values_to_activated_command_in_requested_order(
    dispatcher: CommandDispatcher,
    values: dict[str, str],
) -> None:
    appliance = _Appliance()
    categories: dict[str, HonCommand] = {}
    first = HonCommand(
        "settings",
        {"parameters": {"mode": _enum("old", ["old", "target"])}},
        appliance,
        categories=categories,
        category_name="first",
    )
    second = HonCommand(
        "settings",
        {"parameters": {"mode": _enum("new", ["new", "target"])}},
        appliance,
        categories=categories,
        category_name="second",
    )
    categories.update({"first": first, "second": second})
    appliance.commands["settings"] = first

    prepared = dispatcher._prepare(
        first,
        CommandPatch("settings", values, action="set_category_mode"),
    )

    assert prepared.command is second
    assert first.parameters["mode"].intern_value == "old"
    assert second.parameters["mode"].intern_value == "target"
    assert list(prepared.payload) == list(values)
    assert prepared.payload == values
    assert prepared.requested_keys == frozenset({"category", "mode"})
    assert prepared.mandatory_keys == frozenset()
    assert prepared.changed_keys == frozenset()


def test_prepare_validates_target_schema_before_activating_category(
    dispatcher: CommandDispatcher,
) -> None:
    appliance = _Appliance()
    categories: dict[str, HonCommand] = {}
    first = HonCommand(
        "settings",
        {
            "parameters": {
                "mode": _enum("old", ["old", "target"]),
                "legacy": _enum("0", ["0", "1"]),
            }
        },
        appliance,
        categories=categories,
        category_name="first",
    )
    second = HonCommand(
        "settings",
        {"parameters": {"mode": _enum("new", ["new", "target"])}},
        appliance,
        categories=categories,
        category_name="second",
    )
    categories.update({"first": first, "second": second})
    appliance.commands["settings"] = first

    with pytest.raises(ValueError, match="legacy"):
        dispatcher._prepare(
            first,
            CommandPatch(
                "settings",
                {"category": "second", "legacy": "1"},
                action="invalid_target_field",
            ),
        )

    assert appliance.commands["settings"] is first
    assert first.parameters["legacy"].intern_value == "0"


def test_prepare_rejects_all_off_schema_keys_before_any_mutation(
    dispatcher: CommandDispatcher,
) -> None:
    command = _command()
    prepared_called = False

    def prepare(params: dict[str, Any]) -> None:
        nonlocal prepared_called
        prepared_called = True
        params["light"].value = "1"

    with pytest.raises(ValueError, match=r"missing.*unknown|unknown.*missing"):
        dispatcher._prepare(
            command,
            CommandPatch(
                "settings",
                {"mode": "2", "missing": "x", "unknown": "y"},
                action="bad_patch",
                prepare=prepare,
            ),
        )

    assert prepared_called is False
    assert command.parameters["mode"].intern_value == "1"
    assert command.parameters["light"].intern_value == "0"
