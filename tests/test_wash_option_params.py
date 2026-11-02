# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

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

import asyncio
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
_DW_FIXTURE = REPO / "tests" / "fixtures" / "dw_haier_xs6b0s3fsb" / "startprogram_params.json"
# Issue #112: two washers of the newer schemas, from the reporters' beta7 diagnostics.
_WM_HAIER_FIXTURE = REPO / "tests" / "fixtures" / "wm_haier_hw80_b14959tu1s" / "startprogram_params.json"
_WM_CANDY_HQD_FIXTURE = REPO / "tests" / "fixtures" / "wm_candy_bp49sbl8fr" / "startprogram_params.json"
# Issue #138: a Haier HW90 of the same HQD family, whose schema offers the stain type and
# the anti-crease status.
_WM_HW90_FIXTURE = REPO / "tests" / "fixtures" / "wm_haier_hw90_b14387tu1s" / "startprogram_params.json"
# Issue #142: a CHG dryer, whose schema offers the plain `anticrease` and `bestIroning`.
_TD_CHG_FIXTURE = REPO / "tests" / "fixtures" / "td_hle_h9a3te80" / "startprogram_params.json"
# hon-test-data dryer catalogues, two CHG and four HQD models.
_TD_CATALOGS = REPO / "tests" / "fixtures" / "td_duration" / "catalogs.json"


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

    # getattr-guarded: conftest is imported before any test module and installs the
    # COMPLETE base, so binding HonBaseEntity here is already safe. Force-assigning
    # would poison the entity tests with this narrower class instead.
    uc.CoordinatorEntity = getattr(uc, "CoordinatorEntity", CoordinatorEntity)
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
    switch_mod = _mod("homeassistant.components.switch")
    switch_mod.SwitchEntity = getattr(
        switch_mod, "SwitchEntity", type("SwitchEntity", (), {})
    )
    select_mod = _mod("homeassistant.components.select")
    select_mod.SelectEntity = getattr(
        select_mod, "SelectEntity", type("SelectEntity", (), {})
    )

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
    APPLIANCE_DW,
    APPLIANCE_PROGRAM_GROUP,
    APPLIANCE_TD,
    APPLIANCE_WD,
    APPLIANCE_WM,
    DIVERTER_LEVEL_LABELS,
    STAIN_TYPE_MAP,
)
from custom_components.addhon.program_options import (  # noqa: E402
    couple_half_load_to_basket,
    has_stain_rules,
    is_half_load_with_diverter,
    is_settable_option,
    normalize_code,
    option_choices,
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
        "anti_crease_status": "antiCreaseStatus",  # washers, issue #138
        "best_ironing": "bestIroning",  # CHG dryers, issue #142
        # Washers of the newer schemas (issue #112).
        "night_wash": "nightWashStatus",
        "silent_mode": "silentMode",
        "soak_prewash": "haier_SoakPrewashSelection",
        "fast_mode": "fastMode",
        "intensive_mode": "intensiveMode",
        # Dishwasher (issue #106). `hygiene` above now also serves DW; these six are DW-only.
        "eco_express": "ecoExpress",
        "half_load": "halfLoad",
        "extra_dry": "extraDry",
        "intensive": "intensive",
        "auto_open_door": "openDoor",
        "tabs": "tabStatus",
    }
    _PINNED_SELECTS = {
        # (key, param) pairs: dry_level appears twice (type-gated WM/WD vs TD).
        ("dry_level", "dryLevel"),
        ("dirty_level", "dirtyLevel"),
        ("steam_level", "steamLevel"),
        ("temp_level", "tempLevel"),
        ("spin_speed", "spinSpeed"),
        ("wash_temp", "temp"),
        ("diverter_level", "diverterLevel"),  # dishwasher, issue #106
        ("rinse_iterations", "rinseIterations"),  # washers, issue #112
        ("stain_type", "stainType"),  # washers, issue #138
        ("dry_time", "dryTime"),  # CHG dryers, issue #142
        ("dry_time_mm", "dryTimeMM"),  # HQD dryers, issue #142
    }
    _PINNED_NUMBERS = {
        "delay_time": "delayTime",
        "main_wash_time": "mainWashTime",  # washers, issue #112
    }

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
        # No two controls of the same appliance type may share a param (would write the
        # same option twice). And within a PLATFORM no two may share a key: the entity
        # unique_id is f"{appliance_id}_opt_{key}" scoped per platform, so a same-key
        # collision in one platform clashes (cross-platform reuse is fine -- different
        # entity domains). This is the unique_id-suffix guard the comment promised.
        for app_type in (APPLIANCE_WM, APPLIANCE_WD, APPLIANCE_TD):
            params = [p for p, _ in _catalog_entries(app_type)]
            self.assertEqual(len(params), len(set(params)), f"{app_type}: duplicate catalog param")
            for platform, catalog in (
                ("switch", switch._PROGRAM_OPTION_SWITCHES),
                ("select", select._PROGRAM_OPTION_SELECTS),
                ("number", number._PROGRAM_OPTION_NUMBERS),
            ):
                keys = [d.key for d in catalog if app_type in d.types]
                self.assertEqual(
                    len(keys),
                    len(set(keys)),
                    f"{app_type}/{platform}: duplicate catalog key (unique_id collision)",
                )


class DishwasherControlsTest(unittest.TestCase):
    """Issue #106: the XS 6B0S3FSB dishwasher gets the controls its schema declares.

    Every expectation below is read off the real appliance -- the fixture is its active
    `startProgram` category, rebuilt from the diagnostics attached to #106 -- never
    written from the catalogue side. A test that derived its answer from the catalogue
    would pass on any catalogue.
    """

    # What the gate must produce on that category: seven toggles, the basket selector and
    # the delayed start. Measured on the dump, `eco` programme, functionalId 2.
    _EXPECTED = {
        "ecoExpress", "halfLoad", "extraDry", "hygiene", "intensive", "openDoor",
        "tabStatus", "diverterLevel", "delayTime",
    }

    def test_the_dishwasher_is_in_the_programme_group(self) -> None:
        # The one line issue #106 was about: the family check ran BEFORE the capability
        # checks, and DW was not in the family, so none of them was ever reached.
        self.assertIn(APPLIANCE_DW, APPLIANCE_PROGRAM_GROUP)

    def test_the_settable_set_matches_the_real_dishwasher(self) -> None:
        appliance, _ = _build_appliance(_DW_FIXTURE)
        settable = {
            param
            for param, drop in _catalog_entries(APPLIANCE_DW)
            if is_settable_option(startprogram_option_param(appliance, param), drop)
        }
        self.assertEqual(settable, self._EXPECTED)

    def test_the_fixed_toggles_create_nothing(self) -> None:
        # Fixed in 42 programmes out of 42 on that appliance. They are present in the
        # schema -- that is checked first, so this cannot pass by the param vanishing --
        # and the gate has to turn them down.
        appliance, present = _build_appliance(_DW_FIXTURE)
        for param in ("threeInOne", "autoDose", "opt10"):
            self.assertIn(param, present, f"{param} should be in the DW fixture")
            self.assertFalse(
                is_settable_option(startprogram_option_param(appliance, param)),
                f"{param} is fixed on the XS 6B0S3FSB and must create no entity",
            )

    def test_the_basket_selector_offers_off_and_the_three_baskets(self) -> None:
        # The device declares 0/1/2/6 and every programme defaults to "0". The app names
        # "0" OFF (`getDiverterLevelLabel` @decomp.txt:5037045), so it is a choice here
        # too: dropping it left the select `unknown` whenever no basket was picked.
        appliance, _ = _build_appliance(_DW_FIXTURE)
        param = startprogram_option_param(appliance, "diverterLevel")
        choices = option_choices(param)
        self.assertEqual(sorted(choices), ["0", "1", "2", "6"])
        self.assertEqual(
            [DIVERTER_LEVEL_LABELS[c] for c in sorted(choices)],
            ["off", "upper", "lower", "both"],
        )

    def test_hygiene_serves_the_dishwasher_without_leaving_the_washers(self) -> None:
        # One row for both families. Adding DW must not have cost WM or WD their switch.
        row = next(d for d in switch._PROGRAM_OPTION_SWITCHES if d.key == "hygiene")
        for app_type in (APPLIANCE_WM, APPLIANCE_WD, APPLIANCE_DW):
            self.assertIn(app_type, row.types)
        self.assertNotIn(APPLIANCE_TD, row.types)

    def test_no_dishwasher_row_leaks_onto_the_washers(self) -> None:
        # The legacy slots mean different things on the two families (`opt7` is
        # `extraRinse3` on a washer and `hygiene` here), so a DW-only row applying to a
        # washer would be a control for a parameter that is not what its label says.
        dw_only = {"eco_express", "half_load", "extra_dry", "intensive", "auto_open_door", "tabs"}
        for d in switch._PROGRAM_OPTION_SWITCHES:
            if d.key in dw_only:
                self.assertEqual(tuple(d.types), (APPLIANCE_DW,), d.key)
        dv = next(d for d in select._PROGRAM_OPTION_SELECTS if d.key == "diverter_level")
        self.assertEqual(tuple(dv.types), (APPLIANCE_DW,))


# `model_attributes.option` of the #106 dishwasher, verbatim from its beta2 dump. The
# matrix itself is irrelevant here: the app only asks whether the key is there.
_DW_OPTION = "openDoor|express|tabStatus|extraDry|halfLoadPro|hygiene|intensive|openDoorCommand|totalCare"
_DW_MODEL = {"option": _DW_OPTION, "optCompatibility": "e30=", "series": "h20"}


class DishwasherHalfLoadBasketTest(unittest.TestCase):
    """On the #106 dishwasher the app writes `halfLoad` from the basket, never on its own.

    `isHalfLoadWithDiverter` @decomp.txt:4493117 (H-type and `halfLoadPro` in the model
    `option`) makes the half-load key open the basket drawer, and the drawer writes
    `halfLoad = basket ? '1' : '0'` beside `diverterLevel` (@4518160-4518202).
    """

    def test_the_real_model_is_coupled(self) -> None:
        self.assertTrue(is_half_load_with_diverter(types.SimpleNamespace(model_attributes=_DW_MODEL)))

    def test_h_type_through_the_matrix_alone(self) -> None:
        # The first branch of `isHType`: the matrix decides, whatever the option names.
        model = {"option": "halfLoadPro|extraDry", "optCompatibility": "e30="}
        self.assertTrue(is_half_load_with_diverter(types.SimpleNamespace(model_attributes=model)))

    def test_h_type_through_the_option_names_alone(self) -> None:
        # The second branch of `isHType`: no matrix, but all five legacy names.
        model = {"option": _DW_OPTION}
        self.assertTrue(is_half_load_with_diverter(types.SimpleNamespace(model_attributes=model)))

    def test_no_half_load_pro_is_not_coupled(self) -> None:
        model = dict(_DW_MODEL, option=_DW_OPTION.replace("halfLoadPro|", ""))
        self.assertFalse(is_half_load_with_diverter(types.SimpleNamespace(model_attributes=model)))

    def test_not_h_type_is_not_coupled(self) -> None:
        model = {"option": "halfLoadPro|extraDry"}
        self.assertFalse(is_half_load_with_diverter(types.SimpleNamespace(model_attributes=model)))

    def test_no_model_attributes_is_not_coupled(self) -> None:
        self.assertFalse(is_half_load_with_diverter(types.SimpleNamespace()))

    def _params(self, basket):
        appliance, _ = _build_appliance(_DW_FIXTURE)
        params = appliance.commands["startProgram"].parameters
        params["diverterLevel"].value = basket
        return params

    def test_a_basket_turns_half_load_on(self) -> None:
        for basket in ("1", "2", "6"):
            params = self._params(basket)
            self.assertEqual("1", couple_half_load_to_basket(params), basket)
            self.assertEqual("1", str(params["halfLoad"].value), basket)

    def test_no_basket_turns_half_load_off(self) -> None:
        params = self._params("0")
        params["halfLoad"].value = "1"
        self.assertEqual("0", couple_half_load_to_basket(params))
        self.assertEqual("0", str(params["halfLoad"].value))

    def test_a_missing_parameter_changes_nothing(self) -> None:
        params = self._params("1")
        del params["diverterLevel"]
        params["halfLoad"].value = "0"
        self.assertIsNone(couple_half_load_to_basket(params))
        self.assertEqual("0", str(params["halfLoad"].value))


class DishwasherSwitchBuilderTest(unittest.TestCase):
    """What `_appliance_switches` really builds for the #106 dishwasher."""

    def _built(self, model_attributes=None):
        appliance, _ = _build_appliance(_DW_FIXTURE)
        if model_attributes is not None:
            appliance.model_attributes = model_attributes
        data = {"type": APPLIANCE_DW, "appliance": appliance, "name": "Dishwasher", "attributes": {}}
        coordinator = types.SimpleNamespace(data={"dw1": data}, hass=None)
        return switch._appliance_switches(coordinator, "dw1", data, None)

    def test_the_seven_option_switches_are_built(self) -> None:
        keys = {e._desc.key for e in self._built() if hasattr(e, "_desc")}
        self.assertEqual(
            keys,
            {"eco_express", "half_load", "extra_dry", "hygiene", "intensive", "auto_open_door", "tabs"},
        )

    def test_a_coupled_model_gets_no_half_load_switch(self) -> None:
        # The basket select carries the half load there, as in the app: a separate
        # switch would be overwritten by the basket at Start.
        keys = {e._desc.key for e in self._built(_DW_MODEL) if hasattr(e, "_desc")}
        self.assertEqual(
            keys,
            {"eco_express", "extra_dry", "hygiene", "intensive", "auto_open_door", "tabs"},
        )

    def test_no_pause_switch_without_pause_commands(self) -> None:
        # Joining the programme group makes DW eligible for the pause switch; this model
        # declares neither `pauseProgram` nor `resumeProgram`, so eligible is all it is.
        built = self._built()
        self.assertFalse(
            any(type(e).__name__ == "HonWashingMachinePauseSwitch" for e in built),
            [type(e).__name__ for e in built],
        )


class NewerWasherSchemaTest(unittest.TestCase):
    """Issue #112: the options of the newer washer schemas, on two real appliances.

    The expected sets are the `program_options` settable sets the reporters' own
    diagnostics printed for the category each fixture carries, minus the parameters the
    official app never lets the user set (`delayStatus` and `energyLabel`, which are not
    in its option registry, and `creaseResistSoakStatus`, which it excludes by name) and
    `programCluster`, which no catalogue row names: read off the dumps, never derived
    from the catalogue.
    """

    _EXPECTED = {
        # Haier HW80-B14959TU1-S, category `hqd_smart`.
        _WM_HAIER_FIXTURE: {
            "delayTime", "haier_SoakPrewashSelection", "mainWashTime", "nightWashStatus",
            "permanentPressStatus", "rinseIterations", "spinSpeed", "temp",
        },
        # Candy BP 49SBL8-FR, category `cottons_eu`.
        _WM_CANDY_HQD_FIXTURE: {
            "anticrease", "delayTime", "fastMode", "intensiveMode", "nightWashStatus",
            "permanentPressStatus", "prewash", "silentMode", "spinSpeed", "temp",
        },
        # Haier HW90-B14387TU1-S, category `iot_wash_bed_linen` (issue #138).
        _WM_HW90_FIXTURE: {
            "antiCreaseStatus", "delayTime", "haier_SoakPrewashSelection", "nightWashStatus",
            "permanentPressStatus", "rinseIterations", "spinSpeed", "stainType", "temp",
        },
    }

    def _built(self, fixture):
        appliance, _ = _build_appliance(fixture)
        data = {"type": APPLIANCE_WM, "appliance": appliance, "name": "Washer", "attributes": {}}
        coordinator = types.SimpleNamespace(data={"wm1": data}, hass=None)
        return switch._appliance_switches(coordinator, "wm1", data, None)

    def test_the_settable_sets_match_the_reporters_appliances(self) -> None:
        for fixture, expected in self._EXPECTED.items():
            with self.subTest(fixture=fixture.parent.name):
                appliance, _ = _build_appliance(fixture)
                settable = {
                    param
                    for param, drop in _catalog_entries(APPLIANCE_WM)
                    if is_settable_option(startprogram_option_param(appliance, param), drop)
                }
                self.assertEqual(settable, expected)

    def test_the_params_the_app_never_sets_create_nothing(self) -> None:
        # Settable in the schema, yet no control: they are in no catalogue table.
        catalogue = {param for param, _ in _catalog_entries(APPLIANCE_WM)}
        for fixture in self._EXPECTED:
            _, present = _build_appliance(fixture)
            for param in ("delayStatus", "energyLabel", "creaseResistSoakStatus"):
                if param in present:
                    self.assertNotIn(param, catalogue, param)

    def test_the_soak_pre_wash_switches_on_to_2(self) -> None:
        # The app sends haier_SoakPrewashSelection "2" when Pre-wash is on (the command
        # history of the Haier reporter); its schema is a range 0..2 step 2.
        soak = next(
            e for e in self._built(_WM_HAIER_FIXTURE)
            if getattr(e, "_desc", None) is not None and e._desc.key == "soak_prewash"
        )
        self.assertEqual((soak._off, soak._on), ("0", "2"))

    def test_keep_fresh_is_built_on_a_washer(self) -> None:
        for fixture in self._EXPECTED:
            with self.subTest(fixture=fixture.parent.name):
                keys = {e._desc.key for e in self._built(fixture) if hasattr(e, "_desc")}
                self.assertIn("permanent_press", keys)
                self.assertIn("night_wash", keys)

    def test_rinses_and_main_wash_time_take_the_schema_values(self) -> None:
        appliance, _ = _build_appliance(_WM_HAIER_FIXTURE)
        rinses = option_choices(startprogram_option_param(appliance, "rinseIterations"))
        self.assertEqual(rinses, ["0", "1", "2", "3", "4", "5"])
        wash_time = option_choices(startprogram_option_param(appliance, "mainWashTime"))
        self.assertEqual(wash_time, [str(m) for m in range(5, 55, 5)])


class ChgDryerAntiCreaseTest(unittest.TestCase):
    """Issue #142: the CHG dryer gets the anti-crease and Best Ironing switches.

    The HLE H9A3TE-80/N declares `anticrease` and `bestIroning` as 0/1 ranges; the dump
    shows no entity for either. Both switches are disabled by default on a dryer, while
    the washers keep their anti-crease switch enabled.
    """

    def _switches(self):
        appliance, _ = _build_appliance(_TD_CHG_FIXTURE)
        data = {"type": APPLIANCE_TD, "appliance": appliance, "name": "Dryer", "attributes": {}}
        coordinator = types.SimpleNamespace(data={"td1": data}, hass=None)
        built = switch._appliance_switches(coordinator, "td1", data, None)
        return {e._desc.key: e for e in built if hasattr(e, "_desc")}

    def test_the_settable_set_matches_the_real_dryer(self) -> None:
        appliance, _ = _build_appliance(_TD_CHG_FIXTURE)
        settable = {
            param
            for param, drop in _catalog_entries(APPLIANCE_TD)
            if is_settable_option(startprogram_option_param(appliance, param), drop)
        }
        self.assertEqual(
            settable, {"anticrease", "bestIroning", "dryLevel", "dryTime", "delayTime"}
        )

    def test_both_switches_are_built_off_0_on_1(self) -> None:
        built = self._switches()
        self.assertEqual(set(built), {"anticrease", "best_ironing"})
        for key in ("anticrease", "best_ironing"):
            self.assertEqual((built[key]._off, built[key]._on), ("0", "1"), key)
        self.assertEqual(built["anticrease"]._attr_unique_id, "td1_opt_anticrease")
        self.assertEqual(built["best_ironing"]._attr_unique_id, "td1_opt_best_ironing")

    def test_disabled_on_the_dryer_enabled_on_the_washers(self) -> None:
        built = self._switches()
        for key in ("anticrease", "best_ironing"):
            self.assertIs(built[key]._attr_entity_registry_enabled_default, False, key)
        rows = [d for d in switch._PROGRAM_OPTION_SWITCHES if d.param == "anticrease"]
        washer = next(d for d in rows if APPLIANCE_WM in d.types)
        self.assertEqual(tuple(washer.types), (APPLIANCE_WM, APPLIANCE_WD))
        self.assertIs(washer.enabled_default, True)

    def test_no_dryer_catalogue_has_both_anti_crease_names(self) -> None:
        # Two rows of one label on a dryer would be two "Anti-crease" switches. On the six
        # hon-test-data dryers the CHG pair and the HQD timer never meet.
        models = json.loads(_TD_CATALOGS.read_text(encoding="utf-8"))["models"]
        seen = set()
        for name, model in models.items():
            names = set()
            for category in model["categories"].values():
                names |= set(category.get("parameters", {}))
            with self.subTest(model=name):
                self.assertFalse({"anticrease", "antiCreaseTime"} <= names)
            seen.add(("anticrease" in names, "bestIroning" in names, "antiCreaseTime" in names))
        self.assertEqual(seen, {(True, True, False), (False, False, True)})


class _ListeningCoordinator(types.SimpleNamespace):
    """The bits of a coordinator a buffered option touches."""

    def __init__(self, data: dict) -> None:
        super().__init__(data=data, hass=None, last_update_success=True, broadcasts=0)

    def async_update_listeners(self) -> None:
        self.broadcasts += 1


def _build_chg_dryer(
    rules: bool = True, pin_dry_level: str | None = None, pin_dry_time: str | None = None
):
    """The #142 dryer as the engine loads it: schema, slot map and, unless told
    otherwise, the category's `programRules`."""
    data = json.loads(_TD_CHG_FIXTURE.read_text(encoding="utf-8"))
    params = data["startprogram_params"]
    if pin_dry_level is not None:
        params["dryLevel"] = {"typology": "fixed", "fixedValue": pin_dry_level}
    if pin_dry_time is not None:
        params["dryTime"] = {"typology": "fixed", "fixedValue": pin_dry_time}
    attributes = {"parameters": params, "ancillaryParameters": data["ancillary_params"]}
    if rules:
        attributes["ancillaryParameters"]["programRules"] = {
            "category": "rule", "typology": "fixed", "fixedValue": data["program_rules"]
        }
    appliance = types.SimpleNamespace(zone="", commands={}, options=data["appliance_options"], info={})
    appliance.commands["startProgram"] = HonCommand("startProgram", attributes, appliance, None, "")
    return appliance


class ChgDryerBestIroningLevelTest(unittest.IsolatedAsyncioTestCase):
    """Issue #142: Best Ironing and the dry level move together, as in the app.

    On: dry level "1" (`checkAndApplyBestIroningRules`, apk2 decomp.txt:2638397). Off,
    after on: the programme's level. A dry level of 2, 3 or 4: Best Ironing "0", from the
    catalogue's `programRules`. Anti-crease turning Best Ironing off leaves the level.
    """

    def _entities(self, **build):
        appliance = _build_chg_dryer(**build)
        data = {"type": APPLIANCE_TD, "appliance": appliance, "name": "Dryer", "attributes": {}}
        coordinator = _ListeningCoordinator({"td1": data})

        def switch_for(key):
            desc = next(
                d for d in switch._PROGRAM_OPTION_SWITCHES if d.key == key and APPLIANCE_TD in d.types
            )
            return switch.HonProgramOptionSwitch(coordinator, "td1", desc, None)

        level_desc = next(
            d for d in select._PROGRAM_OPTION_SELECTS if d.key == "dry_level" and APPLIANCE_TD in d.types
        )
        level = select.HonProgramOptionSelect(coordinator, "td1", level_desc, None)
        return appliance, coordinator, switch_for("best_ironing"), switch_for("anticrease"), level

    @staticmethod
    def _buffer(coordinator) -> dict:
        return coordinator.pending_options["td1"]

    async def test_best_ironing_on_buffers_iron_dry(self) -> None:
        _, coordinator, best, _, level = self._entities()
        await best.async_turn_on()
        # The dump's anti-crease is "1", so the exclusion buffers it off too.
        self.assertEqual(
            {"bestIroning": "1", "anticrease": "0", "dryLevel": "1"}, self._buffer(coordinator)
        )
        self.assertEqual("iron_dry", level.current_option)
        self.assertEqual(1, coordinator.broadcasts)

    async def test_best_ironing_off_gives_the_programme_its_level_back(self) -> None:
        _, coordinator, best, _, level = self._entities()
        await level.async_select_option("hang_dry")
        await best.async_turn_on()
        await best.async_turn_off()
        # The programme's level (the schema's "4"), not the "2" picked before.
        self.assertEqual("4", self._buffer(coordinator)["dryLevel"])
        self.assertEqual("0", self._buffer(coordinator)["bestIroning"])
        self.assertEqual("extra_dry", level.current_option)

    async def test_turning_off_a_best_ironing_already_off_leaves_the_level(self) -> None:
        _, coordinator, best, _, level = self._entities()
        await level.async_select_option("hang_dry")
        await best.async_turn_off()
        self.assertEqual({"dryLevel": "2", "bestIroning": "0"}, self._buffer(coordinator))

    async def test_a_pinned_level_is_left_alone(self) -> None:
        _, coordinator, best, _, _ = self._entities(pin_dry_level="0")
        await best.async_turn_on()
        self.assertEqual({"bestIroning": "1", "anticrease": "0"}, self._buffer(coordinator))
        await best.async_turn_off()
        self.assertEqual({"bestIroning": "0", "anticrease": "0"}, self._buffer(coordinator))

    async def test_a_level_of_2_3_or_4_turns_best_ironing_off(self) -> None:
        for option, code in (("hang_dry", "2"), ("cupboard", "3"), ("extra_dry", "4")):
            with self.subTest(option=option):
                _, coordinator, best, _, level = self._entities()
                await best.async_turn_on()
                await level.async_select_option(option)
                self.assertEqual("0", self._buffer(coordinator)["bestIroning"])
                self.assertEqual(code, self._buffer(coordinator)["dryLevel"])
                self.assertFalse(best.is_on)

    async def test_iron_dry_keeps_best_ironing(self) -> None:
        _, coordinator, best, _, level = self._entities()
        await best.async_turn_on()
        await level.async_select_option("iron_dry")
        self.assertEqual("1", self._buffer(coordinator)["bestIroning"])

    async def test_without_the_catalogue_rule_the_level_leaves_best_ironing(self) -> None:
        _, coordinator, best, _, level = self._entities(rules=False)
        await best.async_turn_on()
        await level.async_select_option("cupboard")
        self.assertEqual("1", self._buffer(coordinator)["bestIroning"])
        self.assertEqual("3", self._buffer(coordinator)["dryLevel"])

    async def test_anti_crease_turning_best_ironing_off_keeps_iron_dry(self) -> None:
        _, coordinator, best, anti, _ = self._entities()
        await best.async_turn_on()
        await anti.async_turn_on()
        self.assertEqual(
            {"bestIroning": "0", "anticrease": "1", "dryLevel": "1"}, self._buffer(coordinator)
        )

    async def test_the_buffer_reaches_the_command_as_shown(self) -> None:
        # The catalogue's rules fire while the Start writes the buffer: what the entities
        # show is what the command carries.
        from custom_components.addhon.program_options import apply_pending_options

        for steps, expected in (
            (("best_on",), {"bestIroning": "1", "anticrease": "0", "dryLevel": "1"}),
            (("best_on", "cupboard"), {"bestIroning": "0", "anticrease": "0", "dryLevel": "3"}),
            (("best_on", "best_off"), {"bestIroning": "0", "anticrease": "0", "dryLevel": "4"}),
        ):
            with self.subTest(steps=steps):
                appliance, coordinator, best, _, level = self._entities()
                for step in steps:
                    if step == "best_on":
                        await best.async_turn_on()
                    elif step == "best_off":
                        await best.async_turn_off()
                    else:
                        await level.async_select_option(step)
                params = appliance.commands["startProgram"].parameters
                apply_pending_options(params, self._buffer(coordinator))
                self.assertEqual(expected, {k: normalize_code(params[k].value) for k in expected})

    def test_rule_fixed_value(self) -> None:
        command = _build_chg_dryer().commands["startProgram"]
        self.assertEqual("0", command.rule_fixed_value("dryLevel", "3", "bestIroning"))
        self.assertEqual("1", command.rule_fixed_value("bestIroning", "1", "dryLevel"))
        self.assertEqual("1", command.rule_fixed_value("dryTimeMM", "30", "dryTime"))
        self.assertIsNone(command.rule_fixed_value("dryLevel", "1", "bestIroning"))
        self.assertIsNone(command.rule_fixed_value("dryLevel", "3", "dryTime"))
        self.assertIsNone(_build_chg_dryer(rules=False).commands["startProgram"].rule_fixed_value(
            "dryLevel", "3", "bestIroning"
        ))

    def test_rule_fixed_value_skips_a_rule_with_extra_conditions(self) -> None:
        # `tempSel` is 26 only with ecoMode 1 AND machMode 1: one trigger cannot answer it.
        def node():
            return {"typology": "range", "minimumValue": 0, "maximumValue": 30, "incrementValue": 1, "defaultValue": 0}

        rules = {"tempSel": {"ecoMode": {"1": {"machMode": {"1": {"typology": "fixed", "fixedValue": "26"}}}}}}
        appliance = types.SimpleNamespace(zone="", commands={}, options={}, info={})
        command = HonCommand(
            "startProgram",
            {
                "parameters": {"tempSel": node(), "ecoMode": node(), "machMode": node()},
                "ancillaryParameters": {"programRules": {"category": "rule", "typology": "fixed", "fixedValue": rules}},
            },
            appliance,
            None,
            "",
        )
        self.assertIsNone(command.rule_fixed_value("ecoMode", "1", "tempSel"))


def _built_selects(appliance, app_type: str) -> list[str]:
    """The keys of the program-option selects the setup would build, as select.py gates them."""
    return [
        d.key
        for d in select._PROGRAM_OPTION_SELECTS
        if app_type in d.types
        and not (d.excluded_when is not None and d.excluded_when(appliance))
        and select.HonProgramOptionSelect.supports(appliance, d.param, d.drop)
    ]


def _build_hqd_timer_dryer():
    """HQD_TIMER of the Haier HD90-A3959 DE (hon-test-data): `dryTimeMM` 30..150, level 11."""
    model = json.loads(_TD_CATALOGS.read_text(encoding="utf-8"))["models"]["HD90-A3959 DE"]
    category = json.loads(json.dumps(model["categories"]["PROGRAMS.TD.HQD_TIMER"]))
    category.pop("remainingTimes", None)
    appliance = types.SimpleNamespace(zone="", commands={}, options={}, info={})
    appliance.commands["startProgram"] = HonCommand("startProgram", category, appliance, None, "")
    return appliance


class DryerDryingTimeTest(unittest.IsolatedAsyncioTestCase):
    """Issue #142: a dryer dries to a level or for a time, as on the app's drying page.

    CHG: `dryTime` is an index the select labels in minutes (`dryTimeOptions` TD, apk2
    decomp.txt:996651); a time buffers dry level "0", a level picked while drying for a
    time buffers drying time "0" (`checkAndApplyDryingRules`, @2638175), and Best Ironing,
    which needs a level, ends a time. HQD: the timer is `dryTimeMM` itself, in minutes.
    """

    def _entities(self, app_type=APPLIANCE_TD, attributes=None, **build):
        appliance = _build_chg_dryer(**build)
        data = {
            "type": app_type, "appliance": appliance, "name": "Dryer",
            "attributes": dict(attributes or {}),
        }
        coordinator = _ListeningCoordinator({"td1": data})

        def select_for(key):
            # The description of the appliance under test: a washer-dryer's dry level is
            # the `_WASH_TYPES` row, not the dryer's, and it has no drying-time select.
            desc = next(
                (d for d in select._PROGRAM_OPTION_SELECTS if d.key == key and app_type in d.types),
                None,
            )
            if desc is None:
                return None
            return select.HonProgramOptionSelect(coordinator, "td1", desc, None)

        best_desc = next(
            d for d in switch._PROGRAM_OPTION_SWITCHES if d.key == "best_ironing"
        )
        best = switch.HonProgramOptionSwitch(coordinator, "td1", best_desc, None)
        return coordinator, select_for("dry_time"), select_for("dry_level"), best

    @staticmethod
    def _buffer(coordinator) -> dict:
        return coordinator.pending_options["td1"]

    def test_the_chg_dryer_gets_the_time_select_only(self) -> None:
        # Its `dryTimeMM` is an ancillary parameter mirroring the index: no minutes select.
        appliance = _build_chg_dryer()
        built = _built_selects(appliance, APPLIANCE_TD)
        self.assertIn("dry_time", built)
        self.assertNotIn("dry_time_mm", built)

    async def test_the_time_select_offers_the_programmes_minutes(self) -> None:
        _, dry_time, _, _ = self._entities()
        self.assertEqual(
            ["70", "80", "90", "100", "110", "120", "130", "140", "150"], dry_time.options
        )
        self.assertIs(dry_time._attr_entity_registry_enabled_default, False)
        self.assertEqual("td1_opt_dry_time", dry_time._attr_unique_id)

    async def test_the_time_reads_unknown_while_drying_to_a_level(self) -> None:
        # The shadow reports `dryTime` 4 (70 min) beside the level: not a time to show.
        _, dry_time, level, _ = self._entities(attributes={"dryLevel": 4, "dryTime": 4})
        self.assertIsNone(dry_time.current_option)
        self.assertEqual("extra_dry", level.current_option)

    async def test_a_time_buffers_dry_level_zero(self) -> None:
        coordinator, dry_time, level, _ = self._entities()
        await dry_time.async_select_option("90")
        self.assertEqual({"dryTime": "6", "dryLevel": "0"}, self._buffer(coordinator))
        self.assertEqual("90", dry_time.current_option)
        self.assertIsNone(level.current_option)

    async def test_a_level_after_a_time_buffers_dry_time_zero(self) -> None:
        coordinator, dry_time, level, _ = self._entities()
        await dry_time.async_select_option("90")
        await level.async_select_option("extra_dry")
        self.assertEqual({"dryTime": "0", "dryLevel": "4"}, self._buffer(coordinator))
        self.assertIsNone(dry_time.current_option)

    async def test_a_level_without_a_time_leaves_the_time_alone(self) -> None:
        coordinator, _, level, _ = self._entities()
        await level.async_select_option("cupboard")
        self.assertEqual({"dryLevel": "3"}, self._buffer(coordinator))

    async def test_best_ironing_ends_a_time(self) -> None:
        coordinator, dry_time, _, best = self._entities()
        await dry_time.async_select_option("90")
        await best.async_turn_on()
        self.assertEqual(
            {"dryTime": "0", "dryLevel": "1", "bestIroning": "1", "anticrease": "0"},
            self._buffer(coordinator),
        )

    async def test_best_ironing_on_a_level_leaves_the_time_alone(self) -> None:
        coordinator, _, _, best = self._entities()
        await best.async_turn_on()
        self.assertNotIn("dryTime", self._buffer(coordinator))

    async def test_a_programme_without_a_time_choice_keeps_its_level(self) -> None:
        coordinator, dry_time, _, _ = self._entities(pin_dry_time="4")
        await dry_time.async_select_option("70")
        self.assertNotIn("dryLevel", self._buffer(coordinator))

    async def test_a_fixed_level_still_goes_to_zero_for_a_time(self) -> None:
        # antiallergy on the #142 dryer: level fixed at 4, time settable. The app's
        # `resetParamsValue` writes the "0" whatever the node (apk2 decomp.txt:2637654).
        coordinator, dry_time, _, _ = self._entities(pin_dry_level="4")
        await dry_time.async_select_option("120")
        self.assertEqual({"dryTime": "9", "dryLevel": "0"}, self._buffer(coordinator))

    @staticmethod
    def _after_a_timed_start(coordinator, favourite: bool = False) -> dict:
        """The category as a timed Start from Home Assistant leaves it: `dryLevel` "0"
        below its lowered range beside the time, both kept on success. A favourite is
        marked the way the loader marks one."""
        from custom_components.addhon.client.engine.parameter.fixed import HonParameterFixed

        command = coordinator.data["td1"]["appliance"].commands["startProgram"]
        params = command.parameters
        params["dryLevel"].min = 0
        params["dryLevel"].value = "0"
        params["dryTime"].value = "7"
        if favourite:
            params["favourite"] = HonParameterFixed("favourite", {"fixedValue": "1"}, "custom")
        return params

    async def test_a_time_after_a_timed_start_still_buffers_dry_level_zero(self) -> None:
        # The programme is picked again (it is rebuilt from its schema only at Start) and
        # a time chosen: the coupling reads the level the programme prescribes (4), not
        # the stale "0", or the Start would send the schema's level beside the time.
        coordinator, dry_time, _, _ = self._entities()
        self._after_a_timed_start(coordinator)
        await dry_time.async_select_option("110")
        self.assertEqual({"dryTime": "8", "dryLevel": "0"}, self._buffer(coordinator))

    async def test_a_favourite_prescribes_its_own_level(self) -> None:
        # A favourite IS the saved configuration: its "0" is what it prescribes.
        coordinator, dry_time, _, _ = self._entities()
        self._after_a_timed_start(coordinator, favourite=True)
        await dry_time.async_select_option("110")
        self.assertEqual({"dryTime": "8"}, self._buffer(coordinator))

    async def test_the_time_reads_unknown_once_the_programme_is_picked_again(self) -> None:
        # The timed Start left dryLevel "0" in the category; the programme, picked again,
        # prescribes its own level (4), which is what the Start would send: the gate
        # reads it as the coupling does, so the time goes unknown while the level shows.
        coordinator, dry_time, level, _ = self._entities(rules=False)
        command = coordinator.data["td1"]["appliance"].commands["startProgram"]
        command._categories = {"prog": command}
        self._after_a_timed_start(coordinator)
        coordinator.pending_programs = {"td1": "prog"}
        self.assertIsNone(dry_time.current_option)
        self.assertEqual("extra_dry", level.current_option)

    async def test_the_time_stays_while_the_timed_cycle_runs_unpicked(self) -> None:
        # No programme picked: the category's "0" is the cycle's own, the shadow's time
        # shows.
        coordinator, dry_time, _, _ = self._entities(
            rules=False, attributes={"dryLevel": "0", "dryTime": "7"}
        )
        self._after_a_timed_start(coordinator)
        self.assertEqual("100", dry_time.current_option)

    async def test_a_washer_dryer_level_never_touches_the_time(self) -> None:
        coordinator, _, level, _ = self._entities(app_type=APPLIANCE_WD)
        coordinator.pending_options = {"td1": {"dryLevel": "0", "dryTime": "6"}}
        await level.async_select_option("extra_dry")
        self.assertEqual("6", self._buffer(coordinator)["dryTime"])

    def test_dry_level_zero_reaches_the_command(self) -> None:
        from custom_components.addhon.program_options import apply_pending_options

        params = _build_chg_dryer().commands["startProgram"].parameters
        applied = apply_pending_options(params, {"dryTime": "6", "dryLevel": "0"})
        self.assertEqual(["dryTime", "dryLevel"], applied)
        self.assertEqual("0", normalize_code(params["dryLevel"].value))
        self.assertEqual("6", normalize_code(params["dryTime"].value))
        # And the way back: a level after a time, `dryTime` "0" below its range of 4..12.
        applied = apply_pending_options(params, {"dryLevel": "4", "dryTime": "0"})
        self.assertEqual("0", normalize_code(params["dryTime"].value))
        self.assertEqual("4", normalize_code(params["dryLevel"].value))

    def test_a_zero_lowers_the_minimum_and_reports_the_one_it_replaced(self) -> None:
        # The Start gives the range its minimum back after the send; it needs to know
        # which ones went down, and from where.
        from custom_components.addhon.program_options import apply_pending_options

        params = _build_chg_dryer().commands["startProgram"].parameters
        lowered: dict = {}
        applied = apply_pending_options(params, {"dryLevel": "4", "dryTime": "0"}, lowered)
        self.assertEqual(["dryLevel", "dryTime"], applied)
        self.assertEqual(0, params["dryTime"].min)
        self.assertEqual({"dryTime": 4.0}, lowered)
        # Nothing to report when no value goes below its range.
        lowered = {}
        apply_pending_options(params, {"dryLevel": "3", "dryTime": "6"}, lowered)
        self.assertEqual({}, lowered)

    def test_the_hqd_dryer_gets_the_minutes_select(self) -> None:
        appliance = _build_hqd_timer_dryer()
        built = _built_selects(appliance, APPLIANCE_TD)
        self.assertIn("dry_time_mm", built)
        self.assertNotIn("dry_time", built)
        data = {
            "type": APPLIANCE_TD, "appliance": appliance, "name": "Dryer",
            "attributes": {"dryTimeMM": 30},
        }
        coordinator = _ListeningCoordinator({"td1": data})
        desc = next(d for d in select._PROGRAM_OPTION_SELECTS if d.key == "dry_time_mm")
        minutes = select.HonProgramOptionSelect(coordinator, "td1", desc, None)
        self.assertEqual(["30", "60", "90", "120", "150"], minutes.options)
        self.assertIs(minutes._attr_entity_registry_enabled_default, False)
        # A timed programme: its pinned level 11 is the timed one, so the time shows.
        self.assertEqual("30", minutes.current_option)


class StainAndAntiCreaseTest(unittest.TestCase):
    """Issue #138: the stain type select and the anti-crease status switch.

    Both stand on the HW90-B14387TU1-S schema (`stainType` a range 0..26, `antiCreaseStatus`
    a 0/1 range), and both are disabled by default: the app offers them, the machine's
    reaction to them from here is still to be measured.
    """

    def _data(self):
        appliance, _ = _build_appliance(_WM_HW90_FIXTURE)
        data = {"type": APPLIANCE_WM, "appliance": appliance, "name": "Washer", "attributes": {}}
        return appliance, data

    def _switches(self):
        _, data = self._data()
        coordinator = types.SimpleNamespace(data={"wm1": data}, hass=None)
        return switch._appliance_switches(coordinator, "wm1", data, None)

    def _select(self, key, narrow_stain_to=None):
        appliance, data = self._data()
        if narrow_stain_to is not None:
            appliance.commands["startProgram"].parameters["stainType"].max = narrow_stain_to
        coordinator = types.SimpleNamespace(data={"wm1": data}, hass=None)
        desc = next(d for d in select._PROGRAM_OPTION_SELECTS if d.key == key)
        return select.HonProgramOptionSelect(coordinator, "wm1", desc, None)

    def test_the_stain_select_offers_the_app_codes(self) -> None:
        stain = self._select("stain_type")
        self.assertEqual(stain.options, list(STAIN_TYPE_MAP.values()))
        self.assertEqual(stain._key_to_raw["none"], "0")
        self.assertEqual(stain._key_to_raw["sweat"], "19")
        self.assertEqual(stain._key_to_raw["fruit"], "26")

    def test_a_narrower_stain_range_offers_only_its_codes(self) -> None:
        # The select offers what the range allows, never the whole map.
        stain = self._select("stain_type", narrow_stain_to=5)
        self.assertEqual(stain.options, [STAIN_TYPE_MAP[str(c)] for c in range(6)])

    def test_the_anti_crease_switch_is_off_0_on_1(self) -> None:
        built = [e for e in self._switches() if hasattr(e, "_desc")]
        anti = next(e for e in built if e._desc.key == "anti_crease_status")
        self.assertEqual((anti._off, anti._on), ("0", "1"))
        # A model has this or `anticrease`, never both: the HW90 has no `anticrease`.
        self.assertFalse(any(e._desc.key == "anticrease" for e in built))

    def test_the_two_new_entities_are_disabled_by_default(self) -> None:
        built = {e._attr_unique_id: e for e in self._switches() if hasattr(e, "_desc")}
        self.assertIs(built["wm1_opt_anti_crease_status"]._attr_entity_registry_enabled_default, False)
        stain = self._select("stain_type")
        self.assertEqual(stain._attr_unique_id, "wm1_opt_stain_type")
        self.assertIs(stain._attr_entity_registry_enabled_default, False)
        self.assertIs(self._select("rinse_iterations")._attr_entity_registry_enabled_default, True)

    @staticmethod
    def _with_rules(appliance, group):
        command = appliance.commands["startProgram"]
        command._ancillary_group = group
        command._categories = {"cat": command}
        return appliance

    def test_has_stain_rules(self) -> None:
        for group in (
            {"programRules": {"enumValues": {"temp": {"stainType": {"1": 35}}}}},
            {"programRules": {"fixedValue": {"addWashDefaultTime": {"stainType": {"1": 10}}}}},
            {"programRules": {"enumValues": {"stainType": {"native": {"wine": 1}}}}},
        ):
            with self.subTest(group=group):
                appliance, _ = self._data()
                self.assertTrue(has_stain_rules(self._with_rules(appliance, group)))
        appliance, _ = self._data()
        self.assertFalse(has_stain_rules(appliance))
        self.assertFalse(has_stain_rules(types.SimpleNamespace(commands={})))
        self.assertFalse(has_stain_rules(types.SimpleNamespace()))
        for odd in (None, "x", {"programRules": None}, {"programRules": {"enumValues": 3}}):
            with self.subTest(odd=odd):
                appliance, _ = self._data()
                self.assertFalse(has_stain_rules(self._with_rules(appliance, odd)))

    def test_has_stain_rules_on_the_shape_a_catalogue_really_carries(self) -> None:
        cases = json.loads(
            (REPO / "tests" / "fixtures" / "wm_hqd_duration" / "cases.json").read_text()
        )["models"]
        pets = cases["HW80-B14959TU1-S"]["categories"][
            "PROGRAMS.WM_WD.IOT_WASH_PETS_ODOURS_STAINS_REMOVAL"
        ]["ancillaryParameters"]
        appliance, _ = self._data()
        self.assertTrue(has_stain_rules(self._with_rules(appliance, pets)))
        bathrobe = cases["HW90-B14387TU1-S"]["categories"]["PROGRAMS.WM_WD.IOT_WASH_BATHROBE"]
        appliance, _ = self._data()
        self.assertFalse(has_stain_rules(self._with_rules(appliance, bathrobe["ancillaryParameters"])))

    def test_has_stain_rules_on_the_unwrapped_shape(self) -> None:
        # `programRules.<target>.<trigger>`, with no `enumValues`/`fixedValue` node.
        for rules in (
            {"temp": {"stainType": {"1": 35}}},
            {"addWashDefaultTime": {"stainType": {"1": 10}}},
            {"stainType": {"native": {"wine": 1}}},
        ):
            with self.subTest(rules=rules):
                appliance, _ = self._data()
                self.assertTrue(has_stain_rules(self._with_rules(appliance, {"programRules": rules})))
        for rules in ({"temp": {"other": 1}}, {"temp": 3, "addWashDefaultTime": None}, {}):
            with self.subTest(rules=rules):
                appliance, _ = self._data()
                self.assertFalse(has_stain_rules(self._with_rules(appliance, {"programRules": rules})))

    @staticmethod
    def _created_by_select_setup(appliance, data) -> set[str]:
        import asyncio

        from custom_components.addhon.const import DOMAIN

        coordinator = types.SimpleNamespace(data={"wm1": data}, hass=None)
        hass = types.SimpleNamespace(
            data={DOMAIN: {"e1": {"coordinator": coordinator, "client": None}}}
        )
        added: list = []
        asyncio.run(select.async_setup_entry(hass, types.SimpleNamespace(entry_id="e1"), added.extend))
        return {str(e._attr_unique_id) for e in added}

    def test_no_stain_select_where_stain_rules_exist(self) -> None:
        desc = next(d for d in select._PROGRAM_OPTION_SELECTS if d.key == "stain_type")
        self.assertIs(desc.excluded_when, has_stain_rules)
        appliance, data = self._data()
        self.assertIn("wm1_opt_stain_type", self._created_by_select_setup(appliance, data))
        rules = {"programRules": {"enumValues": {"temp": {"stainType": {"1": 35}}}}}
        self._with_rules(appliance, rules)
        created = self._created_by_select_setup(appliance, data)
        self.assertNotIn("wm1_opt_stain_type", created)
        self.assertIn("wm1_opt_rinse_iterations", created)

    def test_an_untouched_start_sends_zero_for_both(self) -> None:
        # The values the active category holds when nothing was buffered: the default of
        # both ranges, which is what a start sends.
        appliance, _ = self._data()
        params = appliance.commands["startProgram"].parameters
        self.assertEqual(normalize_code(params["stainType"].value), "0")
        self.assertEqual(normalize_code(params["antiCreaseStatus"].value), "0")


class StainTypeKeyTest(unittest.TestCase):
    """Issue #138: a washer start with a stain carries `stainTypeKey: "1"` in its
    ancillaries, as the app's Haier flow writes it beside `parameters.stainType`
    (apk2 decomp.txt @4910616-4910618). The HW90-B14387TU1-S declares the node, an enum
    of stain names, among its ancillaries and with no default: the node is the
    catalogue's (tests/fixtures/wm_hqd_duration), since the dump the other parameters
    come from printed the enum's materialised "0" as its `defaultValue`."""

    def _start(self, stain, *, declares_key=True, kind="WM"):
        params = json.loads(_WM_HW90_FIXTURE.read_text(encoding="utf-8"))["startprogram_params"]
        self.assertIn("stainTypeKey", params)
        params.pop("stainTypeKey")
        catalogue = json.loads(
            (REPO / "tests" / "fixtures" / "wm_hqd_duration" / "cases.json").read_text()
        )["models"]["HW90-B14387TU1-S"]["categories"]["PROGRAMS.WM_WD.HQD_COTTONS"]
        node = catalogue["ancillaryParameters"]["stainTypeKey"]
        self.assertIsNone(node.get("defaultValue"))
        schema = {"parameters": params,
                  "ancillaryParameters": {"stainTypeKey": node} if declares_key else {}}
        sent = []

        class Api:
            async def send_command(self, appliance, name, params, ancillary, category,
                                   **kwargs):
                sent.append((dict(params), dict(ancillary)))
                return True

        appliance = types.SimpleNamespace(
            appliance_type=kind, model_attributes={}, zone=0, options={}, commands={},
            api=Api(), sync_payload_to_params=lambda params: None,
            sync_params_to_command=lambda name: None,
        )
        command = HonCommand("startProgram", schema, appliance,
                             category_name="PROGRAMS.WM_WD.IOT_WASH_BED_LINEN")
        appliance.commands["startProgram"] = command
        command.parameters["stainType"].value = stain
        asyncio.run(command.send())
        return sent[-1]

    def test_a_stain_carries_the_key(self) -> None:
        for kind in ("WM", "WD"):
            with self.subTest(kind=kind):
                params, ancillary = self._start("19", kind=kind)
                self.assertEqual("19", str(params["stainType"]))
                self.assertEqual("1", ancillary["stainTypeKey"])

    def test_no_stain_no_key(self) -> None:
        params, ancillary = self._start("0")
        self.assertEqual("0", str(params["stainType"]))
        self.assertNotIn("stainTypeKey", ancillary)

    def test_a_schema_without_the_node_is_unchanged(self) -> None:
        _params, ancillary = self._start("19", declares_key=False)
        self.assertNotIn("stainTypeKey", ancillary)


if __name__ == "__main__":
    unittest.main()
