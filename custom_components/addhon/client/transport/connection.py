"""Native authenticated HTTP connection (addhOn transport).

get/post with per-request token injection (`build_auth_headers`) and retry on
expired token / 401-403 (loop 0 -> refresh, loop 1 -> re-auth, loop >=2 ->
error). Uses HonAuth.

Happy path validated live; the retry branches have offline tests with a mocked session.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiohttp

from .auth import HonAuth, NativeAuthError
from .device import HonDevice
from .headers import build_auth_headers

_LOGGER = logging.getLogger(__name__)


class HonConnection:
    """Authenticated HTTP session: creates/owns aiohttp.ClientSession + HonAuth."""

    def __init__(
        self,
        email: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
        mobile_id: str = "",
        refresh_token: str = "",
    ) -> None:
        self._email = email
        self._password = password
        self._device = HonDevice(mobile_id)
        self._refresh_token = refresh_token
        self._owns_session = session is None
        self._session = session
        self._auth: HonAuth | None = None

    @property
    def device(self) -> HonDevice:
        return self._device

    @property
    def auth(self) -> HonAuth:
        if self._auth is None:
            raise NativeAuthError("connection not created (create() is missing)")
        return self._auth

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise NativeAuthError("no aiohttp session")
        return self._session

    async def create(self) -> "HonConnection":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        self._auth = HonAuth(self._session, self._email, self._password, self._device)
        return self

    async def _check_headers(self, headers: dict) -> dict:
        # If I have a refresh_token I try to refresh, otherwise (or if the tokens
        # are missing) I log in; then I inject the tokens.
        if self._refresh_token:
            await self.auth.refresh(self._refresh_token)
        if not (self.auth.cognito_token and self.auth.id_token):
            await self.auth.authenticate()
        self._refresh_token = self.auth.refresh_token
        return build_auth_headers(self.auth.cognito_token, self.auth.id_token, headers)

    @asynccontextmanager
    async def _intercept(
        self, method, url: Any, *args: Any, loop: int = 0, **kwargs: Any
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        kwargs["headers"] = await self._check_headers(kwargs.get("headers", {}))
        async with method(url, *args, **kwargs) as response:
            if (self.auth.token_expires_soon or response.status in (401, 403)) and loop == 0:
                _LOGGER.info("addhOn: token expiring/%s, refresh", response.status)
                await self.auth.refresh(self._refresh_token)
                async with self._intercept(method, url, *args, loop=1, **kwargs) as result:
                    yield result
            elif (self.auth.token_is_expired or response.status in (401, 403)) and loop == 1:
                _LOGGER.warning("addhOn: re-auth after %s", response.status)
                await self.create()
                async with self._intercept(method, url, *args, loop=2, **kwargs) as result:
                    yield result
            elif loop >= 2 and (
                self.auth.token_is_expired or response.status in (401, 403)
            ):
                # Third attempt after re-auth: fails only if it is STILL not
                # authorized. If instead the re-auth worked (200), we fall into the
                # else branch and return the response (before, it always raised,
                # discarding a successful recovery).
                raise NativeAuthError(f"Login failure (status {response.status})")
            else:
                # Force a decode-check before yielding.
                # content_type=None: DELIBERATE (consistent with auth.py); it tolerates
                # a non-JSON content-type but a valid JSON body (Salesforce sometimes does this);
                # a NON-JSON body still raises JSONDecodeError -> "Decode Error".
                try:
                    await response.json(content_type=None)
                    yield response
                except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
                    raise NativeAuthError("Decode Error") from exc

    @asynccontextmanager
    async def get(self, *args: Any, **kwargs: Any) -> AsyncIterator[aiohttp.ClientResponse]:
        async with self._intercept(self.session.get, *args, **kwargs) as response:
            yield response

    @asynccontextmanager
    async def post(self, *args: Any, **kwargs: Any) -> AsyncIterator[aiohttp.ClientResponse]:
        async with self._intercept(self.session.post, *args, **kwargs) as response:
            yield response

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
