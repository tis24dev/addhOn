# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError

from .command_diagnostics import emit_command_event, record_expected_update
from .const import DOMAIN
from .client.engine.exceptions import ApiError
from .param_rollback import restore_owned_params, snapshot_params

_LOGGER = logging.getLogger(__name__)


if TYPE_CHECKING:
    from .client.engine.commands import HonCommand
    from .client.interfaces import Appliance

PrepareCallback = Callable[[dict[str, Any]], None]
_MISSING = object()


def _emit_safely(event: str, fields: Mapping[str, object]) -> None:
    try:
        emit_command_event(event, fields)
    except Exception:
        # Diagnostics must never affect the command; that is the whole point of these
        # wrappers. A trace still has to be left: a systematically broken diagnostic
        # was previously indistinguishable from a healthy one that had nothing to say.
        _LOGGER.debug("Command diagnostics step failed", exc_info=True)


def _record_expected_safely(
    appliance: Appliance,
    action: str,
    payload: Mapping[str, object],
) -> None:
    try:
        record_expected_update(appliance, action, payload)
    except Exception:
        # Diagnostics must never affect the command; that is the whole point of these
        # wrappers. A trace still has to be left: a systematically broken diagnostic
        # was previously indistinguishable from a healthy one that had nothing to say.
        _LOGGER.debug("Command diagnostics step failed", exc_info=True)


def _appliance_type(appliance: Appliance) -> str:
    try:
        value = getattr(appliance, "appliance_type", "")
        return str(value) if value else type(appliance).__name__
    except Exception:
        return type(appliance).__name__


def _diagnostic_started() -> float | None:
    try:
        return time.monotonic()
    except Exception:
        return None


def _diagnostic_common_fields(
    appliance: Appliance,
    patch: CommandPatch,
) -> dict[str, object]:
    try:
        return {
            "action": patch.action,
            "appliance_type": _appliance_type(appliance),
            "command": patch.command_name,
        }
    except Exception:
        return {"appliance_type": type(appliance).__name__}


def _emit_intent_safely(
    common_fields: Mapping[str, object],
    patch: CommandPatch,
) -> None:
    try:
        _emit_safely(
            "command_intent",
            {
                **common_fields,
                "requested_values": dict(patch.values),
            },
        )
    except Exception:
        # Diagnostics must never affect the command; that is the whole point of these
        # wrappers. A trace still has to be left: a systematically broken diagnostic
        # was previously indistinguishable from a healthy one that had nothing to say.
        _LOGGER.debug("Command diagnostics step failed", exc_info=True)


def _emit_payload_safely(
    common_fields: Mapping[str, object],
    prepared: PreparedCommand,
) -> None:
    try:
        _emit_safely(
            "command_payload",
            {
                **common_fields,
                "mandatory_keys": [
                    key
                    for key in prepared.payload
                    if key in prepared.mandatory_keys
                ],
                "payload": prepared.payload,
                "requested_keys": [
                    key
                    for key in prepared.payload
                    if key in prepared.requested_keys
                ],
                "rule_added_keys": [
                    key
                    for key in prepared.payload
                    if key in prepared.changed_keys
                ],
            },
        )
    except Exception:
        # Diagnostics must never affect the command; that is the whole point of these
        # wrappers. A trace still has to be left: a systematically broken diagnostic
        # was previously indistinguishable from a healthy one that had nothing to say.
        _LOGGER.debug("Command diagnostics step failed", exc_info=True)


def _emit_result_safely(
    common_fields: Mapping[str, object],
    started: float | None,
    *,
    result: bool,
    outcome: str,
    error: BaseException | None = None,
) -> None:
    try:
        latency_ms = (
            (time.monotonic() - started) * 1000
            if started is not None
            else 0.0
        )
        fields: dict[str, object] = {
            **common_fields,
            "latency_ms": latency_ms,
            "outcome": outcome,
            "result": result,
        }
        if error is not None:
            fields["error_type"] = type(error).__name__
        _emit_safely("command_result", fields)
    except Exception:
        # Diagnostics must never affect the command; that is the whole point of these
        # wrappers. A trace still has to be left: a systematically broken diagnostic
        # was previously indistinguishable from a healthy one that had nothing to say.
        _LOGGER.debug("Command diagnostics step failed", exc_info=True)


def _emit_shadow_safely(
    common_fields: Mapping[str, object],
    shadow_before: Mapping[object, object],
    shadow: Mapping[object, object],
    payload: Mapping[str, object],
) -> None:
    try:
        changed_shadow_keys = sorted(
            key
            for key, before in shadow_before.items()
            if isinstance(key, str)
            and key in shadow
            and hasattr(shadow[key], "__dict__")
            and dict(shadow[key].__dict__) != before
        )
        expected_keys = sorted(
            key for key in changed_shadow_keys if key in payload
        )
        unexpected_keys = sorted(
            key for key in changed_shadow_keys if key not in payload
        )
        missing_keys = sorted(
            key for key in payload if key not in expected_keys
        )
        fields: dict[str, object] = {
            **common_fields,
            "expected_keys": expected_keys,
            "missing_keys": missing_keys,
            "unexpected_keys": unexpected_keys,
        }
        _emit_safely("shadow_update", fields)
        _emit_safely("contract_check", fields)
    except Exception:
        # Diagnostics must never affect the command; that is the whole point of these
        # wrappers. A trace still has to be left: a systematically broken diagnostic
        # was previously indistinguishable from a healthy one that had nothing to say.
        _LOGGER.debug("Command diagnostics step failed", exc_info=True)


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


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


_LockKey = tuple[str, str | int]


@dataclass(frozen=True, slots=True)
class CommandDispatcher:
    _SELECTOR_KEYS = frozenset({"category", "program"})
    _locks: dict[_LockKey, _LockEntry] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def lock_count(self) -> int:
        return len(self._locks)

    @staticmethod
    def _appliance_key(appliance: Appliance) -> _LockKey:
        unique_id = getattr(appliance, "unique_id", _MISSING)
        if unique_id is not _MISSING and unique_id is not None:
            stable_id = str(unique_id)
            if stable_id.strip():
                return ("stable", stable_id)
        return ("object", id(appliance))

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
        key = self._appliance_key(appliance)
        entry = self._locks.get(key)
        if entry is None:
            entry = _LockEntry(asyncio.Lock())
            self._locks[key] = entry
        entry.users += 1

        try:
            async with entry.lock:
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
                # Diagnostics-only baseline: the exact-send path never mutates the
                # shadow before the commit point (HonCommand._send_parameters uses
                # sync_shadow=False for send_exact), so dispatch() does not own it
                # and rollback must never restore it. The MQTT push callback runs
                # on its own thread outside this lock and can update the shadow
                # while send_exact is awaited below; restoring shadow_before here
                # would silently discard that authoritative concurrent update.
                shadow_before = snapshot_params(shadow)

                def rollback(
                    own_write_snapshots: list[tuple[HonCommand, dict]],
                ) -> None:
                    own_write_by_command = dict(own_write_snapshots)
                    for command, baseline in parameter_snapshots:
                        own_write = own_write_by_command.get(command, {})
                        restore_owned_params(command.parameters, baseline, own_write)
                    commands[patch.command_name] = command_before  # type: ignore[index]

                started = _diagnostic_started()
                common_fields = _diagnostic_common_fields(appliance, patch)
                _emit_intent_safely(common_fields, patch)
                own_write_snapshots = parameter_snapshots
                try:
                    try:
                        prepared = self._prepare(command_before, patch)
                    finally:
                        # Taken right after _prepare()'s own mutations (success or
                        # partial failure), before send_exact is awaited: the
                        # baseline rollback compares against to tell "the
                        # transaction's own write" apart from a concurrent MQTT
                        # mutation landing during the await.
                        own_write_snapshots = [
                            (command, snapshot_params(command.parameters))
                            for command, _ in parameter_snapshots
                        ]
                    prepared = PreparedCommand(
                        command=prepared.command,
                        payload=prepared.command.canonical_exact_payload(
                            prepared.payload
                        ),
                        requested_keys=prepared.requested_keys,
                        mandatory_keys=prepared.mandatory_keys,
                        changed_keys=prepared.changed_keys,
                    )
                    _emit_payload_safely(common_fields, prepared)
                    result = await prepared.command.send_exact(prepared.payload)
                except BaseException as error:
                    rollback(own_write_snapshots)
                    _emit_result_safely(
                        common_fields,
                        started,
                        result=False,
                        # A refusal is a refusal however it arrives. ApiError has one
                        # raise site, HonCommand._send_parameters on a falsy api
                        # result, and it means exactly this. Recording it as a generic
                        # error made "cloud_rejected" a label no real refusal ever
                        # carried, indistinguishable in diagnostics from a transport
                        # or preparation failure.
                        outcome=(
                            "cloud_rejected"
                            if isinstance(error, ApiError)
                            else "error"
                        ),
                        error=error,
                    )
                    raise
                if result is not True:
                    rollback(own_write_snapshots)
                    _emit_result_safely(
                        common_fields,
                        started,
                        result=False,
                        outcome="cloud_rejected",
                    )
                    return False

                # A literal True is the remote commit point. Local reconciliation
                # is best-effort and must not make an accepted command retryable.
                sync_error: Exception | None = None
                try:
                    appliance.sync_payload_to_params(prepared.payload)
                except Exception as error:
                    sync_error = error
                _record_expected_safely(
                    appliance,
                    patch.action,
                    prepared.payload,
                )
                _emit_result_safely(
                    common_fields,
                    started,
                    result=True,
                    outcome=(
                        "committed_sync_error"
                        if sync_error is not None
                        else "success"
                    ),
                    error=sync_error,
                )
                _emit_shadow_safely(
                    common_fields,
                    shadow_before,
                    shadow,
                    prepared.payload,
                )
                return True
        finally:
            entry.users -= 1
            if entry.users == 0 and self._locks.get(key) is entry:
                del self._locks[key]


async def async_dispatch_patch(
    hass: Any,
    client: Any,
    appliance: Appliance,
    patch: CommandPatch,
) -> None:
    if not appliance or not client:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="appliance_or_client_unavailable",
        )

    try:
        accepted = await hass.async_add_executor_job(
            client.dispatch_patch_sync,
            appliance,
            patch,
        )
    except ApiError as error:
        # The REACHABLE refusal. `ApiError` has a single raise site,
        # HonCommand._send_parameters on a falsy api result, so it carries exactly the
        # meaning the branch below was written for. Without this it reached the entity
        # as a generic failure carrying the untranslated literal "Can't send command",
        # which an Italian user read as "Comando non riuscito: Can't send command".
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_rejected",
        ) from error
    # `dispatch` distinguishes three outcomes: it raises, it returns True, or it
    # returns False after rolling the transaction back. Discarding that False left the
    # entity refreshing and reporting success on a write the cloud never accepted, and
    # the caller had no way to observe the difference.
    #
    # Unreachable through the current stack: the api returns a literal True and
    # HonCommand._send_parameters raises ApiError on anything falsy, so a rejection
    # arrives here as an exception. That is precisely why it has to be handled rather
    # than trusted; the day a send path starts returning False the failure would
    # otherwise be silent.
    if accepted is not True:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="command_rejected",
        )
