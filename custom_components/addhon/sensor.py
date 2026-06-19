"""Haier hOn sensors, defined per appliance type via a description table.

The sensor set depends on the type (AC / WM / WD / TD): the washing machine (WM)
and the washer-dryer (WD) have the water + energy sensors; the tumble dryer (TD)
does NOT use water and does not expose those counters, so it only gets state,
remaining time and cycles (from programsCounter). The air conditioner (AC) has
temperatures, humidity, compressor frequency and energy.

CONSTRAINT: the `key` of each description matches the SUFFIX of the historic
unique_id (e.g. "temp_indoor", "total_energy", "state", "total_washes"): it must
NOT be changed, otherwise already-registered entities would be duplicated/orphaned.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity
from .const import (
    APPLIANCE_AC,
    APPLIANCE_DW,
    APPLIANCE_FR,
    APPLIANCE_FRE,
    APPLIANCE_HO,
    APPLIANCE_HOB,
    APPLIANCE_IH,
    APPLIANCE_KT,
    APPLIANCE_OV,
    APPLIANCE_REF,
    APPLIANCE_RVC,
    APPLIANCE_TD,
    APPLIANCE_WC,
    APPLIANCE_WD,
    APPLIANCE_WH,
    APPLIANCE_WM,
    AC_ATTR_CH2O,
    AC_ATTR_CO2,
    AC_ATTR_COMPRESSOR_FREQ,
    AC_ATTR_CURRENT_TEMP,
    AC_ATTR_HUMIDITY_INDOOR,
    AC_ATTR_OUTDOOR_TEMP,
    AC_ATTR_PM25,
    AC_ATTR_TOTAL_ENERGY,
    DOMAIN,
    DW_LEVEL_MAP,
    MACHINE_MODE_MAP,
    RVC_POWER_MAP,
    RVC_STATE_MAP,
    TD_ATTR_CYCLES,
    TUMBLE_DRYER_PHASE_MAP,
    WASHING_PHASE_MAP,
    WH_PHASE_MAP,
    WM_ATTR_CURRENT_ENERGY,
    WM_ATTR_CURRENT_WATER,
    WM_ATTR_DELAY,
    WM_ATTR_DIRT_LEVEL,
    WM_ATTR_DRY_LEVEL,
    WM_ATTR_ERRORS,
    WM_ATTR_LOADING,
    WM_ATTR_PROGRAM_NAME,
    WM_ATTR_PROGRAM_PHASE,
    WM_ATTR_REMAINING,
    WM_ATTR_SPIN_SPEED,
    WM_ATTR_STATUS,
    WM_ATTR_TEMP,
    WM_ATTR_TOTAL_ENERGY,
    WM_ATTR_TOTAL_WASH,
    WM_ATTR_TOTAL_WATER,
    WM_STATE_MAP,
)

_LOGGER = logging.getLogger(__name__)


def _wm_state(raw) -> str:
    """Translate machMode into the state text (historic behavior, unchanged)."""
    if raw is None:
        return "Non disponibile"
    code = str(raw)
    return WM_STATE_MAP.get(code, f"Sconosciuto ({code})")


@dataclass(frozen=True, kw_only=True)
class HonSensorEntityDescription(SensorEntityDescription):
    """Description of a Haier hOn sensor.

    - `key` = historic unique_id suffix (do NOT modify).
    - `attr_key` = pyhOn attribute key read via HonBaseEntity._get_attr.
    - `value_fn` optional, transforms the raw value (e.g. a textual state map);
      without value_fn the value is converted to float (None if not numeric).
    - `gated` = if True the sensor is CAPABILITY-GATED: it is created only if the
      device actually exposes `attr_key` (present in coordinator.data[id]
      ["attributes"]). Used for the Tier 2 types, mapped from the app but not
      validated live, so a missing parameter does not produce an "unknown" entity.
      The historic types (AC/WM/WD/TD) stay gated=False (always created).
    """

    attr_key: str
    value_fn: Callable[[object], object] | None = None
    gated: bool = False


# State + remaining time: identical for washer/washer-dryer/tumble dryer.
_STATE = HonSensorEntityDescription(
    key="state",
    icon="mdi:washing-machine",
    attr_key=WM_ATTR_STATUS,
    value_fn=_wm_state,
)
_REMAINING = HonSensorEntityDescription(
    key="remaining_time",
    attr_key=WM_ATTR_REMAINING,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    device_class=SensorDeviceClass.DURATION,
)

# Consumption sensors for washer/washer-dryer (they use water + energy).
_WASH_CONSUMPTION: tuple[HonSensorEntityDescription, ...] = (
    HonSensorEntityDescription(
        key="total_washes",
        attr_key=WM_ATTR_TOTAL_WASH,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    HonSensorEntityDescription(
        key="total_water",
        attr_key=WM_ATTR_TOTAL_WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    HonSensorEntityDescription(
        key="total_energy",
        attr_key=WM_ATTR_TOTAL_ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    HonSensorEntityDescription(
        key="current_energy",
        attr_key=WM_ATTR_CURRENT_ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
    ),
    HonSensorEntityDescription(
        key="current_water",
        attr_key=WM_ATTR_CURRENT_WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,
    ),
)

# Extra sensors for the wash group (keys confirmed live on HW80 / HD100).
# `program_name` is text (no float conversion); the dirt/dry levels are raw
# integer values (labels deferred to a later step).
def _as_text(raw) -> str | None:
    return None if raw is None else str(raw)


def _phase_wash(raw) -> str | None:
    """prPhase -> phase label (washer/washer-dryer)."""
    if raw is None:
        return None
    return WASHING_PHASE_MAP.get(str(raw), f"Fase {raw}")


def _phase_dry(raw) -> str | None:
    """prPhase -> phase label (tumble dryer)."""
    if raw is None:
        return None
    return TUMBLE_DRYER_PHASE_MAP.get(str(raw), f"Fase {raw}")


_PROGRAM_NAME = HonSensorEntityDescription(
    key="program_name",
    icon="mdi:format-list-bulleted",
    attr_key=WM_ATTR_PROGRAM_NAME,
    value_fn=_as_text,
)
_PHASE_WASH = HonSensorEntityDescription(
    key="program_phase",
    icon="mdi:washing-machine",
    attr_key=WM_ATTR_PROGRAM_PHASE,
    value_fn=_phase_wash,
)
_PHASE_DRY = HonSensorEntityDescription(
    key="program_phase",
    icon="mdi:tumble-dryer",
    attr_key=WM_ATTR_PROGRAM_PHASE,
    value_fn=_phase_dry,
)
_ERRORS = HonSensorEntityDescription(
    key="errors",
    icon="mdi:alert-circle-outline",
    attr_key=WM_ATTR_ERRORS,
    value_fn=_as_text,
)
_DELAY = HonSensorEntityDescription(
    key="delay_time",
    icon="mdi:timer-sand",
    attr_key=WM_ATTR_DELAY,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    device_class=SensorDeviceClass.DURATION,
)
_LOADING = HonSensorEntityDescription(
    key="loading_percentage",
    icon="mdi:weight",
    attr_key=WM_ATTR_LOADING,
    native_unit_of_measurement="%",
    state_class=SensorStateClass.MEASUREMENT,
)
_DRY_LEVEL = HonSensorEntityDescription(
    key="dry_level",
    icon="mdi:tumble-dryer",
    attr_key=WM_ATTR_DRY_LEVEL,
)
# Washer/washer-dryer only (wash side).
_WASH_EXTRA: tuple[HonSensorEntityDescription, ...] = (
    HonSensorEntityDescription(
        key="spin_speed",
        icon="mdi:rotate-3d-variant",
        attr_key=WM_ATTR_SPIN_SPEED,
        native_unit_of_measurement="rpm",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="wash_temperature",
        attr_key=WM_ATTR_TEMP,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="dirty_level",
        icon="mdi:liquid-spot",
        attr_key=WM_ATTR_DIRT_LEVEL,
    ),
)

# Washer (WM): state/time + program + wash extras + load/delay + consumption.
_WASHER: tuple[HonSensorEntityDescription, ...] = (
    _STATE, _REMAINING, _PROGRAM_NAME, _PHASE_WASH, *_WASH_EXTRA, _LOADING, _DELAY,
    _ERRORS, *_WASH_CONSUMPTION,
)
# Washer-dryer (WD = WM + drying): like the washer + dry level.
_WASHER_DRYER: tuple[HonSensorEntityDescription, ...] = (
    _STATE, _REMAINING, _PROGRAM_NAME, _PHASE_WASH, *_WASH_EXTRA, _DRY_LEVEL, _LOADING,
    _DELAY, _ERRORS, *_WASH_CONSUMPTION,
)

# Tumble dryer: no water/energy (hOn does not expose them for the TD). The cycles
# reuse the "total_washes" suffix but read programsCounter, so the already-
# registered entity (previously always empty on totalWashCycle) is re-pointed to a
# real value without changing entity_id.
_DRYER: tuple[HonSensorEntityDescription, ...] = (
    _STATE,
    _REMAINING,
    _PROGRAM_NAME,
    _PHASE_DRY,
    _DRY_LEVEL,
    _LOADING,
    _DELAY,
    _ERRORS,
    HonSensorEntityDescription(
        key="total_washes",
        attr_key=TD_ATTR_CYCLES,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)

# Air conditioner. ENERGY NOTE: hOn does NOT provide cumulative kWh for AS-class
# ACs (totalElectricityUsed reports 0 from the device itself, it is not a
# placeholder of ours). We keep the sensor anyway (useful on ACs that do report
# it); for real energy an external meter is needed.
_AC: tuple[HonSensorEntityDescription, ...] = (
    HonSensorEntityDescription(
        key="temp_indoor",
        attr_key=AC_ATTR_CURRENT_TEMP,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="temp_outdoor",
        attr_key=AC_ATTR_OUTDOOR_TEMP,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="humidity_indoor",
        attr_key=AC_ATTR_HUMIDITY_INDOOR,
        native_unit_of_measurement="%",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="compressor_freq",
        attr_key=AC_ATTR_COMPRESSOR_FREQ,
        native_unit_of_measurement="Hz",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="total_energy",
        translation_key="ac_total_energy",
        attr_key=AC_ATTR_TOTAL_ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    HonSensorEntityDescription(
        key="pm25",
        attr_key=AC_ATTR_PM25,
        native_unit_of_measurement="µg/m³",
        device_class=SensorDeviceClass.PM25,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="co2",
        attr_key=AC_ATTR_CO2,
        native_unit_of_measurement="ppm",
        device_class=SensorDeviceClass.CO2,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="ch2o",
        icon="mdi:molecule",
        attr_key=AC_ATTR_CH2O,
        native_unit_of_measurement="mg/m³",
        state_class=SensorStateClass.MEASUREMENT,
    ),
)

# ─── Tier 2: read-only sensors (capability-gated) ────────────────────────────
# Types mapped from the official app but not validated on real devices. Each
# description has gated=True: the entity is created only if the device exposes
# the attribute (see async_setup_entry). The `attr_key` values are the hOn
# parameter names (direct telemetry), used only once here, so they stay inline
# strings (unlike the historic types, which share keys across several platforms).


def _mapped(mapping: dict[str, str], prefix: str) -> Callable[[object], object]:
    """Build a value_fn that translates the raw value via `mapping`.

    None value -> None; value not in the map -> "<prefix> <raw>" (so an
    unexpected code stays visible instead of disappearing)."""

    def _fn(raw):
        if raw is None:
            return None
        return mapping.get(str(raw), f"{prefix} {raw}")

    return _fn


def _g_temp(key: str, attr: str) -> HonSensorEntityDescription:
    return HonSensorEntityDescription(
        key=key,
        attr_key=attr,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    )


def _g_minutes(key: str, attr: str) -> HonSensorEntityDescription:
    return HonSensorEntityDescription(
        key=key,
        attr_key=attr,
        icon="mdi:timer-outline",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        gated=True,
    )


def _g_text(key: str, attr: str, icon: str | None = None,
            value_fn: Callable[[object], object] | None = _as_text) -> HonSensorEntityDescription:
    return HonSensorEntityDescription(
        key=key, attr_key=attr, icon=icon, value_fn=value_fn, gated=True,
    )


# Fridge / fridge-freezer / freezer (REF/FR/FRE): per-zone temperatures +
# ambient. Doors / ice-maker / eco are binary sensors (binary_sensor.py).
_COOLING: tuple[HonSensorEntityDescription, ...] = (
    _g_temp("temp_zone1", "tempZ1"),
    _g_temp("temp_zone2", "tempZ2"),
    _g_temp("temp_zone3", "tempZ3"),
    _g_temp("temp_zone4", "tempZ4"),
    _g_temp("temp_upper", "tempUZ"),
    _g_temp("temp_lower", "tempLZ"),
    _g_temp("temp_ambient", "tempEnv"),
    HonSensorEntityDescription(
        key="humidity_ambient",
        attr_key="humidityEnv",
        native_unit_of_measurement="%",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
)

# Oven (OV): state, cavity temperature, remaining time, meat probes.
_OVEN: tuple[HonSensorEntityDescription, ...] = (
    _g_text("state", "machMode", icon="mdi:stove",
            value_fn=_mapped(MACHINE_MODE_MAP, "Modo")),
    _g_temp("temp_cavity", "temp"),
    _g_minutes("remaining_time", "remainingTimeMM"),
    _g_temp("probe_temp_1", "tempEmployedProbe1"),
    _g_temp("probe_temp_2", "tempEmployedProbe2"),
)

# Dishwasher (DW): state, program, time, salt/rinse-aid levels,
# temperature, errors. The door is a binary sensor.
_DISHWASHER: tuple[HonSensorEntityDescription, ...] = (
    _g_text("state", "machMode", icon="mdi:dishwasher",
            value_fn=_mapped(MACHINE_MODE_MAP, "Modo")),
    _g_text("program_name", "programName", icon="mdi:format-list-bulleted"),
    _g_minutes("remaining_time", "remainingTimeMM"),
    _g_text("salt_level", "saltStatus", icon="mdi:shaker-outline",
            value_fn=_mapped(DW_LEVEL_MAP, "Livello")),
    _g_text("rinse_aid_level", "rinseAidStatus",
            icon="mdi:water-opacity", value_fn=_mapped(DW_LEVEL_MAP, "Livello")),
    HonSensorEntityDescription(
        key="wash_temperature",
        attr_key="temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
    _g_text("errors", "errors", icon="mdi:alert-circle-outline"),
)

# Wine cellar (WC): ambient + zone temperature. Light/presence are binary.
_WINE: tuple[HonSensorEntityDescription, ...] = (
    _g_temp("temp_ambient", "tempEnv"),
    _g_temp("temp_zone2", "tempZ2"),
    _g_minutes("remaining_time", "remainingTimeMM"),
)

# Induction hob (IH/HOB): temperature per cooking zone. Pan detection
# is a binary sensor.
_HOB: tuple[HonSensorEntityDescription, ...] = (
    _g_temp("temp_zone1", "sensorTempZ1"),
    _g_temp("temp_zone2", "sensorTempZ2"),
    _g_temp("temp_zone3", "sensorTempZ3"),
    _g_temp("temp_zone4", "sensorTempZ4"),
    _g_temp("temp_zone5", "sensorTempZ5"),
)

# Hood (HO): fan speed. Light/filter alarm are binary sensors.
_HOOD: tuple[HonSensorEntityDescription, ...] = (
    HonSensorEntityDescription(
        key="fan_speed",
        attr_key="windSpeed",
        icon="mdi:fan",
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
)

# Coffee machine / kettle (KT): instantaneous power + cycle counters.
_COFFEE: tuple[HonSensorEntityDescription, ...] = (
    HonSensorEntityDescription(
        key="current_power",
        attr_key="currentPower",
        native_unit_of_measurement="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
    HonSensorEntityDescription(
        key="descaling_cycles",
        attr_key="descalingCycleCounter",
        icon="mdi:counter",
        gated=True,
    ),
    HonSensorEntityDescription(
        key="lifetime_cycles",
        attr_key="lifetimeCycleCounter",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        gated=True,
    ),
)

# Water heater (WH): water/inlet/outlet temperatures, power, available
# volume, time to target, phase. Light/lock are binary sensors.
_WATER_HEATER: tuple[HonSensorEntityDescription, ...] = (
    _g_temp("water_temp", "temp"),
    _g_temp("temp_inlet", "tempIn"),
    _g_temp("temp_outlet", "tempOut"),
    HonSensorEntityDescription(
        key="power",
        attr_key="power",
        native_unit_of_measurement="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
    HonSensorEntityDescription(
        key="water_volume",
        attr_key="waterVolume",
        icon="mdi:water",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
    _g_minutes("heating_remaining", "remainingTimeMMHeating"),
    _g_text("program_phase", "prPhase", icon="mdi:water-boiler",
            value_fn=_mapped(WH_PHASE_MAP, "Fase")),
)

# Robot vacuum (RVC): battery, state, time, power, areas, errors.
_VACUUM: tuple[HonSensorEntityDescription, ...] = (
    HonSensorEntityDescription(
        key="battery",
        attr_key="batteryStatus",
        native_unit_of_measurement="%",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
    _g_text("state", "prPhase", icon="mdi:robot-vacuum",
            value_fn=_mapped(RVC_STATE_MAP, "Stato")),
    _g_minutes("remaining_time", "remainingTimeMM"),
    _g_text("power_mode", "power", icon="mdi:fan",
            value_fn=_mapped(RVC_POWER_MAP, "Potenza")),
    HonSensorEntityDescription(
        key="last_work_area",
        attr_key="lastWorkArea",
        icon="mdi:ruler-square",
        native_unit_of_measurement="m²",
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
    HonSensorEntityDescription(
        key="total_work_area",
        attr_key="totalWorkArea",
        icon="mdi:ruler-square",
        native_unit_of_measurement="m²",
        state_class=SensorStateClass.TOTAL_INCREASING,
        gated=True,
    ),
    _g_text("errors", "errors", icon="mdi:alert-circle-outline"),
)

SENSORS: dict[str, tuple[HonSensorEntityDescription, ...]] = {
    APPLIANCE_AC: _AC,
    APPLIANCE_WM: _WASHER,
    APPLIANCE_WD: _WASHER_DRYER,
    APPLIANCE_TD: _DRYER,
    # Tier 2 (read-only, capability-gated). FR/FRE reuse the fridge set, HOB
    # reuses the hob set (alias codes for the same device).
    APPLIANCE_REF: _COOLING,
    APPLIANCE_FR: _COOLING,
    APPLIANCE_FRE: _COOLING,
    APPLIANCE_OV: _OVEN,
    APPLIANCE_DW: _DISHWASHER,
    APPLIANCE_WC: _WINE,
    APPLIANCE_IH: _HOB,
    APPLIANCE_HOB: _HOB,
    APPLIANCE_HO: _HOOD,
    APPLIANCE_KT: _COFFEE,
    APPLIANCE_WH: _WATER_HEATER,
    APPLIANCE_RVC: _VACUUM,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create the sensors based on the type of each appliance."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[SensorEntity] = []
    for appliance_id, data in coordinator.data.items():
        app_type = data.get("type", "")
        attributes = data.get("attributes", {})
        attributes = attributes if isinstance(attributes, dict) else {}
        descriptions = SENSORS.get(app_type, ())
        created: list[str] = []
        for description in descriptions:
            # Capability-gating (Tier 2 only): skip the sensors whose attribute
            # is not exposed by the device. The historic types (gated=False) stay
            # always created, as before.
            if description.gated and description.attr_key not in attributes:
                continue
            entities.append(HonSensor(coordinator, appliance_id, description))
            created.append(description.key)
        _LOGGER.debug(
            "Sensor debug: '%s' (type=%s, id=%s) -> %d/%d sensors %s",
            data.get("name", "Haier"),
            app_type,
            appliance_id,
            len(created),
            len(descriptions),
            created,
        )
    async_add_entities(entities)


class HonSensor(HonBaseEntity, SensorEntity):
    """Haier hOn sensor driven by HonSensorEntityDescription."""

    entity_description: HonSensorEntityDescription

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        description: HonSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, appliance_id)
        self.entity_description = description
        self._attr_translation_key = description.translation_key or description.key
        self._attr_unique_id = f"{appliance_id}_{description.key}"

    @property
    def native_value(self):
        raw = self._get_attr(self.entity_description.attr_key)
        value_fn = self.entity_description.value_fn
        if value_fn is not None:
            return value_fn(raw)
        if raw is None:
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None
