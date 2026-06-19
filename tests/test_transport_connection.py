"""Offline test of the native connection wrapper (HonConnection).

Verifies the novel logic: per-request token injection (build_auth_headers) +
retry on 401/403 (loop0->refresh, loop1->re-auth, loop>=2->error). Auth and
session mocked; aiohttp/yarl stubbed (no network). The happy path is also
live-validated.
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
    # HA stubs
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
    # yarl (auth.py)
    yarl = _mod("yarl")
    if not hasattr(yarl, "URL"):
        yarl.URL = type("URL", (), {"__init__": lambda self, s, encoded=False: None})
    # aiohttp (connection.py)
    aio = _mod("aiohttp")
    aio.ClientSession = getattr(aio, "ClientSession", type("ClientSession", (), {}))
    aio.ClientResponse = getattr(aio, "ClientResponse", type("ClientResponse", (), {}))
    aio.ContentTypeError = getattr(aio, "ContentTypeError", type("ContentTypeError", (Exception,), {}))


_install_stubs()

from custom_components.addhon.client.transport.connection import HonConnection  # noqa: E402
from custom_components.addhon.client.transport.auth import NativeAuthError  # noqa: E402


class FakeAuth:
    def __init__(self) -> None:
        self.cognito_token = ""
        self.id_token = ""
        self.refresh_token = ""
        self.token_expires_soon = False
        self.token_is_expired = False
        self.authenticate_calls = 0
        self.refresh_calls = 0

    async def authenticate(self) -> None:
        self.authenticate_calls += 1
        self.cognito_token = "COG"
        self.id_token = "IDT"
        self.refresh_token = "RT"

    async def refresh(self, rt: str = "") -> bool:
        self.refresh_calls += 1
        self.cognito_token = "COG2"
        self.id_token = "IDT2"
        self.refresh_token = "RT2"
        return True


class FakeResp:
    def __init__(self, status=200) -> None:
        self.status = status

    async def json(self, content_type=None):
        return {"ok": True}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, statuses) -> None:
        self._statuses = list(statuses)
        self.calls: list = []

    def _resp(self, url, **kw):
        self.calls.append(kw.get("headers", {}))
        return FakeResp(self._statuses.pop(0) if self._statuses else 200)

    def get(self, url, **kw):
        return self._resp(url, **kw)

    def post(self, url, **kw):
        return self._resp(url, **kw)


def _conn(auth, session):
    c = HonConnection("u@x", "p")
    c._auth = auth
    c._session = session
    return c


class ConnectionTest(unittest.TestCase):
    def test_first_request_authenticates_and_injects_tokens(self) -> None:
        auth = FakeAuth()
        session = FakeSession([200])
        conn = _conn(auth, session)

        async def run():
            async with conn.get("https://x/api") as resp:
                return resp.status

        status = asyncio.run(run())
        self.assertEqual(status, 200)
        self.assertEqual(auth.authenticate_calls, 1)  # no token -> login
        # the injected headers contain the tokens
        hdr = session.calls[0]
        self.assertEqual(hdr["cognito-token"], "COG")
        self.assertEqual(hdr["id-token"], "IDT")

    def test_retry_on_401_refreshes(self) -> None:
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"  # already authenticated
        session = FakeSession([401, 200])  # first 401 -> refresh -> 200
        conn = _conn(auth, session)

        async def run():
            async with conn.post("https://x/api") as resp:
                return resp.status

        status = asyncio.run(run())
        self.assertEqual(status, 200)
        self.assertGreaterEqual(auth.refresh_calls, 1)
        self.assertEqual(len(session.calls), 2)  # one retry

    def test_retry_on_403_refreshes(self) -> None:
        # Same branch as the 401 (the code treats them identically) but made explicit
        # to avoid regressions on the 403 (CodeRabbit nitpick).
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"
        session = FakeSession([403, 200])  # first 403 -> refresh -> 200
        conn = _conn(auth, session)

        async def run():
            async with conn.get("https://x/api") as resp:
                return resp.status

        status = asyncio.run(run())
        self.assertEqual(status, 200)
        self.assertGreaterEqual(auth.refresh_calls, 1)
        self.assertEqual(len(session.calls), 2)

    def test_third_attempt_success_after_reauth_yields(self) -> None:
        # ORACLE: if refresh (loop0) is not enough but the re-auth (loop1) is, the third
        # attempt returns 200 and MUST return the response, not raise. Bug:
        # `elif loop >= 2: raise` discarded any 200 on the third round.
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"  # already authenticated
        session = FakeSession([401, 401, 200])  # 401 -> refresh; 401 -> re-auth; 200 ok
        conn = _conn(auth, session)

        async def _noop_create():
            # successful re-auth: keeps the fake auth and clears the expiry (like a
            # fresh login would), so at loop2 the state no longer forces the failure.
            auth.token_is_expired = False
            return conn

        conn.create = _noop_create

        async def run():
            async with conn.get("https://x/api") as resp:
                return resp.status

        status = asyncio.run(run())
        self.assertEqual(status, 200)
        self.assertEqual(len(session.calls), 3)  # three attempts, the last one succeeded

    def test_persistent_401_at_loop2_still_raises_via_status(self) -> None:
        # Successful re-auth (token NOT expired) but the server keeps returning 401 on
        # the third round: it must raise via the disjunct on the status, not return the
        # 401. Pins that the fix does not rely on token_is_expired alone.
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"
        session = FakeSession([401, 401, 401])
        conn = _conn(auth, session)

        async def _noop_create():
            auth.token_is_expired = False  # fresh login: expiry cleared
            return conn

        conn.create = _noop_create

        async def run():
            async with conn.get("https://x/api"):
                pass

        with self.assertRaises(NativeAuthError):
            asyncio.run(run())

    def test_persistent_401_raises_login_failure(self) -> None:
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"
        auth.token_is_expired = True
        session = FakeSession([401, 401, 401])
        conn = _conn(auth, session)

        # At loop-1 the connection re-auths via create(): here we make it a
        # no-op that KEEPS the fake auth (otherwise create() would instantiate a
        # real HonAuth that would hit the network). So loop>=2 with the fake -> Login failure.
        async def _noop_create():
            return conn

        conn.create = _noop_create

        async def run():
            async with conn.get("https://x/api"):
                pass

        with self.assertRaises(NativeAuthError):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
