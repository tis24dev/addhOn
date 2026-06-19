"""Shared sending of the air conditioner's `settings` command.

Both the climate entity (mode/temp/fan/swing) and the AC switches modify the SAME
the `settings` command, which on send transmits ALL of its parameters. So every
send must apply the same sanitization of
`windDirectionVertical`/`windDirectionHorizontal`: the device may report them as 0
(a value not allowed by the enumValues) and the API would reject the command.
Centralizing this here avoids divergences between climate.py and switch.py.
"""
from __future__ import annotations

import logging

from .hon_commands import async_send_command

_LOGGER = logging.getLogger(__name__)

# Wind-direction parameters that may be 0 (device off): to be sanitized.
AC_WIND_DIR_PARAMS = ("windDirectionVertical", "windDirectionHorizontal")
AC_SWING_V_PARAM = "windDirectionVertical"
AC_SWING_V_ON = "8"  # 8 = vertical oscillation


def settings_param(appliance, name):
    """Return the `name` parameter of the `settings` command, or None if absent."""
    commands = getattr(appliance, "commands", None)
    commands = commands if isinstance(commands, dict) else {}
    settings = commands.get("settings")
    params = getattr(settings, "parameters", None) if settings is not None else None
    if isinstance(params, dict):
        return params.get(name)
    return None


def param_allowed_values(param) -> list[str]:
    """Allowed values (as strings) of an enum parameter, or [] if not an enum."""
    values = getattr(param, "values", None)
    if not isinstance(values, list):
        return []
    return [str(v) for v in values]


def fixed_vertical_value(allowed: list[str]) -> str:
    """FIXED (non-swing) vertical position among the allowed ones; never 0."""
    fixed = [v for v in allowed if v != AC_SWING_V_ON]
    if "2" in fixed:
        return "2"
    return fixed[0] if fixed else AC_SWING_V_ON


def sanitize_wind_direction(command_params: dict) -> None:
    """Reset windDirectionVertical/Horizontal to an allowed value if the current
    one is not (e.g. 0 when off). Does not touch already-valid parameters."""
    for key in AC_WIND_DIR_PARAMS:
        param = command_params.get(key)
        if param is None:
            continue
        allowed = param_allowed_values(param)
        if not allowed:
            continue
        current = str(getattr(param, "value", ""))
        if current in allowed:
            continue
        safe = (
            fixed_vertical_value(allowed)
            if key == AC_SWING_V_PARAM
            else next((v for v in allowed if v != "0"), allowed[0])
        )
        try:
            param.value = safe
            _LOGGER.debug(
                "AC settings: sanitized %s from %r to %s (allowed=%s)", key, current, safe, allowed
            )
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.warning(
                "AC settings: unable to sanitize %s (value %r): %s", key, current, err
            )


async def async_send_settings(hass, client, appliance, params: dict) -> None:
    """Apply `params` to the AC's `settings` command and send it.

    Sanitizes windDirection* before sending (never 0): the requested values win
    anyway. Delegates to the generic sender (hon_commands.async_send_command),
    which handles command/parameter lookup, rollback and execution on the client's
    dedicated loop; the AC sanitization is plugged in as a pre_send hook.
    """
    await async_send_command(
        hass, client, appliance, "settings", params, pre_send=sanitize_wind_direction
    )
