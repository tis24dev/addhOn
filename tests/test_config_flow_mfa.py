"""Tests for the 2FA (email-OTP) config-flow step.

Covers the step graph driven by a paused MFAChallengeRequired: user/reauth -> 2fa
(which sends the code) -> submit (create/update entry with the refresh token) or
re-prompt on a wrong code / resend. stdlib unittest with HA + voluptuous stubs.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

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
    config_entries.ConfigEntry = getattr(config_entries, "ConfigEntry", type("ConfigEntry", (), {}))

    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    config_entries.ConfigFlow = getattr(config_entries, "ConfigFlow", ConfigFlow)
    config_entries.OptionsFlow = getattr(config_entries, "OptionsFlow", type("OptionsFlow", (), {}))
    config_entries.SOURCE_REAUTH = getattr(config_entries, "SOURCE_REAUTH", "reauth")

    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))

    data_entry_flow = _mod("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = getattr(data_entry_flow, "FlowResult", dict)

    exceptions = _mod("homeassistant.exceptions")
    base_err = getattr(exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exceptions.HomeAssistantError = base_err
    exceptions.ConfigEntryNotReady = getattr(exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {}))
    exceptions.ConfigEntryAuthFailed = getattr(exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {}))

    helpers = _mod("homeassistant.helpers")
    update_coordinator = _mod("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = getattr(update_coordinator, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    update_coordinator.UpdateFailed = getattr(update_coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    helpers.update_coordinator = update_coordinator

    if "voluptuous" not in sys.modules:
        vol = _mod("voluptuous")
        vol.Schema = lambda schema=None, **kwargs: schema

        class _Key:
            def __init__(self, key, *args, **kwargs):
                self.key = key

        vol.Required = _Key
        vol.Optional = _Key


_install_stubs()

from custom_components.addhon.client.transport.auth import (  # noqa: E402
    MFACodeInvalid,
    MFAChallengeRequired,
)


class _FakeEntry:
    def __init__(self, email="person@example.com", unique_id="person@example.com"):
        self.entry_id = "entry-1"
        self.data = {"email": email, "password": "old"}
        self.unique_id = unique_id


class _FakeConfigEntries:
    def __init__(self, entry):
        self._entry = entry

    def async_get_entry(self, entry_id):
        return self._entry


class _FakeHass:
    def __init__(self, entry):
        self.config_entries = _FakeConfigEntries(entry)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _FakeMfaClient:
    def __init__(self, *, refresh_token="rt-xyz", fail_code=False, send_raises=False):
        self._refresh_token = refresh_token
        self.fail_code = fail_code
        self.send_raises = send_raises
        self.closed = False
        self.sent = 0
        self.submitted: list[str] = []

    def resend_mfa_code_sync(self, context):
        if self.send_raises:
            from custom_components.addhon.client.transport.auth import MFASendFailed

            raise MFASendFailed("mfa: could not send the verification code")
        self.sent += 1

    def submit_mfa_code_sync(self, context, code):
        self.submitted.append(code)
        if self.fail_code:
            raise MFACodeInvalid("mfa: invalid verification code")

    @property
    def refresh_token(self):
        return self._refresh_token

    async def async_close(self):
        self.closed = True


def _make_flow(entry, *, source="user"):
    from custom_components.addhon.config_flow import ConfigFlow

    flow = ConfigFlow()
    flow.hass = _FakeHass(entry)
    flow.context = {"source": source, "entry_id": entry.entry_id}
    flow.unique_id = None
    flow.calls = {}

    async def _set_unique_id(unique_id):
        flow.unique_id = unique_id

    def _abort_if_unique_id_configured():
        return None

    def _show_form(*, step_id, data_schema=None, errors=None, description_placeholders=None):
        return {
            "type": "form",
            "step_id": step_id,
            "errors": errors or {},
            "description_placeholders": description_placeholders or {},
        }

    def _abort(*, reason):
        return {"type": "abort", "reason": reason}

    def _create_entry(*, title, data):
        flow.calls["create"] = {"title": title, "data": data}
        return {"type": "create_entry", "title": title, "data": data}

    def _update_reload_and_abort(target_entry, *, data=None, **kwargs):
        flow.calls["update"] = {"entry": target_entry, "data": data}
        return {"type": "abort", "reason": "reauth_successful", "data": data}

    flow.async_set_unique_id = _set_unique_id
    flow._abort_if_unique_id_configured = _abort_if_unique_id_configured
    flow.async_show_form = _show_form
    flow.async_abort = _abort
    flow.async_create_entry = _create_entry
    flow.async_update_reload_and_abort = _update_reload_and_abort
    return flow


def _challenge(client):
    err = MFAChallengeRequired(types.SimpleNamespace(challenge_kind="email"))
    err.client = client
    return err


class MfaFlowTest(unittest.IsolatedAsyncioTestCase):
    def _patch_validate(self, fn) -> None:
        from custom_components.addhon import config_flow

        original = config_flow.validate_input
        config_flow.validate_input = fn
        self.addCleanup(setattr, config_flow, "validate_input", original)

    async def test_user_challenge_routes_to_2fa_and_sends_code(self) -> None:
        client = _FakeMfaClient()

        async def challenge(hass, data):
            raise _challenge(client)

        self._patch_validate(challenge)
        flow = _make_flow(_FakeEntry())

        result = await flow.async_step_user({"email": "P@x.com", "password": "p"})

        self.assertEqual("form", result["type"])
        self.assertEqual("2fa", result["step_id"])
        self.assertEqual(1, client.sent)  # entering the step sends the code

    async def test_submit_valid_code_creates_entry_with_refresh_token(self) -> None:
        client = _FakeMfaClient(refresh_token="RT-OK")

        async def challenge(hass, data):
            raise _challenge(client)

        self._patch_validate(challenge)
        flow = _make_flow(_FakeEntry())
        await flow.async_step_user({"email": "p@x.com", "password": "secret"})

        result = await flow.async_step_2fa({"code": "12345", "resend": False})

        self.assertEqual("create_entry", result["type"])
        self.assertEqual(
            {"email": "p@x.com", "password": "secret", "refresh_token": "RT-OK"},
            result["data"],
        )
        self.assertEqual(["12345"], client.submitted)
        self.assertTrue(client.closed)  # client torn down after success

    async def test_wrong_code_reshows_form(self) -> None:
        client = _FakeMfaClient(fail_code=True)

        async def challenge(hass, data):
            raise _challenge(client)

        self._patch_validate(challenge)
        flow = _make_flow(_FakeEntry())
        await flow.async_step_user({"email": "p@x.com", "password": "p"})

        result = await flow.async_step_2fa({"code": "00000", "resend": False})

        self.assertEqual("form", result["type"])
        self.assertEqual("2fa", result["step_id"])
        self.assertEqual("mfa_code_invalid", result["errors"]["base"])
        self.assertEqual("ADDHON-161", result["description_placeholders"]["error_code"])
        self.assertFalse(client.closed)  # kept open for the retry

    async def test_empty_code_reshows_without_submitting(self) -> None:
        client = _FakeMfaClient()

        async def challenge(hass, data):
            raise _challenge(client)

        self._patch_validate(challenge)
        flow = _make_flow(_FakeEntry())
        await flow.async_step_user({"email": "p@x.com", "password": "p"})

        result = await flow.async_step_2fa({"code": "  ", "resend": False})

        self.assertEqual("form", result["type"])
        self.assertEqual("mfa_code_invalid", result["errors"]["base"])
        self.assertEqual([], client.submitted)  # never submitted

    async def test_resend_re_sends_and_reshows(self) -> None:
        client = _FakeMfaClient()

        async def challenge(hass, data):
            raise _challenge(client)

        self._patch_validate(challenge)
        flow = _make_flow(_FakeEntry())
        await flow.async_step_user({"email": "p@x.com", "password": "p"})  # sent=1
        result = await flow.async_step_2fa({"code": "", "resend": True})

        self.assertEqual("form", result["type"])
        self.assertEqual("2fa", result["step_id"])
        self.assertEqual({}, result["errors"])
        self.assertEqual(2, client.sent)  # initial + resend
        self.assertEqual([], client.submitted)

    async def test_reauth_challenge_updates_entry(self) -> None:
        entry = _FakeEntry()
        client = _FakeMfaClient(refresh_token="RT-RE")

        async def challenge(hass, data):
            raise _challenge(client)

        self._patch_validate(challenge)
        flow = _make_flow(entry, source="reauth")
        await flow.async_step_reauth_confirm({"password": "newpass"})

        result = await flow.async_step_2fa({"code": "54321", "resend": False})

        self.assertEqual("abort", result["type"])
        self.assertEqual("reauth_successful", result["reason"])
        self.assertEqual(
            {"email": "person@example.com", "password": "newpass", "refresh_token": "RT-RE"},
            flow.calls["update"]["data"],
        )

    async def test_non_2fa_user_step_persists_refresh_token(self) -> None:
        async def ok(hass, data):
            return {"title": "x", "appliance_count": 1, "refresh_token": "RT-NEW"}

        self._patch_validate(ok)
        flow = _make_flow(_FakeEntry())
        result = await flow.async_step_user({"email": "p@x.com", "password": "pw"})

        self.assertEqual("create_entry", result["type"])
        self.assertEqual("RT-NEW", result["data"]["refresh_token"])

    async def test_async_remove_closes_abandoned_client(self) -> None:
        client = _FakeMfaClient()

        async def challenge(hass, data):
            raise _challenge(client)

        self._patch_validate(challenge)
        flow = _make_flow(_FakeEntry())
        await flow.async_step_user({"email": "p@x.com", "password": "p"})  # held client
        self.assertFalse(client.closed)
        await flow.async_remove()  # HA removes the abandoned flow
        self.assertTrue(client.closed)

    async def test_reentry_closes_previously_held_client(self) -> None:
        # Re-entering the user step while a prior challenge client is still held must
        # close the old client (no orphaned loop/thread/session).
        c1, c2 = _FakeMfaClient(), _FakeMfaClient()
        clients = iter([c1, c2])

        async def challenge(hass, data):
            raise _challenge(next(clients))

        self._patch_validate(challenge)
        flow = _make_flow(_FakeEntry())
        await flow.async_step_user({"email": "p@x.com", "password": "p"})  # holds c1
        self.assertFalse(c1.closed)
        await flow.async_step_user({"email": "p@x.com", "password": "p"})  # holds c2
        self.assertTrue(c1.closed)   # old client torn down
        self.assertFalse(c2.closed)  # new one held for the 2FA step

    async def test_no_challenge_state_aborts(self) -> None:
        flow = _make_flow(_FakeEntry())
        result = await flow.async_step_2fa({"code": "1"})
        self.assertEqual("abort", result["type"])
        self.assertEqual("mfa_no_challenge", result["reason"])

    async def test_send_failure_shows_error(self) -> None:
        client = _FakeMfaClient(send_raises=True)

        async def challenge(hass, data):
            raise _challenge(client)

        self._patch_validate(challenge)
        flow = _make_flow(_FakeEntry())
        result = await flow.async_step_user({"email": "p@x.com", "password": "p"})

        self.assertEqual("form", result["type"])
        self.assertEqual("2fa", result["step_id"])
        # the precise transient code surfaces, NOT a generic auth/credentials error
        self.assertEqual("mfa_send_failed", result["errors"]["base"])
        self.assertEqual("ADDHON-162", result["description_placeholders"]["error_code"])


if __name__ == "__main__":
    unittest.main()
