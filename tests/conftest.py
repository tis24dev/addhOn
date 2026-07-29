# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared test stubs for the addhon suite.

There is no real `homeassistant` package in the test environment; each test module
builds minimal stubs and reuses any already installed in sys.modules (via
`getattr(exc, "HomeAssistantError", <fallback>)`). pytest imports this conftest
before any test module, so installing a Home Assistant-compatible
`HomeAssistantError` here makes every module reuse it.

The integration raises TRANSLATABLE exceptions
(`HomeAssistantError(translation_domain=..., translation_key=..., translation_placeholders=...)`),
exactly as real HA supports. A plain `Exception` subclass would raise TypeError on
those keyword arguments, so the stub below mirrors HA's signature and exposes the
attributes for assertions.
"""
import dataclasses
import sys
import types


def _install_homeassistant_error() -> None:
    ha = sys.modules.get("homeassistant")
    if ha is None:
        ha = types.ModuleType("homeassistant")
        sys.modules["homeassistant"] = ha
    exc = sys.modules.get("homeassistant.exceptions")
    if exc is None:
        exc = types.ModuleType("homeassistant.exceptions")
        sys.modules["homeassistant.exceptions"] = exc
    ha.exceptions = exc

    existing = getattr(exc, "HomeAssistantError", None)
    if existing is not None and getattr(existing, "_addhon_translatable", False):
        return

    class HomeAssistantError(Exception):
        """Mirror of homeassistant.exceptions.HomeAssistantError (translatable)."""

        _addhon_translatable = True

        def __init__(
            self,
            *args,
            translation_domain=None,
            translation_key=None,
            translation_placeholders=None,
        ) -> None:
            super().__init__(*args)
            self.translation_domain = translation_domain
            self.translation_key = translation_key
            self.translation_placeholders = translation_placeholders

    exc.HomeAssistantError = HomeAssistantError


def _ensure_module(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_shared_entity_stubs() -> None:
    """Install the COMPLETE Home Assistant stubs the entity stack binds at import time.

    `custom_components/addhon/base_entity.py` does `class HonBaseEntity(CoordinatorEntity)`
    at import, so whatever `CoordinatorEntity` is in sys.modules the FIRST time base_entity
    is imported becomes the permanent base for the run. Several test modules install an
    INCOMPLETE `CoordinatorEntity` (no `async_write_ha_state` / `available` / `hass`) with a
    first-wins `getattr(...)` idiom; in a partial collection order one of those can win and
    poison every entity instantiated afterwards. Likewise some `const` stubs omit symbols
    the platforms import at module load (e.g. `UnitOfTime`). pytest imports this conftest
    before ANY test module, so installing a COMPLETE `CoordinatorEntity` + the full `const`
    symbol set here makes those per-file first-wins `getattr`/`hasattr` calls reuse the
    complete shared stubs -- the suite stops depending on collection order. Everything is
    getattr/hasattr-guarded so an already-present (or real) symbol is never clobbered."""
    helpers = _ensure_module("homeassistant.helpers")
    sys.modules["homeassistant"].helpers = helpers

    uc = _ensure_module("homeassistant.helpers.update_coordinator")
    helpers.update_coordinator = uc

    class CoordinatorEntity:
        """Complete mirror of HA's CoordinatorEntity (the base HonBaseEntity subclasses)."""

        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
            self.hass = getattr(coordinator, "hass", None)

        @property
        def available(self) -> bool:
            return getattr(self.coordinator, "last_update_success", True)

        @property
        def unique_id(self):
            return getattr(self, "_attr_unique_id", None)

        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

        def _handle_coordinator_update(self) -> None:
            self.async_write_ha_state()

    uc.CoordinatorEntity = getattr(uc, "CoordinatorEntity", CoordinatorEntity)
    uc.DataUpdateCoordinator = getattr(
        uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {})
    )
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

    const = _ensure_module("homeassistant.const")
    sys.modules["homeassistant"].const = const
    if not hasattr(const, "UnitOfTemperature"):
        const.UnitOfTemperature = type("UnitOfTemperature", (), {"CELSIUS": "°C"})
    if not hasattr(const, "UnitOfTime"):
        const.UnitOfTime = type("UnitOfTime", (), {"MINUTES": "min", "SECONDS": "s"})
    if not hasattr(const, "UnitOfEnergy"):
        const.UnitOfEnergy = type("UnitOfEnergy", (), {"KILO_WATT_HOUR": "kWh"})
    if not hasattr(const, "UnitOfVolume"):
        const.UnitOfVolume = type("UnitOfVolume", (), {"LITERS": "L"})
    if not hasattr(const, "UnitOfMass"):
        const.UnitOfMass = type("UnitOfMass", (), {"GRAMS": "g", "KILOGRAMS": "kg"})
    if not hasattr(const, "EntityCategory"):
        const.EntityCategory = type(
            "EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"}
        )

    # base_entity's other import-time HA deps (never order-sensitive today, seeded here so
    # the same class of bug can't resurface for a future entity-only subset).
    entity = _ensure_module("homeassistant.helpers.entity")
    helpers.entity = entity
    entity.DeviceInfo = getattr(entity, "DeviceInfo", dict)
    dr = _ensure_module("homeassistant.helpers.device_registry")
    helpers.device_registry = dr
    dr.DeviceEntryType = getattr(
        dr, "DeviceEntryType", type("DeviceEntryType", (), {"SERVICE": "service"})
    )


def _install_entity_platform_stubs() -> None:
    """Shared entity-platform stubs: `binary_sensor`, `fan`, `light`, `number`,
    `select`, `sensor` and `switch`.

    Installed here rather than per test module: each of these is imported by
    several test modules, and a partial per-file stub winning the first-wins
    `getattr` race is exactly the order-dependence this conftest exists to
    remove. `AddEntitiesCallback` lands here for the same reason.

    A test asserts this list against what the function actually stubs, because the
    name and the docstring were both stale once: it said fan while stubbing seven
    platforms, having grown one per campaign task."""
    components = _ensure_module("homeassistant.components")
    sys.modules["homeassistant"].components = components

    fan = _ensure_module("homeassistant.components.fan")
    components.fan = fan

    class FanEntity:
        """Mirror of HA's FanEntity surface the AP fan overrides.

        The `_attr_*`-reading properties are what real HA's Entity/FanEntity
        provide; without them a test would read the private attribute and pass
        even if the entity never exposed the value to Home Assistant."""

        _attr_preset_mode = None
        _attr_preset_modes = None
        _attr_supported_features = 0
        _attr_is_on = None

        @property
        def preset_mode(self):
            return self._attr_preset_mode

        @property
        def preset_modes(self):
            return self._attr_preset_modes

        @property
        def supported_features(self):
            return self._attr_supported_features

        @property
        def is_on(self):
            return self._attr_is_on

    class FanEntityFeature:
        # Real Home Assistant values, so a stub-vs-real mismatch cannot hide a
        # wrong feature declaration.
        SET_SPEED = 1
        OSCILLATE = 2
        DIRECTION = 4
        PRESET_MODE = 8
        TURN_OFF = 16
        TURN_ON = 32

    fan.FanEntity = getattr(fan, "FanEntity", FanEntity)
    fan.FanEntityFeature = getattr(fan, "FanEntityFeature", FanEntityFeature)

    light = _ensure_module("homeassistant.components.light")
    components.light = light

    class LightEntity:
        """Mirror of HA's LightEntity surface the AP panel light overrides."""

        _attr_brightness = None
        _attr_color_mode = None
        _attr_supported_color_modes = None
        _attr_is_on = None

        @property
        def brightness(self):
            return self._attr_brightness

        @property
        def color_mode(self):
            return self._attr_color_mode

        @property
        def supported_color_modes(self):
            return self._attr_supported_color_modes

        @property
        def is_on(self):
            return self._attr_is_on

    class ColorMode:
        UNKNOWN = "unknown"
        ONOFF = "onoff"
        BRIGHTNESS = "brightness"

    light.LightEntity = getattr(light, "LightEntity", LightEntity)
    light.ColorMode = getattr(light, "ColorMode", ColorMode)
    light.ATTR_BRIGHTNESS = getattr(light, "ATTR_BRIGHTNESS", "brightness")

    # Bare platform bases: the addhon entities define every property themselves,
    # so nothing beyond the class is needed to subclass them.
    switch = _ensure_module("homeassistant.components.switch")
    components.switch = switch
    switch.SwitchEntity = getattr(switch, "SwitchEntity", type("SwitchEntity", (), {}))

    select = _ensure_module("homeassistant.components.select")
    components.select = select

    class SelectEntity:
        """Mirror of HA's SelectEntity: `options` is exposed over `_attr_options`."""

        _attr_options = None
        _attr_current_option = None

        @property
        def options(self):
            return self._attr_options

        @property
        def current_option(self):
            return self._attr_current_option

    select.SelectEntity = getattr(select, "SelectEntity", SelectEntity)

    # The two description dataclasses only (NOT the DeviceClass/StateClass enums,
    # whose member sets legitimately differ per test module). Six modules declare an
    # identical copy of each; installing them here means a description field added
    # later -- `entity_category` was the first -- cannot depend on which module wins
    # the first-wins race.
    sensor = _ensure_module("homeassistant.components.sensor")
    components.sensor = sensor

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        native_unit_of_measurement: str | None = None
        device_class: object | None = None
        state_class: object | None = None
        options: object | None = None
        entity_category: object | None = None

    sensor.SensorEntityDescription = getattr(
        sensor, "SensorEntityDescription", SensorEntityDescription
    )

    # The device/state class enums, with REAL Home Assistant values. Every test
    # module used to declare its own subset, so which MEMBERS existed depended on
    # collection order: a module whose stub lacked SAFETY made an unrelated
    # "never a safety device" assertion fail with AttributeError. Values differed
    # too (`co2` vs the real `carbon_dioxide`), which would let a wrong device class
    # pass unnoticed.
    class SensorDeviceClass:
        AQI = "aqi"
        BATTERY = "battery"
        CO = "carbon_monoxide"
        CO2 = "carbon_dioxide"
        DURATION = "duration"
        ENERGY = "energy"
        ENUM = "enum"
        HUMIDITY = "humidity"
        PM10 = "pm10"
        PM25 = "pm25"
        POWER = "power"
        TEMPERATURE = "temperature"
        TIMESTAMP = "timestamp"
        VOLATILE_ORGANIC_COMPOUNDS_PARTS = "volatile_organic_compounds_parts"
        WATER = "water"
        WEIGHT = "weight"

    class SensorStateClass:
        MEASUREMENT = "measurement"
        TOTAL = "total"
        TOTAL_INCREASING = "total_increasing"

    sensor.SensorDeviceClass = getattr(sensor, "SensorDeviceClass", SensorDeviceClass)
    sensor.SensorStateClass = getattr(sensor, "SensorStateClass", SensorStateClass)

    binary_sensor = _ensure_module("homeassistant.components.binary_sensor")
    components.binary_sensor = binary_sensor

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class BinarySensorEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        device_class: object | None = None
        entity_category: object | None = None

    class BinarySensorDeviceClass:
        CONNECTIVITY = "connectivity"
        DOOR = "door"
        HEAT = "heat"
        LIGHT = "light"
        LOCK = "lock"
        OCCUPANCY = "occupancy"
        POWER = "power"
        PROBLEM = "problem"
        RUNNING = "running"
        SAFETY = "safety"

    binary_sensor.BinarySensorEntityDescription = getattr(
        binary_sensor, "BinarySensorEntityDescription", BinarySensorEntityDescription
    )
    binary_sensor.BinarySensorDeviceClass = getattr(
        binary_sensor, "BinarySensorDeviceClass", BinarySensorDeviceClass
    )

    number = _ensure_module("homeassistant.components.number")
    components.number = number

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class NumberEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        device_class: object | None = None
        entity_category: object | None = None
        native_unit_of_measurement: str | None = None
        native_min_value: float | None = None
        native_max_value: float | None = None
        native_step: float | None = None
        mode: object | None = None

    class NumberDeviceClass:
        TEMPERATURE = "temperature"
        DURATION = "duration"

    class NumberMode:
        AUTO = "auto"
        BOX = "box"
        SLIDER = "slider"

    number.NumberEntityDescription = getattr(
        number, "NumberEntityDescription", NumberEntityDescription
    )
    number.NumberEntity = getattr(number, "NumberEntity", type("NumberEntity", (), {}))
    number.NumberDeviceClass = getattr(number, "NumberDeviceClass", NumberDeviceClass)
    number.NumberMode = getattr(number, "NumberMode", NumberMode)

    entity_platform = _ensure_module("homeassistant.helpers.entity_platform")
    sys.modules["homeassistant.helpers"].entity_platform = entity_platform
    entity_platform.AddEntitiesCallback = getattr(
        entity_platform, "AddEntitiesCallback", object
    )


def _ensure_yarl() -> None:
    """The CI test env installs only pytest (no yarl). config_flow now imports the
    transport auth (which does `from yarl import URL`), so importing config_flow -- and
    thus every config-flow test -- needs yarl present. Use the REAL yarl when installed
    (this also loads it BEFORE any test module's `_mod('yarl')` can shadow it with a
    URL-only stub that would break a real aiohttp's `from yarl import URL, Query`); else
    install a minimal URL stub. Runs at conftest import, before any collection."""
    try:
        import yarl  # noqa: F401
    except ImportError:
        yarl_stub = types.ModuleType("yarl")
        yarl_stub.URL = type(
            "URL",
            (),
            {
                "__init__": lambda self, s, encoded=False: setattr(self, "_s", s),
                "__str__": lambda self: self._s,
            },
        )
        sys.modules["yarl"] = yarl_stub


_install_homeassistant_error()
_install_shared_entity_stubs()
_install_entity_platform_stubs()
_ensure_yarl()
