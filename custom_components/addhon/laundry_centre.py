# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Laundry Centre (LC, #124): one MAC carrying a washer and a dryer.

The cloud reports the whole unit as a single appliance of type `LC`. The app shows
it as two machines, so the entities of each half live on a child device,
`<id>_WM` (washer) and `<id>_TD` (dryer), which hangs off the LC device through
`via_device`. The LC device itself keeps the connectivity and the energy readings.
"""
from __future__ import annotations

from collections.abc import Mapping

from homeassistant.helpers.entity import DeviceInfo

from .const import APPLIANCE_LC, DOMAIN

# The two units and the translation key of the device each one gets.
LC_UNITS: tuple[str, str] = ("WM", "TD")
_UNIT_TRANSLATION_KEYS = {"WM": "lc_washer", "TD": "lc_dryer"}


def unit_device_id(appliance_id: str, unit: str) -> str:
    """The identifier of the child device of one unit of a Laundry Centre."""
    return f"{appliance_id}_{unit}"


def unit_device_info(appliance_id: str, unit: str, data: Mapping) -> DeviceInfo:
    """The child device of `unit`, hanging off the LC device.

    No `name`: the device is named by its translation, which reads "<LC name>
    Washer" / "<LC name> Dryer" in the user's language. Name and model are read
    like `HonBaseEntity.device_info` reads them, defaults included.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, unit_device_id(appliance_id, unit))},
        via_device=(DOMAIN, appliance_id),
        translation_key=_UNIT_TRANSLATION_KEYS[unit],
        translation_placeholders={"name": data.get("name", "Haier Appliance")},
        manufacturer="Haier",
        model=data.get("model", "Unknown"),
    )


def _lc_ids(coord_data) -> list[str]:
    if not isinstance(coord_data, Mapping):
        return []
    return [
        appliance_id
        for appliance_id, device in coord_data.items()
        if isinstance(device, Mapping) and device.get("type") == APPLIANCE_LC
    ]


def lc_parent_id(identifier: str, coord_data: Mapping | None) -> str | None:
    """The LC a unit device identifier belongs to, or None.

    `<id>_WM` / `<id>_TD` name the child device of the LC `<id>`; any other identifier,
    or one whose `<id>` is absent or not an LC, belongs to no LC.
    """
    if not isinstance(identifier, str):
        return None
    appliance_id, _, unit = identifier.rpartition("_")
    if unit not in LC_UNITS or appliance_id not in _lc_ids(coord_data):
        return None
    return appliance_id


def reported_units(attributes) -> tuple[str, ...]:
    """The units whose own keys are in an LC's attributes, in `LC_UNITS` order.

    A unit's shadow keys carry its code as a prefix (`WMmachMode`, `TDprPhase`). The
    bare code does not count: the per-unit statistics land in the attributes under
    `WM` / `TD` whatever the shadow reports.
    """
    if not isinstance(attributes, Mapping):
        return ()
    keys = [str(key) for key in attributes]
    return tuple(
        unit
        for unit in LC_UNITS
        if any(key.startswith(unit) and key != unit for key in keys)
    )


def live_unit_device_ids(coord_data: Mapping | None) -> set[str]:
    """The child device ids that exist: their LC is in the snapshot and their unit
    still reports keys of its own.

    A unit that stopped reporting has no entity left, and its device would otherwise
    stay in the registry, empty and impossible to delete.
    """
    live: set[str] = set()
    for appliance_id in _lc_ids(coord_data):
        attributes = coord_data[appliance_id].get("attributes")
        live.update(unit_device_id(appliance_id, unit) for unit in reported_units(attributes))
    return live


def register_parent_devices(dev_reg, entry_id: str, coord_data: Mapping | None) -> int:
    """Register the device of each LC before the platforms add its child devices.

    The platforms run in parallel and a child entity names its parent only through
    `via_device`. Home Assistant 2024.12 drops a `via_device` that points at a
    device not yet in the registry, silently (`device_registry.py`
    2024.12.0:824-827), so a child that wins the race would be left without a
    parent for good. Creating the parent first removes the race. The values are
    those `HonBaseEntity.device_info` gives, so the entity that later claims the
    device finds it unchanged.

    Returns how many parents were registered.
    """
    count = 0
    for appliance_id in _lc_ids(coord_data):
        data = coord_data[appliance_id]
        dev_reg.async_get_or_create(
            config_entry_id=entry_id,
            identifiers={(DOMAIN, appliance_id)},
            name=data.get("name", "Haier Appliance"),
            manufacturer="Haier",
            model=data.get("model", "Unknown"),
        )
        count += 1
    return count
