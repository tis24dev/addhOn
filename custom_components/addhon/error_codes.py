# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stable error-code catalog for addhOn.

Every setup/connection failure is mapped to a stable ``ADDHON-NNN`` code plus a
short English reason. The code is shown in the config-flow UI and written to the
logs and the downloadable diagnostics, so a user reporting a problem can quote a
single stable token (e.g. ``ADDHON-320``) that pins the exact failure, and the
catalog can be extended over time without renumbering (codes are append-only and
their meaning is permanent).

Pure module: NO Home Assistant / aiohttp / awscrt import, so the transport,
client, config-flow and diagnostics layers can all import it without a cycle
(mirrors ``debug_utils``). ``classify`` reuses the existing routing predicates in
``hon_client`` (lazy import) so the catalog never diverges from the
auth-vs-retryable classification the rest of the integration already relies on.

The UI text (English AND Italian) lives in ``translations/{en,it}.json`` under
``config.error.<slug>`` (Italian must stay out of the code, enforced by
``tests/test_code_is_english.py``). ``reason_en`` here is used for the logs and
diagnostics only.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import errno as _errno
import json
import re
import socket
import ssl
from dataclasses import dataclass

CODE_PREFIX = "ADDHON"

# Retry-relevant HTTP status tokens must be standalone numbers. Besides avoiding
# false positives such as model/identifier H500 or 5000, this covers rate limiting
# (429) and the complete 5xx range instead of the previous hand-picked subset.
_HTTP_STATUS_RE = re.compile(r"(?<![A-Za-z0-9])(429|5\d{2})(?!\d)")


def _http_statuses(text: str) -> set[int]:
    """Return standalone retry-relevant HTTP status numbers found in *text*."""
    return {int(match.group(1)) for match in _HTTP_STATUS_RE.finditer(text)}


def is_rate_limited_text(text: str) -> bool:
    """Whether an error message represents HTTP rate limiting."""
    text = text.lower()
    return 429 in _http_statuses(text) or "too many requests" in text


def is_server_failure_text(text: str) -> bool:
    """Whether an error message represents a retryable server-side failure."""
    text = text.lower()
    return any(500 <= status <= 599 for status in _http_statuses(text)) or any(
        marker in text
        for marker in (
            "internal server error",
            "server error",
            "bad gateway",
            "gateway timeout",
            "temporarily unavailable",
        )
    )


@dataclass(frozen=True)
class HonErrorCode:
    """One catalog entry: a stable number + a routing/UI flavour.

    - ``requires_reauth`` mirrors ``hon_client._requires_reauth`` intent (auth
      codes -> reauth/InvalidAuth, everything else -> cannot_connect/retry).
    - ``ui`` marks the codes that can surface in the config-flow form and so need
      a localized ``config.error.<slug>`` string; runtime-only codes (MQTT/AWS,
      per-appliance) are logged with ``reason_en`` and never reach the form.
    """

    code: int
    slug: str
    reason_en: str
    requires_reauth: bool = False
    ui: bool = True

    @property
    def label(self) -> str:
        return f"{CODE_PREFIX}-{self.code}"

    def __str__(self) -> str:
        return f"{self.label}: {self.reason_en}"


_BY_SLUG: dict[str, HonErrorCode] = {}
_BY_CODE: dict[int, HonErrorCode] = {}


def _reg(
    code: int, slug: str, reason_en: str, requires_reauth: bool = False, ui: bool = True
) -> HonErrorCode:
    entry = HonErrorCode(code, slug, reason_en, requires_reauth, ui)
    if code in _BY_CODE:
        raise ValueError(f"duplicate error code {code}")
    if slug in _BY_SLUG:
        raise ValueError(f"duplicate error slug {slug!r}")
    _BY_CODE[code] = entry
    _BY_SLUG[slug] = entry
    return entry


# --- The catalog (append-only; never reuse or renumber a code) ---------------
# 1xx - credentials / auth steps (reauth)
INVALID_CREDENTIALS = _reg(100, "invalid_credentials", "Invalid email or password", True)
AUTH_INTRODUCE = _reg(110, "auth_introduce", "Login handshake failed", True)
AUTH_LOGIN = _reg(120, "auth_login", "Login rejected (check email or password)", True)
AUTH_GET_TOKEN = _reg(130, "auth_get_token", "Token retrieval failed", True)
AUTH_API_AUTH = _reg(140, "auth_api_auth", "hOn API authorization failed", True)
AUTH_REFRESH = _reg(150, "auth_refresh", "Session refresh failed", True, ui=False)
# 16x - two-factor / OTP (reauth; shown in the config-flow 2FA step). Append-only:
# 162-168 are reserved for future MFA flavours (expired/too-many/channel/cooldown/
# unsupported); only the codes the flow can actually distinguish are registered (the
# server returns a single boolean for verifyEmailOTP, so wrong vs expired are one code).
MFA_REQUIRED = _reg(160, "mfa_required", "Two-factor verification code required", True)
MFA_CODE_INVALID = _reg(161, "mfa_code_invalid", "Verification code was rejected", True)
# Distinguishable 2FA sub-failures (so the user sees "couldn't send" vs "wrong code" vs
# "server hiccup"). 162/163 are NOT reauth: the credentials and OTP are fine, it's a
# transient send/verify problem -> cannot_connect/retry. 164 IS reauth (re-drive login).
MFA_SEND_FAILED = _reg(162, "mfa_send_failed", "Could not send the verification code", False)
MFA_SERVICE_ERROR = _reg(163, "mfa_service_error", "Two-factor verification service error", False)
MFA_TOKEN_AFTER_VERIFY_FAILED = _reg(
    164, "mfa_token_after_verify_failed", "Sign-in could not finish after verification", True
)
# 165 was reserved for "account action required" while the only available marker was
# textual (privacy words appear in EVERY ProgressiveLogin page, so they proved nothing).
# It is registered now that the detection is STRUCTURAL: the page that should carry the
# OAuth hand-off instead carries a set/change-password form or a consent form, which a
# token page never does (issue #67). 166-168 stay reserved.
ACCOUNT_ACTION_REQUIRED = _reg(
    165,
    "account_action_required",
    "hOn is asking for an extra step on the account before sign-in can finish",
    True,
)
# 2xx - appliance inventory / per-appliance (runtime, logged only)
APPLIANCE_LIST_FAILED = _reg(200, "appliance_list_failed", "Could not fetch the appliance list")
APPLIANCE_LIST_EMPTY = _reg(210, "appliance_list_empty", "No appliances on this account", ui=False)
APPLIANCE_LOAD_FAILED = _reg(220, "appliance_load_failed", "Could not load appliance data", ui=False)
APPLIANCE_DATA_MALFORMED = _reg(
    230, "appliance_data_malformed", "Malformed appliance data", ui=False
)
# 3xx - realtime / AWS IoT (runtime only; validation never starts MQTT)
AWS_TOKEN_FAILED = _reg(300, "aws_token_failed", "AWS IoT token request failed", ui=False)
MQTT_CONNECT_TIMEOUT = _reg(310, "mqtt_connect_timeout", "MQTT connect timeout", ui=False)
MQTT_SUBSCRIBE_TIMEOUT = _reg(320, "mqtt_subscribe_timeout", "MQTT subscribe timeout", ui=False)
# 4xx - network / transport
NETWORK_TIMEOUT = _reg(400, "network_timeout", "Network timeout contacting hOn")
DNS_FAILURE = _reg(410, "dns_failure", "DNS resolution failed")
TLS_FAILURE = _reg(420, "tls_failure", "TLS or certificate error")
CONNECTION_REFUSED = _reg(
    430, "connection_refused", "Could not connect to hOn (refused, reset or unreachable)"
)
RATE_LIMITED = _reg(440, "rate_limited", "Rate limited by hOn (try again later)")
SERVER_ERROR = _reg(450, "server_error", "hOn server error")
LOOP_TIMEOUT = _reg(460, "loop_timeout", "Setup timed out")
DECODE_ERROR = _reg(470, "decode_error", "Unreadable server response")
# 405/406: a sign-in that runs out of time is NOT a rejection (requires_reauth=False).
# The login is lazy -- it starts inside the request that load_appliances issues -- so
# before these existed a slow sign-in was reported as ADDHON-400 "network timeout"
# (issue #76) or fell through to the mute ADDHON-460. The reason_en MUST keep the word
# "timeout": hon_client._is_retryable_server_error looks for that substring, which is
# what keeps a phase-timeout code retryable instead of reauth. 401/402 were deliberately
# NOT used: hon_client._is_auth_error matches the bare substrings "401"/"403", so a
# message that lost its carried code would turn a transient timeout into a reauth.
AUTH_TIMEOUT = _reg(405, "auth_timeout", "Timeout during hOn sign-in")
REFRESH_TIMEOUT = _reg(406, "refresh_timeout", "Timeout refreshing the hOn session")
# 480: the dedicated loop was torn down while a call was still in flight (an unload or
# a reload racing a poll/command). `HonClient._run_on_hon_loop` no longer waits under
# the lifecycle lock -- that wait made an unload queue behind the slowest in-flight
# call, minutes with the per-site caps -- so the teardown can now cancel the task under
# a waiter. What reaches the waiter is a bare, message-less
# concurrent.futures.CancelledError, which classify can only call ADDHON-999: the
# user's command fails with "Unknown error" and nothing says the client was shutting
# down. NOT reauth and NOT "timeout"-flavoured: nothing timed out and nothing was
# rejected, the call was simply abandoned, so the coordinator files a plain transient
# failure while the entry goes away.
CLIENT_SHUTDOWN = _reg(
    480, "client_shutdown", "hOn client shut down while the request was running", ui=False
)
# 9xx - fallback
UNKNOWN = _reg(999, "unknown", "Unknown error")


def all_codes() -> tuple[HonErrorCode, ...]:
    return tuple(_BY_CODE.values())


def by_slug(slug: str) -> HonErrorCode | None:
    return _BY_SLUG.get(slug)


class HonCodedError(Exception):
    """An exception that carries a :class:`HonErrorCode`.

    Raised at the points where the original exception would otherwise be opaque
    (the message-less 60s loop timeout, an MQTT subscribe timeout). ``classify``
    returns the carried code verbatim, and ``hon_client._requires_reauth`` reads
    ``.error_code.requires_reauth`` so the routing stays correct. The message must
    NEVER contain device identity (only the code/reason/phase)."""

    def __init__(
        self, error_code: HonErrorCode, message: str = "", *, phase: str | None = None
    ) -> None:
        self.error_code = error_code
        self.phase = phase
        super().__init__(message or str(error_code))


# Map a setup PHASE to the timeout-flavoured code used when that phase stalls
# (the 60s loop cap, or a per-request aiohttp timeout). Only timeout-named codes
# are used here so hon_client._is_retryable_server_error (string "timeout") keeps
# returning True -> the failure is retried, never mistaken for a reauth.
_PHASE_TIMEOUT: dict[str, HonErrorCode] = {
    "mqtt_subscribe": MQTT_SUBSCRIBE_TIMEOUT,
    "mqtt_connect": MQTT_CONNECT_TIMEOUT,
    "aws_token": MQTT_CONNECT_TIMEOUT,
    "load_appliances": NETWORK_TIMEOUT,
    "load_appliance": NETWORK_TIMEOUT,
    "connect": NETWORK_TIMEOUT,
}

# Phases are now HIERARCHICAL ("load_appliances/auth/refresh", client/phase.py), so a
# lazy sign-in nested inside the appliance-list request can name itself instead of
# borrowing the caller's label. Per-SEGMENT table, scanned innermost-first: the deepest
# step that we recognise is the one that actually stalled.
_PHASE_TIMEOUT_SEGMENT: dict[str, HonErrorCode] = {
    **_PHASE_TIMEOUT,
    "auth": AUTH_TIMEOUT,
    "refresh": REFRESH_TIMEOUT,
    "subscribe": MQTT_SUBSCRIBE_TIMEOUT,
    # The scope `NativeHon.setup()` opens around the MQTT start (its budget is
    # MQTT_START, summed into SETUP_CAP). Coded as a CONNECT timeout because that is
    # what the budget mostly pays for; the finer mqtt_connect/mqtt_subscribe split
    # survives on the cross-thread mirror, which the MQTT layer keeps refining while
    # the scope is open (transport/mqtt.py::_set_setup_phase).
    "mqtt_start": MQTT_CONNECT_TIMEOUT,
}

# Everything `phase_timeout_code` can return. `client/phase.py` needs it because a
# budget expiry reaches its scope ALREADY converted into a HonCodedError (the
# conversion has to happen inside the phase scope to name the innermost step), so
# without this set the ledger would file every expiry as a plain 'error'.
PHASE_TIMEOUT_CODES = frozenset({*_PHASE_TIMEOUT_SEGMENT.values(), LOOP_TIMEOUT})


def phase_timeout_code(phase: str | None) -> HonErrorCode:
    """Timeout code for a stalled setup phase (empty/unknown -> LOOP_TIMEOUT).

    Resolution, innermost wins: the exact flat name first (total backward
    compatibility with the phases the MQTT layer and the legacy mirrors still
    write), then each '/'-separated segment from the leaf outwards, then the
    ``load_appliance*`` prefix rule, then LOOP_TIMEOUT.
    """
    if not phase:
        return LOOP_TIMEOUT
    exact = _PHASE_TIMEOUT.get(phase)
    if exact is not None:
        return exact
    for segment in reversed(phase.split("/")):
        code = _PHASE_TIMEOUT_SEGMENT.get(segment)
        if code is not None:
            return code
    if phase.startswith("load_appliance"):
        return NETWORK_TIMEOUT
    return LOOP_TIMEOUT


# A HonCodedError renders as "ADDHON-400: reason" all by itself (see __str__), so a log
# line that prepends the label again reads "Validation failed [ADDHON-400]: ADDHON-400:
# Network timeout contacting hOn" -- the doubled string reported in #76, which also
# occupies the room the useful detail would need. Anchored at the START only: a message
# that merely cites a code mid-sentence is left alone.
_CODE_PREFIX_RE = re.compile(rf"^{CODE_PREFIX}-\d+:\s*")


def error_detail(err: BaseException) -> str:
    """`str(err)` without a leading ``ADDHON-NNN:`` prefix (never empty)."""
    return _CODE_PREFIX_RE.sub("", str(err), count=1).strip() or type(err).__name__


def _is_timeout(err: BaseException) -> bool:
    return isinstance(
        err, (asyncio.TimeoutError, concurrent.futures.TimeoutError, TimeoutError)
    )


# Errnos that all mean "the peer/route did not accept the connection". Kept separate
# from the OSError subclasses because a raw OSError carries only the number.
_REFUSED_ERRNOS = frozenset(
    {
        _errno.ECONNREFUSED,
        _errno.ECONNRESET,
        _errno.ECONNABORTED,
        _errno.EHOSTUNREACH,
        _errno.ENETUNREACH,
        _errno.ENETDOWN,
        _errno.EPIPE,
    }
)


def _structural_transport_code(err: BaseException) -> HonErrorCode | None:
    """TLS vs DNS vs refused decided by TYPE/errno, not by the message text.

    aiohttp cannot be imported here (pure module, and it is not even installed in the
    offline test environment), so this uses the stdlib types the aiohttp errors DERIVE
    from -- `ssl.SSLError` covers ClientConnectorCertificateError/SSLError -- plus the
    documented `.os_error` attribute that `ClientConnectorError` exposes for the
    underlying OSError. That is what tells a name-resolution failure apart from a
    refusal without reading "Name or service not known" out of a string.
    """
    candidate: BaseException | None = err
    # `.os_error` nests at most one level in aiohttp; the bound is defensive.
    for _ in range(3):
        if candidate is None:
            return None
        if isinstance(candidate, ssl.SSLError):
            return TLS_FAILURE
        if isinstance(candidate, socket.gaierror):
            return DNS_FAILURE
        if isinstance(
            candidate,
            (
                ConnectionRefusedError,
                ConnectionResetError,
                ConnectionAbortedError,
                BrokenPipeError,
            ),
        ):
            return CONNECTION_REFUSED
        if getattr(candidate, "errno", None) in _REFUSED_ERRNOS:
            return CONNECTION_REFUSED
        nested = getattr(candidate, "os_error", None)
        candidate = nested if isinstance(nested, BaseException) else None
    return None


def classify(err: BaseException, *, phase: str | None = None) -> HonErrorCode:
    """Map any exception to a stable :class:`HonErrorCode`.

    Order is most-specific-first. A carried code wins; then rate-limit/5xx (which
    must beat the auth-named-class rule, like the existing classifier); then
    timeouts (attributed to ``phase`` when known); then explicit auth rejections,
    TLS/DNS/refused and native auth-step messages; finally broad transport types
    and the coarse buckets via the existing ``hon_client`` predicates.
    """
    code = getattr(err, "error_code", None)
    if isinstance(code, HonErrorCode):
        return code

    # Decode types are unambiguous even when their ordinary messages do not contain
    # "decode error". Broad connection types are deliberately handled later: their
    # messages may still carry a more-specific 5xx or explicit auth rejection.
    if isinstance(err, (json.JSONDecodeError, UnicodeDecodeError)):
        return DECODE_ERROR

    # A RECEIVED HTTP response carries its status as a field (aiohttp's
    # ClientResponseError, raised by our raise_for_status() call sites). Reading it
    # beats grepping the message for a number. Any other value FALLS THROUGH on
    # purpose: a ContentTypeError with status 200 must stay a decode problem, and an
    # exception of ours that happens to own a `status` attribute must not be
    # reclassified by accident. 401/403 do not return here -- they feed the existing
    # rejection cascade below, which also names the auth STEP.
    status = getattr(err, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        if status == 429:
            return RATE_LIMITED
        if 500 <= status <= 599:
            return SERVER_ERROR

    name = type(err).__name__.lower()
    class_names = " ".join(cls.__name__.lower() for cls in type(err).__mro__)
    text = str(err).lower()
    hay = f"{text} {name}"

    if is_rate_limited_text(hay):
        return RATE_LIMITED
    if is_server_failure_text(hay):
        return SERVER_ERROR
    if _is_timeout(err) or "timed out" in hay or "timeout" in hay:
        return phase_timeout_code(phase)

    # A transport-shaped exception can wrap a real HTTP/token rejection. Detect only
    # explicit rejection markers here (not a bare "auth"/"token", which may merely be
    # an endpoint name) so bad credentials propagate to HA reauth instead of being
    # converted to MQTT polling-only retries. Retryable 429/5xx/timeouts above retain
    # priority, including NativeAuthError("api_auth: status 503").
    auth_rejected = (
        status in (401, 403)
        or "unauthorized" in hay
        or any(
            marker in hay
            for marker in (
                "http 401",
                "http 403",
                "status 401",
                "status 403",
                "status=401",
                "status=403",
                "token rejected",
                "rejected token",
                "invalid token",
                "token invalid",
                "expired token",
                "token expired",
                "invalid credential",
                "credential rejected",
            )
        )
    )
    if auth_rejected:
        if "api_auth" in hay:
            return AUTH_API_AUTH
        if "get_token" in hay or "token page" in hay or "progressive" in hay:
            return AUTH_GET_TOKEN
        if "login page" in hay or "introduce" in hay or "no fwuid" in hay:
            return AUTH_INTRODUCE
        if "login:" in hay or "can't login" in hay or "login failed" in hay:
            return AUTH_LOGIN
        return INVALID_CREDENTIALS

    # Structural transport (TYPE/errno) BEFORE the textual TLS/DNS/refused rules, so a
    # real ssl.SSLError or a gaierror wrapped in ClientConnectorError.os_error is named
    # correctly even when its message says nothing recognisable. Deliberately AFTER
    # rate-limit/5xx, timeouts and the explicit rejection markers: those decide WHETHER
    # this is a transport fault at all, this only decides WHICH one.
    structural = _structural_transport_code(err)
    if structural is not None:
        return structural

    # TLS/certificate: key off the exception CLASS NAME or explicit certificate text,
    # NOT a bare "ssl" in the message. aiohttp's ClientConnectorError __str__ ALWAYS
    # contains "ssl:default" for ANY HTTPS connect failure (a plain outage, not a TLS
    # problem), so matching bare "ssl" would mislabel the most common failure mode.
    if (
        "certificate" in hay
        or "ssl" in name
        or "sslcertverification" in text
        or "certificate verify failed" in text
    ):
        return TLS_FAILURE
    if (
        "getaddrinfo" in hay
        or "name or service not known" in hay
        or "name resolution" in hay
        or "nodename nor servname" in hay
    ):
        return DNS_FAILURE
    if (
        "refused" in hay
        or "connection reset" in hay
        or "reset by peer" in hay
        or "cannot connect to host" in hay
        or "network is unreachable" in text
    ):
        return CONNECTION_REFUSED
    if "clientpayloaderror" in class_names or "contenttypeerror" in class_names:
        return DECODE_ERROR
    if "decode error" in hay:
        return DECODE_ERROR
    # Before the token-page rule: this IS a token-page failure, but the account needs a
    # user step, so it must not collapse into the mute ADDHON-130 (issue #67). The
    # carried code already wins above; this keeps the routing right if the marker
    # reaches the classifier as plain text.
    if "account action required" in hay:
        return ACCOUNT_ACTION_REQUIRED
    if "api_auth" in hay:
        return AUTH_API_AUTH
    if "get_token" in hay or "token page" in hay or "progressive" in hay:
        return AUTH_GET_TOKEN
    if "login page" in hay or "introduce" in hay or "no fwuid" in hay:
        return AUTH_INTRODUCE
    if "login:" in hay or "can't login" in hay or "login failed" in hay:
        return AUTH_LOGIN
    if "appliance" in hay and "empty" in hay:
        return APPLIANCE_LIST_EMPTY

    # Broad transport fallbacks come after the semantic markers above. Otherwise a
    # ConnectionError/ClientConnectionError carrying "401 unauthorized" becomes
    # ADDHON-430 and MQTT treats a credential rejection as a retryable outage.
    if isinstance(err, ConnectionError):
        return CONNECTION_REFUSED
    if any(
        family in class_names
        for family in (
            "clientconnectionerror",
            "clientconnectorerror",
            "clientoserror",
            "serverconnectionerror",
            "serverdisconnectederror",
        )
    ):
        return CONNECTION_REFUSED

    # Coarse fallback via the existing routing predicates (single source of truth).
    from .hon_client import _is_auth_error, _is_retryable_server_error

    if _is_retryable_server_error(err):
        return SERVER_ERROR
    if _is_auth_error(err):
        return INVALID_CREDENTIALS
    return UNKNOWN


def representative_failure(
    failures: list[tuple[str, Exception]]
) -> tuple[HonErrorCode, Exception | None]:
    """Pick a representative (code, error) from a batch of per-appliance failures (CR#6).

    The all-failed (and first-poll) paths used to raise a bare RuntimeError, which
    classify() maps to UNKNOWN (ADDHON-999) -- losing the real cause from the logs,
    the UpdateFailed message and Download Diagnostics. This surfaces a MEANINGFUL,
    NON-AUTH code instead: deterministically, the FIRST failure (in poll order) whose
    classify() is neither UNKNOWN nor a reauth code; if none qualifies, fall back to
    APPLIANCE_LOAD_FAILED (ADDHON-220) paired with the first error.

    Rejecting reauth codes is what keeps routing correct. Every error here already
    passed the non-auth gate (_requires_reauth was False at the call site), but
    classify() is substring-based and could still name an auth code (e.g. a message
    that merely contains "login")  -- surfacing it would flip the transient
    UpdateFailed into a reauth (ConfigEntryAuthFailed). APPLIANCE_LOAD_FAILED is
    requires_reauth=False, so the fallback stays non-auth too.

    Lives HERE, not in `hon_client`, because the setup path needs the same rule: the
    session cannot import the client (the client imports the session), and a private
    copy would be the exact "the cause is lost again" drift this function exists to
    stop.
    """
    chosen: Exception | None = None  # the first failure, kept as the fallback cause
    for _name, err in failures:
        if chosen is None:
            chosen = err
        code = classify(err)
        if code is not UNKNOWN and not code.requires_reauth:
            return code, err
    # No meaningful non-auth code found (or -- defensively -- an empty list, which the
    # gated call sites never pass): fall back to APPLIANCE_LOAD_FAILED, NEVER UNKNOWN.
    return APPLIANCE_LOAD_FAILED, chosen
