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
from datetime import datetime, timedelta, timezone
from typing import Any

from yarl import URL

from ...debug_utils import redact_remoting_summary
from ...error_codes import (
    MFA_CODE_INVALID,
    MFA_REQUIRED,
    MFA_SEND_FAILED,
    MFA_SERVICE_ERROR,
    MFA_TOKEN_AFTER_VERIFY_FAILED,
)
from .device import HonDevice
from .headers import USER_AGENT
from .oauth import (
    APEXREMOTE_PATH,
    AUTH_API,
    CLIENT_ID,
    MfaContext,
    build_authorize_url,
    build_finish_body,
    build_login_payload,
    build_remoting_payload,
    detect_progressive_otp,
    extract_login_url,
    generate_nonce,
    is_oauth_done,
    parse_remoting_result,
)
from .tokens import parse_token_fragment

_LOGGER = logging.getLogger(__name__)

API_URL = "https://api-iot.he.services"

_TOKEN_EXPIRES_AFTER_HOURS = 8
_TOKEN_EXPIRE_WARNING_HOURS = 7

# Extracts fwuid + loaded from the Salesforce login page (aura).
_FWUID_RE = re.compile('"fwuid":"(.*?)","loaded":(\\{.*?})')
# Extracts the href of the token page (post-login). Two different regexes are
# used: (.+?) on the first page, (.*?) in the ProgressiveLogin branch; the second
# also matches an empty href, which the flow accepts.
_HREF_RE = re.compile("href\\s*=\\s*[\"'](.+?)[\"']")
_HREF_RE_PROGRESSIVE = re.compile("href\\s*=\\s*[\"'](.*?)[\"']")


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

    def __init__(self, context: MfaContext) -> None:
        self.context = context
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


class _NoAuthNeeded(Exception):
    """The authorize page was already the redirect with the tokens (login not needed)."""


class HonAuth:
    """Native hOn login flow. Assembles the pieces + the HTTP orchestration."""

    def __init__(self, session, email: str, password: str, device: HonDevice) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._device = device
        self._expires = datetime.now(timezone.utc)
        self.access_token = ""
        self.refresh_token = ""
        self.cognito_token = ""
        self.id_token = ""
        self._fw_uid = ""
        self._loaded: Any = None
        self._page_url = ""
        # Last login phase reached, for the DEBUG trace + diagnostics attribution ("failed
        # during mfa_verify"). Updated by _phase(); read via NativeHon.auth_phase.
        self._current_phase = ""

    def _phase(self, name: str, **fields: Any) -> None:
        """Mark + DEBUG-log a login phase. Content is STRUCTURE only (status/booleans/
        phase name) -- never email/password/OTP/token/csrf/cookie/url (leak-proof)."""
        self._current_phase = name
        if _LOGGER.isEnabledFor(logging.DEBUG):
            extra = " ".join(f"{k}={v}" for k, v in fields.items())
            _LOGGER.debug("auth phase %s%s", name, f": {extra}" if extra else "")

    def _expired(self, hours: int) -> bool:
        return datetime.now(timezone.utc) >= self._expires + timedelta(hours=hours)

    @property
    def token_is_expired(self) -> bool:
        return self._expired(_TOKEN_EXPIRES_AFTER_HOURS)

    @property
    def token_expires_soon(self) -> bool:
        return self._expired(_TOKEN_EXPIRE_WARNING_HOURS)

    def _ua(self, extra: dict | None = None) -> dict:
        headers = {"user-agent": USER_AGENT}
        if extra:
            headers.update(extra)
        return headers

    async def _introduce(self) -> str:
        self._phase("introduce")
        url = build_authorize_url(generate_nonce())
        async with self._session.get(url, headers=self._ua()) as resp:
            text = await resp.text()
            self._expires = datetime.now(timezone.utc)
            login_url = extract_login_url(text)
            if login_url is None:
                if is_oauth_done(text):
                    t = parse_token_fragment(text)
                    self.access_token = t.access_token
                    self.refresh_token = t.refresh_token
                    self.id_token = t.id_token
                    self._phase("introduce", status=resp.status, no_auth_needed=True)
                    raise _NoAuthNeeded()
                self._phase("introduce", status=resp.status, login_url=False)
                raise NativeAuthError(f"introduce: no login url (status {resp.status})")
        self._phase("introduce", status=resp.status, login_url=True)
        return login_url

    async def _manual_redirect(self, url: str) -> str:
        async with self._session.get(
            url, allow_redirects=False, headers=self._ua()
        ) as resp:
            return resp.headers.get("Location", "") or url

    async def _handle_redirects(self, login_url: str) -> str:
        self._phase("redirects")
        r1 = await self._manual_redirect(login_url)
        r2 = await self._manual_redirect(r1)
        return f"{r2}&System=IoT_Mobile_App&RegistrationSubChannel=hOn"

    async def _open_login_page(self, login_url: str) -> None:
        self._phase("login_page")
        async with self._session.get(
            URL(login_url, encoded=True), headers=self._ua()
        ) as resp:
            text = await resp.text()
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
        async with self._session.post(
            AUTH_API + "/s/sfsites/aura",
            headers=self._ua({"Content-Type": "application/x-www-form-urlencoded"}),
            data=body,
            params=params,
        ) as resp:
            if resp.status == 200:
                try:
                    result = await resp.json(content_type=None)
                    redirect = str(result["events"][0]["attributes"]["values"]["url"])
                    self._phase("login_submit", status=resp.status, redirect=True)
                    return redirect
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
            self._phase("login_submit", status=resp.status, redirect=False)
            raise NativeAuthError(f"login: failed (status {resp.status})")

    async def _get_token(self, url: str) -> None:
        self._phase("get_token")
        async with self._session.get(url, headers=self._ua()) as resp:
            if resp.status != 200:
                self._phase("get_token", status=resp.status)
                raise NativeAuthError(f"get_token: status {resp.status}")
            href = _HREF_RE.findall(await resp.text())
        if not href:
            self._phase("get_token", status=resp.status, href=False)
            raise NativeAuthError("get_token: no href")
        if "ProgressiveLogin" in href[0]:
            async with self._session.get(href[0], headers=self._ua()) as resp:
                if resp.status != 200:
                    self._phase("progressive_detect", status=resp.status)
                    raise NativeAuthError(f"progressive: status {resp.status}")
                prog_text = await resp.text()
                # resp.url is the final (post-redirect) URL; fall back to the requested
                # href if the response object does not expose it (e.g. test doubles).
                prog_url = str(getattr(resp, "url", "") or href[0])
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
            if not href:  # like the guard after the first findall: no IndexError
                raise NativeAuthError("progressive: no href")
        token_url = AUTH_API + href[0]
        self._phase("get_token", status=200, href=True)
        async with self._session.get(token_url, headers=self._ua()) as resp:
            if resp.status != 200:
                raise NativeAuthError(f"token page: status {resp.status}")
            tokens = parse_token_fragment(await resp.text())
        if not tokens.complete:
            raise NativeAuthError("token page: incomplete tokens")
        self.access_token = tokens.access_token
        self.refresh_token = tokens.refresh_token
        self.id_token = tokens.id_token

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
        async with self._session.post(
            f"{API_URL}/auth/v1/login",
            headers=self._ua({"id-token": self.id_token}),
            json=device_payload,
        ) as resp:
            data = await resp.json(content_type=None)
        self.cognito_token = data.get("cognitoUser", {}).get("Token", "")
        if not self.cognito_token:
            self._phase("api_auth", status=resp.status, cognito_token=False)
            raise NativeAuthError("api_auth: no cognito token")
        self._phase("api_auth", status=resp.status, cognito_token=True)

    async def authenticate(self) -> None:
        self.clear()
        try:
            login_url = await self._introduce()
            redirect = await self._handle_redirects(login_url)
            await self._open_login_page(redirect)
            url = await self._login()
            await self._get_token(url)
            await self._api_auth()
        except _NoAuthNeeded:
            pass
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
        async with self._session.post(
            context.host + APEXREMOTE_PATH, json=payload, headers=headers
        ) as resp:
            status = resp.status
            text = await resp.text()
        entry = parse_remoting_result(text)
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
            await resp.text()  # consume the postback response (redirect to retURL)
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
        async with self._session.get(url, headers=self._ua()) as resp:
            text = await resp.text()
        self._expires = datetime.now(timezone.utc)
        # Extract the done-URL FIRST and parse only it (mirrors the live-validated probe).
        # Parsing the whole page first would let a stray `*_token=...&` substring elsewhere
        # on the page (inline JS, echoed state) be captured instead of the real token. The
        # trailing '&' lets parse_token_fragment capture the last fragment field.
        done_url = extract_login_url(text)
        if done_url and "access_token" in done_url:
            tokens = parse_token_fragment(done_url + "&")
        else:
            tokens = parse_token_fragment(text)
        if not tokens.complete:
            self._phase("resume_token", done_url=bool(done_url), tokens_complete=False)
            raise MFATokenAfterVerifyFailed("mfa: token retrieval failed after verification")
        self.access_token = tokens.access_token
        self.refresh_token = tokens.refresh_token
        self.id_token = tokens.id_token

    async def refresh(self, refresh_token: str = "") -> bool:
        if refresh_token:
            self.refresh_token = refresh_token
        params = {
            "client_id": CLIENT_ID,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }
        async with self._session.post(
            f"{AUTH_API}/services/oauth2/token", params=params, headers=self._ua()
        ) as resp:
            if resp.status >= 400:
                return False
            data = await resp.json(content_type=None)
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
        # Note: `AUTH_API.split("/")[-2]` is '' here (not the host, because there is
        # no trailing slash), so clear_domain('') is effectively a no-op on any
        # session. This is intentional.
        self._session.cookie_jar.clear_domain(AUTH_API.split("/")[-2])
        self.cognito_token = ""
        self.id_token = ""
        self.access_token = ""
        self.refresh_token = ""
