# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import itertools
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

from .debug_utils import comparable_text, redact_identity

_LOGGER = logging.getLogger(__name__)

_COLLECTION_LIMIT = 80
_STRING_LIMIT = 512
_RECORD_LIMIT = 4096
_PENDING_LIMIT = 8
_PENDING_TTL = 60.0
_REDACTED = "***"
# Depth cap for _bound's traversal: comfortably above any real command/shadow
# payload's nesting (2-4 levels), but finite -- so a cyclic or pathologically
# deep structure gets a placeholder instead of a RecursionError that would
# silently drop the whole diagnostic event.
_MAX_DEPTH = 12
_DEPTH_PLACEHOLDER = "<max-depth>"
_CYCLE_PLACEHOLDER = "<cycle>"
_EVENTS = frozenset(
    {
        "command_intent",
        "command_payload",
        "command_result",
        "contract_check",
        "shadow_update",
    }
)
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


def _bound(
    value: object,
    *,
    depth: int = 0,
    seen: frozenset[int] = frozenset(),
) -> object:
    """Single budgeted traversal: bounds depth, item count, and string length,
    breaks cycles, and materializes sets into sorted lists -- all in one pass,
    BEFORE any full-structure copy is made. Only a bounded SAMPLE of each
    collection (at most _COLLECTION_LIMIT items, taken by iteration order via
    islice) is ever touched, so a huge mapping/set costs O(_COLLECTION_LIMIT)
    here instead of a full sort/copy of every item it holds; `seen` tracks
    container ids on the CURRENT recursion path only (not every object ever
    visited), so shared-but-acyclic references are not mistaken for a cycle.
    """
    if isinstance(value, str):
        return value[:_STRING_LIMIT]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if not isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return {"type": type(value).__name__[:_STRING_LIMIT]}
    marker = id(value)
    if marker in seen:
        return _CYCLE_PLACEHOLDER
    if depth >= _MAX_DEPTH:
        return _DEPTH_PLACEHOLDER
    child_seen = seen | {marker}
    if isinstance(value, Mapping):
        sample = sorted(
            itertools.islice(value.items(), _COLLECTION_LIMIT),
            key=lambda item: _sort_key(item[0]),
        )
        result: dict[str, object] = {}
        for key, item in sample:
            bounded_key = str(key)[:_STRING_LIMIT]
            result[bounded_key] = (
                _REDACTED
                if _identity_key(bounded_key)
                else _bound(item, depth=depth + 1, seen=child_seen)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _bound(item, depth=depth + 1, seen=child_seen)
            for item in itertools.islice(value, _COLLECTION_LIMIT)
        ]
    # set / frozenset: sort only the bounded sample for deterministic output,
    # not the whole set.
    return [
        _bound(item, depth=depth + 1, seen=child_seen)
        for item in sorted(
            itertools.islice(value, _COLLECTION_LIMIT), key=_sort_key
        )
    ]


def _encode_record(record: dict[str, object]) -> str:
    encoded = json.dumps(
        record,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) <= _RECORD_LIMIT:
        return encoded

    event = record.get("event")
    reduced: dict[str, object] = {
        "event": event if event in _EVENTS else "contract_check",
        "truncated": True,
    }
    for key in sorted(record):
        if key in reduced:
            continue
        candidate = {**reduced, key: record[key]}
        candidate_encoded = json.dumps(
            candidate,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(candidate_encoded) <= _RECORD_LIMIT:
            reduced = candidate
    encoded = json.dumps(
        reduced,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) <= _RECORD_LIMIT:
        return encoded
    return '{"event":"contract_check","truncated":true}'


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
        valid_event = event if event in _EVENTS else "contract_check"
        if valid_event != event:
            raw_record["invalid_event"] = True
        raw_record["event"] = valid_event
        # Bound FIRST: the budgeted traversal caps depth/items/length and
        # breaks cycles, producing a small, finite, already-materialized (no
        # raw sets left) copy. redact_identity has no such bounds of its own,
        # but is now only ever asked to walk that small, cycle-free result.
        bounded = _bound(raw_record)
        if not isinstance(bounded, dict):
            raise TypeError("command diagnostic record must be a mapping")
        redacted = redact_identity(bounded)
        _LOGGER.debug("%s", _encode_record(redacted))
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
        result[name] = comparable_text(redacted)[:_STRING_LIMIT]
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


def _match_coverage(pending: _PendingUpdate, observed: Mapping[str, str]) -> int:
    """Count of the pending intent's expected key/value pairs the observed
    MQTT push actually confirms. Used to pick the BEST-matching pending
    command instead of the first one sharing any single field (a mandatory
    field like onOff is common to every command on the appliance, so "any
    match" alone would let an older, less-specific command consume a push
    that really belongs to a newer, more fully-confirmed one)."""
    return sum(
        1
        for key, expected in pending.expected.items()
        if observed.get(key) == expected
    )


def observe_mqtt_update(
    appliance: object,
    values: Mapping[str, object],
    timestamp: float | None = None,
) -> None:
    try:
        now = time.monotonic() if timestamp is None else float(timestamp)
        # Both sides are canonicalized where they ENTER, not at each comparison:
        # _match_coverage picks which pending command a push confirms, so a spelling
        # difference there does not just mis-report a field, it can hand the push to
        # the wrong command entirely.
        observed = {
            str(key): comparable_text(value)
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
            match_index: int | None = None
            best_coverage = 0
            # FIFO order (oldest first): a STRICT ">" means the first entry to
            # reach a given coverage score keeps it, so a later entry only
            # displaces it by covering MORE fields -- ties resolve to the
            # older (FIFO) entry, exactly the tie-break rule required.
            for index, pending in enumerate(queue):
                coverage = _match_coverage(pending, observed)
                if coverage > best_coverage:
                    best_coverage = coverage
                    match = pending
                    match_index = index
            if match is not None:
                del queue[match_index]
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
