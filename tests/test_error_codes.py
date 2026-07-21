# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the stable error-code catalog (issue #30).

Covers catalog integrity (unique codes/slugs, label format), the classify()
mapping for the representative failures, the HonCodedError carrier (and that it
never leaks identity), the phase->timeout mapping, the _requires_reauth coupling,
and the catalog<->translations contract (every UI code has en+it strings with the
{error_code} placeholder, and no orphan error keys).

Pure stdlib unittest. HA is stubbed only because importing the package runs its
__init__.py; classify() lazily imports hon_client which is HA-free at module top.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
COMPONENT = REPO / "custom_components" / "addhon"
TRANSLATIONS = COMPONENT / "translations"


def _mod(name: str) -> types.ModuleType:
    m = sys.modules.get(name)
    if m is None:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return m


def _install_ha_stubs() -> None:
    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exc = _mod("homeassistant.exceptions")
    base = getattr(exc, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exc.HomeAssistantError = base
    exc.ConfigEntryNotReady = getattr(exc, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base,), {}))
    exc.ConfigEntryAuthFailed = getattr(exc, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base,), {}))
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))
    ha = _mod("homeassistant")
    ha.config_entries, ha.core, ha.exceptions = ce, core, exc
    ha.helpers = _mod("homeassistant.helpers")
    ha.helpers.update_coordinator = uc


_install_ha_stubs()

from custom_components.addhon import error_codes as ec  # noqa: E402
from custom_components.addhon import hon_client as hc  # noqa: E402


# Class named like the real one (classify keys off the class NAME for auth).
class NativeAuthError(Exception):
    pass


class CatalogIntegrityTest(unittest.TestCase):
    def test_codes_and_slugs_unique(self) -> None:
        codes = [c.code for c in ec.all_codes()]
        slugs = [c.slug for c in ec.all_codes()]
        self.assertEqual(len(codes), len(set(codes)), "duplicate numeric code")
        self.assertEqual(len(slugs), len(set(slugs)), "duplicate slug")

    def test_label_format(self) -> None:
        for c in ec.all_codes():
            self.assertEqual(c.label, f"ADDHON-{c.code}")
            self.assertTrue(c.reason_en.isascii(), f"{c.slug} reason must be ASCII")

    def test_known_code_number(self) -> None:
        # The MQTT subscribe timeout is the user's illustrative example.
        self.assertEqual(ec.MQTT_SUBSCRIBE_TIMEOUT.label, "ADDHON-320")


class ClassifyTest(unittest.TestCase):
    def test_coded_error_returns_its_code(self) -> None:
        err = ec.HonCodedError(ec.MQTT_CONNECT_TIMEOUT)
        self.assertIs(ec.classify(err), ec.MQTT_CONNECT_TIMEOUT)

    def test_timeout_uses_phase(self) -> None:
        self.assertIs(ec.classify(asyncio.TimeoutError()), ec.LOOP_TIMEOUT)
        self.assertIs(
            ec.classify(concurrent.futures.TimeoutError(), phase="connect"),
            ec.NETWORK_TIMEOUT,
        )
        self.assertIs(
            ec.classify(asyncio.TimeoutError(), phase="mqtt_subscribe"),
            ec.MQTT_SUBSCRIBE_TIMEOUT,
        )
        self.assertIs(
            ec.classify(asyncio.TimeoutError(), phase="load_appliance"),
            ec.NETWORK_TIMEOUT,
        )

    def test_auth_step_messages(self) -> None:
        self.assertIs(ec.classify(NativeAuthError("login: failed (status 200)")), ec.AUTH_LOGIN)
        self.assertIs(ec.classify(NativeAuthError("api_auth: no cognito token")), ec.AUTH_API_AUTH)
        self.assertIs(ec.classify(NativeAuthError("get_token: status 404")), ec.AUTH_GET_TOKEN)
        self.assertIs(ec.classify(NativeAuthError("introduce: no login url")), ec.AUTH_INTRODUCE)
        self.assertIs(ec.classify(NativeAuthError("Decode Error")), ec.DECODE_ERROR)

    def test_server_and_rate_limit_win_over_auth_name(self) -> None:
        # Retryable 5xx / 429 must beat the auth-named class (existing routing rule).
        for status in range(500, 600):
            err = NativeAuthError(f"api_auth: status {status}")
            with self.subTest(status=status):
                self.assertIs(ec.classify(err), ec.SERVER_ERROR)
                self.assertFalse(hc._requires_reauth(err))
        self.assertIs(ec.classify(NativeAuthError("429 too many requests")), ec.RATE_LIMITED)

    def test_server_status_format_variants(self) -> None:
        for message in (
            "api_auth: status=501",
            "api_auth: HTTP 505",
            "api_auth: response (507)",
            "api_auth: upstream [511]",
            "599 server response",
        ):
            with self.subTest(message=message):
                err = NativeAuthError(message)
                self.assertIs(ec.classify(err), ec.SERVER_ERROR)
                self.assertFalse(hc._requires_reauth(err))

    def test_textual_server_errors_are_case_insensitive(self) -> None:
        for message in (
            "hOn server error",
            "Internal Server Error",
            "Bad Gateway",
            "Gateway Timeout",
            "Temporarily Unavailable",
        ):
            with self.subTest(message=message):
                err = NativeAuthError(message)
                self.assertIs(ec.classify(err), ec.SERVER_ERROR)
                self.assertFalse(hc._requires_reauth(err))

        rate_limit = NativeAuthError("Too Many Requests")
        self.assertIs(ec.classify(rate_limit), ec.RATE_LIMITED)
        self.assertFalse(hc._requires_reauth(rate_limit))

    def test_status_matching_does_not_accept_longer_identifiers(self) -> None:
        # Model/id-like and out-of-range tokens must not masquerade as an HTTP 5xx.
        for token in ("H500", "X599Y", "5000", "1500", "499", "600"):
            with self.subTest(token=token):
                err = NativeAuthError(f"api_auth: model {token} unavailable")
                self.assertIs(ec.classify(err), ec.AUTH_API_AUTH)
                self.assertTrue(hc._requires_reauth(err))

    def test_rate_limit_status_does_not_accept_longer_identifiers(self) -> None:
        # 429 must match only as a standalone HTTP status, not embedded in longer tokens
        # (mirror of the 5xx guard; _HTTP_STATUS_RE also feeds is_rate_limited_text).
        for token in ("H429", "X429Y", "4290", "1429", "428", "430"):
            with self.subTest(token=token):
                err = NativeAuthError(f"api_auth: model {token} unavailable")
                self.assertFalse(ec.is_rate_limited_text(str(err)))
                self.assertIs(ec.classify(err), ec.AUTH_API_AUTH)
                self.assertTrue(hc._requires_reauth(err))

    def test_network_classes(self) -> None:
        self.assertIs(ec.classify(RuntimeError("certificate verify failed")), ec.TLS_FAILURE)
        self.assertIs(ec.classify(RuntimeError("getaddrinfo failed")), ec.DNS_FAILURE)
        self.assertIs(ec.classify(RuntimeError("Connection refused")), ec.CONNECTION_REFUSED)

    def test_real_decode_and_disconnect_types_are_not_unknown(self) -> None:
        self.assertIs(
            ec.classify(json.JSONDecodeError("Expecting value", "", 0)),
            ec.DECODE_ERROR,
        )
        self.assertIs(
            ec.classify(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")),
            ec.DECODE_ERROR,
        )
        self.assertIs(
            ec.classify(ConnectionError("connection aborted")),
            ec.CONNECTION_REFUSED,
        )

        class ServerDisconnectedError(Exception):
            pass

        class ClientPayloadError(Exception):
            pass

        self.assertIs(
            ec.classify(ServerDisconnectedError("Server disconnected")),
            ec.CONNECTION_REFUSED,
        )
        self.assertIs(
            ec.classify(ClientPayloadError("Response payload is not completed")),
            ec.DECODE_ERROR,
        )

    def test_auth_rejections_beat_broad_connection_types(self) -> None:
        class AuthConnectionError(ConnectionError):
            pass

        class ClientConnectionError(Exception):
            pass

        class ServerDisconnectedError(Exception):
            pass

        cases = (
            (AuthConnectionError("api_auth: status 401"), ec.AUTH_API_AUTH),
            (ClientConnectionError("HTTP 401 unauthorized"), ec.INVALID_CREDENTIALS),
            (ServerDisconnectedError("token rejected"), ec.INVALID_CREDENTIALS),
        )
        for err, expected in cases:
            with self.subTest(error=type(err).__name__):
                self.assertIs(ec.classify(err), expected)
                self.assertTrue(hc._requires_reauth(err))

    def test_connection_class_names_without_auth_are_refused(self) -> None:
        # aiohttp-style connection classes with a plain outage message (no auth
        # marker, no refused/reset/DNS/TLS keyword) must reach the class-name
        # family fallback and classify as CONNECTION_REFUSED. Guards the family
        # list in classify() so dropping a name there is caught by a test.
        class ClientConnectionError(Exception):
            pass

        class ClientConnectorError(Exception):
            pass

        class ClientOSError(Exception):
            pass

        class ServerConnectionError(Exception):
            pass

        cases = (
            ClientConnectionError("connection lost"),
            ClientConnectorError("host unreachable"),
            ClientOSError("socket failure"),
            ServerConnectionError("peer gone"),
        )
        for err in cases:
            with self.subTest(error=type(err).__name__):
                self.assertIs(ec.classify(err), ec.CONNECTION_REFUSED)

    def test_classify_and_requires_reauth_agree_on_auth_phrasings(self) -> None:
        # Coherence guard against drift: classify()'s auth-rejection gate and
        # hon_client._requires_reauth's keyword predicate are two independent
        # lists. For an EXPLICIT rejection phrasing both must route to reauth; for
        # a plain outage neither must. Bare endpoint names ("api_auth" alone) are
        # out of scope here: the two predicates intentionally differ on those. If
        # a new rejection phrase is added to one side only, this test goes red.
        reauth_phrasings = (
            "unauthorized",
            "HTTP 401",
            "HTTP 403",
            "status 401",
            "token rejected",
            "invalid token",
            "expired token",
            "invalid credential",
            "api_auth: status 401",
        )
        non_reauth_phrasings = (
            "503 service unavailable",
            "429 too many requests",
            "connection reset by peer",
        )
        for phrase in reauth_phrasings:
            err = RuntimeError(phrase)
            with self.subTest(reauth=phrase):
                self.assertTrue(ec.classify(err).requires_reauth)
                self.assertTrue(hc._requires_reauth(err))
        for phrase in non_reauth_phrasings:
            err = RuntimeError(phrase)
            with self.subTest(non_reauth=phrase):
                self.assertFalse(ec.classify(err).requires_reauth)
                self.assertFalse(hc._requires_reauth(err))

    def test_server_status_beats_builtin_connection_type(self) -> None:
        err = ConnectionError("503 server error")
        self.assertIs(ec.classify(err), ec.SERVER_ERROR)
        self.assertFalse(hc._requires_reauth(err))

    def test_aiohttp_connect_failure_is_not_tls(self) -> None:
        # aiohttp's ClientConnectorError __str__ ALWAYS carries "ssl:default" for any
        # HTTPS connect failure; that is a plain outage, NOT a TLS problem (refuter F1).
        class ClientConnectorError(Exception):
            pass

        err = ClientConnectorError(
            "Cannot connect to host api-iot.he.services:443 ssl:default [Connect call failed]"
        )
        self.assertIs(ec.classify(err), ec.CONNECTION_REFUSED)

    def test_aiohttp_dns_failure_via_connector(self) -> None:
        class ClientConnectorError(Exception):
            pass

        err = ClientConnectorError(
            "Cannot connect to host api-iot.he.services:443 ssl:default "
            "[Errno -2] Name or service not known"
        )
        self.assertIs(ec.classify(err), ec.DNS_FAILURE)

    def test_real_cert_error_is_tls_by_class_name(self) -> None:
        # A genuine TLS failure: aiohttp raises ClientConnectorCertificateError; the
        # class NAME carries "certificate" even though the message also says
        # "cannot connect to host".
        class ClientConnectorCertificateError(Exception):
            pass

        err = ClientConnectorCertificateError(
            "Cannot connect to host api-iot.he.services:443 ssl:default "
            "[SSLCertVerificationError: certificate has expired]"
        )
        self.assertIs(ec.classify(err), ec.TLS_FAILURE)

    def test_fallback_unknown(self) -> None:
        self.assertIs(ec.classify(RuntimeError("something odd happened")), ec.UNKNOWN)

    def test_generic_auth_keyword_fallback(self) -> None:
        self.assertIs(ec.classify(RuntimeError("HTTP 401 unauthorized")), ec.INVALID_CREDENTIALS)


class CodedErrorTest(unittest.TestCase):
    def test_str_has_label_and_reason_no_identity(self) -> None:
        err = ec.HonCodedError(ec.INVALID_CREDENTIALS, phase="connect")
        text = str(err)
        self.assertIn("ADDHON-100", text)
        self.assertIn("Invalid email or password", text)
        self.assertNotIn("@", text)  # no email/identity ever in the message
        self.assertEqual(err.phase, "connect")
        self.assertIs(err.error_code, ec.INVALID_CREDENTIALS)

    def test_phase_timeout_code(self) -> None:
        self.assertIs(ec.phase_timeout_code(""), ec.LOOP_TIMEOUT)
        self.assertIs(ec.phase_timeout_code(None), ec.LOOP_TIMEOUT)
        self.assertIs(ec.phase_timeout_code("mqtt_connect"), ec.MQTT_CONNECT_TIMEOUT)
        self.assertIs(ec.phase_timeout_code("aws_token"), ec.MQTT_CONNECT_TIMEOUT)
        # Every phase-timeout code must read as retryable (so the failure is retried,
        # never mistaken for a reauth) -> its message must trip _is_retryable_server_error.
        for slug in ("loop_timeout", "network_timeout", "mqtt_connect_timeout", "mqtt_subscribe_timeout"):
            self.assertTrue(
                hc._is_retryable_server_error(ec.HonCodedError(ec.by_slug(slug))),
                f"{slug} should be retryable",
            )


class RequiresReauthCouplingTest(unittest.TestCase):
    def test_coded_error_routes_by_its_flag(self) -> None:
        self.assertTrue(hc._requires_reauth(ec.HonCodedError(ec.AUTH_LOGIN)))
        self.assertTrue(hc._requires_reauth(ec.HonCodedError(ec.INVALID_CREDENTIALS)))
        self.assertFalse(hc._requires_reauth(ec.HonCodedError(ec.NETWORK_TIMEOUT)))
        self.assertFalse(hc._requires_reauth(ec.HonCodedError(ec.LOOP_TIMEOUT)))
        self.assertFalse(hc._requires_reauth(ec.HonCodedError(ec.MQTT_SUBSCRIBE_TIMEOUT)))

    def test_plain_errors_unchanged(self) -> None:
        # The legacy path (no error_code attr) keeps its behaviour.
        self.assertTrue(hc._requires_reauth(NativeAuthError("login failed")))
        self.assertFalse(hc._requires_reauth(NativeAuthError("boom status 500")))
        self.assertFalse(hc._requires_reauth(RuntimeError("network down")))


class CatalogTranslationsContractTest(unittest.TestCase):
    """Every UI code must have an en+it string with the {error_code} placeholder,
    and the config.error key set is exactly the buckets + the UI slugs."""

    def setUp(self) -> None:
        self.errors = {
            lang: json.loads((TRANSLATIONS / f"{lang}.json").read_text("utf-8"))["config"]["error"]
            for lang in ("en", "it")
        }

    def test_ui_codes_have_localized_strings_with_placeholder(self) -> None:
        for code in ec.all_codes():
            if not code.ui:
                continue
            for lang in ("en", "it"):
                self.assertIn(code.slug, self.errors[lang], f"{lang}: missing error.{code.slug}")
                self.assertIn(
                    "{error_code}",
                    self.errors[lang][code.slug],
                    f"{lang}: error.{code.slug} must carry the {{error_code}} placeholder",
                )

    def test_no_orphan_error_keys(self) -> None:
        expected = {"cannot_connect", "invalid_auth"} | {c.slug for c in ec.all_codes() if c.ui}
        for lang in ("en", "it"):
            self.assertEqual(set(self.errors[lang]), expected, f"{lang}: error key drift")


if __name__ == "__main__":
    unittest.main()
