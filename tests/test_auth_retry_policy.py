# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which login steps may be retried, and -- far more important -- which may not.

Issue #76 cause 3: the validation path had NO retry at all, so a single blip on any
of the 9 sequential login round-trips became a permanent user-facing error.

The non-regression half of this file is the valuable half. Retrying a step that
submits credentials, consumes a single-use hand-off URL, mints a fresh MFA context or
spends a rotating refresh token costs a second OTP email, an invalid session or a
permanently burnt token. Those steps must be delivered EXACTLY ONCE.
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

from _golden import install_stubs  # noqa: E402

install_stubs()

from test_transport_auth import (  # noqa: E402
    AUTH,
    FakeResp,
    FakeSession,
    _happy_responses,
)

from custom_components.addhon import error_codes as ec  # noqa: E402
from custom_components.addhon.client import budget as budget_mod  # noqa: E402
from custom_components.addhon.client.session import NativeHon  # noqa: E402
from custom_components.addhon.client.transport import auth as auth_mod  # noqa: E402
from custom_components.addhon.client.transport import (  # noqa: E402
    connection as conn_mod,
)
from custom_components.addhon.client.transport import retry as retry_mod  # noqa: E402
from custom_components.addhon.client.transport.api import HonApi  # noqa: E402
from custom_components.addhon.client.transport.auth import (  # noqa: E402
    HonAuth,
    NativeAuthError,
)
from custom_components.addhon.client.transport.connection import (  # noqa: E402
    HonConnection,
)
from custom_components.addhon.client.transport.device import HonDevice  # noqa: E402

# Index of each step in the scripted happy-path response list.
_STEP_INDEX = {
    "introduce": 0,
    "redirect_1": 1,
    "redirect_2": 2,
    "login_page": 3,
    "login_submit": 4,
    "post_login": 5,
    "token_page": 6,
    "api_auth": 7,
}


class _FlakySession(FakeSession):
    """Fails the response at `fail_at` once (with `error`), then serves it normally."""

    def __init__(self, responses, fail_at: int, error: BaseException) -> None:
        super().__init__(responses)
        self._fail_at = fail_at
        self._error = error
        self._served = 0

    def _next(self, method, url):
        if self._served == self._fail_at and self._fail_at >= 0:
            self._served += 1
            self.calls.append((method, str(url)))
            raise self._error
        self._served += 1
        return super()._next(method, url)


class _NoSleep:
    """Records the retry delays instead of waiting for them."""

    def __init__(self, test) -> None:
        self.delays: list[float] = []
        original = retry_mod.asyncio.sleep

        async def fake_sleep(seconds):
            self.delays.append(seconds)

        retry_mod.asyncio.sleep = fake_sleep
        test.addCleanup(setattr, retry_mod.asyncio, "sleep", original)


def _auth(session):
    return HonAuth(session, "user@x.it", "pw", HonDevice())


class RetryableStepsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeper = _NoSleep(self)

    def _login_recovers_after_one_blip(self, step: str, error: BaseException) -> None:
        session = _FlakySession(_happy_responses(), _STEP_INDEX[step], error)
        auth = _auth(session)
        asyncio.run(auth.authenticate())
        self.assertEqual("COG123", auth.cognito_token, f"{step} did not recover")
        # Delivered twice (the failed attempt + the successful retry), fixed 2s delay.
        self.assertEqual([retry_mod.RETRY_DELAY], self.sleeper.delays)

    def test_introduce_retries(self) -> None:
        self._login_recovers_after_one_blip("introduce", asyncio.TimeoutError())

    def test_first_redirect_retries(self) -> None:
        self._login_recovers_after_one_blip("redirect_1", asyncio.TimeoutError())

    def test_second_redirect_retries(self) -> None:
        self._login_recovers_after_one_blip("redirect_2", asyncio.TimeoutError())

    def test_login_page_retries(self) -> None:
        self._login_recovers_after_one_blip("login_page", asyncio.TimeoutError())

    def test_api_auth_retries(self) -> None:
        self._login_recovers_after_one_blip("api_auth", ConnectionResetError(104, "reset"))


    def test_sso_fast_path_is_not_swallowed_by_the_retry_wrapper(self) -> None:
        # _introduce signals the already-authorized SSO fast path with a control-flow
        # exception. Wrapping it in retry_transport must let that through untouched,
        # or a still-valid session would be re-driven through a full login.
        session = FakeSession(
            [
                FakeResp(
                    text="...oauth/done#access_token=AAA&refresh_token=r&id_token=CCC&"
                ),
                FakeResp(json={"cognitoUser": {"Token": "COG123"}}),
            ]
        )
        auth = _auth(session)
        asyncio.run(auth.authenticate())
        self.assertEqual("COG123", auth.cognito_token)
        self.assertEqual(2, len(session.calls))
        self.assertEqual([], self.sleeper.delays)


class NonRetryableStepsTest(unittest.TestCase):
    """The steps a duplicate delivery would damage. EXACTLY ONE send each."""

    def setUp(self) -> None:
        self.sleeper = _NoSleep(self)

    def _step_is_delivered_once(self, step: str) -> None:
        index = _STEP_INDEX[step]
        session = _FlakySession(_happy_responses(), index, asyncio.TimeoutError())
        auth = _auth(session)
        with self.assertRaises(BaseException):
            asyncio.run(auth.authenticate())
        # The failing step was attempted once and never re-sent.
        self.assertEqual(index + 1, len(session.calls), f"{step} was re-sent")
        self.assertEqual([], self.sleeper.delays)

    def test_login_submit_is_never_retried(self) -> None:
        # Submits the credentials and advances the Salesforce session; its payload
        # embeds the fwuid captured one step earlier.
        self._step_is_delivered_once("login_submit")

    def test_post_login_handoff_is_never_retried(self) -> None:
        # Single-use hand-off URL: a second GET lands on a login page -> "no href" ->
        # a transient blip would become a permanent ADDHON-130 credentials error.
        self._step_is_delivered_once("post_login")

    def test_token_page_is_never_retried(self) -> None:
        # Carries the #access_token fragment: the OAuth hand-off, consumable once.
        self._step_is_delivered_once("token_page")

    def test_refresh_is_never_retried(self) -> None:
        # The refresh token ROTATES and is single-use: a duplicate delivery burns it
        # while the unseen response carried its replacement -> forced full login, and
        # an OTP prompt on a 2FA account.
        session = _FlakySession([FakeResp(json={})], 0, asyncio.TimeoutError())
        auth = _auth(session)
        with self.assertRaises(asyncio.TimeoutError):
            asyncio.run(auth.refresh("rt"))
        self.assertEqual(1, len(session.calls))
        self.assertEqual([], self.sleeper.delays)


class RetryPredicateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeper = _NoSleep(self)

    def test_retryable_code_set_is_pinned(self) -> None:
        # A guard against drift: if `classify` moves one of these codes, the retry
        # policy would change silently.
        self.assertEqual(
            {
                ec.NETWORK_TIMEOUT,
                ec.DNS_FAILURE,
                ec.CONNECTION_REFUSED,
                ec.LOOP_TIMEOUT,
            },
            set(retry_mod.RETRYABLE_CODES),
        )

    def test_received_http_responses_are_not_retried(self) -> None:
        # A RECEIVED response is never a "nothing came back" blip: 5xx/429 already have
        # the appliance-layer backoff, 401/403 are a rejection.
        for err in (
            NativeAuthError("api_auth: status 401"),
            RuntimeError("hOn server error (status 503)"),
            RuntimeError("hOn rate limited (status 429)"),
        ):
            with self.subTest(err=err):
                self.assertFalse(retry_mod._is_retryable(err))

    def test_shared_budget_is_spent_across_steps(self) -> None:
        calls = {"n": 0}

        async def always_times_out():
            calls["n"] += 1
            raise asyncio.TimeoutError()

        async def scenario():
            budget = retry_mod.RetryBudget()
            with self.assertRaises(asyncio.TimeoutError):
                await retry_mod.retry_transport(budget, "introduce", always_times_out)
            return budget

        budget = asyncio.run(scenario())
        # 1 original attempt + RETRY_MAX_EXTRA retries, then it gives up.
        self.assertEqual(1 + retry_mod.RETRY_MAX_EXTRA, calls["n"])
        self.assertEqual(0, budget.extra)

    def test_deadline_gate_refuses_a_retry_that_would_not_fit(self) -> None:
        calls = {"n": 0}

        async def always_times_out():
            calls["n"] += 1
            raise asyncio.TimeoutError()

        async def scenario():
            import time

            budget = retry_mod.RetryBudget(deadline=time.monotonic() + 5)
            with self.assertRaises(asyncio.TimeoutError):
                await retry_mod.retry_transport(budget, "introduce", always_times_out)

        asyncio.run(scenario())
        # No room left for another 30s request + 2s delay -> no retry at all. This is
        # what stops the retry from being the reason the phase budget expires.
        self.assertEqual(1, calls["n"])


def _appliance_list_response() -> FakeResp:
    """What the wire answers AFTER the login: the single POST setup() is there for."""
    return FakeResp(
        json={
            "modules": {
                "applianceList": {
                    "payload": {
                        "appliances": [{"macAddress": "AA", "applianceTypeName": "WM"}]
                    }
                }
            }
        }
    )


class DeadlineComesFromTheEnclosingScopeTest(unittest.TestCase):
    """Whose deadline the gate measures against -- through the REAL production nesting.

    `authenticate()` used to rebuild the deadline as `monotonic() + AUTH_FULL`, a
    number the enclosing scope does not have to agree with. Measured against a deadline
    nobody enforces, the gate allowed every retry, and the retries then spent a shorter,
    real budget: the exact opposite of the invariant retry.py states.

    These tests used to wrap `authenticate()` in a BARE `budgeted(10)` / `budgeted(300)`
    of their own. Production never builds that: the login is lazy, so it always runs
    inside `budgeted(AUTH_FULL, suspends_caller=True)` opened by `_check_headers`,
    itself nested in the caller's scope -- and pinning a nesting production does not
    build is the exact mistake that let the first attempt at #76 ship green. So the
    login is driven from `NativeHon.setup()` through `HonApi`, `_intercept` and
    `_check_headers`, and what differs between the two cases is the size of the scope
    PRODUCTION opens, changed where production reads it.
    """

    def setUp(self) -> None:
        self.sleeper = _NoSleep(self)

    def _sign_in_scope_of(self, seconds: float) -> None:
        original = conn_mod.AUTH_FULL
        conn_mod.AUTH_FULL = seconds
        self.addCleanup(setattr, conn_mod, "AUTH_FULL", original)

    def _setup_driven_login(self, *, with_blip: bool = True):
        # A connection reset, not a TimeoutError: it is just as retryable
        # (CONNECTION_REFUSED is in RETRYABLE_CODES) and it travels out of the scopes
        # untouched, so the assertion is about the RETRY and nothing else.
        responses = [*_happy_responses(), _appliance_list_response()]
        session = _FlakySession(
            responses,
            _STEP_INDEX["introduce"] if with_blip else -1,
            ConnectionResetError(104, "reset"),
        )
        hon = NativeHon(email="user@x.it", password="pw", enable_mqtt=False, minimal=True)
        connection = HonConnection(
            "user@x.it", "pw", session=session, phase_tracker=hon._phase_tracker
        )
        connection._auth = HonAuth(
            session, "user@x.it", "pw", HonDevice(), phase_tracker=hon._phase_tracker
        )
        hon._connection = connection
        hon._api = HonApi(connection)
        return hon, connection

    def _deadlines_handed_to_the_gate(self) -> list[float]:
        """Seconds of room the login's RetryBudget was given, as it was given them."""
        seen: list[float] = []
        original = auth_mod.RetryBudget

        class _Recording(original):  # type: ignore[valid-type,misc]
            def __init__(self, extra=retry_mod.RETRY_MAX_EXTRA, deadline=None):
                seen.append(
                    None if deadline is None else deadline - time.monotonic()
                )
                super().__init__(extra, deadline)

        auth_mod.RetryBudget = _Recording
        self.addCleanup(setattr, auth_mod, "RetryBudget", original)
        return seen

    def test_the_gate_measures_the_sign_ins_own_scope(self) -> None:
        # The lazy sign-in SUSPENDS the request that triggered it, so the tightest scope
        # in force while it runs is its own AUTH_FULL -- not the caller's APPLIANCE_LIST
        # (40s), which would refuse retries the login is entitled to, and not a number
        # re-derived from a constant, which would allow retries nothing can pay for.
        seen = self._deadlines_handed_to_the_gate()
        hon, _connection = self._setup_driven_login(with_blip=False)
        asyncio.run(hon.setup())
        self.assertEqual(1, len(seen))
        room = seen[0]
        self.assertIsNotNone(room, "production must arm the gate with a real deadline")
        self.assertGreater(room, budget_mod.APPLIANCE_LIST)
        self.assertLessEqual(room, budget_mod.AUTH_FULL)
        self.assertGreater(room, budget_mod.AUTH_FULL - 1)

    def test_a_wide_sign_in_scope_lets_the_retry_run(self) -> None:
        self._sign_in_scope_of(300)
        hon, connection = self._setup_driven_login()
        asyncio.run(hon.setup())
        self.assertEqual("COG123", connection.auth.cognito_token)
        self.assertEqual([retry_mod.RETRY_DELAY], self.sleeper.delays)
        self.assertEqual(1, len(hon.appliances))

    def test_a_sign_in_scope_too_tight_for_another_attempt_refuses_the_retry(self) -> None:
        # 10s left cannot absorb a 30s request plus the 2s delay, so the blip must
        # surface now instead of being turned into a budget expiry a moment later.
        # Same login, same blip, same call path -- only the scope production opens
        # around the sign-in differs.
        self._sign_in_scope_of(10)
        hon, _connection = self._setup_driven_login()
        with self.assertRaises(ConnectionResetError):
            asyncio.run(hon.setup())
        self.assertEqual([], self.sleeper.delays)


class StepOrderUnderBlipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeper = _NoSleep(self)

    def test_transient_failure_does_not_alter_the_step_order(self) -> None:
        session = _FlakySession(
            _happy_responses(), _STEP_INDEX["login_page"], asyncio.TimeoutError()
        )
        auth = _auth(session)
        asyncio.run(auth.authenticate())
        methods = [m for m, _ in session.calls]
        # The retried login page shows up twice; everything else keeps its place.
        self.assertEqual(
            ["GET", "GET", "GET", "GET", "GET", "POST", "GET", "GET", "POST"], methods
        )
        self.assertTrue(str(session.calls[-1][1]).endswith("/auth/v1/login"))
        self.assertIn(AUTH, str(session.calls[0][1]))


if __name__ == "__main__":
    unittest.main()
