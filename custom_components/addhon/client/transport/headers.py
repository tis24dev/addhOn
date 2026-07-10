# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""HTTP headers of the addhOn transport (spec: HHT-sec4).

Authenticated header construction: every authenticated request carries
user-agent + Content-Type + the two tokens (cognito-token, id-token).

PURE function: the tokens are inputs, no hardcoded secret. The header VALUES
(``USER_AGENT``, ``CONTENT_TYPE``) live in ``values.py`` with their provenance;
this module only assembles them. The two token header NAMES are Haier-mandated (the
API looks up ``cognito-token`` / ``id-token`` verbatim -- HHT-sec4.2).
"""
from __future__ import annotations

from typing import Mapping

from .values import CONTENT_TYPE, USER_AGENT

# Base headers present on EVERY request (values sourced from values.py).
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
