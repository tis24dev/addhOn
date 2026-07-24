# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded, secret-free structural diagnostics for the hOn login flow."""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

_MAX_EVENTS = 100
_MAX_BYTES = 64 * 1024

_PHASES = frozenset(
    {
        "introduce",
        "redirects",
        "login_page",
        "login_submit",
        "get_token",
        "post_login",
        "progressive_detect",
        "progressive_page",
        "token_response",
        "api_auth",
        "refresh",
        "mfa_send",
        "mfa_verify",
        "mfa_finish",
        "resume_token",
        "appliance_list",
        "setup",
    }
)
_METHODS = frozenset({"GET", "POST"})
_ENDPOINTS = frozenset(
    {
        "authorize",
        "login",
        "manual_redirect",
        "aura_login",
        "post_login",
        "progressive_login",
        "oauth_done",
        "token_refresh",
        "api_auth",
        "mfa_remoting",
        "static_asset",
        "auth_other",
        "api_other",
        "external",
        "other",
        "none",
        # Post-login interstitials the flow can be diverted to (issue #67). Named so a
        # landing page that is NOT the token redirect is identifiable from the trace.
        "change_password",
        "set_password",
        "reset_password",
        "secur",
        "apex_page",
    }
)
_MEDIA_TYPES = frozenset(
    {
        "text/html",
        "text/css",
        "text/plain",
        "application/json",
        "application/javascript",
        "text/javascript",
        "application/x-javascript",
        "application/octet-stream",
    }
)
_CHARSETS = frozenset({"utf-8", "utf8", "us-ascii", "iso-8859-1"})
_REASONS = frozenset(
    {
        "incomplete_tokens",
        "account_action",
        "no_href",
        "status",
        "no_login_url",
        "no_fwuid",
        "login_rejected",
        "no_cognito_token",
        "decode_error",
        "network_error",
        "timeout",
        "appliance_list_failed",
        "mfa_token_failed",
        "unexpected",
        "other",
    }
)
_ALLOWED_JSON_KEYS = frozenset(
    {
        "access_token",
        "attributes",
        "cognitouser",
        "data",
        "error",
        "events",
        "exception",
        "id_token",
        "message",
        "refresh_token",
        "result",
        "statuscode",
        "token",
        "type",
        "url",
        "values",
    }
)
_ALLOWED_TAGS = frozenset(
    {
        "a",
        "body",
        "button",
        "div",
        "form",
        "head",
        "html",
        "input",
        "label",
        "link",
        "meta",
        "script",
        "span",
        "title",
    }
)
_ALLOWED_ATTRS = frozenset(
    {
        "action",
        "charset",
        "class",
        "content",
        "href",
        "http-equiv",
        "id",
        "method",
        "name",
        "rel",
        "src",
        "type",
        "value",
    }
)
_TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")
_PAYLOAD_FIELDS = {
    "aura_login": ("message", "aura_context", "aura_page_uri", "aura_token"),
    "api_auth": ("device",),
    "mfa_remoting": ("action", "method", "data", "type", "tid", "context"),
    "mfa_finish": ("view_state", "finish_marker"),
    "token_refresh": ("client_id", "refresh_token", "grant_type"),
}
_TOKEN_RE = re.compile(
    r"(?:^|&amp;|[#?&])(?P<key>access_token|refresh_token|id_token)="
)
_CODE_RE = re.compile(r"^ADDHON-\d{3}$")

# -- Page-identity vocabulary (issue #67) -------------------------------------
# A sign-in that dies on "incomplete tokens" is only actionable if the trace says WHICH
# page the flow landed on. These allowlists turn a page into named structure: nothing
# outside them is ever emitted (unknown material is only COUNTED), so the identity of
# an unexpected interstitial is readable without shipping URL, markup or text.

# Path segments the login flow can legitimately reach (Salesforce Experience Cloud /
# VisualForce URL space). Anything else is counted as an unknown segment.
_PATH_SEGMENTS = frozenset(
    {
        "apex",
        "aura",
        "authorize",
        "changepassword",
        "checkpasswordresetstatus",
        "consent",
        "expid_login",
        "finaltok",
        "forgotpassword",
        "frontdoor",
        "identity",
        "login",
        "newhonlogin",
        "oauth2",
        "privacy",
        "profile",
        "progressivelogin",
        "register",
        "registration",
        "reset",
        "resetpassword",
        "s",
        "secur",
        "selfregister",
        "services",
        "setpassword",
        "sfsites",
        "terms",
        "token",
        "verify",
        "vforcesite",
    }
)
# Query-parameter NAMES worth naming (values are never read).
_QUERY_NAMES = frozenset(
    {
        "client_id",
        "code",
        "display",
        "ec",
        "error",
        "error_description",
        "expid",
        "inst",
        "isdtp",
        "language",
        "locale",
        "nonce",
        "redirect_uri",
        "registrationsubchannel",
        "response_type",
        "returl",
        "scope",
        "sid",
        "startpage",
        "starturl",
        "state",
        "system",
        "un",
    }
)
# Presence vocabulary, PREFIX-matched against the page's VISIBLE text only (script and
# style bodies excluded). Authored words carry no account material, and they are what
# separates a forced password change from a consent wall or a lock-out.
_TEXT_MARKERS = (
    "accept",
    "agree",
    "blocked",
    "captcha",
    "challeng",
    "complet",
    "confirm",
    "consent",
    "continu",
    "disabl",
    "error",
    "expir",
    "inactiv",
    "invalid",
    "locked",
    "maintenance",
    "mandator",
    "migrat",
    "otp",
    "passphrase",
    "password",
    "polic",
    "privacy",
    "profil",
    "registr",
    "renew",
    "requir",
    "reset",
    "session",
    "suspend",
    "terms",
    "unauthor",
    "unavailab",
    "updat",
    "verif",
    "welcome",
)
_JS_REDIRECT_MARKERS = (
    "location.replace",
    "location.assign",
    "location.href",
    "window.location",
)
_PASSWORD_KINDS = frozenset(
    {"password", "new_password", "confirm_password", "current_password"}
)
_MAX_MARKERS = 25
_MAX_SKELETON_NODES = 60
_TEXT_SCAN_CHARS = 40_000
_TITLE_SCAN_CHARS = 200
# Why a token page carried no tokens. Controlled vocabulary: the two ACCOUNT_ACTION
# verdicts are the ones the user can act on, so they escalate to their own error code.
_TOKEN_PAGE_VERDICTS = frozenset(
    {
        "password_change",
        "consent",
        "login",
        "mfa",
        "token_link_unparsed",
        "empty",
        "unknown",
    }
)
ACCOUNT_ACTION_VERDICTS = frozenset({"password_change", "consent"})


def _enum(value: Any, allowed: frozenset[str], fallback: str = "other") -> str:
    candidate = str(value or "").lower()
    return candidate if candidate in allowed else fallback


def _phase(value: Any) -> str:
    return _enum(value, _PHASES)


def _non_negative(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def classify_endpoint(url: Any) -> str:
    """Return a controlled endpoint category without retaining URL material."""
    try:
        parts = urlsplit(str(url or ""))
        host = (parts.hostname or "").lower()
        path = (parts.path or "").lower()
    except (TypeError, ValueError):
        return "other"

    if path.endswith((".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico")):
        return "static_asset"
    if "progressivelogin" in path:
        return "progressive_login"
    if "oauth/done" in path:
        return "oauth_done"
    if path.endswith("/services/oauth2/authorize"):
        return "authorize"
    if path.endswith("/services/oauth2/token"):
        return "token_refresh"
    # Post-login interstitials (issue #67): checked BEFORE the generic login/apex rules
    # so a forced password step is named instead of collapsing into "auth_other".
    if "changepassword" in path or "change_password" in path:
        return "change_password"
    if "setpassword" in path or "set_password" in path:
        return "set_password"
    if "forgotpassword" in path or "resetpassword" in path or "passwordreset" in path:
        return "reset_password"
    if "/s/login/" in path:
        return "login"
    if path.endswith("/s/sfsites/aura"):
        return "aura_login"
    if path.endswith("/apexremote"):
        return "mfa_remoting"
    if path.endswith("/auth/v1/login"):
        return "api_auth"
    if "/secur/" in path or path.endswith("/frontdoor.jsp"):
        return "secur"
    if "/apex/" in path:
        return "apex_page"
    if not host and not path:
        return "none"
    if host == "account2.hon-smarthome.com" or host.endswith(".salesforce.com"):
        return "auth_other"
    if host.endswith(".he.services"):
        return "api_other"
    if host:
        return "external"
    return "other"


def classify_failure_reason(error: BaseException) -> str:
    """Map an exception to a controlled reason without retaining its message."""
    message = str(error).lower()
    # Checked before the token reasons: the account-action failure IS a token-page
    # failure, but the reason must say the account needs a user step (issue #67).
    if "account action required" in message:
        return "account_action"
    if "incomplete token" in message:
        return "incomplete_tokens"
    if "no href" in message:
        return "no_href"
    if "no login url" in message:
        return "no_login_url"
    if "no fwuid" in message:
        return "no_fwuid"
    if "no cognito token" in message:
        return "no_cognito_token"
    if "status " in message:
        return "status"
    if "decode" in message or "json" in message:
        return "decode_error"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, (ConnectionError, OSError)):
        return "network_error"
    return "unexpected"


def _header_values(headers: Any, name: str) -> tuple[str, ...]:
    if headers is None:
        return ()
    getall = getattr(headers, "getall", None)
    if callable(getall):
        try:
            return tuple(str(value) for value in getall(name, []))
        except (TypeError, ValueError):
            return ()
    if isinstance(headers, Mapping):
        values = [
            value
            for key, value in headers.items()
            if str(key).lower() == name.lower()
        ]
        return tuple(str(value) for value in values)
    return ()


def _cookie_kind(name: str) -> str:
    lowered = name.lower()
    if "consent" in lowered:
        return "consent"
    if "browser" in lowered:
        return "browser"
    if lowered in {"oid", "oinfo"} or "org" in lowered:
        return "organization"
    if "inst" in lowered:
        return "instance"
    if "sid" in lowered or "session" in lowered:
        return "session"
    return "other"


@dataclass(frozen=True)
class ResponseSummary:
    status: int
    elapsed_ms: int
    media: str
    charset: str
    bytes: int
    redirects: int
    location_kind: str
    cookie_kinds: tuple[str, ...]
    unknown_cookies: int
    cookie_secure: bool
    cookie_http_only: bool
    cookie_same_site: bool


def summarize_response(
    *,
    status: Any,
    headers: Any,
    body: str | bytes,
    elapsed_ms: Any,
    redirects: Any = 0,
) -> ResponseSummary:
    """Summarize response metadata without retaining body or header values."""
    content_type_values = _header_values(headers, "content-type")
    content_type = content_type_values[0].lower() if content_type_values else ""
    media_candidate = content_type.split(";", 1)[0].strip()
    media = media_candidate if media_candidate in _MEDIA_TYPES else "other"
    charset = "none"
    for part in content_type.split(";")[1:]:
        key, separator, value = part.strip().partition("=")
        if separator and key == "charset":
            candidate = value.strip(" \"'")
            charset = candidate if candidate in _CHARSETS else "other"
            break

    cookie_kinds: list[str] = []
    unknown_cookies = 0
    cookie_secure = False
    cookie_http_only = False
    cookie_same_site = False
    for value in _header_values(headers, "set-cookie"):
        first, _, attributes = value.partition(";")
        name, separator, _cookie_value = first.partition("=")
        kind = _cookie_kind(name.strip()) if separator else "other"
        if kind == "other":
            unknown_cookies += 1
        elif kind not in cookie_kinds:
            cookie_kinds.append(kind)
        lowered_attributes = attributes.lower()
        cookie_secure = cookie_secure or "secure" in lowered_attributes
        cookie_http_only = cookie_http_only or "httponly" in lowered_attributes
        cookie_same_site = cookie_same_site or "samesite" in lowered_attributes

    location_values = _header_values(headers, "location")
    location_kind = (
        classify_endpoint(location_values[0]) if location_values else "none"
    )
    body_bytes = body if isinstance(body, bytes) else str(body).encode("utf-8")
    return ResponseSummary(
        status=_non_negative(status),
        elapsed_ms=_non_negative(elapsed_ms),
        media=media,
        charset=charset,
        bytes=len(body_bytes),
        redirects=_non_negative(redirects),
        location_kind=location_kind,
        cookie_kinds=tuple(sorted(cookie_kinds)),
        unknown_cookies=unknown_cookies,
        cookie_secure=cookie_secure,
        cookie_http_only=cookie_http_only,
        cookie_same_site=cookie_same_site,
    )


def _input_kind(attributes: Mapping[str, str]) -> str:
    """Controlled kind for one input, read from its NAME/ID/TYPE only (never a value).

    Password fields are split into current/new/confirm because that split is what tells
    a login form apart from a forced password change (issue #67). Matching is by
    SUBSTRING for the password/ViewState families: VisualForce prefixes every field
    (``j_id0:theForm:newPassword``), so name equality saw them all as "other".
    """
    name = attributes.get("name", "").lower()
    identifier = attributes.get("id", "").lower()
    input_type = attributes.get("type", "").lower()
    hay = f"{name} {identifier}"
    if input_type == "password" or "password" in hay or "passwd" in hay:
        if any(marker in hay for marker in ("confirm", "verify", "repeat", "again")):
            return "confirm_password"
        if "new" in hay:
            return "new_password"
        if "current" in hay or "old" in hay:
            return "current_password"
        return "password"
    if name in {"username", "email", "emailaddress", "login"} or input_type == "email":
        return "username"
    if name in {"otp", "code", "verificationcode", "emailotp"} or any(
        marker in hay for marker in ("verificationcode", "emailotp")
    ):
        return "otp"
    if name in {"csrf", "viewstate", "token"} or "viewstate" in hay:
        return "security"
    if any(marker in hay for marker in ("consent", "terms", "privacy", "accept")):
        return "consent"
    return "other"


class _ShapeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags = 0
        self.forms = 0
        self.inputs = 0
        self.links = 0
        self.scripts = 0
        self.input_kinds: list[str] = []
        self.normalized: list[str] = []
        # Page-identity extras (issue #67). The captured text is scanned against
        # _TEXT_MARKERS and then dropped: only the presence booleans leave this module.
        self.title_chars: list[str] = []
        self.text_chars: list[str] = []
        self.title_len = 0
        self.text_len = 0
        self.form_action = ""
        self.form_method = ""
        self.buttons = 0
        self.meta_refresh = False
        self._in_title = False
        self._skip_depth = 0
        self._form_seen = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        safe_tag = tag if tag in _ALLOWED_TAGS else "other"
        safe_attrs = sorted(
            {
                name if name in _ALLOWED_ATTRS else "other"
                for name, _value in attrs
            }
        )
        self.normalized.append(f"{safe_tag}[{','.join(safe_attrs)}]")
        self.tags += 1
        # Only these three read the attributes, and the Lightning login page carries
        # thousands of tags: building the mapping for all of them is pure waste.
        mapping = (
            {str(name).lower(): str(value or "") for name, value in attrs}
            if tag in {"form", "input", "meta"}
            else {}
        )
        if tag == "form":
            self.forms += 1
            # FIRST form only: the one the user would have to submit. Keyed off a flag,
            # not off the captured values: a bare <form> would otherwise leave them empty
            # and let a LATER form claim them, naming the wrong form in the trace.
            if not self._form_seen:
                self._form_seen = True
                self.form_action = mapping.get("action", "")
                self.form_method = mapping.get("method", "")
        elif tag == "input":
            self.inputs += 1
            self.input_kinds.append(_input_kind(mapping))
        elif tag in {"a", "link"}:
            self.links += 1
        elif tag == "script":
            self.scripts += 1
        elif tag == "button":
            self.buttons += 1
        elif tag == "meta" and mapping.get("http-equiv", "").lower() == "refresh":
            self.meta_refresh = True
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        # Script/style bodies are NOT text: scanning them would report every vocabulary
        # word the framework happens to ship (the login page alone is 300+ KB of JS).
        if self._skip_depth:
            return
        if self._in_title:
            if self.title_len < _TITLE_SCAN_CHARS:
                self.title_chars.append(data)
                self.title_len += len(data)
            return
        if self.text_len < _TEXT_SCAN_CHARS:
            self.text_chars.append(data)
        self.text_len += len(data)


@dataclass(frozen=True)
class HtmlSummary:
    tags: int
    forms: int
    inputs: int
    links: int
    scripts: int
    input_kinds: tuple[str, ...]
    login: bool
    progressive_login: bool
    otp: bool
    privacy: bool
    oauth_done: bool
    page_kind: str
    dom_fingerprint: str
    parse_error: bool
    password_inputs: int = 0
    password_change: bool = False
    skeleton: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageSummary:
    """Identity of a page, as named structure only (issue #67)."""

    endpoint: str
    path_depth: int
    path_markers: tuple[str, ...]
    unknown_segments: int
    path_hash: str
    query_names: tuple[str, ...]
    unknown_query: int
    title_markers: tuple[str, ...]
    text_markers: tuple[str, ...]
    text_chars: int
    form_action: str
    form_method: str
    buttons: int
    meta_refresh: bool
    js_redirect: bool
    hon_scheme: bool
    oauth_done_hits: int


def _parse(source: str) -> tuple[_ShapeParser, bool]:
    parser = _ShapeParser()
    parse_error = False
    try:
        parser.feed(source)
        parser.close()
    except Exception:  # noqa: BLE001 - diagnostics are observational, never fatal
        parse_error = True
    return parser, parse_error


def _markers(text: str) -> tuple[str, ...]:
    lowered = text.lower()
    return tuple(
        marker for marker in _TEXT_MARKERS if marker in lowered
    )[:_MAX_MARKERS]


def _path_shape(path: str) -> tuple[tuple[str, ...], int, int]:
    """(named segments, unknown segment count, depth) for a URL path."""
    segments = [segment for segment in path.lower().split("/") if segment]
    markers: list[str] = []
    unknown = 0
    for segment in segments:
        base = segment.split(".", 1)[0]
        if base in _PATH_SEGMENTS:
            if base not in markers:
                markers.append(base)
        else:
            unknown += 1
    return tuple(markers), unknown, len(segments)


def _html_summary(
    source: str, parser: _ShapeParser, parse_error: bool
) -> HtmlSummary:
    lowered = source.lower()
    normalized = "|".join(parser.normalized).encode("ascii", "strict")
    fingerprint = hashlib.sha256(normalized).hexdigest()[:12]
    login = "login" in lowered or "username" in parser.input_kinds
    progressive_login = "progressivelogin" in lowered
    otp = (
        "verifyemailotp" in lowered
        or "verificationcode" in lowered
        or "otp" in parser.input_kinds
    )
    privacy = any(
        marker in lowered for marker in ("privacy", "consent", "terms")
    )
    oauth_done = "oauth/done" in lowered
    password_inputs = sum(
        1 for kind in parser.input_kinds if kind in _PASSWORD_KINDS
    )
    # Two or more password boxes is never a login form and never a token page: it is a
    # set/change-password step (new + confirm), so it gets its own kind.
    password_change = password_inputs >= 2
    # Same precedence as classify_token_page: an interstitial can ECHO the OAuth request
    # it interrupted, and page_kind gates the skeleton emission, so ranking oauth_done
    # first would drop the shape of exactly the page that needs identifying.
    if otp:
        page_kind = "mfa"
    elif password_change:
        page_kind = "password_change"
    elif oauth_done:
        page_kind = "oauth_done"
    elif progressive_login:
        page_kind = "progressive_login"
    elif login:
        page_kind = "login"
    elif privacy:
        page_kind = "privacy"
    else:
        page_kind = "other"
    return HtmlSummary(
        tags=parser.tags,
        forms=parser.forms,
        inputs=parser.inputs,
        links=parser.links,
        scripts=parser.scripts,
        input_kinds=tuple(parser.input_kinds),
        login=login,
        progressive_login=progressive_login,
        otp=otp,
        privacy=privacy,
        oauth_done=oauth_done,
        page_kind=page_kind,
        dom_fingerprint=fingerprint,
        parse_error=parse_error,
        password_inputs=password_inputs,
        password_change=password_change,
        skeleton=tuple(parser.normalized[:_MAX_SKELETON_NODES]),
    )


def _page_summary(url: Any, source: str, parser: _ShapeParser) -> PageSummary:
    try:
        parts = urlsplit(str(url or ""))
        path = parts.path or ""
        query = parts.query or ""
    except (TypeError, ValueError):
        path, query = "", ""
    path_markers, unknown_segments, depth = _path_shape(path)
    query_names: list[str] = []
    unknown_query = 0
    for field in query.split("&"):
        if not field:
            continue
        name = field.partition("=")[0].strip().lower()
        if name in _QUERY_NAMES:
            if name not in query_names:
                query_names.append(name)
        else:
            unknown_query += 1
    lowered = source.lower()
    return PageSummary(
        endpoint=classify_endpoint(url),
        path_depth=depth,
        path_markers=path_markers,
        unknown_segments=unknown_segments,
        # Hash of the PATH only (never the query): two reports of the same landing page
        # are comparable, and a known-good login can be diffed against a broken one.
        path_hash=(
            hashlib.sha256(path.lower().encode("utf-8", "replace")).hexdigest()[:12]
            if path
            else "none"
        ),
        query_names=tuple(query_names),
        unknown_query=unknown_query,
        title_markers=_markers("".join(parser.title_chars)),
        text_markers=_markers("".join(parser.text_chars)),
        text_chars=parser.text_len,
        form_action=(
            classify_endpoint(parser.form_action) if parser.form_action else "none"
        ),
        form_method=_enum(parser.form_method, frozenset({"get", "post"}), "none"),
        buttons=parser.buttons,
        meta_refresh=parser.meta_refresh,
        js_redirect=any(marker in lowered for marker in _JS_REDIRECT_MARKERS),
        # The token redirect leaves fingerprints even when the tokens are absent: if
        # these are set the page DID carry the hand-off and our parsing is at fault,
        # which is the opposite diagnosis from a server-side interstitial.
        hon_scheme="hon://" in lowered,
        oauth_done_hits=min(lowered.count("oauth/done"), 99),
    )


def analyze_page(url: Any, text: Any) -> tuple[HtmlSummary, PageSummary]:
    """Both summaries from a SINGLE parse. Never raises (diagnostics are observational)."""
    source = str(text or "")
    try:
        parser, parse_error = _parse(source)
        return (
            _html_summary(source, parser, parse_error),
            _page_summary(url, source, parser),
        )
    except Exception:  # noqa: BLE001 - diagnostics are observational, never fatal
        empty = _ShapeParser()
        return _html_summary("", empty, True), _page_summary("", "", empty)


def summarize_html(text: Any) -> HtmlSummary:
    """Summarize recognized HTML structure and boolean protocol markers."""
    return analyze_page("", text)[0]


def summarize_page(url: Any, text: Any) -> PageSummary:
    """Summarize the identity of a page (path/query/text shape), never its content."""
    return analyze_page(url, text)[1]


def classify_token_page(html: HtmlSummary, page: PageSummary) -> str:
    """Why a token page carried no tokens, as a controlled verdict (issue #67).

    Structural, not textual: a page that carries the OAuth hand-off never holds a
    password form, so ``password_change``/``consent`` identify a server-side step the
    USER must complete, while ``token_link_unparsed`` accuses our own parser instead.
    """
    if html.otp:
        return "mfa"
    # The password form is checked FIRST: an interstitial can ECHO the OAuth request it
    # interrupted (the redirect_uri and the escaped oauth/done link show up in the
    # markup), and a page holding two password boxes is an account step no matter what
    # it quotes. Only a page with no such form gets to blame our parser.
    if html.password_inputs >= 2:
        return "password_change"
    if html.oauth_done or page.hon_scheme:
        return "token_link_unparsed"
    if html.tags == 0:
        return "empty"
    if html.password_inputs == 1 and "username" in html.input_kinds:
        return "login"
    if html.forms and any(
        marker in page.text_markers
        for marker in ("consent", "terms", "accept", "agree", "polic")
    ):
        return "consent"
    return "unknown"


@dataclass(frozen=True)
class LinksSummary:
    count: int
    kinds: tuple[str, ...]
    selected_index: int
    selected_kind: str


def summarize_links(hrefs: Sequence[Any], selected_index: Any = -1) -> LinksSummary:
    """Classify link destinations while discarding their raw targets."""
    kinds = tuple(classify_endpoint(value) for value in hrefs)
    try:
        index = int(selected_index)
    except (TypeError, ValueError, OverflowError):
        index = -1
    selected_kind = kinds[index] if 0 <= index < len(kinds) else "none"
    return LinksSummary(
        count=len(kinds),
        kinds=kinds,
        selected_index=index,
        selected_kind=selected_kind,
    )


@dataclass(frozen=True)
class JsonSummary:
    keys: tuple[str, ...]
    unknown_keys: int
    objects: int
    arrays: int
    scalars: int
    max_depth: int


def summarize_json(value: Any) -> JsonSummary:
    """Summarize JSON containers and allowlisted keys, never values."""
    keys: set[str] = set()
    unknown_keys = 0
    objects = 0
    arrays = 0
    scalars = 0
    max_depth = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal unknown_keys, objects, arrays, scalars, max_depth
        max_depth = max(max_depth, depth)
        if isinstance(item, Mapping):
            objects += 1
            for key, child in item.items():
                normalized = str(key).lower()
                if normalized in _ALLOWED_JSON_KEYS:
                    keys.add(normalized)
                else:
                    unknown_keys += 1
                visit(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            arrays += 1
            for child in item:
                visit(child, depth + 1)
        else:
            scalars += 1

    visit(value, 0)
    return JsonSummary(
        keys=tuple(sorted(keys)),
        unknown_keys=unknown_keys,
        objects=objects,
        arrays=arrays,
        scalars=scalars,
        max_depth=max_depth,
    )


@dataclass(frozen=True)
class TokenSummary:
    present: tuple[str, ...]
    missing: tuple[str, ...]
    duplicates: tuple[str, ...]
    html_escaped: bool
    complete: bool


def summarize_tokens(text: Any) -> TokenSummary:
    """Report OAuth field structure without retaining any token value."""
    source = str(text or "")
    counts = {field: 0 for field in _TOKEN_FIELDS}
    for match in _TOKEN_RE.finditer(source):
        counts[match.group("key")] += 1
    present = tuple(field for field in _TOKEN_FIELDS if counts[field])
    missing = tuple(field for field in _TOKEN_FIELDS if not counts[field])
    duplicates = tuple(field for field in _TOKEN_FIELDS if counts[field] > 1)
    return TokenSummary(
        present=present,
        missing=missing,
        duplicates=duplicates,
        html_escaped="&amp;" in source,
        complete=not missing,
    )


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "none"
    if isinstance(value, tuple):
        return ",".join(_format_value(item) for item in value) or "none"
    return str(value)


class AuthDiagnosticTrace:
    """Thread-safe bounded buffer containing only controlled diagnostic fields."""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = bool(enabled)
        self.trace_id = secrets.token_hex(4) if self.enabled else ""
        self._lock = threading.Lock()
        self._events: list[tuple[int, str]] = []
        self._serialized_bytes = 0
        self._seen = 0
        self._dropped = 0
        self._finalized = False

    def _append(self, event: str, fields: Sequence[tuple[str, Any]]) -> None:
        if not self.enabled:
            return
        try:
            payload = " ".join(
                [f"event={event}"]
                + [f"{name}={_format_value(value)}" for name, value in fields]
            )
            size = len(payload.encode("utf-8"))
            with self._lock:
                if self._finalized:
                    return
                self._seen += 1
                if (
                    len(self._events) >= _MAX_EVENTS
                    or self._serialized_bytes + size > _MAX_BYTES
                ):
                    self._dropped += 1
                    return
                self._events.append((self._seen, payload))
                self._serialized_bytes += size
        except Exception:
            # Diagnostics are observational and must never affect authentication.
            return

    def request(self, phase: Any, method: Any, endpoint: Any) -> None:
        self._append(
            "request",
            (
                ("phase", _phase(phase)),
                ("method", str(method or "").upper() if str(method or "").upper() in _METHODS else "other"),
                ("endpoint", _enum(endpoint, _ENDPOINTS)),
            ),
        )

    def response(self, phase: Any, summary: ResponseSummary) -> None:
        self._append(
            "response",
            (
                ("phase", _phase(phase)),
                ("status", summary.status),
                ("elapsed_ms", summary.elapsed_ms),
                ("media", summary.media),
                ("charset", summary.charset),
                ("bytes", summary.bytes),
                ("redirects", summary.redirects),
                ("location_kind", summary.location_kind),
                ("cookie_kinds", summary.cookie_kinds),
                ("unknown_cookies", summary.unknown_cookies),
                ("cookie_secure", summary.cookie_secure),
                ("cookie_http_only", summary.cookie_http_only),
                ("cookie_same_site", summary.cookie_same_site),
            ),
        )

    def html(self, phase: Any, summary: HtmlSummary) -> None:
        self._append(
            "html",
            (
                ("phase", _phase(phase)),
                ("tags", summary.tags),
                ("forms", summary.forms),
                ("inputs", summary.inputs),
                ("links", summary.links),
                ("scripts", summary.scripts),
                ("input_kinds", summary.input_kinds),
                ("login", summary.login),
                ("progressive_login", summary.progressive_login),
                ("otp", summary.otp),
                ("privacy", summary.privacy),
                ("oauth_done", summary.oauth_done),
                ("page_kind", summary.page_kind),
                ("dom", summary.dom_fingerprint),
                ("parse_error", summary.parse_error),
            ),
        )

    def page(self, phase: Any, summary: PageSummary) -> None:
        """Emit the named identity of a page (issue #67)."""
        self._append(
            "page",
            (
                ("phase", _phase(phase)),
                ("endpoint", _enum(summary.endpoint, _ENDPOINTS)),
                ("path_depth", summary.path_depth),
                ("path_markers", summary.path_markers),
                ("unknown_segments", summary.unknown_segments),
                ("path_hash", summary.path_hash),
                ("query_names", summary.query_names),
                ("unknown_query", summary.unknown_query),
                ("title_markers", summary.title_markers),
                ("text_markers", summary.text_markers),
                ("text_chars", summary.text_chars),
                ("form_action", _enum(summary.form_action, _ENDPOINTS)),
                ("form_method", summary.form_method),
                ("buttons", summary.buttons),
                ("meta_refresh", summary.meta_refresh),
                ("js_redirect", summary.js_redirect),
                ("hon_scheme", summary.hon_scheme),
                ("oauth_done_hits", summary.oauth_done_hits),
            ),
        )

    def skeleton(self, phase: Any, summary: HtmlSummary) -> None:
        """Emit the bounded tag/attribute-NAME skeleton of an unexpected page.

        Tags and attribute names are allowlisted upstream (anything else is already
        "other") and no value is ever included, so the shape identifies the page
        without shipping its markup."""
        self._append(
            "skeleton",
            (
                ("phase", _phase(phase)),
                ("nodes", len(summary.skeleton)),
                ("shape", "|".join(summary.skeleton) or "none"),
            ),
        )

    def verdict(self, phase: Any, verdict: Any) -> None:
        """Emit why a token page carried no tokens (controlled vocabulary)."""
        self._append(
            "verdict",
            (
                ("phase", _phase(phase)),
                ("page", _enum(verdict, _TOKEN_PAGE_VERDICTS, "unknown")),
            ),
        )

    def links(self, phase: Any, summary: LinksSummary) -> None:
        self._append(
            "links",
            (
                ("phase", _phase(phase)),
                ("count", summary.count),
                ("kinds", summary.kinds),
                ("selected_index", summary.selected_index),
                ("selected_kind", summary.selected_kind),
            ),
        )

    def json_shape(self, phase: Any, summary: JsonSummary) -> None:
        self._append(
            "json",
            (
                ("phase", _phase(phase)),
                ("keys", summary.keys),
                ("unknown_keys", summary.unknown_keys),
                ("objects", summary.objects),
                ("arrays", summary.arrays),
                ("scalars", summary.scalars),
                ("max_depth", summary.max_depth),
            ),
        )

    def token_shape(self, phase: Any, summary: TokenSummary) -> None:
        self._append(
            "tokens",
            (
                ("phase", _phase(phase)),
                ("present", summary.present),
                ("missing", summary.missing),
                ("duplicates", summary.duplicates),
                ("html_escaped", summary.html_escaped),
                ("complete", summary.complete),
            ),
        )

    def payload(self, phase: Any, kind: Any) -> None:
        safe_kind = str(kind or "").lower()
        fields = _PAYLOAD_FIELDS.get(safe_kind)
        if fields is None:
            safe_kind = "other"
            fields = ()
        self._append(
            "payload",
            (
                ("phase", _phase(phase)),
                ("kind", safe_kind),
                ("fields", fields),
            ),
        )

    def phase(
        self, phase: Any, *, status: Any = None, outcome: Any = "other"
    ) -> None:
        self._append(
            "phase",
            (
                ("phase", _phase(phase)),
                ("status", _non_negative(status) if status is not None else "none"),
                ("outcome", _enum(outcome, frozenset({"start", "success", "failed", "paused", "other"}))),
            ),
        )

    def discard(self) -> None:
        with self._lock:
            self._events.clear()
            self._serialized_bytes = 0
            self._finalized = True

    def flush(
        self,
        logger: Any,
        *,
        code: Any,
        phase: Any,
        reason: Any,
    ) -> None:
        """Emit the buffered safe records once; never propagate diagnostic errors."""
        if not self.enabled:
            return
        try:
            with self._lock:
                if self._finalized:
                    return
                self._finalized = True
                events = tuple(self._events)
                dropped = self._dropped
                terminal_seq = self._seen + 2 if dropped else self._seen + 1
                self._events.clear()
            prefix = f"[ADDHON-AUTH trace={self.trace_id}"
            for sequence, payload in events:
                logger.warning("%s", f"{prefix} seq={sequence:02d}] {payload}")
            if dropped:
                logger.warning(
                    "%s",
                    (
                        f"{prefix} seq={self._seen + 1:02d}] event=truncated "
                        f"truncated=true dropped_events={dropped}"
                    ),
                )
            safe_code = str(code) if _CODE_RE.fullmatch(str(code or "")) else "ADDHON-000"
            safe_reason = _enum(reason, _REASONS)
            logger.warning(
                "%s",
                (
                    f"{prefix} seq={terminal_seq:02d}] event=failed "
                    f"code={safe_code} phase={_phase(phase)} reason={safe_reason}"
                ),
            )
        except Exception:
            # Logging failures must not replace the authentication exception.
            return
