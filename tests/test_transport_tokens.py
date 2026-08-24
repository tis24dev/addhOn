# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contract test of the transport's OAuth token readers.

Oracle = the OAuth2 implicit-flow redirect contract (RFC 6749 sec4.2.2), documented
in docs/protocol/HAIER-HON-TRANSPORT.md sec6 -- NOT a transcription of pyhOn's
`name=(.*?)&` regex. tokens.py is stdlib-only, so it is loaded in isolation.

We assert the three tokens + the `complete` flag against the spec-stated result over
a matrix of redirects, including the deliberate, cloud-safe divergences from a naive
parse_qs: access/id kept RAW, only refresh percent-decoded once, an empty value still
counts as "present", and -- unlike pyhOn -- a final field with NO trailing `&` IS
captured (a real fragment need not end in one).

The second half covers the two CLAIM readers, `token_expiry` (RFC 7519 sec4.1.4) and
`token_person_account_id`, which share one decoder. `token_expiry` shipped without
tests of its own; it governs when every request in the transport refreshes its token,
so the extraction of that decoder needed a pin before it could be made -- these are it.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OUR_TOKENS = _ROOT / "custom_components" / "addhon" / "client" / "transport" / "tokens.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# (fragment, expected access, refresh, id, complete) -- each expectation is the
# spec-stated result (HHT-sec6), authored from RFC 6749 sec4.2.2, not from pyhOn.
_CASES = [
    # Complete realistic redirect; refresh is percent-decoded once (%2F -> /).
    ("blah url='/x' oauth/done#access_token=AAA&refresh_token=r%2Ftok&id_token=CCC&state=z&",
     "AAA", "r/tok", "CCC", True),
    # Different order, other params around -> still complete.
    ("#token_type=Bearer&id_token=ID1&access_token=AC1&refresh_token=RF1&expires=3600&",
     "AC1", "RF1", "ID1", True),
    # Missing id_token -> incomplete.
    ("#access_token=AAA&refresh_token=BBB&foo=bar&", "AAA", "BBB", "", False),
    # Missing refresh -> incomplete.
    ("#access_token=AAA&id_token=CCC&", "AAA", "", "CCC", False),
    # Final field WITHOUT a trailing '&': RFC 6749 sec4.2.2 -- the value runs to the
    # end of the fragment, so id_token IS captured and the redirect is complete.
    # (This is the concrete quirk pyhOn had and we deliberately do NOT share.)
    ("#access_token=AAA&refresh_token=BBB&id_token=CCC", "AAA", "BBB", "CCC", True),
    # Empty value but the key is present (access_token=&) -> counts as present.
    ("#access_token=&refresh_token=BBB&id_token=CCC&", "", "BBB", "CCC", True),
    # refresh value with encoded non-separators (%26 = literal '&' inside the value).
    ("#access_token=A&refresh_token=a%26b%3Dc&id_token=I&", "A", "a&b=c", "I", True),
    # Double occurrence: the FIRST value is used.
    ("#access_token=FIRST&x=1&access_token=SECOND&refresh_token=R&id_token=I&",
     "FIRST", "R", "I", True),
    # No token / empty / scattered '&'.
    ("completely unrelated text without tokens", "", "", "", False),
    ("", "", "", "", False),
    ("&&&access_token=ZZ&&&refresh_token=YY&&&id_token=XX&&&", "ZZ", "YY", "XX", True),
    # WHOLE-PAGE parse, single-quoted href: oauth._LOGIN_URL_RE matches url='...'/
    # href='...', so the redirect (with the token fragment) is embedded in single
    # quotes and the LAST token is immediately followed by `'` + markup. The value must
    # stop at the quote -> `CCC`, never `CCC'</script>` (which would be sent as a
    # malformed id-token header). RFC 6749 values are percent-encoded, so `'`,`"`,`<`,`>`
    # never occur literally in a real token.
    ("<script>location='hon://mobilesdk/detect/oauth/done"
     "#access_token=AAA&refresh_token=BBB&id_token=CCC'</script>",
     "AAA", "BBB", "CCC", True),
    # Double-quoted href variant, trailing '>'.
    ('junk href="/x#access_token=AAA&refresh_token=BBB&id_token=CCC">click',
     "AAA", "BBB", "CCC", True),
    # A NON-final token also wrapped in a quote must not absorb it either.
    ("a='#access_token=AAA'&refresh_token=BBB&id_token=CCC&", "AAA", "BBB", "CCC", True),
]


class ParseTokenFragmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parse = _load(_OUR_TOKENS, "addhon_transport_tokens").parse_token_fragment

    def test_matches_spec_contract(self) -> None:
        for text, access, refresh, id_token, complete in _CASES:
            with self.subTest(text=text):
                got = self.parse(text)
                self.assertEqual(got.access_token, access)
                self.assertEqual(got.refresh_token, refresh)
                self.assertEqual(got.id_token, id_token)
                self.assertEqual(got.complete, complete)

    def test_pinned(self) -> None:
        t = self.parse(
            "oauth/done#access_token=AAA&refresh_token=r%2Ftok&id_token=CCC&state=z&"
        )
        self.assertEqual(t.access_token, "AAA")
        self.assertEqual(t.refresh_token, "r/tok")  # only refresh decoded
        self.assertEqual(t.id_token, "CCC")
        self.assertTrue(t.complete)

    def test_only_refresh_is_unquoted(self) -> None:
        # %2F stays raw in access/id, decoded only in the refresh (sec6: the cloud is
        # handed access/id verbatim).
        t = self.parse("#access_token=a%2Fb&refresh_token=c%2Fd&id_token=e%2Ff&")
        self.assertEqual(t.access_token, "a%2Fb")
        self.assertEqual(t.refresh_token, "c/d")
        self.assertEqual(t.id_token, "e%2Ff")

    def test_trailing_token_without_amp_is_captured(self) -> None:
        # RFC 6749 sec4.2.2 (HHT-sec6): a final field with no trailing '&' runs to the
        # end of the fragment. The last id_token IS captured -> the redirect is
        # complete. This is the fix over pyhOn's `name=(.*?)&`, which silently dropped
        # a last field lacking the '&'.
        t = self.parse("#access_token=AAA&refresh_token=BBB&id_token=CCC")
        self.assertEqual(t.id_token, "CCC")
        self.assertTrue(t.complete)

    def test_trailing_field_does_not_absorb_markup(self) -> None:
        # Regression (refuter round 1, R2-4): when parsing a WHOLE PAGE (not just the
        # clean fragment) a token value must stop at whitespace, so trailing markup /
        # newline can't be folded into id_token and forwarded as a malformed id-token
        # header. OAuth token values never contain whitespace.
        t = self.parse("#access_token=AAA&refresh_token=BBB&id_token=CCC\n<html>junk")
        self.assertEqual(t.id_token, "CCC")

    def test_whole_page_stops_at_quote_and_bracket(self) -> None:
        # The exact Greptile-flagged case: the redirect URL is scraped from a
        # single/double-quoted href, so a whole-page parse sees the last token followed
        # by `'`/`"`/`>` with NO whitespace. The value must stop at those markup chars,
        # otherwise a malformed id-token (`CCC'>`) is forwarded to the API/MQTT auth.
        # Scope note: this pins value TERMINATION (where a field value ends). WHICH
        # fragment is picked when several appear on a page is a separate property --
        # _field uses re.search (first match); see the decoy test below.
        for suffix, why in [
            ("'", "single quote (href='...')"),
            ('"', "double quote (href=\"...\")"),
            (">", "angle bracket"),
            ("'></a>", "single quote + closing tag"),
            ('"/></html>', "double quote + self-closing tag"),
        ]:
            with self.subTest(suffix=why):
                t = self.parse(
                    f"#access_token=AAA&refresh_token=BBB&id_token=CCC{suffix}"
                )
                self.assertEqual(t.id_token, "CCC")
                self.assertEqual(t.access_token, "AAA")
                self.assertTrue(t.complete)

    def test_selection_is_first_match_known_limitation(self) -> None:
        # DOCUMENTED LIMITATION (pre-existing, NOT introduced by the char-class fix):
        # _field uses re.search, so when several well-formed fragments appear on one
        # page the FIRST is selected -- a value-SELECTION property, distinct from value
        # TERMINATION (which the char-class fix hardens). In the live flow the pages are
        # same-origin and host-pinned (oauth.absolutize) so this is not attacker-driven,
        # but a benign inline example placed before the real fragment WOULD be picked.
        # This test pins the current behaviour so any future change to selection is
        # deliberate; hardening selection (e.g. anchoring to the extracted done-URL at
        # the _get_token/_introduce call sites) is tracked as a separate follow-up.
        page = (
            "example: #access_token=DECOY_A&refresh_token=DECOY_R&id_token=DECOY_I\n"
            "real: #access_token=AAA&refresh_token=BBB&id_token=CCC"
        )
        t = self.parse(page)
        self.assertEqual(t.access_token, "DECOY_A")  # first match wins (documented)
        self.assertEqual(t.id_token, "DECOY_I")


def _jwt(claims, *, header="eyJhbGciOiJIUzI1NiJ9", signature="sig") -> str:
    """A JWT whose payload section carries `claims`, built the way the IdP does.

    Base64URL with the padding STRIPPED (RFC 7515 sec2), because that is the form the
    readers have to restore and the one a hand-written fixture would quietly get
    right. The header and signature are inert: nothing under test verifies either.
    """
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"{header}.{body}.{signature}"


class TokenExpiryTest(unittest.TestCase):
    """`token_expiry` reads the token's OWN stated lifetime (RFC 7519 sec4.1.4).

    Untested until now, and load-bearing: `HonAuth` stores its result as
    `_access_expiry` and `token_expires_soon` decides from it whether every request in
    the transport refreshes first. A None here does not fail loudly -- it falls back to
    a conservative window -- so a decoder that quietly stopped working would look like
    a slightly chattier integration and nothing else.
    """

    def setUp(self) -> None:
        self.module = _load(_OUR_TOKENS, "addhon_transport_tokens")
        self.expiry = self.module.token_expiry

    def test_reads_the_exp_claim_as_epoch_seconds(self) -> None:
        self.assertEqual(1_800_000_000.0, self.expiry(_jwt({"exp": 1_800_000_000})))
        self.assertIsInstance(self.expiry(_jwt({"exp": 1_800_000_000})), float)
        # A fractional exp is legal (sec4.1.4 says NumericDate, not integer).
        self.assertEqual(1.5, self.expiry(_jwt({"exp": 1.5})))

    def test_padding_is_restored_for_every_payload_length(self) -> None:
        # THE regression this file exists for. Base64URL strips `=` (RFC 7515 sec2), so
        # the reader has to add it back; the bug only appears at payload lengths whose
        # remainder mod 4 is not 0, so a single fixture proves nothing. Padding claims
        # of growing length walks through all three remainders several times over.
        for filler in range(24):
            with self.subTest(filler=filler):
                claims = {"exp": 1_800_000_000, "pad": "x" * filler}
                self.assertEqual(1_800_000_000.0, self.expiry(_jwt(claims)))

    def test_anything_that_is_not_a_readable_exp_is_none(self) -> None:
        for junk, why in (
            (_jwt({}), "no exp claim"),
            (_jwt({"exp": "1800000000"}), "exp as text"),
            (_jwt({"exp": None}), "exp null"),
            (_jwt([1, 2, 3]), "payload is a JSON array, not an object"),
            (_jwt("plain"), "payload is a JSON string"),
            ("not-a-jwt", "no dot-separated sections"),
            ("", "empty"),
            ("aaa.!!!not-base64!!!.ccc", "payload is not base64"),
            (f"aaa.{base64.urlsafe_b64encode(b'{not json').decode()}.ccc", "not JSON"),
            (None, "not a string at all"),
            (["a.b.c"], "a list"),
        ):
            with self.subTest(why=why):
                self.assertIsNone(self.expiry(junk))

    def test_a_boolean_exp_is_not_an_expiry(self) -> None:
        # isinstance(True, int) is True, so without the explicit bool refusal an `exp`
        # of `true` becomes 1.0 -- the epoch -- and the transport would treat the token
        # as permanently stale and re-authenticate on every single request.
        self.assertIsNone(self.expiry(_jwt({"exp": True})))
        self.assertIsNone(self.expiry(_jwt({"exp": False})))


class TokenPersonAccountIdTest(unittest.TestCase):
    """`token_person_account_id` reads the account id OUR id_token claims.

    Its one consumer compares it against the `sfPersonAccountId` the cloud stamps on
    every appliance, so that a session which silently resolved to a different account
    stops looking exactly like an account that owns nothing (ADDHON-210). The claim is
    an identifier: it is compared in the transport and never emitted.
    """

    def setUp(self) -> None:
        self.module = _load(_OUR_TOKENS, "addhon_transport_tokens")
        self.claim = self.module.token_person_account_id

    def test_reads_the_claim_from_the_live_shape(self) -> None:
        # The claim set of a real id_token of this integration, as captured on
        # 2026-08-24 (apk/analysis/addhon210-healthy-envelope-baseline.md). The nine
        # neighbours are not decoration: the reader must find PersonAccountId among
        # them, and among the top-level claims that also travel in the same payload.
        token = _jwt({
            "sub": "SYNTHETIC-SUB", "email": "user@example.com", "iss": "https://idp",
            "exp": 1_800_000_000,
            "custom_attributes": {
                "Country": "IT", "EulaUpdateRequired": "false",
                "ExternalSource": "hOn", "ExternalSubSource": "app",
                "OemAppId": "haier", "PersonAccountId": "ACCOUNT-OURS",
                "PersonContactId": "CONTACT-OURS", "PrivacyUpdated": "true",
                "UserLanguage": "it",
            },
        })
        self.assertEqual("ACCOUNT-OURS", self.claim(token))
        # The two readers share a decoder and must not interfere: the same token still
        # yields its expiry.
        self.assertEqual(1_800_000_000.0, self.module.token_expiry(token))

    def test_a_missing_or_unreadable_claim_is_none(self) -> None:
        # None is a DIAGNOSIS, not a failure: "we hold no identity to compare with",
        # which the census publishes as `no_claim` and which is a different finding
        # from "the comparison ran and disagreed".
        for junk, why in (
            (_jwt({"custom_attributes": {}}), "no PersonAccountId"),
            (_jwt({"custom_attributes": {"PersonAccountId": ""}}), "empty claim"),
            (_jwt({"custom_attributes": {"PersonAccountId": 42}}), "claim is a number"),
            (_jwt({"custom_attributes": {"PersonAccountId": None}}), "claim is null"),
            (_jwt({"custom_attributes": "ACCOUNT-OURS"}), "attributes is a string"),
            (_jwt({"custom_attributes": ["ACCOUNT-OURS"]}), "attributes is a list"),
            (_jwt({"PersonAccountId": "ACCOUNT-OURS"}), "claim at the top level"),
            (_jwt({}), "no attributes at all"),
            ("not-a-jwt", "not a token"),
            ("", "empty"),
            (None, "not a string at all"),
        ):
            with self.subTest(why=why):
                self.assertIsNone(self.claim(junk))

    def test_the_claim_is_not_searched_for_anywhere_in_the_payload(self) -> None:
        # A `flat = json.dumps(payload); "PersonAccountId" in flat` shortcut is the
        # obvious cheap implementation (the investigation probe used one), and it would
        # find the name inside an unrelated nested object -- reading someone else's id
        # as ours and turning a mismatch into a match. Only the ONE documented location
        # counts.
        decoy = _jwt({
            "custom_attributes": {"Country": "IT"},
            "app_metadata": {"custom_attributes": {"PersonAccountId": "ACCOUNT-DECOY"}},
        })
        self.assertIsNone(self.claim(decoy))

    def test_a_string_subclass_claim_is_refused(self) -> None:
        # `json.loads` cannot produce one, but this value is about to be compared for
        # equality against a string chosen by the cloud, and a subclass with a custom
        # __eq__ equals whatever it likes. The guard is `type(v) is str`, so a future
        # caller handing this a claims-shaped object cannot smuggle one through.
        class Sneaky(str):
            def __eq__(self, other):  # pragma: no cover - only if the guard fails
                return True

            def __hash__(self):
                return hash(str(self))

        # The guard lives in the READER, and `json.loads` will not hand it a subclass,
        # so the decoder is stood in for over this one call. Standing in for the whole
        # reader instead would test the double.
        claims = {"custom_attributes": {"PersonAccountId": Sneaky("ACCOUNT-OURS")}}
        original = self.module._jwt_claims
        try:
            self.module._jwt_claims = lambda _text: claims
            self.assertIsNone(self.claim("irrelevant"))
            # The premise: this object satisfies the isinstance check the guard
            # deliberately does not use.
            self.assertIsInstance(claims["custom_attributes"]["PersonAccountId"], str)
        finally:
            self.module._jwt_claims = original


if __name__ == "__main__":
    unittest.main()
