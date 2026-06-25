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
_ensure_yarl()
