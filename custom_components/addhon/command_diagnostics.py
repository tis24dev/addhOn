# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

from .debug_utils import redact_identity

_LOGGER = logging.getLogger(__name__)

_COLLECTION_LIMIT = 80
_STRING_LIMIT = 512
_RECORD_LIMIT = 4096
_PENDING_LIMIT = 8
_PENDING_TTL = 60.0
_REDACTED = "***"
_EXTRA_IDENTITY_KEYS = frozenset(
    {
        "accountid",
        "applianceid",
        "cloudid",
        "deviceid",
        "uniqueid",
        "userid",
    }
)


@dataclass(frozen=True, slots=True)
class _PendingUpdate:
    action: str
    expected: dict[str, str]
    timestamp: float


_PENDING: WeakKeyDictionary[object, deque[_PendingUpdate]] = WeakKeyDictionary()
_PENDING_LOCK = threading.Lock()


def _identity_key(value: str) -> bool:
    normalized = "".join(character for character in value.lower() if character.isalnum())
    return normalized in _EXTRA_IDENTITY_KEYS


def _sort_key(value: object) -> tuple[str, str]:
    try:
        text = str(value)
    except Exception:
        text = type(value).__name__
    return type(value).__name__, text[:_STRING_LIMIT]


def _bound(value: object) -> object:
    if isinstance(value, str):
        return value[:_STRING_LIMIT]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        items = sorted(value.items(), key=lambda item: _sort_key(item[0]))
        for key, item in items[:_COLLECTION_LIMIT]:
            bounded_key = str(key)[:_STRING_LIMIT]
            result[bounded_key] = (
                _REDACTED if _identity_key(bounded_key) else _bound(item)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_bound(item) for item in value[:_COLLECTION_LIMIT]]
    if isinstance(value, (set, frozenset)):
        return [
            _bound(item)
            for item in sorted(value, key=_sort_key)[:_COLLECTION_LIMIT]
        ]
    return {"type": type(value).__name__[:_STRING_LIMIT]}


def _encode_record(record: dict[str, object]) -> str:
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
    if len(encoded) <= _RECORD_LIMIT:
        return encoded

    reduced: dict[str, object] = {
        "event": record.get("event"),
        "truncated": True,
    }
    for key in sorted(record):
        if key in reduced:
            continue
        candidate = {**reduced, key: record[key]}
        candidate_encoded = json.dumps(
            candidate,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(candidate_encoded) <= _RECORD_LIMIT:
            reduced = candidate
    return json.dumps(reduced, sort_keys=True, separators=(",", ":"))


def _log_failure() -> None:
    try:
        _LOGGER.debug("command diagnostic event failed")
    except Exception:
        pass


def emit_command_event(event: str, fields: Mapping[str, object]) -> None:
    try:
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        raw_record = dict(fields)
        raw_record["event"] = event
        redacted = redact_identity(raw_record)
        bounded = _bound(redacted)
        if not isinstance(bounded, dict):
            raise TypeError("command diagnostic record must be a mapping")
        _LOGGER.debug("%s", _encode_record(bounded))
    except Exception:
        _log_failure()


def _expected_values(payload: Mapping[str, object]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in sorted(payload.items(), key=lambda item: _sort_key(item[0])):
        name = str(key)[:_STRING_LIMIT]
        if _identity_key(name):
            continue
        redacted = redact_identity({name: value}).get(name)
        if redacted == _REDACTED or not isinstance(redacted, (str, int, float)):
            continue
        result[name] = str(redacted)[:_STRING_LIMIT]
        if len(result) == _COLLECTION_LIMIT:
            break
    return result


def record_expected_update(
    appliance: object,
    action: str,
    payload: Mapping[str, object],
) -> None:
    try:
        expected = _expected_values(payload)
        if not expected:
            return
        now = time.monotonic()
        pending = _PendingUpdate(
            action=str(action)[:_STRING_LIMIT],
            expected=expected,
            timestamp=now,
        )
        with _PENDING_LOCK:
            queue = _PENDING.get(appliance)
            if queue is None:
                queue = deque(maxlen=_PENDING_LIMIT)
                _PENDING[appliance] = queue
            while queue and now - queue[0].timestamp >= _PENDING_TTL:
                queue.popleft()
            queue.append(pending)
    except Exception:
        _log_failure()


def observe_mqtt_update(
    appliance: object,
    values: Mapping[str, object],
    timestamp: float | None = None,
) -> None:
    try:
        now = time.monotonic() if timestamp is None else float(timestamp)
        observed = {
            str(key): str(value)
            for key, value in values.items()
            if isinstance(key, str) and isinstance(value, (str, int, float))
        }
        match: _PendingUpdate | None = None
        with _PENDING_LOCK:
            queue = _PENDING.get(appliance)
            if queue is None:
                return
            while queue and now - queue[0].timestamp >= _PENDING_TTL:
                queue.popleft()
            for index, pending in enumerate(queue):
                if any(
                    observed.get(key) == expected
                    for key, expected in pending.expected.items()
                ):
                    match = pending
                    del queue[index]
                    break
            if not queue:
                del _PENDING[appliance]

        if match is None:
            return
        expected_keys = sorted(
            key
            for key, expected in match.expected.items()
            if observed.get(key) == expected
        )
        missing_keys = sorted(
            key
            for key, expected in match.expected.items()
            if observed.get(key) != expected
        )
        unexpected_keys = sorted(
            key
            for key, value in observed.items()
            if key not in match.expected or match.expected[key] != value
        )
        fields: dict[str, object] = {
            "action": match.action,
            "expected_keys": expected_keys,
            "method": "time_window_key_value",
            "missing_keys": missing_keys,
            "unexpected_keys": unexpected_keys,
        }
        emit_command_event("shadow_update", fields)
        emit_command_event("contract_check", fields)
    except Exception:
        _log_failure()


__all__ = [
    "emit_command_event",
    "observe_mqtt_update",
    "record_expected_update",
]
