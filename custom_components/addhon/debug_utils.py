"""Shared debug logging helpers for Haier hOn."""
from __future__ import annotations

import re

DEBUG_KEY_SAMPLE_LIMIT = 80

# A MAC address in either ':' or '-' form (the hOn MQTT topic embeds the appliance
# MAC, e.g. 'haier/things/3c-71-bf-bd-32-2c/event/appliancestatus/update').
_MAC_RE = re.compile(r"[0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5}")


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


_REDACTED = "***"

# Identity/credential key names whose VALUE must be masked in logs. Matched by
# EXACT key name, case-insensitive (not substring). Kept here (pure util, no HA
# import) so the transport layer can redact before logging without reaching the
# HA-layer diagnostics module. MUST stay a superset of diagnostics._TO_REDACT so
# the log path redacts at least what the Download-Diagnostics path does (a
# drift-guard test enforces it).
_IDENTITY_KEYS = frozenset(
    {
        "serial",
        "serialnumber",
        "serial_number",
        "mac",
        "macaddress",
        "mac_address",
        "code",
        "nickname",
        "nick_name",
        "email",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "secret",
        "transactionid",
        "transaction_id",
        "mobileid",
        "mobile_id",
    }
)


def redact_identity(obj):
    """Deep-copy a mapping/list masking identity/credential VALUES to '***'.

    Redaction is keyed on the dict KEY name (exact, case-insensitive) against
    _IDENTITY_KEYS; everything else passes through. Non-container leaves are
    returned unchanged. Pure (no HA import) and non-mutating (returns a copy), so
    transport modules can redact a raw appliance/command dict before logging it.
    """
    if isinstance(obj, dict):
        return {
            key: (
                _REDACTED
                if isinstance(key, str) and key.lower() in _IDENTITY_KEYS
                else redact_identity(val)
            )
            for key, val in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact_identity(item) for item in obj]
    return obj


def redact_mac(mac: str | None) -> str | None:
    """Redact a single MAC value for logs. A MAC is entirely identity material, so
    any non-empty value -> '***' (consistent with diagnostics); falsy -> None."""
    if not mac:
        return None
    return _REDACTED


def redact_id(value, parent_id=None):
    """Redact a device identifier (MAC / serial / code / nickname) or an entity
    unique_id for logs.

    A bare identifier is masked entirely -> '***'. When `parent_id` is given and is a
    prefix of `value` (an entity unique_id is `f"{appliance_id}_{suffix}"`), ONLY the
    identifier prefix is masked and the human-useful suffix is kept, e.g.
    'AA:BB:..._program' -> '***_program', so the logs still say WHICH entity without
    exposing the MAC. A falsy value is returned unchanged (so an `or <fallback>` at the
    call site still works)."""
    if not value:
        return value
    text = value if isinstance(value, str) else str(value)
    if parent_id:
        prefix = parent_id if isinstance(parent_id, str) else str(parent_id)
        if prefix and text.startswith(prefix):
            return _REDACTED + text[len(prefix):]
    return _REDACTED


def redact_topic(topic):
    """Mask any MAC embedded in an MQTT topic, keeping the rest of the path.

    'haier/things/3c-71-bf-bd-32-2c/event/appliancestatus/update' ->
    'haier/things/***/event/appliancestatus/update'. The MAC is hard device identity;
    the event path is the useful diagnostic part and is preserved. A falsy topic is
    returned unchanged."""
    if not topic:
        return topic
    return _MAC_RE.sub(_REDACTED, topic if isinstance(topic, str) else str(topic))


__all__ = [
    "DEBUG_KEY_SAMPLE_LIMIT",
    "command_names",
    "debug_key_sample",
    "param_snapshot",
    "redact_email",
    "redact_id",
    "redact_identity",
    "redact_mac",
    "redact_topic",
]
