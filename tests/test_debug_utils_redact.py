# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

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
comparable_text = debug_utils.comparable_text


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


class IdentityKeysBehaviouralTest(unittest.TestCase):
    """Every name in _IDENTITY_KEYS must actually MASK A VALUE on the log path.

    This replaces a set-equality pin that compared _IDENTITY_KEYS to a hand-copied
    `_EXPECTED` frozenset. That pin held the CONSTANT, not the guarantee, and it was
    measured to be worth nothing against either realistic regression:

      * Narrow only the MATCHING (leave both constants byte-identical, stop nine names
        from being looked up) and the pin PASSES -- measured, whole suite green at
        rc=0 with authorization/nickname/refresh_token and six more emitting cleartext.
      * Delete a name from the constant AND from the transcription -- which is exactly
        what the pin's own failure message instructs a developer to do -- and the pin
        is neutralised by construction. Nine such edits were run; the pin appeared in
        none of the failure lists, and four of them (mac_address, mobile_id,
        serial_number, transaction_id) left the WHOLE suite green.

    So the assertions below drive redact_identity itself. Making a retired key green
    again now requires DELETING a line that says that key must be masked, and watching
    a canary credential appear in the output -- not syncing a bookkeeping literal.
    """

    _CANARY_VALUE = "CANARY-CREDENTIAL-VALUE"

    # NAMED on purpose, NOT `for key in _IDENTITY_KEYS`. A loop over the constant
    # proves every SURVIVING member still works but goes quiet the moment a member is
    # removed -- its iteration simply stops visiting the retired key. Measured: with a
    # derived loop in place, dropping mac_address from the constant still ran green.
    _MUST_MASK = (
        "serial", "serialnumber", "serial_number",
        "mac", "macaddress", "mac_address",
        "code", "nickname", "nick_name", "email",
        "password", "token", "access_token", "refresh_token",
        "authorization", "secret",
        "transactionid", "transaction_id", "mobileid", "mobile_id",
        # The appliance-list envelope, captured live 2026-08-24: every element carries
        # the owning Salesforce account and the DynamoDB keys, whose PK IS the cognito
        # identity (`user#<region>:<identity>`) in plain text under two letters.
        "sfpersonaccountid", "personaccountid", "personcontactid",
        "pk", "sk", "applianceid", "eepromid",
    )

    # Keys no exact set would have contained, masked by the substring rule. Named the
    # same way and for the same reason: a loop over _IDENTITY_KEY_PARTS would prove the
    # rule fires without proving it fires on the SPELLING the cloud actually sends.
    _MUST_MASK_BY_PART = (
        "cognitoTokenNew",   # the one that proved the exact set insufficient
        "cognitoToken",
        "idToken",
        "sfAccessToken",
        "userPassword",
        "clientSecret",
    )

    def test_named_identity_keys_mask_their_value_on_the_log_path(self) -> None:
        for key in self._MUST_MASK:
            # Case variants: the cloud sends camelCase (nickName, macAddress) and the
            # match is documented as case-insensitive, so the spelling must not matter.
            for spelling in (key, key.upper(), key.title()):
                with self.subTest(key=spelling):
                    flat = debug_utils.redact_identity({spelling: self._CANARY_VALUE})
                    self.assertEqual("***", flat[spelling])
                    # ...and at the depth the sinks actually log: api.py:92 passes the
                    # whole appliance-list body, mqtt.py:515 the whole broker payload.
                    nested = debug_utils.redact_identity(
                        {"payload": [{"attributes": {spelling: self._CANARY_VALUE}}]}
                    )
                    # The VALUE, not the absence of the canary: `assertNotIn` on
                    # the repr would also pass if the redactor deleted the field
                    # instead of masking it, and a dropped key is a different
                    # bug that this test would then certify as the fix.
                    self.assertEqual(
                        "***", nested["payload"][0]["attributes"][spelling]
                    )

    def test_vendor_spellings_are_masked_by_the_substring_rule(self) -> None:
        """`token` was in the set and `cognitoTokenNew` was not.

        That gap was a bearer credential travelling unmasked into a WARNING this
        project asks users to paste into public issues -- the aggregator returns a
        replacement cognito token inside `modules.applianceList.authInfo`, and the
        empty-list branch is exactly where that response gets logged.
        """
        for key in self._MUST_MASK_BY_PART:
            with self.subTest(key=key):
                self.assertTrue(debug_utils._is_identity_key(key))
                nested = debug_utils.redact_identity(
                    {"modules": {"applianceList": {"authInfo": {key: self._CANARY_VALUE}}}}
                )
                self.assertEqual(
                    "***",
                    nested["modules"]["applianceList"]["authInfo"][key],
                )

    def test_the_substring_rule_stays_narrow(self) -> None:
        # A wider list would start masking the structure the logs exist to show. These
        # are keys the envelope really carries and that must survive redaction.
        for key in ("applianceTypeName", "modelName", "connectivity", "attributes",
                    "success", "payload", "modules", "series", "brand"):
            with self.subTest(key=key):
                self.assertFalse(debug_utils._is_identity_key(key))

    def test_a_value_used_as_a_key_is_masked_by_shape(self) -> None:
        """A vendor keying a mapping BY an identity would leak it one level up.

        `structure_only` exists so no leaf value is copied, and printing every key
        verbatim would have reintroduced exactly that leak in the key position. The
        test is by shape and not by blacklist for the reason `cognitoTokenNew` already
        demonstrated: no set enumerates a vendor's naming.
        """
        for hostile in (
            "user#eu-west-1:8f3c-4a21-9b0e",       # the DynamoDB identity partition
            "0011q00001IbepGAAR",                   # a Salesforce account id
            "ABC123456789",                         # a serial: long digit run
            "ABCDEFGH",                             # a serial with no digits at all:
                                                    # only the lowercase rule stops it
            "mario.rossi@gmail.com",
            "3C:71:BF:AA:BB:CC",
            "a1b2c3d4-5e6f-7890-abcd-ef1234567890",
        ):
            with self.subTest(key=hostile):
                shape = debug_utils.structure_only({"payload": {hostile: {"x": 1}}})
                self.assertEqual({"payload": {"***": "***"}}, shape)

    def test_the_shape_rule_keeps_every_key_the_real_envelope_carries(self) -> None:
        # Measured against the appliance-list capture of 2026-08-24: a rule that
        # masked a real schema key would leave the log unreadable, which is the other
        # way to fail. Identity keys are excluded here -- they are masked ON PURPOSE
        # and covered by _MUST_MASK above.
        real = (
            "applianceModelId", "applianceStatus", "applianceTypeId",
            "applianceTypeName", "attributes", "brand", "connectivity", "coords",
            "defaultWarrantyYears", "eepromName", "enrollmentDate", "firstEnrollment",
            "firstEnrollmentTBC", "fwVersion", "id", "lastCheckUp", "lastUpdate",
            "modelName", "purchaseDate", "sections", "series", "seriesVersion",
            "topics", "executionTime", "modules", "success", "payload", "appliances",
            "authInfo", "tempSelZ2",
        )
        for key in real:
            with self.subTest(key=key):
                self.assertTrue(debug_utils._is_schema_key(key))
                self.assertEqual(
                    {key: "<int>"}, debug_utils.structure_only({key: 1})
                )

    def test_the_shape_never_copies_a_leaf_value(self) -> None:
        # The whole contract in one assertion.
        body = {"payload": {"appliances": []}, "note": "CANARY", "n": 42,
                "deep": {"deeper": ["CANARY", 1]}}
        self.assertNotIn("CANARY", repr(debug_utils.structure_only(body)))

    def test_every_identity_key_has_a_behavioural_assertion(self) -> None:
        # The replacement for the old pin. This is still a set equality, but the
        # right-hand side is no longer a copy kept only for comparison: _MUST_MASK is
        # the list that DRIVES the redactor above. So this asserts a real property --
        # "every member of the constant is behaviourally covered" -- and a key ADDED to
        # _IDENTITY_KEYS fails here until it is given a real assertion.
        self.assertEqual(set(self._MUST_MASK), set(debug_utils._IDENTITY_KEYS))

    def test_non_identity_key_value_survives(self) -> None:
        # Positive control: the assertions above are not passing vacuously because
        # redact_identity masks everything it is handed.
        self.assertEqual({"label": "ok"}, debug_utils.redact_identity({"label": "ok"}))


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


class ComparableTextTest(unittest.TestCase):
    """`comparable_text` decides whether a command's echo confirms it.

    One side is a parameter's intern_value (always a string), the other is the
    cloud's raw parValue, which arrives as a string, a number or a bool and which
    the cloud may reformat. Getting this wrong reports a healthy round trip as a
    missing key, and it also picks WHICH pending command a push belongs to.
    """

    def test_the_spellings_of_one_number_all_agree(self) -> None:
        for value in ("5", "5.0", "05", " 5 ", 5, 5.0):
            self.assertEqual("5", comparable_text(value), repr(value))

    def test_a_bool_reads_as_the_device_spelling(self) -> None:
        """Taken before everything else because the rest goes through str(): a bool
        becomes "True", which no numeric parse accepts, so it would compare as that
        literal against the device's own 1/0 spelling."""
        self.assertEqual("1", comparable_text(True))
        self.assertEqual("0", comparable_text(False))
        self.assertNotEqual(comparable_text(True), str(True))

    def test_a_fraction_keeps_its_value(self) -> None:
        self.assertEqual("5.5", comparable_text("5.5"))
        self.assertEqual("5.5", comparable_text(5.5))
        self.assertNotEqual(comparable_text("5.5"), comparable_text("5"))

    def test_non_numeric_text_is_trimmed_and_kept(self) -> None:
        self.assertEqual("high", comparable_text(" high "))
        self.assertEqual("E12", comparable_text("E12"))
        self.assertEqual("", comparable_text(""))

    def test_different_values_stay_different(self) -> None:
        """The comparison is forgiving about spelling, never about value."""
        distinct = ["5", "6", "5.5", "-5", "0", "high", ""]
        rendered = [comparable_text(value) for value in distinct]
        self.assertEqual(len(distinct), len(set(rendered)), rendered)

    def test_overflow_and_nan_keep_their_raw_text(self) -> None:
        """Not for want of a short form: str(float("1e400")) is "inf", so collapsing
        these would make "1e400" and a 400-digit number compare EQUAL. Overflow is the
        one place where being forgiving about spelling would lose a value."""
        for value in ("nan", "inf", "-inf", "infinity", "1e400", "1" + "0" * 400):
            self.assertEqual(value, comparable_text(value), value[:12])
        self.assertEqual(comparable_text("inf"), comparable_text(" inf "))
        self.assertNotEqual(comparable_text("1e400"), comparable_text("1" + "0" * 400))

    def test_two_integers_past_float_precision_stay_different(self) -> None:
        """Overflow is not the first place value is lost. A float holds every integer
        only up to 2**53, so canonicalizing above that renders the ROUNDED double and
        lands two distinct numbers on one string, which is the one thing this function
        must not do. Below the bound the numeric form still applies."""
        low = "12345678901234567890"
        high = "12345678901234567891"
        self.assertEqual(low, comparable_text(low))
        self.assertEqual(high, comparable_text(high))
        self.assertNotEqual(comparable_text(low), comparable_text(high))
        # The first pair that collides sits one above the bound, so this pins WHERE
        # the bound is and not merely that some large numbers are kept: 2**53 + 1 has
        # no float of its own and rounds onto 2**53.
        self.assertNotEqual(
            comparable_text("9007199254740992"), comparable_text("9007199254740993")
        )
        # And the numeric form still applies right below it.
        self.assertEqual("9007199254740991", comparable_text("9007199254740991.0"))

    def test_a_decimal_comma_reads_as_a_decimal_point(self) -> None:
        """The engine's own str_to_float accepts it, so the shadow already holds 5.5
        for a cloud that sent "5,5"; a diagnostic calling that a mismatch would
        contradict the value the integration stored."""
        self.assertEqual("5.5", comparable_text("5,5"))
        self.assertEqual(comparable_text("5,5"), comparable_text("5.5"))
        self.assertNotEqual(comparable_text("5,5"), comparable_text("55"))


if __name__ == "__main__":
    unittest.main()
