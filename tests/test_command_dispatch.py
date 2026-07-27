# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from typing import Any, Callable

import pytest

from tests._golden import install_stubs

install_stubs()

from custom_components.addhon.client.engine.attributes import HonAttribute
from custom_components.addhon.client.engine.commands import HonCommand
from custom_components.addhon.client.engine.exceptions import ApiError
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


def _mutate_selected_category_and_shadow(
    appliance: _DispatchAppliance,
    command: _DispatchCommand,
) -> None:
    command.parameters["coupled"].values = ["corrupt"]
    command.parameters["coupled"].value = "corrupt"
    appliance.attributes["parameters"]["mode"].update("corrupt", shield=True)


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


def test_dispatch_transaction_rolls_back_cloud_false() -> None:
    appliance, _, second = _dispatch_appliance()
    before = _snapshot_transaction(appliance)
    second.send_result = False
    second.before_send = lambda: _mutate_selected_category_and_shadow(
        appliance, second
    )

    result = asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    assert result is False
    assert _snapshot_transaction(appliance) == before
    assert appliance.synced_payloads == []


def test_dispatch_transaction_rolls_back_cloud_api_error() -> None:
    appliance, _, second = _dispatch_appliance()
    before = _snapshot_transaction(appliance)
    second.send_error = ApiError("cloud rejected")
    second.before_send = lambda: _mutate_selected_category_and_shadow(
        appliance, second
    )

    with pytest.raises(ApiError, match="cloud rejected"):
        asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    assert _snapshot_transaction(appliance) == before
    assert appliance.synced_payloads == []


def test_dispatch_transaction_rolls_back_transport_exception() -> None:
    appliance, _, second = _dispatch_appliance()
    before = _snapshot_transaction(appliance)
    second.send_error = RuntimeError("send boom")
    second.before_send = lambda: _mutate_selected_category_and_shadow(
        appliance, second
    )

    with pytest.raises(RuntimeError, match="send boom"):
        asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    assert _snapshot_transaction(appliance) == before
    assert appliance.synced_payloads == []


def test_dispatch_cancellation_rolls_back_and_propagates() -> None:
    appliance, _, second = _dispatch_appliance()
    before = _snapshot_transaction(appliance)
    second.send_error = asyncio.CancelledError()
    second.before_send = lambda: _mutate_selected_category_and_shadow(
        appliance, second
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(CommandDispatcher().dispatch(appliance, _category_patch()))

    assert _snapshot_transaction(appliance) == before
    assert appliance.synced_payloads == []


def test_dispatch_serializes_same_appliance_in_order_and_cleans_lock() -> None:
    async def scenario() -> None:
        appliance, _, command = _dispatch_appliance()
        appliance.unique_id = 17
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


def test_concurrent_dispatches_for_different_appliances_overlap() -> None:
    async def scenario() -> None:
        first_appliance, _, first_command = _dispatch_appliance()
        second_appliance, _, second_command = _dispatch_appliance()
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

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("first_unique_id", "second_unique_id"),
    [(17, "17"), (None, None)],
    ids=["stringified", "present-falsy"],
)
def test_dispatch_serializes_matching_stringified_unique_ids(
    first_unique_id: object,
    second_unique_id: object,
) -> None:
    async def scenario() -> None:
        first_appliance, _, first_command = _dispatch_appliance()
        second_appliance, _, second_command = _dispatch_appliance()
        first_appliance.unique_id = first_unique_id
        second_appliance.unique_id = second_unique_id
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
