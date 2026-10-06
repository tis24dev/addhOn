# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Heat-pump water heater (type HW, issue #113): the two vacation dates.

Read-only for now: setting a date raises a localized error. The write (the two dates
with `grSetVacDate`, as the app's `sendVacModeDate` sends them, apk2
decomp.txt:2334500-2334545) is planned on these same entities, so the unique ids
stay. Not behind the experimental option: like every other HW reading, they only
read the shadow.
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
from .const import APPLIANCE_HW, DOMAIN
from .hpwh import vacation_date

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
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = []
    for appliance_id, data in coordinator_data_map(coordinator).items():
        if not isinstance(data, Mapping) or data.get("type") != APPLIANCE_HW:
            continue
        attributes = data.get("attributes")
        attributes = attributes if isinstance(attributes, Mapping) else {}
        for key, shadow_key, icon in _VACATION_DATES:
            if shadow_key in attributes:
                entities.append(
                    HonHeatPumpVacationDate(coordinator, appliance_id, key, shadow_key, icon)
                )
    async_add_entities(entities)


class HonHeatPumpVacationDate(HonBaseEntity, DateEntity):
    """`vacStartDate` or `vacEndDate`; unknown while cleared ('', '2000-01-01')."""

    def __init__(
        self, coordinator, appliance_id: str, key: str, shadow_key: str, icon: str
    ) -> None:
        super().__init__(coordinator, appliance_id)
        self._shadow_key = shadow_key
        self._attr_translation_key = key
        self._attr_unique_id = f"{appliance_id}_{key}"
        self._attr_icon = icon

    @property
    def native_value(self) -> date | None:
        return vacation_date(self._get_attr(self._shadow_key))

    async def async_set_value(self, value: date) -> None:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="vacation_dates_read_only"
        )
