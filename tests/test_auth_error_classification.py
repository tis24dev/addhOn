# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

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
        # Class name = auth, but every 5xx status is retryable -> NOT reauth.
        for status in (500, 501, 505, 507, 511, 599):
            err = NativeAuthError(f"boom status {status}")
            with self.subTest(status=status):
                self.assertTrue(hc._is_auth_error(err))      # via class name
                self.assertFalse(hc._requires_reauth(err))   # but retryable wins

    def test_message_based_classification_still_works(self) -> None:
        self.assertTrue(hc._is_auth_error(RuntimeError("HTTP 401 unauthorized")))
        self.assertTrue(hc._is_auth_error(RuntimeError("token expired")))


class CoordinatorErrorClassificationTest(unittest.TestCase):
    """#11: the setup/update error branches wrap _requires_reauth into the right HA
    exception. Previously only _requires_reauth was tested in isolation, so a swapped
    branch (auth -> UpdateFailed instead of ConfigEntryAuthFailed) passed the suite.
    These exercise the extracted classifiers directly."""

    def _imports(self):
        from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
        from homeassistant.helpers.update_coordinator import UpdateFailed
        from custom_components.addhon import _raise_setup_error, _raise_update_error

        return (
            ConfigEntryAuthFailed, ConfigEntryNotReady, UpdateFailed,
            _raise_setup_error, _raise_update_error,
        )

    def test_setup_auth_error_is_config_entry_auth_failed(self) -> None:
        AuthFailed, _NotReady, _UF, setup, _upd = self._imports()
        with self.assertRaises(AuthFailed):
            setup(NativeAuthError("login failed (status 200)"))

    def test_setup_generic_error_is_config_entry_not_ready(self) -> None:
        _AF, NotReady, _UF, setup, _upd = self._imports()
        with self.assertRaises(NotReady):
            setup(RuntimeError("network down"))

    def test_setup_retryable_5xx_is_not_ready_not_auth(self) -> None:
        # Auth-named class but a 5xx message -> retryable wins -> NotReady (retry),
        # NOT a reauth prompt.
        _AF, NotReady, _UF, setup, _upd = self._imports()
        for status in (500, 501, 505, 507, 511, 599):
            with self.subTest(status=status), self.assertRaises(NotReady):
                setup(NativeAuthError(f"boom status {status}"))

    def test_update_auth_error_is_config_entry_auth_failed(self) -> None:
        AuthFailed, _NotReady, _UF, _setup, upd = self._imports()
        with self.assertRaises(AuthFailed):
            upd(HonAuthenticationError("Can't login"))

    def test_update_generic_error_is_update_failed(self) -> None:
        _AF, _NotReady, UpdateFailed, _setup, upd = self._imports()
        with self.assertRaises(UpdateFailed):
            upd(RuntimeError("transient 503-less generic error"))

    def test_update_retryable_5xx_is_update_failed_not_auth(self) -> None:
        _AF, _NotReady, UpdateFailed, _setup, upd = self._imports()
        for status in (500, 501, 505, 507, 511, 599):
            with self.subTest(status=status), self.assertRaises(UpdateFailed):
                upd(NativeAuthError(f"boom status {status}"))

    def test_setup_mfa_challenge_is_config_entry_auth_failed(self) -> None:
        # A 2FA challenge during a BACKGROUND setup cannot prompt -> must route to the
        # reauth flow (ConfigEntryAuthFailed), NOT a ConfigEntryNotReady retry loop that
        # could never satisfy the OTP (and would re-send emails). Guards the
        # requires_reauth=True flag on MFA_REQUIRED staying coupled to this routing.
        from custom_components.addhon.client.transport.auth import MFAChallengeRequired
        from custom_components.addhon.client.transport.oauth import MfaContext

        AuthFailed, _NotReady, _UF, setup, _upd = self._imports()
        ctx = MfaContext("email", True, "h", "r", "v", {}, {}, "a", {}, "m", "SmartHome", None)
        with self.assertRaises(AuthFailed):
            setup(MFAChallengeRequired(ctx))

    def test_update_mfa_code_invalid_is_config_entry_auth_failed(self) -> None:
        from custom_components.addhon.client.transport.auth import MFACodeInvalid

        AuthFailed, _NotReady, _UF, _setup, upd = self._imports()
        with self.assertRaises(AuthFailed):
            upd(MFACodeInvalid("mfa: invalid verification code"))

    def test_chaining_preserves_original_error(self) -> None:
        _AF, _NotReady, UpdateFailed, _setup, upd = self._imports()
        original = RuntimeError("root cause")
        try:
            upd(original)
        except UpdateFailed as wrapped:
            self.assertIs(wrapped.__cause__, original)
        else:
            self.fail("expected UpdateFailed")


if __name__ == "__main__":
    unittest.main()
