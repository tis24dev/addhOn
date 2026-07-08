"""Contract test of the transport's OAuth token parser: parse_token_fragment.

Oracle = the OAuth2 implicit-flow redirect contract (RFC 6749 sec4.2.2), documented
in docs/protocol/HAIER-HON-TRANSPORT.md sec6 -- NOT a transcription of pyhOn's
`name=(.*?)&` regex. tokens.py is stdlib-only, so it is loaded in isolation.

We assert the three tokens + the `complete` flag against the spec-stated result over
a matrix of redirects, including the deliberate, cloud-safe divergences from a naive
parse_qs: access/id kept RAW, only refresh percent-decoded once, an empty value still
counts as "present", and -- unlike pyhOn -- a final field with NO trailing `&` IS
captured (a real fragment need not end in one).
"""
from __future__ import annotations

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
