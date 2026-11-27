# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""CIAM sign-in, as the hOn app 2.30.7 does it. Not used unless an entry asks for it.

The app no longer carries the Salesforce sign-in `HonAuth` drives (no account2 host, no
`services/oauth2`, no Aura or ProgressiveLogin in its bundle): it signs in through four
steps on `API_URL/ciam` (apk2 decomp.txt @3315470-3315680, analysis in
apk2/analysis/repo-esterni-2026-11-27/E-ciam-login.md):

1. `GET authorize?username&password&code_challenge` with a PKCE challenge, which
   answers a `session_id`, or `mfa_required` for a two-factor account (`loginAws`
   @3530291-3530520);
2. `POST otp {otp, session_id}` for that account (`setOtpEpic` @3360445);
3. `POST token {session_id, code_verifier}`, which hands over the id, cognito, access
   and refresh tokens at once (`retrieveToken` @3358951-3359110);
4. `POST refresh {session_id}` to renew the id and cognito tokens (@564121-564600).

`CiamAuth` keeps `HonAuth`'s contract towards `HonConnection`: the same token
attributes, `authenticate`, `refresh`, the two 2FA calls and `clear`. Its
`refresh_token` holds the CIAM `session_id`, the one value the renewal needs, so the
entry keeps it where it keeps Salesforce's refresh token.

The sign-in spans four modules: this one runs the steps; `ciam_http` makes the one
HTTP exchange each step goes through; `ciam_protocol` holds the app's shapes (PKCE,
the 2FA context, what each answer means), with no I/O; `ciam_state` holds the tokens
and their freshness.

Two choices of the owner (2026-11-27) set it apart from `HonAuth`:
- no `auth/v2/login` after the tokens: the app calls it to register the phone, it
  hands back no token, and whether the cloud needs it is for a live login to tell;
- no sign-in with the password once a session exists (`allows_password_relogin`): a
  refusal of the session raises the reauth, as the app logs out. A rate limit or a
  server error stays transient. The app never repeats authorize on its own, and
  another hOn client that did reported the account being throttled.

The password travels in authorize's query string, as the app sends it. The URL is
therefore never logged, never put into the diagnostics trace and never left in an
exception: `ciam_http` re-raises an error that holds the request without it.
"""
from __future__ import annotations

from typing import Any

import aiohttp

from ...error_codes import DECODE_ERROR
from ..phase import PhaseTracker
from .auth import MFAChallengeRequired, NativeAuthError
from .ciam_http import exchange
from .ciam_protocol import REFUSED, CiamMfaContext, coded, pkce_pair
from .ciam_state import CiamTokenState
from .device import HonDevice

__all__ = ["CiamAuth", "CiamMfaContext", "pkce_pair"]


class CiamAuth(CiamTokenState):
    """The CIAM sign-in, with `HonAuth`'s contract (see the module docstring)."""

    # A session refused by the cloud raises the reauth instead of a new sign-in with
    # the stored password. Read by HonConnection; HonAuth answers True.
    allows_password_relogin = False

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        device: HonDevice,
        auth_trace: Any = None,
        phase_tracker: PhaseTracker | None = None,
    ) -> None:
        # `device` and `auth_trace` are taken for the contract only: the CIAM steps
        # carry no device descriptor, and record nothing in the trace, so the
        # authorize URL and its password can never reach a diagnostics dump.
        del device, auth_trace
        self._session = session
        self._email, self._password = email, password
        self._phase_tracker = phase_tracker
        self._current_phase = ""
        self.clear()

    # -- sign-in -------------------------------------------------------------------
    async def authenticate(self) -> None:
        """Sign in with the email and password: authorize, then the tokens.

        A two-factor account raises `MFAChallengeRequired` with a `CiamMfaContext`;
        the flow then calls `submit_mfa_code`.
        """
        self.clear()
        await self._take_tokens(await self._authorize())

    async def _authorize(self) -> CiamMfaContext:
        verifier, challenge = pkce_pair()
        body = await exchange(self, 
            "authorize",
            {"username": self._email, "password": self._password, "code_challenge": challenge},
        )
        session_id = body.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            text, code = REFUSED["authorize"]
            raise coded(NativeAuthError(f"{text} (no session)"), code)
        context = CiamMfaContext(session_id=session_id, code_verifier=verifier)
        if body.get("mfa_required"):
            raise MFAChallengeRequired(context)
        return context

    async def _take_tokens(self, context: CiamMfaContext) -> None:
        body = await exchange(self, 
            "token", {"session_id": context.session_id, "code_verifier": context.code_verifier}
        )
        text, code = REFUSED["token"]
        self._adopt(body, coded(NativeAuthError(f"{text} (answer)"), code))
        self.refresh_token = context.session_id

    # -- two-factor ----------------------------------------------------------------
    async def submit_mfa_code(self, context: CiamMfaContext, code: str) -> None:
        """Send the code, then take the tokens of the same session."""
        await exchange(self, "otp", {"otp": code, "session_id": context.session_id})
        await self._take_tokens(context)

    async def resend_mfa_code(self, context: CiamMfaContext) -> None:
        """A new code is a new sign-in, as in the app: the context moves to its session."""
        try:
            fresh = await self._authorize()
        except MFAChallengeRequired as challenge:
            fresh = challenge.context
        context.session_id, context.code_verifier = fresh.session_id, fresh.code_verifier

    # -- renewal -------------------------------------------------------------------
    async def refresh(self, refresh_token: str = "") -> bool:
        """Renew the id and cognito tokens of the session (`POST refresh {session_id}`).

        True on success. A refused session raises AUTH_REFRESH (reauth), a rate limit
        or a server error a transient error, an answer without tokens an unreadable
        one: never False, so the connection never falls back to a sign-in with the
        password.
        """
        self.refresh_token = refresh_token or self.refresh_token
        body = await exchange(self, "refresh", {"session_id": self.refresh_token})
        self._adopt(body, coded(RuntimeError("ciam refresh: answer without tokens"), DECODE_ERROR))
        return True
