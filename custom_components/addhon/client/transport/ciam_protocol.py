# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shapes of the hOn app's CIAM sign-in: PKCE, the 2FA context, and what each answer means.

No I/O here: `ciam.CiamAuth` runs the steps, `ciam_state` holds the tokens. Sources in
the app 2.30.7 (apk2 decomp.txt) are cited beside each piece; the whole flow is in
apk2/analysis/repo-esterni-2026-11-27/E-ciam-login.md.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import string
from dataclasses import dataclass, field
from typing import Any

from ...error_codes import AUTH_GET_TOKEN, AUTH_REFRESH, INVALID_CREDENTIALS
from .auth import MFACodeInvalid, MFAServiceError, NativeAuthError

# PKCE as the app builds it (module 10249, @3363087-3363160): a 128-character verifier
# of ASCII letters and digits, and the base64url SHA-256 of it without padding.
_VERIFIER_ALPHABET = string.ascii_letters + string.digits
_VERIFIER_LENGTH = 128


def pkce_pair() -> tuple[str, str]:
    """A fresh `(code_verifier, code_challenge)` (S256, as the app computes it)."""
    verifier = "".join(secrets.choice(_VERIFIER_ALPHABET) for _ in range(_VERIFIER_LENGTH))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@dataclass
class CiamMfaContext:
    """What a CIAM two-factor sign-in needs to resume: the session and its verifier.

    Mutable on purpose: a resend is a new authorize (the app's "send another code"
    re-runs the sign-in, @3531516-3531518), which opens a new session with a new
    verifier, and the flow keeps holding this same object.
    """

    session_id: str = field(repr=False)
    code_verifier: str = field(repr=False)
    challenge_kind: str = "otp"
    can_resend: bool = True

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"CiamMfaContext(kind={self.challenge_kind}, can_resend={self.can_resend})"

    __repr__ = __str__


def coded(error: Exception, code: Any) -> Exception:
    error.error_code = code  # type: ignore[attr-defined]
    return error


# What a refusal (a 4xx other than 429, or an answer naming an error) of each step
# raises. A rate limit or a server fault is transient on every step but `otp`, where the
# 2FA flow tells the user to retry; `otp` refusals are a wrong or expired code.
REFUSED = {
    "authorize": ("ciam authorize: credential rejected", INVALID_CREDENTIALS),
    "token": ("ciam get_token: no tokens", AUTH_GET_TOKEN),
    "refresh": ("ciam refresh: session rejected", AUTH_REFRESH),
}

# The tokens a step must hand over, and where each one lands on the auth object. The
# CIAM refresh token is kept for the app's detach-device call; nothing here sends it.
REQUIRED_TOKENS = ("id_token", "cognito_token")
TOKEN_SLOTS = {
    "id_token": "id_token",
    "cognito_token": "cognito_token",
    "access_token": "access_token",
    "refresh_token": "_ciam_refresh_token",
}


def tokens_of(data: Any) -> dict[str, Any]:
    """`tokens` of a CIAM answer, an object or its JSON text; {} when absent."""
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except ValueError:
            return {}
    return tokens if isinstance(tokens, dict) else {}


def check(step: str, status: int, body: dict[str, Any]) -> None:
    """Raise what `step` raises for `status` and `body`; return on a usable answer."""
    if step == "otp":
        if status == 429 or status >= 500:
            raise MFAServiceError("mfa: verification service error")
        if status >= 400 or body.get("error"):
            raise MFACodeInvalid("mfa: invalid verification code")
        return
    if status == 429:
        raise RuntimeError("hOn rate limited (status 429)")
    if status >= 500:
        raise RuntimeError(f"hOn server error (status {status}, ciam {step})")
    if status >= 400 or body.get("error"):
        text, code = REFUSED[step]
        raise coded(NativeAuthError(f"{text} (status {status})"), code)

