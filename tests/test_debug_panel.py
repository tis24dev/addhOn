"""Tests for the account-level debug panel entities (switches, sensors, buttons).

The integration exposes its debug controls on a dedicated per-account
"diagnostics" device: two persistent switches mirrored to ``entry.options`` (so
they stay in sync with the Options flow and re-apply the log levels via the
existing options update listener, no reload), read-only diagnostic sensors, and
two action buttons (force refresh + reset debug).

stdlib unittest with hand-rolled HA stubs (mirrors test_tier2_sensors /
test_options_flow), so no real Home Assistant is required.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime
import logging
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _mod(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_stubs() -> None:
    ha = _mod("homeassistant")

    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(config_entries, "ConfigEntry", type("ConfigEntry", (), {}))

    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))

    exceptions = _mod("homeassistant.exceptions")
    base_err = getattr(exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exceptions.HomeAssistantError = base_err
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {})
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {})
    )

    helpers = _mod("homeassistant.helpers")
    entity = _mod("homeassistant.helpers.entity")
    entity.DeviceInfo = getattr(entity, "DeviceInfo", dict)
    device_registry = _mod("homeassistant.helpers.device_registry")
    device_registry.DeviceEntryType = getattr(
        device_registry, "DeviceEntryType", type("DeviceEntryType", (), {"SERVICE": "service"})
    )
    entity_platform = _mod("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = getattr(entity_platform, "AddEntitiesCallback", object)

    uc = _mod("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
            self.hass = getattr(coordinator, "hass", None)

        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

    uc.CoordinatorEntity = getattr(uc, "CoordinatorEntity", CoordinatorEntity)
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

    components = _mod("homeassistant.components")

    class _StateEntity:
        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

    switch_mod = _mod("homeassistant.components.switch")
    switch_mod.SwitchEntity = getattr(switch_mod, "SwitchEntity", type("SwitchEntity", (_StateEntity,), {}))
    button_mod = _mod("homeassistant.components.button")
    button_mod.ButtonEntity = getattr(button_mod, "ButtonEntity", type("ButtonEntity", (_StateEntity,), {}))

    # sensor platform
    sensor_mod = _mod("homeassistant.components.sensor")

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

    sensor_mod.SensorEntityDescription = getattr(sensor_mod, "SensorEntityDescription", SensorEntityDescription)
    sensor_mod.SensorEntity = getattr(sensor_mod, "SensorEntity", type("SensorEntity", (_StateEntity,), {}))
    sensor_mod.SensorDeviceClass = getattr(sensor_mod, "SensorDeviceClass", type("SensorDeviceClass", (), {
        "TEMPERATURE": "temperature", "HUMIDITY": "humidity", "ENERGY": "energy",
        "WATER": "water", "DURATION": "duration", "PM25": "pm25", "CO2": "co2",
        "PM10": "pm10", "CO": "carbon_monoxide", "AQI": "aqi",
        "VOLATILE_ORGANIC_COMPOUNDS_PARTS": "volatile_organic_compounds_parts",
        "WEIGHT": "weight", "BATTERY": "battery", "POWER": "power", "ENUM": "enum",
        "TIMESTAMP": "timestamp",
    }))
    sensor_mod.SensorStateClass = getattr(sensor_mod, "SensorStateClass", type("SensorStateClass", (), {
        "MEASUREMENT": "measurement", "TOTAL": "total", "TOTAL_INCREASING": "total_increasing",
    }))

    # binary_sensor platform
    binary_mod = _mod("homeassistant.components.binary_sensor")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class BinarySensorEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        device_class: object | None = None

    binary_mod.BinarySensorEntityDescription = getattr(binary_mod, "BinarySensorEntityDescription", BinarySensorEntityDescription)
    binary_mod.BinarySensorEntity = getattr(binary_mod, "BinarySensorEntity", type("BinarySensorEntity", (_StateEntity,), {}))
    binary_mod.BinarySensorDeviceClass = getattr(binary_mod, "BinarySensorDeviceClass", type("BinarySensorDeviceClass", (), {
        "DOOR": "door", "PROBLEM": "problem", "RUNNING": "running",
        "OCCUPANCY": "occupancy", "LIGHT": "light", "CONNECTIVITY": "connectivity", "HEAT": "heat",
    }))

    const = _mod("homeassistant.const")
    for unit_cls in ("UnitOfTemperature", "UnitOfEnergy", "UnitOfTime", "UnitOfVolume", "UnitOfMass"):
        if not hasattr(const, unit_cls):
            setattr(const, unit_cls, type(unit_cls, (), {
                "CELSIUS": "C", "KILO_WATT_HOUR": "kWh", "MINUTES": "min", "LITERS": "L",
                "GRAMS": "g", "KILOGRAMS": "kg", "SECONDS": "s",
            }))
    const.EntityCategory = getattr(
        const, "EntityCategory", type("EntityCategory", (), {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"})
    )

    # dt_util.utcnow(): the timestamp source for HonLastRefreshSensor (lazy-imported
    # in production so most stubs need not provide it). Our lifecycle tests DO drive
    # a coordinator update, so supply a tz-aware UTC clock (TIMESTAMP must be aware).
    util = _mod("homeassistant.util")
    dt = _mod("homeassistant.util.dt")
    if not hasattr(dt, "utcnow"):
        dt.utcnow = lambda: datetime.datetime.now(datetime.timezone.utc)
    util.dt = dt

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    ha.const = const
    ha.components = components
    helpers.entity = entity
    helpers.device_registry = device_registry
    helpers.entity_platform = entity_platform
    helpers.update_coordinator = uc
    ha.util = util
    components.switch = switch_mod
    components.button = button_mod
    components.sensor = sensor_mod
    components.binary_sensor = binary_mod


_install_stubs()

from custom_components.addhon.const import (  # noqa: E402
    CONF_ENABLE_DEBUG,
    CONF_ENABLE_MQTT_DEBUG,
    DOMAIN,
)

INTEGRATION_LOGGER = "custom_components.addhon"
MQTT_LOGGER = "custom_components.addhon.client.transport.mqtt"


class FakeConfigEntries:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def async_update_entry(self, entry, options=None, **kwargs) -> bool:
        self.updates.append(dict(options or {}))
        if options is not None:
            entry.options = dict(options)
        return True


class FakeHass:
    def __init__(self, data=None) -> None:
        self.data = data or {}
        self.config_entries = FakeConfigEntries()


class FakeEntry:
    def __init__(self, options=None, entry_id="entry-1") -> None:
        self.entry_id = entry_id
        self.options = dict(options or {})
        self.update_listeners: list = []

    def add_update_listener(self, callback):
        self.update_listeners.append(callback)
        return lambda: None


class FakeCoordinator:
    def __init__(self, data=None, last_update_success=True) -> None:
        self.data = data if data is not None else {}
        self.last_update_success = last_update_success
        self.refreshes = 0

    async def async_request_refresh(self) -> None:
        self.refreshes += 1


def _entry_data(coordinator, entry):
    return {DOMAIN: {entry.entry_id: {
        "coordinator": coordinator, "client": None, "integration_version": "9.9.9",
    }}}


def _install_write_counter(ent) -> None:
    """Replace ``async_write_ha_state`` with an instance-level counter.

    The shared HA stubs other test modules install are bare (no
    ``async_write_ha_state``), so we cannot rely on the base class; bind a counter
    directly on the instance to actually observe the state writes the account
    entities perform (a regression that drops a write is then caught instead of
    being masked by a ``lambda: None`` no-op)."""
    ent._write_count = 0

    def _writer() -> None:
        ent._write_count += 1

    ent.async_write_ha_state = _writer


# Real-HA-like lifecycle no-ops. The shared stub base classes installed by the
# other test modules are bare, so ``super().async_added_to_hass()`` /
# ``super()._handle_coordinator_update()`` / ``self.async_on_remove(...)`` would
# raise AttributeError under the full suite. ``_ha_lifecycle`` temporarily grafts
# these onto the ACTUAL bound base class (found via the production MRO, robust to
# whichever module won the stub race) and removes exactly what it added.
async def _noop_async(self, *args, **kwargs):
    return None


def _forward_write(self) -> None:
    self.async_write_ha_state()


def _record_on_remove(self, func) -> None:
    self._unsubs = getattr(self, "_unsubs", [])
    self._unsubs.append(func)


def _first_stub_base(cls):
    """The class ``super()`` lands on from the production entity: the first MRO
    entry that is neither one of our ``custom_components`` classes nor ``object``
    (i.e. the hand-rolled HA stub base the other test modules installed)."""
    for base in cls.__mro__:
        if base is object:
            continue
        if getattr(base, "__module__", "").startswith("custom_components"):
            continue
        return base
    return None


@contextlib.contextmanager
def _ha_lifecycle(*entity_classes):
    impls = {
        "async_added_to_hass": _noop_async,
        "_handle_coordinator_update": _forward_write,
        "async_on_remove": _record_on_remove,
    }
    added: list = []
    for ecls in entity_classes:
        base = _first_stub_base(ecls)
        if base is None:
            continue
        for name, impl in impls.items():
            # Add only what the stub base lacks (don't clobber a richer stub); the
            # production class's own override is irrelevant here -- it is the caller
            # of super(), which must resolve onto this base.
            if not hasattr(base, name):
                setattr(base, name, impl)
                added.append((base, name))
    try:
        yield
    finally:
        for base, name in added:
            delattr(base, name)


async def _build_switches(entry, coordinator=None):
    from custom_components.addhon import switch

    coordinator = coordinator or FakeCoordinator()
    hass = FakeHass(_entry_data(coordinator, entry))
    added: list = []
    await switch.async_setup_entry(hass, entry, added.extend)
    debug = [e for e in added if getattr(e, "_addhon_account", False)]
    for ent in debug:
        ent.hass = hass
        # Real HA supplies async_write_ha_state; the shared SwitchEntity stub may be
        # the bare one installed first by another test module. Bind a real counter
        # so the state writes are actually observable, not masked by a no-op.
        _install_write_counter(ent)
    return hass, {e._option_key: e for e in debug}


class DebugSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_two_debug_switches_created_with_keys_and_category(self) -> None:
        from homeassistant.const import EntityCategory

        entry = FakeEntry()
        _hass, switches = await _build_switches(entry)
        self.assertEqual({CONF_ENABLE_DEBUG, CONF_ENABLE_MQTT_DEBUG}, set(switches))
        debug = switches[CONF_ENABLE_DEBUG]
        self.assertEqual("debug_logging", debug._attr_translation_key)
        self.assertEqual(EntityCategory.CONFIG, debug._attr_entity_category)
        self.assertEqual("entry-1_diag_debug_logging", debug._attr_unique_id)
        # All bound to the same per-account diagnostics device.
        info = debug.device_info
        self.assertEqual({(DOMAIN, "entry-1_diagnostics")}, info["identifiers"])
        self.assertEqual("9.9.9", info["sw_version"])

    async def test_is_on_reads_entry_options(self) -> None:
        entry = FakeEntry({CONF_ENABLE_DEBUG: True, CONF_ENABLE_MQTT_DEBUG: False})
        _hass, switches = await _build_switches(entry)
        self.assertTrue(switches[CONF_ENABLE_DEBUG].is_on)
        self.assertFalse(switches[CONF_ENABLE_MQTT_DEBUG].is_on)

    async def test_turn_on_persists_and_preserves_other_toggle(self) -> None:
        entry = FakeEntry({CONF_ENABLE_MQTT_DEBUG: True})
        hass, switches = await _build_switches(entry)
        await switches[CONF_ENABLE_DEBUG].async_turn_on()
        self.assertEqual(
            {CONF_ENABLE_DEBUG: True, CONF_ENABLE_MQTT_DEBUG: True},
            hass.config_entries.updates[-1],
        )
        # Source of truth updated, so is_on now reflects it.
        self.assertTrue(switches[CONF_ENABLE_DEBUG].is_on)
        # The toggle wrote its new HA state (not masked by a no-op stub).
        self.assertEqual(1, switches[CONF_ENABLE_DEBUG]._write_count)

    async def test_turn_off_when_already_off_is_a_noop(self) -> None:
        entry = FakeEntry({CONF_ENABLE_DEBUG: False})
        hass, switches = await _build_switches(entry)
        await switches[CONF_ENABLE_DEBUG].async_turn_off()
        self.assertEqual([], hass.config_entries.updates)
        # The early-out guard means no redundant state write either.
        self.assertEqual(0, switches[CONF_ENABLE_DEBUG]._write_count)

    async def test_entry_update_listener_refreshes_switch(self) -> None:
        # Bidirectional sync: when the option is changed elsewhere (Options flow /
        # Reset button), the registered entry update listener re-renders the switch.
        entry = FakeEntry()
        _hass, switches = await _build_switches(entry)
        switch = switches[CONF_ENABLE_DEBUG]
        switch._write_count = 0
        await switch._async_entry_updated(None, entry)
        self.assertEqual(1, switch._write_count)

    async def test_async_added_registers_entry_update_listener(self) -> None:
        from custom_components.addhon.switch import HonDebugSwitch

        entry = FakeEntry()
        _hass, switches = await _build_switches(entry)
        switch = switches[CONF_ENABLE_DEBUG]
        with _ha_lifecycle(HonDebugSwitch):
            await switch.async_added_to_hass()
        # The switch wired its own listener so the Options flow / Reset button
        # propagate to it; without it the sync guarantee silently breaks.
        self.assertEqual([switch._async_entry_updated], entry.update_listeners)


class DebugSensorsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._saved = {n: logging.getLogger(n).level for n in (INTEGRATION_LOGGER, MQTT_LOGGER)}

    def tearDown(self) -> None:
        for name, level in self._saved.items():
            logging.getLogger(name).setLevel(level)

    async def _build_sensors(self, entry, coordinator):
        from custom_components.addhon import sensor

        hass = FakeHass(_entry_data(coordinator, entry))
        added: list = []
        await sensor.async_setup_entry(hass, entry, added.extend)
        account = [e for e in added if getattr(e, "_addhon_account", False)]
        return {e._attr_translation_key: e for e in account}

    async def test_debug_status_maps_all_combinations(self) -> None:
        coordinator = FakeCoordinator({"a": {}, "b": {}})
        cases = {
            (): "off",
            (CONF_ENABLE_DEBUG,): "integration",
            (CONF_ENABLE_MQTT_DEBUG,): "mqtt",
            (CONF_ENABLE_DEBUG, CONF_ENABLE_MQTT_DEBUG): "full",
        }
        for on_keys, expected in cases.items():
            entry = FakeEntry({k: True for k in on_keys})
            sensors = await self._build_sensors(entry, coordinator)
            self.assertEqual(expected, sensors["debug_status"].native_value)

    async def test_log_level_sensor_reads_effective_level(self) -> None:
        logging.getLogger(INTEGRATION_LOGGER).setLevel(logging.DEBUG)
        entry = FakeEntry()
        sensors = await self._build_sensors(entry, FakeCoordinator())
        self.assertEqual("debug", sensors["integration_log_level"].native_value)

    async def test_appliances_discovered_and_update_ok(self) -> None:
        entry = FakeEntry()
        coordinator = FakeCoordinator({"a": {}, "b": {}, "c": {}}, last_update_success=True)
        sensors = await self._build_sensors(entry, coordinator)
        self.assertEqual(3, sensors["appliances_discovered"].native_value)

        from custom_components.addhon import binary_sensor

        hass = FakeHass(_entry_data(coordinator, entry))
        added: list = []
        await binary_sensor.async_setup_entry(hass, entry, added.extend)
        update_ok = next(e for e in added if getattr(e, "_addhon_account", False))
        self.assertTrue(update_ok.is_on)
        coordinator.last_update_success = False
        self.assertFalse(update_ok.is_on)
        # Must stay available even on a failed refresh, otherwise the OFF state is
        # never shown (it would go "unavailable" exactly when it matters).
        self.assertTrue(update_ok.available)
        self.assertTrue(sensors["last_refresh"].available)
        self.assertTrue(sensors["appliances_discovered"].available)

    async def test_debug_status_entry_update_listener_writes_state(self) -> None:
        # The ENUM status sensor re-renders when the toggles change elsewhere.
        entry = FakeEntry()
        sensors = await self._build_sensors(entry, FakeCoordinator())
        status = sensors["debug_status"]
        _install_write_counter(status)
        await status._async_entry_updated(None, entry)
        self.assertEqual(1, status._write_count)

    async def test_last_refresh_seeds_on_add_and_advances_on_update(self) -> None:
        from custom_components.addhon.sensor import HonLastRefreshSensor

        entry = FakeEntry()
        coordinator = FakeCoordinator(last_update_success=True)
        sensors = await self._build_sensors(entry, coordinator)
        last = sensors["last_refresh"]
        _install_write_counter(last)
        self.assertIsNone(last._attr_native_value)
        with _ha_lifecycle(HonLastRefreshSensor):
            # First refresh already ran before add, so seed now (no "unknown" flash).
            await last.async_added_to_hass()
            seeded = last._attr_native_value
            self.assertIsNotNone(seeded)
            self.assertIsNotNone(seeded.tzinfo)  # TIMESTAMP must be tz-aware
            # A later refresh advances the timestamp and writes state.
            last._attr_native_value = seeded - datetime.timedelta(hours=1)
            last._handle_coordinator_update()
        self.assertGreater(last._attr_native_value, seeded - datetime.timedelta(hours=1))
        self.assertGreaterEqual(last._write_count, 1)

    async def test_last_refresh_not_seeded_when_refresh_failed(self) -> None:
        from custom_components.addhon.sensor import HonLastRefreshSensor

        entry = FakeEntry()
        coordinator = FakeCoordinator(last_update_success=False)
        sensors = await self._build_sensors(entry, coordinator)
        last = sensors["last_refresh"]
        with _ha_lifecycle(HonLastRefreshSensor):
            await last.async_added_to_hass()
        self.assertIsNone(last._attr_native_value)


class DebugButtonsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._saved = {n: logging.getLogger(n).level for n in (INTEGRATION_LOGGER, MQTT_LOGGER)}

    def tearDown(self) -> None:
        for name, level in self._saved.items():
            logging.getLogger(name).setLevel(level)

    async def _build_buttons(self, entry, coordinator):
        from custom_components.addhon import button

        hass = FakeHass(_entry_data(coordinator, entry))
        added: list = []
        await button.async_setup_entry(hass, entry, added.extend)
        account = {e._attr_translation_key: e for e in added if getattr(e, "_addhon_account", False)}
        for ent in account.values():
            ent.hass = hass
        return hass, account

    async def test_force_refresh_calls_coordinator(self) -> None:
        entry = FakeEntry()
        coordinator = FakeCoordinator()
        _hass, buttons = await self._build_buttons(entry, coordinator)
        await buttons["force_refresh"].async_press()
        self.assertEqual(1, coordinator.refreshes)

    async def test_reset_debug_turns_off_and_resets_loggers(self) -> None:
        logging.getLogger(INTEGRATION_LOGGER).setLevel(logging.DEBUG)
        logging.getLogger(MQTT_LOGGER).setLevel(logging.DEBUG)
        entry = FakeEntry({CONF_ENABLE_DEBUG: True, CONF_ENABLE_MQTT_DEBUG: True})
        hass, buttons = await self._build_buttons(entry, FakeCoordinator())

        await buttons["reset_debug"].async_press()

        self.assertEqual(
            {CONF_ENABLE_DEBUG: False, CONF_ENABLE_MQTT_DEBUG: False},
            hass.config_entries.updates[-1],
        )
        self.assertEqual(logging.NOTSET, logging.getLogger(INTEGRATION_LOGGER).level)
        self.assertEqual(logging.WARNING, logging.getLogger(MQTT_LOGGER).level)

    async def test_reset_debug_resets_loggers_even_when_already_off(self) -> None:
        # No options to persist, but a runtime set_log_level override must still clear.
        logging.getLogger(INTEGRATION_LOGGER).setLevel(logging.DEBUG)
        entry = FakeEntry()
        hass, buttons = await self._build_buttons(entry, FakeCoordinator())

        await buttons["reset_debug"].async_press()

        self.assertEqual([], hass.config_entries.updates)
        self.assertEqual(logging.NOTSET, logging.getLogger(INTEGRATION_LOGGER).level)


if __name__ == "__main__":
    unittest.main()
