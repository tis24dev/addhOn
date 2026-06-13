"""Tests for debug_utils.redact_email.

Covers the privacy fix that stops logging the full account email at INFO: the
shared redact_email helper turns 'a@b.com' into '***@b.com'.

Loads debug_utils.py DIRECTLY by file path with importlib: the module has no
intra-package imports and pulls in nothing from homeassistant, so this avoids
triggering custom_components.haier_hon.__init__ (which would need HA stubs).
Stdlib unittest, no Home Assistant install required.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_DEBUG_UTILS_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "haier_hon"
    / "debug_utils.py"
)


def _load_debug_utils():
    spec = importlib.util.spec_from_file_location(
        "haier_hon_debug_utils_standalone", _DEBUG_UTILS_PATH
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


if __name__ == "__main__":
    unittest.main()
