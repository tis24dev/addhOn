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

from .device import HonDevice
from .headers import USER_AGENT
from .oauth import (
    AUTH_API,
    CLIENT_ID,
    build_authorize_url,
    build_login_payload,
    extract_login_url,
    generate_nonce,
    is_oauth_done,
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
                    raise _NoAuthNeeded()
                raise NativeAuthError(f"introduce: no login url (status {resp.status})")
        return login_url

    async def _manual_redirect(self, url: str) -> str:
        async with self._session.get(
            url, allow_redirects=False, headers=self._ua()
        ) as resp:
            return resp.headers.get("Location", "") or url

    async def _handle_redirects(self, login_url: str) -> str:
        r1 = await self._manual_redirect(login_url)
        r2 = await self._manual_redirect(r1)
        return f"{r2}&System=IoT_Mobile_App&RegistrationSubChannel=hOn"

    async def _open_login_page(self, login_url: str) -> None:
        async with self._session.get(
            URL(login_url, encoded=True), headers=self._ua()
        ) as resp:
            text = await resp.text()
            match = _FWUID_RE.findall(text)
            if not match:
                raise NativeAuthError(f"login page: no fwuid (status {resp.status})")
            self._fw_uid, loaded_str = match[0]
            self._loaded = json.loads(loaded_str)
            self._page_url = login_url.replace(AUTH_API, "")

    async def _login(self) -> str:
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
                    return str(result["events"][0]["attributes"]["values"]["url"])
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass
            raise NativeAuthError(f"login: failed (status {resp.status})")

    async def _get_token(self, url: str) -> None:
        async with self._session.get(url, headers=self._ua()) as resp:
            if resp.status != 200:
                raise NativeAuthError(f"get_token: status {resp.status}")
            href = _HREF_RE.findall(await resp.text())
        if not href:
            raise NativeAuthError("get_token: no href")
        if "ProgressiveLogin" in href[0]:
            async with self._session.get(href[0], headers=self._ua()) as resp:
                if resp.status != 200:
                    raise NativeAuthError(f"progressive: status {resp.status}")
                href = _HREF_RE_PROGRESSIVE.findall(await resp.text())
            if not href:  # like the guard after the first findall: no IndexError
                raise NativeAuthError("progressive: no href")
        token_url = AUTH_API + href[0]
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
            raise NativeAuthError("api_auth: no cognito token")

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
            return

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
        self._expires = datetime.now(timezone.utc)
        self.id_token = data["id_token"]
        self.access_token = data["access_token"]
        await self._api_auth()
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
