# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Air purifier fan: power and the Sleep/Auto/Max presets.

The entity exists only where the live schema and the live state support it (see
`air_purifier.discover_capabilities`); no model name is ever read. Every write is
expressed as a `CommandPatch` and goes through the transactional dispatcher, so a
mode change carries only `machMode` and cannot disturb the light, aroma, lock or
tone settings.

A preset is written through `startProgram` while the purifier is stopped and
through the settings command while it runs, which is why only the modes BOTH
commands declare are offered (`AirPurifierCapabilities.writable_modes`).
"""
from __future__ import annotations

import logging

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
from .const import AP_LAST_MODE_STORE, APPLIANCE_AP, DOMAIN
from .debug_utils import redact_id

_LOGGER = logging.getLogger(__name__)

# Deterministic fallback when no active mode has been observed yet (a fresh
# reload has an empty store). Auto, per the design; if this device does not
# declare Auto the lowest writable mode is used instead, so the choice stays
# deterministic rather than arbitrary.
_DEFAULT_PRESET = "auto"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create one fan per air purifier that can actually be driven."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data.get("client")
    entities: list[FanEntity] = []
    for appliance_id, data in coordinator_data_map(coordinator).items():
        if data.get("type") != APPLIANCE_AP:
            continue
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
            continue
        entities.append(
            HonAirPurifierFan(coordinator, appliance_id, capabilities, client)
        )
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
