# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The CIAM sign-in (client/transport/ciam.py), against a recorded fake of its four steps.

The steps and their shapes come from the app 2.30.7 (apk2 decomp.txt @3315470-3315680,
apk2/analysis/repo-esterni-2026-11-27/E-ciam-login.md). No network: the aiohttp session
is a double that answers each path with a scripted status and body.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import install_stubs  # noqa: E402

install_stubs()

from custom_components.addhon.client.transport import ciam as ciam_module  # noqa: E402
from custom_components.addhon.client.transport.auth import (  # noqa: E402
    MFAChallengeRequired,
    MFACodeInvalid,
    MFAServiceError,
    NativeAuthError,
)
from custom_components.addhon.client.transport.ciam import (  # noqa: E402
    CiamAuth,
    CiamMfaContext,
    pkce_pair,
)
from custom_components.addhon.client.transport.device import HonDevice  # noqa: E402
from custom_components.addhon.client.transport.values import CIAM_URL  # noqa: E402
from custom_components.addhon.error_codes import (  # noqa: E402
    AUTH_GET_TOKEN,
    AUTH_REFRESH,
    DECODE_ERROR,
    INVALID_CREDENTIALS,
    RATE_LIMITED,
    classify,
)

_PASSWORD = "s3cret-pass"
_TOKENS = {
    "id_token": "id.jwt",
    "cognito_token": "cognito.jwt",
    "access_token": "access",
    "refresh_token": "ciam-refresh",
}


class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Answers `<step>` with the next scripted (status, body); records every call."""

    def __init__(self, **script: list) -> None:
        self._script = {step: list(answers) for step, answers in script.items()}
        self.calls: list[tuple[str, str, dict]] = []

    def _answer(self, method: str, url: str, payload: dict):
        step = url.rsplit("/", 1)[-1]
        self.calls.append((method, step, payload))
        answer = self._script[step].pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _Response(*answer)

    def get(self, url, params=None, headers=None):
        return self._answer("GET", url, dict(params or {}))

    def post(self, url, json=None, headers=None):
        return self._answer("POST", url, dict(json or {}))


def _auth(session: _Session) -> CiamAuth:
    return CiamAuth(session, "user@example.com", _PASSWORD, HonDevice("addhon"))


def _run(coro):
    return asyncio.run(coro)


class PkceTest(unittest.TestCase):
    def test_the_pair_is_the_app_s_s256(self) -> None:
        verifier, challenge = pkce_pair()
        self.assertEqual(128, len(verifier))
        self.assertTrue(verifier.isascii() and verifier.isalnum())
        digest = hashlib.sha256(verifier.encode()).digest()
        self.assertEqual(base64.urlsafe_b64encode(digest).rstrip(b"=").decode(), challenge)
        self.assertNotIn("=", challenge)

    def test_every_pair_is_new(self) -> None:
        self.assertNotEqual(pkce_pair()[0], pkce_pair()[0])


class SignInTest(unittest.TestCase):
    def test_authorize_then_token_fills_the_contract(self) -> None:
        session = _Session(
            authorize=[(200, {"session_id": "S1"})],
            token=[(200, {"tokens": dict(_TOKENS)})],
        )
        auth = _auth(session)
        _run(auth.authenticate())
        self.assertEqual(("id.jwt", "cognito.jwt", "access"),
                         (auth.id_token, auth.cognito_token, auth.access_token))
        self.assertEqual("S1", auth.refresh_token)  # the session_id is what renews
        (_, step1, query), (_, step2, body) = session.calls
        self.assertEqual("authorize", step1)
        self.assertEqual({"username", "password", "code_challenge"}, set(query))
        self.assertEqual("token", step2)
        self.assertEqual("S1", body["session_id"])
        # The verifier sent to token is the one the challenge was made from.
        digest = hashlib.sha256(body["code_verifier"].encode()).digest()
        self.assertEqual(
            base64.urlsafe_b64encode(digest).rstrip(b"=").decode(), query["code_challenge"]
        )

    def test_tokens_given_as_json_text_are_read(self) -> None:
        session = _Session(
            authorize=[(200, {"session_id": "S1"})],
            token=[(200, {"tokens": json.dumps(_TOKENS)})],
        )
        auth = _auth(session)
        _run(auth.authenticate())
        self.assertEqual("cognito.jwt", auth.cognito_token)

    def test_a_refused_sign_in_is_invalid_credentials(self) -> None:
        for answer in ((401, {"code": "AUTH-INV-PARAM"}), (200, {"error": "x"}), (200, {})):
            with self.subTest(answer=answer):
                auth = _auth(_Session(authorize=[answer]))
                with self.assertRaises(NativeAuthError) as caught:
                    _run(auth.authenticate())
                self.assertIs(INVALID_CREDENTIALS, classify(caught.exception))

    def test_a_rate_limit_is_transient(self) -> None:
        auth = _auth(_Session(authorize=[(429, {})]))
        with self.assertRaises(RuntimeError) as caught:
            _run(auth.authenticate())
        self.assertIs(RATE_LIMITED, classify(caught.exception))

    def test_a_token_step_without_tokens_is_a_token_failure(self) -> None:
        auth = _auth(_Session(authorize=[(200, {"session_id": "S1"})], token=[(200, {})]))
        with self.assertRaises(NativeAuthError) as caught:
            _run(auth.authenticate())
        self.assertIs(AUTH_GET_TOKEN, classify(caught.exception))

    def test_the_password_never_leaves_in_an_error_or_a_log(self) -> None:
        class _RequestError(Exception):
            # As aiohttp's client errors: the request, URL included, rides along.
            request_info = object()

        for error in (_RequestError("bad request"),
                      ValueError(f"bad url ?password={_PASSWORD}")):
            with self.subTest(error=type(error).__name__):
                auth = _auth(_Session(authorize=[error]))
                with self.assertLogs("custom_components.addhon.client.transport",
                                     level=logging.DEBUG) as logs:
                    with self.assertRaises(RuntimeError) as caught:
                        _run(auth.authenticate())
                self.assertNotIn(_PASSWORD, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertFalse(any(_PASSWORD in record for record in logs.output))

    def test_an_error_without_the_request_keeps_its_own_type(self) -> None:
        auth = _auth(_Session(authorize=[OSError("Cannot connect to host api-iot.he.services")]))
        with self.assertRaises(OSError):
            _run(auth.authenticate())


class TwoFactorTest(unittest.TestCase):
    def _challenge(self, session: _Session) -> tuple[CiamAuth, CiamMfaContext]:
        auth = _auth(session)
        with self.assertRaises(MFAChallengeRequired) as caught:
            _run(auth.authenticate())
        return auth, caught.exception.context

    def test_a_code_then_the_tokens_of_the_same_session(self) -> None:
        session = _Session(
            authorize=[(200, {"session_id": "S1", "mfa_required": True})],
            otp=[(200, {})],
            token=[(200, {"tokens": dict(_TOKENS)})],
        )
        auth, context = self._challenge(session)
        self.assertIsInstance(context, CiamMfaContext)
        _run(auth.submit_mfa_code(context, "123456"))
        self.assertEqual("S1", auth.refresh_token)
        self.assertEqual(("POST", "otp", {"otp": "123456", "session_id": "S1"}), session.calls[1])
        self.assertEqual(context.code_verifier, session.calls[2][2]["code_verifier"])

    def test_a_wrong_code_and_a_service_fault_are_told_apart(self) -> None:
        cases = (
            ((400, {}), MFACodeInvalid),
            ((200, {"error": "x"}), MFACodeInvalid),
            ((503, {}), MFAServiceError),
            ((429, {}), MFAServiceError),
        )
        for answer, expected in cases:
            with self.subTest(answer=answer):
                session = _Session(
                    authorize=[(200, {"session_id": "S1", "mfa_required": True})], otp=[answer]
                )
                auth, context = self._challenge(session)
                with self.assertRaises(expected):
                    _run(auth.submit_mfa_code(context, "000000"))

    def test_a_resend_is_a_new_sign_in_on_the_same_context(self) -> None:
        session = _Session(authorize=[(200, {"session_id": "S1", "mfa_required": True}),
                                      (200, {"session_id": "S2", "mfa_required": True})])
        auth, context = self._challenge(session)
        first_verifier = context.code_verifier
        _run(auth.resend_mfa_code(context))
        self.assertEqual("S2", context.session_id)
        self.assertNotEqual(first_verifier, context.code_verifier)

    def test_the_context_shows_no_secret(self) -> None:
        context = CiamMfaContext(session_id="S1", code_verifier="V" * 128)
        self.assertNotIn("S1", repr(context))
        self.assertNotIn("V" * 10, str(context))


class RefreshTest(unittest.TestCase):
    def test_the_session_renews_id_and_cognito(self) -> None:
        session = _Session(refresh=[(200, {"tokens": {"id_token": "id2", "cognito_token": "c2"}})])
        auth = _auth(session)
        self.assertTrue(_run(auth.refresh("S1")))
        self.assertEqual(
            ("id2", "c2", "S1"), (auth.id_token, auth.cognito_token, auth.refresh_token)
        )
        self.assertEqual(("POST", "refresh", {"session_id": "S1"}), session.calls[0])

    def test_a_refused_session_asks_for_the_reauth(self) -> None:
        for status in (400, 401, 403):
            with self.subTest(status=status):
                auth = _auth(_Session(refresh=[(status, {})]))
                with self.assertRaises(NativeAuthError) as caught:
                    _run(auth.refresh("S1"))
                self.assertIs(AUTH_REFRESH, classify(caught.exception))

    def test_a_rate_limit_or_a_server_fault_is_transient_never_false(self) -> None:
        for status in (429, 500, 503):
            with self.subTest(status=status):
                auth = _auth(_Session(refresh=[(status, {})]))
                with self.assertRaises(RuntimeError) as caught:
                    _run(auth.refresh("S1"))
                self.assertIsNot(AUTH_REFRESH, classify(caught.exception))

    def test_an_answer_without_tokens_is_unreadable(self) -> None:
        auth = _auth(_Session(refresh=[(200, {"tokens": {}})]))
        with self.assertRaises(RuntimeError) as caught:
            _run(auth.refresh("S1"))
        self.assertIs(DECODE_ERROR, classify(caught.exception))

    def test_the_contract_flag(self) -> None:
        self.assertFalse(CiamAuth.allows_password_relogin)


class ConnectionChoiceTest(unittest.TestCase):
    def test_an_entry_naming_ciam_gets_the_ciam_sign_in(self) -> None:
        from custom_components.addhon.client.transport.auth import HonAuth
        from custom_components.addhon.client.transport.connection import HonConnection
        from custom_components.addhon.client.transport.values import (
            AUTH_BACKEND_CIAM,
            AUTH_BACKEND_SALESFORCE,
        )

        async def built(backend):
            conn = HonConnection("e", "p", session=object(), auth_backend=backend)
            await conn.create()
            return type(conn.auth)

        self.assertIs(CiamAuth, _run(built(AUTH_BACKEND_CIAM)))
        self.assertIs(HonAuth, _run(built(AUTH_BACKEND_SALESFORCE)))
        self.assertIs(HonAuth, _run(built("anything else")))

    def test_a_ciam_connection_never_signs_in_again_with_the_password(self) -> None:
        from custom_components.addhon.client.transport.connection import HonConnection
        from custom_components.addhon.client.transport.values import AUTH_BACKEND_CIAM

        async def reauth():
            conn = HonConnection("e", "p", session=object(), auth_backend=AUTH_BACKEND_CIAM)
            await conn.create()
            called = []

            async def authenticate():
                called.append(True)

            conn.auth.authenticate = authenticate
            try:
                await conn._reauth_after_rejection(conn.refresh_generation)
            finally:
                self.assertEqual([], called)

        with self.assertRaises(NativeAuthError) as caught:
            _run(reauth())
        self.assertIs(AUTH_REFRESH, classify(caught.exception))


if __name__ == "__main__":
    unittest.main()
