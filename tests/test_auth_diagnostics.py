# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Secret-safety and lifecycle tests for opt-in authentication diagnostics."""
from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _mod(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_stubs() -> None:
    ha = _mod("homeassistant")
    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exceptions = _mod("homeassistant.exceptions")
    base = getattr(
        exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {})
    )
    exceptions.HomeAssistantError = base
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base,), {})
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base,), {})
    )
    helpers = _mod("homeassistant.helpers")
    update_coordinator = _mod("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = getattr(
        update_coordinator,
        "DataUpdateCoordinator",
        type("DataUpdateCoordinator", (), {}),
    )
    update_coordinator.UpdateFailed = getattr(
        update_coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {})
    )
    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    helpers.update_coordinator = update_coordinator


_install_stubs()


from custom_components.addhon.client.auth_diagnostics import (
    AuthDiagnosticTrace,
    classify_failure_reason,
    classify_endpoint,
    summarize_html,
    summarize_json,
    summarize_links,
    summarize_response,
    summarize_tokens,
)


class AuthDiagnosticClassifierTest(unittest.TestCase):
    def test_failure_reason_classifier_returns_only_controlled_values(self) -> None:
        secret = "CANARY-secret@example.com"
        self.assertEqual(
            "incomplete_tokens",
            classify_failure_reason(
                RuntimeError(f"token page: incomplete tokens {secret}")
            ),
        )
        self.assertEqual(
            "no_href",
            classify_failure_reason(RuntimeError(f"progressive: no href {secret}")),
        )
        self.assertEqual(
            "status",
            classify_failure_reason(RuntimeError(f"token page: status 503 {secret}")),
        )
        self.assertEqual(
            "unexpected",
            classify_failure_reason(RuntimeError(secret)),
        )

    def test_endpoint_classifier_never_returns_url_material(self) -> None:
        secret = "person@example.com"
        self.assertEqual(
            "authorize",
            classify_endpoint(
                "https://account2.hon-smarthome.com/services/oauth2/authorize"
                f"?login_hint={secret}"
            ),
        )
        self.assertEqual(
            "progressive_login",
            classify_endpoint(
                f"https://account2.hon-smarthome.com/ProgressiveLogin?email={secret}"
            ),
        )
        self.assertEqual(
            "static_asset",
            classify_endpoint(f"https://evil.invalid/{secret}/page.css"),
        )
        self.assertEqual(
            "external",
            classify_endpoint(f"https://evil.invalid/{secret}/continue"),
        )

    def test_structural_summaries_contain_no_hostile_values(self) -> None:
        secret = "CANARY-secret@example.com-token"
        html = (
            "<html data-private='CANARY-secret@example.com-token'><body>"
            "<form action='/ProgressiveLogin?email=CANARY-secret@example.com-token'>"
            "<input name='username' value='CANARY-secret@example.com-token'>"
            "<input name='CANARY-secret@example.com-token'>"
            "<a href='/sCSS/CANARY-secret@example.com-token.css'>css</a>"
            "<script src='/CANARY-secret@example.com-token.js'></script>"
            "ProgressiveLogin verifyEmailOTP privacy"
            "</form></body></html>"
        )
        response = summarize_response(
            status=200,
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "Set-Cookie": f"sid={secret}; Secure; HttpOnly",
                "Location": f"https://evil.invalid/?token={secret}",
            },
            body=html,
            elapsed_ms=17,
            redirects=2,
        )
        html_shape = summarize_html(html)
        links = summarize_links(
            [
                f"/sCSS/{secret}.css",
                f"/ProgressiveLogin?email={secret}",
                f"https://evil.invalid/{secret}",
            ],
            selected_index=0,
        )
        json_shape = summarize_json(
            {
                "events": [{"attributes": {"values": {"url": secret}}}],
                secret: secret,
            }
        )
        token_shape = summarize_tokens(
            f"#access_token={secret}&amp;id_token={secret}&unknown={secret}"
        )

        rendered = repr(
            (response, html_shape, links, json_shape, token_shape)
        )
        self.assertNotIn(secret, rendered)
        self.assertEqual(response.media, "text/html")
        self.assertEqual(response.charset, "utf-8")
        self.assertEqual(response.location_kind, "external")
        self.assertEqual(response.cookie_kinds, ("session",))
        self.assertEqual(html_shape.forms, 1)
        self.assertEqual(html_shape.inputs, 2)
        self.assertEqual(html_shape.input_kinds, ("username", "other"))
        self.assertTrue(html_shape.progressive_login)
        self.assertTrue(html_shape.otp)
        self.assertTrue(html_shape.privacy)
        self.assertEqual(html_shape.page_kind, "mfa")
        self.assertEqual(
            links.kinds, ("static_asset", "progressive_login", "external")
        )
        self.assertEqual(links.selected_kind, "static_asset")
        self.assertEqual(json_shape.keys, ("attributes", "events", "url", "values"))
        self.assertEqual(json_shape.unknown_keys, 1)
        self.assertEqual(token_shape.present, ("access_token", "id_token"))
        self.assertEqual(token_shape.missing, ("refresh_token",))
        self.assertTrue(token_shape.html_escaped)

    def test_token_summary_counts_a_leading_field_once(self) -> None:
        for delimiter in ("&", "&amp;"):
            with self.subTest(delimiter=delimiter):
                summary = summarize_tokens(
                    delimiter.join(
                        (
                            "access_token=A",
                            "refresh_token=R",
                            "id_token=I",
                        )
                    )
                )

                self.assertTrue(summary.complete)
                self.assertEqual(summary.duplicates, ())


class AuthDiagnosticTraceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("tests.addhon.auth_diagnostics")

    @patch(
        "custom_components.addhon.client.auth_diagnostics.secrets.token_hex",
        return_value="7c19a2e4",
    )
    def test_failure_emits_ordered_warning_lines_once(self, _token_hex) -> None:
        trace = AuthDiagnosticTrace(enabled=True)
        trace.request("introduce", "GET", "authorize")
        trace.response(
            "introduce",
            summarize_response(
                status=200,
                headers={"Content-Type": "text/html"},
                body="<html></html>",
                elapsed_ms=12,
            ),
        )
        trace.token_shape(
            "get_token",
            summarize_tokens("#access_token=A&id_token=I"),
        )

        with self.assertLogs(self.logger.name, level="WARNING") as captured:
            trace.flush(
                self.logger,
                code="ADDHON-130",
                phase="get_token",
                reason="incomplete_tokens",
            )
            trace.flush(
                self.logger,
                code="ADDHON-999",
                phase="other",
                reason="other",
            )

        messages = [line.split(":", 2)[-1].strip() for line in captured.output]
        self.assertEqual(len(messages), 4)
        self.assertIn("trace=7c19a2e4 seq=01", messages[0])
        self.assertIn("event=request", messages[0])
        self.assertIn("seq=02", messages[1])
        self.assertIn("event=response", messages[1])
        self.assertIn("seq=03", messages[2])
        self.assertIn("event=tokens", messages[2])
        self.assertIn("seq=04", messages[3])
        self.assertIn("event=failed", messages[3])
        self.assertIn("code=ADDHON-130", messages[3])

    def test_disabled_and_discarded_traces_are_silent(self) -> None:
        disabled = AuthDiagnosticTrace(enabled=False)
        disabled.request("introduce", "GET", "authorize")
        discarded = AuthDiagnosticTrace(enabled=True)
        discarded.request("introduce", "GET", "authorize")
        discarded.discard()

        with self.assertNoLogs(self.logger.name, level="WARNING"):
            disabled.flush(
                self.logger,
                code="ADDHON-130",
                phase="get_token",
                reason="incomplete_tokens",
            )
            discarded.flush(
                self.logger,
                code="ADDHON-130",
                phase="get_token",
                reason="incomplete_tokens",
            )

    @patch(
        "custom_components.addhon.client.auth_diagnostics.secrets.token_hex",
        return_value="7c19a2e4",
    )
    def test_trace_is_bounded_and_reports_dropped_events(self, _token_hex) -> None:
        trace = AuthDiagnosticTrace(enabled=True)
        for _ in range(105):
            trace.request("introduce", "GET", "authorize")

        with self.assertLogs(self.logger.name, level="WARNING") as captured:
            trace.flush(
                self.logger,
                code="ADDHON-130",
                phase="get_token",
                reason="incomplete_tokens",
            )

        joined = "\n".join(captured.output)
        self.assertIn("truncated=true", joined)
        self.assertIn("dropped_events=5", joined)
        self.assertEqual(len(captured.output), 102)

    @patch(
        "custom_components.addhon.client.auth_diagnostics.secrets.token_hex",
        return_value="7c19a2e4",
    )
    def test_emitted_trace_excludes_all_canary_values(self, _token_hex) -> None:
        secret = "CANARY-password-token-cookie@example.com"
        trace = AuthDiagnosticTrace(enabled=True)
        trace.response(
            "token_response",
            summarize_response(
                status=200,
                headers={
                    "Content-Type": "text/html",
                    "Set-Cookie": f"sid={secret}; Secure",
                    "Location": f"https://evil.invalid/?secret={secret}",
                },
                body=f"<html>{secret}</html>",
                elapsed_ms=3,
            ),
        )
        trace.html("token_response", summarize_html(f"<html>{secret}</html>"))
        trace.links(
            "token_response",
            summarize_links([f"https://evil.invalid/{secret}"], selected_index=0),
        )
        trace.json_shape("login_submit", summarize_json({"events": secret}))
        trace.token_shape(
            "token_response",
            summarize_tokens(
                f"access_token={secret}&refresh_token={secret}&id_token={secret}"
            ),
        )

        with self.assertLogs(self.logger.name, level="WARNING") as captured:
            trace.flush(
                self.logger,
                code="ADDHON-130",
                phase="get_token",
                reason="incomplete_tokens",
            )

        self.assertNotIn(secret, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
