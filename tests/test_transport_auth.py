"""Offline test of the native auth flow (HonAuth) with a MOCKED session.

The happy path is already LIVE-validated (apk/validate_auth_live.py: real login ->
token -> 4 appliances == pyhOn). This is the CI guard for the flow LOGIC (step
order, headers, payload, parsing, branches): yarl stubbed, no network, HTTP
responses scripted in call order.
"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _mod(name: str) -> types.ModuleType:
    m = sys.modules.get(name)
    if m is None:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return m


def _install_stubs() -> None:
    # Minimal HA stubs to let the package __init__ import.
    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
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
    # Stub yarl.URL (auth.py does URL(login_url, encoded=True)).
    yarl = _mod("yarl")
    if not hasattr(yarl, "URL"):
        class URL:
            def __init__(self, s, encoded=False):
                self._s = s

            def __str__(self):
                return self._s
        yarl.URL = URL


_install_stubs()

from custom_components.addhon.client.transport.auth import HonAuth, NativeAuthError  # noqa: E402
from custom_components.addhon.client.transport.device import HonDevice  # noqa: E402

AUTH = "https://account2.hon-smarthome.com"


class FakeResp:
    def __init__(self, status=200, text="", json=None, headers=None) -> None:
        self.status = status
        self._text = text
        self._json = json
        self.headers = headers or {}

    async def text(self):
        return self._text

    async def json(self, content_type=None):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Returns the scripted responses IN call ORDER (the flow is linear)."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list = []
        self.cookie_jar = types.SimpleNamespace(clear_domain=lambda d: None)

    def _next(self, method, url):
        self.calls.append((method, str(url)))
        if not self._responses:
            raise AssertionError(f"unexpected call: {method} {url}")
        return self._responses.pop(0)

    def get(self, url, **kw):
        return self._next("GET", url)

    def post(self, url, **kw):
        return self._next("POST", url)


class StrictUrlFakeSession(FakeSession):
    """Like FakeSession but rejects a non-absolute URL, the way a real aiohttp
    ClientSession WITHOUT base_url raises InvalidUrlClientError. The plain FakeSession
    ignores the URL, so the relative-href crash (#3) is invisible to it; this double
    makes the bug -- and its fix -- observable offline.

    Scope: it models the http(s) absolute-URL requirement only (the flow fetches only
    http(s) after the fix). It is intentionally lenient on non-http schemes, which the
    flow never GETs, so it is a guard for #3 -- not a full aiohttp URL validator."""

    def _next(self, method, url):
        s = str(url)
        parts = urlsplit(s)
        if not parts.scheme or (parts.scheme in ("http", "https") and not parts.netloc):
            raise ValueError(f"not an absolute URL (no base_url on the session): {s!r}")
        return super()._next(method, url)


def _happy_responses():
    return [
        # _introduce: authorize page with the login url
        FakeResp(text="x url = '/s/login/p?startURL=%2Fhome' y"),
        # _handle_redirects: 2 redirects (Location)
        FakeResp(status=302, headers={"Location": f"{AUTH}/r1"}),
        FakeResp(status=302, headers={"Location": f"{AUTH}/r2?startURL=%2Fhome"}),
        # _open_login_page: fwuid + loaded
        FakeResp(text='..."fwuid":"FW123","loaded":{"APPLICATION@x":"y"}...'),
        # _login: events url
        FakeResp(json={"events": [{"attributes": {"values": {"url": f"{AUTH}/tokpage"}}}]}),
        # _get_token: page with href (no ProgressiveLogin)
        FakeResp(text="href = '/finaltok'"),
        # _get_token: final token page
        FakeResp(text="#access_token=AAA&refresh_token=r%2Fb&id_token=CCC&"),
        # _api_auth
        FakeResp(json={"cognitoUser": {"Token": "COG123"}}),
    ]


class NativeAuthFlowTest(unittest.TestCase):
    def _auth(self, responses):
        return HonAuth(FakeSession(responses), "user@x.it", "pw", HonDevice())

    def test_happy_path(self) -> None:
        auth = self._auth(_happy_responses())
        asyncio.run(auth.authenticate())
        self.assertEqual(auth.id_token, "CCC")
        self.assertEqual(auth.access_token, "AAA")
        self.assertEqual(auth.refresh_token, "r/b")  # only refresh url-decoded
        self.assertEqual(auth.cognito_token, "COG123")

    def test_no_auth_needed(self) -> None:
        # The authorize page is already the redirect with the tokens: the login steps
        # are skipped, but _api_auth STILL runs so cognito_token is minted (connection.py
        # needs it for every API call).
        auth = self._auth([
            FakeResp(text="...oauth/done#access_token=AAA&refresh_token=BBB&id_token=CCC&..."),
            FakeResp(json={"cognitoUser": {"Token": "COG123"}}),  # _api_auth
        ])
        asyncio.run(auth.authenticate())
        self.assertEqual(auth.id_token, "CCC")
        self.assertEqual(auth.access_token, "AAA")
        self.assertEqual(auth.cognito_token, "COG123")

    def test_login_page_without_fwuid_raises(self) -> None:
        auth = self._auth([
            FakeResp(text="x url = '/s/login/p?startURL=%2Fhome' y"),
            FakeResp(status=302, headers={"Location": f"{AUTH}/r1"}),
            FakeResp(status=302, headers={"Location": f"{AUTH}/r2"}),
            FakeResp(text="pagina senza fwuid"),
        ])
        with self.assertRaises(NativeAuthError):
            asyncio.run(auth.authenticate())

    def test_api_auth_without_cognito_raises(self) -> None:
        responses = _happy_responses()
        responses[-1] = FakeResp(json={"cognitoUser": {}})  # no Token
        auth = self._auth(responses)
        with self.assertRaises(NativeAuthError):
            asyncio.run(auth.authenticate())

    def test_progressive_login_without_href_raises(self) -> None:
        # If the ProgressiveLogin page contains no href, before we reached
        # href[0] with an IndexError that was not classified as an auth error.
        responses = _happy_responses()[:5]  # up to and including _login
        responses.append(FakeResp(text="href = '/ProgressiveLogin?x=1'"))  # _get_token -> progressive
        responses.append(FakeResp(text="pagina progressive senza alcun href"))  # findall -> []
        auth = self._auth(responses)
        with self.assertRaises(NativeAuthError):
            asyncio.run(auth.authenticate())

    def test_step_order(self) -> None:
        # The order of the calls reflects the pyhOn flow.
        session = FakeSession(_happy_responses())
        auth = HonAuth(session, "u", "p", HonDevice())
        asyncio.run(auth.authenticate())
        methods = [m for m, _ in session.calls]
        self.assertEqual(methods, ["GET", "GET", "GET", "GET", "POST", "GET", "GET", "POST"])


class NativeAuthUrlNormalizationTest(unittest.TestCase):
    """#3: every login href is absolutized through ONE seam, so the whole flow works
    against a base_url-less session whether the server returns hrefs relative or
    absolute. Run under StrictUrlFakeSession (rejects non-absolute URLs like real
    aiohttp), which the plain FakeSession cannot catch (it ignores the URL)."""

    def test_full_flow_is_base_url_safe(self) -> None:
        # The happy fixture's login_url is RELATIVE ('/s/login/...'); the token href
        # is RELATIVE ('/finaltok'). Under a base_url-less session this was a latent
        # crash in BOTH the redirect chain and _get_token. Must now complete clean.
        session = StrictUrlFakeSession(_happy_responses())
        auth = HonAuth(session, "u", "p", HonDevice())
        asyncio.run(auth.authenticate())  # must NOT raise (ValueError = non-absolute)
        self.assertEqual(auth.access_token, "AAA")
        self.assertIn(("GET", f"{AUTH}/finaltok"), session.calls)

    def test_absolute_token_href_not_double_hosted(self) -> None:
        # An ABSOLUTE token href must be fetched verbatim, not concat-corrupted into
        # AUTH+absolute (the old `token_url = AUTH_API + href[0]` bug).
        responses = _happy_responses()
        responses[5] = FakeResp(text=f"href = '{AUTH}/finaltok'")
        session = StrictUrlFakeSession(responses)
        auth = HonAuth(session, "u", "p", HonDevice())
        asyncio.run(auth.authenticate())
        self.assertIn(("GET", f"{AUTH}/finaltok"), session.calls)
        self.assertNotIn(("GET", f"{AUTH}{AUTH}/finaltok"), session.calls)

    def test_relative_no_slash_token_href_resolves(self) -> None:
        # No leading slash: old concat -> '...comfinaltok' (not absolute -> crash).
        responses = _happy_responses()
        responses[5] = FakeResp(text="href = 'finaltok'")
        session = StrictUrlFakeSession(responses)
        auth = HonAuth(session, "u", "p", HonDevice())
        asyncio.run(auth.authenticate())
        self.assertIn(("GET", f"{AUTH}/finaltok"), session.calls)

    def test_relative_progressive_href_does_not_crash(self) -> None:
        # The ProgressiveLogin first GET used to pass href[0] bare: a relative
        # '/ProgressiveLogin?x=1' crashed a base_url-less session BEFORE the flow
        # could classify the 'no href' error. Now it is fetched absolute.
        responses = _happy_responses()[:5]
        responses.append(FakeResp(text="href = '/ProgressiveLogin?x=1'"))
        responses.append(FakeResp(text="progressive page, no usable href, no OTP"))
        session = StrictUrlFakeSession(responses)
        auth = HonAuth(session, "u", "p", HonDevice())
        with self.assertRaises(NativeAuthError):  # 'progressive: no href', NOT a URL ValueError
            asyncio.run(auth.authenticate())
        self.assertIn(("GET", f"{AUTH}/ProgressiveLogin?x=1"), session.calls)


class RefreshTest(unittest.TestCase):
    """refresh() token rotation (#15) and malformed-response handling (#7)."""

    def _auth(self, responses, refresh_token="old"):
        auth = HonAuth(FakeSession(responses), "user@x.it", "pw", HonDevice())
        auth.refresh_token = refresh_token
        return auth

    def test_refresh_rotates_refresh_token(self) -> None:
        # A new refresh_token in the response must be persisted (#15).
        auth = self._auth([
            FakeResp(json={"id_token": "I", "access_token": "A", "refresh_token": "NEWRT"}),
            FakeResp(json={"cognitoUser": {"Token": "COG"}}),  # _api_auth
        ])
        ok = asyncio.run(auth.refresh())
        self.assertTrue(ok)
        self.assertEqual("NEWRT", auth.refresh_token)
        self.assertEqual("I", auth.id_token)
        self.assertEqual("A", auth.access_token)

    def test_refresh_keeps_token_when_not_rotated(self) -> None:
        auth = self._auth([
            FakeResp(json={"id_token": "I", "access_token": "A"}),
            FakeResp(json={"cognitoUser": {"Token": "COG"}}),
        ])
        ok = asyncio.run(auth.refresh())
        self.assertTrue(ok)
        self.assertEqual("old", auth.refresh_token)

    def test_refresh_malformed_2xx_returns_false(self) -> None:
        # 200 without id/access token -> False, no KeyError, _expires untouched,
        # and _api_auth is NOT reached (only the token POST happens).
        auth = self._auth([FakeResp(json={})])
        before = auth._expires
        ok = asyncio.run(auth.refresh())
        self.assertFalse(ok)
        self.assertEqual(before, auth._expires)
        self.assertEqual(1, len(auth._session.calls))  # no _api_auth call

    def test_refresh_4xx_returns_false(self) -> None:
        auth = self._auth([FakeResp(status=400)])
        self.assertFalse(asyncio.run(auth.refresh()))


if __name__ == "__main__":
    unittest.main()
