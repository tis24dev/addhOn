"""Test for the LOW fix: auth error classification (wrong-password -> reauth).

`_is_auth_error` now also checks the NAME of the exception class, so errors from
the login flow (our NativeAuthError, pyhOn's HonAuthenticationError) which
contain "auth" in the name but often not in the message, are classified as auth
errors -> reauth (invalid_auth) instead of cannot_connect. The "retryable 5xx"
check still takes priority.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _mod(name: str) -> types.ModuleType:
    m = sys.modules.get(name)
    if m is None:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return m


def _install_ha_stubs() -> None:
    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exc = _mod("homeassistant.exceptions")
    base = getattr(exc, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exc.HomeAssistantError = base
    exc.ConfigEntryNotReady = getattr(exc, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base,), {}))
    exc.ConfigEntryAuthFailed = getattr(exc, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base,), {}))
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))
    ha = _mod("homeassistant")
    ha.config_entries, ha.core, ha.exceptions = ce, core, exc
    ha.helpers = _mod("homeassistant.helpers")
    ha.helpers.update_coordinator = uc


_install_ha_stubs()

from custom_components.addhon import hon_client as hc  # noqa: E402


# Fake exceptions mimicking the real NAMES (the message does NOT contain auth keywords).
class NativeAuthError(Exception):
    pass


class HonAuthenticationError(Exception):
    pass


class AuthErrorClassificationTest(unittest.TestCase):
    def test_wrong_password_native_classifies_as_auth(self) -> None:
        err = NativeAuthError("login: fallito (status 200)")  # no keyword in the msg
        self.assertTrue(hc._is_auth_error(err))
        self.assertTrue(hc._requires_reauth(err))

    def test_pyhon_cant_login_classifies_as_auth(self) -> None:
        err = HonAuthenticationError("Can't login")  # no keyword in the msg
        self.assertTrue(hc._is_auth_error(err))
        self.assertTrue(hc._requires_reauth(err))

    def test_generic_error_is_not_auth(self) -> None:
        err = RuntimeError("qualcosa è andato storto")
        self.assertFalse(hc._is_auth_error(err))
        self.assertFalse(hc._requires_reauth(err))

    def test_auth_class_but_5xx_does_not_reauth(self) -> None:
        # Class name = auth, but message 500 -> retryable -> NOT reauth.
        err = NativeAuthError("boom status 500")
        self.assertTrue(hc._is_auth_error(err))      # via class name
        self.assertFalse(hc._requires_reauth(err))   # but retryable wins

    def test_message_based_classification_still_works(self) -> None:
        self.assertTrue(hc._is_auth_error(RuntimeError("HTTP 401 unauthorized")))
        self.assertTrue(hc._is_auth_error(RuntimeError("token expired")))


if __name__ == "__main__":
    unittest.main()
