"""HTTP headers of the addhOn transport.

Authenticated header construction: every authenticated request carries
user-agent + Content-Type + the two tokens (cognito-token, id-token).

PURE function: the tokens are inputs, no hardcoded secret. `USER_AGENT` is the
value sent to the cloud (pinned by the test); the real app value will enter as a
separate step.
"""
from __future__ import annotations

from typing import Mapping

# User-Agent value sent to the cloud (impersonation placeholder).
USER_AGENT = "Chrome/999.999.999.999"
CONTENT_TYPE = "application/json"

# Base headers present on EVERY request.
BASE_HEADERS: dict[str, str] = {
    "user-agent": USER_AGENT,
    "Content-Type": CONTENT_TYPE,
}


def build_auth_headers(
    cognito_token: str,
    id_token: str,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Headers for an authenticated request.

    Merges the base headers with the caller's `extra` PLUS the two tokens: the
    `extra` (and the tokens) win over the base ones, and the tokens are always
    present.
    """
    overrides: dict[str, str] = dict(extra) if extra else {}
    overrides["cognito-token"] = cognito_token
    overrides["id-token"] = id_token
    return BASE_HEADERS | overrides
