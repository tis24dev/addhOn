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
    _IDENTITY_KEYS. As a second layer, any MAC embedded in a STRING LEAF is masked
    too (same _MAC_RE as redact_topic) -- so identity that arrives where key-name
    redaction can't reach it (a bare list element, or a value under a benign key, e.g.
    a malformed MQTT `parameters` scalar -- CR#4) does not slip through. Non-MAC
    scalars pass through (a serial has no safe pattern -> documented residual; the
    callers that can receive a bare scalar log only its type). Pure (no HA import) and
    non-mutating (returns a copy), so transport modules can redact a raw dict for logs.
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
    if isinstance(obj, str):
        return _MAC_RE.sub(_REDACTED, obj)
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


def redact_store(store):
    """Redact a coordinator store dump for logs: mask the KEYS, keep the VALUES.

    A coordinator store (e.g. PROGRAM_PENDING_STORE) is keyed by the appliance id
    (a MAC-derived unique_id) -- hard identity -- and its values are non-identity
    program codes (e.g. 'iot_auto'). `dict(store)` in a debug log would dump the raw
    MAC/serial keys, and the AST redaction guard cannot catch a `dict(...)`-wrapped
    arg, so use this. Keys are masked via redact_id (a bare id -> '***'); values pass
    through unchanged (they ARE the diagnostic signal and carry no identity).

    Distinct keys all mask to '***', which in a returned dict would collapse multiple
    appliances' entries into one and silently drop their values -- so a colliding
    masked key gets a stable insertion-order ordinal ('***', '***#2', ...) to preserve
    the count and every value. Non-mapping input is returned unchanged (defensive, so
    the dict(store)->redact_store(store) swap never changes behaviour on a bad type)."""
    if not isinstance(store, dict):
        return store
    out: dict = {}
    for key, value in store.items():
        masked = redact_id(key)
        if masked in out:
            n = 2
            while f"{masked}#{n}" in out:
                n += 1
            masked = f"{masked}#{n}"
        out[masked] = value
    return out


def redact_remoting_summary(entry) -> dict:
    """Leak-proof structural summary of a Salesforce JS-Remoting response entry.

    A 2FA remoting result can carry signed/sensitive material (a `message`/`data`/
    `stackTrace`, or -- in other shapes -- tokens), so a debug log must NEVER dump the
    entry. This keeps ONLY the finite-domain control fields needed to diagnose a 2FA
    failure: the boolean `result`, the int `statusCode`, the `type` ('rpc'/'exception'),
    and a bounded SAMPLE of the KEY NAMES (never the values). Passing this (a Call node)
    instead of the bare entry also keeps the AST leak-guard satisfied by construction."""
    if not isinstance(entry, dict):
        return {"type": type(entry).__name__}
    result = entry.get("result")
    status = entry.get("statusCode")
    return {
        "result": result if isinstance(result, bool) else None,
        "statusCode": status if isinstance(status, int) else None,
        "type": entry.get("type") if isinstance(entry.get("type"), str) else None,
        "keys": debug_key_sample(entry),
    }


__all__ = [
    "DEBUG_KEY_SAMPLE_LIMIT",
    "command_names",
    "debug_key_sample",
    "param_snapshot",
    "redact_email",
    "redact_id",
    "redact_identity",
    "redact_mac",
    "redact_remoting_summary",
    "redact_store",
    "redact_topic",
]
