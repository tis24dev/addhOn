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
from typing import Any, Optional

from ...command_diagnostics import observe_shadow_read
from ..catalog_repository import CommandCatalogRepository
from ..helpers import parse_cloud_timestamp
from .appliances import registry as _native_appliances
from .attributes import HonAttribute
from .command_loader import HonCommandLoader, command_identity, start_identity
from .commands import HonCommand
from .exceptions import NoAuthenticationException
from .parameter.base import HonParameter

_LOGGER = logging.getLogger(__name__)


# Marks an activity not looked at yet: the first poll after a load records the cycle
# already running, which the `/history` just read describes (or cannot describe).
_UNSEEN = object()


class HonAppliance:
    _MINIMAL_UPDATE_INTERVAL = 5  # seconds
    # Types whose start is followed when a cycle begins elsewhere (issue #112, decision
    # D4 of 2026-10-06): the four the app's dashboard shows a running programme for.
    _FOLLOWED_TYPES = frozenset({"WM", "WD", "TD", "DW"})
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
        # last activity seen, the start announced and still awaited, the reads left,
        # whether the cycle's start is already handled, the read in flight, a list
        # read and not applied yet, and how many starts were followed.
        self._recovered_start: Optional[str] = None
        self._seen_slot_start: Optional[str] = None
        self._seen_activity: Any = _UNSEEN
        self._awaited_start: Optional[str] = None
        self._follow_reads_left = 0
        self._cycle_start_handled = False
        self._follow_task: Optional[asyncio.Task] = None
        self._follow_list: Optional[list[dict[str, Any]]] = None
        self._followed_starts = 0
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
        if listed is not None and listed != self._recovered_start:
            how = HonCommandLoader(
                self._api, self, self._catalog_repository
            ).recover_start(self._commands, history)
            self._recovered_start = listed
            if how is not None:
                self._history_recovery = {**self._history_recovery, "startProgram": how}
            self._followed_starts += 1
        # A start the slot announced and this list does not hold yet (the cloud is
        # behind, or a second start came while the read was in flight) keeps the reads
        # going; anything else ends the follow, a list with no newer start included.
        if self._awaited_start is None or self._awaited_start == self._recovered_start:
            self._end_follow()

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
            self._cycle_start_handled = True
            self._arm_follow()

    def _watch_activity(self) -> None:
        activity = self._attributes.get("activity")
        started = activity.get("activityStarted") if isinstance(activity, dict) else None
        if self._seen_activity is _UNSEEN:
            self._seen_activity = started or None
            return
        if not started or started == self._seen_activity:
            return
        self._seen_activity = started
        if self._cycle_start_handled:
            # The slot already announced this cycle's start: one read is enough.
            self._cycle_start_handled = False
            return
        self._arm_follow()

    def _arm_follow(self) -> None:
        if self._follow_reads_left == 0:
            self._follow_reads_left = self._FOLLOW_ATTEMPTS

    def _end_follow(self) -> None:
        self._follow_reads_left = 0
        self._awaited_start = None

    def _read_for_new_cycle(self) -> None:
        if self._follow_reads_left == 0:
            self._awaited_start = None
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

    async def load_attributes(self) -> None:
        attributes = await self.api.load_attributes(self)
        for name, values in attributes.pop("shadow", {}).get("parameters", {}).items():
            if name in self._attributes.get("parameters", {}):
                self._attributes["parameters"][name].update(values)
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
        if self._extra:
            self._attributes = self._extra.attributes(self._attributes)
        # A command the cloud accepted but the device never applied shows up only
        # here, in the next cloud read (issue #115, O4). Whoever asked for the read
        # (refresh after the command, poll, fallback) it is judged once, and it only
        # logs. observe_shadow_read is failure-safe itself; the guard is mqtt.py's.
        try:
            observe_shadow_read(self)
        except Exception:
            _LOGGER.debug("Delivery check failed", exc_info=True)

    async def load_statistics(self) -> None:
        self._statistics = await self.api.load_statistics(self)
        self._statistics |= await self.api.load_maintenance(self)

    async def update(self, force: bool = False) -> None:
        now = datetime.now()
        min_age = now - timedelta(seconds=self._MINIMAL_UPDATE_INTERVAL)
        if force or not self._last_update or self._last_update < min_age:
            self._last_update = now
            follow = self.appliance_type in self._FOLLOWED_TYPES
            if follow:
                self._apply_followed_list()
            await self.load_attributes()
            self.sync_params_to_command("settings")
            if follow:
                self._follow_new_cycle()

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
        for key, value in params.items():
            current = shadow.get(key)
            if current is not None and hasattr(current, "update"):
                current.update(str(value), shield=True)

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
