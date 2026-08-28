# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared helpers to send hOn commands to the controls (Tier 3).

Generalizes the pattern already used by button.py (sending a command while
applying parameter overrides) and by ac_command.async_send_settings (set on the
write command), making it neutral with respect to the command name. The Tier 3
controls (number, switch/select/button for fridge/oven/...) reuse it without
duplicating lookup, rollback and execution on the client's dedicated loop.

Gating principle (see memory/repo): every control is CAPABILITY-GATED, i.e. it is
created only if the device ACTUALLY exposes the command + parameter (the client runtime
schema), with the candidate superset seeded from the app mapping. This way it is
validated where we have the real dump, broad for the other models, and safe
everywhere (a missing parameter does not generate an entity).
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
import logging

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .param_rollback import restore_params, snapshot_params

_LOGGER = logging.getLogger(__name__)

# the hOn commands from which the "set" controls (number/switch/select-mode) read
# and write the free parameters. The client names the command after the device's
# top-level key: "settings" is the AC's and the real fridge's one (the active
# category exposes setParameters); "setParameters" as a fallback for other models.
SETTINGS_COMMANDS: tuple[str, ...] = ("settings", "setParameters")

# `HonCommand.categories` returns `{"_": self}` for a command that has no categories at
# all (commands.py), so a caller walking categories sees this placeholder alongside real
# program codes. It is not a program name and must never reach a user-facing state.
SYNTHETIC_CATEGORY = "_"


def get_commands(appliance) -> dict:
    """Command dictionary of the device, or {} if absent/invalid."""
    commands = getattr(appliance, "commands", None)
    return commands if isinstance(commands, dict) else {}


def get_command(appliance, name: str):
    """Command `name`, or None."""
    return get_commands(appliance).get(name)


def command_param(appliance, command_name: str, param_name: str):
    """Parameter `param_name` of command `command_name`, or None if absent."""
    command = get_command(appliance, command_name)
    params = getattr(command, "parameters", None) if command is not None else None
    if isinstance(params, dict):
        return params.get(param_name)
    return None


def find_settings_param(
    appliance, param_name: str, command_names: Sequence[str] = SETTINGS_COMMANDS
):
    """Search for `param_name` among the `command_names` commands (in order).

    Returns (command_name, param) of the first match, or None. It is the
    capability-gate of the controls that write to a settings/setParameters command.
    """
    for name in command_names:
        param = command_param(appliance, name, param_name)
        if param is not None:
            return name, param
    return None


def program_code_for_fixed_value(
    appliance,
    param_name: str,
    value,
    *,
    codes: Collection[str] | None = None,
) -> str | None:
    """Program code whose `param_name` is PINNED to `value`, or None.

    Some device settings are not free values but a program's signature: the fridge My
    Zone drawer has no writable mode of its own, and each of its programs instead pins
    `tempSelZ3` to one fixed number. Reading that number back out of the shadow says
    which program is running -- so this is a lookup, not a guess.

    It is what the official app does, and in the same order: `getMyZoneMappedMode` calls
    `getModeNameFromCommands` FIRST and only falls back to a static value table for
    numbers no program explains (apk/analysis/issue93-ref-unmapped-values.md section 4.4).

    Deliberately restricted to FIXED parameters. A range or an enum member merely ALLOWS
    a value; only `typology: "fixed"` means "this program is the reason the value is what
    it is", so a free setpoint that happens to sit on a program's number is never
    mistaken for that program running. `HonParameterFixed` is duck-typed through
    `.typology` rather than imported, like every other reader in this module.

    Returns the CATEGORY key, which the loader has already reduced to the same slug the
    program select offers (`PROGRAMS.REF.ZERO_FRESH` -> `zero_fresh`), so callers can
    compare it against the select's options without a second translation.

    `codes` NARROWS the walk to a caller-supplied set of program codes, and it is a
    correction rather than an optimisation. The unrestricted walk answers with the FIRST
    category that pins the number, and "a program pins this value" is not the same claim
    as "this program is the reason the value is what it is" once several programs pin the
    same parameter. On damigioanna's HDPW5620CNPK (apk/dump/ref_10136/commands.json)
    five DOWNLOAD presets pin `tempSelZ3` to 2, 2, 5, 5 and 5, so an unrestricted lookup
    calls the My Zone drawer `iot_extra_cold` at 2 and `iot_daily_use` at 5 -- a
    whole-appliance preset reported as the state of one drawer. A caller that knows which
    programs OWN the parameter passes them (`ref_programs.my_zone_code_for_value` passes
    the drawer's); a caller with a parameter only one program can pin passes nothing and
    gets exactly today's behaviour.
    """
    if value is None:
        return None
    command = get_command(appliance, "startProgram")
    categories = getattr(command, "categories", None) if command is not None else None
    if not isinstance(categories, dict):
        return None
    allowed = None if codes is None else {str(code) for code in codes}
    wanted = str(value).strip()
    for code, category in categories.items():
        if allowed is not None and str(code) not in allowed:
            continue
        # Not a program: a category-less command reports itself under `SYNTHETIC_CATEGORY`,
        # and its own parameters would otherwise be answered as if a program had pinned
        # them. Returning that placeholder would put a bare "_" in front of the user.
        if str(code) == SYNTHETIC_CATEGORY:
            continue
        params = getattr(category, "parameters", None)
        param = params.get(param_name) if isinstance(params, dict) else None
        if param is None or str(getattr(param, "typology", "")) != "fixed":
            continue
        if str(getattr(param, "value", "")).strip() == wanted:
            return str(code)
    return None


def param_values(param) -> list[str]:
    """Allowed values (strings) of an enum parameter, or [] if not enumerated."""
    values = getattr(param, "values", None)
    if isinstance(values, (list, tuple)):
        return [str(v) for v in values]
    return []


def param_range(param) -> tuple[float, float, float] | None:
    """(min, max, step) of a range parameter, or None if it is not a range.

    Duck-typing on min/max/step (HonParameterRange exposes them). step returns 1.0
    if the parameter reports it as 0 (no declared increment)."""
    if not all(hasattr(param, attr) for attr in ("min", "max", "step")):
        return None
    try:
        lo = float(param.min)
        hi = float(param.max)
        step = float(param.step) or 1.0
    except (TypeError, ValueError):
        return None
    if hi < lo:
        return None
    if step <= 0:  # non-positive increment: inconsistent range for a numeric control
        return None
    return lo, hi, step


async def async_send_command(
    hass,
    client,
    appliance,
    command_name: str,
    params: dict,
    *,
    pre_send: Callable[[dict], None] | None = None,
) -> None:
    """Apply `params` (name->value) to command `command_name` and send it on
    the client's dedicated loop, with rollback if an assignment fails.

    `pre_send(command_params)`: optional hook run BEFORE applying the requested
    parameters (the AC uses it to sanitize windDirection*). The requested values
    win anyway over whatever pre_send has set.
    """
    if not appliance or not client:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="appliance_or_client_unavailable",
        )

    def _do_send():
        async def _inner():
            command = get_command(appliance, command_name)
            if command is None:
                raise RuntimeError(
                    f"Command '{command_name}' not found on the device"
                )
            command_params = getattr(command, "parameters", {})
            if not isinstance(command_params, dict):
                command_params = {}
            missing = [key for key in params if key not in command_params]
            if missing:
                raise RuntimeError(
                    f"Parameter(s) not found in command {command_name}: "
                    + ", ".join(missing)
                )
            # Snapshot of the complete internal state of EVERY parameter BEFORE pre_send.
            # Assigning a trigger parameter fires the rules, which mutate the siblings
            # (value AND values/min/max); on a pre_send or send() failure we restore the
            # full pre-mutation state via the shared param_rollback helper (copies
            # __dict__ directly, so rules are not re-fired and values/min/max come back).
            snapshots = snapshot_params(command_params)
            try:
                if pre_send is not None:
                    pre_send(command_params)
                for key, value in params.items():
                    command_params[key].value = value
                    _LOGGER.debug("Command %s: '%s' = %s", command_name, key, value)
                await command.send()
            except Exception:
                restore_params(command_params, snapshots)
                raise
            _LOGGER.debug(
                "Command %s: send completed (params=%s)", command_name, list(params)
            )

        client.run_command_sync(_inner())

    await hass.async_add_executor_job(_do_send)
