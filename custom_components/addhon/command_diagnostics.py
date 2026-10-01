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

from .client.helpers import parse_cloud_timestamp
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
# Delivery check (issue #115, O4; apk2/analysis/issue115-hw/decisioni/
# 9-prova-ac-mik.md). The cloud answers resultCode "0" to a command it ACCEPTED,
# whether or not the appliance then runs it. The verdict waits for the first cloud
# read at least _DELIVERY_SETTLE after the send: before that the shadow still holds
# our own optimistic write (HonAttribute's 10 s shield), and a delivered command
# needs up to ~5 s to show up (0.6-2.6 s accepted -> executed in 34 dumped
# entries of 7 types, 4.9 s on #65, value +0.2-0.5 s). A read past _DELIVERY_TTL
# is too late to blame the send for what it shows (three polls missed; the app may
# have written since). _UNDELIVERED_GAP: the slot is stamped to 0.1 s; undelivered
# sends showed 0.0 (4 of 4), delivered ones never less than 0.6.
_DELIVERY_SETTLE = 15.0
_DELIVERY_TTL = 180.0
_UNDELIVERED_GAP = 0.3
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


@dataclass(frozen=True, slots=True)
class DeliveryBaseline:
    """Taken just before a send: the history slot's `timestampAccepted`, and the
    payload's keys whose value differs from the shadow's (only those can show
    whether the command was applied)."""

    accepted: object
    expected: dict[str, str]


@dataclass(slots=True)
class _DeliveryCheck:
    action: str
    command: str
    appliance_type: str
    accepted: object
    expected: dict[str, str]
    timestamp: float


# One check per appliance, not a queue: the history slot only ever shows the
# latest command, so an older check could not be told apart from a newer send.
_DELIVERY: WeakKeyDictionary[object, _DeliveryCheck] = WeakKeyDictionary()
_DELIVERY_LOCK = threading.Lock()


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
        _confirm_delivery(appliance, observed)
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


def _shadow_text(attributes: object, key: str) -> str | None:
    """The shadow's value of `key`, spelled for comparison; None when it has none."""
    if not isinstance(attributes, Mapping):
        return None
    parameters = attributes.get("parameters")
    if not isinstance(parameters, Mapping) or key not in parameters:
        return None
    value = parameters[key]
    value = getattr(value, "value", value)
    if not isinstance(value, (str, int, float)) or value == "":
        return None
    return comparable_text(value)[:_STRING_LIMIT]


def _history_times(attributes: object) -> tuple[object, object]:
    if not isinstance(attributes, Mapping):
        return None, None
    history = attributes.get("commandHistory")
    if not isinstance(history, Mapping):
        return None, None
    return history.get("timestampAccepted"), history.get("timestampExecuted")


def delivery_baseline(
    appliance: object,
    payload: Mapping[str, object],
) -> DeliveryBaseline | None:
    """What `record_delivery_check` compares against, read BEFORE the send.

    Before, because a poll can land while the send is awaited and stamp our own
    command into the slot; and because the dispatcher writes the sent values into
    the shadow as soon as the cloud says yes. A key whose value is already the one
    sent is left out: the device has nothing to change, and the cloud may not even
    forward it (the AC's same-value restore came back in 0.1 s). A key the shadow
    lacks stays in: the slot can still speak for it.
    """
    try:
        attributes = getattr(appliance, "attributes", None)
        expected = {
            key: value
            for key, value in _expected_values(payload).items()
            if _shadow_text(attributes, key) != value
        }
        accepted, _executed = _history_times(attributes)
        return DeliveryBaseline(accepted=accepted, expected=expected)
    except Exception:
        _log_failure()
        return None


def record_delivery_check(
    appliance: object,
    action: str,
    command: str,
    baseline: DeliveryBaseline | None,
    timestamp: float | None = None,
) -> None:
    """Arm the check for one accepted send, replacing the appliance's previous one.

    The previous one goes even when this send has nothing to check: from now on
    the slot describes this send, not that one.
    """
    try:
        now = time.monotonic() if timestamp is None else float(timestamp)
        appliance_type = str(getattr(appliance, "appliance_type", "") or "")
        with _DELIVERY_LOCK:
            _DELIVERY.pop(appliance, None)
            if baseline is None or not baseline.expected:
                return
            _DELIVERY[appliance] = _DeliveryCheck(
                action=str(action)[:_STRING_LIMIT],
                command=str(command)[:_STRING_LIMIT],
                appliance_type=appliance_type[:_STRING_LIMIT],
                accepted=baseline.accepted,
                expected=dict(baseline.expected),
                timestamp=now,
            )
    except Exception:
        _log_failure()


def _confirm_delivery(appliance: object, observed: Mapping[str, str]) -> None:
    """An MQTT push carrying the sent values is the device's own proof: those keys
    are done, and with none left the check is closed. A value the app writes after
    that must not be blamed on our send at the next poll. Its own guard: it must
    never cost `observe_mqtt_update` the command correlation that follows."""
    try:
        with _DELIVERY_LOCK:
            check = _DELIVERY.get(appliance)
            if check is None:
                return
            confirmed = [
                key
                for key, value in check.expected.items()
                if observed.get(key) == value
            ]
            for key in confirmed:
                del check.expected[key]
            if not check.expected:
                del _DELIVERY[appliance]
    except Exception:
        _log_failure()


def _undelivered_gap(attributes: object, accepted_before: object) -> float | None:
    """Seconds from accepted to executed when they say "never reached the device".

    None when the slot cannot say: it still shows the command before ours, a time
    is missing or unreadable, or executed is OLDER than accepted (4 of 41 dumped
    entries, meaning unknown, so no verdict).
    """
    accepted_raw, executed_raw = _history_times(attributes)
    if accepted_raw is None or accepted_raw == accepted_before:
        return None
    accepted = parse_cloud_timestamp(accepted_raw)
    executed = parse_cloud_timestamp(executed_raw)
    if accepted is None or executed is None:
        return None
    gap = (executed - accepted).total_seconds()
    return gap if 0 <= gap < _UNDELIVERED_GAP else None


def observe_shadow_read(appliance: object, timestamp: float | None = None) -> None:
    """Judge the appliance's pending delivery check on a fresh cloud read.

    Called after every `load_attributes`, so the first read past
    `_DELIVERY_SETTLE` decides, and the check is dropped whatever the verdict:
    one WARNING per send at most, never an error for the user.
    """
    try:
        now = time.monotonic() if timestamp is None else float(timestamp)
        with _DELIVERY_LOCK:
            check = _DELIVERY.get(appliance)
            if check is None or now - check.timestamp < _DELIVERY_SETTLE:
                return
            del _DELIVERY[appliance]
        age = now - check.timestamp
        if age >= _DELIVERY_TTL:
            return
        attributes = getattr(appliance, "attributes", None)
        reasons: list[str] = []
        gap = _undelivered_gap(attributes, check.accepted)
        if gap is not None:
            reasons.append(
                f"the cloud marked it executed {gap:.1f} s after accepting it, "
                "so it likely never reached the appliance"
            )
        stale = []
        for key, sent in check.expected.items():
            current = _shadow_text(attributes, key)
            if current is not None and current != sent:
                stale.append(f"{key} is {redact_identity(current)}, {sent} was sent")
        if stale:
            reasons.append(f"{age:.0f} s after the send " + ", ".join(stale))
        if reasons:
            _LOGGER.warning(
                "hOn accepted %s (%s) for a %s appliance but it does not look "
                "applied: %s",
                check.action,
                check.command,
                check.appliance_type,
                "; ".join(reasons),
            )
    except Exception:
        _log_failure()


__all__ = [
    "DeliveryBaseline",
    "delivery_baseline",
    "emit_command_event",
    "observe_mqtt_update",
    "observe_shadow_read",
    "record_delivery_check",
    "record_expected_update",
]
