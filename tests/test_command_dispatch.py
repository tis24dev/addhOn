# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import ast
import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
import textwrap
from typing import Any, Callable

import pytest

from tests._golden import install_stubs

install_stubs()

from custom_components.addhon.client.engine.attributes import HonAttribute
from custom_components.addhon.client.engine.commands import HonCommand
from custom_components.addhon.client.engine.exceptions import ApiError
from custom_components.addhon.command_dispatch import CommandDispatcher, CommandPatch
from tests.contract_fixtures import load_contract_cases


_REPO_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED_LEGACY_CALL_EDGES = {
    "ac_command.py": {
        "async_send_settings": ("async_send_command",),
    },
    "button.py": {
        "HonProgramCommandButton.async_press._do": (
            "client.run_command_sync->_inner",
        ),
        "HonProgramCommandButton.async_press._do._inner": ("command.send",),
    },
    "climate.py": {
        "HaierClimateEntity.async_set_hvac_mode": ("async_send_command",),
    },
    # The cooker hood keeps ONE leg on the legacy sender: `stopProgram`, whose
    # every parameter is `fixed`, so the whole-command send is byte-for-byte the
    # payload this hood's command history shows it accepted AND executed. Its
    # `settings` leg moved to the transactional dispatcher, because that group also
    # holds three clock fields the device never mirrors back and a full-group send
    # zeroed the hood's clock on every speed change. `HonHoodFan._send` argues both
    # halves; the purifier fan in the same module has always dispatched.
    "fan.py": {
        "HonHoodFan._send": ("async_send_command",),
    },
    "number.py": {
        "HonNumber.async_set_native_value": ("async_send_command",),
    },
    "program_options.py": {
        "async_send_program._do": ("client.run_command_sync->_inner",),
        "async_send_program._do._inner": ("command.send",),
    },
    "select.py": {
        "HonRefProgramSelect.async_select_option": ("async_send_command",),
        "HonHobPowerLimitSelect.async_select_option": ("async_send_command",),
    },
    "switch.py": {
        "HonWashingMachinePauseSwitch._send_pause_command._do": (
            "client.run_command_sync->_inner",
        ),
        "HonWashingMachinePauseSwitch._send_pause_command._do._inner": (
            "command.send",
        ),
    },
}


_FORBIDDEN_DISPATCH_SYMBOLS = frozenset(
    {
        "CommandDispatcher",
        "CommandPatch",
        "PreparedCommand",
        "async_dispatch_patch",
        "dispatch_patch_sync",
    }
)
_LEGACY_CALLEES = frozenset({"async_send_command", "run_command_sync", "send"})


def _call_path(node: ast.expr) -> str | None:
    if isinstance(node, (ast.Name, ast.Attribute)):
        return ast.unparse(node)
    return None


def _legacy_call_edges(source: str) -> dict[str, tuple[str, ...]]:
    edges: dict[str, list[str]] = {}

    class LegacyEdgeVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def _visit_function(
            self, node: ast.FunctionDef | ast.AsyncFunctionDef
        ) -> None:
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_FunctionDef = _visit_function
        visit_AsyncFunctionDef = _visit_function

        def visit_Lambda(self, _node: ast.Lambda) -> None:
            return

        def visit_Call(self, node: ast.Call) -> None:
            path = _call_path(node.func)
            name = path.rsplit(".", 1)[-1] if path is not None else None
            if self.scope and path is not None and name in _LEGACY_CALLEES:
                edge = path
                if name == "run_command_sync":
                    target = (
                        _call_path(node.args[0].func)
                        if node.args
                        and isinstance(node.args[0], ast.Call)
                        else None
                    )
                    edge = f"{path}->{target or '<invalid>'}"
                edges.setdefault(".".join(self.scope), []).append(edge)
            self.generic_visit(node)

    LegacyEdgeVisitor().visit(ast.parse(source))
    return {scope: tuple(calls) for scope, calls in edges.items()}


def _dispatcher_violations(source: str) -> set[str]:
    violations: set[str] = set()
    imported_symbol_aliases: set[str] = set()
    tree = ast.parse(source)

    class DispatcherVisitor(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                if alias.name.endswith(".command_dispatch"):
                    rendered = alias.name
                    if alias.asname:
                        rendered += f" as {alias.asname}"
                    violations.add(f"forbidden-import:{rendered}")

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            module = node.module or ""
            imports_dispatch_module = module.endswith("command_dispatch")
            for alias in node.names:
                if imports_dispatch_module and (
                    alias.name in _FORBIDDEN_DISPATCH_SYMBOLS
                    or alias.name == "*"
                ):
                    rendered = alias.name
                    if alias.asname:
                        rendered += f" as {alias.asname}"
                    violations.add(f"forbidden-import:{rendered}")
                    local_name = alias.asname or alias.name
                    imported_symbol_aliases.add(local_name)
                elif (
                    module.endswith("custom_components.addhon")
                    or node.level > 0
                ) and alias.name == "command_dispatch":
                    rendered = f"{module + '.' if module else ''}{alias.name}"
                    if alias.asname:
                        rendered += f" as {alias.asname}"
                    violations.add(f"forbidden-import:{rendered}")

        def visit_Name(self, node: ast.Name) -> None:
            if (
                node.id in _FORBIDDEN_DISPATCH_SYMBOLS
                and node.id not in imported_symbol_aliases
            ):
                violations.add(f"forbidden-reference:{node.id}")

        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Attribute) and node.func.attr == "dispatch":
                violations.add(f"dispatcher-call:{ast.unparse(node.func)}")
            self.generic_visit(node)

        def visit_Attribute(self, node: ast.Attribute) -> None:
            rendered = ast.unparse(node)
            if node.attr in _FORBIDDEN_DISPATCH_SYMBOLS:
                violations.add(f"forbidden-reference:{rendered}")
            self.generic_visit(node)

    DispatcherVisitor().visit(tree)
    return violations


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


def test_existing_entities_keep_legacy_command_entry_points() -> None:
    component_root = _REPO_ROOT / "custom_components" / "addhon"

    observed = {
        filename: _legacy_call_edges(
            (component_root / filename).read_text(encoding="utf-8")
        )
        for filename in _EXPECTED_LEGACY_CALL_EDGES
    }

    assert observed == _EXPECTED_LEGACY_CALL_EDGES


def test_dispatcher_has_no_production_entity_caller() -> None:
    """No LEGACY appliance path may reach the transactional dispatcher.

    This guard originally kept the dispatcher wholly dormant. The air purifier
    is the sanctioned caller: `air_purifier.py` builds every AP write as a
    CommandPatch and each AP platform module dispatches it, so those are
    allow-listed here while every legacy entity module stays pinned to its
    existing sender. Widening this set is a deliberate act -- adding a module
    means declaring that its writes are transactional.
    """
    component_root = _REPO_ROOT / "custom_components" / "addhon"
    allowed = {
        Path("command_dispatch.py"),
        Path("hon_client.py"),
        Path("air_purifier.py"),
        Path("fan.py"),
        Path("light.py"),
        # Mixed files: the AP entities dispatch, the legacy ones stay legacy.
        # test_mixed_platform_legacy_classes_keep_the_legacy_sender guards the split,
        # which this file-level allow-list can no longer express.
        Path("switch.py"),
        Path("select.py"),
        Path("number.py"),
    }
    violations = {
        path.relative_to(component_root): _dispatcher_violations(
            path.read_text(encoding="utf-8")
        )
        for path in component_root.rglob("*.py")
        if path.relative_to(component_root) not in allowed
    }

    assert {path: found for path, found in violations.items() if found} == {}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            """
            from custom_components.addhon.command_dispatch import (
                CommandDispatcher as SparseSender,
            )

            async def write(appliance, patch):
                runner = SparseSender()
                await runner.dispatch(appliance, patch)
            """,
            {
                "forbidden-import:CommandDispatcher as SparseSender",
                "dispatcher-call:runner.dispatch",
            },
        ),
        (
            """
            import custom_components.addhon.command_dispatch as sparse

            async def write(hass, client, appliance):
                await sparse.async_dispatch_patch(hass, client, appliance, object())
            """,
            {
                "forbidden-import:custom_components.addhon.command_dispatch as sparse",
                "forbidden-reference:sparse.async_dispatch_patch",
            },
        ),
        (
            """
            async def wrapper(runner, appliance, patch):
                return await runner.dispatch(appliance, patch)
            """,
            {"dispatcher-call:runner.dispatch"},
        ),
        (
            """
            def write(client, appliance, patch):
                return client.dispatch_patch_sync(appliance, patch)
            """,
            {"forbidden-reference:client.dispatch_patch_sync"},
        ),
        (
            """
            def write(patch: CommandPatch) -> PreparedCommand:
                return patch
            """,
            {
                "forbidden-reference:CommandPatch",
                "forbidden-reference:PreparedCommand",
            },
        ),
    ],
    ids=[
        "constructor-alias",
        "module-alias",
        "dispatch-wrapper",
        "sync-wrapper",
        "dispatcher-types",
    ],
)
def test_dispatcher_guard_rejects_synthetic_migrations(
    source: str,
    expected: set[str],
) -> None:
    assert _dispatcher_violations(textwrap.dedent(source)) == expected


def test_dispatcher_guard_accepts_linked_legacy_nested_send() -> None:
    source = textwrap.dedent(
        """
        def write(client):
            def _do():
                async def _inner():
                    await command.send()
                client.run_command_sync(_inner())
                event_router.publish("sent")
        """
    )

    assert _dispatcher_violations(source) == set()
    assert _legacy_call_edges(source) == {
        "write._do": ("client.run_command_sync->_inner",),
        "write._do._inner": ("command.send",),
    }


def test_legacy_edge_guard_rejects_wrong_send_receiver() -> None:
    source = textwrap.dedent(
        """
        def write(client):
            def _do():
                async def _inner():
                    await telemetry.send()
                client.run_command_sync(_inner())
        """
    )

    observed = _legacy_call_edges(source)

    assert observed == {
        "write._do": ("client.run_command_sync->_inner",),
        "write._do._inner": ("telemetry.send",),
    }
    assert observed["write._do._inner"] != ("command.send",)


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


class _DispatchAppliance(_Appliance):
    def __init__(self) -> None:
        super().__init__()
        self.info: dict[str, Any] = {}
        self.attributes = {
            "parameters": {
                "category": HonAttribute("first"),
                "mode": HonAttribute("old"),
                "coupled": HonAttribute("0"),
                "untouched": HonAttribute("keep"),
            }
        }
        self.synced_payloads: list[dict[str, str | float]] = []

    def sync_payload_to_params(
        self,
        payload: dict[str, str | float],
    ) -> None:
        self.synced_payloads.append(dict(payload))
        for key, value in payload.items():
            if attribute := self.attributes["parameters"].get(key):
                attribute.update(str(value), shield=True)


class _DispatchCommand(HonCommand):
    def __init__(
        self,
        name: str,
        attributes: dict[str, Any],
        appliance: _DispatchAppliance,
        categories: dict[str, HonCommand],
        category_name: str,
    ) -> None:
        self.send_result: bool = True
        self.send_error: BaseException | None = None
        self.before_send: Callable[[], None] | None = None
        self.sent_payloads: list[dict[str, str | float]] = []
        super().__init__(
            name,
            attributes,
            appliance,
            categories=categories,
            category_name=category_name,
        )

    async def send_exact(self, payload: dict[str, str | float]) -> bool:
        self.sent_payloads.append(dict(payload))
        if self.before_send is not None:
            self.before_send()
        if self.send_error is not None:
            raise self.send_error
        return self.send_result


def _dispatch_appliance() -> tuple[
    _DispatchAppliance,
    _DispatchCommand,
    _DispatchCommand,
]:
    appliance = _DispatchAppliance()
    categories: dict[str, HonCommand] = {}
    first = _DispatchCommand(
        "settings",
        {
            "parameters": {
                "mode": _enum("old", ["old", "target"]),
                "coupled": _enum("0", ["0", "4"]),
            }
        },
        appliance,
        categories,
        "first",
    )
    second = _DispatchCommand(
        "settings",
        {
            "parameters": {
                "mode": _enum("new", ["new", "target"]),
                "coupled": _enum("0", ["0", "4"]),
            },
            "ancillaryParameters": {
                "programRules": {
                    "category": "rule",
                    "typology": "fixed",
                    "fixedValue": {
                        "coupled": {
                            "mode": {
                                "target": {
                                    "typology": "enum",
                                    "enumValues": "4",
                                    "defaultValue": "4",
                                }
                            }
                        }
                    },
                }
            },
        },
        appliance,
        categories,
        "second",
    )
    categories.update({"first": first, "second": second})
    appliance.commands["settings"] = first
    return appliance, first, second


class _ConcurrencyState:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.order: list[str] = []


def _block_command_sends(
    command: _DispatchCommand,
    *,
    label: str,
    started: asyncio.Queue[str],
    releases: list[asyncio.Event],
    state: _ConcurrencyState,
) -> None:
    call_index = 0

    async def send_exact(payload: dict[str, str | float]) -> bool:
        nonlocal call_index
        release = releases[call_index]
        call_index += 1
        command.sent_payloads.append(dict(payload))
        state.active += 1
        state.max_active = max(state.max_active, state.active)
        state.order.append(str(payload.get("mode", label)))
        started.put_nowait(label)
        try:
            await release.wait()
        finally:
            state.active -= 1
        return True

    command.send_exact = send_exact  # type: ignore[method-assign]


def _all_commands(appliance: _DispatchAppliance) -> list[HonCommand]:
    pending = list(appliance.commands.values())
    commands: list[HonCommand] = []
    visited: set[int] = set()
    while pending:
        command = pending.pop()
        if id(command) in visited:
            continue
        visited.add(id(command))
        commands.append(command)
        pending.extend(command.categories.values())
    return commands


def _snapshot_transaction(appliance: _DispatchAppliance) -> dict[str, Any]:
    commands = _all_commands(appliance)
    return {
        "pointers": {
            name: (id(command), command.category)
            for name, command in appliance.commands.items()
        },
        "commands": {
            id(command): {
                name: dict(parameter.__dict__)
                for name, parameter in command.parameters.items()
            }
            for command in commands
        },
        "shadow": {
            name: dict(parameter.__dict__)
            for name, parameter in appliance.attributes["parameters"].items()
        },
    }


def _category_patch() -> CommandPatch:
    return CommandPatch(
        "settings",
        {"category": "second", "mode": "target"},
        action="select_category_mode",
    )


def _race_concurrent_update_during_send(
    appliance: _DispatchAppliance,
    command: _DispatchCommand,
) -> None:
    """Simulate an MQTT push landing on the SAME live objects while send_exact
    is in flight, AFTER the transaction's own prepare()-time writes. "coupled"
    is rule-cascaded to "4" by our own patch, but this write lands after that
    (same as a real MQTT thread racing the awaited send); "mode" on the shadow
    was never transaction-owned at all. Rollback must leave both exactly as
    this race left them, and still undo only its own untouched-since writes
    (the category swap and `mode` on the command)."""
    command.parameters["coupled"].values = ["raced"]
    command.parameters["coupled"].value = "raced"
    appliance.attributes["parameters"]["mode"].update("raced", shield=True)


def test_dispatch_transaction_success_syncs_exact_payload_once() -> None:
    appliance, first, second = _dispatch_appliance()
    untouched_before = dict(
        appliance.attributes["parameters"]["untouched"].__dict__
    )

    result = asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    expected = {"category": "second", "mode": "target", "coupled": "4"}
    assert result is True
    assert appliance.commands["settings"] is second
    assert appliance.commands["settings"] is not first
    assert second.sent_payloads == [expected]
    assert appliance.synced_payloads == [expected]
    assert second.parameters["coupled"].values == ["4"]
    assert (
        dict(appliance.attributes["parameters"]["untouched"].__dict__)
        == untouched_before
    )


def test_dispatch_post_commit_sync_error_keeps_committed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.addhon.command_dispatch as dispatch_module

    appliance, first, second = _dispatch_appliance()
    recorded_payloads: list[dict[str, str | float]] = []
    events: list[tuple[str, dict[str, object]]] = []

    def partial_sync(payload: dict[str, str | float]) -> None:
        appliance.synced_payloads.append(dict(payload))
        appliance.attributes["parameters"]["category"].update(
            str(payload["category"]), shield=True
        )
        raise RuntimeError("sync boom")

    appliance.sync_payload_to_params = partial_sync  # type: ignore[method-assign]
    monkeypatch.setattr(
        dispatch_module,
        "record_expected_update",
        lambda _appliance, _action, payload: recorded_payloads.append(payload),
    )
    monkeypatch.setattr(
        dispatch_module,
        "emit_command_event",
        lambda event, fields: events.append((event, dict(fields))),
    )

    result = asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    expected = {"category": "second", "mode": "target", "coupled": "4"}
    assert result is True
    assert appliance.commands["settings"] is second
    assert appliance.commands["settings"] is not first
    assert second.sent_payloads == [expected]
    assert appliance.synced_payloads == [expected]
    assert appliance.attributes["parameters"]["category"].value == "second"
    assert second.parameters["mode"].intern_value == "target"
    assert recorded_payloads == [expected]
    result_fields = next(fields for event, fields in events if event == "command_result")
    assert result_fields["result"] is True
    assert result_fields["outcome"] == "committed_sync_error"
    assert result_fields["error_type"] == "RuntimeError"


def test_dispatch_diagnostic_events_describe_successful_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.addhon.command_dispatch as dispatch_module

    appliance, _, _ = _dispatch_appliance()
    appliance.appliance_type = "test"
    events: list[tuple[str, dict[str, object]]] = []

    def capture(event: str, fields: dict[str, object]) -> None:
        events.append((event, dict(fields)))

    monkeypatch.setattr(dispatch_module, "emit_command_event", capture)

    result = asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    assert result is True
    assert [event for event, _ in events] == [
        "command_intent",
        "command_payload",
        "command_result",
        "shadow_update",
        "contract_check",
    ]
    intent = events[0][1]
    assert intent["action"] == "select_category_mode"
    assert intent["command"] == "settings"
    assert intent["appliance_type"] == "test"
    payload = events[1][1]
    assert payload["requested_keys"] == ["category", "mode"]
    assert payload["mandatory_keys"] == []
    assert payload["rule_added_keys"] == ["coupled"]
    result_event = events[2][1]
    assert result_event["result"] is True
    assert isinstance(result_event["latency_ms"], float)
    assert result_event["latency_ms"] >= 0
    shadow = events[3][1]
    assert shadow["expected_keys"] == ["category", "coupled", "mode"]
    assert shadow["unexpected_keys"] == []
    assert shadow["missing_keys"] == []
    assert events[4][1]["unexpected_keys"] == []


def test_dispatch_diagnostic_reports_cloud_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.addhon.command_dispatch as dispatch_module

    appliance, _, command = _dispatch_appliance()
    command.send_result = False
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        dispatch_module,
        "emit_command_event",
        lambda event, fields: events.append((event, dict(fields))),
    )

    result = asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    assert result is False
    result_fields = next(fields for event, fields in events if event == "command_result")
    assert result_fields["result"] is False
    assert result_fields["outcome"] == "cloud_rejected"
    assert isinstance(result_fields["latency_ms"], float)
    assert not any(event == "shadow_update" for event, _ in events)


def test_a_cloud_refusal_is_labelled_as_one_however_it_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`outcome="cloud_rejected"` used to sit only on the branch that returns False,
    which no real refusal reaches: the api answers with a literal True or False, and
    HonCommand._send_parameters raises ApiError on anything falsy. So every actual
    refusal was recorded as a generic "error", indistinguishable in diagnostics from a
    transport or preparation failure, and the label counted zero in the field.
    """
    import custom_components.addhon.command_dispatch as dispatch_module

    appliance, _, command = _dispatch_appliance()
    command.send_error = ApiError("Can't send command")
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        dispatch_module,
        "emit_command_event",
        lambda event, fields: events.append((event, dict(fields))),
    )

    with pytest.raises(ApiError):
        asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    result_fields = next(fields for event, fields in events if event == "command_result")
    assert result_fields["result"] is False
    assert result_fields["outcome"] == "cloud_rejected"


def test_any_other_failure_stays_a_generic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The label must stay meaningful: only a refusal may carry it."""
    import custom_components.addhon.command_dispatch as dispatch_module

    appliance, _, command = _dispatch_appliance()
    command.send_error = TimeoutError("transport timeout")
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        dispatch_module,
        "emit_command_event",
        lambda event, fields: events.append((event, dict(fields))),
    )

    with pytest.raises(TimeoutError):
        asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    result_fields = next(fields for event, fields in events if event == "command_result")
    assert result_fields["outcome"] == "error"


def test_a_broken_diagnostic_is_swallowed_but_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: Any,
) -> None:
    """Both halves of the contract, together.

    Diagnostics must never affect the command, which is why these wrappers swallow.
    But a bare `pass` made a systematically broken diagnostic indistinguishable from a
    healthy one with nothing to say: the correlation could be dead for every command
    and look identical from outside.
    """
    import logging

    import custom_components.addhon.command_dispatch as dispatch_module

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("diagnostics exploded")

    monkeypatch.setattr(dispatch_module, "emit_command_event", _explode)
    monkeypatch.setattr(dispatch_module, "record_expected_update", _explode)

    appliance, _, _command = _dispatch_appliance()

    with caplog.at_level(logging.DEBUG, logger=dispatch_module.__name__):
        result = asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    # Swallowed: the command still committed.
    assert result is True
    # And traced: every wrapper that fired left a record.
    failures = [
        record
        for record in caplog.records
        if record.name == dispatch_module.__name__
        and "diagnostics step failed" in record.getMessage()
    ]
    assert len(failures) >= 3, [r.getMessage() for r in caplog.records]
    assert all(record.exc_info for record in failures)


def test_the_fixture_loader_catches_a_duplicate_id_in_either_spelling(
    tmp_path: Any,
) -> None:
    """The loader guards every contract matrix in this suite, so a duplicate id it
    misses means one case silently shadows another in any id-keyed lookup.

    It compared the RAW id against a set of strings, so mixed spellings slipped: "1"
    recorded first, then a bare 1, matched nothing and collapsed onto the same entry.
    """
    import json

    import tests.contract_fixtures as fixtures

    def _case(case_id: object) -> dict[str, object]:
        return {
            "id": case_id,
            "evidence": sorted(fixtures._EVIDENCE)[0],
            "support": sorted(fixtures._SUPPORT)[0],
            **{key: "x" for key in fixtures._REQUIRED - {"id", "evidence", "support"}},
        }

    for first, second in (("1", 1), (1, "1"), ("a", "a")):
        path = tmp_path / "cases.json"
        path.write_text(json.dumps([_case(first), _case(second)]), encoding="utf-8")
        original_root = fixtures._ROOT
        fixtures._ROOT = tmp_path
        try:
            with pytest.raises(AssertionError, match="duplicate id"):
                fixtures.load_contract_cases("cases.json")
        finally:
            fixtures._ROOT = original_root

    # The control: genuinely distinct ids still load.
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([_case("1"), _case("2")]), encoding="utf-8")
    original_root = fixtures._ROOT
    fixtures._ROOT = tmp_path
    try:
        assert len(fixtures.load_contract_cases("cases.json")) == 2
    finally:
        fixtures._ROOT = original_root


def test_adapter_localizes_the_reachable_refusal() -> None:
    """The refusal that actually happens arrives as ApiError, not as False.

    Before this it passed through the adapter untouched and reached the entity as a
    generic failure carrying the untranslated literal "Can't send command", which an
    Italian user read as "Comando non riuscito: Can't send command". The carefully
    worded localized string sat on the unreachable branch.
    """
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.addhon.command_dispatch import async_dispatch_patch

    refusal = ApiError("Can't send command")

    class Client:
        def dispatch_patch_sync(self, _appliance: object, _patch: CommandPatch) -> bool:
            raise refusal

    class Hass:
        async def async_add_executor_job(
            self,
            func: Callable[..., Any],
            *args: object,
        ) -> Any:
            return func(*args)

    patch = CommandPatch("settings", {"mode": "2"}, action="set_mode")

    with pytest.raises(HomeAssistantError) as exc_info:
        asyncio.run(async_dispatch_patch(Hass(), Client(), object(), patch))

    assert exc_info.value.translation_key == "command_rejected"
    assert exc_info.value.__cause__ is refusal


def test_dispatch_diagnostic_failure_cannot_change_command_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.addhon.command_dispatch as dispatch_module

    appliance, _, second = _dispatch_appliance()

    def fail_diagnostic(_event: str, _fields: dict[str, object]) -> None:
        raise RuntimeError("diagnostic failure")

    monkeypatch.setattr(dispatch_module, "emit_command_event", fail_diagnostic)
    monkeypatch.setattr(
        dispatch_module,
        "record_expected_update",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("correlation failure")),
    )

    result = asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    assert result is True
    assert second.sent_payloads == [
        {"category": "second", "mode": "target", "coupled": "4"}
    ]


def test_dispatch_diagnostic_appliance_type_failure_cannot_block_send() -> None:
    appliance, _, second = _dispatch_appliance()

    class RaisingString:
        def __str__(self) -> str:
            raise RuntimeError("identity string failure")

    appliance.appliance_type = RaisingString()

    result = asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    assert result is True
    assert second.sent_payloads == [
        {"category": "second", "mode": "target", "coupled": "4"}
    ]


def test_dispatch_diagnostic_mixed_shadow_keys_do_not_rollback_sent_command() -> None:
    appliance, first, second = _dispatch_appliance()
    mixed_key = 1
    mixed_attribute = HonAttribute("before")
    appliance.attributes["parameters"][mixed_key] = mixed_attribute
    second.before_send = lambda: mixed_attribute.update("after", shield=True)

    result = asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    assert result is True
    assert appliance.commands["settings"] is second
    assert appliance.commands["settings"] is not first
    assert str(mixed_attribute) == "after"
    assert second.sent_payloads == [
        {"category": "second", "mode": "target", "coupled": "4"}
    ]
    assert appliance.synced_payloads == [
        {"category": "second", "mode": "target", "coupled": "4"}
    ]


def test_dispatch_transaction_rolls_back_assignment_failure() -> None:
    appliance, first, second = _dispatch_appliance()
    before = _snapshot_transaction(appliance)
    patch = CommandPatch(
        "settings",
        {"category": "second", "mode": "missing"},
        action="invalid_assignment",
    )

    with pytest.raises(ValueError, match="Allowed values"):
        asyncio.run(CommandDispatcher().dispatch(appliance, patch))

    assert _snapshot_transaction(appliance) == before
    assert appliance.commands["settings"] is first
    assert second.sent_payloads == []
    assert appliance.synced_payloads == []


def test_dispatch_transaction_rolls_back_prepare_failure() -> None:
    appliance, _, second = _dispatch_appliance()
    before = _snapshot_transaction(appliance)

    def fail_prepare(parameters: dict[str, Any]) -> None:
        parameters["coupled"].values = ["corrupt"]
        parameters["coupled"].value = "corrupt"
        raise RuntimeError("prepare boom")

    patch = CommandPatch(
        "settings",
        {"category": "second", "mode": "target"},
        action="prepare_failure",
        prepare=fail_prepare,
    )

    with pytest.raises(RuntimeError, match="prepare boom"):
        asyncio.run(CommandDispatcher().dispatch(appliance, patch))

    assert _snapshot_transaction(appliance) == before
    assert second.sent_payloads == []
    assert appliance.synced_payloads == []


def _assert_rollback_undid_own_write_but_kept_the_race(
    appliance: _DispatchAppliance,
    first: _DispatchCommand,
    second: _DispatchCommand,
) -> None:
    # The transaction's own prepare()-time write, untouched by the race,
    # is still correctly undone.
    assert appliance.commands["settings"] is first
    assert second.parameters["mode"].intern_value == "new"
    # The race that landed after our own write (like an MQTT push during the
    # awaited send) is left exactly as it left things -- not clobbered by the
    # stale pre-send snapshot.
    assert second.parameters["coupled"].intern_value == "raced"
    assert appliance.attributes["parameters"]["mode"].value == "raced"
    assert appliance.synced_payloads == []


def test_dispatch_transaction_rolls_back_cloud_false() -> None:
    appliance, first, second = _dispatch_appliance()
    second.send_result = False
    second.before_send = lambda: _race_concurrent_update_during_send(
        appliance, second
    )

    result = asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    assert result is False
    _assert_rollback_undid_own_write_but_kept_the_race(appliance, first, second)


def test_dispatch_transaction_rolls_back_cloud_api_error() -> None:
    appliance, first, second = _dispatch_appliance()
    second.send_error = ApiError("cloud rejected")
    second.before_send = lambda: _race_concurrent_update_during_send(
        appliance, second
    )

    with pytest.raises(ApiError, match="cloud rejected"):
        asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    _assert_rollback_undid_own_write_but_kept_the_race(appliance, first, second)


def test_dispatch_transaction_rolls_back_transport_exception() -> None:
    appliance, first, second = _dispatch_appliance()
    second.send_error = RuntimeError("send boom")
    second.before_send = lambda: _race_concurrent_update_during_send(
        appliance, second
    )

    with pytest.raises(RuntimeError, match="send boom"):
        asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    _assert_rollback_undid_own_write_but_kept_the_race(appliance, first, second)


def test_dispatch_cancellation_rolls_back_and_propagates() -> None:
    appliance, first, second = _dispatch_appliance()
    second.send_error = asyncio.CancelledError()
    second.before_send = lambda: _race_concurrent_update_during_send(
        appliance, second
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    _assert_rollback_undid_own_write_but_kept_the_race(appliance, first, second)


def test_dispatch_rollback_preserves_update_landing_while_send_is_suspended() -> None:
    """The same scenario as the rollback tests above, but the concurrent update
    is applied by a SEPARATE task while `dispatch` is genuinely suspended at an
    `await` inside `send_exact` -- true event-loop interleaving, matching how a
    real MQTT push (scheduled onto the loop from the awscrt thread) would land
    relative to an in-flight send -- rather than a synchronous pre-return hook."""

    async def scenario() -> None:
        appliance, first, second = _dispatch_appliance()
        dispatcher = CommandDispatcher()
        started = asyncio.Event()
        release = asyncio.Event()

        async def send_exact(payload: dict[str, str | float]) -> bool:
            second.sent_payloads.append(dict(payload))
            started.set()
            await release.wait()
            raise RuntimeError("cloud send failed")

        second.send_exact = send_exact  # type: ignore[method-assign]

        dispatch_task = asyncio.create_task(
            dispatcher.dispatch(appliance, _category_patch())
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        async def mqtt_push() -> None:
            second.parameters["coupled"].values = ["mqtt_live", "0", "4"]
            second.parameters["coupled"].value = "mqtt_live"
            appliance.attributes["parameters"]["mode"].update(
                "mqtt_live", shield=True
            )

        await asyncio.create_task(mqtt_push())
        release.set()

        with pytest.raises(RuntimeError, match="cloud send failed"):
            await dispatch_task

        assert appliance.commands["settings"] is first
        assert second.parameters["mode"].intern_value == "new"
        assert second.parameters["coupled"].intern_value == "mqtt_live"
        assert appliance.attributes["parameters"]["mode"].value == "mqtt_live"
        assert appliance.synced_payloads == []

    asyncio.run(scenario())


def test_dispatch_serializes_same_appliance_without_unique_id_in_order() -> None:
    async def scenario() -> None:
        appliance, _, command = _dispatch_appliance()
        dispatcher = CommandDispatcher()
        started: asyncio.Queue[str] = asyncio.Queue()
        releases = [asyncio.Event(), asyncio.Event()]
        state = _ConcurrencyState()
        _block_command_sends(
            command,
            label="same",
            started=started,
            releases=releases,
            state=state,
        )

        first = asyncio.create_task(
            dispatcher.dispatch(appliance, _category_patch())
        )
        assert await asyncio.wait_for(started.get(), timeout=1) == "same"
        second = asyncio.create_task(
            dispatcher.dispatch(
                appliance,
                CommandPatch(
                    "settings",
                    {"category": "second", "mode": "new"},
                    action="select_category_new",
                ),
            )
        )
        await asyncio.sleep(0)

        assert state.active == 1
        assert dispatcher.lock_count == 1

        releases[0].set()
        assert await asyncio.wait_for(started.get(), timeout=1) == "same"
        releases[1].set()

        assert await asyncio.gather(first, second) == [True, True]
        assert state.order == ["target", "new"]
        assert state.max_active == 1
        assert dispatcher.lock_count == 0

    asyncio.run(scenario())


async def _assert_appliance_dispatches_overlap(
    first_appliance: _DispatchAppliance,
    first_command: _DispatchCommand,
    second_appliance: _DispatchAppliance,
    second_command: _DispatchCommand,
) -> None:
    dispatcher = CommandDispatcher()
    started: asyncio.Queue[str] = asyncio.Queue()
    releases = [asyncio.Event(), asyncio.Event()]
    state = _ConcurrencyState()
    _block_command_sends(
        first_command,
        label="first",
        started=started,
        releases=[releases[0]],
        state=state,
    )
    _block_command_sends(
        second_command,
        label="second",
        started=started,
        releases=[releases[1]],
        state=state,
    )

    tasks = [
        asyncio.create_task(
            dispatcher.dispatch(first_appliance, _category_patch())
        ),
        asyncio.create_task(
            dispatcher.dispatch(second_appliance, _category_patch())
        ),
    ]
    labels = {
        await asyncio.wait_for(started.get(), timeout=1),
        await asyncio.wait_for(started.get(), timeout=1),
    }

    assert labels == {"first", "second"}
    assert state.max_active == 2
    assert dispatcher.lock_count == 2

    for release in releases:
        release.set()

    assert await asyncio.gather(*tasks) == [True, True]
    assert dispatcher.lock_count == 0


def test_concurrent_dispatches_for_different_appliances_overlap() -> None:
    first_appliance, _, first_command = _dispatch_appliance()
    second_appliance, _, second_command = _dispatch_appliance()

    asyncio.run(
        _assert_appliance_dispatches_overlap(
            first_appliance,
            first_command,
            second_appliance,
            second_command,
        )
    )


@pytest.mark.parametrize(
    ("first_unique_id", "second_unique_id"),
    [(None, None), ("", ""), (" \t ", " \t ")],
    ids=["none", "empty", "whitespace"],
)
def test_concurrent_dispatches_with_unstable_unique_ids_overlap(
    first_unique_id: object,
    second_unique_id: object,
) -> None:
    first_appliance, _, first_command = _dispatch_appliance()
    second_appliance, _, second_command = _dispatch_appliance()
    first_appliance.unique_id = first_unique_id
    second_appliance.unique_id = second_unique_id

    asyncio.run(
        _assert_appliance_dispatches_overlap(
            first_appliance,
            first_command,
            second_appliance,
            second_command,
        )
    )


def test_concurrent_dispatches_use_distinct_key_namespaces() -> None:
    first_appliance, _, first_command = _dispatch_appliance()
    second_appliance, _, second_command = _dispatch_appliance()
    second_appliance.unique_id = str(id(first_appliance))

    asyncio.run(
        _assert_appliance_dispatches_overlap(
            first_appliance,
            first_command,
            second_appliance,
            second_command,
        )
    )


def test_dispatch_serializes_matching_nonempty_stringified_unique_ids() -> None:
    async def scenario() -> None:
        first_appliance, _, first_command = _dispatch_appliance()
        second_appliance, _, second_command = _dispatch_appliance()
        first_appliance.unique_id = 17
        second_appliance.unique_id = "17"
        dispatcher = CommandDispatcher()
        started: asyncio.Queue[str] = asyncio.Queue()
        releases = [asyncio.Event(), asyncio.Event()]
        state = _ConcurrencyState()
        _block_command_sends(
            first_command,
            label="first",
            started=started,
            releases=[releases[0]],
            state=state,
        )
        _block_command_sends(
            second_command,
            label="second",
            started=started,
            releases=[releases[1]],
            state=state,
        )

        first = asyncio.create_task(
            dispatcher.dispatch(first_appliance, _category_patch())
        )
        assert await asyncio.wait_for(started.get(), timeout=1) == "first"
        second = asyncio.create_task(
            dispatcher.dispatch(second_appliance, _category_patch())
        )
        await asyncio.sleep(0)

        assert state.active == 1
        assert dispatcher.lock_count == 1

        releases[0].set()
        assert await asyncio.wait_for(started.get(), timeout=1) == "second"
        releases[1].set()

        assert await asyncio.gather(first, second) == [True, True]
        assert state.max_active == 1
        assert dispatcher.lock_count == 0

    asyncio.run(scenario())


def test_dispatch_cancelled_waiter_releases_lock_reference() -> None:
    async def scenario() -> None:
        appliance, _, command = _dispatch_appliance()
        appliance.unique_id = "cancel-waiter"
        dispatcher = CommandDispatcher()
        started: asyncio.Queue[str] = asyncio.Queue()
        releases = [asyncio.Event(), asyncio.Event()]
        state = _ConcurrencyState()
        _block_command_sends(
            command,
            label="waiter",
            started=started,
            releases=releases,
            state=state,
        )

        holder = asyncio.create_task(
            dispatcher.dispatch(appliance, _category_patch())
        )
        assert await asyncio.wait_for(started.get(), timeout=1) == "waiter"
        waiter = asyncio.create_task(
            dispatcher.dispatch(appliance, _category_patch())
        )
        await asyncio.sleep(0)
        waiter.cancel()

        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert dispatcher.lock_count == 1

        releases[0].set()
        assert await holder is True
        assert dispatcher.lock_count == 0

        later = asyncio.create_task(
            dispatcher.dispatch(appliance, _category_patch())
        )
        assert await asyncio.wait_for(started.get(), timeout=1) == "waiter"
        releases[1].set()

        assert await later is True
        assert dispatcher.lock_count == 0

    asyncio.run(scenario())


def test_dispatch_cancelled_lock_holder_releases_waiter_and_cleans_lock() -> None:
    async def scenario() -> None:
        appliance, _, command = _dispatch_appliance()
        appliance.unique_id = "cancel-holder"
        dispatcher = CommandDispatcher()
        started: asyncio.Queue[str] = asyncio.Queue()
        releases = [asyncio.Event(), asyncio.Event()]
        state = _ConcurrencyState()
        _block_command_sends(
            command,
            label="holder",
            started=started,
            releases=releases,
            state=state,
        )

        holder = asyncio.create_task(
            dispatcher.dispatch(appliance, _category_patch())
        )
        assert await asyncio.wait_for(started.get(), timeout=1) == "holder"
        waiter = asyncio.create_task(
            dispatcher.dispatch(appliance, _category_patch())
        )
        await asyncio.sleep(0)
        holder.cancel()

        with pytest.raises(asyncio.CancelledError):
            await holder
        assert await asyncio.wait_for(started.get(), timeout=1) == "holder"
        releases[1].set()

        assert await waiter is True
        assert dispatcher.lock_count == 0

    asyncio.run(scenario())


def test_adapter_delegates_patch_through_hass_executor_exactly_once() -> None:
    from custom_components.addhon.command_dispatch import async_dispatch_patch

    appliance = object()
    patch = CommandPatch("settings", {"mode": "2"}, action="set_mode")
    dispatch_calls: list[tuple[object, CommandPatch]] = []
    executor_calls: list[tuple[Callable[..., Any], tuple[object, ...]]] = []

    class Client:
        def dispatch_patch_sync(
            self,
            observed_appliance: object,
            observed_patch: CommandPatch,
        ) -> bool:
            dispatch_calls.append((observed_appliance, observed_patch))
            return True

    class Hass:
        async def async_add_executor_job(
            self,
            func: Callable[..., Any],
            *args: object,
        ) -> Any:
            executor_calls.append((func, args))
            return func(*args)

    client = Client()
    result = asyncio.run(async_dispatch_patch(Hass(), client, appliance, patch))

    assert result is None
    assert executor_calls == [(client.dispatch_patch_sync, (appliance, patch))]
    assert dispatch_calls == [(appliance, patch)]


@pytest.mark.parametrize(
    ("client", "appliance"),
    [(None, object()), (object(), None)],
)
def test_adapter_translates_unavailable_client_or_appliance(
    client: object | None,
    appliance: object | None,
) -> None:
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.addhon.command_dispatch import async_dispatch_patch

    class Hass:
        async def async_add_executor_job(self, *_args: object) -> None:
            pytest.fail("unavailable inputs must not reach the executor")

    patch = CommandPatch("settings", {}, action="noop")

    with pytest.raises(HomeAssistantError) as exc_info:
        asyncio.run(async_dispatch_patch(Hass(), client, appliance, patch))

    assert exc_info.value.translation_domain == "addhon"
    assert exc_info.value.translation_key == "appliance_or_client_unavailable"


def test_adapter_turns_a_refused_transaction_into_an_error() -> None:
    """`dispatch` has three outcomes: raise, True, or False after rolling back.

    The adapter used to discard that False, so the entity went on to refresh and
    report success on a write the cloud never accepted. Unreachable through the
    current stack, where a rejection arrives as ApiError, which is exactly why it
    must be handled rather than trusted.
    """
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.addhon.command_dispatch import async_dispatch_patch

    class Client:
        def dispatch_patch_sync(self, _appliance: object, _patch: CommandPatch) -> bool:
            return False

    class Hass:
        async def async_add_executor_job(
            self,
            func: Callable[..., Any],
            *args: object,
        ) -> Any:
            return func(*args)

    patch = CommandPatch("settings", {"mode": "2"}, action="set_mode")

    with pytest.raises(HomeAssistantError) as exc_info:
        asyncio.run(async_dispatch_patch(Hass(), Client(), object(), patch))

    assert exc_info.value.translation_domain == "addhon"
    assert exc_info.value.translation_key == "command_rejected"


def test_only_a_literal_true_is_an_acceptance() -> None:
    """The adapter applies the SAME rule as the dispatcher it fronts.

    `CommandDispatcher.dispatch` treats anything that is not the literal True as a
    refusal and rolls back. An adapter that were more permissive would accept a value
    the dispatcher had already refused. The check is by identity, not equality: 1 and
    True compare equal in Python, and a client answering with an int is not a client
    confirming a write.
    """
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.addhon.command_dispatch import async_dispatch_patch

    class Hass:
        async def async_add_executor_job(
            self,
            func: Callable[..., Any],
            *args: object,
        ) -> Any:
            return func(*args)

    patch = CommandPatch("settings", {}, action="noop")

    def _client(answer: object) -> object:
        class Client:
            def dispatch_patch_sync(
                self,
                _appliance: object,
                _patch: CommandPatch,
                _answer: object = answer,
            ) -> object:
                return _answer

        return Client()

    assert asyncio.run(async_dispatch_patch(Hass(), _client(True), object(), patch)) is None

    for answer in (False, None, 0, 1, "", "ok", [], {}):
        with pytest.raises(HomeAssistantError) as exc_info:
            asyncio.run(async_dispatch_patch(Hass(), _client(answer), object(), patch))
        assert exc_info.value.translation_key == "command_rejected", repr(answer)


def test_adapter_propagates_transaction_error_unchanged() -> None:
    from custom_components.addhon.command_dispatch import async_dispatch_patch

    transaction_error = RuntimeError("transaction failed")

    class Client:
        def dispatch_patch_sync(self, _appliance: object, _patch: CommandPatch) -> bool:
            raise transaction_error

    class Hass:
        async def async_add_executor_job(
            self,
            func: Callable[..., Any],
            *args: object,
        ) -> Any:
            return func(*args)

    patch = CommandPatch("settings", {}, action="noop")

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(async_dispatch_patch(Hass(), Client(), object(), patch))

    assert exc_info.value is transaction_error


def test_mixed_platform_legacy_classes_keep_the_legacy_sender() -> None:
    """Per-CLASS guard for the platform files that contain both senders.

    `switch.py`, `select.py` and `number.py` each hold an air purifier entity that
    dispatches AND legacy entities that intentionally send a whole command.
    Allow-listing such a file above silences the module-level dispatcher check for
    everything in it, and `_EXPECTED_LEGACY_CALL_EDGES` does not help either:
    `async_send_settings` is not one of its tracked callees. Without this test a
    legacy class could be quietly converted to the dispatcher.
    """
    import inspect

    from tests._golden import install_stubs

    install_stubs()

    from custom_components.addhon import switch

    from custom_components.addhon import number, select

    legacy_only = {
        switch.HonWashingMachinePauseSwitch: "run_command_sync",
        select.HonAcDirectionSelect: "async_send_settings",
        select.HonRefProgramSelect: "async_send_command",
        # Buffers onto startProgram instead of sending; the buffering IS its write
        # path, so losing it would be the same regression as losing a sender.
        number.HonProgramOptionNumber: "self._buffer(",
    }
    for entity_class, legacy_callee in legacy_only.items():
        source = inspect.getsource(entity_class)
        assert legacy_callee in source, entity_class.__name__
        for forbidden in _FORBIDDEN_DISPATCH_SYMBOLS:
            assert forbidden not in source, f"{entity_class.__name__}: {forbidden}"

    # Classes serving BOTH families at once: the air conditioner and the wine
    # cooler (and the fridge/oven setpoints) need the whole `settings` group on the
    # wire, the cooker hood must never send it, and one description field picks the
    # channel. Both senders therefore have to survive in the source, and losing
    # EITHER is a regression -- dropping the legacy one would silently make every
    # AC toggle sparse, dropping the dispatcher one would put the hood's clock back
    # in the payload. Which appliance gets which channel is a behavioural claim a
    # source scan cannot make: `test_ac_write_path.SettingsSwitchChannelTest` and
    # `test_hood_entities.HoodSparseWritePayloadTest` pin the two payloads.
    mixed = {
        switch.HonSettingsSwitch: ("async_send_settings", "async_dispatch_patch"),
        number.HonNumber: ("async_send_command", "async_dispatch_patch"),
    }
    for entity_class, callees in mixed.items():
        source = inspect.getsource(entity_class)
        for callee in callees:
            assert callee in source, f"{entity_class.__name__}: {callee}"


def _ap_dispatch_appliance() -> tuple[_DispatchAppliance, _DispatchCommand]:
    """An AP appliance whose settings command carries real purifier parameters,
    plus identity material a diagnostic event must never echo."""
    appliance = _DispatchAppliance()
    appliance.appliance_type = "AP"
    appliance.model_name = "SYNTHETIC-AP-MODEL"
    appliance.info = {
        "macAddress": "AA:BB:CC:DD:EE:FF",
        "serialNumber": "AP-PLAINTEXT-SERIAL",
    }
    appliance.attributes["parameters"]["lightStatus"] = HonAttribute("2")
    appliance.attributes["parameters"]["onOffStatus"] = HonAttribute("1")
    categories: dict[str, HonCommand] = {}
    command = _DispatchCommand(
        "settings",
        {
            "parameters": {
                "lightStatus": _enum("2", ["0", "1", "2"]),
                "onOffStatus": {
                    "typology": "fixed",
                    "category": "command",
                    "mandatory": 1,
                    "fixedValue": "1",
                },
            }
        },
        appliance,
        categories,
        "ap",
    )
    categories["ap"] = command
    appliance.commands["settings"] = command
    return appliance, command


def _ap_light_patch(appliance: _DispatchAppliance) -> CommandPatch:
    from custom_components.addhon.air_purifier import ap_patch, discover_capabilities

    capabilities = discover_capabilities(
        appliance, {"onOffStatus": "1", "machMode": "2", "lightStatus": "2"}
    )
    return ap_patch("set_light", capabilities, value="0")


def test_ap_diagnostic_events_correlate_action_keys_and_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The AP action name, the requested/mandatory/rule-added split, the latency
    and the shadow outcome must all be present, so a beta report says WHICH intent
    produced WHICH payload without any live device on hand."""
    import custom_components.addhon.command_dispatch as dispatch_module

    appliance, _command = _ap_dispatch_appliance()
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        dispatch_module,
        "emit_command_event",
        lambda event, fields: events.append((event, dict(fields))),
    )

    result = asyncio.run(
        CommandDispatcher().dispatch(appliance, _ap_light_patch(appliance))
    )

    assert result is True
    assert [event for event, _ in events] == [
        "command_intent",
        "command_payload",
        "command_result",
        "shadow_update",
        "contract_check",
    ]
    intent = events[0][1]
    assert intent["action"] == "ap_set_light"
    assert intent["appliance_type"] == "AP"
    assert intent["command"] == "settings"
    assert intent["requested_values"] == {"lightStatus": "0"}
    payload = events[1][1]
    assert payload["requested_keys"] == ["lightStatus"]
    assert payload["mandatory_keys"] == ["onOffStatus"]
    assert payload["rule_added_keys"] == []
    assert payload["payload"] == {"lightStatus": "0", "onOffStatus": "1"}
    outcome = events[2][1]
    assert outcome["result"] is True
    assert isinstance(outcome["latency_ms"], float)
    shadow = events[3][1]
    # Both payload keys land in the shadow: the sparse intent plus the mandatory
    # key the dispatcher added. What matters is that NOTHING else moved.
    assert shadow["expected_keys"] == ["lightStatus", "onOffStatus"]
    assert shadow["unexpected_keys"] == []
    assert shadow["missing_keys"] == []


def test_ap_diagnostic_records_carry_no_appliance_identity() -> None:
    """Through the REAL emitter, not a capture: the encoded records must not carry
    the appliance id, model, serial or MAC."""
    import logging

    logger = logging.getLogger("custom_components.addhon.command_diagnostics")
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    handler = _Capture()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        appliance, _command = _ap_dispatch_appliance()
        asyncio.run(
            CommandDispatcher().dispatch(appliance, _ap_light_patch(appliance))
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert records, "no diagnostic record was emitted"
    joined = "\n".join(records)
    assert "ap_set_light" in joined
    for identity in (
        "AA:BB:CC:DD:EE:FF",
        "AP-PLAINTEXT-SERIAL",
        "SYNTHETIC-AP-MODEL",
    ):
        assert identity not in joined, identity
