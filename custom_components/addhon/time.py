# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Heat-pump water heater (type HW): the hour of the anti-legionella cycle.

Replaces the block-3 `sensor.sterilization_time` (never released) with a `time`
entity, block 5 (user decisions of 2026-10-06). A reading for everyone, as the
sensor was and the vacation dates are; writable only with the experimental option,
on the plain m7/m8 series. A write sends the four sterilization keys the app always
sends together (apk2 decomp.txt:4592313-4592337), the other three from the shadow,
the hour unpadded as the app writes it ("3:30"). Without the option a write raises
the localized `sterilization_time_read_only`.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity, coordinator_data_map
from .command_dispatch import async_dispatch_patch
from .const import APPLIANCE_HW, CONF_ENABLE_EXPERIMENTAL, DOMAIN
from .hpwh import (
    HPWH_STERILIZATION_KEYS,
    raise_refusal,
    schedule_writes_supported,
    settings_parameters,
    sterilization_clock,
    sterilization_clock_value,
    sterilization_write,
)

_SHADOW_KEY = "sterilizationTime"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """The sterilization hour of every HW appliance that publishes one."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data.get("client")
    # Read once: toggling the option reloads the entry (`_async_options_updated`).
    experimental = bool(entry.options.get(CONF_ENABLE_EXPERIMENTAL, False))
    entities = []
    for appliance_id, data in coordinator_data_map(coordinator).items():
        if not isinstance(data, Mapping) or data.get("type") != APPLIANCE_HW:
            continue
        attributes = data.get("attributes")
        if isinstance(attributes, Mapping) and _SHADOW_KEY in attributes:
            entities.append(
                HonHeatPumpSterilizationTime(
                    coordinator, appliance_id, client=client, experimental=experimental
                )
            )
    async_add_entities(entities)


class HonHeatPumpSterilizationTime(HonBaseEntity, TimeEntity):
    """`sterilizationTime`, padded or not; unknown when it is no time of day."""

    _attr_translation_key = "sterilization_time"
    _attr_icon = "mdi:clock-outline"

    def __init__(
        self, coordinator, appliance_id: str, *, client=None, experimental: bool = False
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._experimental = experimental
        # The key of the block-3 sensor it replaces: another platform, no clash.
        self._attr_unique_id = f"{appliance_id}_sterilization_time"

    @property
    def native_value(self) -> time | None:
        return sterilization_clock(self._get_attr(_SHADOW_KEY))

    async def async_set_value(self, value: time) -> None:
        if not self._experimental:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="sterilization_time_read_only"
            )
        if not schedule_writes_supported(self._appliance, HPWH_STERILIZATION_KEYS):
            raise_refusal("hpwh_write_not_supported")
        patch = sterilization_write(
            self._get_attr,
            settings_parameters(self._appliance),
            _SHADOW_KEY,
            sterilization_clock_value(value),
        )
        await async_dispatch_patch(self.hass, self._hon_client, self._appliance, patch)
        await self._async_request_command_refresh()
