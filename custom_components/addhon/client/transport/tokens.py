# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

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

3. :func:`token_person_account_id` -- read the Salesforce person-account id our own
   id_token claims, so the appliance-list census can check WHOSE account the cloud
   answered for without asking the cloud anything (see :func:`api.account_match`).
   A sibling of `token_expiry`, over the same decoder: both are unverified reads of a
   claim out of a token we already hold.
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


def _jwt_claims(jwt: str) -> dict | None:
    """Unverified read of a JWT payload section as a claims mapping, or None.

    The signature is NOT checked: every caller reads a token THIS process already
    holds and the IdP already validated, and none of them makes a trust decision on
    what comes back -- `token_expiry` only shortens a lifetime, and
    `token_person_account_id` only feeds a comparison whose output is a closed-domain
    token. A verifying parser here would need the IdP's keys and a network round trip
    to answer a question nobody asks.

    EXTRACTED from `token_expiry` rather than copied into the second reader. The three
    steps below (take section 1, restore the base64url padding RFC 7515 sec2 strips,
    urlsafe-decode) are exactly the ones that go wrong quietly: a padding rule that
    drifts between two copies gives one reader a claim and the other a None, and this
    one sits under the token-refresh path of every request in the transport.

    Returns None -- never a partial value -- for anything that is not a readable JWT
    carrying a JSON OBJECT, so a caller always reads its claim off a real mapping.
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
    return claims if isinstance(claims, dict) else None


def token_expiry(jwt: str) -> float | None:
    """Unverified read of a JWT's ``exp`` claim as epoch seconds (RFC 7519 sec4.1.4).

    The signature is NOT checked (the IdP already validated it and we only need the
    stated lifetime, so we never send a token past its own ``exp``). Returns None for
    anything that is not a readable JWT with a numeric ``exp`` -- the caller then falls
    back to a conservative window rather than trusting a made-up lifetime.

    `bool` is refused explicitly: ``isinstance(True, int)`` is True, and an ``exp`` of
    ``true`` would otherwise become an expiry of 1.0 (the epoch), i.e. a token this
    transport would consider permanently stale.
    """
    claims = _jwt_claims(jwt)
    exp = claims.get("exp") if claims is not None else None
    return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None


# Where the hOn id_token keeps its Salesforce identity claims. Both names are OURS --
# read off a live capture of this integration's own token and written down here
# (apk/analysis/addhon210-healthy-envelope-baseline.md) -- not values chosen by the
# cloud, so nothing in this lookup can be steered by a response.
_CUSTOM_ATTRIBUTES = "custom_attributes"
_PERSON_ACCOUNT_ID = "PersonAccountId"


def token_person_account_id(jwt: str) -> str | None:
    """The ``custom_attributes.PersonAccountId`` claim of an id_token, or None.

    The one identity of ours that the appliance-list response also carries: every
    appliance in it is stamped with `sfPersonAccountId`, so this claim is what lets
    `api.account_match` answer "did this session resolve to the account we think we
    are logged into" WITHOUT a second request and without the cloud's cooperation.

    Returns None -- "we have no identity to compare with", a diagnosis in its own
    right -- for a token that is not readable, that carries no `custom_attributes`
    object, or whose claim is absent or empty. `type(value) is str`, not isinstance:
    the value is about to be compared for equality against a string from the cloud,
    and a str SUBCLASS is free to override `__eq__` so that it equals anything, which
    would turn a mismatch into a match. `json.loads` cannot produce one; a future
    caller handing this function a claims-shaped object could.

    THE VALUE IS AN IDENTIFIER AND NEVER LEAVES THE PROCESS. Its only consumer
    compares it and emits a token of `api.ACCOUNT_TOKENS`; it is never logged, never
    stored, and never reaches a diagnostics document.
    """
    claims = _jwt_claims(jwt)
    custom = claims.get(_CUSTOM_ATTRIBUTES) if claims is not None else None
    value = custom.get(_PERSON_ACCOUNT_ID) if isinstance(custom, dict) else None
    return value if type(value) is str and value else None
