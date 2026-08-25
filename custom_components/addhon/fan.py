# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Haier hOn fans: the air purifier's presets and the cooker hood's speed axis.

Two entities that share a platform and nothing else. The purifier has no speed
axis and runs in discrete modes; the hood has no modes and is a plain 0..N speed
range. Both are capability-gated on the LIVE command schema, so no model name is
ever read and a device that does not declare the parameter simply gets no fan.

AIR PURIFIER. Every write is expressed as a `CommandPatch` and goes through the
transactional dispatcher, so a mode change carries only `machMode` and cannot
disturb the light, aroma, lock or tone settings. A preset is written through
`startProgram` while the purifier is stopped and through the settings command
while it runs, which is why only the modes BOTH commands declare are offered
(`AirPurifierCapabilities.writable_modes`).

COOKER HOOD. Every write is a `CommandPatch` too, built by `hood.hood_patch` and
dispatched the same way. Speed AND the fan's own off both go out as
`startProgram.windSpeed`, which reproduces the official app's body once the
dispatcher adds the command's mandatory `onOffStatus`. Switching the appliance off
is a different action on a different entity: it belongs to the hood's power
switch, not to the fan. `hood.py`'s module docstring carries the whole argument.
"""
from __future__ import annotations

import logging

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from .air_purifier import (
    AP_MODE_TO_PRESET,
    AP_PRESET_TO_MODE,
    AirPurifierCapabilities,
    ap_patch,
    discover_capabilities,
    raw_text,
)
from .base_entity import HonBaseEntity, coordinator_data_map
from .command_dispatch import async_dispatch_patch
from .const import AP_LAST_MODE_STORE, APPLIANCE_AP, APPLIANCE_HO, DOMAIN
from .debug_utils import redact_id
from .hood import (
    HOOD_SPEED_PARAM,
    HOOD_START_COMMAND,
    hood_patch,
    speed_level,
    speed_levels,
)

_LOGGER = logging.getLogger(__name__)

# Deterministic fallback when no active mode has been observed yet (a fresh
# reload has an empty store). Auto, per the design; if this device does not
# declare Auto the lowest writable mode is used instead, so the choice stays
# deterministic rather than arbitrary.
_DEFAULT_PRESET = "auto"


def _purifier_fan(coordinator, appliance_id: str, data: dict, client):
    """The purifier fan of ONE appliance, or None when it cannot be driven."""
    attributes = data.get("attributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    capabilities = discover_capabilities(data.get("appliance"), attributes)
    if not capabilities.supports_fan:
        _LOGGER.debug(
            "Fan debug: skip purifier id=%s (no fan capability: "
            "start=%s stop=%s writable_modes=%s power_state=%s mode_state=%s)",
            redact_id(appliance_id),
            capabilities.can_start,
            capabilities.can_stop,
            sorted(capabilities.writable_modes),
            capabilities.has_power_state,
            capabilities.has_mode_state,
        )
        return None
    return HonAirPurifierFan(coordinator, appliance_id, capabilities, client)


def _hood_fan(coordinator, appliance_id: str, data: dict, client):
    """The hood fan of ONE appliance, or None when the hood declares no speed axis.

    The rejection is LOGGED rather than silently skipped, like every other
    capability gate in this integration: a hood that ships without its fan and a
    hood that never reached this branch look identical from a field report, and
    the schema bounds are the only input that decides between them.
    """
    levels = speed_levels(data.get("appliance"))
    if levels is None:
        _LOGGER.debug(
            "Fan debug: skip hood id=%s (no writable '%s' range on the '%s' command)",
            redact_id(appliance_id),
            HOOD_SPEED_PARAM,
            HOOD_START_COMMAND,
        )
        return None
    return HonHoodFan(coordinator, appliance_id, levels, client)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create one fan per air purifier or cooker hood that can actually be driven."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data.get("client")
    # Dispatch by type rather than filtering to one: the purifier branch is
    # untouched by construction, so adding the hood cannot change which purifier
    # fans exist or how they are built.
    builders = {APPLIANCE_AP: _purifier_fan, APPLIANCE_HO: _hood_fan}
    entities: list[FanEntity] = []
    for appliance_id, data in coordinator_data_map(coordinator).items():
        builder = builders.get(data.get("type"))
        if builder is None:
            continue
        entity = builder(coordinator, appliance_id, data, client)
        if entity is not None:
            entities.append(entity)
    async_add_entities(entities)


class HonAirPurifierFan(HonBaseEntity, FanEntity):
    """Purifier power plus the writable subset of Sleep/Auto/Max."""

    _attr_translation_key = "purifier"
    _attr_icon = "mdi:air-purifier"
    _attr_supported_features = (
        FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        capabilities: AirPurifierCapabilities,
        client=None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._capabilities = capabilities
        self._attr_unique_id = f"{appliance_id}_purifier"
        self._attr_preset_modes = list(capabilities.preset_options)
        _LOGGER.debug(
            "Fan debug: initialized purifier fan id=%s presets=%s",
            redact_id(appliance_id),
            self._attr_preset_modes,
        )

    @property
    def _raw_mode(self) -> str | None:
        raw = self._get_attr("machMode")
        return None if raw is None else raw_text(raw)

    @property
    def is_on(self) -> bool | None:
        raw = self._get_attr("onOffStatus")
        if raw is None:
            return None
        return raw_text(raw) == "1"

    @property
    def preset_mode(self) -> str | None:
        """The active preset, or None while stopped.

        `machMode=0` is the off-state sentinel the device reports when stopped and
        is never a preset, so an unmapped or off value reads as no active preset
        rather than being invented into one.
        """
        if not self.is_on:
            return None
        return AP_MODE_TO_PRESET.get(self._raw_mode or "")

    def _handle_coordinator_update(self) -> None:
        """Remember the running mode so a later turn-on can restore it.

        Recorded here rather than in `preset_mode`, which must stay a pure read:
        Home Assistant queries properties far more often than the state changes.
        """
        self._remember_mode(self._raw_mode)
        super()._handle_coordinator_update()

    def _remember_mode(self, raw: str | None) -> None:
        """Store `raw` only if it is genuinely selectable on this device.

        Guards the store against the off sentinel and against any mode the live
        schema does not declare, so the value a later turn-on replays is always
        one the device will accept.
        """
        if raw is None or raw not in self._capabilities.writable_modes:
            return
        self._coordinator_store(AP_LAST_MODE_STORE)[self._appliance_id] = raw

    def _last_mode(self) -> str:
        """The mode a bare turn-on should use: last active, else the default."""
        stored = self._coordinator_store(AP_LAST_MODE_STORE).get(self._appliance_id)
        writable = self._capabilities.writable_modes
        if stored in writable:
            return stored
        default = AP_PRESET_TO_MODE[_DEFAULT_PRESET]
        if default in writable:
            return default
        return sorted(writable)[0]

    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs
    ) -> None:
        """Start the purifier in a preset, refusing a speed it cannot honour.

        The purifier has no speed axis: it runs in discrete modes, which is why
        SET_SPEED is not declared and no percentage is exposed anywhere. The
        parameter stays in the signature because the service passes it positionally.

        A caller can still hand one over, and dropping it silently is the worst of
        the three options: the device starts in the REMEMBERED mode, the service
        returns success, and the automation reads as though the speed had been
        applied. Refusing costs a visible error on a call that was never going to
        do what it asked.
        """
        if percentage is not None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="speed_not_supported",
            )
        mode = (
            AP_PRESET_TO_MODE.get(preset_mode)
            if preset_mode is not None
            else self._last_mode()
        )
        if mode is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": f"unknown preset {preset_mode!r}"},
            )
        await self._dispatch("turn_on", value=mode)
        self._remember_mode(mode)

    async def async_turn_off(self, **kwargs) -> None:
        await self._dispatch("turn_off")

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Change the running mode, or start the purifier in it while stopped.

        The command differs by state (settings while running, startProgram while
        stopped), which is exactly why only modes both declare are offered.
        """
        mode = AP_PRESET_TO_MODE.get(preset_mode)
        if mode is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": f"unknown preset {preset_mode!r}"},
            )
        await self._dispatch(
            "set_preset" if self.is_on else "turn_on", value=mode
        )
        self._remember_mode(mode)

    async def _dispatch(self, action: str, **values) -> None:
        """Build and send one AP intent, then refresh.

        The intent is built INSIDE the try so a value the live schema rejects
        surfaces as the same localized command error as a transport failure,
        instead of a bare ValueError reaching Home Assistant.
        """
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        try:
            patch = ap_patch(action, self._capabilities, **values)
            _LOGGER.debug(
                "Fan debug: purifier %s id=%s command=%s values=%s",
                action,
                redact_id(self._appliance_id),
                patch.command_name,
                dict(patch.values),
            )
            await async_dispatch_patch(self.hass, client, appliance, patch)
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Purifier fan: %s failed: %s", action, err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err


class HonHoodFan(HonBaseEntity, FanEntity):
    """Cooker hood extraction fan: one speed step per declared wind-speed level.

    The speed axis is the LIVE `startProgram.windSpeed` range, never
    `model_attributes.speedLevel`: both report 5 on the hood of issue #83, but the
    schema is the value the device will actually accept. On that hood the app
    labels the top one or two steps as "boost" and "double boost"
    (`getSpeedLevelsTitles` in the decompiled app); we expose them as the plain
    percentage steps they are, because the boost is not a separate mode the device
    reports back and a preset that cannot be read is a preset that ships unknown.

    THIS ENTITY IS THE EXTRACTION FAN, NOT THE APPLIANCE. It reads and writes
    `windSpeed` and nothing else; `onOffStatus` -- the lit-or-dark panel -- belongs
    to the hood's power switch. That separation is the fix for the first shipped
    version, whose off sent `stopProgram` and so darkened the whole appliance:
    from the dark state the hood ignores every later write, so Home Assistant could
    switch the fan off once and never switch it back on.

    TURNING THE FAN OFF NOW LEAVES THE LIGHT ALONE, because it is a `windSpeed` of
    zero and touches nothing else. The device's own declaration says the same: 0 is
    a legal value of the range it publishes, and the app's slider sends exactly
    that. Switching everything off, light included, is the power switch.
    """

    _attr_translation_key = "hood"
    _attr_icon = "mdi:fan"
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        levels: tuple[int, ...],
        client=None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._levels = levels
        lowest, highest = levels[0], levels[-1]
        # `speed_count` is the number of SELECTABLE steps, so Home Assistant
        # renders one slider notch per real level. Counted from the enumerated
        # grid rather than from the distance between the endpoints: on a hood
        # declaring an increment above 1 the two disagree, and it is the grid
        # that the device will accept.
        self._attr_speed_count = len(levels)
        self._attr_unique_id = f"{appliance_id}_hood"
        _LOGGER.debug(
            "Fan debug: initialized hood fan id=%s speeds=%s..%s (%d steps: %s)",
            redact_id(appliance_id),
            lowest,
            highest,
            self._attr_speed_count,
            levels,
        )

    @property
    def _level(self) -> int | None:
        return speed_level(self._get_attr(HOOD_SPEED_PARAM))

    @property
    def is_on(self) -> bool | None:
        level = self._level
        return None if level is None else level != 0

    @property
    def percentage(self) -> int | None:
        """The extraction level as a percentage, 0 while the fan is stopped.

        Clamped at 100 because a device is free to report a level above the range
        it declares as writable, and a percentage over 100 makes Home Assistant's
        slider unusable rather than merely inaccurate.
        """
        level = self._level
        if level is None:
            return None
        if level <= 0:
            return 0
        if level in self._levels:
            return ordered_list_item_to_percentage(self._levels, level)
        # Off-grid reading: snap to the nearest declared level instead of letting
        # `ordered_list_item_to_percentage` raise on a value it cannot place. A
        # property that raises takes the entity down; a percentage that is one
        # notch off on a reading the device should never have sent does not.
        nearest = min(self._levels, key=lambda declared: abs(declared - level))
        return ordered_list_item_to_percentage(self._levels, nearest)

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the extraction level; 0 means stop, as Home Assistant defines it."""
        if percentage == 0:
            await self.async_turn_off()
            return
        # Rounds UP by construction (`ceil(percentage * len / 100)`), so any
        # non-zero request produces a moving fan: rounding to nearest would turn
        # the bottom slice of the slider into a silent no-op that reads as "the
        # integration ignored me". Picking from the enumerated grid also means the
        # value sent is always one the schema declares, never an interpolation
        # between two legal steps.
        level = percentage_to_ordered_list_item(self._levels, percentage)
        await self._send("set_percentage", str(level))

    async def async_turn_on(
        self, percentage: int | None = None, preset_mode: str | None = None, **kwargs
    ) -> None:
        """Start extraction at the requested level, or at the lowest one.

        `preset_mode` stays in the signature because the service passes it
        positionally; the hood declares no PRESET_MODE feature, so Home Assistant
        never fills it.
        """
        if percentage:
            await self.async_set_percentage(percentage)
            return
        await self._send("turn_on", str(self._levels[0]))

    async def async_turn_off(self, **kwargs) -> None:
        """Stop extraction by writing a wind speed of zero.

        NOT `stopProgram`, which pins `onOffStatus` and `lightStatus` to zero as
        well and leaves the appliance in the dark state no later write can reach.
        Zero is a declared value of the same range every other speed comes from,
        and it is what the app's own slider sends at its bottom notch, so the panel
        and the light stay exactly as the user left them.

        A HOOD ALREADY AT ZERO IS LEFT ALONE. Every write on this channel carries
        the schema's mandatory `onOffStatus = "1"`, so sending one to a hood whose
        fan is already stopped would WAKE the control panel -- a `fan.turn_off`
        service call, or a `homeassistant.turn_off` sweeping a whole area, would
        switch the appliance ON. Home Assistant calls this method regardless of the
        entity's current state, so the guard has to live here. An unreadable level
        is not zero and still writes: unknown is not a reason to skip a command the
        user asked for.

        THE GUARD LOOKS TWICE, and the refresh between the two looks is the point.
        A hood that started extracting since the last poll -- from its own panel, or
        from the app -- still reports zero in the cached reading, and skipping on
        that first look would make `turn_off` report success while the fan kept
        running. `_async_request_command_refresh` awaits a full coordinator refresh,
        so the second look is on current data.
        """
        if self._level == 0:
            await self._async_request_command_refresh()
            if self._level == 0:
                return
        await self._send("turn_off", "0")

    async def _send(self, action: str, level: str) -> None:
        """Write one wind speed, then refresh.

        ONE command, one channel: a sparse `startProgram` patch through the
        transactional dispatcher, carrying `windSpeed` and the one field the live
        schema marks mandatory. That field is `onOffStatus`, pinned to "1" by the
        schema itself, so the wire body is the official app's speed body to the
        key -- and a speed change wakes a dark panel exactly like the app's does.

        Sparse, and through the dispatcher, for the same reason as every other hood
        write: `hood.hood_patch` builds it, which is also where the `programName`
        suppression lives. The full-command sender is deliberately NOT used here.
        It would transmit the whole group, `lightStatus` included -- and this
        hood's `startProgram.lightStatus` loads at 1 and is never refreshed from
        the shadow, so every speed change would switch the light ON. It would also
        pre-write that value into the shadow before the network call, where nothing
        undoes it if the send fails.

        The payload is built INSIDE the try so a value the live schema rejects
        surfaces as the same localized command error as a transport failure,
        instead of a bare RuntimeError reaching Home Assistant.
        """
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        try:
            _LOGGER.debug(
                "Fan debug: hood %s id=%s command=%s %s=%s",
                action,
                redact_id(self._appliance_id),
                HOOD_START_COMMAND,
                HOOD_SPEED_PARAM,
                level,
            )
            await async_dispatch_patch(
                self.hass,
                client,
                appliance,
                hood_patch(
                    HOOD_START_COMMAND,
                    {HOOD_SPEED_PARAM: level},
                    action=f"hood_{action}",
                ),
            )
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Hood fan: %s failed: %s", action, err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err
