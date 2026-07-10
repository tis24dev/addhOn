# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Contract test of the OAuth login pieces: build_authorize_url / extract_login_url /
absolutize / is_oauth_done / generate_nonce / build_login_payload.

Oracle = the Salesforce-fronted hOn OAuth contract authored in
docs/protocol/HAIER-HON-TRANSPORT.md (sec5 login handshake, sec12 SSRF pinning) plus
public RFCs (OpenID Connect nonce = RFC 4122 v4 UUID) -- NOT a transcription of
pyhOn. The authorize URL and the aura login body are built byte-for-byte because the
server is strict about them; the expected shapes here are re-derived from the
documented contract and the values.py provenance source, so the pin tracks Haier's
requirement, not pyhOn's code.

The constants now live in values.py, so oauth.py imports them and can no longer be
loaded in isolation; this test imports it through the package (HA stubbed), copying
the pattern in test_transport_headers.py.
"""
from __future__ import annotations

import json
import sys
import types
import unittest
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _install_stubs() -> None:
    def _mod(name: str) -> types.ModuleType:
        m = sys.modules.get(name)
        if m is None:
            m = types.ModuleType(name)
            sys.modules[name] = m
        return m

    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    core.ServiceCall = getattr(core, "ServiceCall", type("ServiceCall", (), {}))
    core.callback = getattr(core, "callback", lambda f: f)
    exc = _mod("homeassistant.exceptions")
    base = getattr(exc, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exc.HomeAssistantError = base
    exc.ConfigEntryNotReady = getattr(exc, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base,), {}))
    exc.ConfigEntryAuthFailed = getattr(exc, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base,), {}))
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))
    ha = _mod("homeassistant")
    ha.config_entries, ha.core, ha.exceptions = ce, core, exc
    ha.helpers = _mod("homeassistant.helpers")
    ha.helpers.update_coordinator = uc


_install_stubs()

from custom_components.addhon.client.transport import oauth as o  # noqa: E402
from custom_components.addhon.client.transport.tokens import (  # noqa: E402
    parse_token_fragment,
)
from custom_components.addhon.client.transport.values import (  # noqa: E402
    APP,
    AUTH_API,
    CLIENT_ID,
    OAUTH_RESPONSE_TYPE,
    OAUTH_SCOPE,
)

_AUTH_HOST = urlsplit(AUTH_API).netloc


def _expected_authorize_url(nonce: str) -> str:
    """Authorize URL from the HHT-sec5 contract (values from values.py provenance).

    The scope keeps its spaces and the redirect_uri is pre-quoted -- both are the
    server's strict requirement, re-derived here from the spec, not from pyhOn.
    """
    redirect_uri = quote(f"{APP}://mobilesdk/detect/oauth/done")
    params = {
        "response_type": OAUTH_RESPONSE_TYPE,
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "display": "touch",
        "scope": OAUTH_SCOPE,
        "nonce": nonce,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{AUTH_API}/services/oauth2/authorize/expid_Login?{query}"


def _expected_login_body(email, password, fw_uid, loaded, page_url):
    """Salesforce Lightning aura login body from the HHT-sec5 contract.

    The `message.actions` + `aura.context` shape and the
    `&`.join(f"{k}={quote(json.dumps(v))}") encoding (key order included) are the
    Aura framework's documented POST contract, re-authored here from that contract.
    """
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
    def test_authorize_url_matches_spec(self) -> None:
        for nonce in ("abcd1234-aa-bb-cc-dd", "00000000-0000-0000-0000-000000000000", ""):
            with self.subTest(nonce=nonce):
                self.assertEqual(o.build_authorize_url(nonce), _expected_authorize_url(nonce))

    def test_authorize_url_preserves_unencoded_scope(self) -> None:
        # sec5: the scope keeps its spaces (NOT urlencoded) and redirect_uri is
        # pre-quoted (only ':' -> %3A; '/' kept). Server-strict, re-derived from spec.
        url = o.build_authorize_url("N")
        self.assertIn("scope=api openid refresh_token web", url)
        self.assertIn("redirect_uri=hon%3A//mobilesdk/detect/oauth/done", url)

    def test_reexports_values_from_single_source(self) -> None:
        # oauth.py must not re-inline the endpoints: it re-exports the provenance-
        # tracked values.py constants (the single documented source, spec: HHT-sec2).
        self.assertEqual(o.AUTH_API, AUTH_API)
        self.assertEqual(o.APP, APP)
        self.assertEqual(o.CLIENT_ID, CLIENT_ID)

    def test_extract_login_url_from_page(self) -> None:
        # sec5: the authorize page bootstraps navigation via a url=/href= sink; we take
        # the FIRST single-quoted target, and rewrite the post-Jul-2024 relative
        # /NewhOnLogin path back onto the old /s/login endpoint. None if no sink.
        cases = [
            ("blah url = 'https://account2.hon-smarthome.com/s/login/abc' end",
             "https://account2.hon-smarthome.com/s/login/abc"),
            ("x href='/NewhOnLogin/foo?bar=1' y",
             f"{AUTH_API}/s/login/NewhOnLogin/foo?bar=1"),
            ("href = '/some/relative/path'", "/some/relative/path"),
            ("first url='AAA' second url='BBB'", "AAA"),
            ("nessun link qui", None),
            ("", None),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(o.extract_login_url(text), expected)

    def test_first_match_selection_matches_historical_parser(self) -> None:
        # Regression (refuter round 1, R2-2): the login-url regex must accept ONLY the
        # historical shape (optional single space around `=`, no tab/newline/multi-space
        # crossing). Otherwise a decoy url/href separated by tab/newline/2+ spaces would
        # be matched FIRST and login would navigate to it. Each case must pick REAL.
        for text in (
            "junk href\t=\t'DECOY' more url = 'REAL'",
            "x href\n=\n'DECOY'; var url = 'REAL';",
            "a href  =  'DECOY' b url = 'REAL'",
        ):
            with self.subTest(text=text):
                self.assertEqual(o.extract_login_url(text), "REAL")

    def test_newhonlogin_rewrite(self) -> None:
        out = o.extract_login_url("href='/NewhOnLogin/x'")
        self.assertEqual(out, "https://account2.hon-smarthome.com/s/login/NewhOnLogin/x")

    def test_is_oauth_done(self) -> None:
        self.assertTrue(o.is_oauth_done("...oauth/done#access_token=AAA&..."))
        self.assertFalse(o.is_oauth_done("normal login page"))

    def test_oauth_done_fragment_slices_from_marker(self) -> None:
        page = "junk before ...oauth/done#access_token=AAA&refresh_token=BBB&id_token=CCC'>"
        self.assertEqual(
            o.oauth_done_fragment(page),
            "oauth/done#access_token=AAA&refresh_token=BBB&id_token=CCC'>",
        )
        self.assertIsNone(o.oauth_done_fragment("normal login page"))

    def test_sso_fast_path_ignores_stray_earlier_token(self) -> None:
        # Regression (greptile P1): a whole authorize page with a stray earlier
        # `access_token=` (e.g. echoed state) BEFORE the real oauth/done redirect.
        # Anchoring on the marker must select the REAL token, not the stray one.
        page = (
            "<a href='https://x/#access_token=STRAY&refresh_token=OLD&id_token=OLD'>x</a>"
            "...hon://mobilesdk/detect/oauth/done#access_token=REAL&refresh_token=RT&id_token=IT'>"
        )
        # Naive whole-page parse would grab the stray token first.
        self.assertEqual(parse_token_fragment(page).access_token, "STRAY")
        # The fix parses from the oauth/done marker and gets the real token.
        fixed = parse_token_fragment(o.oauth_done_fragment(page))
        self.assertEqual(fixed.access_token, "REAL")
        self.assertEqual(fixed.id_token, "IT")
        self.assertTrue(fixed.complete)

    def test_absolutize_is_byte_identical_to_concat_where_concat_was_valid(self) -> None:
        # sec5: on-host relative/empty/query/fragment hrefs resolve exactly like the
        # historical `AUTH_API + href` concat.
        for href in ("/finaltok", "/ProgressiveLogin?x=1",
                     "/s/login/p?startURL=%2Fhome#f", "", "?only=q", "#frag"):
            with self.subTest(href=href):
                self.assertEqual(o.absolutize(href), AUTH_API + href)

    def test_absolutize_fixes_relative_and_absolute(self) -> None:
        # relative without leading slash (old concat -> '...comfinaltok', not absolute)
        self.assertEqual(o.absolutize("finaltok"), f"{AUTH_API}/finaltok")
        self.assertEqual(o.absolutize("apex/x"), f"{AUTH_API}/apex/x")
        # same-host absolute: returned verbatim (old concat double-hosted it)
        self.assertEqual(o.absolutize(f"{AUTH_API}/abs/tok"), f"{AUTH_API}/abs/tok")
        # custom-scheme redirect preserved verbatim (non-http -> not pinned)
        self.assertEqual(
            o.absolutize("hon://mobilesdk/detect/oauth/done#access_token=AAA"),
            "hon://mobilesdk/detect/oauth/done#access_token=AAA",
        )

    def test_absolutize_pins_off_host_to_auth_host(self) -> None:
        # sec12 SSRF invariant: the login flow never legitimately leaves the auth host,
        # so every off-host http(s)/ws/wss/tcp result is re-pinned (check is on the
        # RESOLVED host, so whitespace / control-char / protocol-relative / empty-
        # authority bypasses cannot slip the token fetch off-host).
        for href in ("//evil.com/x", " //evil.com/x", "\t//evil.com/x",
                     "/\t/evil.com/x", "\\\\evil.com/x", "https://evil.com/x",
                     "https://other.salesforce.com/apex/x", "//evil.com:8080/x",
                     "//user@evil.com/x", "///evil.com/x", "\n//evil.com",
                     "http:///evil.com/steal", "http:////evil.com/x", "https:///evil.com/x",
                     "http://@evil.com/x", "https://account2.hon-smarthome.com@evil.com/x",
                     "ws://evil.com/x", "wss://evil.com/x", "tcp://evil.com/x"):
            with self.subTest(href=href):
                self.assertEqual(urlsplit(o.absolutize(href)).netloc, _AUTH_HOST)
        # the canonical protocol-relative case demotes the host to a path segment
        self.assertEqual(o.absolutize("//evil.com/x"), f"{AUTH_API}/evil.com/x")

    def test_login_payload_matches_spec(self) -> None:
        cases = [
            ("user@x.it", "p@ss&w=rd", "FWUID1", {"a": 1}, "/s/login/x?startURL=%2Fhome%3Dz&System=IoT"),
            ("e", "p", "F", {"app": "siteforce:loginApp2", "x": [1, 2]}, "/p?foo=1"),
        ]
        for email, pw, fw, loaded, page in cases:
            with self.subTest(page=page):
                self.assertEqual(
                    o.build_login_payload(email, pw, fw, loaded, page),
                    _expected_login_body(email, pw, fw, loaded, page),
                )

    def test_nonce_is_rfc4122_v4_uuid(self) -> None:
        # sec5: the server needs *a* fresh nonce; the shape is a free client choice, so
        # we mint an RFC 4122 v4 UUID (not a hand-sliced hex string).
        n = o.generate_nonce()
        parsed = uuid.UUID(n)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(str(parsed), n)
        self.assertNotEqual(n, o.generate_nonce())  # random each call


if __name__ == "__main__":
    unittest.main()
