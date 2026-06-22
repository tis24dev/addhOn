"""Haier hOn binary sensors (wash group): door, locks, maintenance alarms.

The entities are CAPABILITY-GATED: a description is created only if the device
actually exposes that attribute (present in coordinator.data[id]["attributes"]),
so no perpetually "unknown" entities show up on models that do not report it
(e.g. doorLockStatus is not guaranteed on the tumble dryer). All the keys used
here are direct 0/1 attributes, confirmed live on HW80 (washer) / HD100 (dryer).
"""
from __future__ import annotations

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

from .base_entity import HonAccountCoordinatorEntity, HonBaseEntity
from .const import (
    APPLIANCE_AC,
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
    DOMAIN,
    WM_ATTR_CHILD_LOCK,
    WM_ATTR_DOOR,
    WM_ATTR_DOOR_OPEN,
    WM_ATTR_DRUM_CLEAN,
    WM_ATTR_DRY_CLEAN_NEEDED,
    WM_ATTR_FILTER_CLEAN,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HonBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Description of a Haier hOn binary sensor.

    - `key` = unique_id suffix (new, no historic entity uses these suffixes).
    - `attr_key` = key read via HonBaseEntity._get_attr.
    - `on_value` = raw value that corresponds to the "on" state (default "1").
    """

    attr_key: str
    on_value: str = "1"


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

# Hob (IH/HOB): pan detected per zone.
_HOB_BINARY: tuple[HonBinarySensorEntityDescription, ...] = tuple(
    HonBinarySensorEntityDescription(
        key=f"pan_zone{z}",
        icon="mdi:pot-steam",
        attr_key=f"panStatusZ{z}",
    )
    for z in range(1, 7)
)

# Hood (HO).
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

BINARY_SENSORS: dict[str, tuple[HonBinarySensorEntityDescription, ...]] = {
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
    for appliance_id, data in coordinator.data.items():
        app_type = data.get("type", "")
        attributes = data.get("attributes", {})
        attributes = attributes if isinstance(attributes, dict) else {}
        created: list[str] = []
        for description in BINARY_SENSORS.get(app_type, ()):
            if description.attr_key not in attributes:
                _LOGGER.debug(
                    "Binary debug: skip '%s' on '%s' id=%s (key '%s' absent)",
                    description.key, data.get("name"), appliance_id, description.attr_key,
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
            data.get("name"), app_type, appliance_id, len(created), created,
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
    def is_on(self) -> bool | None:
        raw = self._get_attr(self.entity_description.attr_key)
        if raw is None:
            return None
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
