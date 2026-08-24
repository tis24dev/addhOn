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

COOKER HOOD. Speed goes out as `settings.windSpeed` and the stop as
`stopProgram`, deliberately diverging from the official app, which uses
`startProgram` for both. The reasoning, and the `programName` that divergence
avoids, is written out once in `hood.py`'s module docstring.
"""
from __future__ import annotations

import logging
import math

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
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
from .hon_commands import async_send_command
from .hood import (
    HOOD_SETTINGS_COMMAND,
    HOOD_SPEED_PARAM,
    HOOD_STOP_COMMAND,
    speed_level,
    speed_range,
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
    bounds = speed_range(data.get("appliance"))
    if bounds is None:
        _LOGGER.debug(
            "Fan debug: skip hood id=%s (no writable '%s' range on the '%s' command)",
            redact_id(appliance_id),
            HOOD_SPEED_PARAM,
            HOOD_SETTINGS_COMMAND,
        )
        return None
    return HonHoodFan(coordinator, appliance_id, bounds, client)


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

    The speed axis is the LIVE `settings.windSpeed` range, never
    `model_attributes.speedLevel`: both report 5 on the hood of issue #83, but the
    schema is the value the device will actually accept. On that hood the app
    labels the top one or two steps as "boost" and "double boost"
    (`getSpeedLevelsTitles` in the decompiled app); we expose them as the plain
    percentage steps they are, because the boost is not a separate mode the device
    reports back and a preset that cannot be read is a preset that ships unknown.

    ON/OFF IS READ FROM `windSpeed`, NOT FROM `onOffStatus`. The two agree on the
    dump (both 0 while stopped), but `windSpeed` is what we write and what we read
    back, so binding the state to it keeps the entity self-consistent even if a
    firmware moved `onOffStatus` on its own.

    TURNING THE FAN OFF ALSO TURNS THE LIGHT OFF. `stopProgram` pins `windSpeed`
    AND `lightStatus` to "0" -- they are `fixed` in the schema, so the values are
    the device's own declaration, not a choice of ours -- and the official app
    behaves the same way. The light switch re-reads its state right after, because
    the post-command refresh reloads the whole appliance.
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
        speed_bounds: tuple[int, int],
        client=None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._speed_bounds = speed_bounds
        lowest, highest = speed_bounds
        # `speed_count` is the number of SELECTABLE steps, endpoints included, so
        # Home Assistant renders one slider notch per real level. Deriving it from
        # the bounds rather than from the top value keeps a hypothetical hood whose
        # range does not start at 1 honest.
        self._attr_speed_count = highest - lowest + 1
        self._attr_unique_id = f"{appliance_id}_hood"
        _LOGGER.debug(
            "Fan debug: initialized hood fan id=%s speeds=%s..%s (%d steps)",
            redact_id(appliance_id),
            lowest,
            highest,
            self._attr_speed_count,
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
        return min(100, ranged_value_to_percentage(self._speed_bounds, level))

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the extraction level; 0 means stop, as Home Assistant defines it."""
        if percentage == 0:
            await self.async_turn_off()
            return
        lowest, highest = self._speed_bounds
        # Rounded UP so that any non-zero request produces a moving fan: rounding
        # to nearest would turn the bottom slice of the slider into a silent
        # no-op that reads as "the integration ignored me".
        level = math.ceil(percentage_to_ranged_value(self._speed_bounds, percentage))
        await self._send(
            "set_percentage",
            HOOD_SETTINGS_COMMAND,
            {HOOD_SPEED_PARAM: str(max(lowest, min(highest, level)))},
        )

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
        await self._send(
            "turn_on",
            HOOD_SETTINGS_COMMAND,
            {HOOD_SPEED_PARAM: str(self._speed_bounds[0])},
        )

    async def async_turn_off(self, **kwargs) -> None:
        """Stop the hood with `stopProgram`, the one command it is known to run.

        No values are passed: every parameter this command carries is `fixed` in
        the schema, so the device's own declaration is what goes on the wire --
        the exact payload its command history shows it accepted and executed.
        """
        await self._send("turn_off", HOOD_STOP_COMMAND, {})

    async def _send(self, action: str, command_name: str, values: dict[str, str]) -> None:
        """Send one hood intent, then refresh.

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
                "Fan debug: hood %s id=%s command=%s values=%s",
                action,
                redact_id(self._appliance_id),
                command_name,
                values,
            )
            await async_send_command(
                self.hass, client, appliance, command_name, values
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
