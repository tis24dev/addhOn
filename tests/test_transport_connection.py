"""Offline test del connection wrapper nativo (HonConnection).

Verifica la logica novel: iniezione token per-richiesta (build_auth_headers) +
retry su 401/403 (loop0→refresh, loop1→re-auth, loop≥2→errore). Auth e sessione
mockate; aiohttp/yarl stubati (nessuna rete). L'happy path è anche live-validato.
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
        self.assertEqual(auth.authenticate_calls, 1)  # niente token → login
        # gli header iniettati contengono i token
        hdr = session.calls[0]
        self.assertEqual(hdr["cognito-token"], "COG")
        self.assertEqual(hdr["id-token"], "IDT")

    def test_retry_on_401_refreshes(self) -> None:
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"  # già autenticato
        session = FakeSession([401, 200])  # primo 401 → refresh → 200
        conn = _conn(auth, session)

        async def run():
            async with conn.post("https://x/api") as resp:
                return resp.status

        status = asyncio.run(run())
        self.assertEqual(status, 200)
        self.assertGreaterEqual(auth.refresh_calls, 1)
        self.assertEqual(len(session.calls), 2)  # un retry

    def test_persistent_401_raises_login_failure(self) -> None:
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"
        auth.token_is_expired = True
        session = FakeSession([401, 401, 401])
        conn = _conn(auth, session)

        # Al loop-1 la connessione fa re-auth via create(): qui lo rendiamo un
        # no-op che MANTIENE il fake auth (altrimenti create() istanzierebbe un
        # HonAuth reale che farebbe rete). Così il loop≥2 col fake → Login failure.
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
