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

        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

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
_ensure_yarl()
