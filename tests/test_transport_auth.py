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
        # The authorize page is already the redirect with the tokens: no login, no cognito.
        auth = self._auth([
            FakeResp(text="...oauth/done#access_token=AAA&refresh_token=BBB&id_token=CCC&..."),
        ])
        asyncio.run(auth.authenticate())
        self.assertEqual(auth.id_token, "CCC")
        self.assertEqual(auth.cognito_token, "")  # _api_auth skipped (like pyhOn)

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


if __name__ == "__main__":
    unittest.main()
