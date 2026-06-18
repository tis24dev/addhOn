"""Connessione HTTP autenticata nativa (transport addhОn).

Porting di pyhon `connection/handler/hon.py` + `base.py`: get/post con iniezione
dei token per-richiesta (`build_auth_headers`) e retry su token scaduto / 401-403
(loop 0 → refresh, loop 1 → re-auth, loop ≥2 → errore). Usa il NOSTRO HonAuth.

Happy path validato live; i rami di retry hanno test offline a sessione mockata.
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
    """Sessione HTTP autenticata: crea/possiede aiohttp.ClientSession + HonAuth."""

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
            raise NativeAuthError("connessione non creata (manca create())")
        return self._auth

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise NativeAuthError("nessuna sessione aiohttp")
        return self._session

    async def create(self) -> "HonConnection":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        self._auth = HonAuth(self._session, self._email, self._password, self._device)
        return self

    async def _check_headers(self, headers: dict) -> dict:
        # Come pyhon _check_headers: se ho un refresh_token provo a rinfrescare,
        # altrimenti (o se mancano i token) faccio il login; poi inietto i token.
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
                _LOGGER.info("addhОn: token in scadenza/%s, refresh", response.status)
                await self.auth.refresh(self._refresh_token)
                async with self._intercept(method, url, *args, loop=1, **kwargs) as result:
                    yield result
            elif (self.auth.token_is_expired or response.status in (401, 403)) and loop == 1:
                _LOGGER.warning("addhОn: re-auth dopo %s", response.status)
                await self.create()
                async with self._intercept(method, url, *args, loop=2, **kwargs) as result:
                    yield result
            elif loop >= 2 and (
                self.auth.token_is_expired or response.status in (401, 403)
            ):
                # Terzo tentativo dopo re-auth: fallisce solo se è ANCORA non
                # autorizzato. Se invece la re-auth ha funzionato (200), si cade nel
                # ramo else e si restituisce la risposta (prima si sollevava sempre,
                # scartando un recupero riuscito).
                raise NativeAuthError(f"Login failure (status {response.status})")
            else:
                # Forza un decode-check prima di yield-are (come pyhОn).
                # content_type=None: deviazione VOLUTA (coerente con auth.py) — tollera
                # content-type non-JSON ma body JSON valido (Salesforce a volte lo fa);
                # un body NON-JSON solleva comunque JSONDecodeError → "Decode Error".
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
