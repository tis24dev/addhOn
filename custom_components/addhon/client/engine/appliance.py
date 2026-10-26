# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Native ROOT HonAppliance.

Puts together our whole engine: native attributes (engine.attributes), native loader/
commands/rules/program (engine.command_loader), native per-type layer
(engine.appliances.registry).

Implements ONLY the surface actually consumed (measured across integration + session
+ engine): identity/state properties, load_*/update, settings/data/command_parameters,
sync_*. `api` is OUR transport.api.HonApi.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from time import monotonic
from types import MappingProxyType
from typing import Any, Optional

from ...command_diagnostics import observe_shadow_read
from ..catalog_repository import CommandCatalogRepository
from ..helpers import parse_cloud_timestamp
from . import cycle_log as _cycle_log
from .appliances import registry as _native_appliances
from .attributes import HonAttribute
from .command_loader import HonCommandLoader, command_identity, start_identity
from .commands import HonCommand
from .exceptions import NoAuthenticationException
from .parameter.base import HonParameter
from .parameter.program import CycleActivity

_LOGGER = logging.getLogger(__name__)


# Marks an activity not looked at yet: the first poll after a load records the cycle
# already running, which the `/history` just read describes (or cannot describe).
_UNSEEN = object()
# A context read whose payload carried no `activity` key: it says nothing on the cycle.
_UNREAD = object()
# The `machMode` values under which the context's activity is the running cycle (issue
# #130, decision 6): execution 2, pause 3, delayed start 5, the ones the app's washer
# dashboard shows a cycle for (apk2 decomp.txt @2588640-2588665); the dishwasher's adds
# 4. The app's context read also takes test 8 (@3398666-3398745), left out so that one
# set rules the name, the select and the cycle edges alike (review of PR #139, S4).
_CYCLE_MODES = frozenset({2, 3, 5})
_CYCLE_MODES_BY_TYPE = {"DW": _CYCLE_MODES | {4}}
# The types whose starts are followed when a cycle begins elsewhere (issue #112, decision
# D4 of 2026-10-06): the four the app's dashboard shows a running programme for.
FOLLOWED_TYPES = frozenset({"WM", "WD", "TD", "DW"})
# A push that moves `machMode` into a cycle from outside one, or out of one into the end 7
# or selection 1, asks for a read of the context (issue #130, decision C1): the activity
# that names the cycle comes only with it. The cycle is `cycle_modes`' set, the one the
# name and the select read too (review of PR #139, S4); entering it at the pause 3 is a
# resume, not a start.
_PAUSE = 3
_EDGE_END = frozenset({7, 1})


def cycle_modes(appliance_type: Any) -> Optional[frozenset[int]]:
    """The `machMode` values of a running cycle for `appliance_type`, None if not followed.

    One set for the three things that ask whether a cycle runs: the activity that names
    it (`HonAppliance._watch_cycle`), the push that starts or ends it
    (`HonAppliance.cycle_edge`) and the programme select at rest (`select.py`).
    """
    if appliance_type not in FOLLOWED_TYPES:
        return None
    return _CYCLE_MODES_BY_TYPE.get(appliance_type, _CYCLE_MODES)


def _programme_key(name: Any) -> Optional[str]:
    """A programme name as a start row and an activity can both spell it.

    `/history` and the `commandHistory` slot carry the catalog key
    (`PROGRAMS.WM_WD.IOT_WASH_DARK`), an activity the bare name (`IOT_WASH_DARK`).
    """
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip().rsplit(".", 1)[-1].upper()


def _as_int(value: Any) -> Optional[int]:
    """`value` as an int, None if absent or unreadable (an activity's prCode)."""
    if value is None:
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _open_started(activity: Any) -> Any:
    """`activityStarted` of `activity` when it is open (started, not ended), else None."""
    if not isinstance(activity, dict) or activity.get("activityEnded"):
        return None
    return activity.get("activityStarted") or None


def _pushed_since(attribute: Any, instant: datetime) -> bool:
    """Whether a push delivered `attribute`'s value after `instant` (HA's clock)."""
    received = getattr(attribute, "received_at", None)
    return (
        getattr(attribute, "received_via", None) == "mqtt"
        and isinstance(received, datetime)
        and received > instant
    )


def _newest_start(history: Any) -> dict[str, Any]:
    """The newest `startProgram` body a `/history` list holds, {} without one."""
    for entry in history if isinstance(history, list) else ():
        command = entry.get("command") if isinstance(entry, dict) else None
        if isinstance(command, dict) and command.get("commandName") == "startProgram":
            return command
    return {}


class HonAppliance:
    _MINIMAL_UPDATE_INTERVAL = 5  # seconds
    # Types whose start is followed when a cycle begins elsewhere (see FOLLOWED_TYPES).
    _FOLLOWED_TYPES = FOLLOWED_TYPES
    # Reads of `/history` for one new cycle, one per poll, before giving up (decision of
    # 2026-10-06): a read that failed, or a list without the start just announced.
    _FOLLOW_ATTEMPTS = 3
    # Realtime liveness is authoritative only while RECENT: a realtime message overrides
    # a STALE REST DISCONNECTED only if received within this wall-clock window. Without
    # the bound, a once-seen realtime time would keep a silently-dead appliance online
    # FOREVER, because the cloud's lastConnEvent can stay frozen at an OLD disconnect
    # indefinitely (observed: frozen ~2.5h) and never emit a disconnect newer than the
    # last traffic. After the window we defer to the REST lastConnEvent. 5 poll cycles:
    # long enough to avoid flicker for a connected-but-quiet device, short enough to
    # recover a dead one promptly.
    _REALTIME_LIVENESS_TTL = 300  # seconds

    def __init__(
        self,
        api: Any,
        info: dict[str, Any],
        zone: int = 0,
        *,
        catalog_repository: CommandCatalogRepository | None = None,
    ) -> None:
        if attributes := info.get("attributes"):
            info["attributes"] = {v["parName"]: v["parValue"] for v in attributes}
        self._info: dict[str, Any] = info
        self._api = api
        self._catalog_repository = (
            catalog_repository
            if catalog_repository is not None
            else CommandCatalogRepository(None, "en")
        )
        self._appliance_model: dict[str, Any] = {}
        self._commands: dict[str, HonCommand] = {}
        self._statistics: dict[str, Any] = {}
        self._attributes: dict[str, Any] = {}
        self._zone = zone
        self._additional_data: dict[str, Any] = {}
        self._command_history: list[dict[str, Any]] = []
        # HA's UTC instant of the last SUCCESSFUL read of `/history` (None: never), and
        # the outcome of the last re-read, for a dump or for a new cycle ("ok",
        # "pending" while in flight, or the exception's class name). Issues #112/#115:
        # a list read only at setup was 42 h old in a dump and silently missed the
        # starts it was opened to show.
        self._command_history_at: Optional[datetime] = None
        self._command_history_refresh: Optional[str] = None
        # Following a cycle started elsewhere (issue #112, see `_follow_new_cycle`):
        # the start the last recovery used, the last `commandHistory` start seen, the
        # last activity seen, the start announced and still awaited, the programme an
        # app start seen only through its activity is awaited by, the reads left, the
        # programme of the start the slot announced and whose activity is still to
        # come ("" when the slot named none), the read in flight, a list read and not
        # applied yet, and how many starts were followed.
        self._recovered_start: Optional[str] = None
        self._seen_slot_start: Optional[str] = None
        self._seen_activity: Any = _UNSEEN
        self._awaited_start: Optional[str] = None
        self._awaited_programme: Optional[str] = None
        self._follow_reads_left = 0
        self._announced_programme: Optional[str] = None
        self._announced_polls_left = 0
        self._follow_task: Optional[asyncio.Task] = None
        self._follow_list: Optional[list[dict[str, Any]]] = None
        self._followed_starts = 0
        # The cycle the context's activity last described (issue #130, see
        # `_watch_cycle`). Written by the context read alone, on the client loop; a push
        # only reads it.
        self._cycle_activity: Optional[CycleActivity] = None
        # The last read a push at the start or end of a cycle asked for, as the MQTT
        # transport records it (see `note_cycle_read`); None until the first.
        self._cycle_read: Optional[dict[str, Any]] = None
        # Context reads by start order: the last one started, and the last one whose
        # answer was applied (see `load_attributes`).
        self._context_reads_started = 0
        self._context_read_applied = 0
        self._command_payload: dict[str, str] = {}
        self._history_recovery: dict[str, str] = {}
        # (key, shadow value) pairs already reported as unsyncable: sync runs on every
        # poll and every MQTT push, so only the first sight of a pair is worth an INFO.
        self._unsyncable_seen: set[tuple[str, str]] = set()
        self._last_update: Optional[datetime] = None
        self._default_setting = HonParameter("", {}, "")
        self._connection = (
            not self._attributes.get("lastConnEvent", {}).get("category", "")
            == "DISCONNECTED"
        )
        # Most recent CLOUD-stamped time at which we saw realtime MQTT traffic from this
        # appliance (None until the first push). Realtime traffic is positive evidence of
        # connectivity (the hOn app trusts it): it sets `_connection` True and is compared
        # against `lastConnEvent` so a STALE REST disconnect cannot clobber a live device
        # back offline at the next 60s poll. Cloud-stamped (not wall-clock) so the
        # comparison against the cloud-stamped lastConnEvent is skew-free. See
        # mark_realtime_seen and load_attributes.
        self._last_realtime_ts: Optional[datetime] = None
        # MONOTONIC receipt time (seconds) of the last realtime message. Used ONLY for the
        # freshness bound (_REALTIME_LIVENESS_TTL): a monotonic-vs-monotonic elapsed
        # measure, immune to wall-clock jumps (NTP steps, DST transitions) that a naive
        # datetime.now() would distort. Distinct from _last_realtime_ts, which is the CLOUD
        # timestamp used for ordering against the cloud-stamped lastConnEvent.
        self._last_realtime_local: Optional[float] = None
        # What the cycles did over time, for the dump (issue #138): the followed types
        # only, one log per appliance id that a reload takes up again. The first read of
        # this object seeds it (`load_attributes`).
        self._cycle_log: Optional[_cycle_log.CycleLog] = None
        self._cycle_log_seeded = False
        if self.appliance_type in FOLLOWED_TYPES:
            try:
                self._cycle_log = _cycle_log.attach(self.unique_id)
            except Exception:  # noqa: BLE001 - the log must never cost the appliance
                _LOGGER.debug("Cycle log unavailable", exc_info=True)
        # Per-type layer (resolved via the static registry).
        self._extra = _native_appliances.get_extra(self)

    def _check_name_zone(self, name: str, frontend: bool = True) -> str:
        zone = " Z" if frontend else "_z"
        attribute: str = self._info.get(name, "")
        if attribute and self._zone:
            return f"{attribute}{zone}{self._zone}"
        return attribute

    # --- identity / metadata ---
    @property
    def appliance_model_id(self) -> str:
        return str(self._info.get("applianceModelId", ""))

    @property
    def appliance_type(self) -> str:
        return str(self._info.get("applianceTypeName", ""))

    @property
    def mac_address(self) -> str:
        return str(self.info.get("macAddress", ""))

    @property
    def unique_id(self) -> str:
        default_mac = "xx-xx-xx-xx-xx-xx"
        import_name = f"{self.appliance_type.lower()}_{self.appliance_model_id}"
        result = self._check_name_zone("macAddress", frontend=False)
        return result.replace(default_mac, import_name)

    @property
    def model_name(self) -> str:
        return self._check_name_zone("modelName")

    @property
    def brand(self) -> str:
        brand = self._check_name_zone("brand")
        return brand[0].upper() + brand[1:] if brand else brand

    @property
    def nick_name(self) -> str:
        result = self._check_name_zone("nickName")
        if not result or re.findall("^[xX1\\s-]+$", result):
            return self.model_name
        return result

    @property
    def code(self) -> str:
        code: str = self.info.get("code", "")
        if code:
            return code
        serial_number: str = self.info.get("serialNumber", "")
        return serial_number[:8] if len(serial_number) < 18 else serial_number[:11]

    @property
    def model_id(self) -> int:
        # The `0` default only covers a MISSING key; a present-but-empty or
        # non-numeric `applianceModelId` (seen on some payloads) would make
        # int("") raise ValueError. model_id feeds the entity identity, so parse
        # defensively and fall back to 0.
        try:
            return int(self._info.get("applianceModelId") or 0)
        except (TypeError, ValueError):
            return 0

    # --- state / data ---
    @property
    def options(self) -> dict[str, Any]:
        return dict(self._appliance_model.get("options", {}))

    @property
    def model_attributes(self) -> dict[str, Any]:
        """Per-MODEL metadata from `applianceModel.attributes` (cloud catalogue).

        Distinct from `attributes`, which is the device SHADOW (live telemetry):
        this describes what the MODEL is, not what it is currently doing:
        `zones`, `vtRoom1`/`vtRoom2`, `seriesVersion`, `doorNumber`, ... The hOn
        app treats these as authoritative where the shadow is not: it decides
        which fridge zones exist from `zones`.split("|"), never from which
        `tempZ*`/`tempSel*` keys the shadow happens to carry.

        The cloud sends a LIST of `{parName, parValue, ...}` rows; flatten it to
        parName -> parValue, the same normalisation __init__ applies to the
        appliance-level `attributes`. Some payloads send a mapping already;
        accept both and never raise on a malformed row.
        """
        raw = self._appliance_model.get("attributes")
        if isinstance(raw, Mapping):
            return {str(key): value for key, value in raw.items()}
        if isinstance(raw, list):
            return {
                str(row["parName"]): row.get("parValue")
                for row in raw
                if isinstance(row, Mapping) and row.get("parName")
            }
        return {}

    @property
    def commands(self) -> dict[str, HonCommand]:
        return self._commands

    @property
    def attributes(self) -> dict[str, Any]:
        return self._attributes

    @property
    def statistics(self) -> dict[str, Any]:
        return self._statistics

    @property
    def info(self) -> dict[str, Any]:
        return self._info

    @property
    def additional_data(self) -> dict[str, Any]:
        return self._additional_data

    @property
    def command_payload(self) -> dict[str, str]:
        """{top-level key of the commands payload: what became of it}; see
        `CommandHydration.command_payload`. Read by the diagnostics dump only."""
        return self._command_payload

    @property
    def command_history(self) -> list[dict[str, Any]]:
        """The cloud's `/history` list from the last catalog load, verbatim.

        Read by the diagnostics dump only. Refreshed when the catalog is loaded (setup
        or reload) and by `refresh_command_history` (a dump, a new cycle), not on every
        poll.
        """
        return self._command_history

    @property
    def command_history_at(self) -> Optional[datetime]:
        """HA's UTC instant of the last successful `/history` read, or None."""
        return self._command_history_at

    @property
    def command_history_refresh(self) -> Optional[str]:
        """Outcome of the last `refresh_command_history`: None if never run."""
        return self._command_history_refresh

    async def refresh_command_history(self) -> Optional[list[dict[str, Any]]]:
        """Read `/history` again (for a dump, or for a new cycle). Never raises.

        A failure keeps the list and its instant and names the exception's class, so a
        dump can tell a fresh list from an old one it could not replace. Cancellation
        still propagates. Returns the list read, None on a failure. The recovery is not
        re-run here: only a new cycle re-runs it (`_follow_new_cycle`), a dump never.
        """
        self._command_history_refresh = "pending"
        try:
            history = await self.api.load_command_history(self)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a dump must degrade, never raise
            self._command_history_refresh = type(error).__name__
            return None
        if not isinstance(history, list):
            self._command_history_refresh = "invalid"
            return None
        self._command_history = history
        self._command_history_at = datetime.now(timezone.utc)
        self._command_history_refresh = "ok"
        return history

    @property
    def cycle_activity(self) -> Optional[CycleActivity]:
        """The cycle the context's activity last described, or None (issue #130).

        Read by the programme name (`ApplianceExtra.attributes`) on every poll and
        push; see `_watch_cycle` for when it is set and dropped.
        """
        return self._cycle_activity

    def _watch_cycle(self, activity: Any) -> None:
        """Keep `cycle_activity` in step with the context just read (issue #130).

        Lajahome, 2026-10-16: the app had last started Denim (prCode 115), Cottons
        (also 115) was started on the panel, and Home Assistant named Denim for the
        whole cycle, though the activity said `HQD_COTTONS`. A panel start never
        reaches `/history` (T1), so only the activity can name it. On every read of
        the context:

        - an open activity read while the shadow's `machMode` says a cycle runs, as
          the app requires (decision 6), is the cycle: it replaces the one kept;
        - otherwise the cycle kept stays while the shadow keeps its code, so the
          programme that ran is still named when the cycle ends and the activity
          empties (decision S1), and is dropped once the shadow has left the code (the
          next programme, prCode 0, the machine's resting code: 136 on Lajahome's 3
          idle dumps out of 3).

        The cycle taken carries the start's options and the activity's prCode too
        (issue #138, `cycle_option`); they change nothing above.

        Runs on the client loop only, from `load_attributes`, before the name is
        derived. Nothing else writes `_cycle_activity`.
        """
        if activity is _UNREAD:
            return
        activity = activity if isinstance(activity, dict) else {}
        details = activity.get("attributes")
        details = details if isinstance(details, dict) else {}
        code = self._live_int("prCode")
        # The same reading of "open" as the check a cycle's start read is settled by
        # (`cycle_edge_settled`): what is kept and what is compared are one thing
        # (review of PR #139, S2).
        started = _open_started(activity)
        modes = cycle_modes(self.appliance_type) or _CYCLE_MODES
        # An activity that began before the cycle kept is an answer older than the one
        # that brought it, whatever read it came with: it says nothing on what runs
        # (review of PR #139, S1).
        if (
            started is not None
            and self._live_int("machMode") in modes
            and not self._older_than_kept(started)
        ):
            self._cycle_activity = CycleActivity(
                programme=details.get("programName"),
                # The app reads it on the dishwasher alone (`mapLastActivity`).
                functional_id=(
                    details.get("functionalId") if self.appliance_type == "DW" else None
                ),
                channel=details.get("channel"),
                label=details.get("prStrDisp"),
                code=code,
                started=started,
                options=self._start_options(details),
                activity_code=_as_int(details.get("prCode")),
            )
            return
        # No open activity to take (none, ended, read at rest, or older than the cycle
        # kept): the cycle kept stays while the shadow keeps its code (decision S1 of
        # 2026-10-25) and goes once it has left it. The previous cycle's activity is not
        # dropped at a new start: the start read settles only on another one (S2).
        if self._cycle_activity is not None and self._cycle_activity.code != code:
            self._cycle_activity = None

    def _start_options(self, details: Mapping[str, Any]) -> Mapping[str, str]:
        """The activity's values of the start's parameters, as text (issue #138).

        The activity carries the start's parameters next to what the app adds
        (`principalUserId`, `deviceModel`, `prStrDisp`): only the keys of the
        `startProgram` command are kept, and only the scalar values, as the text the
        shadow holds (a number arrives as `19` or `"19"`). A schema that declares the
        start's title (`prStr`, a favourite's name) or another key of who started it
        still gives none of them (`cycle_log.IDENTITY_KEYS`). Read-only: a push reads it.
        """
        command = self._commands.get("startProgram")
        keys = set(getattr(command, "setting_keys", None) or ())
        return MappingProxyType({
            key: str(value)
            for key, value in details.items()
            if key in keys
            and not _cycle_log.is_identity_key(key)
            and isinstance(value, (str, int, float))
            and not isinstance(value, bool)
        })

    def cycle_option(self, key: str) -> Optional[str]:
        """What the running cycle was started with for `key`, or None (issue #138).

        The shadow of a washer never reports `permanentPressStatus` (Keep Fresh) or
        `stainType` during a cycle; the app shows a running cycle's options from its
        activity, and only while `machMode` is 2, 3 or 5 (`slot143`, apk2 decomp.txt
        @2588626-2588744; the running card, @2591986-2592000). The value answers only
        when all of these hold, else None:

        - a cycle is kept, and was not started on the panel (`channel` `appliance`,
          whose activity is a picture of the shadow, not what was chosen);
        - the context last read still has that activity open (decision C1): a cycle
          kept after its end for its name (S1) would otherwise lend its options to
          the next cycle on the same code, before the read that brings its activity;
        - the kept cycle's code, the activity's prCode and the shadow's prCode now
          are one, as the app's card checks the activity's (@2591727-2591733);
        - the shadow's `machMode` now is in `cycle_modes` (decision D4).

        Reads the shadow and the kept cycle only: no I/O, nothing written.
        """
        cycle = self._cycle_activity
        if cycle is None or cycle.channel == "appliance":
            return None
        started = self.activity_started
        if started is None or started != cycle.started:
            return None
        code = self._live_int("prCode")
        if code is None or cycle.code != code or cycle.activity_code != code:
            return None
        modes = cycle_modes(self.appliance_type)
        if modes is None or self._live_int("machMode") not in modes:
            return None
        return cycle.options.get(key)

    def _older_than_kept(self, started: Any) -> bool:
        """Whether an activity that began at `started` began before the cycle kept.

        Both are the cloud's own `activityStarted`, so one clock; when either cannot be
        read the activity is taken, as before.
        """
        kept = self._cycle_activity
        if kept is None:
            return False
        new, old = parse_cloud_timestamp(started), parse_cloud_timestamp(kept.started)
        return new is not None and old is not None and new < old

    def note_cycle_read(self, record: dict[str, Any]) -> None:
        """Keep the record of the read a push just asked for (issue #130, decision 9).

        The MQTT transport hands the record over when it asks for the read and fills
        it in as the reads run: the edge, when it was asked, and each read's instant
        and outcome. Read by the diagnostics dump only.
        """
        self._cycle_read = record
        if self._cycle_log is not None:
            try:
                self._cycle_log.cycle_read(record)
            except Exception:  # noqa: BLE001 - the log must never cost the read
                _LOGGER.debug("Cycle log failed", exc_info=True)

    @property
    def cycle_log(self) -> Optional[_cycle_log.CycleLog]:
        """The log of the cycles for the dump (issue #138); None for the types whose
        cycles are not followed."""
        return self._cycle_log

    @property
    def cycle_read(self) -> Optional[dict[str, Any]]:
        """The record `note_cycle_read` keeps, None before the first read asked."""
        return self._cycle_read

    @property
    def mach_mode(self) -> Optional[int]:
        """The shadow's `machMode` as an int, None if absent or unreadable."""
        return self._live_int("machMode")

    def cycle_edge(self, before: Optional[int]) -> Optional[str]:
        """"start" or "end" when `machMode` moved from `before` into or out of a cycle.

        Called by the MQTT transport after a push applied its parameters (issue #130,
        decision C1): the start of a cycle, or its end, is when the context's activity
        changes, and a push never carries the activity. None for any other move, a
        push that left `machMode` alone, and the types whose starts are not followed.
        """
        if self.appliance_type not in self._FOLLOWED_TYPES:
            return None
        after = self.mach_mode
        if after is None or after == before:
            return None
        running = cycle_modes(self.appliance_type) or _CYCLE_MODES
        if after in running and after != _PAUSE and before not in running:
            return "start"
        if after in _EDGE_END and before in running:
            return "end"
        return None

    @property
    def activity_started(self) -> Any:
        """`activityStarted` of the open activity the context last read, else None."""
        return _open_started(self._attributes.get("activity"))

    def cycle_edge_settled(self, edge: str, before: Any = None) -> bool:
        """Whether the context last read already shows `edge` (decision C3: otherwise
        it is read once more): at the start an open activity other than `before`, the
        one open when the push came; at the end none. An activity the cloud has not
        replaced yet is the previous cycle's, not this one (review of PR #139, S2)."""
        started = self.activity_started
        if edge == "start":
            return started is not None and started != before
        return started is None

    def _live_int(self, key: str) -> Optional[int]:
        """A shadow parameter as an int, None if absent or unreadable."""
        attribute = self._attributes.get("parameters", {}).get(key)
        if attribute is None:
            return None
        try:
            return int(float(str(attribute)))
        except (TypeError, ValueError, OverflowError):
            # OverflowError: an infinite value ("inf"), unreadable like a NaN (review of
            # PR #139, S5).
            return None

    @property
    def followed_starts(self) -> int:
        """How many starts made elsewhere were followed since the catalog load.

        The integration drops the programme and options chosen in Home Assistant and
        not started when it moves (issue #112, decision D2 of 2026-10-06).
        """
        return self._followed_starts

    def _follow_new_cycle(self) -> None:
        """Follow a cycle started elsewhere: the app, another phone, the panel.

        Issue #112: `/history` was read only at catalog load, so a programme the app
        started later showed as the base programme of its shared prCode, with the
        options of the start read at setup, until a reload. Decisions of 2026-10-06
        (`apk2/analysis/issue112-wm/7-dump-infy1995-beta10.md` section 4), as the app
        does on its `TECHNICAL_ACTIVITY_ID` push (same folder,
        `10-app-selezione-e-history.md` section 3):

        - a new cycle is a new `startProgram` in the context's `commandHistory` slot
          (pause, stop and a slot the cloud nulled are not) or a new `activityStarted`;
        - it is followed by reading `/history` in the background and, at the next
          poll, recovering the start it lists, options included;
        - one read per cycle whichever signals fire, up to three when a read fails or
          lacks the start the slot announced;
        - a list with no newer start (a panel start never reaches it) changes nothing.

        Review of the 5.26.0 release PR (#127), decisions of 2026-10-14: an activity
        names its programme and channel (`mobileApp` from the app, `appliance` from
        the panel), so the activity of a start the slot announced is recognised by its
        programme (S1); an app start seen only through its activity waits, up to three
        reads, for a row of that programme and takes no other (S3, S4); a panel start
        reads nothing; every new start gets three reads of its own (S6). An activity
        that names no programme starts no follow (S4), an announcement whose activity
        does not come within three polls expires (S1), and a start of ours is
        recovered but not counted in `followed_starts` (S9).

        Runs once per poll, on the client loop, after the context was read; the list
        read is applied by `_apply_followed_list` at the start of the next poll, before
        the context, so the programme name derived from it already sees the recovery.
        """
        self._watch_command_slot()
        self._watch_activity()
        self._read_for_new_cycle()

    def _apply_followed_list(self) -> None:
        history, self._follow_list = self._follow_list, None
        if history is None:
            return
        listed = start_identity(history)
        if (
            listed is not None
            and listed != self._recovered_start
            and self._is_awaited_row(history)
        ):
            how = HonCommandLoader(
                self._api, self, self._catalog_repository
            ).recover_start(self._commands, history)
            self._recovered_start = listed
            if how is not None:
                self._history_recovery = {**self._history_recovery, "startProgram": how}
            newest = _newest_start(history)
            device = newest.get("device")
            if not (isinstance(device, dict) and device.get("deviceModel") == "addhon"):
                # Our own start consumed the choices made in HA already (S9).
                self._followed_starts += 1
            if _programme_key(newest.get("programName")) == (
                self._awaited_programme
            ):
                self._awaited_programme = None
        # A start the slot announced, or an app start its activity named, that this
        # list does not hold yet (the cloud is behind, or a second start came while
        # the read was in flight) keeps the reads going; anything else ends the
        # follow, a list with no newer start included.
        if (
            self._awaited_start is None or self._awaited_start == self._recovered_start
        ) and self._awaited_programme is None:
            self._end_follow()

    def _is_awaited_row(self, history: list[dict[str, Any]]) -> bool:
        """Whether the newest start of `history` may be applied (S4).

        An app start known only through its activity has no identity, only a
        programme: a newer row of another programme is not its row. A start the slot
        announced is awaited by identity, and any newer row is applied on the way.
        """
        if self._awaited_programme is None or self._awaited_start not in (
            None,
            self._recovered_start,
        ):
            return True
        row = _programme_key(_newest_start(history).get("programName"))
        return row == self._awaited_programme

    def _watch_command_slot(self) -> None:
        slot = self._attributes.get("commandHistory")
        command = slot.get("command") if isinstance(slot, dict) else None
        if not isinstance(command, dict) or command.get("commandName") != "startProgram":
            return
        start = command_identity(command)
        if start is None or start == self._seen_slot_start:
            return
        self._seen_slot_start = start
        if start != self._recovered_start:
            self._awaited_start = start
            self._announced_programme = _programme_key(command.get("programName")) or ""
            self._announced_polls_left = self._FOLLOW_ATTEMPTS
            self._arm_follow()

    def _watch_activity(self) -> None:
        activity = self._attributes.get("activity")
        started = activity.get("activityStarted") if isinstance(activity, dict) else None
        if self._seen_activity is _UNSEEN:
            self._seen_activity = started or None
            return
        if not started or started == self._seen_activity:
            if self._announced_programme is not None:
                # No activity for the announced start within three polls: it had no
                # cycle behind it, and must not hide the next one (S1).
                self._announced_polls_left -= 1
                if self._announced_polls_left <= 0:
                    self._announced_programme = None
            return
        self._seen_activity = started
        details = activity.get("attributes") if isinstance(activity, dict) else None
        details = details if isinstance(details, dict) else {}
        programme = _programme_key(details.get("programName"))
        announced, self._announced_programme = self._announced_programme, None
        if announced is not None and (
            not announced or programme is None or programme == announced
        ):
            # The slot already announced this cycle's start: one read is enough (S1:
            # only when the activity names that programme, or names none).
            return
        if details.get("channel") == "appliance" or programme is None:
            # Started on the panel: such a start never reaches /history. An activity
            # naming no programme cannot tell its row from another (S4).
            return
        self._awaited_programme = programme
        self._arm_follow()

    def _arm_follow(self) -> None:
        # A new start gets its own reads, whatever an earlier one left (S6).
        self._follow_reads_left = self._FOLLOW_ATTEMPTS

    def _end_follow(self) -> None:
        self._follow_reads_left = 0
        self._awaited_start = None
        self._awaited_programme = None

    def _read_for_new_cycle(self) -> None:
        if self._follow_reads_left == 0:
            self._awaited_start = None
            self._awaited_programme = None
            return
        if self._follow_task is not None and not self._follow_task.done():
            return
        self._follow_reads_left -= 1
        self._follow_task = asyncio.get_running_loop().create_task(self._read_followed())

    async def _read_followed(self) -> None:
        history = await self.refresh_command_history()
        if history is not None:
            self._follow_list = history

    @property
    def history_recovery(self) -> dict[str, str]:
        """{command name: how the last recovery chose the category it restored}.

        The catalog load fills it; a followed start (`_follow_new_cycle`) updates the
        `startProgram` row.

        See `CommandHydration.history_recovery`. Read by the diagnostics dump only.
        """
        return self._history_recovery

    @property
    def zone(self) -> int:
        return self._zone

    @property
    def api(self) -> Any:
        if self._api is None:
            raise NoAuthenticationException("Missing hOn login")
        return self._api

    @property
    def connection(self) -> bool:
        return self._connection

    @connection.setter
    def connection(self, connection: bool) -> None:
        self._connection = connection

    def mark_realtime_seen(self, timestamp: Any = None) -> None:
        """Record positive realtime evidence: this appliance just sent MQTT traffic.

        Called from the MQTT transport for any thing-scoped realtime message. It is
        POSITIVE-ONLY: it sets `connection` True and remembers WHEN (the cloud-stamped
        message `timestamp`, not local wall-clock, to stay comparable with
        `lastConnEvent` and skew-free). It NEVER sets False -- absence of traffic stays
        the job of the REST lastConnEvent / disconnect events / watchdog.

        Defensive (runs on the awscrt callback thread, must never raise): a
        missing/garbage/None timestamp is tolerated -- we still mark connected (the
        message itself is the evidence) but, lacking a usable time, do NOT advance
        `_last_realtime_ts`, so reconciliation falls back to the REST-only behavior for
        that appliance rather than asserting a bogus ordering. The recorded time only
        ever moves FORWARD (max), so an out-of-order older message cannot rewind it.

        `available` is mirrored onto the cached attribute dict (not just `_connection`)
        because the entities read the RAW `available` key: the connectivity
        binary_sensor (attr_key="available") and the availability gate
        (base_entity: `_attributes.get("available")`). `attributes` returns the cached
        dict WITHOUT recomputing from `connection`, so without this the sensor would
        only flip at the next 60s poll instead of the instant the MQTT proof arrives.
        """
        self._connection = True
        self._attributes["available"] = True
        # Monotonic receipt time for the freshness bound (see load_attributes). Recorded
        # unconditionally -- even a timestamp-less message proves the appliance is live
        # NOW -- and only ever moves forward.
        local_now = monotonic()
        if self._last_realtime_local is None or local_now > self._last_realtime_local:
            self._last_realtime_local = local_now
        ts = parse_cloud_timestamp(timestamp)
        if ts is not None and (
            self._last_realtime_ts is None or ts > self._last_realtime_ts
        ):
            self._last_realtime_ts = ts

    def mark_realtime_disconnected(self) -> None:
        """Record EXPLICIT negative realtime evidence (MQTT `disconnected` / presence).

        An explicit disconnect is authoritative: the appliance is offline NOW. Beyond
        setting `connection`/`available` False, it CLEARS the realtime liveness marks so a
        subsequent STALE REST `lastConnEvent=DISCONNECTED` (whose timestamp may predate
        the last realtime traffic) cannot resurrect the appliance at the next poll. Like
        mark_realtime_seen it mirrors onto the cached `available` attribute so the
        connectivity binary_sensor reflects the disconnect immediately, not a poll later.
        """
        self._connection = False
        self._attributes["available"] = False
        self._last_realtime_ts = None
        self._last_realtime_local = None

    # --- loading ---
    async def load_commands(self) -> None:
        hydration = await HonCommandLoader(
            self.api, self, self._catalog_repository
        ).load_commands()
        self._commands = hydration.commands
        self._additional_data = hydration.additional_data
        self._command_history = hydration.command_history
        # A failed history leaves the list empty: an earlier load's instant would
        # date a list it never read (PR #121 review).
        self._command_history_at = (
            datetime.now(timezone.utc)
            if hydration.history_outcome in ("ok", "empty")
            else None
        )
        self._history_recovery = hydration.history_recovery
        # The start this list describes is the one a new cycle is compared with.
        self._recovered_start = start_identity(hydration.command_history)
        self._command_payload = hydration.command_payload
        self._appliance_model = hydration.appliance_model
        self.sync_params_to_command("settings")

    async def load_attributes(self, *, reason: str = "load") -> bool:
        # Two reads of the context can overlap -- the poll, a read a cycle edge asked
        # for, the refresh after a command -- and answer out of order. An answer older
        # than one already applied is dropped whole, and a parameter a push delivered
        # after this read started keeps the pushed value: the answer was taken before it
        # (review of PR #139, S1). Every read runs on the client loop, so the counters
        # need no lock. `reason` names the read in the cycle log (issue #138): "load"
        # at setup, "poll", or "cycle_read" for the one a cycle's edge asked for.
        self._context_reads_started += 1
        order = self._context_reads_started
        asked = datetime.now(timezone.utc)
        attributes = await self.api.load_attributes(self)
        log = self._cycle_log
        if order < self._context_read_applied:
            _LOGGER.debug("Context read answered after a newer one: dropped")
            if log is not None:
                try:
                    log.context(
                        reason=reason, order=order, asked=asked.timestamp(), applied=False
                    )
                except Exception:  # noqa: BLE001 - the log must never cost the read
                    _LOGGER.debug("Cycle log failed", exc_info=True)
            return False
        self._context_read_applied = order
        activity = attributes.get("activity", _UNREAD)
        rows: list[_cycle_log.ShadowRow] = []
        for name, values in attributes.pop("shadow", {}).get("parameters", {}).items():
            if name in self._attributes.get("parameters", {}):
                held = self._attributes["parameters"][name]
                if _pushed_since(held, asked):
                    continue
                if log is None:
                    held.update(values)
                    continue
                before, stamped = str(held), held.last_update
                applied = held.update(values)
                offered = values.get("parNewVal") if isinstance(values, dict) else values
                rows.append(_cycle_log.ShadowRow(
                    name, before, str(held) if applied else offered, applied,
                    # Only when `lastUpdate` moved: the value a push brought, read again.
                    stamp=(
                        values.get("lastUpdate")
                        if isinstance(values, dict) and held.last_update != stamped
                        else None
                    ),
                ))
            else:
                self._attributes.setdefault("parameters", {})[name] = HonAttribute(values)
        self._attributes |= attributes
        # Authoritative connectivity = lastConnEvent.category (app model
        # ApplianceConnectionState, see apk/analysis/per-type-derivations.md #5).
        # Updating `_connection` here on every poll keeps the per-type layers that zero
        # based on `self.connection` (td/wd/dw/ov) accurate and consistent with wm (which
        # reads lastConnEvent). Validated live: an offline TD showed a stale machMode=1,
        # now 0 like WM. Does not overwrite a fresher MQTT state if lastConnEvent is missing.
        lce = self._attributes.get("lastConnEvent")
        # Only the authoritative `category` updates connectivity. If lastConnEvent is
        # absent, malformed, or a dict without `category`, keep the (MQTT-derived) state
        # rather than forcing it True.
        if isinstance(lce, dict) and "category" in lce:
            if lce["category"] != "DISCONNECTED":
                # A non-DISCONNECTED (e.g. CONNECTED) REST event is itself positive
                # cloud evidence -> connected, unchanged behavior.
                self._connection = True
            else:
                # DISCONNECTED from REST. Realtime MQTT traffic is also authoritative
                # connectivity evidence, so a STALE disconnect must NOT clobber a device
                # we KNOW is live. We keep it online only when the realtime evidence is
                # BOTH:
                #   * NEWER than this disconnect event (cloud-vs-cloud ordering, so a
                #     genuinely later disconnect wins -> self-correcting); AND
                #   * RECENT in wall-clock terms (within _REALTIME_LIVENESS_TTL), so a
                #     once-seen realtime time cannot pin a now-silent appliance online
                #     forever while the cloud's lastConnEvent stays frozen in the past
                #     (the exact failure the washer's frozen 13:35 disconnect would cause
                #     after the device stops talking). Past the window we defer to REST.
                # If either timestamp is missing/unparseable we cannot order -> honor the
                # DISCONNECTED (prior REST-only behavior). Prefer epoch-ms `timestampEvent`;
                # fall back to ISO `instantTime` when absent OR explicitly null.
                disconnect_ts = parse_cloud_timestamp(lce.get("timestampEvent"))
                if disconnect_ts is None:
                    disconnect_ts = parse_cloud_timestamp(lce.get("instantTime"))
                realtime_ts = self._last_realtime_ts
                realtime_newer = (
                    realtime_ts is not None
                    and disconnect_ts is not None
                    and realtime_ts > disconnect_ts
                )
                realtime_fresh = self._last_realtime_local is not None and (
                    monotonic() - self._last_realtime_local
                    < self._REALTIME_LIVENESS_TTL
                )
                # Newer-than-disconnect AND still fresh -> trust realtime; else offline.
                self._connection = realtime_newer and realtime_fresh
        # `available` UNIVERSAL (even for types without a per-type layer, e.g. AC):
        # connectivity is first-class as in the app. The per-type layer re-sets it (same value).
        self._attributes["available"] = self._connection
        if self.appliance_type in self._FOLLOWED_TYPES:
            # Before the derivation, so the name of this read already sees it.
            self._watch_cycle(activity)
        self.derive_attributes()
        # A command the cloud accepted but the device never applied shows up only
        # here, in the next cloud read (issue #115, O4). Whoever asked for the read
        # (refresh after the command, poll, fallback) it is judged once, and it only
        # logs. observe_shadow_read is failure-safe itself; the guard is mqtt.py's.
        try:
            observe_shadow_read(self)
        except Exception:
            _LOGGER.debug("Delivery check failed", exc_info=True)
        if log is not None:
            self._log_read(log, rows, reason, order, asked, activity)
        return True

    def _log_read(
        self,
        log: _cycle_log.CycleLog,
        rows: list[_cycle_log.ShadowRow],
        reason: str,
        order: int,
        asked: datetime,
        activity: Any,
    ) -> None:
        """An applied read of the context in the cycle log (issue #138). Never raises.

        The first read of this object seeds the log with the whole shadow instead of
        rows: a log a reload took up again logs only what moved meanwhile.
        """
        try:
            if self._cycle_log_seeded:
                log.shadow("rest", rows)
            else:
                self._cycle_log_seeded = True
                log.seed({
                    name: str(value)
                    for name, value in self._attributes.get("parameters", {}).items()
                })
            log.context(
                reason=reason,
                order=order,
                asked=asked.timestamp(),
                applied=True,
                summary=_cycle_log.context_summary(
                    self._attributes, None if activity is _UNREAD else activity
                ),
            )
        except Exception:  # noqa: BLE001 - the log must never cost the read
            _LOGGER.debug("Cycle log failed", exc_info=True)

    def derive_attributes(self) -> None:
        """Re-run the per-type derivations (programName, active, pause, available).

        They are read from the shadow parameters, so whatever changes those parameters
        calls this: `load_attributes` after a poll, the MQTT transport after a push.
        Without the second, a pushed prCode kept the previous programme's name until
        the next poll. Pure: no I/O, reads only the attributes already held.
        """
        if self._extra:
            self._attributes = self._extra.attributes(self._attributes)

    async def load_statistics(self) -> None:
        self._statistics = await self.api.load_statistics(self)
        self._statistics |= await self.api.load_maintenance(self)

    async def update(self, force: bool = False) -> Optional[bool]:
        """Read the context, unless the last read is under 5 s old and not `force`d.

        Returns whether the answer was applied (False: a newer read's answer already
        was, see `load_attributes`), None when nothing was read.
        """
        now = datetime.now()
        min_age = now - timedelta(seconds=self._MINIMAL_UPDATE_INTERVAL)
        if not (force or not self._last_update or self._last_update < min_age):
            return None
        self._last_update = now
        follow = self.appliance_type in self._FOLLOWED_TYPES
        if follow:
            self._apply_followed_list()
        applied = await self.load_attributes(reason="cycle_read" if force else "poll")
        self.sync_params_to_command("settings")
        if follow:
            self._follow_new_cycle()
        return applied

    # --- derived views ---
    @property
    def command_parameters(self) -> dict[str, dict[str, str | float]]:
        return {n: c.parameter_value for n, c in self._commands.items()}

    @property
    def settings(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, command in self._commands.items():
            for key in command.setting_keys:
                setting = command.settings.get(key, self._default_setting)
                result[f"{name}.{key}"] = setting
        if self._extra:
            return self._extra.settings(result)
        return result

    @property
    def available_settings(self) -> list[str]:
        result = []
        for name, command in self._commands.items():
            for key in command.setting_keys:
                result.append(f"{name}.{key}")
        return result

    @property
    def data(self) -> dict[str, Any]:
        return {
            "attributes": self.attributes,
            "appliance": self.info,
            "statistics": self.statistics,
            "additional_data": self._additional_data,
            **self.command_parameters,
            **self.attributes,
        }

    # --- sync attributes <-> commands ---
    def sync_command_to_params(self, command_name: str) -> None:
        if not (command := self.commands.get(command_name)):
            return
        for key in self.attributes.get("parameters", {}):
            if new := command.parameters.get(key):
                self.attributes["parameters"][key].update(
                    str(new.intern_value), shield=True
                )

    def sync_payload_to_params(
        self, params: Mapping[str, str | float]
    ) -> None:
        shadow = self.attributes.get("parameters", {})
        if not isinstance(shadow, dict):
            return
        # getattr: the air purifier's contract tests borrow this method for a double.
        log = getattr(self, "_cycle_log", None)
        rows: list[_cycle_log.ShadowRow] = []
        for key, value in params.items():
            current = shadow.get(key)
            if current is not None and hasattr(current, "update"):
                before = str(current) if log is not None else None
                current.update(str(value), shield=True)
                if log is not None:
                    rows.append(_cycle_log.ShadowRow(key, before, str(current), True))
        if rows:
            # Our own write after an accepted command, in the cycle log (issue #138).
            try:
                log.shadow("local", rows)
            except Exception:  # noqa: BLE001 - the log must never cost the write
                _LOGGER.debug("Cycle log failed", exc_info=True)

    def sync_params_to_command(self, command_name: str) -> None:
        if not (command := self.commands.get(command_name)):
            return
        for key in command.setting_keys:
            if (
                new := self.attributes.get("parameters", {}).get(key)
            ) is None or new.value == "":
                continue
            setting = command.settings.get(key)
            if setting is None:
                continue
            raw = str(new.value)
            try:
                # Always assign as a STRING (range params included): the range
                # setter runs str_to_float, which tries int() first, so a float
                # like 22.5 would be TRUNCATED to 22 without error (see
                # helpers.str_to_float, and the same note in number.py /
                # rules.py._apply_fixed). A string preserves the decimals, so a
                # half-degree setpoint is not silently rounded when synced into
                # the command and later re-sent to the cloud.
                setting.value = raw
            except ValueError as error:
                # The device shadow can report an OFF-GRID value for a range setpoint
                # (a measured 17.2 for a step-1 target). Skipping would leave the command
                # at its load-time default (min), which a later FULL-command send writes
                # back to the device -- clobbering the real setpoint (both wine-cooler
                # zones snapped to 5C when the light switch fired, discussion #62). Snap
                # the shadow value onto the grid so the command carries the true setpoint.
                snap = getattr(setting, "snap_to_grid", None)
                if callable(snap):
                    try:
                        setting.value = snap(raw)
                        continue
                    except ValueError:
                        pass
                pair = (key, raw)
                level = logging.DEBUG if pair in self._unsyncable_seen else logging.INFO
                self._unsyncable_seen.add(pair)
                _LOGGER.log(level, "Can't sync %s from shadow %r - %s", key, raw, error)
                continue
