# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a washer, washer-dryer, dryer or dishwasher did over its last cycles (issue #138).

A diagnostics dump is a photograph: the last value of each key, the last read a push
asked for. Whether the machine echoes a start's options, when the activity opens and
closes against `machMode`, what the phase after the end does: each needs the sequence.
This log keeps it, in memory, bounded, per appliance. Events, by `kind`:

- `shadow`: the values a push (`mqtt`), a read of the context (`rest`) or our own
  shielded write after a command (`local`) handed over. A push keeps every row (R1),
  marked `refused` by the 10 s shield of our own write (the start's echo of
  `stainType`/`antiCreaseStatus` lands inside it), `same` as the value held, or
  `cloud_from` when the cloud's old value is not the one held (a push missed); the keys
  absent from the shadow are listed as `unseeded`. A read keeps the rows refused,
  changed, or whose `lastUpdate` moved (`stamp`); a write of ours the rows it changed.
  Pushes that only step the countdown and consumption counters, with nothing between,
  are one event (`until`, `steps`);
- `context`: a read of the context whose activity, `commandHistory` slot or connection
  differs from the last one logged, or whose answer was dropped as stale; a read that
  repeats it is only counted on that event (`unchanged`, `last_at`);
- `command`: each command sent and how the cloud answered;
- `cycle_read`: a read a push at a cycle's start or end asked for, as the MQTT transport
  fills it in;
- `session`: a new appliance object took the log (setup, reload).

One ring of `EVENT_LIMIT` events for every kind (R2), what it drops counted by kind.
Kept per appliance id at module level: a reload of the entry adds to the same log, a
restart of Home Assistant empties it. Values are text, MAC-shaped substrings masked, then
cut to `TEXT_LIMIT`; who started a cycle and its title never enter it (`IDENTITY_KEYS`).

Thread-safe: a push lands on an awscrt thread, a read on the client loop, the dump on
Home Assistant's. One lock per log; nothing is logged, awaited or rendered under it.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any, Callable, NamedTuple, Optional

from ...debug_utils import _MAC_RE

EVENT_LIMIT = 2000
ROW_LIMIT = 128
TEXT_LIMIT = 64

# Who started a cycle and from where, never what it does: the hOn accounts, the phone,
# the start's title (a favourite's name is the user's own text), the MAC and the
# command's transaction id (`<MAC>_<timestamp>`). Left out of the log and of the options
# a cycle is kept with (`HonAppliance._start_options`).
IDENTITY_KEYS = frozenset({
    "principalUserId", "secondaryUserId", "macAddress", "prStrDisp", "prStr",
    "mobileId", "mobileOs", "osVersion", "appVersion", "deviceModel", "transactionId",
})
_IDENTITY = frozenset(key.lower() for key in IDENTITY_KEYS)

# The counters a running cycle moves a step at a time, and the step of one push (None:
# any increase). A push of these steps alone joins the run before it.
_COUNTERS: dict[str, Optional[float]] = {
    "remainingTimeMM": -1,
    "remainingMainWashTime": -1,
    "currentCycleTime": 1,
    "currentWaterUsed": None,
    "currentElectricityUsed": None,
    "totalWaterUsed": None,
    "totalElectricityUsed": None,
}
_PLAIN = frozenset({"key", "before", "after"})
_CONTAINERS = (Mapping, list, tuple, set, frozenset)


class ShadowRow(NamedTuple):
    """One value handed to the shadow: held `before`, held `after` (the value offered
    when `applied` is False), the cloud's own old value and the row's `lastUpdate` when
    it moved."""

    key: str
    before: str | None
    after: str | None
    applied: bool
    cloud_old: str | None = None
    stamp: str | None = None


def is_identity_key(key: Any) -> bool:
    """Whether `key` names who started a cycle rather than what it does."""
    return isinstance(key, str) and key.lower() in _IDENTITY


def _text(value: Any) -> Optional[str]:
    """A value as the log keeps it: text, MAC-shaped substrings masked, then cut."""
    if value is None or isinstance(value, _CONTAINERS):
        return None
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 - a foreign object must not cost the event
        return None
    return _MAC_RE.sub("***", text)[:TEXT_LIMIT]


def value_text(value: Any) -> Optional[str]:
    """`value` as the log keeps it, None when it has no text. Never raises: a row the
    cloud sent with `parNewVal` null or a number leaves a held value whose `str()`
    raises, and the hooks read the value held before and after each update with this:
    a held value is read raw (`_value`), not through `str()`."""
    return _text(getattr(value, "_value", value))


def _scalars(values: Any) -> dict[str, Optional[str]]:
    """The scalar entries of `values` as text, the identity keys left out."""
    kept: dict[str, Optional[str]] = {}
    for key, value in values.items() if isinstance(values, Mapping) else ():
        if len(kept) == ROW_LIMIT:
            break
        name = _text(key)
        if name is not None and not is_identity_key(key) and not isinstance(value, _CONTAINERS):
            kept[name] = _text(value)
    return kept


def context_summary(attributes: Mapping, activity: Any) -> dict:
    """What one read of the context says about the cycle, as the log compares it.

    `activity` is the read's activity, None when the payload carried none. Its start and
    end and the scalars of its `attributes` are kept; of the `commandHistory` slot its
    scalars and its command's; of `lastConnEvent` the category. Identity keys are left
    out everywhere.
    """
    attributes = attributes if isinstance(attributes, Mapping) else {}
    if not isinstance(activity, Mapping):
        summary_activity: Optional[dict] = None
    elif not activity:
        summary_activity = {}
    else:
        summary_activity = {
            key: _text(activity.get(key))
            for key in ("activityStarted", "activityEnded", "completed")
        }
        summary_activity["attributes"] = _scalars(activity.get("attributes"))
    slot = attributes.get("commandHistory")
    connection = attributes.get("lastConnEvent")
    return {
        "activity": summary_activity,
        "commandHistory": (
            _scalars(slot) | {"command": _scalars(slot.get("command"))}
            if isinstance(slot, Mapping)
            else None
        ),
        "connection": (
            _text(connection.get("category")) if isinstance(connection, Mapping) else None
        ),
    }


def _row(road: str, row: ShadowRow) -> Optional[dict]:
    """`row` as the log keeps it, marked, or None when `road` does not keep it."""
    key = _text(row.key)
    if key is None:
        return None
    before, after, applied = _text(row.before), _text(row.after), bool(row.applied)
    entry: dict[str, Any] = {"key": key, "before": before, "after": after}
    if not applied:
        entry["refused"] = True
    elif before == after:
        entry["same"] = True
    cloud_old = _text(row.cloud_old)
    if cloud_old is not None and cloud_old != before:
        entry["cloud_from"] = cloud_old
    if (stamp := _text(row.stamp)) is not None:
        entry["stamp"] = stamp
    if road == "mqtt" or not applied or before != after:
        return entry
    return entry if road == "rest" and stamp is not None else None


def _step(entry: dict) -> bool:
    """Whether `entry` is an unmarked step of a counter a running cycle moves."""
    if entry.keys() != _PLAIN or entry["key"] not in _COUNTERS:
        return False
    try:
        change = float(entry["after"]) - float(entry["before"])
    except (TypeError, ValueError):
        return False
    step = _COUNTERS[entry["key"]]
    return change > 0 if step is None else change == step


def _record(record: Any) -> dict:
    """A cycle read's record as the transport has filled it in so far.

    The transport writes it on the client loop without this log's lock: each level is
    copied whole (`dict()`, `list()`) before it is read.
    """
    record = dict(record) if isinstance(record, Mapping) else {}
    reads = list(record.get("reads")) if isinstance(record.get("reads"), list) else []
    return {
        "edge": _text(record.get("edge")),
        "asked_at": _text(record.get("asked_at")),
        "reads": [
            {
                key: value if isinstance(value, bool) else _text(value)
                for key, value in dict(read).items()
                if key in ("at", "settled", "dropped", "error")
            }
            for read in reads
            if isinstance(read, Mapping)
        ],
        "outcome": _text(record.get("outcome")),
    }


def _copy(value: Any) -> Any:
    """`value` with its dicts and lists copied, for a snapshot."""
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value


class CycleLog:
    """One appliance's log; see the module docstring."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._since = clock()
        self._events: deque[dict] = deque(maxlen=EVENT_LIMIT)
        self._seq = 0
        self._sessions = 0
        self._dropped: dict[str, int] = {}
        # The value of each key as last logged, None before the first seed: what a later
        # session's first read is compared with.
        self._known: Optional[dict[str, Optional[str]]] = None
        # The newest event while it is a run of counter steps, and the context event
        # whose summary the next reads are compared with.
        self._run: Optional[dict] = None
        self._context: Optional[dict] = None

    # -- writers (each takes the lock once; the event is built before) ----------
    def _append(self, kind: str, now: float, **fields: Any) -> dict:
        """Add an event; under the lock. Any event ends a run of counter steps."""
        if len(self._events) == EVENT_LIMIT:
            evicted = self._events[0]
            self._dropped[evicted["kind"]] = self._dropped.get(evicted["kind"], 0) + 1
            if evicted is self._context:
                # The next read is logged again, so the dump always shows the last one.
                self._context = None
        self._seq += 1
        event = {"seq": self._seq, "at": now, "kind": kind, **fields}
        self._events.append(event)
        self._run = None
        return event

    def session(self) -> None:
        """A new appliance object took this log (setup, reload)."""
        now = self._clock()
        with self._lock:
            self._sessions += 1
            self._append("session", now, session=self._sessions)

    def seed(self, values: Mapping[str, Any]) -> None:
        """The first read of a session: stored silently on the log's first session,
        logged as what differs from the values last logged (`resumed`) on later ones."""
        now = self._clock()
        current = {
            name: _text(value)
            for key, value in values.items()
            if (name := _text(key)) is not None
        }
        with self._lock:
            known, self._known = self._known, current
            if known is None:
                return
            rows = [
                {"key": key, "before": known.get(key), "after": value}
                for key, value in current.items()
                if known.get(key) != value
            ]
            if rows:
                self._append("shadow", now, road="rest", resumed=True, **_bounded(rows))

    def shadow(
        self,
        road: str,
        rows: Iterable[ShadowRow],
        *,
        cloud_at: Any = None,
        event_id: Any = None,
        unseeded: Iterable[str] = (),
    ) -> None:
        """The rows one push, read or write of ours handed over; see the module."""
        now = self._clock()
        kept = [entry for row in rows if (entry := _row(road, row)) is not None]
        names = [name for name in map(_text, unseeded) if name is not None]
        if not kept and not names:
            return
        fields = _bounded(kept)
        if names:
            fields["unseeded"] = names[:ROW_LIMIT]
            if len(names) > ROW_LIMIT:
                fields["truncated"] = fields.get("truncated", 0) + len(names) - ROW_LIMIT
        if (cloud_at := _text(cloud_at)) is not None:
            fields["cloud_at"] = cloud_at
        if (event_id := _text(event_id)) is not None:
            fields["event_id"] = event_id
        steps = bool(kept) and fields.keys() <= {"rows", "cloud_at", "event_id"} and all(
            map(_step, kept)
        )
        with self._lock:
            if self._known is not None:
                self._known.update(
                    (entry["key"], entry["after"]) for entry in kept if "refused" not in entry
                )
            if steps and self._extend_run(road, kept, now, event_id):
                return
            event = self._append("shadow", now, road=_text(road), **fields)
            if steps:
                self._run = event

    def _extend_run(self, road: str, kept: list, now: float, event_id: Any) -> bool:
        """Join `kept` to the run before it when it continues it; under the lock."""
        run = self._run
        if run is None or run["road"] != road:
            return False
        held = {entry["key"]: entry for entry in run["rows"]}
        if (
            len(kept) != len(held)
            or {entry["key"] for entry in kept} != held.keys()
            or any(held[entry["key"]]["after"] != entry["before"] for entry in kept)
        ):
            return False
        for entry in kept:
            held[entry["key"]]["after"] = entry["after"]
        run["until"] = now
        run["steps"] = run.get("steps", 1) + 1
        if event_id is not None:
            run["last_event_id"] = event_id
        return True

    def context(
        self,
        *,
        reason: str,
        order: int,
        asked: float,
        applied: bool,
        summary: Mapping | None = None,
    ) -> None:
        """One read of the context: an event when its answer was dropped or its summary
        differs from the last one logged, otherwise counted on that one."""
        now = self._clock()
        fields = {
            "reason": _text(reason),
            "order": order if type(order) is int else None,
            "asked": asked if type(asked) in (int, float) else None,
            "applied": bool(applied),
        }
        with self._lock:
            last = self._context
            if applied and last is not None and last["summary"] == summary:
                last["unchanged"] += 1
                last["last_at"] = now
                return
            if not applied:
                self._append("context", now, **fields)
                return
            self._context = self._append(
                "context", now, **fields, summary=summary, unchanged=0
            )

    def command(
        self,
        *,
        name: str,
        programme: str | None,
        sent: str | None,
        outcome: str,
        code: Any = None,
        error: str | None = None,
    ) -> None:
        """One command sent (`sent` is its timestamp, which `/history` keeps too) and
        how the cloud answered: `accepted`, `rejected` (`code`, `error`), `raised`."""
        now = self._clock()
        fields = {
            "name": _text(name),
            "programme": _text(programme),
            "sent": _text(sent),
            "outcome": _text(outcome),
        }
        if code is not None:
            fields["result_code"] = _text(code)
        if error is not None:
            fields["error"] = _text(error)
        with self._lock:
            self._append("command", now, **fields)

    def cycle_read(self, record: Mapping) -> None:
        """A read a push at a cycle's edge asked for; the transport fills `record` in
        afterwards, and the snapshot shows it as it stands then."""
        now = self._clock()
        with self._lock:
            self._append("cycle_read", now, record=record)

    # -- reader ----------------------------------------------------------------
    def snapshot(self) -> dict:
        """A copy of the log for the dump; instants are Home Assistant's epoch seconds,
        except the cloud's own, kept as the text it sent."""
        with self._lock:
            events = [
                {key: value if key == "record" else _copy(value) for key, value in event.items()}
                for event in self._events
            ]
            state = {
                "since": self._since,
                "sessions": self._sessions,
                "dropped": dict(self._dropped),
            }
        for event in events:
            if "record" in event:
                event.update(_record(event.pop("record")))
        limits = {"events": EVENT_LIMIT, "rows": ROW_LIMIT, "text": TEXT_LIMIT}
        return {**state, "limits": limits, "events": events}


def _bounded(rows: list) -> dict:
    """{"rows": at most `ROW_LIMIT` rows, "truncated": how many were cut, if any}."""
    fields: dict[str, Any] = {"rows": rows[:ROW_LIMIT]}
    if len(rows) > ROW_LIMIT:
        fields["truncated"] = len(rows) - ROW_LIMIT
    return fields


_LOGS: dict[str, CycleLog] = {}
_LOGS_LOCK = threading.Lock()


def attach(appliance_id: str) -> CycleLog:
    """The log of `appliance_id`, created on first use; logs a new session."""
    with _LOGS_LOCK:
        log = _LOGS.get(appliance_id)
        if log is None:
            log = _LOGS[appliance_id] = CycleLog()
    log.session()
    return log


def forget_all() -> None:
    """Drop every log (tests)."""
    with _LOGS_LOCK:
        _LOGS.clear()
