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
import math

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
    STAIN_TYPE_MAP,
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


def _wm_state(raw) -> str | None:
    """Map machMode to the washer/dryer ENUM state key (None if missing/unknown)."""
    if raw is None:
        return None
    return WM_STATE_MAP.get(str(raw))


@dataclass(frozen=True, kw_only=True)
class HonSensorEntityDescription(SensorEntityDescription):
    """Description of a Haier hOn sensor.

    - `key` = historic unique_id suffix (do NOT modify).
    - `attr_key` = the attribute key read via HonBaseEntity._get_attr.
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
    device_class=SensorDeviceClass.ENUM,
    options=sorted(set(WM_STATE_MAP.values())),
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
    """prPhase -> washing-phase ENUM key (None if missing/unknown)."""
    if raw is None:
        return None
    return WASHING_PHASE_MAP.get(str(raw))


def _phase_dry(raw) -> str | None:
    """prPhase -> tumble-dryer phase ENUM key (None if missing/unknown)."""
    if raw is None:
        return None
    return TUMBLE_DRYER_PHASE_MAP.get(str(raw))


def _stain(raw) -> str | None:
    """stainType -> stain ENUM key (None if missing/unknown)."""
    if raw is None:
        return None
    return STAIN_TYPE_MAP.get(str(raw))


def _loading_pct(raw) -> float | None:
    """Average drum-load percentage from the loadingPercentage attribute.

    Laundry devices report loadingPercentage as a history LIST of
    {"current", "max", "date"} records (load vs drum capacity over past cycles),
    not a scalar, so the generic float() path would raise on the list and the
    sensor would stay unknown. Following the official app's "Loading Percentage"
    statistic, a record whose own max is 0/missing borrows the largest max in the
    list, each record's current is clamped to its max, and the load percent
    (current / max * 100) is averaged across the records. The clamp keeps the
    result within 0..100. A plain scalar / numeric string is passed through
    unchanged for forward/backward compatibility.

    The app limits the average to the five most recent records by `date`. We
    deliberately average ALL records instead and ignore `date`: its serialization
    is not verified against a live washer (the only known sample is the app's mock
    of JS Date objects), so any ordering would be unreliable, and the app's own
    backfill reducer is buggy. Real statistics lists observed so far are short
    (<= 5), where averaging all and "the most recent five" are identical. Revisit
    the windowing once a washer is available to validate the `date` shape live.

    Returns None (sensor "unknown", not a crash) when the value is missing, the
    list is empty/malformed, or no usable max can be derived (e.g. a device with
    no completed cycle yet, whose records all report max == 0 so drum capacity is
    unknown).
    """
    if raw is None:
        return None
    # Scalar / numeric-string passthrough (a model that ever reports a plain value).
    if not isinstance(raw, (list, tuple)):
        try:
            value = float(raw)
        except (ValueError, TypeError):
            return None
        return value if math.isfinite(value) else None
    records = [r for r in raw if isinstance(r, dict) and r.get("current") is not None]
    if not records:
        return None
    # Fleet-wide fallback for records whose own max is 0/missing: borrow the
    # largest known drum capacity (a clean global max, unlike the app's reducer).
    valid_maxes = []
    for record in records:
        try:
            candidate = float(record["max"])
        except (KeyError, ValueError, TypeError):
            continue
        if math.isfinite(candidate) and candidate > 0:
            valid_maxes.append(candidate)
    fallback_max = max(valid_maxes) if valid_maxes else None
    ratios = []
    for record in records:
        try:
            current = float(record["current"])
        except (ValueError, TypeError):
            continue
        try:
            maximum = float(record.get("max"))
        except (ValueError, TypeError):
            maximum = 0.0
        # A non-finite/non-positive own max is unusable; borrow the fleet capacity.
        usable_max = maximum if (math.isfinite(maximum) and maximum > 0) else None
        denom = usable_max if usable_max else fallback_max
        if not denom or denom <= 0:
            continue
        current = min(current, denom)  # clamp like the app's Math.min(current, max)
        ratio = current / denom * 100.0
        if math.isfinite(ratio) and ratio >= 0:
            ratios.append(ratio)
    if not ratios:
        return None
    return round(sum(ratios) / len(ratios), 1)


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
    device_class=SensorDeviceClass.ENUM,
    options=sorted(set(WASHING_PHASE_MAP.values())),
    value_fn=_phase_wash,
)
_PHASE_DRY = HonSensorEntityDescription(
    key="program_phase",
    translation_key="dryer_phase",
    icon="mdi:tumble-dryer",
    attr_key=WM_ATTR_PROGRAM_PHASE,
    device_class=SensorDeviceClass.ENUM,
    options=sorted(set(TUMBLE_DRYER_PHASE_MAP.values())),
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
    value_fn=_loading_pct,
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
    HonSensorEntityDescription(
        key="stain_type",
        icon="mdi:liquid-spot",
        attr_key="stainType",
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(STAIN_TYPE_MAP.values())),
        value_fn=_stain,
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
# real value without changing entity_id. No loading_percentage: the app gates the
# Loading Percentage statistic to WM/WD only (TD uses loadEfficiency instead), so
# the sensor would be perpetually unknown on a dryer.
_DRYER: tuple[HonSensorEntityDescription, ...] = (
    _STATE,
    _REMAINING,
    _PROGRAM_NAME,
    _PHASE_DRY,
    _DRY_LEVEL,
    _DELAY,
    _ERRORS,
    HonSensorEntityDescription(
        key="temp_level",
        icon="mdi:thermometer-lines",
        attr_key="tempLevel",
    ),
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

# --- Tier 2: read-only sensors (capability-gated) ----------------------------
# Types mapped from the official app but not validated on real devices. Each
# description has gated=True: the entity is created only if the device exposes
# the attribute (see async_setup_entry). The `attr_key` values are the hOn
# parameter names (direct telemetry), used only once here, so they stay inline
# strings (unlike the historic types, which share keys across several platforms).


def _mapped(mapping: dict[str, str]) -> Callable[[object], object]:
    """Build a value_fn that maps the raw value to an ENUM key via `mapping`.

    None / unknown value -> None (the sensor reports "unknown" rather than an
    out-of-options value)."""

    def _fn(raw):
        if raw is None:
            return None
        return mapping.get(str(raw))

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


def _g_enum(key: str, attr: str, mapping: dict[str, str], *,
            translation_key: str | None = None,
            icon: str | None = None) -> HonSensorEntityDescription:
    """Capability-gated ENUM sensor: native_value is a machine key from `mapping`,
    rendered per-language via the entity state translations."""
    return HonSensorEntityDescription(
        key=key,
        translation_key=translation_key,
        attr_key=attr,
        icon=icon,
        device_class=SensorDeviceClass.ENUM,
        options=sorted(set(mapping.values())),
        value_fn=_mapped(mapping),
        gated=True,
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
    _g_enum("state", "machMode", MACHINE_MODE_MAP,
            translation_key="machine_mode", icon="mdi:stove"),
    _g_temp("temp_cavity", "temp"),
    _g_minutes("remaining_time", "remainingTimeMM"),
    _g_temp("probe_temp_1", "tempEmployedProbe1"),
    _g_temp("probe_temp_2", "tempEmployedProbe2"),
)

# Dishwasher (DW): state, program, time, salt/rinse-aid levels,
# temperature, errors. The door is a binary sensor.
_DISHWASHER: tuple[HonSensorEntityDescription, ...] = (
    _g_enum("state", "machMode", MACHINE_MODE_MAP,
            translation_key="machine_mode", icon="mdi:dishwasher"),
    _g_text("program_name", "programName", icon="mdi:format-list-bulleted"),
    _g_minutes("remaining_time", "remainingTimeMM"),
    _g_enum("salt_level", "saltStatus", DW_LEVEL_MAP, icon="mdi:shaker-outline"),
    _g_enum("rinse_aid_level", "rinseAidStatus", DW_LEVEL_MAP,
            icon="mdi:water-opacity"),
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
    _g_enum("program_phase", "prPhase", WH_PHASE_MAP,
            translation_key="heater_phase", icon="mdi:water-boiler"),
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
    _g_enum("state", "prPhase", RVC_STATE_MAP,
            translation_key="vacuum_state", icon="mdi:robot-vacuum"),
    _g_minutes("remaining_time", "remainingTimeMM"),
    _g_enum("power_mode", "power", RVC_POWER_MAP, icon="mdi:fan"),
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
