# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""OAuth token parsing + lifetime derivation for the addhOn transport (spec: HHT-sec6).

Two independent pieces, both authored from public contracts (RFC 6749 / RFC 7519):

1. :func:`parse_token_fragment` -- read access/refresh/id tokens out of the OAuth2
   implicit-flow redirect (RFC 6749 sec4.2.2: the tokens come back in the URL
   *fragment* as ``&``-delimited ``name=value`` fields). Deliberate, cloud-safe
   divergences from a naive ``parse_qs``:
     * access_token / id_token are kept RAW -- the cloud is handed the exact bytes,
       so they are NOT percent-decoded;
     * only refresh_token is percent-decoded once (``unquote``);
     * a field is "present" if its key appears (an empty value still counts);
     * the LAST field needs NO trailing ``&`` (a real fragment need not end in one).
   The field name is anchored to a fragment delimiter so ``access_token`` cannot
   match inside a longer key.

2. :func:`token_expiry` -- read the JWT ``exp`` claim (RFC 7519 sec4.1.4) so the
   transport trusts the token's own stated lifetime instead of a guessed constant.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from urllib.parse import unquote


@dataclass(frozen=True)
class OAuthTokens:
    """Tokens extracted from the OAuth redirect. `complete` = all three present.

    NB: `cognito_token` is NOT here: it comes from a separate POST (token-refresh),
    not from the redirect.
    """

    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    complete: bool = False


def _field(name: str, text: str) -> str | None:
    """First value of fragment field ``name`` in ``text``, or None if absent.

    RFC 6749 sec4.2.2: fragment fields are ``&``-delimited, so a value runs up to the
    next ``&`` OR the end of the string. The name is anchored to a delimiter boundary
    (start, ``#``, ``?`` or ``&``) so it cannot match a substring of another key.
    """
    # The value class stops at any character that cannot legitimately appear in a
    # fragment field value: `&` (the field delimiter), whitespace, and the quote /
    # angle-bracket markup that wraps the redirect URL inside a page (`href='...'`,
    # `href="..."`, `...>`). RFC 6749 sec4.2.2 values are application/x-www-form-
    # urlencoded, so `"`, `'`, `<`, `>` and whitespace are always percent-encoded and
    # never appear literally in a real token; excluding them means a WHOLE-PAGE parse
    # cannot fold the surrounding markup into a token (which would otherwise forward a
    # malformed id-token header to the cloud -- e.g. `id_token=CCC'>` -> `CCC`).
    match = re.search(r"(?:\A|[#?&])" + re.escape(name) + r"=([^&\s\"'<>]*)", text)
    return match.group(1) if match else None


def parse_token_fragment(text: str) -> OAuthTokens:
    """Extract access/refresh/id token from the OAuth redirect text."""
    access = _field("access_token", text)
    refresh = _field("refresh_token", text)
    id_token = _field("id_token", text)
    return OAuthTokens(
        access_token=access or "",
        # Only the refresh token is URL-decoded (access/id are forwarded verbatim).
        refresh_token=unquote(refresh) if refresh is not None else "",
        id_token=id_token or "",
        # "Present" = the field key appeared, even with an empty value.
        complete=None not in (access, refresh, id_token),
    )


def token_expiry(jwt: str) -> float | None:
    """Unverified read of a JWT's ``exp`` claim as epoch seconds (RFC 7519 sec4.1.4).

    The signature is NOT checked (the IdP already validated it and we only need the
    stated lifetime, so we never send a token past its own ``exp``). Returns None for
    anything that is not a readable JWT with a numeric ``exp`` -- the caller then falls
    back to a conservative window rather than trusting a made-up lifetime.
    """
    try:
        payload_b64 = jwt.split(".")[1]
    except (AttributeError, IndexError):
        return None
    payload_b64 += "=" * (-len(payload_b64) % 4)  # restore base64url padding
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None
