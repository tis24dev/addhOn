# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Haier hOn binary sensors (wash group): door, locks, maintenance alarms.

The entities are CAPABILITY-GATED: a description is created only if the device
actually exposes that attribute (present in coordinator.data[id]["attributes"]),
so no perpetually "unknown" entities show up on models that do not report it
(e.g. doorLockStatus is not guaranteed on the tumble dryer). All the keys used
here are direct 0/1 attributes, confirmed live on HW80 (washer) / HD100 (dryer).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonAccountCoordinatorEntity, HonBaseEntity, coordinator_data_map
from .const import (
    APPLIANCE_AC,
    APPLIANCE_AP,
    APPLIANCE_DW,
    APPLIANCE_FR,
    APPLIANCE_FRE,
    APPLIANCE_HO,
    APPLIANCE_HOB,
    APPLIANCE_IH,
    APPLIANCE_OV,
    APPLIANCE_REF,
    APPLIANCE_TD,
    APPLIANCE_WC,
    APPLIANCE_WD,
    APPLIANCE_WH,
    APPLIANCE_WM,
    CONF_ENABLE_EXPERIMENTAL,
    DOMAIN,
    WM_ATTR_CHILD_LOCK,
    WM_ATTR_DOOR,
    WM_ATTR_DOOR_OPEN,
    WM_ATTR_DRUM_CLEAN,
    WM_ATTR_DRY_CLEAN_NEEDED,
    WM_ATTR_FILTER_CLEAN,
)
from .air_purifier import co_alarm, has_problem, is_engaged
from .debug_utils import redact_id

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HonBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Description of a Haier hOn binary sensor.

    - `key` = unique_id suffix (new, no historic entity uses these suffixes).
    - `attr_key` = key read via HonBaseEntity._get_attr.
    - `on_value` = raw value that corresponds to the "on" state (default "1").
    - `value_fn` optional: derives the state from the raw value instead of
      comparing it to `on_value`. For signals whose "off" is a SET of raw values
      rather than a single one (an error code has several no-error spellings), so
      the rule stays one shared function instead of a literal per description.
      Never called with None: a missing reading stays unknown.
    """

    attr_key: str
    on_value: str = "1"
    value_fn: Callable[[object], bool | None] | None = None
    # When True the binary exists only while the experimental option is on: its
    # meaning is inferred from incomplete evidence.
    experimental: bool = False
    # When True the entity hides instead of reporting a state it cannot interpret
    # (value_fn returned None). For an alarm whose all-clear value is unknown,
    # reporting "off" would assert an absence that no evidence supports.
    unavailable_when_unmapped: bool = False


_DOOR_OPEN = HonBinarySensorEntityDescription(
    key="door_open",
    attr_key=WM_ATTR_DOOR_OPEN,           # doorStatus: 1 = open
    device_class=BinarySensorDeviceClass.DOOR,
)
_DOOR_LOCK = HonBinarySensorEntityDescription(
    key="door_lock",
    icon="mdi:lock",
    attr_key=WM_ATTR_DOOR,                # doorLockStatus: 1 = locked
)
_CHILD_LOCK = HonBinarySensorEntityDescription(
    key="child_lock",
    icon="mdi:lock-alert",
    attr_key=WM_ATTR_CHILD_LOCK,         # lockStatus: 1 = active
)
_DRUM_CLEAN = HonBinarySensorEntityDescription(
    key="drum_clean_needed",
    attr_key=WM_ATTR_DRUM_CLEAN,
    device_class=BinarySensorDeviceClass.PROBLEM,
)
_FILTER_CLEAN = HonBinarySensorEntityDescription(
    key="filter_clean_needed",
    attr_key=WM_ATTR_FILTER_CLEAN,
    device_class=BinarySensorDeviceClass.PROBLEM,
)
_DRY_CLEAN = HonBinarySensorEntityDescription(
    key="dry_clean_needed",
    attr_key=WM_ATTR_DRY_CLEAN_NEEDED,
    device_class=BinarySensorDeviceClass.PROBLEM,
)

# Connectivity: UNIVERSAL (every device) and ALWAYS available (it must be able to
# signal 'disconnected'). Reads the `available` flag (from the engine, from
# lastConnEvent.category). on = connected.
_CONNECTIVITY = HonBinarySensorEntityDescription(
    key="connectivity",
    attr_key="available",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
)

# Universal capability-gated binaries: candidates on ANY appliance type, created
# only where the device reports the attribute (unlike _CONNECTIVITY, which is
# always created so it can signal 'disconnected'). remoteCtrValid = whether remote
# control is currently authorized; distinct from `available` (network reachability).
_REMOTE_CONTROL = HonBinarySensorEntityDescription(
    key="remote_control",
    icon="mdi:remote",
    attr_key="remoteCtrValid",            # "1" = remote control authorized
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
)
_UNIVERSAL_GATED: tuple["HonBinarySensorEntityDescription", ...] = (_REMOTE_CONTROL,)


def _g_running(key: str, attr: str) -> "HonBinarySensorEntityDescription":
    """Gated read-only RUNNING binary for an `option engaged` (0/1) flag."""
    return HonBinarySensorEntityDescription(
        key=key, attr_key=attr, device_class=BinarySensorDeviceClass.RUNNING,
    )


# Per-type sets (candidates; the capability-gate drops those not present on the device).
# The option flags (night_wash/steam/energy_saving) are gvigroux-live-tested 0/1
# params, gated by the universal binary gate so they appear only where reported.
_WASH_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    _DOOR_OPEN, _DOOR_LOCK, _CHILD_LOCK, _DRUM_CLEAN, _FILTER_CLEAN, _DRY_CLEAN,
    _g_running("night_wash", "nightWashStatus"),
    _g_running("steam", "steamStatus"),
    HonBinarySensorEntityDescription(
        key="energy_saving", attr_key="energySavingStatus", icon="mdi:leaf",
    ),
)
_DRY_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    _DOOR_OPEN, _DOOR_LOCK, _CHILD_LOCK,
)


# --- Tier 2: binary sensors (capability-gated like all binary sensors) -------
# Inline keys = hOn parameter names (0/1 telemetry) of the types mapped but not
# validated live. The per-attribute gate (already active for all binary sensors)
# automatically drops those that a given model does not report.


def _door(key: str, attr: str, translation_key=None) -> HonBinarySensorEntityDescription:
    return HonBinarySensorEntityDescription(
        key=key, attr_key=attr, translation_key=translation_key,
        device_class=BinarySensorDeviceClass.DOOR,
    )


# Fridge / fridge-freezer / freezer (REF/FR/FRE).
_COOLING_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    _door("door_zone1", "doorStatusZ1"),
    _door("door2_zone1", "door2StatusZ1"),
    _door("door_zone2", "doorStatusZ2"),
    _door("door_zone3", "doorStatusZ3"),
    HonBinarySensorEntityDescription(
        key="ice_maker",
        icon="mdi:snowflake",
        attr_key="icemakerOnOffStatus",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    HonBinarySensorEntityDescription(
        key="ice_box_full",
        attr_key="icemakerIceboxFullStatus",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    HonBinarySensorEntityDescription(
        key="energy_saving",
        icon="mdi:leaf",
        attr_key="energySavingStatus",
    ),
    # Active-mode flags (0/1). Read-only mirrors of the boost/special modes; the
    # engine also folds these into the derived modeZ1/modeZ2 (ref.py). Live-confirmed
    # present on the real fridge (quickModeZ1/quickModeZ2/intelligenceMode/holidayMode).
    HonBinarySensorEntityDescription(
        key="quick_cool",
        icon="mdi:snowflake",
        attr_key="quickModeZ1",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    HonBinarySensorEntityDescription(
        key="quick_freeze",
        icon="mdi:snowflake-variant",
        attr_key="quickModeZ2",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    HonBinarySensorEntityDescription(
        key="auto_set",
        icon="mdi:auto-mode",
        attr_key="intelligenceMode",
    ),
    HonBinarySensorEntityDescription(
        key="holiday_mode",
        icon="mdi:palm-tree",
        attr_key="holidayMode",
    ),
)

# Oven (OV).
_OVEN_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    _door("door_open", "doorStatus"),
    _door("door_zone1", "doorStatusZ1", translation_key="door_cavity1"),
    _door("door_zone2", "doorStatusZ2", translation_key="door_cavity2"),
    HonBinarySensorEntityDescription(
        key="preheat",
        icon="mdi:thermometer-chevron-up",
        attr_key="preheatStatus",
        # preheatStatus is 0=idle / 1=preheating / 2=ready; on == "1" (heating in
        # progress), matching the app's `=='1'` test. HEAT fits the semantics.
        device_class=BinarySensorDeviceClass.HEAT,
    ),
)

# Dishwasher (DW): door + program-option flags (live-confirmed on real DW, gated).
_DISHWASHER_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    _door("door_open", "doorStatus"),
    _g_running("extra_dry", "extraDry"),
    _g_running("half_load", "halfLoad"),
    _g_running("auto_open_door", "openDoor"),
    _g_running("eco_express", "ecoExpress"),
)

# Wine cellar (WC).
_WINE_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    HonBinarySensorEntityDescription(
        key="light",
        attr_key="lightStatus",
        device_class=BinarySensorDeviceClass.LIGHT,
    ),
    HonBinarySensorEntityDescription(
        key="presence",
        attr_key="humanSensingResult",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
    ),
)

# The zone index the per-zone hob families are generated over. Kept as the six the
# pan sensors have always used: the app's own IH parameter list runs to Z6, and the
# per-attribute gate is what reduces it to the four a HA2MTSJ68MC reports.
_HOB_ZONES = range(1, 7)

# Hob (IH/HOB): everything the shadow says about each cooking zone, plus the
# panel lock.
#
# `child_lock` REUSES the wash group's description object rather than declaring a
# hob-specific twin. It is the same `lockStatus` parameter with the same meaning,
# so it gets the same key, the same icon and the same translation -- and no
# device_class: BinarySensorDeviceClass.LOCK is inverted in Home Assistant (on
# means UNlocked), so a hob reporting lockStatus=1 would display as unlocked.
_HOB_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    *(
        HonBinarySensorEntityDescription(
            key=f"pan_zone{z}",
            icon="mdi:pot-steam",
            attr_key=f"panStatusZ{z}",
        )
        for z in _HOB_ZONES
    ),
    # Whether the zone is switched on at all. Distinct from `pan_zone*`, which
    # says only that the hob can see cookware on the ring.
    *(
        HonBinarySensorEntityDescription(
            key=f"zone_on_zone{z}",
            attr_key=f"onOffStatusZ{z}",
            device_class=BinarySensorDeviceClass.RUNNING,
        )
        for z in _HOB_ZONES
    ),
    # Residual heat: the "H" the panel shows after a zone is switched off. This is
    # the reading issue #84 asks for by name.
    *(
        HonBinarySensorEntityDescription(
            key=f"hot_zone{z}",
            attr_key=f"hotStatusZ{z}",
            device_class=BinarySensorDeviceClass.HEAT,
        )
        for z in _HOB_ZONES
    ),
    # Per-zone fault. `has_problem` and NOT the platform's `on_value`
    # comparison: the hob spells "no error" as 0 AND as the app's zero-padded
    # "00", and the client folds "01" to "1" on the way in, so a comparison
    # against a raw code would light a PROBLEM permanently on all four zones.
    *(
        HonBinarySensorEntityDescription(
            key=f"error_zone{z}",
            attr_key=f"errorZ{z}",
            device_class=BinarySensorDeviceClass.PROBLEM,
            value_fn=has_problem,
        )
        for z in _HOB_ZONES
    ),
    # Flex zones bridged into one cooking surface. Behind the experimental option:
    # the value is 0 on every zone of the only hob we have, and the app reads 1
    # and 2 as two different halves of a bridged pair, a distinction a boolean
    # cannot carry and nothing here can verify.
    *(
        HonBinarySensorEntityDescription(
            key=f"combi_mode_zone{z}",
            icon="mdi:link-variant",
            attr_key=f"combiModeZ{z}",
            value_fn=lambda raw: str(raw).strip() not in ("", "0", "0.0"),
            experimental=True,
        )
        for z in _HOB_ZONES
    ),
    _CHILD_LOCK,
)

def _hood_filter_cleaning(raw) -> bool | None:
    """True while the hood reports a filter-cleaning cycle in progress.

    Its own reader because this is the one hood flag the device spells as a
    TEXTUAL boolean: the reporting HADG6DS46BWIFI publishes the string "false",
    not 0/1, so the platform's default `raw == on_value` comparison would read
    every value -- "true" included -- as off. Both spellings are accepted, and an
    unrecognized one reads as unknown rather than being forced to a side: the
    decompiled app never touches this attribute, so there is no third spelling we
    can claim to know the meaning of.
    """
    text = str(raw).strip().lower()
    if text in ("1", "true", "on", "yes"):
        return True
    if text in ("0", "false", "off", "no"):
        return False
    return None


# Hood (HO). `filter_clean_needed` (filterCleaningAlarmStatus) and `filter_cleaning`
# (filterCleaningStatus) are DELIBERATELY two entities: the first is the alarm flag
# the settings command declares as a fixed "1", the second the cycle-in-progress
# flag, and on the reporting hood they have not moved together since 2023. Folding
# them would assert an equivalence nothing in the app or the dump supports.
_HOOD_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    HonBinarySensorEntityDescription(
        key="light",
        attr_key="lightStatus",
        device_class=BinarySensorDeviceClass.LIGHT,
    ),
    HonBinarySensorEntityDescription(
        key="filter_clean_needed",
        attr_key="filterCleaningAlarmStatus",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    HonBinarySensorEntityDescription(
        key="filter_cleaning",
        icon="mdi:air-filter",
        attr_key="filterCleaningStatus",
        value_fn=_hood_filter_cleaning,
        # An unreadable spelling hides the entity instead of claiming "not
        # cleaning": see _hood_filter_cleaning.
        unavailable_when_unmapped=True,
    ),
    # The appliance's own on/off flag: on this hood `onOffStatus` is the lit-or-dark
    # control panel, which the device treats as its power state -- while it is 0 the
    # hood ignores every speed and light command it receives. Read from there rather
    # than from windSpeed, which the fan entity already publishes and which answers
    # the narrower question "is it extracting right now".
    #
    # The power SWITCH added for the same field is a control, not a duplicate of
    # this reading: it is the entity that writes the flag, and it can be gated away
    # on a hood that declares no startProgram while this sensor still reports.
    HonBinarySensorEntityDescription(
        key="running",
        attr_key="onOffStatus",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
)

# Air conditioner (AC): status alarms/processes (0/1). Capability-gated like every
# binary sensor. Live-confirmed present on the real AC (filterChangeStatusLocal,
# ch2oCleaningStatus).
_AC_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    HonBinarySensorEntityDescription(
        key="filter_change",
        attr_key="filterChangeStatusLocal",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    HonBinarySensorEntityDescription(
        key="ch2o_cleaning",
        icon="mdi:molecule",
        attr_key="ch2oCleaningStatus",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
)

# Water heater (WH).
_WATER_HEATER_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    HonBinarySensorEntityDescription(
        key="light",
        translation_key="indicator_light",
        attr_key="lightStatus",
        device_class=BinarySensorDeviceClass.LIGHT,
    ),
    HonBinarySensorEntityDescription(
        key="child_lock",
        icon="mdi:lock-alert",
        attr_key="lockStatus",
    ),
)

# Air purifier (AP). Both signals are reported and meaningful with the purifier
# stopped, so neither is gated on power (unlike the AP environmental sensors).
_AIR_PURIFIER_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    HonBinarySensorEntityDescription(
        key="eco_active",
        attr_key="ecoModeStatus",       # AP_PARAMS_ENUM; 1 = eco engaged
        icon="mdi:leaf",
        value_fn=is_engaged,
    ),
    # Derived through has_problem() rather than compared to one literal: the
    # device spells "no error" as zero, "00" and "100" interchangeably, so a
    # single on_value would report a healthy purifier as faulty.
    HonBinarySensorEntityDescription(
        key="problem",
        attr_key="errors",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=has_problem,
    ),
    # EXPERIMENTAL interpretation of the raw `coLevel` the AP sensor table already
    # reports. Only the alarming value is known, so this is on for that value and
    # UNAVAILABLE for everything else: it never reports an all-clear. Diagnostic
    # category and deliberately NO device class -- BinarySensorDeviceClass.SAFETY
    # would present it as a certified detector, which it is not.
    HonBinarySensorEntityDescription(
        key="co_alarm",
        attr_key="coLevel",
        icon="mdi:molecule-co",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=co_alarm,
        experimental=True,
        unavailable_when_unmapped=True,
    ),
)

BINARY_SENSORS: dict[str, tuple[HonBinarySensorEntityDescription, ...]] = {
    APPLIANCE_AP: _AIR_PURIFIER_BINARY,
    APPLIANCE_WM: _WASH_BINARY,
    APPLIANCE_WD: _WASH_BINARY,
    APPLIANCE_TD: _DRY_BINARY,
    APPLIANCE_AC: _AC_BINARY,
    # Tier 2 (read-only). FR/FRE reuse the fridge set, HOB the hob set.
    APPLIANCE_REF: _COOLING_BINARY,
    APPLIANCE_FR: _COOLING_BINARY,
    APPLIANCE_FRE: _COOLING_BINARY,
    APPLIANCE_OV: _OVEN_BINARY,
    APPLIANCE_DW: _DISHWASHER_BINARY,
    APPLIANCE_WC: _WINE_BINARY,
    APPLIANCE_IH: _HOB_BINARY,
    APPLIANCE_HOB: _HOB_BINARY,
    APPLIANCE_HO: _HOOD_BINARY,
    APPLIANCE_WH: _WATER_HEATER_BINARY,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create the binary sensors only for the keys actually exposed by the device."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    entities: list[BinarySensorEntity] = []
    data_map = coordinator_data_map(coordinator)
    experimental = bool(entry.options.get(CONF_ENABLE_EXPERIMENTAL, False))
    for appliance_id, data in data_map.items():
        app_type = data.get("type", "")
        attributes = data.get("attributes", {})
        attributes = attributes if isinstance(attributes, dict) else {}
        created: list[str] = []
        for description in BINARY_SENSORS.get(app_type, ()):
            # Inferred from incomplete evidence: absent unless explicitly enabled.
            if description.experimental and not experimental:
                continue
            if description.attr_key not in attributes:
                _LOGGER.debug(
                    "Binary debug: skip '%s' on '%s' id=%s (key '%s' absent)",
                    description.key, data.get("name"), redact_id(appliance_id), description.attr_key,
                )
                continue
            entities.append(HonBinarySensor(coordinator, appliance_id, description))
            created.append(description.key)
        # Universal capability-gated binaries (any type that reports the attr).
        for description in _UNIVERSAL_GATED:
            if description.attr_key not in attributes:
                continue
            entities.append(HonBinarySensor(coordinator, appliance_id, description))
            created.append(description.key)
        # Connectivity: universal (every type, even those without a per-type set) and not
        # capability-gated: it must always exist to signal the connection state.
        entities.append(HonConnectivityBinarySensor(coordinator, appliance_id, _CONNECTIVITY))
        created.append(_CONNECTIVITY.key)
        _LOGGER.debug(
            "Binary debug: '%s' (type=%s, id=%s) -> %d binary sensors %s",
            data.get("name"), app_type, redact_id(appliance_id), len(created), created,
        )
    # Account-level diagnostic binary sensor (one per config entry).
    sw_version = entry_data.get("integration_version")
    entities.append(HonUpdateOkBinarySensor(coordinator, entry, sw_version))
    async_add_entities(entities)


class HonBinarySensor(HonBaseEntity, BinarySensorEntity):
    """Haier hOn binary sensor driven by HonBinarySensorEntityDescription."""

    entity_description: HonBinarySensorEntityDescription

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        description: HonBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, appliance_id)
        self.entity_description = description
        self._attr_translation_key = description.translation_key or description.key
        self._attr_unique_id = f"{appliance_id}_{description.key}"

    @property
    def available(self) -> bool:
        """Base rules first; an uninterpretable reading then hides the entity.

        Only descriptions that opt in are affected (the flag defaults off), so no
        existing binary changes behavior.
        """
        if not super().available:
            return False
        if self.entity_description.unavailable_when_unmapped and self.is_on is None:
            return False
        return True

    @property
    def is_on(self) -> bool | None:
        raw = self._get_attr(self.entity_description.attr_key)
        if raw is None:
            return None
        value_fn = self.entity_description.value_fn
        if value_fn is not None:
            return value_fn(raw)
        return str(raw) == self.entity_description.on_value


class HonConnectivityBinarySensor(HonBinarySensor):
    """Device connectivity. ALWAYS available (even if the device is offline): it must
    be able to signal 'disconnected'. `on` = connected. Bypasses the availability gate
    of base_entity (which would mark it unavailable exactly when it is needed)."""

    @property
    def available(self) -> bool:
        # no connectivity gate: it is enough that the coordinator is ok and the appliance present
        return self._present

    @property
    def is_on(self) -> bool | None:
        # `available` is a bool (from the engine), not a raw "1"/"0": read it directly
        val = self._attributes.get("available")
        return None if val is None else bool(val)


class HonUpdateOkBinarySensor(HonAccountCoordinatorEntity, BinarySensorEntity):
    """Whether the last coordinator refresh succeeded (account diagnostics)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "update_ok"

    def __init__(self, coordinator, entry, sw_version: str | None = None) -> None:
        super().__init__(coordinator, entry, "update_ok", sw_version)

    @property
    def is_on(self) -> bool:
        return bool(getattr(self.coordinator, "last_update_success", True))
