"""Native authenticated HTTP connection (addhOn transport).

get/post with per-request token injection (`build_auth_headers`) and retry on
expired token / 401-403 (loop 0 -> refresh, loop 1 -> re-auth, loop >=2 ->
error). Uses HonAuth.

Happy path validated live; the retry branches have offline tests with a mocked session.
"""
from __future__ import annotations

import asyncio
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

# Per-request HTTP timeouts on the session WE own. Without them aiohttp defaults to
# a 300s total, so a dead/blocked endpoint (e.g. api-iot.he.services or AWS IoT
# unreachable on the user's network) only failed when the 60s dedicated-loop cap
# fired, as an opaque message-less timeout (issue #30). These bound each request
# well under that cap, so a stuck endpoint fails fast and attributable.
_CONNECT_TIMEOUT = 10  # TCP connect + TLS handshake to one endpoint
_TOTAL_TIMEOUT = 30  # whole request incl. response read
_SOCK_READ_TIMEOUT = 20  # gap between received chunks


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
        # Serializes token refresh/authenticate across concurrent requests (e.g.
        # the asyncio.gather burst in load_commands): without it, N parallel
        # _check_headers would each fire a refresh on the SAME refresh_token.
        # Lives on the connection (stable owner), NOT on HonAuth which create()
        # replaces. Instantiated here (not in create()) so concurrent coroutines
        # share the same lock.
        self._refresh_lock = asyncio.Lock()
        # Monotonic counter, bumped (under _refresh_lock) on every successful
        # refresh/authenticate by BOTH the pre-request path (_check_headers) and the
        # 401/403 retry path (_refresh_after_rejection). Each request snapshots it
        # when it sends; the retry recovery refreshes only if the gen is unchanged ->
        # a concurrent burst collapses to a single refresh without gating on
        # token_expires_soon (a non-expiry 401 must still refresh once). See CR#3.
        self._refresh_gen = 0

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
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=_TOTAL_TIMEOUT,
                    connect=_CONNECT_TIMEOUT,
                    sock_connect=_CONNECT_TIMEOUT,
                    sock_read=_SOCK_READ_TIMEOUT,
                )
            )
        try:
            self._auth = HonAuth(self._session, self._email, self._password, self._device)
        except BaseException:
            # We just created (and own) the ClientSession; if anything after that fails
            # a failed create() must not leak it. close() only closes a session WE own
            # (a caller-supplied session is left alone). (#31 root, defense in depth.)
            await self.close()
            raise
        return self

    async def _check_headers(self, headers: dict) -> dict:
        # Refresh ONLY when needed: no usable token in RAM (first request, or a
        # restart with a persisted refresh_token) OR the token is near expiry.
        # Previously this refreshed on EVERY request (#1) and recursed into a
        # second refresh on 401 (#14). The 401 recovery lives in _intercept and is
        # untouched. The lock + re-check (double-checked locking) collapses a burst
        # of concurrent requests into a single refresh/authenticate (#race), so a
        # rotating IdP cannot invalidate the shared refresh_token mid-flight.
        def _need_refresh() -> bool:
            have_tokens = bool(self.auth.cognito_token and self.auth.id_token)
            return bool(self._refresh_token) and (not have_tokens or self.auth.token_expires_soon)

        def _need_auth() -> bool:
            return not (self.auth.cognito_token and self.auth.id_token)

        if _need_refresh() or _need_auth():
            async with self._refresh_lock:
                if _need_refresh():
                    await self.auth.refresh(self._refresh_token)
                    self._refresh_token = self.auth.refresh_token
                    self._refresh_gen += 1
                if _need_auth():
                    await self.auth.authenticate()
                    self._refresh_token = self.auth.refresh_token
                    self._refresh_gen += 1
        return build_auth_headers(self.auth.cognito_token, self.auth.id_token, headers)

    async def _refresh_after_rejection(self, gen_at_send: int) -> None:
        # 401/403 recovery refresh, single-flighted under the SAME lock as the
        # pre-request path (CR#3). Without the lock a concurrent-request burst (e.g.
        # command_loader's asyncio.gather) that all 401 would each fire a refresh on
        # the same rotating, possibly single-use refresh_token. `gen_at_send` is the
        # refresh generation captured when THIS request was sent: under the lock we
        # refresh only if no sibling (pre-request OR retry) has refreshed since, so the
        # burst collapses to exactly one refresh -- yet a genuine non-expiry 401 still
        # refreshes once (we do NOT gate on token_expires_soon). Copy the (possibly
        # rotated) refresh_token back so a later refresh/persist uses the current one
        # (the missing copy-back let a stale token re-stale auth and force a re-login).
        #
        # The lock is RELEASED on return, BEFORE the caller's recursive _intercept,
        # which re-acquires it via _check_headers -- asyncio.Lock is not reentrant, so
        # holding it across the recursion would deadlock.
        async with self._refresh_lock:
            if self._refresh_gen != gen_at_send:
                return  # a sibling already refreshed; reuse its fresh tokens
            await self.auth.refresh(self._refresh_token)
            self._refresh_token = self.auth.refresh_token
            self._refresh_gen += 1

    @asynccontextmanager
    async def _intercept(
        self, method, url: Any, *args: Any, loop: int = 0, **kwargs: Any
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        kwargs["headers"] = await self._check_headers(kwargs.get("headers", {}))
        # Generation of the token these headers carry: if a concurrent request (the
        # pre-request path or another 401 retry) refreshes before our recovery runs,
        # the gen advances and _refresh_after_rejection skips a redundant, token-
        # consuming refresh (CR#3). Snapshot BEFORE sending so it reflects the token
        # that may get rejected, not one a sibling rotated to meanwhile.
        refresh_gen = self._refresh_gen
        async with method(url, *args, **kwargs) as response:
            if (self.auth.token_expires_soon or response.status in (401, 403)) and loop == 0:
                _LOGGER.info("addhOn: token expiring/%s, refresh", response.status)
                await self._refresh_after_rejection(refresh_gen)
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
