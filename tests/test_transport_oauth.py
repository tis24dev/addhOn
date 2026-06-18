"""Differential test del pezzo 5: build_authorize_url / extract_login_url / is_oauth_done.

Oracolo = trascrizione di pyhon auth.HonAuth._introduce (la parte PURA: build URL +
parsing pagina; il resto è HTTP, validato live). Costanti caricate dal vero
const.py (puro) → pinna anche il drift di AUTH_API/CLIENT_ID/APP.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

_ROOT = Path(__file__).resolve().parents[1]
_OUR = _ROOT / "custom_components" / "addhon" / "client" / "transport" / "oauth.py"
_PYHON_CONST = _ROOT / "custom_components" / "addhon" / "_vendor" / "pyhon" / "const.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _pyhon_authorize_url(c, nonce):
    """Verbatim di _introduce (build URL)."""
    redirect_uri = quote(f"{c.APP}://mobilesdk/detect/oauth/done")
    params = {
        "response_type": "token+id_token",
        "client_id": c.CLIENT_ID,
        "redirect_uri": redirect_uri,
        "display": "touch",
        "scope": "api openid refresh_token web",
        "nonce": nonce,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{c.AUTH_API}/services/oauth2/authorize/expid_Login?{query}"


def _pyhon_extract_login(c, text):
    """Verbatim di _introduce (parsing pagina), modellando 'nessun match' -> None."""
    login_url = re.findall("(?:url|href) ?= ?'(.+?)'", text)
    if not login_url:
        return None
    if login_url[0].startswith("/NewhOnLogin"):
        login_url[0] = f"{c.AUTH_API}/s/login{login_url[0]}"
    return login_url[0]


class OAuthPiecesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.o = _load(_OUR, "addhon_transport_oauth")
        self.c = _load(_PYHON_CONST, "pyhon_const_for_oauth")

    def test_authorize_url_matches_pyhon(self) -> None:
        for nonce in ("abcd1234-aa-bb-cc-dd", "00000000-0000-0000-0000-000000000000", ""):
            with self.subTest(nonce=nonce):
                self.assertEqual(
                    self.o.build_authorize_url(nonce), _pyhon_authorize_url(self.c, nonce)
                )

    def test_authorize_url_preserves_unencoded_scope(self) -> None:
        # quirk pyhon: lo scope tiene gli spazi (NON urlencoded).
        url = self.o.build_authorize_url("N")
        self.assertIn("scope=api openid refresh_token web", url)
        # quote() lascia gli '/' (safe='/'): solo ':' -> %3A, slash invariati.
        self.assertIn("redirect_uri=hon%3A//mobilesdk/detect/oauth/done", url)

    def test_extract_login_url_matches_pyhon(self) -> None:
        fixtures = [
            "blah url = 'https://account2.hon-smarthome.com/s/login/abc' end",
            "x href='/NewhOnLogin/foo?bar=1' y",          # relativo -> riscritto
            "href = '/some/relative/path'",                 # relativo non-NewhOnLogin -> as-is
            "first url='AAA' second url='BBB'",            # primo match
            "nessun link qui",                              # None
            "",                                             # None
        ]
        for text in fixtures:
            with self.subTest(text=text):
                self.assertEqual(
                    self.o.extract_login_url(text), _pyhon_extract_login(self.c, text)
                )

    def test_newhonlogin_rewrite(self) -> None:
        out = self.o.extract_login_url("href='/NewhOnLogin/x'")
        self.assertEqual(out, "https://account2.hon-smarthome.com/s/login/NewhOnLogin/x")

    def test_is_oauth_done(self) -> None:
        self.assertTrue(self.o.is_oauth_done("...oauth/done#access_token=AAA&..."))
        self.assertFalse(self.o.is_oauth_done("normal login page"))

    def test_constants_match_vendored_const(self) -> None:
        # Drift-guard: le costanti inline devono eguagliare quelle di pyhОn.
        self.assertEqual(self.o.AUTH_API, self.c.AUTH_API)
        self.assertEqual(self.o.APP, self.c.APP)
        self.assertEqual(self.o.CLIENT_ID, self.c.CLIENT_ID)


if __name__ == "__main__":
    unittest.main()
