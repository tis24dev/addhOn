"""Test del fix LOW: classificazione errori di auth (wrong-password -> reauth).

`_is_auth_error` ora controlla anche il NOME della classe d'eccezione, così gli
errori del flusso di login (NativeAuthError nostro, HonAuthenticationError di
pyhОn) — che contengono "auth" nel nome ma spesso non nel messaggio — vengono
classificati come errori di auth → reauth (invalid_auth) invece di cannot_connect.
Il check "retryable 5xx" resta prioritario.
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


# Finte eccezioni che imitano i NOMI reali (il messaggio NON contiene keyword auth).
class NativeAuthError(Exception):
    pass


class HonAuthenticationError(Exception):
    pass


class AuthErrorClassificationTest(unittest.TestCase):
    def test_wrong_password_native_classifies_as_auth(self) -> None:
        err = NativeAuthError("login: fallito (status 200)")  # nessuna keyword nel msg
        self.assertTrue(hc._is_auth_error(err))
        self.assertTrue(hc._requires_reauth(err))

    def test_pyhon_cant_login_classifies_as_auth(self) -> None:
        err = HonAuthenticationError("Can't login")  # nessuna keyword nel msg
        self.assertTrue(hc._is_auth_error(err))
        self.assertTrue(hc._requires_reauth(err))

    def test_generic_error_is_not_auth(self) -> None:
        err = RuntimeError("qualcosa è andato storto")
        self.assertFalse(hc._is_auth_error(err))
        self.assertFalse(hc._requires_reauth(err))

    def test_auth_class_but_5xx_does_not_reauth(self) -> None:
        # Nome classe = auth, ma messaggio 500 -> retryable -> NON reauth.
        err = NativeAuthError("boom status 500")
        self.assertTrue(hc._is_auth_error(err))      # via nome classe
        self.assertFalse(hc._requires_reauth(err))   # ma retryable vince

    def test_message_based_classification_still_works(self) -> None:
        self.assertTrue(hc._is_auth_error(RuntimeError("HTTP 401 unauthorized")))
        self.assertTrue(hc._is_auth_error(RuntimeError("token expired")))


if __name__ == "__main__":
    unittest.main()
