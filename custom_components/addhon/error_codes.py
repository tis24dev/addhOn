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
from dataclasses import dataclass

CODE_PREFIX = "ADDHON"


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
# 165 (account-action-required/privacy) is intentionally RESERVED, not registered: the
# privacy markers live in every ProgressiveLogin page's remoting registry, so it cannot be
# detected reliably without a captured privacy-only page. 165-168 stay reserved.
MFA_SEND_FAILED = _reg(162, "mfa_send_failed", "Could not send the verification code", False)
MFA_SERVICE_ERROR = _reg(163, "mfa_service_error", "Two-factor verification service error", False)
MFA_TOKEN_AFTER_VERIFY_FAILED = _reg(
    164, "mfa_token_after_verify_failed", "Sign-in could not finish after verification", True
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


def phase_timeout_code(phase: str | None) -> HonErrorCode:
    """Timeout code for a stalled setup phase (empty/unknown -> LOOP_TIMEOUT)."""
    if not phase:
        return LOOP_TIMEOUT
    if phase.startswith("load_appliance"):
        return NETWORK_TIMEOUT
    return _PHASE_TIMEOUT.get(phase, LOOP_TIMEOUT)


def _is_timeout(err: BaseException) -> bool:
    return isinstance(
        err, (asyncio.TimeoutError, concurrent.futures.TimeoutError, TimeoutError)
    )


def classify(err: BaseException, *, phase: str | None = None) -> HonErrorCode:
    """Map any exception to a stable :class:`HonErrorCode`.

    Order is most-specific-first. A carried code wins; then rate-limit/5xx (which
    must beat the auth-named-class rule, like the existing classifier); then
    timeouts (attributed to ``phase`` when known); then TLS/DNS/refused; then the
    native auth-step messages; finally the coarse buckets via the existing
    ``hon_client`` predicates.
    """
    code = getattr(err, "error_code", None)
    if isinstance(code, HonErrorCode):
        return code

    name = type(err).__name__.lower()
    text = str(err).lower()
    hay = f"{text} {name}"

    if "429" in hay or "too many requests" in hay:
        return RATE_LIMITED
    if any(
        token in hay
        for token in (
            "500",
            "502",
            "503",
            "504",
            "internal server error",
            "server error",
            "bad gateway",
            "gateway timeout",
            "temporarily unavailable",
        )
    ):
        return SERVER_ERROR
    if _is_timeout(err) or "timed out" in hay or "timeout" in hay:
        return phase_timeout_code(phase)
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
    if "decode error" in hay:
        return DECODE_ERROR
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

    # Coarse fallback via the existing routing predicates (single source of truth).
    from .hon_client import _is_auth_error, _is_retryable_server_error

    if _is_retryable_server_error(err):
        return SERVER_ERROR
    if _is_auth_error(err):
        return INVALID_CREDENTIALS
    return UNKNOWN
