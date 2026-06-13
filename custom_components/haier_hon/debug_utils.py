"""Shared debug logging helpers for Haier hOn."""
from __future__ import annotations

DEBUG_KEY_SAMPLE_LIMIT = 80


def debug_key_sample(values: dict) -> list[str]:
    """Return a bounded, sorted sample of mapping keys for debug logs."""
    keys = sorted(str(key) for key in values.keys())
    if len(keys) <= DEBUG_KEY_SAMPLE_LIMIT:
        return keys
    return [
        *keys[:DEBUG_KEY_SAMPLE_LIMIT],
        f"... (+{len(keys) - DEBUG_KEY_SAMPLE_LIMIT})",
    ]


def command_names(appliance) -> list[str]:
    """Return sorted command names exposed by an appliance."""
    commands = getattr(appliance, "commands", None)
    return sorted(commands.keys()) if isinstance(commands, dict) else []


def param_snapshot(params) -> dict:
    """Return a compact debug snapshot of command parameters."""
    if not isinstance(params, dict):
        return {"<non-dict>": type(params).__name__}
    snapshot = {}
    for name, param in params.items():
        values = getattr(param, "values", None)
        snapshot[str(name)] = {
            "value": getattr(param, "value", None),
            "has_values": hasattr(param, "values"),
            "values_count": len(values) if isinstance(values, dict) else None,
        }
    return snapshot


def redact_email(email: str | None) -> str | None:
    """Redact an account email for logs: 'a@b.com' -> '***@b.com'."""
    if not email:
        return None
    if "@" not in email:
        return "***"
    _, domain = email.split("@", 1)
    return f"***@{domain}"


__all__ = [
    "DEBUG_KEY_SAMPLE_LIMIT",
    "command_names",
    "debug_key_sample",
    "param_snapshot",
    "redact_email",
]
