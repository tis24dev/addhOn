"""Differential test del 3° pezzo del transport: parse_token_fragment.

Oracolo = trascrizione VERBATIM di pyhon auth._parse_token_data (la mutazione
self._auth diventa un dict locale; il metodo è su HonAuth, che tira dentro
connection/handler/aiohttp, quindi non importabile a sé). Confrontiamo i tre
token + il flag `complete` su molte redirect, incluse le quirk (unquote solo del
refresh, token finale senza `&`, valori vuoti, urlencoding).
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
    """Oracolo: verbatim di pyhon auth._parse_token_data (mutazione -> dict)."""
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
    # Redirect realistica completa (refresh urlencoded: %2F -> /).
    "blah url='/x' oauth/done#access_token=AAA&refresh_token=r%2Ftok&id_token=CCC&state=z&",
    # Ordine diverso, altri parametri intorno.
    "#token_type=Bearer&id_token=ID1&access_token=AC1&refresh_token=RF1&expires=3600&",
    # Manca id_token -> incompleto.
    "#access_token=AAA&refresh_token=BBB&foo=bar&",
    # Manca refresh -> incompleto.
    "#access_token=AAA&id_token=CCC&",
    # Token finale SENZA '&' finale: id_token non catturato (quirk regex).
    "#access_token=AAA&refresh_token=BBB&id_token=CCC",
    # Valore vuoto ma pattern matchato (access_token=&): pyhОn lo conta presente.
    "#access_token=&refresh_token=BBB&id_token=CCC&",
    # refresh con caratteri urlencoded che NON sono separatori (%26 = & letterale nel valore).
    "#access_token=A&refresh_token=a%26b%3Dc&id_token=I&",
    # Doppia occorrenza: si usa la prima.
    "#access_token=FIRST&x=1&access_token=SECOND&refresh_token=R&id_token=I&",
    # Nessun token.
    "completely unrelated text without tokens",
    # Vuoto.
    "",
    # Solo '&' sparsi.
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
        self.assertEqual(t.refresh_token, "r/tok")  # solo refresh decodificato
        self.assertEqual(t.id_token, "CCC")
        self.assertTrue(t.complete)

    def test_only_refresh_is_unquoted(self) -> None:
        # %2F resta grezzo in access/id, decodificato solo nel refresh.
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
