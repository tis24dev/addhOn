# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .param_rollback import restore_params, snapshot_params

if TYPE_CHECKING:
    from .client.engine.commands import HonCommand
    from .client.interfaces import Appliance

PrepareCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class CommandPatch:
    command_name: str
    values: Mapping[str, str | float]
    action: str
    prepare: PrepareCallback | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, slots=True)
class PreparedCommand:
    command: HonCommand
    payload: dict[str, str | float]
    requested_keys: frozenset[str]
    mandatory_keys: frozenset[str]
    changed_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class CommandDispatcher:
    _SELECTOR_KEYS = frozenset({"category", "program"})

    @staticmethod
    def _command_tree(commands: Mapping[str, HonCommand]) -> list[HonCommand]:
        pending = list(commands.values())
        result: list[HonCommand] = []
        visited: set[int] = set()
        while pending:
            command = pending.pop()
            if id(command) in visited:
                continue
            visited.add(id(command))
            result.append(command)
            categories = getattr(command, "categories", {})
            if isinstance(categories, Mapping):
                pending.extend(categories.values())
        return result

    @staticmethod
    def _active_command(command: HonCommand, command_name: str) -> HonCommand:
        commands = getattr(command.appliance, "commands", {})
        if isinstance(commands, Mapping):
            return commands.get(command_name, command)
        return command

    @classmethod
    def _selector_key(
        cls,
        command: HonCommand,
        patch: CommandPatch,
    ) -> str | None:
        return next(
            (
                key
                for key in patch.values
                if key in cls._SELECTOR_KEYS and key in command.parameters
            ),
            None,
        )

    def _prepare(
        self,
        command: HonCommand,
        patch: CommandPatch,
    ) -> PreparedCommand:
        parameters = command.parameters
        selector_key = self._selector_key(command, patch)
        target_command = command
        if selector_key is not None:
            selector_value = str(patch.values[selector_key])
            categories = getattr(command, "categories", {})
            if isinstance(categories, Mapping):
                target_command = categories.get(selector_value, command)
        target_parameters = target_command.parameters
        unknown_keys = [key for key in patch.values if key not in target_parameters]
        if unknown_keys:
            unknown = ", ".join(repr(key) for key in unknown_keys)
            raise ValueError(
                f"Unknown parameters for {patch.command_name!r}: {unknown}"
            )

        # Retain the complete pre-mutation state boundary needed by the transaction
        # layer; transmitted-value comparison deliberately uses intern_value.
        parameter_snapshot = snapshot_params(target_parameters)
        before_values = {
            key: target_parameters[key].intern_value for key in parameter_snapshot
        }

        if patch.prepare is not None:
            patch.prepare(parameters)

        active_command = self._active_command(command, patch.command_name)
        if selector_key is not None:
            active_command.parameters[selector_key].value = patch.values[selector_key]
            active_command = self._active_command(active_command, patch.command_name)

        for key, value in patch.values.items():
            if key != selector_key:
                active_command.parameters[key].value = value

        active_command = self._active_command(active_command, patch.command_name)
        active_parameters = active_command.parameters

        requested_order = list(patch.values)
        requested = frozenset(requested_order)
        mandatory_order = [
            key
            for key, parameter in active_parameters.items()
            if parameter.mandatory and key not in requested
        ]
        mandatory = frozenset(mandatory_order)
        changed_order = [
            key
            for key, parameter in active_parameters.items()
            if key not in requested
            and key not in mandatory
            and key in before_values
            and parameter.intern_value != before_values[key]
        ]
        changed = frozenset(changed_order)

        payload = {
            key: active_parameters[key].intern_value
            for key in requested_order + mandatory_order + changed_order
        }
        return PreparedCommand(
            command=active_command,
            payload=payload,
            requested_keys=requested,
            mandatory_keys=mandatory,
            changed_keys=changed,
        )

    async def dispatch(
        self,
        appliance: Appliance,
        patch: CommandPatch,
    ) -> bool:
        commands = appliance.commands
        command_before = commands.get(patch.command_name)
        if command_before is None:
            raise KeyError(patch.command_name)

        parameter_snapshots = [
            (command, snapshot_params(command.parameters))
            for command in self._command_tree(commands)
        ]
        attributes = getattr(appliance, "attributes", {})
        shadow = (
            attributes.get("parameters", {})
            if isinstance(attributes, Mapping)
            else {}
        )
        shadow_before = snapshot_params(shadow)

        def rollback() -> None:
            for command, snapshot in parameter_snapshots:
                restore_params(command.parameters, snapshot)
            restore_params(shadow, shadow_before)
            commands[patch.command_name] = command_before  # type: ignore[index]

        try:
            prepared = self._prepare(command_before, patch)
            result = await prepared.command.send_exact(prepared.payload)
            if result is not True:
                rollback()
                return False
            appliance.sync_payload_to_params(prepared.payload)
            return True
        except BaseException:
            rollback()
            raise
