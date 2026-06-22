"""Validate that switch.py `_AC_SWITCHES` `param` names exist on a real Haier AC.

The per-type entity tests validate the entity `key` (the slug) but NOT the `param`
(the Haier command-parameter name the switch reads/writes). A wrong `param` makes a
capability-gated switch silently never created on any device, and no key-based test
catches it. This guards the `param` side against a real device's schema (fixture
tests/fixtures/ac_as35/, captured live on 2026-06-22 from an AS35PBPHRA-PRE), so a
typo such as `echoStatus` -> `ecoStatus` fails here instead of only in production.

Stdlib unittest with inline Home Assistant stubs (switch.py pulls the platform
stack); no real Home Assistant install required.
"""
from __future__ import annotations

import dataclasses
import json
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_AC_FIXTURE = REPO / "tests" / "fixtures" / "ac_as35" / "settings_command_params.json"


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
    dr.DeviceEntryType = getattr(
        dr, "DeviceEntryType", type("DeviceEntryType", (), {"SERVICE": "service"})
    )
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
    _mod("homeassistant.components.switch").SwitchEntity = type("SwitchEntity", (), {})

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


_install_stubs()

from custom_components.addhon import switch  # noqa: E402


def _real_ac_settings_params() -> set[str]:
    data = json.loads(_AC_FIXTURE.read_text(encoding="utf-8"))
    return set(data["settings_command_params"])


class AcSwitchParamRealityTest(unittest.TestCase):
    """The `param` of every AC switch must exist in a real AC's settings schema."""

    def test_every_ac_switch_param_exists_on_real_device(self):
        real = _real_ac_settings_params()
        missing = {desc.param for desc in switch._AC_SWITCHES} - real
        self.assertEqual(
            missing,
            set(),
            "These _AC_SWITCHES params are absent from the real AS35 settings schema "
            "(likely a typo - a capability-gated switch would then NEVER be created on "
            f"any AC): {sorted(missing)}. Verify against a real device before changing.",
        )

    def test_fixture_is_sane(self):
        # Guard against an empty/garbled fixture silently passing the check above.
        real = _real_ac_settings_params()
        self.assertGreater(len(real), 20)
        self.assertIn("echoStatus", real)   # the genuine Haier name (with the 'h')
        self.assertNotIn("ecoStatus", real)  # the tempting-but-wrong spelling

    def test_ac_switch_keys_and_params_unique(self):
        keys = [desc.key for desc in switch._AC_SWITCHES]
        params = [desc.param for desc in switch._AC_SWITCHES]
        self.assertEqual(len(keys), len(set(keys)), "duplicate AC switch key")
        self.assertEqual(len(params), len(set(params)), "duplicate AC switch param")


class AcSwitchPinTest(unittest.TestCase):
    """Drift guard: the (key -> param) mapping is pinned to the values verified against
    a real AS35 + the decompiled hOn app on 2026-06-22. `echoStatus` (with the 'h') is
    the genuine Haier parameter name, NOT a typo for `ecoStatus`. Any change here must
    be re-verified against a real device (see the reality test above)."""

    _PINNED = {
        "sleep": "silentSleepStatus",
        "mute": "muteStatus",
        "eco": "echoStatus",
        "rapid": "rapidMode",
        "health": "healthMode",
        "self_clean": "selfCleaningStatus",
        "self_clean_56": "selfCleaning56Status",
        "display": "screenDisplayStatus",
        "light": "lightStatus",
        "ten_degree_heating": "10degreeHeatingStatus",
        "child_lock": "lockStatus",
        "human_sensing": "humanSensingStatus",
        "electric_heating": "electricHeatingStatus",
        "fresh_air": "freshAirStatus",
        "half_degree": "halfDegreeSettingStatus",
        "energy_saving": "energySavingStatus",
    }

    def test_ac_switches_pinned(self):
        actual = {desc.key: desc.param for desc in switch._AC_SWITCHES}
        self.assertEqual(actual, self._PINNED)


if __name__ == "__main__":
    unittest.main()
