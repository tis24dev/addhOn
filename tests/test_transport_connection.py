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
    # connection.create() now passes an explicit timeout; the stub just records kwargs.
    aio.ClientTimeout = getattr(aio, "ClientTimeout", lambda **kw: kw)


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
        # Faithful to the real refresh(): resetting _expires clears the expiry
        # flags, so a guarded pre-refresh does not fire again right after.
        self.token_expires_soon = False
        self.token_is_expired = False
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

    def test_no_pre_refresh_when_token_fresh(self) -> None:
        # #1: a valid, non-expiring token with a refresh_token present must NOT
        # trigger a pre-request refresh (the old code refreshed on every request).
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"
        auth.token_expires_soon = False
        conn = _conn(auth, FakeSession([200]))
        conn._refresh_token = "RT"

        async def run():
            async with conn.get("https://x/api") as resp:
                return resp.status

        asyncio.run(run())
        self.assertEqual(auth.refresh_calls, 0)
        self.assertEqual(auth.authenticate_calls, 0)

    def test_pre_refresh_when_expires_soon(self) -> None:
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"
        auth.token_expires_soon = True
        conn = _conn(auth, FakeSession([200]))
        conn._refresh_token = "RT"

        async def run():
            async with conn.get("https://x/api"):
                pass

        asyncio.run(run())
        self.assertEqual(auth.refresh_calls, 1)
        # The pre-refresh must happen BEFORE the first request, so the very first
        # outgoing request already carries the refreshed token (isolates the
        # pre-refresh from the post-response refresh branch in _intercept).
        self.assertEqual(conn._session.calls[0]["cognito-token"], "COG2")

    def test_refresh_rotation_propagates_to_connection(self) -> None:
        # A rotated refresh_token from auth must be stored on the connection, so a
        # later restart persists the new token (anti IdP-rotation invariant).
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"
        auth.token_expires_soon = True
        conn = _conn(auth, FakeSession([200]))
        conn._refresh_token = "RT"

        async def run():
            async with conn.get("https://x/api"):
                pass

        asyncio.run(run())
        self.assertEqual(conn._refresh_token, "RT2")  # FakeAuth.refresh rotates to RT2

    def test_single_401_triggers_one_refresh(self) -> None:
        # #14: a single 401 must refresh exactly once (was 3: pre + loop0 + recursion).
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"
        auth.token_expires_soon = False
        conn = _conn(auth, FakeSession([401, 200]))
        conn._refresh_token = "RT"

        async def run():
            async with conn.post("https://x/api") as resp:
                return resp.status

        status = asyncio.run(run())
        self.assertEqual(status, 200)
        self.assertEqual(auth.refresh_calls, 1)
        self.assertEqual(len(conn._session.calls), 2)

    def test_restart_with_refresh_token_refreshes_not_logins(self) -> None:
        # State after restart: refresh_token persisted but in-RAM tokens empty and
        # not yet near expiry -> use the persisted token (refresh) instead of a
        # full re-login (authenticate).
        auth = FakeAuth()  # cognito/id empty
        auth.token_expires_soon = False
        conn = _conn(auth, FakeSession([200]))
        conn._refresh_token = "RT"

        async def run():
            async with conn.get("https://x/api"):
                pass

        asyncio.run(run())
        self.assertEqual(auth.refresh_calls, 1)
        self.assertEqual(auth.authenticate_calls, 0)

    def test_concurrent_requests_single_refresh(self) -> None:
        # The lock + double-check collapses a burst of concurrent requests (the
        # asyncio.gather in load_commands) into ONE refresh on the shared token.
        class SlowFakeAuth(FakeAuth):
            async def refresh(self, rt: str = "") -> bool:
                self.refresh_calls += 1
                await asyncio.sleep(0)  # yield so the other coroutines interleave
                self.cognito_token, self.id_token, self.refresh_token = "C2", "I2", "RT2"
                return True

        auth = SlowFakeAuth()  # tokens empty -> all 3 initially _need_refresh
        conn = _conn(auth, FakeSession([200, 200, 200]))
        conn._refresh_token = "RT"

        async def one():
            async with conn.get("https://x/api") as resp:
                return resp.status

        async def run():
            return await asyncio.gather(one(), one(), one())

        asyncio.run(run())
        self.assertEqual(auth.refresh_calls, 1)
        self.assertEqual(auth.authenticate_calls, 0)

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


class RetryRefreshSingleFlightTest(unittest.TestCase):
    """CR#3: the 401/403 RETRY refresh (loop=0 in _intercept) must be single-flighted
    under the same _refresh_lock as the pre-request path, must copy the rotated
    refresh_token back, and must release the lock BEFORE the recursive _intercept
    (asyncio.Lock is not reentrant -> would deadlock)."""

    def test_concurrent_401s_single_retry_refresh(self) -> None:
        # A burst of concurrent requests (the asyncio.gather in load_commands) that all
        # 401 must collapse to ONE refresh on the shared rotating token -- not N. The
        # refresh yields so the siblings interleave and block on the lock.
        class SlowFakeAuth(FakeAuth):
            async def refresh(self, rt: str = "") -> bool:
                self.refresh_calls += 1
                await asyncio.sleep(0)  # yield: siblings reach the lock before gen bumps
                self.cognito_token, self.id_token, self.refresh_token = "C2", "I2", "RT2"
                self.token_expires_soon = False
                return True

        auth = SlowFakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"  # authenticated, NOT expiring
        conn = _conn(auth, FakeSession([401, 401, 401, 200, 200, 200]))
        conn._refresh_token = "RT"

        async def one():
            async with conn.get("https://x/api") as resp:
                return resp.status

        async def run():
            return await asyncio.gather(one(), one(), one())

        results = asyncio.run(run())
        self.assertEqual(results, [200, 200, 200])
        self.assertEqual(auth.refresh_calls, 1)        # single-flight (was N)
        self.assertEqual(auth.authenticate_calls, 0)
        self.assertEqual(conn._refresh_token, "RT2")   # rotated token copied back

    def test_gen_snapshot_taken_before_send_skips_inflight_sibling_refresh(self) -> None:
        # Pins the snapshot PLACEMENT: refresh_gen is captured BEFORE the request is
        # sent, so a request whose token was minted at gen G and is rejected while a
        # sibling refreshed WHILE IT WAS IN FLIGHT (gen -> G+1) correctly SKIPS its own
        # refresh. If the snapshot were taken AFTER the send, the in-flight request would
        # read the advanced gen and refresh redundantly (the herd survives). Coordinated
        # with events for determinism (no reliance on sleep ordering).
        b_sent = asyncio.Event()       # set when request B has sent (snapshotted gen=0)
        a_refreshed = asyncio.Event()  # set when request A has finished refreshing

        class GateAuth(FakeAuth):
            async def refresh(self, rt: str = "") -> bool:
                self.refresh_calls += 1
                await b_sent.wait()  # hold the refresh until B has sent (and snapshotted)
                self.cognito_token, self.id_token, self.refresh_token = "C2", "I2", "RT2"
                self.token_expires_soon = False
                a_refreshed.set()
                return True

        class GatedResp:
            def __init__(self, status, on_enter=None, wait_for=None):
                self.status = status
                self._on_enter = on_enter
                self._wait_for = wait_for

            async def json(self, content_type=None):
                return {"ok": True}

            async def __aenter__(self):
                if self._on_enter is not None:
                    self._on_enter.set()
                if self._wait_for is not None:
                    await self._wait_for.wait()  # stay IN FLIGHT until the sibling refreshed
                return self

            async def __aexit__(self, *a):
                return False

        class GatedSession:
            def __init__(self):
                self.calls: list = []
                self._n = 0

            def _resp(self, url, **kw):
                self.calls.append(kw.get("headers", {}))
                self._n += 1
                if self._n == 1:
                    return GatedResp(401)                                  # A: 401 now
                if self._n == 2:
                    return GatedResp(401, on_enter=b_sent, wait_for=a_refreshed)  # B: 401, in-flight
                return GatedResp(200)                                      # retries: 200

            def get(self, url, **kw):
                return self._resp(url, **kw)

            def post(self, url, **kw):
                return self._resp(url, **kw)

        auth = GateAuth()
        auth.cognito_token, auth.id_token = "C", "I"  # authenticated, not expiring
        conn = _conn(auth, GatedSession())
        conn._refresh_token = "RT"

        async def one():
            async with conn.get("https://x/api") as resp:
                return resp.status

        async def run():
            return await asyncio.gather(one(), one())

        results = asyncio.run(asyncio.wait_for(run(), timeout=2.0))
        self.assertEqual(results, [200, 200])
        # B's token was minted at gen 0; A bumped to gen 1 while B was in flight, so B
        # must SKIP -> exactly one refresh. (Snapshot-after-send would make this 2.)
        self.assertEqual(auth.refresh_calls, 1)

    def test_rotation_after_401_retry_propagates_to_connection(self) -> None:
        # Sub-claim (b): the retry refresh rotates the refresh_token; it MUST be copied
        # back to the connection (the loop=0 path used to skip this, leaving it stale).
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"  # authenticated, not expiring
        conn = _conn(auth, FakeSession([401, 200]))
        conn._refresh_token = "RT"

        async def run():
            async with conn.post("https://x/api") as resp:
                return resp.status

        status = asyncio.run(run())
        self.assertEqual(status, 200)
        self.assertEqual(auth.refresh_calls, 1)
        self.assertEqual(conn._refresh_token, "RT2")  # FakeAuth.refresh rotates to RT2

    def test_single_401_retry_completes_no_deadlock(self) -> None:
        # The retry lock must be released BEFORE the recursive _intercept (loop=1),
        # which re-acquires it via _check_headers. With token_expires_soon left STICKY
        # by refresh, the loop=1 _check_headers genuinely re-takes the lock -> if the
        # retry held it across the recursion this would deadlock (caught by wait_for).
        class StickyExpiryAuth(FakeAuth):
            async def refresh(self, rt: str = "") -> bool:
                self.refresh_calls += 1
                self.cognito_token, self.id_token, self.refresh_token = "C2", "I2", "RT2"
                self.token_expires_soon = True  # sticky -> loop=1 _check_headers re-locks
                return True

        auth = StickyExpiryAuth()
        auth.cognito_token, auth.id_token = "C", "I"
        auth.token_expires_soon = False
        conn = _conn(auth, FakeSession([401, 200, 200]))
        conn._refresh_token = "RT"

        async def run():
            async with conn.get("https://x/api") as resp:
                return resp.status

        # wait_for turns a deadlock into a TimeoutError instead of hanging the suite.
        status = asyncio.run(asyncio.wait_for(run(), timeout=2.0))
        self.assertEqual(status, 200)
        self.assertGreaterEqual(auth.refresh_calls, 2)  # loop0 retry + loop1 pre-request

    def test_double_check_skips_when_gen_advanced(self) -> None:
        # Directly: if a sibling already refreshed since this request was sent (the
        # generation advanced), _refresh_after_rejection must NOT refresh again.
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"
        conn = _conn(auth, FakeSession([]))
        conn._refresh_token = "RT"
        conn._refresh_gen = 5

        asyncio.run(conn._refresh_after_rejection(3))  # snapshot (3) != current (5)
        self.assertEqual(auth.refresh_calls, 0)
        self.assertEqual(conn._refresh_token, "RT")    # untouched

    def test_double_check_refreshes_when_gen_matches(self) -> None:
        # If nobody refreshed since this request was sent, the retry refreshes once,
        # copies the rotated token back, and bumps the generation.
        auth = FakeAuth()
        auth.cognito_token, auth.id_token = "C", "I"
        conn = _conn(auth, FakeSession([]))
        conn._refresh_token = "RT"
        conn._refresh_gen = 5

        asyncio.run(conn._refresh_after_rejection(5))  # snapshot matches current
        self.assertEqual(auth.refresh_calls, 1)
        self.assertEqual(conn._refresh_token, "RT2")   # rotated + copied back
        self.assertEqual(conn._refresh_gen, 6)         # generation bumped


class ConnectionCreateCleanupTest(unittest.TestCase):
    """#31: a failed create() must not leak the aiohttp.ClientSession it created,
    and must never close a caller-supplied session (which the caller owns)."""

    def _patch(self, obj, name, value):
        real = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, real))

    def test_create_failure_closes_owned_session(self) -> None:
        import custom_components.addhon.client.transport.connection as conn_mod

        closed = {"n": 0}

        class FakeSess:
            async def close(self):
                closed["n"] += 1

        def boom(*a, **k):
            raise RuntimeError("auth ctor boom")

        self._patch(conn_mod.aiohttp, "ClientSession", lambda *a, **k: FakeSess())
        self._patch(conn_mod, "HonAuth", boom)

        conn = HonConnection("u@x", "p")  # session=None -> connection owns it
        with self.assertRaises(RuntimeError):
            asyncio.run(conn.create())
        self.assertEqual(closed["n"], 1)  # owned session closed, not leaked

    def test_owned_session_gets_explicit_timeout(self) -> None:
        # #30: the session WE create must carry an explicit ClientTimeout (so a dead
        # endpoint fails fast, not after aiohttp's 300s default).
        import custom_components.addhon.client.transport.connection as conn_mod

        captured = {}

        class FakeSess:
            async def close(self):
                pass

        def fake_client_session(*a, **k):
            captured.update(k)
            return FakeSess()

        self._patch(conn_mod.aiohttp, "ClientSession", fake_client_session)
        self._patch(conn_mod, "HonAuth", lambda *a, **k: object())

        conn = HonConnection("u@x", "p")  # owns the session
        asyncio.run(conn.create())
        self.assertIn("timeout", captured)
        self.assertIsNotNone(captured["timeout"])

    def test_create_failure_leaves_caller_session_open(self) -> None:
        import custom_components.addhon.client.transport.connection as conn_mod

        closed = {"n": 0}

        class FakeSess:
            async def close(self):
                closed["n"] += 1

        def boom(*a, **k):
            raise RuntimeError("auth ctor boom")

        self._patch(conn_mod, "HonAuth", boom)

        passed = FakeSess()
        conn = HonConnection("u@x", "p", session=passed)  # caller owns the session
        with self.assertRaises(RuntimeError):
            asyncio.run(conn.create())
        self.assertEqual(closed["n"], 0)  # caller-supplied session must stay open

    def test_create_baseexception_closes_owned_session(self) -> None:
        # #31: the guard is `except BaseException` on purpose; a CANCELLED create()
        # (CancelledError is a BaseException, NOT an Exception) must still release
        # the owned session. `except Exception` would leak it -> kill that mutant.
        import custom_components.addhon.client.transport.connection as conn_mod

        closed = {"n": 0}

        class FakeSess:
            async def close(self):
                closed["n"] += 1

        def cancel_boom(*a, **k):
            raise asyncio.CancelledError()

        self._patch(conn_mod.aiohttp, "ClientSession", lambda *a, **k: FakeSess())
        self._patch(conn_mod, "HonAuth", cancel_boom)

        conn = HonConnection("u@x", "p")  # owns the session
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(conn.create())
        self.assertEqual(closed["n"], 1)  # owned session closed on BaseException too


if __name__ == "__main__":
    unittest.main()
