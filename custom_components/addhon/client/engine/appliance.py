"""Native ROOT HonAppliance.

Puts together our whole engine: native attributes (engine.attributes), native loader/
commands/rules/program (engine.command_loader), native per-type layer
(engine.appliances.registry).

Implements ONLY the surface actually consumed (measured across integration + session
+ engine): identity/state properties, load_*/update, settings/data/command_parameters,
sync_*. `api` is OUR transport.api.HonApi.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, Optional

from ..helpers import parse_cloud_timestamp
from .appliances import registry as _native_appliances
from .attributes import HonAttribute
from .command_loader import HonCommandLoader
from .commands import HonCommand
from .exceptions import NoAuthenticationException
from .parameter.base import HonParameter

_LOGGER = logging.getLogger(__name__)


class HonAppliance:
    _MINIMAL_UPDATE_INTERVAL = 5  # seconds
    # Realtime liveness is authoritative only while RECENT: a realtime message overrides
    # a STALE REST DISCONNECTED only if received within this wall-clock window. Without
    # the bound, a once-seen realtime time would keep a silently-dead appliance online
    # FOREVER, because the cloud's lastConnEvent can stay frozen at an OLD disconnect
    # indefinitely (observed: frozen ~2.5h) and never emit a disconnect newer than the
    # last traffic. After the window we defer to the REST lastConnEvent. 5 poll cycles:
    # long enough to avoid flicker for a connected-but-quiet device, short enough to
    # recover a dead one promptly.
    _REALTIME_LIVENESS_TTL = 300  # seconds

    def __init__(self, api: Any, info: dict[str, Any], zone: int = 0) -> None:
        if attributes := info.get("attributes"):
            info["attributes"] = {v["parName"]: v["parValue"] for v in attributes}
        self._info: dict[str, Any] = info
        self._api = api
        self._appliance_model: dict[str, Any] = {}
        self._commands: dict[str, HonCommand] = {}
        self._statistics: dict[str, Any] = {}
        self._attributes: dict[str, Any] = {}
        self._zone = zone
        self._additional_data: dict[str, Any] = {}
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
        return int(self._info.get("applianceModelId", 0))

    # --- state / data ---
    @property
    def options(self) -> dict[str, Any]:
        return dict(self._appliance_model.get("options", {}))

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
        command_loader = HonCommandLoader(self.api, self)
        await command_loader.load_commands()
        self._commands = command_loader.commands
        self._additional_data = command_loader.additional_data
        self._appliance_model = command_loader.appliance_data
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

    async def load_statistics(self) -> None:
        self._statistics = await self.api.load_statistics(self)
        self._statistics |= await self.api.load_maintenance(self)

    async def update(self, force: bool = False) -> None:
        now = datetime.now()
        min_age = now - timedelta(seconds=self._MINIMAL_UPDATE_INTERVAL)
        if force or not self._last_update or self._last_update < min_age:
            self._last_update = now
            await self.load_attributes()
            self.sync_params_to_command("settings")

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

    def sync_params_to_command(self, command_name: str) -> None:
        if not (command := self.commands.get(command_name)):
            return
        for key in command.setting_keys:
            if (
                new := self.attributes.get("parameters", {}).get(key)
            ) is None or new.value == "":
                continue
            try:
                # Always assign as a STRING (range params included): the range
                # setter runs str_to_float, which tries int() first, so a float
                # like 22.5 would be TRUNCATED to 22 without error (see
                # helpers.str_to_float, and the same note in number.py /
                # rules.py._apply_fixed). A string preserves the decimals, so a
                # half-degree setpoint is not silently rounded when synced into
                # the command and later re-sent to the cloud.
                command.settings[key].value = str(new.value)
            except ValueError as error:
                _LOGGER.info("Can't set %s - %s", key, error)
                continue
