"""Validate the #35 program-option catalogs against erpayo's REAL device schemas.

The per-type entity tests validate entity `key`s (slugs) but not the `param`
(the Haier startProgram parameter name a control reads/writes) nor that the
schema-driven GATE produces exactly the settable set we expect on a real model. A
wrong `param` makes a capability-gated control silently never created, and a gate
bug would either resurrect erpayo's fixed "No disponible" toggles or drop a
genuinely settable one - no key-based test catches either.

This guards both against erpayo's two real machines (fixtures built from his
redacted config-entry diagnostics, discussion #35):
  - WM Candy TCA286TM5-S  -> tests/fixtures/wm_candy_tca286/
  - TD Haier HD90-A3959    -> tests/fixtures/td_haier_hd90/
The catalog is a SUPERSET (decompiled hOn / andre0512): not every candidate param
exists on every model (steamLevel/permanentPress are absent here). So we assert
the GATE result (the genuinely-settable set) matches erpayo's known set, prove the
fixed-on-his-unit toggles gate OFF (the anti-"No disponible" assertion), and pin
the (key -> param) maps against drift.

Stdlib unittest with inline Home Assistant stubs (the platform modules pull the
HA stack); the engine + gate run for real against reconstructed HonParameters.
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_WM_FIXTURE = REPO / "tests" / "fixtures" / "wm_candy_tca286" / "startprogram_params.json"
_TD_FIXTURE = REPO / "tests" / "fixtures" / "td_haier_hd90" / "startprogram_params.json"


def _mod(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_stubs() -> None:
    ha = _mod("homeassistant")

    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))

    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))

    exc = _mod("homeassistant.exceptions")
    base_err = getattr(exc, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exc.HomeAssistantError = base_err
    exc.ConfigEntryNotReady = getattr(exc, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {}))
    exc.ConfigEntryAuthFailed = getattr(exc, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {}))

    helpers = _mod("homeassistant.helpers")
    entity = _mod("homeassistant.helpers.entity")
    entity.DeviceInfo = getattr(entity, "DeviceInfo", dict)
    dr = _mod("homeassistant.helpers.device_registry")
    dr.DeviceEntryType = getattr(dr, "DeviceEntryType", type("DeviceEntryType", (), {"SERVICE": "service"}))
    ep = _mod("homeassistant.helpers.entity_platform")
    ep.AddEntitiesCallback = getattr(ep, "AddEntitiesCallback", object)
    er = _mod("homeassistant.helpers.entity_registry")
    er.async_get = getattr(er, "async_get", lambda hass: None)
    er.async_entries_for_config_entry = getattr(
        er, "async_entries_for_config_entry", lambda registry, entry_id: []
    )
    uc = _mod("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
            self.hass = getattr(coordinator, "hass", None)

        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

        @property
        def available(self) -> bool:
            return self.coordinator.last_update_success

    # FORCE-ASSIGN (not getattr): this module imports the addhon entities below, binding
    # HonBaseEntity to whatever CoordinatorEntity is installed now. If collected first it
    # must bind a COMPLETE base (hass + async_write_ha_state + available, mirroring
    # test_ac_write_path) so it never poisons the entity tests that DO instantiate.
    uc.CoordinatorEntity = CoordinatorEntity
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

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

    components = _mod("homeassistant.components")
    _mod("homeassistant.components.switch").SwitchEntity = type("SwitchEntity", (), {})
    _mod("homeassistant.components.select").SelectEntity = type("SelectEntity", (), {})

    number_mod = _mod("homeassistant.components.number")
    import dataclasses

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class NumberEntityDescription:
        key: str
        name: str | None = None
        translation_key: str | None = None
        icon: str | None = None
        device_class: object | None = None
        native_unit_of_measurement: str | None = None
        mode: object | None = None

    number_mod.NumberEntityDescription = getattr(number_mod, "NumberEntityDescription", NumberEntityDescription)
    number_mod.NumberEntity = getattr(number_mod, "NumberEntity", type("NumberEntity", (), {}))
    number_mod.NumberDeviceClass = getattr(number_mod, "NumberDeviceClass", type("NumberDeviceClass", (), {"TEMPERATURE": "temperature"}))
    number_mod.NumberMode = getattr(number_mod, "NumberMode", type("NumberMode", (), {"AUTO": "auto", "BOX": "box", "SLIDER": "slider"}))

    ha.config_entries = ce
    ha.core = core
    ha.exceptions = exc
    ha.helpers = helpers
    ha.const = const
    ha.components = components
    helpers.entity = entity
    helpers.entity_platform = ep
    helpers.entity_registry = er
    helpers.update_coordinator = uc
    helpers.device_registry = dr
    components.number = number_mod


_install_stubs()

from custom_components.addhon import number, select, switch  # noqa: E402
from custom_components.addhon.client.engine.commands import HonCommand  # noqa: E402
from custom_components.addhon.const import (  # noqa: E402
    APPLIANCE_TD,
    APPLIANCE_WD,
    APPLIANCE_WM,
)
from custom_components.addhon.program_options import (  # noqa: E402
    is_settable_option,
    startprogram_option_param,
)

# erpayo's genuinely-settable program-option params per model (discussion #35).
_KNOWN_SETTABLE = {
    APPLIANCE_WM: {
        "spinSpeed", "temp", "delayTime",
        "extraRinse1", "extraRinse2", "extraRinse3", "acquaplus",
    },
    APPLIANCE_TD: {
        "dryLevel", "tempLevel", "delayTime",
        "antiCreaseTime", "sterilizationStatus", "tumblingStatus",
    },
}


def _build_appliance(fixture_path: Path):
    """Reconstruct a startProgram HonCommand from a fixture schema."""
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    params = data["startprogram_params"]
    appliance = types.SimpleNamespace(zone="", commands={})
    command = HonCommand("startProgram", {"parameters": params}, appliance, None, "")
    appliance.commands["startProgram"] = command
    return appliance, set(params)


def _catalog_entries(app_type: str):
    """(param, drop) for every catalog control applicable to app_type."""
    for d in switch._PROGRAM_OPTION_SWITCHES:
        if app_type in d.types:
            yield d.param, frozenset()
    for d in select._PROGRAM_OPTION_SELECTS:
        if app_type in d.types:
            yield d.param, d.drop
    for d in number._PROGRAM_OPTION_NUMBERS:
        if app_type in d.types:
            yield d.param, frozenset()


class GateMatchesRealDeviceTest(unittest.TestCase):
    """The schema-driven gate must yield EXACTLY erpayo's settable set on each model."""

    def _settable_from_catalog(self, fixture_path: Path, app_type: str) -> set[str]:
        appliance, _ = _build_appliance(fixture_path)
        settable: set[str] = set()
        for param, drop in _catalog_entries(app_type):
            if is_settable_option(startprogram_option_param(appliance, param), drop):
                settable.add(param)
        return settable

    def test_wm_settable_set_matches(self) -> None:
        self.assertEqual(
            self._settable_from_catalog(_WM_FIXTURE, APPLIANCE_WM),
            _KNOWN_SETTABLE[APPLIANCE_WM],
        )

    def test_td_settable_set_matches(self) -> None:
        self.assertEqual(
            self._settable_from_catalog(_TD_FIXTURE, APPLIANCE_TD),
            _KNOWN_SETTABLE[APPLIANCE_TD],
        )

    def test_known_settable_params_exist_in_schema(self) -> None:
        # The params we rely on must be present (a rename in the catalog would
        # otherwise silently drop the control).
        for fixture, app_type in ((_WM_FIXTURE, APPLIANCE_WM), (_TD_FIXTURE, APPLIANCE_TD)):
            _, present = _build_appliance(fixture)
            missing = _KNOWN_SETTABLE[app_type] - present
            self.assertEqual(missing, set(), f"{app_type}: settable params absent from schema: {sorted(missing)}")

    def test_fixed_toggles_gate_off(self) -> None:
        # The anti-"No disponible" assertion: erpayo's WM prewash/hygiene/anticrease/
        # goodNight/dirtyLevel are FIXED on his unit, so the gate must NOT create them.
        appliance, present = _build_appliance(_WM_FIXTURE)
        for param in ("prewash", "hygiene", "anticrease", "goodNight", "dirtyLevel"):
            self.assertIn(param, present, f"{param} should be in the WM fixture")
            self.assertFalse(
                is_settable_option(startprogram_option_param(appliance, param)),
                f"{param} is fixed on erpayo's WM and must gate OFF (no 'No disponible' control)",
            )
        # TD: dryTimeMM is fixed [0] on his dryer.
        appliance, present = _build_appliance(_TD_FIXTURE)
        self.assertIn("dryTimeMM", present)
        self.assertFalse(is_settable_option(startprogram_option_param(appliance, "dryTimeMM")))

    def test_junk_params_excluded_by_allowlist(self) -> None:
        # 'lang' passes the >= 2 gate (range 0..25) but is junk; the allowlist excludes
        # it by omission -> it must never be a catalog param.
        all_params = {p for d in switch._PROGRAM_OPTION_SWITCHES for p in (d.param,)}
        all_params |= {d.param for d in select._PROGRAM_OPTION_SELECTS}
        all_params |= {d.param for d in number._PROGRAM_OPTION_NUMBERS}
        for junk in ("lang", "energyLabel", "programFamily", "programCluster"):
            self.assertNotIn(junk, all_params, f"junk param '{junk}' must not be in the catalog")


class CatalogPinTest(unittest.TestCase):
    """Drift guard: the (key -> param) maps are pinned to the values verified against
    erpayo's real WM+TD + the decompiled hOn app (discussion #35). Any change must be
    re-verified against a real device (see GateMatchesRealDeviceTest)."""

    _PINNED_SWITCHES = {
        "extra_rinse_1": "extraRinse1",
        "extra_rinse_2": "extraRinse2",
        "extra_rinse_3": "extraRinse3",
        "acquaplus": "acquaplus",
        "prewash": "prewash",
        "hygiene": "hygiene",
        "anticrease": "anticrease",
        "good_night": "goodNight",
        "sterilization": "sterilizationStatus",
        "tumbling": "tumblingStatus",
        "permanent_press": "permanentPressStatus",
        "anti_crease_time": "antiCreaseTime",
    }
    _PINNED_SELECTS = {
        # (key, param) pairs: dry_level appears twice (type-gated WM/WD vs TD).
        ("dry_level", "dryLevel"),
        ("dirty_level", "dirtyLevel"),
        ("steam_level", "steamLevel"),
        ("temp_level", "tempLevel"),
        ("spin_speed", "spinSpeed"),
        ("wash_temp", "temp"),
    }
    _PINNED_NUMBERS = {"delay_time": "delayTime"}

    def test_switch_catalog_pinned(self) -> None:
        actual = {d.key: d.param for d in switch._PROGRAM_OPTION_SWITCHES}
        self.assertEqual(actual, self._PINNED_SWITCHES)

    def test_select_catalog_pinned(self) -> None:
        actual = {(d.key, d.param) for d in select._PROGRAM_OPTION_SELECTS}
        self.assertEqual(actual, self._PINNED_SELECTS)

    def test_number_catalog_pinned(self) -> None:
        actual = {d.key: d.param for d in number._PROGRAM_OPTION_NUMBERS}
        self.assertEqual(actual, self._PINNED_NUMBERS)

    def test_dry_level_type_gated(self) -> None:
        # The two dry_level descriptions must have DISJOINT types and DISTINCT label
        # maps (value "1" = EXTRA_DRY on WM/WD vs IRON_DRY on TD).
        dry = [d for d in select._PROGRAM_OPTION_SELECTS if d.key == "dry_level"]
        self.assertEqual(len(dry), 2)
        types_a, types_b = set(dry[0].types), set(dry[1].types)
        self.assertEqual(types_a & types_b, set(), "dry_level types must be disjoint")
        self.assertEqual(types_a | types_b, {APPLIANCE_WM, APPLIANCE_WD, APPLIANCE_TD})
        self.assertNotEqual(dry[0].label_map, dry[1].label_map)
        self.assertNotEqual(dry[0].label_map["1"], dry[1].label_map["1"])

    def test_catalog_keys_and_params_unique_within_type(self) -> None:
        # No two controls of the same appliance type may share a param (would write
        # the same option twice) or a unique_id suffix collision.
        for app_type in (APPLIANCE_WM, APPLIANCE_WD, APPLIANCE_TD):
            params = [p for p, _ in _catalog_entries(app_type)]
            self.assertEqual(len(params), len(set(params)), f"{app_type}: duplicate catalog param")


if __name__ == "__main__":
    unittest.main()
