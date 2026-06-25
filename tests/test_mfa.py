"""Offline tests for the 2FA (email-OTP) support on the Salesforce path.

Covers the pure scrape/build helpers in transport.oauth, the HonAuth resume flow
(detection -> send -> verify -> finish -> token) with a mocked session, and the new
error-code catalog entries. No network: the live end-to-end is validated separately
(apk/probe_2fa_interactive.py). Reuses the FakeSession/FakeResp + HA-stub style of
test_transport_auth.py.
"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# session.py (used by MfaNativeChainTest) does `import aiohttp`; the pytest-only CI env
# has no aiohttp. Use the real one when present, else a minimal stub (the tests drive a
# FakeSession, so aiohttp's runtime behavior is never exercised). yarl is ensured by
# conftest.
try:  # noqa: E402
    import aiohttp  # noqa: F401
except ImportError:
    _aiohttp_stub = types.ModuleType("aiohttp")
    _aiohttp_stub.ClientSession = type("ClientSession", (), {})
    _aiohttp_stub.ClientResponse = type("ClientResponse", (), {})
    _aiohttp_stub.ContentTypeError = type("ContentTypeError", (Exception,), {})
    sys.modules["aiohttp"] = _aiohttp_stub

from test_transport_auth import (  # noqa: E402 - reuse the stubs + fakes
    AUTH,
    FakeResp,
    FakeSession,
    _happy_responses,
)

from custom_components.addhon.client.transport import oauth  # noqa: E402
from custom_components.addhon.client.transport.auth import (  # noqa: E402
    HonAuth,
    MFAChallengeRequired,
    MFACodeInvalid,
    MFASendFailed,
    MFAServiceError,
    MFATokenAfterVerifyFailed,
    NativeAuthError,
)
from custom_components.addhon.client.transport.device import HonDevice  # noqa: E402
from custom_components.addhon import error_codes  # noqa: E402

# A minimal but faithful ProgressiveLogin OTP page (the markers the detector keys on).
OTP_PAGE = """
<html><head>
<script>function finishFlowCall(){jsfcljs(document.forms['ProgressiveLogin:j_id8'],
'ProgressiveLogin:j_id8:j_id12,ProgressiveLogin:j_id8:j_id12','');return false}</script>
<script>Visualforce.remoting.Manager.add(new $VFRM.RemotingProviderImpl({"vf":{"vid":"0664VID"},
"actions":{"ProgressiveLoginController":{"ms":[
{"name":"resendEmailCode","len":1,"ns":"","ver":45.0,"csrf":"RCSRF","authorization":"RAUTH"},
{"name":"verifyEmailOTP","len":1,"ns":"","ver":45.0,"csrf":"VCSRF","authorization":"VAUTH"}
]}},"service":"apexremote"}));</script>
</head><body>
<form id="ProgressiveLogin:j_id8" name="ProgressiveLogin:j_id8" method="post" action="/ProgressiveLogin">
<input type="hidden" name="com.salesforce.visualforce.ViewState" value="VS1" />
<input type="hidden" name="com.salesforce.visualforce.ViewStateCSRF" value="VSC" />
<input type="hidden" name="com.salesforce.visualforce.ViewStateMAC" value="VSM" />
<input type="hidden" name="com.salesforce.visualforce.ViewStateVersion" value="VSV" />
<input id="emailCode" name="emailCode" type="text" maxlength="5" />
</form></body></html>
"""

OTP_URL = f"{AUTH}/apex/ProgressiveLogin?retURL=%2Fhome&locale=en"


class OauthHelpersTest(unittest.TestCase):
    def test_is_progressive_otp_true(self) -> None:
        self.assertTrue(oauth.is_progressive_otp(OTP_PAGE))

    def test_is_progressive_otp_false_on_plain_page(self) -> None:
        self.assertFalse(oauth.is_progressive_otp("<html>no otp here</html>"))
        # privacy-only ProgressiveLogin (no emailCode/verifyEmailOTP) must NOT match
        self.assertFalse(
            oauth.is_progressive_otp("ProgressiveLoginController privacyUpdate only")
        )

    def test_detect_builds_context(self) -> None:
        ctx = oauth.detect_progressive_otp(OTP_PAGE, OTP_URL)
        self.assertIsNotNone(ctx)
        self.assertEqual("email", ctx.challenge_kind)
        self.assertTrue(ctx.can_resend)
        self.assertEqual("account2.hon-smarthome.com", ctx.host.split("//")[1])
        self.assertEqual("0664VID", ctx.vid)
        self.assertEqual("VCSRF", ctx.verify["csrf"])
        self.assertEqual("VAUTH", ctx.verify["authorization"])
        self.assertEqual("verifyEmailOTP", ctx.verify["method"])
        self.assertEqual("RCSRF", ctx.resend["csrf"])
        self.assertEqual(f"{AUTH}/ProgressiveLogin", ctx.vf_action)
        self.assertEqual("ProgressiveLogin:j_id8:j_id12", ctx.finish_marker)
        self.assertEqual("en", ctx.locale)
        # the 4 ViewState hidden inputs are captured (emailCode is not type=hidden)
        self.assertIn("com.salesforce.visualforce.ViewState", ctx.vf_hidden)
        self.assertEqual("VS1", ctx.vf_hidden["com.salesforce.visualforce.ViewState"])
        self.assertNotIn("emailCode", ctx.vf_hidden)

    def test_detect_none_without_remoting_creds(self) -> None:
        # OTP markers present but the verify descriptor has no csrf -> not usable.
        page = OTP_PAGE.replace('"csrf":"VCSRF","authorization":"VAUTH"', '"csrf":"","authorization":""')
        self.assertIsNone(oauth.detect_progressive_otp(page, OTP_URL))

    def test_detect_none_on_non_otp_page(self) -> None:
        self.assertIsNone(oauth.detect_progressive_otp("<html>plain</html>", OTP_URL))

    def test_detect_relative_url_falls_back_to_auth_host(self) -> None:
        ctx = oauth.detect_progressive_otp(OTP_PAGE, "/apex/ProgressiveLogin?x=1")
        self.assertEqual(AUTH, ctx.host)
        self.assertEqual(f"{AUTH}/ProgressiveLogin", ctx.vf_action)

    def test_build_remoting_payload(self) -> None:
        ctx = oauth.detect_progressive_otp(OTP_PAGE, OTP_URL)
        payload = oauth.build_remoting_payload(ctx.vid, ctx.verify, ["12345"], 21)
        self.assertEqual("ProgressiveLoginController", payload["action"])
        self.assertEqual("verifyEmailOTP", payload["method"])
        self.assertEqual(["12345"], payload["data"])
        self.assertEqual("rpc", payload["type"])
        self.assertEqual(21, payload["tid"])
        self.assertEqual(
            {"csrf": "VCSRF", "vid": "0664VID", "ns": "", "ver": 45, "authorization": "VAUTH"},
            payload["ctx"],
        )

    def test_build_finish_body(self) -> None:
        ctx = oauth.detect_progressive_otp(OTP_PAGE, OTP_URL)
        body = oauth.build_finish_body(ctx)
        self.assertEqual("VS1", body["com.salesforce.visualforce.ViewState"])
        # the commandLink marker is set to itself (the jsfcljs convention)
        self.assertEqual(
            "ProgressiveLogin:j_id8:j_id12", body["ProgressiveLogin:j_id8:j_id12"]
        )

    def test_parse_remoting_result(self) -> None:
        self.assertEqual({"result": True}, oauth.parse_remoting_result('[{"result":true}]'))
        self.assertEqual({"result": False}, oauth.parse_remoting_result('{"result":false}'))
        self.assertEqual({}, oauth.parse_remoting_result("not json"))
        self.assertEqual({}, oauth.parse_remoting_result("[]"))

    def test_is_progressive_otp_tolerant_of_quote_and_space(self) -> None:
        # A VF template tweak (single quotes / spaces around '=') must still detect.
        for variant in ("name='emailCode'", "name = \"emailCode\"", "NAME=\"EMAILCODE\""):
            page = OTP_PAGE.replace('name="emailCode"', variant)
            self.assertTrue(oauth.is_progressive_otp(page), variant)

    def test_context_repr_is_redacted(self) -> None:
        ctx = oauth.detect_progressive_otp(OTP_PAGE, OTP_URL)
        text = f"{ctx!r} {ctx}"
        # secrets AND the session-sensitive ViewState id must not appear
        for secret in ("VCSRF", "VAUTH", "RCSRF", "VS1", "VSC", "0664VID"):
            self.assertNotIn(secret, text)
        self.assertIn("email", text)


def _verify_remoting(result: bool) -> FakeResp:
    return FakeResp(text=f'[{{"statusCode":200,"result":{str(result).lower()}}}]')


class MfaResumeTest(unittest.TestCase):
    def _auth(self, responses):
        return HonAuth(FakeSession(responses), "user@x.it", "pw", HonDevice())

    def _context(self):
        return oauth.detect_progressive_otp(OTP_PAGE, OTP_URL)

    def test_detection_raises_challenge(self) -> None:
        # authenticate() up to the ProgressiveLogin OTP page -> MFAChallengeRequired.
        responses = _happy_responses()[:5]  # introduce, 2 redirects, login page, _login
        responses.append(FakeResp(text="href = '/ProgressiveLogin?x=1'"))  # get_token
        responses.append(FakeResp(text=OTP_PAGE))  # the OTP step
        auth = self._auth(responses)
        with self.assertRaises(MFAChallengeRequired) as cm:
            asyncio.run(auth.authenticate())
        self.assertEqual("email", cm.exception.context.challenge_kind)
        self.assertEqual(error_codes.MFA_REQUIRED, cm.exception.error_code)

    def test_submit_valid_code_completes(self) -> None:
        auth = self._auth([
            _verify_remoting(True),  # verifyEmailOTP -> ok
            FakeResp(text=""),  # finish VF postback
            FakeResp(text="...oauth/done#access_token=AAA&refresh_token=r%2Fb&id_token=CCC&..."),
            FakeResp(json={"cognitoUser": {"Token": "COG"}}),  # _api_auth
        ])
        asyncio.run(auth.submit_mfa_code(self._context(), "12345"))
        self.assertEqual("AAA", auth.access_token)
        self.assertEqual("r/b", auth.refresh_token)
        self.assertEqual("CCC", auth.id_token)
        self.assertEqual("COG", auth.cognito_token)

    def test_submit_valid_code_no_trailing_amp(self) -> None:
        # The done-URL last field (id_token) has NO trailing '&' -> the extract-URL +
        # '&' path must still capture all three tokens (the load-bearing fix).
        auth = self._auth([
            _verify_remoting(True),
            FakeResp(text=""),
            FakeResp(text="x url = 'hon://mobilesdk/detect/oauth/done#access_token=AAA&refresh_token=BBB&id_token=CCC' y"),
            FakeResp(json={"cognitoUser": {"Token": "COG"}}),
        ])
        asyncio.run(auth.submit_mfa_code(self._context(), "12345"))
        self.assertEqual("AAA", auth.access_token)
        self.assertEqual("CCC", auth.id_token)
        self.assertEqual("COG", auth.cognito_token)

    def test_submit_ignores_stray_token_substring_before_done_url(self) -> None:
        # A stray `*_token=...&` earlier in the page must NOT be captured instead of the
        # real tokens (extract-URL-first, not raw-page-first).
        auth = self._auth([
            _verify_remoting(True),
            FakeResp(text=""),
            FakeResp(text="var x='id_token=STALE&access_token=STALE&refresh_token=STALE&';"
                          " url = 'hon://x/oauth/done#access_token=REAL&refresh_token=RB&id_token=RID' "),
            FakeResp(json={"cognitoUser": {"Token": "COG"}}),
        ])
        asyncio.run(auth.submit_mfa_code(self._context(), "12345"))
        self.assertEqual("REAL", auth.access_token)
        self.assertEqual("RID", auth.id_token)

    def test_submit_invalid_code_raises(self) -> None:
        auth = self._auth([_verify_remoting(False)])
        with self.assertRaises(MFACodeInvalid) as cm:
            asyncio.run(auth.submit_mfa_code(self._context(), "00000"))
        self.assertEqual(error_codes.MFA_CODE_INVALID, cm.exception.error_code)

    def test_submit_exception_type_is_service_error(self) -> None:
        # type=="exception" alone (no 5xx) -> transient MFA_SERVICE_ERROR, not invalid code.
        auth = self._auth([FakeResp(text='[{"type":"exception","message":"boom"}]')])
        with self.assertRaises(MFAServiceError) as cm:
            asyncio.run(auth.submit_mfa_code(self._context(), "12345"))
        self.assertNotIsInstance(cm.exception, MFACodeInvalid)
        self.assertEqual(error_codes.MFA_SERVICE_ERROR, error_codes.classify(cm.exception))

    def test_submit_5xx_is_service_error(self) -> None:
        # statusCode>=500 alone (no exception type) -> transient MFA_SERVICE_ERROR.
        auth = self._auth([FakeResp(text='[{"statusCode":503,"result":false}]')])
        with self.assertRaises(MFAServiceError) as cm:
            asyncio.run(auth.submit_mfa_code(self._context(), "12345"))
        self.assertEqual(error_codes.MFA_SERVICE_ERROR, error_codes.classify(cm.exception))

    def test_submit_plain_false_is_invalid_code(self) -> None:
        # result==false with no exception/5xx -> wrong code (MFACodeInvalid), NOT service.
        auth = self._auth([_verify_remoting(False)])
        with self.assertRaises(MFACodeInvalid):
            asyncio.run(auth.submit_mfa_code(self._context(), "00000"))

    def test_remoting_debug_line_is_redacted(self) -> None:
        # With DEBUG on, the remoting breadcrumb runs and emits the method + redacted
        # summary, never the response body/secret.
        import logging

        auth = self._auth([FakeResp(text='[{"result":false,"message":"SECRET-MSG"}]')])
        with self.assertLogs(
            "custom_components.addhon.client.transport.auth", level="DEBUG"
        ) as cm:
            try:
                asyncio.run(auth.submit_mfa_code(self._context(), "0"))
            except MFACodeInvalid:
                pass
        blob = "\n".join(cm.output)
        self.assertIn("remoting method=verifyEmailOTP", blob)
        self.assertNotIn("SECRET-MSG", blob)

    def test_submit_unreadable_remoting_raises(self) -> None:
        auth = self._auth([FakeResp(text="<html>not json</html>")])
        with self.assertRaises(NativeAuthError):
            asyncio.run(auth.submit_mfa_code(self._context(), "12345"))

    def test_resend_ok(self) -> None:
        auth = self._auth([_verify_remoting(True)])
        asyncio.run(auth.resend_mfa_code(self._context()))  # no raise

    def test_resend_failure_is_send_failed(self) -> None:
        auth = self._auth([_verify_remoting(False)])
        with self.assertRaises(MFASendFailed) as cm:
            asyncio.run(auth.resend_mfa_code(self._context()))
        self.assertEqual(error_codes.MFA_SEND_FAILED, error_codes.classify(cm.exception))

    def test_submit_token_retrieval_failure_is_token_after_verify(self) -> None:
        auth = self._auth([
            _verify_remoting(True),
            FakeResp(text=""),  # finish
            FakeResp(text="no tokens here"),  # authorize returns no done url
        ])
        with self.assertRaises(MFATokenAfterVerifyFailed) as cm:
            asyncio.run(auth.submit_mfa_code(self._context(), "12345"))
        self.assertEqual(
            error_codes.MFA_TOKEN_AFTER_VERIFY_FAILED, error_codes.classify(cm.exception)
        )

    def test_auth_phase_tracked_and_cleared(self) -> None:
        # On a verify failure the phase pinpoints mfa_verify; on success it is cleared.
        auth = self._auth([_verify_remoting(False)])
        try:
            asyncio.run(auth.submit_mfa_code(self._context(), "0"))
        except MFACodeInvalid:
            pass
        self.assertEqual("mfa_verify", auth._current_phase)

        auth2 = self._auth([
            _verify_remoting(True), FakeResp(text=""),
            FakeResp(text="...oauth/done#access_token=A&refresh_token=B&id_token=C&..."),
            FakeResp(json={"cognitoUser": {"Token": "COG"}}),
        ])
        asyncio.run(auth2.submit_mfa_code(self._context(), "12345"))
        self.assertEqual("", auth2._current_phase)  # cleared on success


class MfaNativeChainTest(unittest.TestCase):
    """Drive submit through the REAL NativeHon -> HonConnection -> HonAuth chain (over a
    FakeSession), so a dropped/zeroed token in any wrapper would fail this test."""

    def test_native_submit_completes_and_propagates_tokens(self) -> None:
        from custom_components.addhon.client.session import NativeHon
        from custom_components.addhon.client.transport.api import HonApi
        from custom_components.addhon.client.transport.connection import HonConnection

        session = FakeSession([
            _verify_remoting(True),  # verifyEmailOTP
            FakeResp(text=""),  # finish VF postback
            FakeResp(text="...oauth/done#access_token=AAA&refresh_token=RB&id_token=CCC&..."),
            FakeResp(json={"cognitoUser": {"Token": "COG"}}),  # _api_auth
            FakeResp(json={"modules": {}}),  # setup() -> load_appliances (0 appliances)
        ])

        async def _run():
            hon = NativeHon("u@x.it", "pw", session=session, enable_mqtt=False, minimal=True)
            hon._connection = await HonConnection("u@x.it", "pw", session=session).create()
            hon._api = HonApi(hon._connection)
            ctx = oauth.detect_progressive_otp(OTP_PAGE, OTP_URL)
            await hon.submit_mfa_code(ctx, "12345")
            return hon

        hon = asyncio.run(_run())
        self.assertEqual("COG", hon._connection.auth.cognito_token)
        self.assertEqual("CCC", hon._connection.auth.id_token)
        # the rotated refresh token is propagated to the connection (for persistence)
        self.assertEqual("RB", hon._connection._refresh_token)
        self.assertEqual("RB", hon.refresh_token)
        self.assertEqual([], hon.appliances)  # 0 appliances, setup completed
        self.assertEqual("", hon.auth_phase)  # phase cleared on success (end-to-end)


class HonClientMfaCaptureTest(unittest.TestCase):
    """The dedicated-loop sync wrappers must record the precise code+phase on a 2FA
    failure (so diagnostics/form reflect the real cause), and clear it on success."""

    class _FakeHon:
        def __init__(self, exc=None):
            self._exc = exc
            self.auth_phase = "mfa_verify"

        async def submit_mfa_code(self, context, code):
            if self._exc:
                raise self._exc
            return self

        async def resend_mfa_code(self, context):
            if self._exc:
                raise self._exc

    def _client(self, fake):
        from custom_components.addhon.hon_client import HonClient

        client = HonClient("u@x.it", "pw")
        client._start_hon_loop()
        client._hon_instance = fake
        self.addCleanup(client._stop_hon_loop)
        return client

    def test_submit_failure_records_code_and_phase(self) -> None:
        client = self._client(self._FakeHon(MFACodeInvalid("bad")))
        with self.assertRaises(MFACodeInvalid):
            client.submit_mfa_code_sync(object(), "0")
        self.assertEqual(error_codes.MFA_CODE_INVALID, client.last_error_code)
        self.assertEqual("mfa_verify", client.last_error_phase)

    def test_resend_failure_records_send_code(self) -> None:
        client = self._client(self._FakeHon(MFASendFailed("no")))
        with self.assertRaises(MFASendFailed):
            client.resend_mfa_code_sync(object())
        self.assertEqual(error_codes.MFA_SEND_FAILED, client.last_error_code)
        self.assertEqual("mfa_send", client.last_error_phase)

    def test_submit_success_clears_stale_record(self) -> None:
        client = self._client(self._FakeHon())
        client.last_error_code = error_codes.MFA_REQUIRED  # stale challenge record
        client.last_error_phase = "mfa_challenge"
        client.last_mfa_summary = {"challenge_kind": "email", "can_resend": True}
        client.submit_mfa_code_sync(object(), "12345")
        self.assertIsNone(client.last_error_code)
        self.assertIsNone(client.last_error_phase)
        self.assertIsNone(client.last_mfa_summary)


class MfaErrorCodesTest(unittest.TestCase):
    def test_codes_registered(self) -> None:
        self.assertEqual(160, error_codes.MFA_REQUIRED.code)
        self.assertEqual(161, error_codes.MFA_CODE_INVALID.code)
        self.assertEqual(162, error_codes.MFA_SEND_FAILED.code)
        self.assertEqual(163, error_codes.MFA_SERVICE_ERROR.code)
        self.assertEqual(164, error_codes.MFA_TOKEN_AFTER_VERIFY_FAILED.code)
        for code in (
            error_codes.MFA_REQUIRED, error_codes.MFA_CODE_INVALID,
            error_codes.MFA_SEND_FAILED, error_codes.MFA_SERVICE_ERROR,
            error_codes.MFA_TOKEN_AFTER_VERIFY_FAILED,
        ):
            self.assertTrue(code.ui)
        # 162/163 are transient (NOT reauth); the rest are reauth.
        self.assertFalse(error_codes.MFA_SEND_FAILED.requires_reauth)
        self.assertFalse(error_codes.MFA_SERVICE_ERROR.requires_reauth)
        for code in (
            error_codes.MFA_REQUIRED, error_codes.MFA_CODE_INVALID,
            error_codes.MFA_TOKEN_AFTER_VERIFY_FAILED,
        ):
            self.assertTrue(code.requires_reauth)

    def test_classify_carries_mfa_codes(self) -> None:
        challenge = MFAChallengeRequired(oauth.detect_progressive_otp(OTP_PAGE, OTP_URL))
        self.assertEqual(error_codes.MFA_REQUIRED, error_codes.classify(challenge))
        self.assertEqual(
            error_codes.MFA_CODE_INVALID, error_codes.classify(MFACodeInvalid("bad"))
        )

    def test_requires_reauth_routing(self) -> None:
        from custom_components.addhon.hon_client import _requires_reauth

        self.assertTrue(_requires_reauth(MFAChallengeRequired(oauth.detect_progressive_otp(OTP_PAGE, OTP_URL))))
        self.assertTrue(_requires_reauth(MFACodeInvalid("bad")))
        self.assertTrue(_requires_reauth(MFATokenAfterVerifyFailed("x")))
        # transient ones must NOT route to reauth (retry instead)
        self.assertFalse(_requires_reauth(MFASendFailed("x")))
        self.assertFalse(_requires_reauth(MFAServiceError("x")))


class RedactRemotingSummaryTest(unittest.TestCase):
    def test_keeps_only_safe_fields(self) -> None:
        from custom_components.addhon.debug_utils import redact_remoting_summary

        entry = {
            "result": True, "statusCode": 200, "type": "rpc",
            "message": "SECRET-MESSAGE", "data": "SECRET-DATA",
            "authorization": "JWT-SECRET",
        }
        summary = redact_remoting_summary(entry)
        text = repr(summary)
        for secret in ("SECRET-MESSAGE", "SECRET-DATA", "JWT-SECRET"):
            self.assertNotIn(secret, text)
        self.assertEqual(True, summary["result"])
        self.assertEqual(200, summary["statusCode"])
        self.assertEqual("rpc", summary["type"])
        # keys are NAMES only (a bounded sample), never the values
        self.assertIn("message", summary["keys"])

    def test_non_dict_is_safe(self) -> None:
        from custom_components.addhon.debug_utils import redact_remoting_summary

        self.assertEqual({"type": "str"}, redact_remoting_summary("boom"))


if __name__ == "__main__":
    unittest.main()
