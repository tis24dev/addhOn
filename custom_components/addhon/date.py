# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Heat-pump water heater (type HW, issue #113): the two vacation dates.

A reading for everyone: like every other HW reading they only read the shadow, so
they exist without the experimental option. Writable only with it (block 5, user
decisions of 2026-10-06), on the plain m7/m8 series: every write sends BOTH dates
with `grSetVacDate`, as the app's `sendVacModeDate` does (apk2
decomp.txt:2334500-2334545), the other half taken from the shadow. Without the
option a write raises the localized `vacation_dates_read_only`.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from homeassistant.components.date import DateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity, coordinator_data_map
from .command_dispatch import async_dispatch_patch
from .const import APPLIANCE_HW, CONF_ENABLE_EXPERIMENTAL, DOMAIN
from .hpwh import (
    HPWH_VACATION_KEYS,
    raise_refusal,
    schedule_writes_supported,
    vacation_block,
    vacation_date,
    vacation_order_block,
    vacation_patch,
    vacation_range,
)

# (unique_id suffix and translation key, shadow attribute, icon). Plain tuples, not
# entity descriptions: two fixed entities of one type, like the water heater.
_VACATION_DATES: tuple[tuple[str, str, str], ...] = (
    ("vacation_start", "vacStartDate", "mdi:calendar-start"),
    ("vacation_end", "vacEndDate", "mdi:calendar-end"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """One date per vacation attribute the appliance publishes; HW only."""
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
        attributes = attributes if isinstance(attributes, Mapping) else {}
        for key, shadow_key, icon in _VACATION_DATES:
            if shadow_key in attributes:
                entities.append(
                    HonHeatPumpVacationDate(
                        coordinator, appliance_id, key, shadow_key, icon,
                        client=client, experimental=experimental,
                    )
                )
    async_add_entities(entities)


class HonHeatPumpVacationDate(HonBaseEntity, DateEntity):
    """`vacStartDate` or `vacEndDate`; unknown while cleared ('', '2000-01-01')."""

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        key: str,
        shadow_key: str,
        icon: str,
        *,
        client=None,
        experimental: bool = False,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._shadow_key = shadow_key
        self._experimental = experimental
        self._attr_translation_key = key
        self._attr_unique_id = f"{appliance_id}_{key}"
        self._attr_icon = icon

    @property
    def native_value(self) -> date | None:
        return vacation_date(self._get_attr(self._shadow_key))

    async def async_set_value(self, value: date) -> None:
        if not self._experimental:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="vacation_dates_read_only"
            )
        if not schedule_writes_supported(self._appliance, HPWH_VACATION_KEYS):
            raise_refusal("hpwh_write_not_supported")
        raise_refusal(vacation_block(self._get_attr))
        if self._shadow_key == "vacStartDate":
            start, end = vacation_range(self._get_attr, start=value)
        else:
            start, end = vacation_range(self._get_attr, end=value)
        raise_refusal(vacation_order_block(start, end))
        await async_dispatch_patch(
            self.hass, self._hon_client, self._appliance, vacation_patch(start, end)
        )
        await self._async_request_command_refresh()
