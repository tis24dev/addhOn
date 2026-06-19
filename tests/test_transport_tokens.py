"""Differential test of the transport's 3rd piece: parse_token_fragment.

Oracle = VERBATIM transcription of pyhon auth._parse_token_data (the self._auth
mutation becomes a local dict; the method is on HonAuth, which pulls in
connection/handler/aiohttp, so it is not importable on its own). We compare the
three tokens + the `complete` flag over many redirects, including the quirks
(unquote of the refresh only, final token without `&`, empty values, urlencoding).
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote

_ROOT = Path(__file__).resolve().parents[1]
_OUR_TOKENS = _ROOT / "custom_components" / "addhon" / "client" / "transport" / "tokens.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _pyhon_parse(text):
    """Oracle: verbatim of pyhon auth._parse_token_data (mutation -> dict)."""
    auth = {"access_token": "", "refresh_token": "", "id_token": ""}
    access_token = re.findall("access_token=(.*?)&", text)
    if access_token:
        auth["access_token"] = access_token[0]
    refresh_token = re.findall("refresh_token=(.*?)&", text)
    if refresh_token:
        auth["refresh_token"] = unquote(refresh_token[0])
    id_token = re.findall("id_token=(.*?)&", text)
    if id_token:
        auth["id_token"] = id_token[0]
    complete = bool(access_token and refresh_token and id_token)
    return auth, complete


_FIXTURES = [
    # Complete realistic redirect (refresh urlencoded: %2F -> /).
    "blah url='/x' oauth/done#access_token=AAA&refresh_token=r%2Ftok&id_token=CCC&state=z&",
    # Different order, other parameters around.
    "#token_type=Bearer&id_token=ID1&access_token=AC1&refresh_token=RF1&expires=3600&",
    # Missing id_token -> incomplete.
    "#access_token=AAA&refresh_token=BBB&foo=bar&",
    # Missing refresh -> incomplete.
    "#access_token=AAA&id_token=CCC&",
    # Final token WITHOUT a trailing '&': id_token not captured (regex quirk).
    "#access_token=AAA&refresh_token=BBB&id_token=CCC",
    # Empty value but pattern matched (access_token=&): pyhOn counts it as present.
    "#access_token=&refresh_token=BBB&id_token=CCC&",
    # refresh with urlencoded characters that are NOT separators (%26 = literal & in the value).
    "#access_token=A&refresh_token=a%26b%3Dc&id_token=I&",
    # Double occurrence: the first one is used.
    "#access_token=FIRST&x=1&access_token=SECOND&refresh_token=R&id_token=I&",
    # No token.
    "completely unrelated text without tokens",
    # Empty.
    "",
    # Only scattered '&'.
    "&&&access_token=ZZ&&&refresh_token=YY&&&id_token=XX&&&",
]


class ParseTokenFragmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parse = _load(_OUR_TOKENS, "addhon_transport_tokens").parse_token_fragment

    def test_matches_pyhon(self) -> None:
        for text in _FIXTURES:
            with self.subTest(text=text):
                ours = self.parse(text)
                ref, complete = _pyhon_parse(text)
                self.assertEqual(ours.access_token, ref["access_token"])
                self.assertEqual(ours.refresh_token, ref["refresh_token"])
                self.assertEqual(ours.id_token, ref["id_token"])
                self.assertEqual(ours.complete, complete)

    def test_pinned(self) -> None:
        t = self.parse(
            "oauth/done#access_token=AAA&refresh_token=r%2Ftok&id_token=CCC&state=z&"
        )
        self.assertEqual(t.access_token, "AAA")
        self.assertEqual(t.refresh_token, "r/tok")  # only refresh decoded
        self.assertEqual(t.id_token, "CCC")
        self.assertTrue(t.complete)

    def test_only_refresh_is_unquoted(self) -> None:
        # %2F stays raw in access/id, decoded only in the refresh.
        t = self.parse("#access_token=a%2Fb&refresh_token=c%2Fd&id_token=e%2Ff&")
        self.assertEqual(t.access_token, "a%2Fb")
        self.assertEqual(t.refresh_token, "c/d")
        self.assertEqual(t.id_token, "e%2Ff")

    def test_trailing_token_without_amp_not_captured(self) -> None:
        t = self.parse("#access_token=AAA&refresh_token=BBB&id_token=CCC")
        self.assertEqual(t.id_token, "")
        self.assertFalse(t.complete)


if __name__ == "__main__":
    unittest.main()
