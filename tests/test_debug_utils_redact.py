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
redact_id = debug_utils.redact_id
redact_topic = debug_utils.redact_topic


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

    def test_masks_mac_shaped_string_leaf(self) -> None:
        # CR#4: identity that arrives where key-name redaction can't reach -- a bare
        # list element or a value under a benign key -- is masked via the MAC pattern.
        self.assertEqual(redact_identity("AA:BB:CC:DD:EE:FF"), "***")
        self.assertEqual(
            redact_identity({"parameters": ["AA:BB:CC:DD:EE:FF", "ok"]}),
            {"parameters": ["***", "ok"]},
        )
        self.assertEqual(
            redact_identity({"benign": "AA:BB:CC:DD:EE:FF"}), {"benign": "***"}
        )

    def test_mac_with_dash_separators_masked(self) -> None:
        self.assertEqual(redact_identity("aa-bb-cc-dd-ee-ff"), "***")

    def test_non_mac_string_leaf_passes_through(self) -> None:
        # no over-redaction of legitimate non-MAC values
        self.assertEqual(redact_identity("iot_auto"), "iot_auto")
        self.assertEqual(
            redact_identity({"benign": "HDPW5620CNPK"}), {"benign": "HDPW5620CNPK"}
        )

    def test_embedded_mac_in_string_leaf_masked(self) -> None:
        # a MAC embedded mid-string (not an exact match) is still masked
        self.assertEqual(
            redact_identity("device AA:BB:CC:DD:EE:FF online"), "device *** online"
        )

    def test_serial_leaf_passes_through_documented_residual(self) -> None:
        # DOCUMENTED RESIDUAL: a serial/mobile-id has no safe pattern, so a BARE serial
        # scalar (e.g. a malformed parameters element, or a value under a benign key) is
        # NOT masked by redact_identity -- only the MAC class is. A serial under a real
        # `serialNumber` KEY is still masked (key-based). This pins the deliberate CR#2
        # residual: if a future change starts masking bare serials, update this test.
        self.assertEqual(redact_identity("SN0123456789ABC"), "SN0123456789ABC")
        self.assertEqual(redact_identity(["SN0123456789ABC"]), ["SN0123456789ABC"])
        self.assertEqual(redact_identity({"serialNumber": "SN0123456789ABC"}),
                         {"serialNumber": "***"})  # key-based still masks it

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


class RedactIdTest(unittest.TestCase):
    def test_bare_id_is_fully_masked(self) -> None:
        self.assertEqual(redact_id("AA:BB:CC:DD:EE:FF"), "***")
        self.assertEqual(redact_id("SERIAL123"), "***")

    def test_falsy_passthrough(self) -> None:
        # Falsy returned unchanged so an `or <fallback>` at the call site still works.
        self.assertIsNone(redact_id(None))
        self.assertEqual(redact_id(""), "")

    def test_raw_id_never_leaks(self) -> None:
        out = redact_id("AA:BB:CC:DD:EE:FF")
        self.assertNotIn("AA", out)
        self.assertNotIn(":", out)

    def test_unique_id_keeps_suffix_masks_prefix(self) -> None:
        # f"{appliance_id}_{suffix}" -> the MAC prefix is masked, the suffix kept.
        self.assertEqual(redact_id("AA:BB:CC_program", "AA:BB:CC"), "***_program")
        self.assertEqual(
            redact_id("SERIAL123_target_temp_zone3", "SERIAL123"),
            "***_target_temp_zone3",
        )

    def test_prefix_absent_falls_back_to_full_mask(self) -> None:
        # parent_id not a prefix (defensive) -> mask the whole thing, never leak.
        self.assertEqual(redact_id("HonNumberXYZ", "AA:BB:CC"), "***")

    def test_no_parent_id_full_mask(self) -> None:
        self.assertEqual(redact_id("AA:BB:CC_program"), "***")

    def test_non_string_value_coerced(self) -> None:
        self.assertEqual(redact_id(12345), "***")

    def test_exported_in_all(self) -> None:
        self.assertIn("redact_id", debug_utils.__all__)


class RedactTopicTest(unittest.TestCase):
    def test_masks_dash_mac_in_topic(self) -> None:
        self.assertEqual(
            redact_topic("haier/things/3c-71-bf-bd-32-2c/event/appliancestatus/update"),
            "haier/things/***/event/appliancestatus/update",
        )

    def test_masks_colon_mac_in_topic(self) -> None:
        self.assertEqual(
            redact_topic("haier/things/AA:BB:CC:DD:EE:FF/event/connected"),
            "haier/things/***/event/connected",
        )

    def test_keeps_event_path(self) -> None:
        out = redact_topic("x/3c-71-bf-bd-32-2c/event/disconnected")
        self.assertIn("event/disconnected", out)
        self.assertNotIn("3c-71-bf-bd-32-2c", out)

    def test_no_mac_unchanged(self) -> None:
        self.assertEqual(redact_topic("haier/things/foo/event"), "haier/things/foo/event")

    def test_raw_mac_never_leaks(self) -> None:
        mac = "3c-71-bf-bd-32-2c"
        self.assertNotIn(mac, redact_topic(f"haier/things/{mac}/event/x"))

    def test_falsy_passthrough(self) -> None:
        self.assertIsNone(redact_topic(None))
        self.assertEqual(redact_topic(""), "")

    def test_exported_in_all(self) -> None:
        self.assertIn("redact_topic", debug_utils.__all__)


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


redact_store = debug_utils.redact_store


class RedactStoreTest(unittest.TestCase):
    """CR#1: a coordinator store dumped to a debug log must mask its KEYS (MAC-derived
    appliance ids) while keeping the non-identity VALUES (program codes)."""

    def test_masks_mac_keyed_keys(self) -> None:
        out = redact_store({"AA:BB:CC:DD:EE:FF": "iot_auto"})
        self.assertEqual(out, {"***": "iot_auto"})

    def test_raw_mac_key_never_leaks(self) -> None:
        out = redact_store({"AA:BB:CC:DD:EE:FF": "iot_auto"})
        self.assertNotIn("AA:BB:CC:DD:EE:FF", str(out))
        self.assertNotIn("AA:BB", str(out))

    def test_values_pass_through(self) -> None:
        # program codes are the diagnostic signal and carry no identity
        self.assertEqual(redact_store({"mac": "super_cool"}), {"***": "super_cool"})

    def test_multiple_appliances_keep_all_values_no_collapse(self) -> None:
        # distinct keys all mask to '***'; without disambiguation they would collapse
        # to one entry and drop a value -> the ordinal preserves count + every value.
        out = redact_store({"AA:BB:CC:DD:EE:01": "a", "AA:BB:CC:DD:EE:02": "b"})
        self.assertEqual(set(out), {"***", "***#2"})
        self.assertEqual(sorted(out.values()), ["a", "b"])

    def test_three_way_collision_keeps_all(self) -> None:
        # exercises the ordinal loop beyond the 2-way case
        out = redact_store({"AA:11": "a", "BB:22": "b", "CC:33": "c"})
        self.assertEqual(set(out), {"***", "***#2", "***#3"})
        self.assertEqual(sorted(out.values()), ["a", "b", "c"])

    def test_int_id_fallback_key_is_masked(self) -> None:
        # _appliance_id can fall back to id(obj); even a non-string key must mask.
        self.assertEqual(redact_store({140234567890: "iot_auto"}), {"***": "iot_auto"})

    def test_empty_store(self) -> None:
        self.assertEqual(redact_store({}), {})

    def test_does_not_mutate_input(self) -> None:
        src = {"AA:BB:CC:DD:EE:FF": "iot_auto"}
        redact_store(src)
        self.assertEqual(src, {"AA:BB:CC:DD:EE:FF": "iot_auto"})

    def test_non_mapping_returned_unchanged(self) -> None:
        self.assertEqual(redact_store("not-a-dict"), "not-a-dict")
        self.assertIsNone(redact_store(None))

    def test_exported_in_all(self) -> None:
        self.assertIn("redact_store", debug_utils.__all__)


if __name__ == "__main__":
    unittest.main()
