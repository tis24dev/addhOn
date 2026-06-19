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


_install_homeassistant_error()
