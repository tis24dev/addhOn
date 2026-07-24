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
    if "/s/login/" in path:
        return "login"
    if path.endswith("/s/sfsites/aura"):
        return "aura_login"
    if path.endswith("/apexremote"):
        return "mfa_remoting"
    if path.endswith("/auth/v1/login"):
        return "api_auth"
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
    name = attributes.get("name", "").lower()
    input_type = attributes.get("type", "").lower()
    if name in {"username", "email", "emailaddress", "login"} or input_type == "email":
        return "username"
    if name in {"password", "passwd"} or input_type == "password":
        return "password"
    if name in {"otp", "code", "verificationcode", "emailotp"}:
        return "otp"
    if name in {"csrf", "viewstate", "token"}:
        return "security"
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
        if tag == "form":
            self.forms += 1
        elif tag == "input":
            self.inputs += 1
            self.input_kinds.append(
                _input_kind({str(name): str(value or "") for name, value in attrs})
            )
        elif tag in {"a", "link"}:
            self.links += 1
        elif tag == "script":
            self.scripts += 1


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


def summarize_html(text: Any) -> HtmlSummary:
    """Summarize recognized HTML structure and boolean protocol markers."""
    source = str(text or "")
    parser = _ShapeParser()
    parse_error = False
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        parse_error = True
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
    if oauth_done:
        page_kind = "oauth_done"
    elif otp:
        page_kind = "mfa"
    elif privacy:
        page_kind = "privacy"
    elif progressive_login:
        page_kind = "progressive_login"
    elif login:
        page_kind = "login"
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
    )


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
