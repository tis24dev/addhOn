# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Air purifier panel light: a three-level, INVERSELY encoded brightness.

The device encoding runs backwards from Home Assistant's: raw `2` is off, `1` is
half, `0` is full. Only the exact observed three-value schema creates the entity,
because reusing this mapping against a two- or four-level device would send a
value that device never declared.

Home Assistant's brightness scale is continuous (0-255) while the panel has three
steps, so a requested brightness is quantized to the nearest supported level.
Writes go through the transactional dispatcher and carry `lightStatus` alone.
"""
from __future__ import annotations

import logging

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .air_purifier import (
    AP_BRIGHTNESS_TO_LIGHT,
    AP_LIGHT_TO_BRIGHTNESS,
    AirPurifierCapabilities,
    ap_patch,
    discover_capabilities,
    raw_text,
    reports_attribute,
)
from .base_entity import HonBaseEntity, coordinator_data_map
from .command_dispatch import async_dispatch_patch
from .const import AP_LAST_LIGHT_STORE, APPLIANCE_AP, DOMAIN
from .debug_utils import redact_id

_LOGGER = logging.getLogger(__name__)

_LIGHT_PARAM = "lightStatus"
# Home Assistant brightness that means "off". Kept explicit because the INVERSE
# encoding makes the off level the HIGHEST raw value, which is easy to misread.
_OFF_BRIGHTNESS = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create one panel light per purifier whose schema matches the observed one."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data.get("client")
    entities: list[LightEntity] = []
    for appliance_id, data in coordinator_data_map(coordinator).items():
        if data.get("type") != APPLIANCE_AP:
            continue
        attributes = data.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        capabilities = discover_capabilities(data.get("appliance"), attributes)
        # The read state is gated separately from the write schema: a panel light
        # that cannot be read would ship permanently unknown.
        if not capabilities.supports_light or not reports_attribute(
            attributes, _LIGHT_PARAM
        ):
            _LOGGER.debug(
                "Light debug: skip purifier id=%s (writable=%s values=%s state=%s)",
                redact_id(appliance_id),
                capabilities.supports_light,
                sorted(capabilities.light_values),
                reports_attribute(attributes, _LIGHT_PARAM),
            )
            continue
        entities.append(
            HonAirPurifierLight(coordinator, appliance_id, capabilities, client)
        )
    async_add_entities(entities)


class HonAirPurifierLight(HonBaseEntity, LightEntity):
    """Panel light with the device's three inverse levels."""

    _attr_translation_key = "panel_light"
    _attr_icon = "mdi:television-ambient-light"
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        capabilities: AirPurifierCapabilities,
        client=None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._capabilities = capabilities
        self._attr_unique_id = f"{appliance_id}_panel_light"
        # Fixed at construction from the live schema. A later shadow value outside
        # this set must never add or drop a level, so the entity's own idea of what
        # it can do cannot drift with the device's reporting.
        self._levels = sorted(
            AP_LIGHT_TO_BRIGHTNESS[raw] for raw in capabilities.light_values
        )
        self._lit_levels = [
            level for level in self._levels if level != _OFF_BRIGHTNESS
        ]
        _LOGGER.debug(
            "Light debug: initialized panel light id=%s levels=%s",
            redact_id(appliance_id),
            self._levels,
        )

    @property
    def supported_brightness_levels(self) -> list[int]:
        """The panel's discrete levels, in Home Assistant brightness units."""
        return list(self._levels)

    @property
    def _raw_level(self) -> str | None:
        raw = self._get_attr(_LIGHT_PARAM)
        return None if raw is None else raw_text(raw)

    @property
    def brightness(self) -> int | None:
        """Current level, or None for a raw value the schema does not declare."""
        return AP_LIGHT_TO_BRIGHTNESS.get(self._raw_level or "")

    @property
    def is_on(self) -> bool | None:
        brightness = self.brightness
        if brightness is None:
            return None
        return brightness != _OFF_BRIGHTNESS

    def _handle_coordinator_update(self) -> None:
        """Remember the lit level so a bare turn-on can restore it."""
        self._remember_level(self.brightness)
        super()._handle_coordinator_update()

    def _remember_level(self, brightness: int | None) -> None:
        """Store `brightness` only if it is a genuine LIT level of this device.

        One membership test covers all three rejections: an unreported level
        (None), the off level (restoring it on a turn-on would leave the panel
        dark), and a raw value the schema does not declare (which could not be
        sent back anyway). Deriving the set from the schema keeps this correct if
        the light capability is ever widened beyond the observed three values.
        """
        if brightness not in self._lit_levels:
            return
        self._coordinator_store(AP_LAST_LIGHT_STORE)[self._appliance_id] = brightness

    def _dimmest_lit_level(self) -> int:
        return min(self._lit_levels)

    def _brightest_level(self) -> int:
        return max(self._levels)

    def _quantize(self, brightness: int) -> int:
        """Nearest supported level to `brightness`, ties going to the BRIGHTER one.

        Home Assistant sends a continuous 0-255 value and the panel has three
        steps. Rounding a tie upward keeps a "make it a bit dimmer" request from
        crossing all the way to off. Out-of-range input needs no clamp: the
        nearest level to a value below 0 or above 255 is the boundary level
        anyway, so the search already yields something the device declares.
        """
        target = int(brightness)
        # max() over (-distance, level) picks the smallest distance and, among
        # equals, the largest level -- the tie-to-brighter rule, in one pass.
        return max(self._levels, key=lambda level: (-abs(level - target), level))

    async def async_turn_on(self, **kwargs) -> None:
        """Set a level, defaulting to the last lit one, else full brightness.

        A quantized request that lands on the off level is raised to the dimmest
        LIT level: `turn_on` must leave the panel on.
        """
        requested = kwargs.get("brightness")
        if requested is None:
            stored = self._coordinator_store(AP_LAST_LIGHT_STORE).get(
                self._appliance_id
            )
            level = stored if stored in self._levels else self._brightest_level()
        else:
            level = self._quantize(requested)
            if level == _OFF_BRIGHTNESS:
                level = self._dimmest_lit_level()
        await self._dispatch(AP_BRIGHTNESS_TO_LIGHT[level])
        self._remember_level(level)

    async def async_turn_off(self, **kwargs) -> None:
        await self._dispatch(AP_BRIGHTNESS_TO_LIGHT[_OFF_BRIGHTNESS])

    async def _dispatch(self, raw: str) -> None:
        """Send one light intent, then refresh.

        The intent is built INSIDE the try so a value the live schema rejects
        surfaces as the same localized command error as a transport failure.
        """
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        try:
            patch = ap_patch("set_light", self._capabilities, value=raw)
            _LOGGER.debug(
                "Light debug: panel light id=%s -> %s=%s",
                redact_id(self._appliance_id),
                _LIGHT_PARAM,
                raw,
            )
            await async_dispatch_patch(self.hass, client, appliance, patch)
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Purifier light: set %s failed: %s", raw, err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err
