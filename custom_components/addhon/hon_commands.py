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

from collections.abc import Callable, Sequence
import logging

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# the hOn commands from which the "set" controls (number/switch/select-mode) read
# and write the free parameters. The client names the command after the device's
# top-level key: "settings" is the AC's and the real fridge's one (the active
# category exposes setParameters); "setParameters" as a fallback for other models.
SETTINGS_COMMANDS: tuple[str, ...] = ("settings", "setParameters")


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
            # (value AND values/min/max). On a pre_send or send() failure we restore by
            # copying __dict__ DIRECTLY, without going through the setters: this way we
            # do NOT re-fire the rules and we also restore values/min/max. A rollback
            # via setter would leave the .values restricted and would raise on
            # revalidation -> corrupted state that would contaminate later sends. The
            # parameters REPLACE the lists (never mutated in-place), so a shallow copy
            # of __dict__ is enough.
            snapshots: dict = {
                key: dict(attr.__dict__)
                for key, attr in command_params.items()
                if hasattr(attr, "__dict__")
            }
            try:
                if pre_send is not None:
                    pre_send(command_params)
                for key, value in params.items():
                    command_params[key].value = value
                    _LOGGER.debug("Command %s: '%s' = %s", command_name, key, value)
                await command.send()
            except Exception:
                for key, snap in snapshots.items():
                    attr = command_params.get(key)
                    if attr is None or not hasattr(attr, "__dict__"):
                        continue
                    attr.__dict__.clear()
                    attr.__dict__.update(snap)
                raise
            _LOGGER.debug(
                "Command %s: send completed (params=%s)", command_name, list(params)
            )

        client.run_command_sync(_inner())

    await hass.async_add_executor_job(_do_send)
