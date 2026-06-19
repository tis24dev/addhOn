"""Parsing of the OAuth tokens from the hOn login redirect (addhOn transport).

From the redirect
`.../mobilesdk/detect/oauth/done#access_token=...&refresh_token=...&id_token=...`
it extracts the three tokens via the regex `name=(.*?)&` (up to the first `&`).

The tokens are passed to the cloud unchanged, so the parsing rules are exact:
- only `refresh_token` is URL-decoded (`unquote`); access/id stay raw;
- a token at the end WITHOUT a trailing `&` is NOT captured (the regex requires the `&`);
- `complete` = all three patterns HAVE matched; an empty captured value is still
  accepted, so this is "all three matched", not "all values non-empty".
"""
from __future__ import annotations

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


def parse_token_fragment(text: str) -> OAuthTokens:
    """Extract access/refresh/id token from the OAuth redirect text."""

    def _match(name: str) -> str | None:
        found = re.findall(f"{name}=(.*?)&", text)
        return found[0] if found else None

    access = _match("access_token")
    refresh = _match("refresh_token")
    id_token = _match("id_token")
    return OAuthTokens(
        access_token=access or "",
        # Only the refresh token is URL-decoded.
        refresh_token=unquote(refresh) if refresh is not None else "",
        id_token=id_token or "",
        # What counts is that the pattern MATCHED (not that the value is
        # non-empty), hence `None not in (...)`.
        complete=None not in (access, refresh, id_token),
    )
