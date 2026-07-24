# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Page identity of the login flow, and the actionable failure it enables (issue #67).

The v5.9.2 diagnostic release localized a report to "the token page carried no tokens",
but could not say WHAT that page was: the trace only knew ``page_kind=other`` and
``endpoint=auth_other``. These tests pin the two things that close that gap:

- ``analyze_page`` names a page (path/query/title/text/form shape) using allowlists
  only, so an unexpected interstitial is identifiable from ONE user log;
- ``classify_token_page`` turns "no tokens" into a verdict, and a verdict the user can
  act on (a set/change-password form, a consent wall) raises ADDHON-165 instead of the
  mute ADDHON-130.

The reproduction replays the exact shape of the reported trace: login accepted (session
cookie minted), post-login ProgressiveLogin hop, then a VisualForce form with two
password boxes where the OAuth hand-off should have been.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _mod(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_stubs() -> None:
    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exc = _mod("homeassistant.exceptions")
    base = getattr(exc, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exc.HomeAssistantError = base
    exc.ConfigEntryNotReady = getattr(
        exc, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base,), {})
    )
    exc.ConfigEntryAuthFailed = getattr(
        exc, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base,), {})
    )
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = getattr(
        uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {})
    )
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))
    ha = _mod("homeassistant")
    ha.config_entries, ha.core, ha.exceptions = ce, core, exc
    ha.helpers = _mod("homeassistant.helpers")
    ha.helpers.update_coordinator = uc
    yarl = _mod("yarl")
    if not hasattr(yarl, "URL"):

        class URL:
            def __init__(self, s, encoded=False):
                self._s = s

            def __str__(self):
                return self._s

        yarl.URL = URL


_install_stubs()

from custom_components.addhon import error_codes as ec  # noqa: E402
from custom_components.addhon.client.auth_diagnostics import (  # noqa: E402
    ACCOUNT_ACTION_VERDICTS,
    AuthDiagnosticTrace,
    analyze_page,
    classify_endpoint,
    classify_failure_reason,
    classify_token_page,
    summarize_html,
    summarize_page,
)
from custom_components.addhon.client.transport.auth import (  # noqa: E402
    AccountActionRequired,
    HonAuth,
    NativeAuthError,
)
from custom_components.addhon.client.transport.device import HonDevice  # noqa: E402

AUTH = "https://account2.hon-smarthome.com"
CANARY = "CANARY-secret@example.com-token"

# A Salesforce/VisualForce forced password step: hidden ViewState fields, two password
# boxes (new + confirm), no login markers. Field names carry the j_id0 prefix exactly as
# VisualForce emits them, which is why the kind detection matches by substring.
CHANGE_PASSWORD_PAGE = f"""
<html><head><title>Change Your Password</title></head><body>
<form id="theForm" action="/apex/ChangePassword" method="post">
<input type="hidden" name="com.salesforce.visualforce.ViewState" value="{CANARY}">
<input type="hidden" name="com.salesforce.visualforce.ViewStateVersion" value="{CANARY}">
<input type="hidden" name="j_id0:theForm:j_id5" value="{CANARY}">
<input type="hidden" name="j_id0:theForm:j_id6" value="{CANARY}">
<input type="hidden" name="j_id0:theForm:j_id7" value="{CANARY}">
<input type="hidden" name="j_id0:theForm:j_id8" value="{CANARY}">
<label>New Password</label>
<input type="password" name="j_id0:theForm:newPassword" value="">
<label>Confirm New Password</label>
<input type="password" name="j_id0:theForm:confirmPassword" value="">
<button type="submit">Continue</button>
<div>Your password has expired. Set a new password to continue.</div>
</form>
<script>window.location.href = '{CANARY}';</script>
</body></html>
"""

CONSENT_PAGE = f"""
<html><head><title>Terms of Service</title></head><body>
<form action="/apex/PrivacyConsent" method="post">
<input type="hidden" name="com.salesforce.visualforce.ViewState" value="{CANARY}">
<input type="checkbox" name="acceptTerms">
<label>I accept the terms and the privacy policy</label>
<button>Continue</button>
</form></body></html>
"""

LOGIN_AGAIN_PAGE = f"""
<html><head><title>Login</title></head><body>
<form action="/s/login/" method="post">
<input name="username" value="{CANARY}">
<input type="password" name="pw" value="">
<button>Log in</button></form></body></html>
"""

# The 300+ KB Lightning login page mentions privacy/terms in its footer. The v5.9.2
# trace reported it as page_kind=privacy, which read like a consent wall when it was
# simply the login page.
LOGIN_PAGE = (
    "<html><head><title>hOn Login</title></head><body>"
    "<div>Sign in</div><a href='/privacy'>Privacy policy</a>"
    "<a href='/terms'>Terms of use</a>"
    "<script>var loginConfig = {\"fwuid\":\"FW\"};</script>"
    "</body></html>"
)


class FakeResp:
    def __init__(self, status=200, text="", json=None, headers=None) -> None:
        self.status = status
        self._text = text
        self._json = json
        self.headers = headers or {}

    async def text(self):
        return self._text

    async def json(self, content_type=None):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list = []
        self.cookie_jar = types.SimpleNamespace(clear_domain=lambda d: None)

    def _next(self, method, url):
        self.calls.append((method, str(url)))
        if not self._responses:
            raise AssertionError(f"unexpected call: {method} {url}")
        return self._responses.pop(0)

    def get(self, url, **kw):
        return self._next("GET", url)

    def post(self, url, **kw):
        return self._next("POST", url)


def _reported_chain(token_page: str):
    """The reported flow (issue #67), up to and including the token page.

    Mirrors the trace: the two manual redirects answer 200 with NO Location (so the
    login URL is reused), the post-login page offers two ProgressiveLogin hrefs, and the
    ProgressiveLogin page is a bare JS bounce with a single relative href.
    """
    return [
        FakeResp(text="x url = '/s/login/p?startURL=%2Fhome' y"),
        FakeResp(status=200, headers={}),
        FakeResp(status=200, headers={}),
        FakeResp(
            text=(
                "<html><head><title>hOn Login</title></head><body><div>Sign in</div>"
                "<a href='/privacy'>Privacy policy</a><script>var ctx = "
                '{"fwuid":"FW123","loaded":{"APPLICATION@x":"y"}};</script>'
                "</body></html>"
            )
        ),
        FakeResp(json={"events": [{"attributes": {"values": {"url": f"{AUTH}/tokpage"}}}]}),
        FakeResp(
            text=(
                "<a href='/apex/ProgressiveLogin?startURL=%2Fhome'>a</a>"
                "<a href='/apex/ProgressiveLogin?retURL=%2Fhome'>b</a>"
            )
        ),
        FakeResp(text="<html><script>href = '/s/nextstep'</script></html>"),
        FakeResp(text=token_page),
    ]


class PageIdentityTest(unittest.TestCase):
    def test_login_page_is_no_longer_reported_as_a_consent_wall(self) -> None:
        shape = summarize_html(LOGIN_PAGE)

        self.assertTrue(shape.login)
        self.assertTrue(shape.privacy)  # the footer words are still there
        self.assertEqual("login", shape.page_kind)  # but they no longer win

    def test_change_password_page_is_named_by_structure(self) -> None:
        shape, page = analyze_page(f"{AUTH}/apex/ChangePassword?startURL=%2Fhome", CHANGE_PASSWORD_PAGE)

        self.assertEqual(2, shape.password_inputs)
        self.assertTrue(shape.password_change)
        self.assertEqual("password_change", shape.page_kind)
        self.assertIn("new_password", shape.input_kinds)
        self.assertIn("confirm_password", shape.input_kinds)
        self.assertIn("security", shape.input_kinds)  # the ViewState hidden fields
        # The path names the step itself: the changepassword rule beats the generic
        # /apex/ one, so the log says WHICH interstitial it was.
        self.assertEqual("change_password", page.endpoint)
        self.assertEqual(("apex", "changepassword"), page.path_markers)
        self.assertEqual(("starturl",), page.query_names)
        self.assertEqual("change_password", page.form_action)
        self.assertEqual("post", page.form_method)
        self.assertIn("password", page.title_markers)
        self.assertIn("expir", page.text_markers)
        self.assertIn("continu", page.text_markers)
        self.assertTrue(page.js_redirect)
        self.assertFalse(page.hon_scheme)
        self.assertEqual(0, page.oauth_done_hits)

    def test_script_bodies_are_not_read_as_page_text(self) -> None:
        # Otherwise every marker in 300 KB of framework JS would be "present" and the
        # vocabulary would say nothing at all.
        page = summarize_page(
            f"{AUTH}/s/x",
            "<html><body><script>var terms='accept the privacy policy'</script>"
            "<div>Hello</div></body></html>",
        )

        self.assertEqual((), page.text_markers)

    def test_page_summary_keeps_no_url_or_content_material(self) -> None:
        shape, page = analyze_page(
            f"{AUTH}/apex/ChangePassword?startURL=%2F{CANARY}&secretName={CANARY}",
            CHANGE_PASSWORD_PAGE,
        )

        rendered = repr((shape, page))
        self.assertNotIn(CANARY, rendered)
        self.assertNotIn("secretname", rendered)
        self.assertEqual(1, page.unknown_query)  # counted, never named
        self.assertEqual(12, len(page.path_hash))

    def test_new_interstitial_endpoints_are_named(self) -> None:
        cases = {
            f"{AUTH}/s/changepassword?ec=1": "change_password",
            f"{AUTH}/secur/setpassword.jsp": "set_password",
            f"{AUTH}/s/forgotpassword": "reset_password",
            f"{AUTH}/secur/frontdoor.jsp?sid=x": "secur",
            f"{AUTH}/apex/SomeStep": "apex_page",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(expected, classify_endpoint(url))


class TokenPageVerdictTest(unittest.TestCase):
    def _verdict(self, url: str, page_html: str) -> str:
        shape, page = analyze_page(url, page_html)
        return classify_token_page(shape, page)

    def test_password_form_is_an_account_action(self) -> None:
        verdict = self._verdict(f"{AUTH}/apex/ChangePassword", CHANGE_PASSWORD_PAGE)

        self.assertEqual("password_change", verdict)
        self.assertIn(verdict, ACCOUNT_ACTION_VERDICTS)

    def test_consent_form_is_an_account_action(self) -> None:
        verdict = self._verdict(f"{AUTH}/apex/PrivacyConsent", CONSENT_PAGE)

        self.assertEqual("consent", verdict)
        self.assertIn(verdict, ACCOUNT_ACTION_VERDICTS)

    def test_a_returned_login_form_is_not_blamed_on_the_account(self) -> None:
        verdict = self._verdict(f"{AUTH}/s/login/", LOGIN_AGAIN_PAGE)

        self.assertEqual("login", verdict)
        self.assertNotIn(verdict, ACCOUNT_ACTION_VERDICTS)

    def test_a_present_but_unparsed_hand_off_accuses_our_parser(self) -> None:
        verdict = self._verdict(
            f"{AUTH}/finaltok",
            "<html><body>href = 'hon://mobilesdk/detect/oauth/done"
            "#access_token=AAA&amp;id_token=CCC'</body></html>",
        )

        self.assertEqual("token_link_unparsed", verdict)
        self.assertNotIn(verdict, ACCOUNT_ACTION_VERDICTS)

    def test_an_echoed_hand_off_does_not_hide_the_password_form(self) -> None:
        # The reported page carried HTML-escaped markup, and a Salesforce interstitial
        # echoes the OAuth request it interrupted. Quoting hon://...oauth/done must not
        # outrank two password boxes, or the real diagnosis flips to "our parser".
        echoed = CHANGE_PASSWORD_PAGE.replace(
            "<div>Your password",
            "<div>hon://mobilesdk/detect/oauth/done#state=x&amp;y</div><div>Your password",
        )

        shape, page = analyze_page(f"{AUTH}/apex/ChangePassword", echoed)

        self.assertEqual("password_change", classify_token_page(shape, page))
        # page_kind must agree: it is what gates the event=skeleton emission, so an
        # oauth_done classification here would drop the shape of this very page.
        self.assertEqual("password_change", shape.page_kind)

    def test_empty_body_is_named_empty(self) -> None:
        self.assertEqual("empty", self._verdict(f"{AUTH}/finaltok", ""))


class ReportedFailureTest(unittest.TestCase):
    """End to end over the reported chain, with the diagnostics OFF and ON."""

    def _auth(self, responses, trace=None):
        return HonAuth(
            FakeSession(responses), "user@x.it", "pw", HonDevice(), auth_trace=trace
        )

    def test_password_step_raises_an_actionable_code_without_diagnostics(self) -> None:
        # The actionable error must NOT depend on the opt-in checkbox: a user who never
        # enables diagnostics still has to be told what to do.
        auth = self._auth(_reported_chain(CHANGE_PASSWORD_PAGE))

        with self.assertRaises(AccountActionRequired) as caught:
            asyncio.run(auth.authenticate())

        err = caught.exception
        self.assertIs(ec.ACCOUNT_ACTION_REQUIRED, err.error_code)
        self.assertEqual("ADDHON-165", err.error_code.label)
        self.assertIs(ec.ACCOUNT_ACTION_REQUIRED, ec.classify(err))
        self.assertTrue(err.error_code.requires_reauth)
        self.assertEqual("account_action", classify_failure_reason(err))
        self.assertNotIn(CANARY, str(err))

    def test_unknown_token_page_keeps_the_generic_code(self) -> None:
        auth = self._auth(
            _reported_chain("<html><body><div>nothing useful here</div></body></html>")
        )

        with self.assertRaises(NativeAuthError) as caught:
            asyncio.run(auth.authenticate())

        err = caught.exception
        self.assertNotIsInstance(err, AccountActionRequired)
        self.assertIs(ec.AUTH_GET_TOKEN, ec.classify(err))
        self.assertEqual("incomplete_tokens", classify_failure_reason(err))

    def test_trace_identifies_the_page_and_the_verdict(self) -> None:
        trace = AuthDiagnosticTrace(enabled=True)
        auth = self._auth(_reported_chain(CHANGE_PASSWORD_PAGE), trace=trace)
        logger = logging.getLogger("tests.addhon.page_identity")

        with self.assertRaises(AccountActionRequired):
            asyncio.run(auth.authenticate())

        with self.assertLogs(logger.name, level="WARNING") as captured:
            trace.flush(
                logger,
                code="ADDHON-165",
                phase="get_token",
                reason="account_action",
            )

        joined = "\n".join(captured.output)
        self.assertNotIn(CANARY, joined)
        self.assertIn("event=verdict phase=token_response page=password_change", joined)
        self.assertIn("event=page phase=token_response", joined)
        self.assertIn("form_action=change_password", joined)
        self.assertIn("page_kind=password_change", joined)
        # The skeleton is emitted for the pages the flow did not expect, and NOT for the
        # ordinary login stop.
        self.assertIn("event=skeleton phase=token_response", joined)
        self.assertNotIn("event=skeleton phase=login_page", joined)
        self.assertIn("code=ADDHON-165", joined)
        self.assertIn("reason=account_action", joined)

    def test_consent_wall_without_a_next_hop_is_still_actionable(self) -> None:
        # A consent form has no navigation href at all, so the flow used to die on the
        # mute "get_token: no href" one step earlier than the token page.
        responses = _reported_chain(CHANGE_PASSWORD_PAGE)[:5]
        responses.append(FakeResp(text=CONSENT_PAGE))
        auth = self._auth(responses)

        with self.assertRaises(AccountActionRequired) as caught:
            asyncio.run(auth.authenticate())

        self.assertIn("post_login", str(caught.exception))
        self.assertIs(ec.ACCOUNT_ACTION_REQUIRED, ec.classify(caught.exception))

    def test_progressive_dead_end_is_still_actionable(self) -> None:
        responses = _reported_chain(CHANGE_PASSWORD_PAGE)[:6]
        responses.append(FakeResp(text=CHANGE_PASSWORD_PAGE.replace("href", "hxef")))
        auth = self._auth(responses)

        with self.assertRaises(AccountActionRequired) as caught:
            asyncio.run(auth.authenticate())

        self.assertIn("progressive_page", str(caught.exception))

    def test_a_broken_classifier_still_raises_the_token_error(self) -> None:
        # The classification is a diagnostic: if it blows up, the login must still fail
        # with its own error instead of a crash from the diagnostics.
        auth = self._auth(_reported_chain(CHANGE_PASSWORD_PAGE))

        with patch(
            "custom_components.addhon.client.transport.auth.analyze_page",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(NativeAuthError) as caught:
                asyncio.run(auth.authenticate())

        self.assertNotIsInstance(caught.exception, AccountActionRequired)
        self.assertIs(ec.AUTH_GET_TOKEN, ec.classify(caught.exception))

    def test_trace_stays_bounded_on_a_huge_hostile_page(self) -> None:
        trace = AuthDiagnosticTrace(enabled=True)
        huge = "<div>" + (CANARY + " password ") * 20000 + "</div>"
        auth = self._auth(_reported_chain(huge), trace=trace)
        logger = logging.getLogger("tests.addhon.page_identity.bounded")

        with self.assertRaises(NativeAuthError):
            asyncio.run(auth.authenticate())

        with self.assertLogs(logger.name, level="WARNING") as captured:
            trace.flush(
                logger, code="ADDHON-130", phase="get_token", reason="incomplete_tokens"
            )

        joined = "\n".join(captured.output)
        self.assertNotIn(CANARY, joined)
        for line in captured.output:
            self.assertLess(len(line), 4096)


if __name__ == "__main__":
    unittest.main()
