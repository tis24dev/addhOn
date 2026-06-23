"""Tests for debug_utils.redact_email.

Covers the privacy fix that stops logging the full account email at INFO: the
shared redact_email helper turns 'a@b.com' into '***@b.com'.

Loads debug_utils.py DIRECTLY by file path with importlib: the module has no
intra-package imports and pulls in nothing from homeassistant, so this avoids
triggering custom_components.addhon.__init__ (which would need HA stubs).
Stdlib unittest, no Home Assistant install required.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_DEBUG_UTILS_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "addhon"
    / "debug_utils.py"
)


def _load_debug_utils():
    spec = importlib.util.spec_from_file_location(
        "addhon_debug_utils_standalone", _DEBUG_UTILS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


debug_utils = _load_debug_utils()
redact_email = debug_utils.redact_email


class RedactEmailTest(unittest.TestCase):
    def test_none_returns_none(self) -> None:
        self.assertIsNone(redact_email(None))

    def test_empty_string_returns_none(self) -> None:
        # Matches the existing _redact_email copies: `not ""` is True -> None.
        self.assertIsNone(redact_email(""))

    def test_normal_email_redacts_local_part(self) -> None:
        self.assertEqual(redact_email("person@example.com"), "***@example.com")

    def test_no_at_sign_returns_stars(self) -> None:
        self.assertEqual(redact_email("weird-no-at"), "***")

    def test_multiple_at_keeps_remainder_as_domain(self) -> None:
        self.assertEqual(redact_email("a@b@c"), "***@b@c")

    def test_local_part_never_leaks(self) -> None:
        self.assertNotIn("person", redact_email("person@example.com"))

    def test_exported_in_all(self) -> None:
        self.assertIn("redact_email", debug_utils.__all__)


redact_identity = debug_utils.redact_identity
redact_mac = debug_utils.redact_mac


class RedactIdentityTest(unittest.TestCase):
    def test_masks_known_identity_keys(self) -> None:
        out = redact_identity({"macAddress": "AA:BB", "serialNumber": "X", "code": "C"})
        self.assertEqual(out, {"macAddress": "***", "serialNumber": "***", "code": "***"})

    def test_case_insensitive_key_match(self) -> None:
        self.assertEqual(redact_identity({"MAC": "z"}), {"MAC": "***"})

    def test_non_identity_values_pass_through(self) -> None:
        self.assertEqual(
            redact_identity({"modelName": "HDPW", "temp": 4}),
            {"modelName": "HDPW", "temp": 4},
        )

    def test_recurses_into_nested_dicts_and_lists(self) -> None:
        src = {"outer": {"transactionId": "AA_123"}, "items": [{"mobileId": "m"}]}
        self.assertEqual(
            redact_identity(src),
            {"outer": {"transactionId": "***"}, "items": [{"mobileId": "***"}]},
        )

    def test_does_not_mutate_input(self) -> None:
        src = {"mac": "secret", "nested": {"token": "t"}}
        redact_identity(src)
        self.assertEqual(src, {"mac": "secret", "nested": {"token": "t"}})

    def test_scalar_input_returned_unchanged(self) -> None:
        self.assertEqual(redact_identity("plain"), "plain")
        self.assertEqual(redact_identity(7), 7)
        self.assertIsNone(redact_identity(None))

    def test_exported_in_all(self) -> None:
        self.assertIn("redact_identity", debug_utils.__all__)


class RedactMacTest(unittest.TestCase):
    def test_none_returns_none(self) -> None:
        self.assertIsNone(redact_mac(None))

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(redact_mac(""))

    def test_full_mac_is_masked(self) -> None:
        self.assertEqual(redact_mac("AA:BB:CC:DD:EE:FF"), "***")

    def test_raw_mac_never_leaks(self) -> None:
        out = redact_mac("AA:BB:CC:DD:EE:FF")
        self.assertNotIn("AA", out)
        self.assertNotIn(":", out)

    def test_exported_in_all(self) -> None:
        self.assertIn("redact_mac", debug_utils.__all__)


class IdentityKeysPinTest(unittest.TestCase):
    """Pin the EXACT _IDENTITY_KEYS set: iterating the set in a test would silently
    stop checking a removed key, so dropping one (e.g. the snake-case transaction_id
    not covered by the diagnostics drift-guard) must fail here."""

    _EXPECTED = frozenset(
        {
            "serial", "serialnumber", "serial_number",
            "mac", "macaddress", "mac_address",
            "code", "nickname", "nick_name", "email",
            "password", "token", "access_token", "refresh_token",
            "authorization", "secret",
            "transactionid", "transaction_id", "mobileid", "mobile_id",
        }
    )

    def test_identity_keys_exact(self) -> None:
        self.assertEqual(set(debug_utils._IDENTITY_KEYS), set(self._EXPECTED))


if __name__ == "__main__":
    unittest.main()
