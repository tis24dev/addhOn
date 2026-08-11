# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Native addhOn auth: the hOn login flow (Salesforce OAuth).

Assembles the native pieces (oauth, tokens, device, headers) + the HTTP
orchestration. Validated LIVE (not offline): the login makes real requests to the
cloud. Uses a single aiohttp.ClientSession (the Salesforce flow cookies must
persist across the requests).

The PURE sub-builders/parsers (build_login_payload, the fwuid/href regexes) have
offline tests; the orchestration (authenticate) is validated live.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from yarl import URL

from ...debug_utils import redact_remoting_summary
from ...error_codes import (
    ACCOUNT_ACTION_REQUIRED,
    MFA_CODE_INVALID,
    MFA_REQUIRED,
    MFA_SEND_FAILED,
    MFA_SERVICE_ERROR,
    MFA_TOKEN_AFTER_VERIFY_FAILED,
)
from ..auth_diagnostics import (
    ACCOUNT_ACTION_VERDICTS,
    AuthDiagnosticTrace,
    analyze_page,
    classify_endpoint,
    classify_token_page,
    summarize_json,
    summarize_links,
    summarize_response,
    summarize_tokens,
)
from ..budget import AUTH_FULL, current_deadline
from ..phase import PhaseTracker
from .device import HonDevice
from .headers import USER_AGENT
from .retry import RetryBudget, retry_transport
from .oauth import (
    APEXREMOTE_PATH,
    AUTH_API,
    CLIENT_ID,
    MfaContext,
    absolutize,
    build_authorize_url,
    build_finish_body,
    build_login_payload,
    build_remoting_payload,
    detect_progressive_otp,
    extract_login_url,
    generate_nonce,
    is_oauth_done,
    oauth_done_fragment,
    parse_remoting_result,
)
from .tokens import parse_token_fragment, token_expiry
from .values import API_URL

_LOGGER = logging.getLogger(__name__)

# Token freshness is derived from the id_token's own JWT `exp` (spec: HHT-sec6.3), so
# the pyhOn 8h/7h invented heuristic is gone. These two apply ONLY when a token is
# opaque (no readable exp): a SHORT conservative window, never hours-stale.
_OPAQUE_TTL_SECONDS = 55 * 60      # common Cognito/OAuth access-token life
_REFRESH_SKEW_SECONDS = 5 * 60     # refresh this long before the stated expiry

# Aura framework bootstrap (spec: HHT-sec5): the login page embeds the framework
# descriptor as JSON `{"fwuid":"<hash>","loaded":{...},...}`. Authored from that JSON
# shape -- read the fwuid hash and the adjacent `loaded` object.
_FWUID_RE = re.compile(r'"fwuid":"(?P<fwuid>.*?)","loaded":(?P<loaded>\{.*?\})')
# Post-login redirect pages (spec: HHT-sec5) carry the next hop as an `href="..."`
# (double- or single-quoted). Two authored variants: the strict one requires a
# non-empty target (the token page); the progressive one also accepts an empty href,
# which the ProgressiveLogin branch legitimately yields.
_HREF_RE = re.compile(r"""href\s*=\s*["'](?P<target>.+?)["']""")
_HREF_RE_PROGRESSIVE = re.compile(r"""href\s*=\s*["'](?P<target>.*?)["']""")


class NativeAuthError(Exception):
    """Error of the native auth flow."""


class MFAChallengeRequired(Exception):
    """A 2FA email-OTP challenge surfaced during login (Salesforce ProgressiveLogin).

    NOT an auth failure: it carries the :class:`MfaContext` needed to send/verify the
    code and resume on the SAME session. `error_code` routes it like a reauth (so a
    background setup hitting it fails into the reauth flow that CAN prompt), while the
    interactive config flow catches the TYPE to drive the 2FA step. Message is a fixed
    identity-free token."""

    error_code = MFA_REQUIRED

    def __init__(self, context: MfaContext, client: Any = None) -> None:
        self.context = context
        # The live HonClient whose session holds the challenge, attached by the
        # config flow (validate_input). DECLARED (not a dynamic attribute) so the
        # carry is part of the contract: any layer that re-raises THIS exception
        # MUST preserve `client`, otherwise the client is orphaned -- validate_input
        # deliberately skips its close on a challenge, trusting this handoff.
        self.client = client
        super().__init__("mfa_required")


class MFACodeInvalid(NativeAuthError):
    """The submitted OTP was rejected by verifyEmailOTP (wrong or expired code)."""

    error_code = MFA_CODE_INVALID


# Distinguishable 2FA sub-failures, carried so classify()/_requires_reauth route them
# precisely (no fragile message matching). All subclass NativeAuthError so the existing
# broad excepts keep working. 162/163 are NOT reauth (transient); 164 IS.
class MFASendFailed(NativeAuthError):
    """resendEmailCode did not confirm the send (transient: the user can retry/resend)."""

    error_code = MFA_SEND_FAILED


class MFAServiceError(NativeAuthError):
    """verifyEmailOTP returned a service exception / 5xx (transient, not a wrong code)."""

    error_code = MFA_SERVICE_ERROR


class MFATokenAfterVerifyFailed(NativeAuthError):
    """OTP accepted but the post-verify authorize did not yield tokens."""

    error_code = MFA_TOKEN_AFTER_VERIFY_FAILED


class AccountActionRequired(NativeAuthError):
    """The password was accepted but the account is parked on a step only the USER can
    clear (a set/change-password form, a consent wall) instead of the token hand-off.

    Structurally detected on the token page (issue #67): retrying cannot fix it, so it
    gets its own code and a message that says WHERE to go, rather than the mute
    "token retrieval failed"."""

    error_code = ACCOUNT_ACTION_REQUIRED


class _NoAuthNeeded(Exception):
    """The authorize page was already the redirect with the tokens (login not needed)."""


class HonAuth:
    """Native hOn login flow. Assembles the pieces + the HTTP orchestration."""

    def __init__(
        self,
        session,
        email: str,
        password: str,
        device: HonDevice,
        auth_trace: AuthDiagnosticTrace | None = None,
        phase_tracker: PhaseTracker | None = None,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._device = device
        self._auth_trace = auth_trace or AuthDiagnosticTrace(enabled=False)
        self._expires = datetime.now(timezone.utc)
        # Epoch seconds of the id_token's JWT `exp`, or None for an opaque token
        # (then the conservative opaque window applies). Set by _remember_expiry().
        self._access_expiry: float | None = None
        self.access_token = ""
        self.refresh_token = ""
        self.cognito_token = ""
        self.id_token = ""
        self._fw_uid = ""
        self._loaded: Any = None
        self._page_url = ""
        # Last login phase reached, for the DEBUG trace + diagnostics attribution ("failed
        # during mfa_verify"). Updated by _phase(); read via NativeHon.auth_phase. Stays
        # FLAT on purpose: it is the legacy mirror the diagnostics already publish.
        self._current_phase = ""
        # Shared with the session/connection (client/phase.py): the cross-thread mirror
        # of the HIERARCHICAL phase, so a login running lazily inside load_appliances
        # says "load_appliances/auth/..." instead of borrowing its caller's label (#76).
        self._phase_tracker = phase_tracker
        # Extra attempts for the idempotent steps of the CURRENT authenticate(); None
        # outside it, so the refresh and 2FA paths are never retried.
        self._retry_budget: RetryBudget | None = None

    def _phase(self, name: str, **fields: Any) -> None:
        """Mark + DEBUG-log a login phase. Content is STRUCTURE only (status/booleans/
        phase name) -- never email/password/OTP/token/csrf/cookie/url (leak-proof)."""
        self._current_phase = name
        if self._phase_tracker is not None:
            self._phase_tracker.step(name)
        if _LOGGER.isEnabledFor(logging.DEBUG):
            extra = " ".join(f"{k}={v}" for k, v in fields.items())
            _LOGGER.debug("auth phase %s%s", name, f": {extra}" if extra else "")

    def _remember_expiry(self) -> None:
        """Record the id_token's own expiry (its JWT `exp`) after it is (re)assigned,
        so freshness tracks the token itself rather than an invented constant."""
        self._access_expiry = token_expiry(self.id_token)

    def _opaque_deadline(self) -> float:
        """Fallback expiry epoch for an opaque token: a short window from when it was
        obtained (never pyhOn's 8h)."""
        return self._expires.timestamp() + _OPAQUE_TTL_SECONDS

    @property
    def token_is_expired(self) -> bool:
        now = datetime.now(timezone.utc).timestamp()
        deadline = self._opaque_deadline() if self._access_expiry is None else self._access_expiry
        return now >= deadline

    @property
    def token_expires_soon(self) -> bool:
        now = datetime.now(timezone.utc).timestamp()
        deadline = self._opaque_deadline() if self._access_expiry is None else self._access_expiry
        return now >= deadline - _REFRESH_SKEW_SECONDS

    def _ua(self, extra: dict | None = None) -> dict:
        headers = {"user-agent": USER_AGENT}
        if extra:
            headers.update(extra)
        return headers

    def _diagnostic_request(
        self, phase: str, method: str, endpoint: str
    ) -> float | None:
        if not self._auth_trace.enabled:
            return None
        self._auth_trace.request(phase, method, endpoint)
        return time.monotonic()

    def _diagnostic_response(
        self, phase: str, resp: Any, body: str | bytes, started: float | None
    ) -> None:
        if not self._auth_trace.enabled or started is None:
            return
        history = getattr(resp, "history", ()) or ()
        self._auth_trace.response(
            phase,
            summarize_response(
                status=getattr(resp, "status", 0),
                headers=getattr(resp, "headers", {}),
                body=body,
                elapsed_ms=(time.monotonic() - started) * 1000,
                redirects=len(history),
            ),
        )

    def _diagnostic_page(
        self, phase: str, url: Any, text: Any
    ) -> tuple[Any, Any] | None:
        """Emit the structure AND the named identity of a page (issue #67).

        The skeleton is emitted only for a page the flow did not expect: on the known
        stops it would be noise, on an unexpected landing page it is what identifies it
        without a second round-trip to the reporter. Returns the summary pair so a
        caller that later needs a verdict does not parse the same body twice."""
        if not self._auth_trace.enabled:
            return None
        try:
            shape, page = analyze_page(url, text)
        except Exception:  # noqa: BLE001 - diagnostics are observational, never fatal
            return None
        self._auth_trace.html(phase, shape)
        self._auth_trace.page(phase, page)
        if shape.page_kind not in ("login", "progressive_login", "oauth_done", "mfa"):
            self._auth_trace.skeleton(phase, shape)
        return shape, page

    def _page_verdict(
        self,
        phase: str,
        url: Any,
        text: Any,
        summaries: tuple[Any, Any] | None = None,
    ) -> str:
        """Why a page is a dead end, as a controlled verdict. Runs with the diagnostics
        OFF too: the actionable error must not depend on the opt-in checkbox. Reuses the
        summaries the trace already built, when there are any."""
        verdict = "unknown"
        try:
            shape, page = summaries if summaries else analyze_page(url, text)
            verdict = classify_token_page(shape, page)
        except Exception:  # noqa: BLE001 - a diagnostic must never replace the failure
            verdict = "unknown"
        self._auth_trace.verdict(phase, verdict)
        return verdict

    def _guard_account_action(
        self,
        phase: str,
        url: Any,
        text: Any,
        summaries: tuple[Any, Any] | None = None,
    ) -> None:
        """Raise AccountActionRequired when a dead-end page is a step only the USER can
        clear, instead of the mute "no href" the caller would otherwise raise."""
        verdict = self._page_verdict(phase, url, text, summaries)
        if verdict in ACCOUNT_ACTION_VERDICTS:
            raise AccountActionRequired(
                f"{phase}: account action required ({verdict})"
            )

    def _diagnostic_links(
        self, phase: str, hrefs: list[str], selected_index: int = -1
    ) -> None:
        if self._auth_trace.enabled:
            self._auth_trace.links(
                phase,
                summarize_links(hrefs, selected_index=selected_index),
            )

    def _diagnostic_json(self, phase: str, value: Any) -> None:
        if self._auth_trace.enabled:
            self._auth_trace.json_shape(phase, summarize_json(value))

    def _diagnostic_tokens(self, phase: str, text: Any) -> None:
        if self._auth_trace.enabled:
            self._auth_trace.token_shape(phase, summarize_tokens(text))

    async def _introduce(self) -> str:
        self._phase("introduce")
        url = build_authorize_url(generate_nonce())
        started = self._diagnostic_request("introduce", "GET", "authorize")
        async with self._session.get(url, headers=self._ua()) as resp:
            text = await resp.text()
            self._diagnostic_response("introduce", resp, text, started)
            self._diagnostic_page("introduce", url, text)
            self._expires = datetime.now(timezone.utc)
            login_url = extract_login_url(text)
            if login_url is None:
                if is_oauth_done(text):
                    # SSO fast-path: the authorize page already carried the token
                    # fragment. Parse from the real `oauth/done#` marker (not the whole
                    # page) so a stray earlier `*_token=` elsewhere cannot be first-
                    # matched; parse_token_fragment then reads the last field with no
                    # trailing '&' (RFC 6749 sec4.2.2). Require .complete before
                    # committing, mirroring _resume_tokens_after_2fa.
                    t = parse_token_fragment(oauth_done_fragment(text) or text)
                    self._diagnostic_tokens(
                        "introduce", oauth_done_fragment(text) or text
                    )
                    if not t.complete:
                        self._phase(
                            "introduce", status=resp.status,
                            no_auth_needed=True, tokens_complete=False,
                        )
                        raise NativeAuthError(
                            f"introduce: incomplete token fragment (status {resp.status})"
                        )
                    self.access_token = t.access_token
                    self.refresh_token = t.refresh_token
                    self.id_token = t.id_token
                    self._remember_expiry()
                    self._phase("introduce", status=resp.status, no_auth_needed=True)
                    raise _NoAuthNeeded()
                self._phase("introduce", status=resp.status, login_url=False)
                raise NativeAuthError(f"introduce: no login url (status {resp.status})")
        self._phase("introduce", status=resp.status, login_url=True)
        return login_url

    async def _manual_redirect(self, url: str) -> str:
        started = self._diagnostic_request(
            "redirects", "GET", "manual_redirect"
        )
        async with self._session.get(
            absolutize(url), allow_redirects=False, headers=self._ua()
        ) as resp:
            self._diagnostic_response("redirects", resp, b"", started)
            return resp.headers.get("Location", "") or url

    async def _handle_redirects(self, login_url: str) -> str:
        self._phase("redirects")
        budget = self._retry_budget
        r1 = await retry_transport(
            budget, "manual_redirect", lambda: self._manual_redirect(login_url)
        )
        r2 = await retry_transport(
            budget, "manual_redirect", lambda: self._manual_redirect(r1)
        )
        return f"{r2}&System=IoT_Mobile_App&RegistrationSubChannel=hOn"

    async def _open_login_page(self, login_url: str) -> None:
        self._phase("login_page")
        # absolutize() then URL(..., encoded=True): urljoin does NOT re-encode the
        # already-encoded startURL=%2F... query, so the encoded contract is preserved
        # while a relative login_url no longer crashes the base_url-less session.
        login_url = absolutize(login_url)
        started = self._diagnostic_request(
            "login_page", "GET", classify_endpoint(login_url)
        )
        async with self._session.get(
            URL(login_url, encoded=True), headers=self._ua()
        ) as resp:
            text = await resp.text()
            self._diagnostic_response("login_page", resp, text, started)
            self._diagnostic_page("login_page", login_url, text)
            match = _FWUID_RE.findall(text)
            if not match:
                self._phase("login_page", status=resp.status, fwuid=False)
                raise NativeAuthError(f"login page: no fwuid (status {resp.status})")
            self._fw_uid, loaded_str = match[0]
            self._loaded = json.loads(loaded_str)
            self._page_url = login_url.replace(AUTH_API, "")
        self._phase("login_page", status=resp.status, fwuid=True)

    async def _login(self) -> str:
        self._phase("login_submit")
        body, params = build_login_payload(
            self._email, self._password, self._fw_uid, self._loaded, self._page_url
        )
        started = self._diagnostic_request(
            "login_submit", "POST", "aura_login"
        )
        self._auth_trace.payload("login_submit", "aura_login")
        async with self._session.post(
            AUTH_API + "/s/sfsites/aura",
            headers=self._ua({"Content-Type": "application/x-www-form-urlencoded"}),
            data=body,
            params=params,
        ) as resp:
            if resp.status == 200:
                try:
                    result = await resp.json(content_type=None)
                    self._diagnostic_json("login_submit", result)
                    redirect = str(result["events"][0]["attributes"]["values"]["url"])
                    self._diagnostic_response(
                        "login_submit", resp, b"", started
                    )
                    self._phase("login_submit", status=resp.status, redirect=True)
                    return redirect
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
            self._diagnostic_response("login_submit", resp, b"", started)
            self._phase("login_submit", status=resp.status, redirect=False)
            raise NativeAuthError(f"login: failed (status {resp.status})")

    async def _get_token(self, url: str) -> None:
        self._phase("get_token")
        started = self._diagnostic_request(
            "post_login", "GET", "post_login"
        )
        post_login_url = absolutize(url)
        async with self._session.get(post_login_url, headers=self._ua()) as resp:
            if resp.status != 200:
                self._diagnostic_response("post_login", resp, b"", started)
                self._phase("get_token", status=resp.status)
                raise NativeAuthError(f"get_token: status {resp.status}")
            text = await resp.text()
            self._diagnostic_response("post_login", resp, text, started)
            post_login_summaries = self._diagnostic_page(
                "post_login", post_login_url, text
            )
            href = _HREF_RE.findall(text)
            self._diagnostic_links(
                "post_login", href, selected_index=0 if href else -1
            )
        if not href:
            # A post-login page with no next hop may BE the account step (a consent form
            # has no navigation href at all), so name it before failing (issue #67).
            self._guard_account_action(
                "post_login", post_login_url, text, post_login_summaries
            )
            self._phase("get_token", status=resp.status, href=False)
            raise NativeAuthError("get_token: no href")
        if "ProgressiveLogin" in href[0]:
            started = self._diagnostic_request(
                "progressive_page", "GET", "progressive_login"
            )
            async with self._session.get(absolutize(href[0]), headers=self._ua()) as resp:
                if resp.status != 200:
                    self._diagnostic_response(
                        "progressive_page", resp, b"", started
                    )
                    self._phase("progressive_detect", status=resp.status)
                    raise NativeAuthError(f"progressive: status {resp.status}")
                prog_text = await resp.text()
                self._diagnostic_response(
                    "progressive_page", resp, prog_text, started
                )
                # resp.url is the final (post-redirect) URL; fall back to the requested
                # href (absolutized, so the MfaContext host derivation is correct) if the
                # response object does not expose it (e.g. test doubles).
                prog_url = str(getattr(resp, "url", "") or absolutize(href[0]))
                prog_summaries = self._diagnostic_page(
                    "progressive_page", prog_url, prog_text
                )
            # 2FA: when email OTP is enabled this page IS the verification step (no
            # usable redirect href -- the first one is a CSS asset). Detect it and
            # pause the login with the context to resume; otherwise behave exactly as
            # before (follow the redirect). Inert on non-2FA accounts.
            challenge = detect_progressive_otp(prog_text, prog_url)
            self._phase(
                "progressive_detect", otp=challenge is not None,
                can_resend=getattr(challenge, "can_resend", None),
            )
            if challenge is not None:
                raise MFAChallengeRequired(challenge)
            href = _HREF_RE_PROGRESSIVE.findall(prog_text)
            self._diagnostic_links(
                "progressive_page",
                href,
                selected_index=0 if href else -1,
            )
            if not href:  # like the guard after the first findall: no IndexError
                self._guard_account_action(
                    "progressive_page", prog_url, prog_text, prog_summaries
                )
                raise NativeAuthError("progressive: no href")
        token_url = absolutize(href[0])
        self._phase("get_token", status=200, href=True)
        started = self._diagnostic_request(
            "token_response", "GET", classify_endpoint(token_url)
        )
        async with self._session.get(token_url, headers=self._ua()) as resp:
            if resp.status != 200:
                self._diagnostic_response(
                    "token_response", resp, b"", started
                )
                raise NativeAuthError(f"token page: status {resp.status}")
            token_text = await resp.text()
            self._diagnostic_response(
                "token_response", resp, token_text, started
            )
            token_summaries = self._diagnostic_page(
                "token_response", token_url, token_text
            )
            self._diagnostic_tokens("token_response", token_text)
            tokens = parse_token_fragment(token_text)
        if not tokens.complete:
            # The page that should carry the OAuth hand-off did not. Say WHAT it carried
            # instead: a set/change-password form or a consent wall is an account step
            # only the user can clear, and retrying it forever (ADDHON-130) told them
            # nothing (issue #67). Never let the classification break the login: a
            # diagnostic failure must still raise the original token error.
            verdict = self._page_verdict(
                "token_response", token_url, token_text, token_summaries
            )
            self._phase("get_token", tokens_complete=False, page=verdict)
            if verdict in ACCOUNT_ACTION_VERDICTS:
                raise AccountActionRequired(
                    f"token page: account action required ({verdict})"
                )
            raise NativeAuthError(f"token page: incomplete tokens ({verdict})")
        self.access_token = tokens.access_token
        self.refresh_token = tokens.refresh_token
        self.id_token = tokens.id_token
        self._remember_expiry()

    async def _api_auth(self) -> None:
        self._phase("api_auth")
        # Our HonDevice exposes payload(); the get() branch is a defensive fallback
        # for a device that exposes the old interface. Same dictionary in
        # both cases.
        device_payload = (
            self._device.payload()
            if hasattr(self._device, "payload")
            else self._device.get()
        )
        started = self._diagnostic_request("api_auth", "POST", "api_auth")
        self._auth_trace.payload("api_auth", "api_auth")
        async with self._session.post(
            f"{API_URL}/auth/v1/login",
            headers=self._ua({"id-token": self.id_token}),
            json=device_payload,
        ) as resp:
            if resp.status != 200:
                self._diagnostic_response("api_auth", resp, b"", started)
                self._phase("api_auth", status=resp.status, cognito_token=False)
                # Preserve the HTTP status in the exception: the setup classifier uses
                # it to distinguish retryable server/rate-limit failures from an actual
                # credentials problem. Without it, every 5xx looked like invalid auth
                # and unnecessarily opened Home Assistant's reauth/2FA flow.
                raise NativeAuthError(f"api_auth: status {resp.status}")
            data = await resp.json(content_type=None)
            self._diagnostic_response("api_auth", resp, b"", started)
            self._diagnostic_json("api_auth", data)
        self.cognito_token = data.get("cognitoUser", {}).get("Token", "")
        if not self.cognito_token:
            self._phase("api_auth", status=resp.status, cognito_token=False)
            raise NativeAuthError(
                f"api_auth: no cognito token (status {resp.status})"
            )
        self._phase("api_auth", status=resp.status, cognito_token=True)

    async def authenticate(self) -> None:
        # WHAT IS RETRIED, and why the rest is not (issue #76). Retried, because a
        # duplicate delivery costs nothing: _introduce (a GET whose replay only opens
        # another authorize session, and which re-mints its own nonce), the two
        # _manual_redirect hops (GETs with allow_redirects=False that read one header),
        # _open_login_page (a GET, and the replay re-reads the fwuid/loaded that a
        # framework rotation would have invalidated) and _api_auth (the hOn endpoint we
        # already re-invoke on every successful refresh).
        #
        # NOT retried: _login submits the credentials and advances the Salesforce
        # session (its payload embeds the fwuid captured one step earlier, and a
        # duplicate delivery races the `sid` cookie the next three steps depend on); the
        # three GETs inside _get_token each consume a SINGLE-USE hand-off (a second
        # post-login fetch lands on a login page -> "no href" -> a transient blip would
        # become a permanent credentials error, and a second ProgressiveLogin fetch mints
        # a NEW MfaContext that any already-sent OTP no longer matches); refresh() spends
        # a rotating, single-use refresh token; every MFA step sends an email or burns a
        # verification attempt.
        self.clear()
        # Deadline for the shared retry budget, taken from the budget scope that is
        # ACTUALLY in force (client/budget.py) rather than re-derived from AUTH_FULL.
        # Re-deriving it was a fiction: the enclosing scope may be tighter, and a gate
        # measured against a deadline nobody enforces never refuses anything -- so the
        # retry became the very thing that spent the budget, the opposite of the
        # invariant retry.py claims. None only outside any scope (direct use, unit
        # tests), where the phase budget the call sites open is the honest stand-in.
        deadline = current_deadline()
        if deadline is None:
            deadline = time.monotonic() + AUTH_FULL
        budget = RetryBudget(deadline=deadline)
        self._retry_budget = budget
        # The enclosing `phase("auth")` scope is opened by the CALLER (connection.py),
        # next to the AUTH_FULL budget it belongs to, so an expiry is still inside the
        # scope when it is converted into a coded error.
        try:
            try:
                login_url = await retry_transport(budget, "introduce", self._introduce)
                redirect = await self._handle_redirects(login_url)
                await retry_transport(
                    budget, "login_page", lambda: self._open_login_page(redirect)
                )
                url = await self._login()
                await self._get_token(url)
                await retry_transport(budget, "api_auth", self._api_auth)
            except _NoAuthNeeded:
                # The authorize page already carried the OAuth tokens (a still-valid
                # SSO cookie), so the login steps are skipped -- but cognito_token is
                # minted ONLY by _api_auth and connection.py needs it for every API
                # call. Run it so this path completes with usable auth headers
                # instead of empty ones.
                await retry_transport(budget, "api_auth", self._api_auth)
        finally:
            self._retry_budget = None
        # Login complete: clear the phase so a LATER non-auth failure (e.g. a poll) is
        # not mis-attributed to the last auth step.
        self._current_phase = ""

    # -- Two-factor (email OTP) resume -----------------------------------------
    # These run on the SAME aiohttp session that hit the challenge (its cookies bind
    # the Salesforce verification), so they MUST be called on the connection whose
    # authenticate() raised MFAChallengeRequired. Validated live 2026-06-25.

    async def _mfa_remoting(
        self, context: MfaContext, descriptor: dict, data: list, tid: int, phase: str
    ) -> dict:
        """One Salesforce JS-Remoting call (POST /apexremote), returns the result entry."""
        self._phase(phase)
        payload = build_remoting_payload(context.vid, descriptor, data, tid)
        headers = self._ua(
            {
                "Content-Type": "application/json",
                "X-User-Agent": "Visualforce-Remoting",
                "Referer": context.referer,
            }
        )
        started = self._diagnostic_request(
            phase, "POST", "mfa_remoting"
        )
        self._auth_trace.payload(phase, "mfa_remoting")
        async with self._session.post(
            context.host + APEXREMOTE_PATH, json=payload, headers=headers
        ) as resp:
            status = resp.status
            text = await resp.text()
            self._diagnostic_response(phase, resp, text, started)
        entry = parse_remoting_result(text)
        self._diagnostic_json(phase, entry)
        # Leak-proof structural summary (result/statusCode/type/key-names only).
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "auth phase %s: remoting method=%s http=%d %s",
                phase, descriptor.get("method"), status, redact_remoting_summary(entry),
            )
        if not entry:
            raise NativeAuthError(f"mfa: unreadable remoting response (status {status})")
        return entry

    async def resend_mfa_code(self, context: MfaContext) -> None:
        """(Re)send the email OTP via resendEmailCode. This is also the FIRST send:
        merely loading the page does not email a code, the page's JS does."""
        entry = await self._mfa_remoting(
            context, context.resend,
            [{"expid": context.expid, "localeId": context.locale}], 11, "mfa_send",
        )
        if entry.get("result") is not True:
            raise MFASendFailed("mfa: could not send the verification code")

    async def submit_mfa_code(self, context: MfaContext, code: str) -> None:
        """Verify the OTP (remoting) -> finish (VF postback) -> obtain the tokens.

        On a wrong/expired code raises MFACodeInvalid so the flow can re-prompt."""
        entry = await self._mfa_remoting(context, context.verify, [code], 21, "mfa_verify")
        if entry.get("result") is not True:
            # A Salesforce remoting EXCEPTION / 5xx is a transient service error, not a
            # wrong code: surface it as MFAServiceError (cannot_connect/retry) so the user
            # is not told to re-enter a perfectly good OTP. A plain result==false IS a
            # wrong/expired code.
            status = entry.get("statusCode")
            if entry.get("type") == "exception" or (
                isinstance(status, int) and status >= 500
            ):
                raise MFAServiceError("mfa: verification service error")
            raise MFACodeInvalid("mfa: invalid verification code")
        # finishFlowCall: VF form postback (ViewState + the commandLink marker).
        self._phase("mfa_finish")
        started = self._diagnostic_request(
            "mfa_finish", "POST", "progressive_login"
        )
        self._auth_trace.payload("mfa_finish", "mfa_finish")
        async with self._session.post(
            context.vf_action,
            data=build_finish_body(context),
            headers=self._ua(
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": context.referer,
                }
            ),
        ) as resp:
            finish_text = await resp.text()
            self._diagnostic_response(
                "mfa_finish", resp, finish_text, started
            )
        await self._resume_tokens_after_2fa()
        await self._api_auth()
        self._current_phase = ""  # 2FA login complete

    async def _resume_tokens_after_2fa(self) -> None:
        """Re-run authorize on the now-verified session and extract the tokens.

        Post-2FA the authorize redirect carries the tokens. `extract_login_url` matches
        the `hon://...oauth/done#access_token=...` URL before `is_oauth_done` would, so
        we parse the tokens from whichever the page yields (with a trailing '&' so the
        last fragment field is captured)."""
        self._phase("resume_token")
        url = build_authorize_url(generate_nonce())
        started = self._diagnostic_request(
            "resume_token", "GET", "authorize"
        )
        async with self._session.get(url, headers=self._ua()) as resp:
            text = await resp.text()
            self._diagnostic_response("resume_token", resp, text, started)
            self._diagnostic_page("resume_token", url, text)
        self._expires = datetime.now(timezone.utc)
        # Extract the done-URL FIRST and parse only it (mirrors the live-validated probe).
        # Parsing the whole page first would let a stray `*_token=...&` substring elsewhere
        # on the page (inline JS, echoed state) be captured instead of the real token.
        # parse_token_fragment reads the last field with no trailing '&' (RFC 6749).
        done_url = extract_login_url(text)
        if done_url and "access_token" in done_url:
            token_source = done_url
        else:
            token_source = text
        self._diagnostic_tokens("resume_token", token_source)
        tokens = parse_token_fragment(token_source)
        if not tokens.complete:
            self._phase("resume_token", done_url=bool(done_url), tokens_complete=False)
            raise MFATokenAfterVerifyFailed("mfa: token retrieval failed after verification")
        self.access_token = tokens.access_token
        self.refresh_token = tokens.refresh_token
        self.id_token = tokens.id_token
        self._remember_expiry()

    async def refresh(self, refresh_token: str = "") -> bool:
        if refresh_token:
            self.refresh_token = refresh_token
        params = {
            "client_id": CLIENT_ID,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }
        started = self._diagnostic_request(
            "refresh", "POST", "token_refresh"
        )
        self._auth_trace.payload("refresh", "token_refresh")
        async with self._session.post(
            # Send the refresh_token in the FORM BODY (data=), not the query string
            # (params=). With params= it lands in the request URL, where it leaks into
            # proxy/access logs and aiohttp exception reprs (request_info.real_url). The
            # OAuth2 token endpoint expects application/x-www-form-urlencoded; Salesforce
            # accepts both, and a dict passed as data= is form-encoded into the body.
            f"{AUTH_API}/services/oauth2/token", data=params, headers=self._ua()
        ) as resp:
            if resp.status >= 400:
                self._diagnostic_response("refresh", resp, b"", started)
                return False
            data = await resp.json(content_type=None)
            self._diagnostic_response("refresh", resp, b"", started)
            self._diagnostic_json("refresh", data)
        # A malformed 2xx (no id_token/access_token) must NOT raise KeyError: treat
        # it as a failed refresh so the caller falls back to authenticate(). Do not
        # touch _expires before validating, or a fake refresh would mask expiry.
        id_token = data.get("id_token") if isinstance(data, dict) else None
        access_token = data.get("access_token") if isinstance(data, dict) else None
        if not id_token or not access_token:
            _LOGGER.warning("addhOn: refresh response missing tokens; treating as failure")
            return False
        self._expires = datetime.now(timezone.utc)
        self.id_token = id_token
        self.access_token = access_token
        self._remember_expiry()
        # Honour refresh_token rotation: if the IdP returned a new one, persist it
        # (otherwise the old token is reused and a future refresh would fail).
        if new_refresh := (data.get("refresh_token") if isinstance(data, dict) else None):
            self.refresh_token = new_refresh
        await self._api_auth()
        # Refresh succeeded (no full login): clear the phase so a later non-auth failure
        # is not mis-attributed to "api_auth" (symmetric with authenticate()).
        self._current_phase = ""
        return True

    def clear(self) -> None:
        # Clear the auth host's cookies so a REUSED session cannot carry a stale SSO
        # cookie into the next login and send _introduce down the already-authorized
        # fast-path with tokens that may no longer be valid. The previous
        # `AUTH_API.split("/")[-2]` was '' (AUTH_API has no trailing slash), so
        # clear_domain('') was a no-op and never cleared anything; use the real host.
        # urlsplit().netloc (stdlib) mirrors how oauth._AUTH_HOST is derived and, unlike
        # yarl.URL(...).host, works under the CI's minimal URL stub (which has no .host).
        auth_host = urlsplit(AUTH_API).netloc
        if auth_host:
            self._session.cookie_jar.clear_domain(auth_host)
        self.cognito_token = ""
        self.id_token = ""
        self.access_token = ""
        self.refresh_token = ""
        self._access_expiry = None
