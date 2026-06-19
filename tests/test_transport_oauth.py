"""Differential test of piece 5: build_authorize_url / extract_login_url / is_oauth_done.

Oracle = transcription of pyhon auth.HonAuth._introduce (the PURE part: build URL +
page parsing; the rest is HTTP, validated live). Constants loaded from the real
const.py (pure) -> also pins the AUTH_API/CLIENT_ID/APP drift.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import quote, unquote

import types as _types

_ROOT = Path(__file__).resolve().parents[1]
_OUR = _ROOT / "custom_components" / "addhon" / "client" / "transport" / "oauth.py"
# pyhOn constants (now OURS): transcribed as an oracle-replica after deleting
# `_vendor/`; they are the values our oauth.py exposes (self-owned drift-guard).
_CONST = _types.SimpleNamespace(
    AUTH_API="https://account2.hon-smarthome.com",
    APP="hon",
    CLIENT_ID="3MVG9QDx8IX8nP5T2Ha8ofvlmjLZl5L_gvfbT9.HJvpHGKoAS_dcMN8LYpTSYeVFCraUnV.2Ag1Ki7m4znVO6",
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _pyhon_authorize_url(c, nonce):
    """Verbatim of _introduce (build URL)."""
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
    """Verbatim of _introduce (page parsing), modeling 'no match' -> None."""
    login_url = re.findall("(?:url|href) ?= ?'(.+?)'", text)
    if not login_url:
        return None
    if login_url[0].startswith("/NewhOnLogin"):
        login_url[0] = f"{c.AUTH_API}/s/login{login_url[0]}"
    return login_url[0]


def _pyhon_login_body(email, password, fw_uid, loaded, page_url):
    """Verbatim of pyhon auth._login's body."""
    start_url = page_url.rsplit("startURL=", maxsplit=1)[-1]
    start_url = unquote(start_url).split("%3D")[0]
    action = {
        "id": "79;a",
        "descriptor": "apex://LightningLoginCustomController/ACTION$login",
        "callingDescriptor": "markup://c:loginForm",
        "params": {"username": email, "password": password, "startUrl": start_url},
    }
    data = {
        "message": {"actions": [action]},
        "aura.context": {
            "mode": "PROD",
            "fwuid": fw_uid,
            "app": "siteforce:loginApp2",
            "loaded": loaded,
            "dn": [],
            "globals": {},
            "uad": False,
        },
        "aura.pageURI": page_url,
        "aura.token": None,
    }
    body = "&".join(f"{k}={quote(json.dumps(v))}" for k, v in data.items())
    params = {"r": 3, "other.LightningLoginCustom.login": 1}
    return body, params


class OAuthPiecesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.o = _load(_OUR, "addhon_transport_oauth")
        self.c = _CONST

    def test_authorize_url_matches_pyhon(self) -> None:
        for nonce in ("abcd1234-aa-bb-cc-dd", "00000000-0000-0000-0000-000000000000", ""):
            with self.subTest(nonce=nonce):
                self.assertEqual(
                    self.o.build_authorize_url(nonce), _pyhon_authorize_url(self.c, nonce)
                )

    def test_authorize_url_preserves_unencoded_scope(self) -> None:
        # pyhon quirk: the scope keeps the spaces (NOT urlencoded).
        url = self.o.build_authorize_url("N")
        self.assertIn("scope=api openid refresh_token web", url)
        # quote() leaves the '/' (safe='/'): only ':' -> %3A, slashes unchanged.
        self.assertIn("redirect_uri=hon%3A//mobilesdk/detect/oauth/done", url)

    def test_extract_login_url_matches_pyhon(self) -> None:
        fixtures = [
            "blah url = 'https://account2.hon-smarthome.com/s/login/abc' end",
            "x href='/NewhOnLogin/foo?bar=1' y",          # relative -> rewritten
            "href = '/some/relative/path'",                 # relative non-NewhOnLogin -> as-is
            "first url='AAA' second url='BBB'",            # first match
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
        # Drift-guard: the inline constants must equal pyhOn's.
        self.assertEqual(self.o.AUTH_API, self.c.AUTH_API)
        self.assertEqual(self.o.APP, self.c.APP)
        self.assertEqual(self.o.CLIENT_ID, self.c.CLIENT_ID)

    def test_login_payload_matches_pyhon(self) -> None:
        cases = [
            ("user@x.it", "p@ss&w=rd", "FWUID1", {"a": 1}, "/s/login/x?startURL=%2Fhome%3Dz&System=IoT"),
            ("e", "p", "F", {"app": "siteforce:loginApp2", "x": [1, 2]}, "/p?foo=1"),
        ]
        for email, pw, fw, loaded, page in cases:
            with self.subTest(page=page):
                self.assertEqual(
                    self.o.build_login_payload(email, pw, fw, loaded, page),
                    _pyhon_login_body(email, pw, fw, loaded, page),
                )

    def test_nonce_format(self) -> None:
        n = self.o.generate_nonce()
        self.assertRegex(n, r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        self.assertNotEqual(n, self.o.generate_nonce())  # random


if __name__ == "__main__":
    unittest.main()
