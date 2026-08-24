# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

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
        # Added after the appliance-list envelope was captured live: the cloud stamps
        # each appliance with its owning Salesforce account and with DynamoDB keys whose
        # PK is `user#<region>:<cognito-identity>` -- the identity itself, in plain text
        # under a two-letter key no exact-name rule would have guessed.
        "sfpersonaccountid",
        "personaccountid",
        "personcontactid",
        "pk",
        "sk",
        "applianceid",
        "eepromid",
    }
)

# Substring rule, applied to the lowercased key AFTER the exact set misses. The exact
# set cannot enumerate a vendor's naming: `token` is in it and `cognitoTokenNew` is not,
# which is how a bearer credential the aggregator returns inside `authInfo` would have
# travelled unmasked into a log this project asks users to paste into public issues.
# Kept deliberately short -- these three words do not appear in a benign key by accident,
# and a wider list would start masking the structure the logs exist to show.
_IDENTITY_KEY_PARTS = ("token", "password", "secret")

# A key NAME is safe to print only when it is a SCHEMA key -- a name the vendor's
# developers typed -- and not a value the vendor used AS a key. The distinction cannot
# be made by blacklist (that is what `cognitoTokenNew` cost), so it is made by SHAPE,
# the same way `_ADDHON_LABEL_RE` bounds a catalog label in the diagnostics dump.
#
# A schema key is a short identifier with at least one lowercase letter: `payload`,
# `applianceTypeName`, `fwVersion`, `authInfo`. A value used as a key is not: an
# identity partition `user#eu-west-1:8f3c-...` fails on `#` and `:`, a serial
# `ABC123456789` fails for having no lowercase, an email fails on `@`, a UUID fails on
# `-`, and anything the vendor made long fails the length bound. Digit runs are capped
# because a schema key does not carry a five-digit number; `tempSelZ2` survives, a
# stock code does not.
_SCHEMA_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,39}$")
_DIGIT_RUN_RE = re.compile(r"[0-9]{5,}")


def _is_schema_key(key) -> bool:
    """True when a key NAME may be printed as-is in a log meant for public issues."""
    if not isinstance(key, str) or not _SCHEMA_KEY_RE.match(key):
        return False
    if _DIGIT_RUN_RE.search(key):
        return False
    return any(c.islower() for c in key)


def _is_identity_key(key) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return lowered in _IDENTITY_KEYS or any(p in lowered for p in _IDENTITY_KEY_PARTS)


# A float holds every integer only up to 2**53. Past that, distinct integers share one
# double, so rendering them is no longer a spelling choice: it discards value.
_EXACT_INTEGER_LIMIT = 2**53


def comparable_text(value) -> str:
    """Canonical text for comparing a value SENT against a value REPORTED BACK.

    The two sides of that comparison arrive spelled differently and neither side is
    wrong. What leaves the machine is a command parameter's `intern_value`, always a
    string ("60"). What comes back is the cloud's raw JSON `parValue`, which may be a
    string, a number or a bool, and which the cloud is free to reformat: "60" echoed
    as "60.0" is the same setting, and a plain `str()` on both sides calls it a
    mismatch.

    So numeric values compare NUMERICALLY and everything else compares as trimmed
    text. Booleans are taken first because everything below goes through str(): a bool
    would become "True", which no numeric parse accepts, and would then compare as
    that literal against the device's own 1/0 spelling.

    A decimal comma is accepted for the same reason `client.helpers.str_to_float`
    accepts it: the hOn cloud does send that spelling, and the engine already reads
    "5,5" as 5.5, so a diagnostic that called it a mismatch would contradict the value
    the integration actually stored.

    This is deliberately more forgiving than `air_purifier.raw_text`, which prepares a
    value to be WRITTEN and must never invent a spelling the schema does not declare.
    Here nothing reaches the wire; the cost of being too strict is a diagnostic that
    reports a healthy round trip as a missing key. Forgiving about SPELLING, though,
    never about value: two different numbers must never land on one string.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).strip()
    number = _as_float(text)
    if number is None:
        return text
    if number != number or number in (float("inf"), float("-inf")):
        # Overflow and NaN keep their raw text. Not for want of a short form (str()
        # would give "inf"), but because collapsing every spelling that overflows onto
        # one token would make "1e400" and a 400-digit number compare EQUAL, which is
        # the one thing this function must not do. No device parameter is infinite, so
        # this only ever costs a mismatch on values that are already nonsense.
        return text
    if abs(number) >= _EXACT_INTEGER_LIMIT:
        # The same rule as overflow, one step earlier. Precision is gone well before
        # inf: str(int(number)) renders the ROUNDED double, so 12345678901234567890
        # and 12345678901234567891 would both land on 12345678901234567168. Keeping
        # the raw text costs a mismatch on a spelling nothing sends, and no parameter
        # in the schema reaches this band.
        return text
    return str(int(number)) if number.is_integer() else str(number)


def _as_float(text: str) -> float | None:
    for candidate in (text, text.replace(",", ".", 1)):
        try:
            return float(candidate)
        except (TypeError, ValueError):
            continue
    return None


def redact_identity(obj):
    """Deep-copy a mapping/list masking identity/credential VALUES to '***'.

    Redaction is keyed on the dict KEY name (case-insensitive) against _IDENTITY_KEYS
    exactly, then against _IDENTITY_KEY_PARTS as a substring -- the second rule exists
    because an exact set cannot enumerate a vendor's naming, and `cognitoTokenNew` is
    the counter-example that proved it: `token` was in the set and that key was not. As a second layer, any MAC embedded in a STRING LEAF is masked
    too (same _MAC_RE as redact_topic) -- so identity that arrives where key-name
    redaction can't reach it (a bare list element, or a value under a benign key, e.g.
    a malformed MQTT `parameters` scalar -- CR#4) does not slip through. Non-MAC
    scalars pass through (a serial has no safe pattern -> documented residual; the
    callers that can receive a bare scalar log only its type). Pure (no HA import) and
    non-mutating (returns a copy), so transport modules can redact a raw dict for logs.
    """
    if isinstance(obj, dict):
        return {
            key: (_REDACTED if _is_identity_key(key) else redact_identity(val))
            for key, val in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact_identity(item) for item in obj]
    if isinstance(obj, str):
        return _MAC_RE.sub(_REDACTED, obj)
    return obj


def safe_key_names(obj) -> list | str:
    """The sorted key names of a mapping, each masked unless it is a schema key.

    The companion of `structure_only` for the places that want the names alone. Same
    rule and the same reason: a mapping the vendor keyed BY a serial or by a cognito
    identity partition puts that identity in the key position, where copying "just the
    names" is copying a value. `sorted(x.keys())` reads as obviously safe and is not.

    Returns "n/a" for a non-mapping, so a caller can print the result either way.
    """
    if not isinstance(obj, dict):
        return "n/a"
    return sorted(
        key if _is_schema_key(key) and not _is_identity_key(key) else _REDACTED
        for key in obj
    )


def structure_only(obj, _depth: int = 0):
    """The SHAPE of a response: key names and value types, never a value.

    `redact_identity` is a blacklist, and a blacklist is always one vendor spelling
    behind -- `cognitoTokenNew` proved that at the cost of a bearer token nearly
    reaching a public issue. Where a log is meant to be pasted in public, the shape is
    the stronger contract: no leaf value is copied at all, so there is nothing to have
    forgotten to mask.

    It loses less than it sounds on the branch that uses it. The empty-list warning
    fires when `appliances` is `[]`, so the response carries no appliance data to
    begin with: what is left is the envelope, which is exactly what the question
    "why is the list empty" is about.

    Key NAMES are emitted, because they are the answer -- but only the ones that pass
    `_is_schema_key`, a SHAPE test rather than a blacklist. A mapping keyed by a serial,
    by an account id or by a cognito identity partition (`user#<region>:<identity>`) is
    a vendor using a VALUE as a key, and its names are masked exactly like values. That
    residual is why this is not simply `{k: type(v)}`: printing every key verbatim would
    reintroduce, one level up, the leak the whole function exists to remove.

    Depth-bounded so a self-referential or pathologically nested body cannot turn a log
    line into a hang; the bound is generous next to the four levels the envelope has.
    """
    if _depth >= 8:
        return "<deeper>"
    if isinstance(obj, dict):
        return {
            (key if _is_schema_key(key) and not _is_identity_key(key) else _REDACTED): (
                _REDACTED
                if _is_identity_key(key) or not _is_schema_key(key)
                else structure_only(val, _depth + 1)
            )
            for key, val in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        # The length is the finding on this branch: `<list of 0>` IS the report.
        if not obj:
            return "<list of 0>"
        return [f"<list of {len(obj)}>", structure_only(obj[0], _depth + 1)]
    if isinstance(obj, bool) or obj is None:
        # Booleans and null are the envelope's own verdicts (`success`), not data.
        return obj
    return f"<{type(obj).__name__}>"


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
    "comparable_text",
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
