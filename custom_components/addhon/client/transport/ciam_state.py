# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The tokens a CIAM sign-in holds, and how fresh they are (`ciam.CiamAuth`'s state).

The attributes are `HonAuth`'s, which `HonConnection` reads: `id_token`,
`cognito_token`, `access_token`, `refresh_token` (here the CIAM `session_id`), and the
two freshness flags.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .auth import _OPAQUE_TTL_SECONDS, _REFRESH_SKEW_SECONDS
from .ciam_protocol import REQUIRED_TOKENS, TOKEN_SLOTS, tokens_of
from .tokens import token_expiry


class CiamTokenState:
    """Token slots and freshness, filled by `_adopt` from a CIAM answer."""

    _current_phase = ""

    # -- state ---------------------------------------------------------------------
    def clear(self) -> None:
        for slot in (*TOKEN_SLOTS.values(), "refresh_token"):
            setattr(self, slot, "")
        # When the id token was taken and its JWT `exp` (None for an opaque token).
        self._taken_at = datetime.now(timezone.utc).timestamp()
        self._access_expiry: float | None = None

    def _seconds_left(self) -> float:
        expiry = self._access_expiry
        if expiry is None:
            expiry = self._taken_at + _OPAQUE_TTL_SECONDS
        return expiry - datetime.now(timezone.utc).timestamp()

    @property
    def token_is_expired(self) -> bool:
        return self._seconds_left() <= 0

    @property
    def token_expires_soon(self) -> bool:
        return self._seconds_left() <= _REFRESH_SKEW_SECONDS

    def _adopt(self, body: dict[str, Any], missing: Exception) -> None:
        """Take the tokens of `body`, or raise `missing` when a required one is absent."""
        tokens = tokens_of(body)
        if not all(tokens.get(name) for name in REQUIRED_TOKENS):
            raise missing
        for name, slot in TOKEN_SLOTS.items():
            if tokens.get(name):
                setattr(self, slot, str(tokens[name]))
        self._taken_at = datetime.now(timezone.utc).timestamp()
        self._access_expiry = token_expiry(self.id_token)
        self._current_phase = ""
