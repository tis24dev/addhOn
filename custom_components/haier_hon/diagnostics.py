"""Diagnostics support for Haier hOn (Extended)."""
from __future__ import annotations

import logging
from collections.abc import Mapping

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _redact_title(title: str | None) -> str | None:
    if not title:
        return None
    if "@" not in title:
        return title
    prefix, domain_and_suffix = title.rsplit("@", 1)
    open_paren = prefix.rfind("(")
    safe_prefix = prefix[: open_paren + 1] if open_paren >= 0 else ""
    return f"{safe_prefix}***@{domain_and_suffix}"


def _redact_email(email: str | None) -> str | None:
    if not email:
        return None
    if "@" in email:
        _, domain = email.split("@", 1)
        return f"***@{domain}"
    return "***"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """Return diagnostics for a config entry."""
    domain_data = hass.data.get(DOMAIN, {})
    entry_data = domain_data.get(entry.entry_id, {})
    coordinator = entry_data.get("coordinator")
    _LOGGER.debug(
        "Diagnostics debug: richiesta diagnostics entry=%s title=%s coordinator_present=%s",
        entry.entry_id,
        _redact_title(getattr(entry, "title", None)),
        coordinator is not None,
    )

    appliances: list[dict] = []
    coord_data = getattr(coordinator, "data", None)
    if isinstance(coord_data, dict):
        for appliance_id, data in coord_data.items():
            appliance = data.get("appliance")
            commands_info: dict[str, list[str]] = {}

            commands = getattr(appliance, "commands", None)
            if isinstance(commands, Mapping):
                for cmd_name, cmd in commands.items():
                    params = getattr(cmd, "parameters", None)
                    if isinstance(params, Mapping):
                        commands_info[cmd_name] = sorted([str(k) for k in params.keys()])
                    else:
                        commands_info[cmd_name] = []

            attributes = data.get("attributes") if isinstance(data, dict) else None
            settings = data.get("settings") if isinstance(data, dict) else None
            _LOGGER.debug(
                "Diagnostics debug: appliance id=%s name=%s type=%s attributes=%d settings=%d commands=%s",
                appliance_id,
                data.get("name"),
                data.get("type"),
                len(attributes) if isinstance(attributes, dict) else 0,
                len(settings) if isinstance(settings, dict) else 0,
                commands_info,
            )

            appliances.append(
                {
                    "id": "***",
                    "name": data.get("name"),
                    "type": data.get("type"),
                    "model": data.get("model"),
                    "serial": "***",
                    "attribute_keys": sorted(list(attributes.keys()))
                    if isinstance(attributes, dict)
                    else [],
                    "settings_keys": sorted(list(settings.keys()))
                    if isinstance(settings, dict)
                    else [],
                    "commands": commands_info,
                }
            )

    return {
        "entry": {
            "title": _redact_title(entry.title),
            "data": {
                "email": _redact_email(entry.data.get("email")),
                "password": "***",
            },
            "options": dict(entry.options),
        },
        "appliances": appliances,
    }
