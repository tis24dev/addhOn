# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Differential test of the native HTTP api (addhOn transport, Phase 3 piece 2).

pyhOn's `connection/api.HonAPI` methods live INLINE in async+HTTP methods, so they
are not importable on their own: the oracle is their VERBATIM transcription (the
`_oracle_*` below). For each method we verify TWO things:
  * the emitted REQUEST (verb, path, params/json) = pinned to the exact pyhOn
    contract (what goes to the cloud byte-identical);
  * the return VALUE on well-formed responses = identical to the pyhOn oracle.
Plus the INTENTIONAL DIVERGENCE cases where pyhOn crashes on a malformed response
(KeyError/TypeError/AttributeError) and we fall back to the safe empty default.

aiohttp/yarl/homeassistant are stubbed (no network): we inject a FakeConnection
into HonApi, so we do not touch the real transport.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import sys
import types
import unittest
from pathlib import Path

from _envelopes import OTHER_ACCOUNT, OUR_ACCOUNT, healthy, reporter

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _mod(name: str) -> types.ModuleType:
    m = sys.modules.get(name)
    if m is None:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return m


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
        yarl.URL = type("URL", (), {"__init__": lambda self, s, encoded=False: None})
    aio = _mod("aiohttp")
    aio.ClientSession = getattr(aio, "ClientSession", type("ClientSession", (), {}))
    aio.ClientResponse = getattr(aio, "ClientResponse", type("ClientResponse", (), {}))
    aio.ContentTypeError = getattr(
        aio, "ContentTypeError", type("ContentTypeError", (Exception,), {})
    )


_install_stubs()

from custom_components.addhon.client.transport import api as api_mod  # noqa: E402
from custom_components.addhon.client.transport import device as _device  # noqa: E402
from custom_components.addhon.client.transport.api import HonApi, API_URL  # noqa: E402


# --------------------------------------------------------------------------- #
# Test doubles                                                                #
# --------------------------------------------------------------------------- #
class FakeResponse:
    # `status` mirrors aiohttp.ClientResponse.status, which load_appliances reads into
    # its fetch census: a 4xx carrying a JSON body decodes cleanly and is delivered as
    # a success by connection.py (it re-raises only on 429 and >= 500), so the status
    # is the only thing that separates "the endpoint moved" from "the schema drifted".
    def __init__(self, body, text="<text>", status: int = 200) -> None:
        self._body = body
        self._text = text
        self.status = status

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _ReqCtx:
    def __init__(self, conn, method, url, kwargs, resp) -> None:
        self._conn = conn
        self._method = method
        self._url = url
        self._kwargs = kwargs
        self._resp = resp

    async def __aenter__(self):
        self._conn.calls.append((self._method, self._url, self._kwargs))
        return self._resp

    async def __aexit__(self, *a):
        return False


class FakeConnection:
    """Replaces HonConnection: records the requests, returns a fixed body."""

    def __init__(self, body, text="<text>", mobile_id="pyhOn", status: int = 200) -> None:
        self._body = body
        self._text = text
        self._status = status
        self.calls: list = []
        self.device = _device.HonDevice(mobile_id)

    def _ctx(self, method, url, kwargs):
        return _ReqCtx(
            self, method, url, kwargs, FakeResponse(self._body, self._text, self._status)
        )

    def get(self, url, **kwargs):
        return self._ctx("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._ctx("POST", url, kwargs)


class FakeAppliance:
    def __init__(self, **info) -> None:
        self.appliance_type = "REF"
        # The real HonAppliance.appliance_model_id returns str(self._info.get(...)) -
        # ALWAYS a string; we use the same type to avoid giving false confidence.
        self.appliance_model_id = "4321"
        self.mac_address = "AA:BB:CC:DD:EE:FF"
        self.code = "CODE123"
        self.info = info
        self.options = {"opt": 1}


def _run(coro):
    return asyncio.run(coro)


def _call(conn):
    """HonApi over a FakeConnection; the body is cloned on each run (the methods can
    mutate the payload via pop)."""
    return HonApi(conn)


# --------------------------------------------------------------------------- #
# VERBATIM oracles: pyhOn extraction (return value only; logging is omitted)    #
# --------------------------------------------------------------------------- #
def _oracle_commands(body):
    result = body.get("payload", {})
    if not result or result.pop("resultCode") != "0":
        return {}
    return result


def _oracle_history(result):
    if not result or not result.get("payload"):
        return []
    return result["payload"]["history"]


def _oracle_favourites(result):
    if not result or not result.get("payload"):
        return []
    return result["payload"]["favourites"]


def _oracle_last_activity(result):
    if result:
        activity = result.get("attributes", "")
        if activity:
            return activity
    return {}


def _oracle_appliance_data(result):
    if result:
        return result.get("payload", {}).get("applianceModel", {})
    return {}


def _oracle_payload(result):
    return result.get("payload", {})


def _oracle_aws_token(result):
    return result.get("payload", {}).get("tokenSigned", "")


# --------------------------------------------------------------------------- #
class ApiRequestShapeTest(unittest.TestCase):
    """The emitted REQUEST must match the exact pyhOn contract."""

    def test_load_appliances_posts_unified_api(self) -> None:
        body = {"modules": {"applianceList": {"payload": {"appliances": [{"a": 1}]}}}}
        conn = FakeConnection(body, mobile_id="MID")
        _run(_call(conn).load_appliances())
        method, url, kwargs = conn.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{API_URL}/unified-api/v1/view/appliance-list")
        self.assertEqual(kwargs["json"], {"deviceId": "MID"})

    def test_load_appliances_returns_parse_appliance_list(self) -> None:
        body = {"modules": {"applianceList": {"payload": {"appliances": [{"a": 1}]}}}}
        from custom_components.addhon.client.transport.parse import parse_appliance_list

        got = _run(_call(FakeConnection(copy.deepcopy(body))).load_appliances())
        self.assertEqual(got, parse_appliance_list(copy.deepcopy(body)))
        self.assertEqual(got, [{"a": 1}])

    def test_load_appliances_empty_returns_empty_and_warns(self) -> None:
        # 0 appliances (request OK): returns [] and logs the diagnostic warning.
        body = {"modules": {"applianceList": {"payload": {"appliances": []}}}}
        with self.assertLogs("custom_components.addhon.client.transport.api", level="WARNING") as cm:
            got = _run(_call(FakeConnection(body)).load_appliances())
        self.assertEqual(got, [])
        self.assertTrue(any("0 appliance" in m for m in cm.output))

    def test_load_commands_request(self) -> None:
        body = {"payload": {"resultCode": "0"}}
        conn = FakeConnection(body)
        app = FakeAppliance(eepromId="EE", fwVersion="1.2", series="S")
        _run(_call(conn).load_commands(app))
        method, url, kwargs = conn.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, f"{API_URL}/commands/v1/retrieve")
        self.assertEqual(
            kwargs["params"],
            {
                "applianceType": "REF",
                "applianceModelId": "4321",
                "macAddress": "AA:BB:CC:DD:EE:FF",
                "os": _device.OS,
                "appVersion": _device.APP_VERSION,
                "code": "CODE123",
                "firmwareId": "EE",
                "fwVersion": "1.2",
                "series": "S",
            },
        )

    def test_load_commands_optional_params_omitted_when_absent(self) -> None:
        conn = FakeConnection({"payload": {"resultCode": "0"}})
        _run(_call(conn).load_commands(FakeAppliance()))
        params = conn.calls[0][2]["params"]
        for absent in ("firmwareId", "fwVersion", "series"):
            self.assertNotIn(absent, params)

    def test_load_commands_optional_params_skip_falsy(self) -> None:
        # pyhOn uses `if value := info.get(...)`: a falsy value (e.g. "") does NOT go in params.
        conn = FakeConnection({"payload": {"resultCode": "0"}})
        _run(_call(conn).load_commands(FakeAppliance(eepromId="", fwVersion=0, series="")))
        params = conn.calls[0][2]["params"]
        for absent in ("firmwareId", "fwVersion", "series"):
            self.assertNotIn(absent, params)

    def test_simple_get_requests(self) -> None:
        app = FakeAppliance()
        mac = app.mac_address
        cases = {
            "load_command_history": (
                f"{API_URL}/commands/v1/appliance/{mac}/history",
                None,
            ),
            "load_favourites": (
                f"{API_URL}/commands/v1/appliance/{mac}/favourite",
                None,
            ),
            "load_last_activity": (
                f"{API_URL}/commands/v1/retrieve-last-activity",
                {"macAddress": mac},
            ),
            "load_appliance_data": (
                f"{API_URL}/commands/v1/appliance-model",
                {"code": "CODE123", "macAddress": mac},
            ),
            "load_attributes": (
                f"{API_URL}/commands/v1/context",
                {"macAddress": mac, "applianceType": "REF", "category": "CYCLE"},
            ),
            "load_statistics": (
                f"{API_URL}/commands/v1/statistics",
                {"macAddress": mac, "applianceType": "REF"},
            ),
            "load_maintenance": (
                f"{API_URL}/commands/v1/maintenance-cycle",
                {"macAddress": mac},
            ),
        }
        for method_name, (exp_url, exp_params) in cases.items():
            with self.subTest(method=method_name):
                conn = FakeConnection({"payload": {}})
                _run(getattr(_call(conn), method_name)(app))
                verb, url, kwargs = conn.calls[0]
                self.assertEqual(verb, "GET")
                self.assertEqual(url, exp_url)
                self.assertEqual(kwargs.get("params"), exp_params)

    def test_load_aws_token_request(self) -> None:
        conn = FakeConnection({"payload": {"tokenSigned": "T"}})
        _run(_call(conn).load_aws_token())
        verb, url, kwargs = conn.calls[0]
        self.assertEqual(verb, "GET")
        self.assertEqual(url, f"{API_URL}/auth/v1/introspection")
        self.assertEqual(kwargs.get("params"), None)


class ApiReturnVsOracleTest(unittest.TestCase):
    """On well-formed responses the return value must be IDENTICAL to the pyhOn oracle."""

    def test_load_commands(self) -> None:
        body = {"payload": {"resultCode": "0", "settings": {"x": 1}, "startProgram": {}}}
        got = _run(_call(FakeConnection(copy.deepcopy(body))).load_commands(FakeAppliance()))
        self.assertEqual(got, _oracle_commands(copy.deepcopy(body)))
        # the resultCode was removed from the returned dict (like pyhOn)
        self.assertNotIn("resultCode", got)
        self.assertEqual(got, {"settings": {"x": 1}, "startProgram": {}})

    def test_load_command_history(self) -> None:
        body = {"payload": {"history": [{"command": {"commandName": "x"}}]}}
        got = _run(
            _call(FakeConnection(copy.deepcopy(body))).load_command_history(FakeAppliance())
        )
        self.assertEqual(got, _oracle_history(copy.deepcopy(body)))
        self.assertEqual(got, [{"command": {"commandName": "x"}}])

    def test_load_favourites(self) -> None:
        body = {"payload": {"favourites": [{"a": 1}]}}
        got = _run(
            _call(FakeConnection(copy.deepcopy(body))).load_favourites(FakeAppliance())
        )
        self.assertEqual(got, _oracle_favourites(copy.deepcopy(body)))
        self.assertEqual(got, [{"a": 1}])

    def test_load_last_activity(self) -> None:
        body = {"attributes": {"foo": "bar"}}
        got = _run(
            _call(FakeConnection(copy.deepcopy(body))).load_last_activity(FakeAppliance())
        )
        self.assertEqual(got, _oracle_last_activity(copy.deepcopy(body)))
        self.assertEqual(got, {"foo": "bar"})

    def test_load_appliance_data(self) -> None:
        body = {"payload": {"applianceModel": {"m": 1}}}
        got = _run(
            _call(FakeConnection(copy.deepcopy(body))).load_appliance_data(FakeAppliance())
        )
        self.assertEqual(got, _oracle_appliance_data(copy.deepcopy(body)))
        self.assertEqual(got, {"m": 1})

    def test_load_attributes_statistics_maintenance(self) -> None:
        body = {"payload": {"k": "v"}}
        for method_name in ("load_attributes", "load_statistics", "load_maintenance"):
            with self.subTest(method=method_name):
                got = _run(
                    getattr(_call(FakeConnection(copy.deepcopy(body))), method_name)(
                        FakeAppliance()
                    )
                )
                self.assertEqual(got, _oracle_payload(copy.deepcopy(body)))
                self.assertEqual(got, {"k": "v"})

    def test_load_aws_token(self) -> None:
        body = {"payload": {"tokenSigned": "SIGNED"}}
        got = _run(_call(FakeConnection(copy.deepcopy(body))).load_aws_token())
        self.assertEqual(got, _oracle_aws_token(copy.deepcopy(body)))
        self.assertEqual(got, "SIGNED")


class ApiHardeningTest(unittest.TestCase):
    """Where pyhOn crashes on a malformed response, we fall back to the safe default."""

    def test_commands_missing_result_code_crashes_pyhon_safe_for_us(self) -> None:
        body = {"payload": {"settings": {}}}  # no resultCode
        with self.assertRaises(KeyError):
            _oracle_commands(copy.deepcopy(body))
        got = _run(_call(FakeConnection(copy.deepcopy(body))).load_commands(FakeAppliance()))
        self.assertEqual(got, {})

    def test_commands_payload_non_dict_safe(self) -> None:
        for payload in ([{"x": 1}], "str", 5):
            with self.subTest(payload=payload):
                body = {"payload": payload}
                got = _run(
                    _call(FakeConnection(copy.deepcopy(body))).load_commands(FakeAppliance())
                )
                self.assertEqual(got, {})

    def test_load_commands_failure_redacts_identity_in_log(self) -> None:
        # #33: both failure branches log the raw cloud response at ERROR (never
        # gated); identity in the body must be redacted.
        mac = "AA:BB:CC:DD:EE:FF"
        logger = "custom_components.addhon.client.transport.api"
        for body in (
            {"macAddress": mac, "payload": {}},                   # invalid-payload branch
            {"macAddress": mac, "payload": {"resultCode": "1"}},  # resultCode != 0 branch
            {"meta": {"macAddress": mac}, "payload": {}},         # nested mac (recursion)
            {"payload": {"resultCode": "1", "dev": {"macAddress": mac}}},  # nested mac
        ):
            with self.subTest(body=body):
                with self.assertLogs(logger, level="ERROR") as cm:
                    got = _run(
                        _call(FakeConnection(copy.deepcopy(body))).load_commands(FakeAppliance())
                    )
                self.assertEqual(got, {})
                blob = "\n".join(cm.output)
                self.assertNotIn(mac, blob)
                self.assertIn("***", blob)

    def test_commands_result_code_nonzero(self) -> None:
        body = {"payload": {"resultCode": "1", "settings": {}}}
        self.assertEqual(_oracle_commands(copy.deepcopy(body)), {})
        got = _run(_call(FakeConnection(copy.deepcopy(body))).load_commands(FakeAppliance()))
        self.assertEqual(got, {})

    def test_history_payload_without_history_key_safe(self) -> None:
        body = {"payload": {"other": 1}}  # pyhOn -> KeyError on ["history"]
        with self.assertRaises(KeyError):
            _oracle_history(copy.deepcopy(body))
        got = _run(
            _call(FakeConnection(copy.deepcopy(body))).load_command_history(FakeAppliance())
        )
        self.assertEqual(got, [])

    def test_favourites_payload_without_key_safe(self) -> None:
        body = {"payload": {"other": 1}}
        with self.assertRaises(KeyError):
            _oracle_favourites(copy.deepcopy(body))
        got = _run(
            _call(FakeConnection(copy.deepcopy(body))).load_favourites(FakeAppliance())
        )
        self.assertEqual(got, [])

    def test_empty_or_non_dict_bodies_safe(self) -> None:
        for body in ({}, None, [], "x", 7):
            with self.subTest(body=body):
                c = _call(FakeConnection(body))
                self.assertEqual(_run(c.load_command_history(FakeAppliance())), [])
                self.assertEqual(_run(_call(FakeConnection(body)).load_favourites(FakeAppliance())), [])
                self.assertEqual(_run(_call(FakeConnection(body)).load_last_activity(FakeAppliance())), {})
                self.assertEqual(_run(_call(FakeConnection(body)).load_appliance_data(FakeAppliance())), {})
                self.assertEqual(_run(_call(FakeConnection(body)).load_attributes(FakeAppliance())), {})
                self.assertEqual(_run(_call(FakeConnection(body)).load_statistics(FakeAppliance())), {})
                self.assertEqual(_run(_call(FakeConnection(body)).load_maintenance(FakeAppliance())), {})
                self.assertEqual(_run(_call(FakeConnection(body)).load_aws_token()), "")

    def test_appliance_data_payload_non_dict_safe(self) -> None:
        body = {"payload": "x"}  # pyhOn -> AttributeError ("x".get)
        with self.assertRaises(AttributeError):
            _oracle_appliance_data(copy.deepcopy(body))
        got = _run(
            _call(FakeConnection(copy.deepcopy(body))).load_appliance_data(FakeAppliance())
        )
        self.assertEqual(got, {})


class SendCommandTest(unittest.TestCase):
    def _patch_clock(self, iso_micro: str):
        """Pin api._command_timestamp's clock to a known naive instant (real datetime)."""
        from datetime import datetime as _dt

        fixed = _dt.fromisoformat(iso_micro)  # naive

        class _Frozen:
            @staticmethod
            def now(tz=None):
                return fixed

        real = api_mod.datetime
        api_mod.datetime = _Frozen
        self.addCleanup(lambda: setattr(api_mod, "datetime", real))

    def test_send_command_body_exact(self) -> None:
        self._patch_clock("2026-06-18T12:34:56.789012")
        conn = FakeConnection({"payload": {"resultCode": "0"}})
        app = FakeAppliance()
        ok = _run(
            _call(conn).send_command(
                app,
                "setParameters",
                {"tempSelZ1": 4},
                {"anc": 1},
            )
        )
        self.assertTrue(ok)
        verb, url, kwargs = conn.calls[0]
        self.assertEqual(verb, "POST")
        self.assertEqual(url, f"{API_URL}/commands/v1/send")
        data = kwargs["json"]
        ts = "2026-06-18T12:34:56.789Z"  # [:-3] cuts micros to millis + "Z"
        self.assertEqual(data["timestamp"], ts)
        self.assertEqual(data["transactionId"], f"{app.mac_address}_{ts}")
        self.assertEqual(data["macAddress"], app.mac_address)
        self.assertEqual(data["commandName"], "setParameters")
        self.assertEqual(data["applianceOptions"], {"opt": 1})
        self.assertEqual(data["device"], _device.HonDevice("pyhOn").payload(mobile=True))
        self.assertIn("mobileOs", data["device"])  # device payload mobile=True
        self.assertEqual(
            data["attributes"],
            {"channel": "mobileApp", "origin": "standardProgram", "energyLabel": "0"},
        )
        self.assertEqual(data["ancillaryParameters"], {"anc": 1})
        self.assertEqual(data["parameters"], {"tempSelZ1": 4})
        self.assertEqual(data["applianceType"], "REF")
        self.assertNotIn("programName", data)  # not startProgram

    def test_send_command_start_program_adds_program_name(self) -> None:
        self._patch_clock("2026-06-18T12:34:56.789012")
        conn = FakeConnection({"payload": {"resultCode": "0"}})
        _run(
            _call(conn).send_command(
                FakeAppliance(), "startProgram", {}, {}, program_name="super_cool"
            )
        )
        self.assertEqual(conn.calls[0][2]["json"]["programName"], "SUPER_COOL")

    def test_send_command_start_program_without_name_no_key(self) -> None:
        self._patch_clock("2026-06-18T12:34:56.789012")
        conn = FakeConnection({"payload": {"resultCode": "0"}})
        _run(_call(conn).send_command(FakeAppliance(), "startProgram", {}, {}))
        self.assertNotIn("programName", conn.calls[0][2]["json"])

    def test_send_command_failure_results(self) -> None:
        for body in (
            {"payload": {"resultCode": "1"}},
            {"payload": {}},
            {"payload": None},
            {},
            None,
        ):
            with self.subTest(body=body):
                self._patch_clock("2026-06-18T12:34:56.789012")
                conn = FakeConnection(body)
                ok = _run(_call(conn).send_command(FakeAppliance(), "x", {}, {}))
                self.assertFalse(ok)

    def test_send_command_failure_redacts_identity_in_logs(self) -> None:
        # #23: on failure the request payload (macAddress, transactionId=MAC,
        # device.mobileId) must NOT be logged in cleartext; only command+resultCode
        # at ERROR, the redacted payload at DEBUG.
        self._patch_clock("2026-06-18T12:34:56.789012")
        conn = FakeConnection({"payload": {"resultCode": "7"}}, mobile_id="SECRET_MOBILE")
        app = FakeAppliance()  # mac_address = "AA:BB:CC:DD:EE:FF"
        logger = "custom_components.addhon.client.transport.api"
        with self.assertLogs(logger, level="DEBUG") as cm:
            ok = _run(_call(conn).send_command(app, "setParameters", {"tempSelZ1": 4}, {}))
        self.assertFalse(ok)
        blob = "\n".join(cm.output)
        self.assertNotIn(app.mac_address, blob)   # mac (also covers transactionId=<mac>_<ts>)
        self.assertNotIn("SECRET_MOBILE", blob)   # device.mobileId (nested)
        errors = "\n".join(r.getMessage() for r in cm.records if r.levelno == logging.ERROR)
        self.assertIn("setParameters", errors)    # command in the ERROR line
        self.assertIn("7", errors)                # resultCode in the ERROR line
        self.assertIn("***", blob)                # redaction marker


class CommandTimestampTest(unittest.TestCase):
    def test_format_millis_and_z(self) -> None:
        ts = api_mod._command_timestamp()
        # ISO with milliseconds (3 digits) + Z, no timezone offset
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertNotIn("+", ts)

    def _frozen(self, iso_micro: str):
        """Patch the clock so that now(utc).replace(tzinfo=None) has known microseconds."""
        from datetime import datetime as _dt

        fixed = _dt.fromisoformat(iso_micro)  # naive

        class _Frozen:
            @staticmethod
            def now(tz=None):
                return fixed

        real = api_mod.datetime
        api_mod.datetime = _Frozen
        self.addCleanup(lambda: setattr(api_mod, "datetime", real))

    def test_iso8601_millis_utc_z_on_common_path(self) -> None:
        # sec8: the transactionId timestamp is ISO-8601 UTC with exactly 3-digit
        # milliseconds and a 'Z' zone suffix. Oracle = ISO-8601, not pyhOn.
        self._frozen("2026-06-18T12:34:56.789012")
        self.assertEqual(api_mod._command_timestamp(), "2026-06-18T12:34:56.789Z")

    def test_truncates_not_rounds_at_boundary(self) -> None:
        # Guard: timespec="milliseconds" TRUNCATES, it does not round:
        # .789999 -> .789Z (not .790Z). Hardens against a future change of semantics.
        self._frozen("2026-06-18T12:34:56.789999")
        self.assertEqual(api_mod._command_timestamp(), "2026-06-18T12:34:56.789Z")

    def test_millis_always_three_digits_when_microsecond_zero(self) -> None:
        # sec8: milliseconds are ALWAYS 3 digits, even at .000 -- the seconds are never
        # lost. (A naive strip of the last 3 microsecond digits would drop ':56' here;
        # timespec="milliseconds" keeps the full ISO-8601 shape.)
        self._frozen("2026-06-18T12:34:56")
        self.assertEqual(api_mod._command_timestamp(), "2026-06-18T12:34:56.000Z")


class ApiIntentionalNarrowingTest(unittest.TestCase):
    """Pins the INTENTIONAL DIVERGENCES: on malformed shapes where pyhOn would return
    a non-dict/non-list value (or crash downstream), we narrow to the safe empty
    default. Explicit test so a future refactor does not change them silently."""

    def test_last_activity_non_dict_attributes_narrowed(self) -> None:
        # pyhOn would return the raw value (str/list/int); we -> {}.
        for attrs in ("str", [1, 2], 5):
            with self.subTest(attrs=attrs):
                self.assertEqual(_oracle_last_activity({"attributes": attrs}), attrs)
                got = _run(
                    _call(FakeConnection({"attributes": attrs})).load_last_activity(
                        FakeAppliance()
                    )
                )
                self.assertEqual(got, {})

    def test_history_non_list_narrowed(self) -> None:
        body = {"payload": {"history": "notalist"}}
        self.assertEqual(_oracle_history(copy.deepcopy(body)), "notalist")
        got = _run(_call(FakeConnection(body)).load_command_history(FakeAppliance()))
        self.assertEqual(got, [])

    def test_favourites_non_list_narrowed(self) -> None:
        body = {"payload": {"favourites": {"a": 1}}}
        self.assertEqual(_oracle_favourites(copy.deepcopy(body)), {"a": 1})
        got = _run(_call(FakeConnection(body)).load_favourites(FakeAppliance()))
        self.assertEqual(got, [])

    def test_appliance_data_non_dict_model_narrowed(self) -> None:
        body = {"payload": {"applianceModel": [1]}}
        self.assertEqual(_oracle_appliance_data(copy.deepcopy(body)), [1])
        got = _run(_call(FakeConnection(body)).load_appliance_data(FakeAppliance()))
        self.assertEqual(got, {})

    def test_attributes_inner_non_dict_narrowed(self) -> None:
        # branch relevant to the live flow (appliance.load_attributes does |= attributes):
        # pyhOn would return None/str/list (and crash downstream on .pop), we -> {}.
        for payload in (None, "x", [1]):
            with self.subTest(payload=payload):
                self.assertEqual(_oracle_payload({"payload": payload}), payload)
                got = _run(
                    _call(FakeConnection({"payload": payload})).load_attributes(
                        FakeAppliance()
                    )
                )
                self.assertEqual(got, {})

    def test_aws_token_non_str_narrowed(self) -> None:
        body = {"payload": {"tokenSigned": 123}}
        self.assertEqual(_oracle_aws_token(copy.deepcopy(body)), 123)
        got = _run(_call(FakeConnection(body)).load_aws_token())
        self.assertEqual(got, "")


_NO_CT = object()


class _StrictResponse(FakeResponse):
    """json() requires content_type=None to be passed explicitly; otherwise it
    raises, simulating aiohttp's ContentTypeError on a wrong Content-Type."""

    async def json(self, content_type=_NO_CT):
        if content_type is _NO_CT:
            raise AssertionError("response.json() called without content_type=None")
        return self._body


class _StrictConnection(FakeConnection):
    def _ctx(self, method, url, kwargs):
        return _ReqCtx(
            self, method, url, kwargs, _StrictResponse(self._body, self._text, self._status)
        )


class ContentTypeTest(unittest.TestCase):
    """#8: every api.py call-site must pass content_type=None (the cloud sometimes
    returns valid JSON with a non-JSON Content-Type)."""

    def test_load_appliances_passes_content_type_none(self) -> None:
        body = {"modules": {"applianceList": {"payload": {"appliances": [{"a": 1}]}}}}
        # Would raise if load_appliances called .json() without content_type=None.
        _run(_call(_StrictConnection(body)).load_appliances())

    def test_load_commands_passes_content_type_none(self) -> None:
        body = {"payload": {"resultCode": "0"}}
        app = FakeAppliance(eepromId="EE", fwVersion="1.2", series="S")
        _run(_call(_StrictConnection(body)).load_commands(app))

    def test_send_command_passes_content_type_none(self) -> None:
        # Write path: the cloud's non-JSON Content-Type response motivated #8.
        conn = _StrictConnection({"payload": {"resultCode": "0"}})
        _run(_call(conn).send_command(FakeAppliance(), "setParameters", {"x": 1}, {}))

    def test_load_attributes_passes_content_type_none(self) -> None:
        _run(_call(_StrictConnection({})).load_attributes(FakeAppliance()))

    def test_load_statistics_passes_content_type_none(self) -> None:
        _run(_call(_StrictConnection({})).load_statistics(FakeAppliance()))


# --------------------------------------------------------------------------- #
# The appliance-list fetch census                                              #
# --------------------------------------------------------------------------- #
class _RaisingCtx:
    """A request context that dies before there is a body, like connection.py does on
    429, on any >= 500 and on a non-JSON body (a CDN or maintenance page)."""

    def __init__(self, conn, method, url, kwargs, error) -> None:
        self._conn = conn
        self._method = method
        self._url = url
        self._kwargs = kwargs
        self._error = error

    async def __aenter__(self):
        self._conn.calls.append((self._method, self._url, self._kwargs))
        raise self._error

    async def __aexit__(self, *a):
        return False


class _RaisingConnection(FakeConnection):
    def __init__(self, error, **kwargs) -> None:
        super().__init__(None, **kwargs)
        self._error = error

    def _ctx(self, method, url, kwargs):
        return _RaisingCtx(self, method, url, kwargs, self._error)


class _NoStatusResponse:
    """A duck-typed response with no `status` attribute at all.

    Every response double in this suite grew one when the census landed; this one
    deliberately did not, because the field the census reads must be optional. A
    diagnostic field that can abort a setup is worse than the missing diagnosis.
    """

    def __init__(self, body) -> None:
        self._body = body

    async def json(self, content_type=None):
        return self._body


class _NoStatusConnection(FakeConnection):
    def _ctx(self, method, url, kwargs):
        return _ReqCtx(self, method, url, kwargs, _NoStatusResponse(self._body))


class FetchCensusTest(unittest.TestCase):
    """`load_appliances` records what the ONE appliance-list call did, on BOTH paths.

    The live case this exists for: the cloud answered with `result` keys
    ['executionTime', 'modules', 'success'] and `modules` keys ['applianceList'], the
    hOn app showed two appliances, and the dump said `"appliances": []` with
    `"last_error": null`. Nothing in that file said whether the list was empty, whether
    our walk stopped early, or whether the call reached a body at all. The census is
    what answers that, and it has to survive the raising path too -- an exception
    leaves the field untouched otherwise, which reads exactly like "no session".
    """

    def test_a_successful_fetch_records_status_and_probe(self) -> None:
        body = {
            "modules": {"applianceList": {"payload": {"appliances": [{"a": 1}, {"b": 2}]}}}
        }
        api = _call(FakeConnection(body))
        # Nothing recorded before the call: the field starts as None so the diagnostics
        # reader can tell "this session never fetched" from "this session fetched".
        self.assertIsNone(api.last_appliance_fetch)
        _run(api.load_appliances())
        census = api.last_appliance_fetch
        self.assertEqual(200, census["status"])
        self.assertEqual("ok", census["outcome"])
        self.assertEqual(2, census["count"])
        self.assertIsNone(census["code"])
        self.assertIsNone(census["stopped_at"])
        # Aware UTC, so the diagnostics layer can subtract it from its own clock.
        self.assertIsNotNone(census["at"].utcoffset())

    def test_a_non_200_with_a_json_body_records_its_status(self) -> None:
        # Root cause (C): the endpoint moved. connection.py re-raises only on 429 and
        # >= 500, so a 404 with a JSON body is delivered here as a success and the
        # parser reports an empty account. The status is the whole diagnosis.
        api = _call(FakeConnection({"message": "Not Found"}, status=404))
        with self.assertLogs(
            "custom_components.addhon.client.transport.api", level="WARNING"
        ):
            self.assertEqual([], _run(api.load_appliances()))
        census = api.last_appliance_fetch
        self.assertEqual(404, census["status"])
        self.assertEqual("missing_key", census["outcome"])
        self.assertEqual("modules", census["stopped_at"])
        self.assertIsNone(census["count"])

    def test_a_raising_fetch_still_records_a_census(self) -> None:
        # Root cause (E): the call never reached a body. The exception must still
        # propagate untouched -- the census is a bystander, not a handler.
        from custom_components.addhon.error_codes import classify

        error = RuntimeError("hOn server error (status 503)")
        api = _call(_RaisingConnection(error))
        with self.assertRaises(RuntimeError) as caught:
            _run(api.load_appliances())
        self.assertIs(error, caught.exception)
        census = api.last_appliance_fetch
        self.assertEqual("raised", census["outcome"])
        self.assertEqual(classify(error).label, census["code"])
        self.assertRegex(census["code"], r"^ADDHON-[0-9]{3}$")
        # No body was reached, so nothing downstream of it can be reported.
        self.assertIsNone(census["status"])
        self.assertIsNone(census["count"])
        self.assertIsNone(census["node_type"])

    def test_a_response_without_a_status_attribute_does_not_raise(self) -> None:
        body = {"modules": {"applianceList": {"payload": {"appliances": [{"a": 1}]}}}}
        api = _call(_NoStatusConnection(body))
        self.assertEqual([{"a": 1}], _run(api.load_appliances()))
        self.assertIsNone(api.last_appliance_fetch["status"])
        self.assertEqual("ok", api.last_appliance_fetch["outcome"])

    def test_the_census_carries_no_cloud_string(self) -> None:
        # The leak test on the writer side: this dict is published verbatim into a
        # document users paste into public issues, so not one string of it may come
        # from the response. `default=str` so the datetime does not hide a failure to
        # serialize behind a TypeError.
        import json

        body = {
            "AA:BB:CC:DD:EE:FF": "user@example.com",
            "modules": {
                "applianceList": {
                    "payload": {
                        "appliances": [
                            {
                                "macAddress": "AA:BB:CC:DD:EE:FF",
                                "serialNumber": "PLAINTEXT-SERIAL",
                                "nickName": "Kitchen Washer",
                            }
                        ]
                    }
                }
            },
        }
        api = _call(FakeConnection(body))
        _run(api.load_appliances())
        blob = json.dumps(api.last_appliance_fetch, default=str)
        for leak in (
            "AA:BB:CC:DD:EE:FF",
            "user@example.com",
            "PLAINTEXT-SERIAL",
            "Kitchen Washer",
        ):
            self.assertNotIn(leak, blob, leak)

    def test_both_paths_stamp_an_instant_the_dump_can_render(self) -> None:
        """WHEN the fetch ran, on both paths, carried end to end into the block.

        Without the stamp the block says the cloud sent an empty list without saying
        whether that was six seconds or six days ago -- that is, without saying whether
        asking the reporter for a reload would change anything. `generated_at` dates the
        DOWNLOAD, not the fetch, and in a dump whose account came back empty it is the only
        instant left in the document: every other one lives inside an appliance block,
        and there are none.

        BOTH paths are asserted because `load_appliances` builds two SEPARATE dict
        literals (api.py:98-107 for the raising branch, :115-120 for the success branch)
        and
        nothing else in this suite reads `at` on the raising one. A stamp dropped there,
        or written as a naive `datetime.now()`, is refused by `_as_utc(...,
        assume_naive_utc=False)` (diagnostics.py:1259-1260), which blanks `at` AND
        `age_s` on exactly the root cause where the age decides what to do next: a 503
        six days old is a census nobody should act on, a 503 six seconds old is an
        outage in progress.

        The READER half runs here and not in tests/test_diagnostics.py because that file
        stubs only the `homeassistant.*` tree and deliberately provides no `yarl` (its
        comment at :844-845), so it cannot import this transport at all; this file stubs
        aiohttp and yarl already, so it is the only place where the instant the writer
        stamps meets the real `_stamp_text` -> `_age_seconds` path. Split across the two
        files both halves stay green while the field renders `null`: every writer test
        above reads `census["at"]` as a datetime object, and every reader test in
        test_diagnostics.py hand-writes the instant it then reads back.

        The api object stands in for the client because the accessor chain forwards this
        very dict without copying it: `HonApi.last_appliance_fetch` ->
        `NativeHon.last_appliance_fetch` (session.py:654) -> `HonClient`
        (hon_client.py:853).
        """
        from datetime import timedelta
        from types import SimpleNamespace

        from custom_components.addhon import diagnostics
        from custom_components.addhon.const import DOMAIN

        body = {"modules": {"applianceList": {"payload": {"appliances": [{"a": 1}]}}}}
        served = _call(FakeConnection(body))
        _run(served.load_appliances())
        refused = _call(_RaisingConnection(RuntimeError("hOn server error (status 503)")))
        with self.assertRaises(RuntimeError):
            _run(refused.load_appliances())

        for path, api in (("served", served), ("refused", refused)):
            with self.subTest(path=path):
                stamped = api.last_appliance_fetch["at"]
                # Aware UTC at the source. An offset-carrying instant is what the reader
                # is allowed to subtract from a clock of its own, and normalising to
                # +00:00 is also what keeps the emitted text from matching `_MAC_RE` --
                # `_stamp_text` measured '2026-04-09T12:34:56-05:30:15' surviving
                # `_jsonable` as '2026-04-09T***' (diagnostics.py:1286-1292).
                self.assertEqual(timedelta(0), stamped.utcoffset())
                hass = SimpleNamespace(data={DOMAIN: {"e1": {"client": api}}})
                block = diagnostics._last_fetch(
                    hass,
                    SimpleNamespace(entry_id="e1"),
                    now=stamped + timedelta(seconds=51_840),
                )
                self.assertEqual("recorded", block["state"])
                # Equality with the writer's own isoformat, not merely "not None": it is
                # what proves the real stamp clears `_ISO_RE` and the 40-character cap
                # instead of being dropped by them.
                self.assertEqual(stamped.isoformat(), block["at"])
                self.assertTrue(block["at"].endswith("+00:00"), block["at"])
                self.assertEqual(51_840, block["age_s"])


# --------------------------------------------------------------------------- #
# The identity self-check                                                      #
# --------------------------------------------------------------------------- #
def _id_token(person_account_id) -> str:
    """An id_token shaped like the live one, carrying `person_account_id`.

    The nine `custom_attributes` and the top-level claims are the ones observed on
    2026-08-24 (apk/analysis/addhon210-healthy-envelope-baseline.md): the reader has to
    find its claim among neighbours, not alone in an otherwise empty object.
    """
    claims = {
        "sub": "SYNTHETIC-SUB",
        "email": "user@example.com",
        "exp": 1_800_000_000,
        "custom_attributes": {
            "Country": "IT",
            "EulaUpdateRequired": "false",
            "ExternalSource": "hOn",
            "ExternalSubSource": "app",
            "OemAppId": "haier",
            "PersonContactId": "CONTACT-OURS",
            "PrivacyUpdated": "true",
            "UserLanguage": "it",
        },
    }
    if person_account_id is not None:
        claims["custom_attributes"]["PersonAccountId"] = person_account_id
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.sig"


class _AuthedConnection(FakeConnection):
    """A connection whose `auth` holds an id_token, as the live one does.

    Every other double in this file has NO `auth` at all -- `HonConnection.auth` raises
    when the connection was never created -- which is exactly the state the census
    guard is written for, and exactly why a double that does carry one is needed for
    the verdict to be anything but `no_claim`.
    """

    def __init__(self, body, id_token, **kwargs) -> None:
        super().__init__(body, **kwargs)
        self.auth = types.SimpleNamespace(id_token=id_token)


class AccountMatchTest(unittest.TestCase):
    """`account_match` answers whose appliances these are, as a verdict.

    The question ADDHON-210 kept needing and nothing could answer: a session that
    silently resolves to another account returns 200, a well-formed envelope, and a
    list that is empty or simply not ours -- and every other field of the dump reads
    like an account that owns nothing.
    """

    def setUp(self) -> None:
        self.match = api_mod.account_match

    def test_the_healthy_capture_matches(self) -> None:
        self.assertEqual("match", self.match(healthy(), OUR_ACCOUNT))

    def test_the_reporter_envelope_separates_empty_from_unreadable(self) -> None:
        # THE acceptance case, and the reason this function walks the response itself
        # instead of calling `parse_appliance_list`: that parser returns [] for an
        # empty list AND for a drift its fail-safe cannot follow (sec9), which is the
        # very distinction between these two verdicts.
        self.assertEqual("no_appliances", self.match(reporter(), OUR_ACCOUNT))
        # A level that went missing: the walk never reaches a list at all.
        drifted = reporter()
        del drifted["modules"]["applianceList"]["payload"]
        self.assertEqual("unknown", self.match(drifted, OUR_ACCOUNT))
        # And the case that separates the two BRANCHES rather than the two responses:
        # the walk completes and the leaf is not a list. `no_appliances` claims the
        # cloud said "you own nothing"; only an actual empty list says that, and a
        # single `not isinstance(node, list) or not node` would quietly report the
        # cloud's silence as its answer. Both falsy and truthy non-lists, because a
        # falsy one is what a merged condition swallows.
        for leaf in ({"x": 1}, "x", 0, None, {}, ""):
            with self.subTest(leaf=leaf):
                bent = reporter()
                bent["modules"]["applianceList"]["payload"]["appliances"] = leaf
                self.assertEqual("unknown", self.match(bent, OUR_ACCOUNT))

    def test_a_foreign_account_is_a_mismatch_not_an_empty_account(self) -> None:
        theirs = healthy()
        theirs["modules"]["applianceList"]["payload"]["appliances"][0][
            "sfPersonAccountId"
        ] = OTHER_ACCOUNT
        self.assertEqual("mismatch", self.match(theirs, OUR_ACCOUNT))

    def test_a_response_spanning_two_accounts_is_mixed(self) -> None:
        both = healthy()
        appliances = both["modules"]["applianceList"]["payload"]["appliances"]
        appliances.append({**appliances[0], "sfPersonAccountId": OTHER_ACCOUNT})
        self.assertEqual("mixed", self.match(both, OUR_ACCOUNT))

    def test_no_claim_wins_over_every_other_verdict(self) -> None:
        # "Our own token carries no identity" says the problem may be on our side of
        # the wire, so it is reported ahead of anything read off the response -- and
        # ahead of `no_appliances`, which the field report would otherwise show while
        # the real finding was that no comparison happened at all.
        for response in (healthy(), reporter(), None, {"modules": 3}):
            with self.subTest(response=type(response).__name__):
                for claim in (None, "", 42, b"ACCOUNT-OURS", ["ACCOUNT-OURS"]):
                    self.assertEqual("no_claim", self.match(response, claim))

    def test_appliances_with_no_readable_owner_do_not_vote(self) -> None:
        # A missing or non-string `sfPersonAccountId` is a SCHEMA question, and the
        # schema questions are what the probe already answers. Counting it as a
        # mismatch would report an identity failure for a renamed key.
        silent = healthy()
        appliances = silent["modules"]["applianceList"]["payload"]["appliances"]
        appliances.append({"macAddress": "AA:BB:CC:DD:EE:FF"})
        appliances.append({"sfPersonAccountId": {"nested": "object"}})
        appliances.append({"sfPersonAccountId": ""})
        appliances.append("not-a-dict")
        self.assertEqual("match", self.match(silent, OUR_ACCOUNT))

    def test_an_all_silent_list_is_unknown_and_never_a_match(self) -> None:
        # The vote must not be won by acclamation: with nothing readable the honest
        # answer is that the check did not run.
        nameless = healthy()
        nameless["modules"]["applianceList"]["payload"]["appliances"] = [
            {"macAddress": "AA:BB:CC:DD:EE:FF"},
            {"sfPersonAccountId": 42},
        ]
        self.assertEqual("unknown", self.match(nameless, OUR_ACCOUNT))

    def test_a_string_subclass_owner_cannot_forge_a_match(self) -> None:
        # The single most misleading thing this field could say. `isinstance` admits a
        # str SUBCLASS, and one whose `__eq__` returns True equals our account id while
        # its characters are someone else's -- turning the mismatch that explains the
        # whole report into a clean bill of health. The guard is `type(v) is str`.
        class Sneaky(str):
            def __eq__(self, other):  # pragma: no cover - only if the guard fails
                return True

            def __hash__(self):
                return hash(str(self))

        forged = healthy()
        forged["modules"]["applianceList"]["payload"]["appliances"][0][
            "sfPersonAccountId"
        ] = Sneaky(OTHER_ACCOUNT)
        self.assertEqual("unknown", self.match(forged, OUR_ACCOUNT))

    def test_every_verdict_is_a_declared_token_and_it_never_raises(self) -> None:
        # It runs inside NativeHon.setup(): an exception here does not spoil a
        # diagnostic field, it takes the config entry down. And a verdict outside the
        # declared tuple would print as "other" in the dump, which for an identity
        # question is the one answer nobody can act on.
        class Hostile(dict):
            def __contains__(self, key):
                raise RuntimeError("hostile mapping")

        responses = [
            healthy(), reporter(), None, [], "", 0, {}, {"modules": []},
            {"modules": {"applianceList": {"payload": {"appliances": "x"}}}},
            Hostile({"modules": {}}),
            {"modules": {"applianceList": {"payload": {"appliances": [None, 1, "x"]}}}},
        ]
        seen = set()
        for response in responses:
            for claim in (OUR_ACCOUNT, OTHER_ACCOUNT, None):
                with self.subTest(response=type(response).__name__, claim=claim):
                    verdict = self.match(response, claim)
                    self.assertIn(verdict, api_mod.ACCOUNT_TOKENS)
                    seen.add(verdict)
        # The guard has a producer: without it the hostile mapping raises out of a
        # setup instead of answering.
        self.assertEqual("unknown", self.match(Hostile({"modules": {}}), OUR_ACCOUNT))
        self.assertLessEqual({"match", "mismatch", "no_appliances", "no_claim",
                              "unknown"}, seen)

    def test_the_verdict_carries_no_identifier(self) -> None:
        # Both values compared here are account ids. Neither may appear in the answer:
        # the whole reason the comparison happens in the transport is that only the
        # verdict is allowed to cross into a document a user pastes into a public issue.
        hostile = healthy()
        hostile["modules"]["applianceList"]["payload"]["appliances"][0][
            "sfPersonAccountId"
        ] = "3C:71:BF:AA:BB:CC"
        for claim in (OUR_ACCOUNT, "user@example.com"):
            verdict = self.match(hostile, claim)
            self.assertNotIn("3C:71:BF:AA:BB:CC", verdict)
            self.assertNotIn("example.com", verdict)
            self.assertNotIn(OUR_ACCOUNT, verdict)


class FetchCensusEnvelopeTest(unittest.TestCase):
    """The census carries the envelope and the identity check, end to end.

    Written against the two envelopes of the investigation (`tests/_envelopes.py`):
    the healthy one captured live on 2026-08-24, and the reporter's, whose two
    recorded levels are identical to it. Everything the dump could already say about
    those two responses was the same; this is the pin that it no longer is.
    """

    def _census(self, body, claim=OUR_ACCOUNT, **kwargs):
        api = _call(_AuthedConnection(body, _id_token(claim), **kwargs))
        _run(api.load_appliances())
        return api.last_appliance_fetch

    def test_the_warning_carries_the_whole_diagnosis_without_debug(self) -> None:
        """The empty-list WARNING is a finished diagnosis, at a level everyone sees.

        This branch is the hardest report in the project to act on, and asking for a
        downloaded dump has repeatedly failed to produce one: the appliance-list call
        happens ONCE, during setup, so a reporter who enables debug afterwards captures
        nothing, and the raw response used to live at DEBUG behind exactly that trap.

        So the census travels with the warning. Everything asserted here is a token or
        a bounded int produced by OUR code -- `probe_appliance_list` and
        `account_match` -- never a value chosen by the cloud, which is what lets a
        WARNING carry it into a public issue unedited.
        """
        with self.assertLogs(
            "custom_components.addhon.client.transport.api", level="WARNING"
        ) as logs:
            self._census(reporter())
        summary = next(line for line in logs.output if "ADDHON-210" in line)
        for fragment in (
            "envelope_ok=True",
            "module_ok=True",
            "auth_keys=0",
            "account=no_appliances",
        ):
            self.assertIn(fragment, summary)
        # And the response SHAPE is emitted at WARNING too: it is the one artefact
        # that answers what is UNDER `payload`, which no census field can.
        shape = next(
            (line for line in logs.output if "appliance response shape" in line), None
        )
        self.assertIsNotNone(shape, logs.output)
        self.assertIn("<list of 0>", shape)

    def test_the_shape_line_cannot_carry_a_bearer_token(self) -> None:
        """The reason it is a shape and not a redacted body.

        `redact_identity` matches key NAMES against a set, and no set enumerates a
        vendor's naming: `token` was in it and `cognitoTokenNew` was not. That key is
        the replacement cognito credential the aggregator returns inside
        `modules.applianceList.authInfo`, and THIS branch is where the response is
        logged -- at a level this project asks reporters to paste into public issues.
        """
        poisoned = reporter()
        poisoned["modules"]["applianceList"]["authInfo"] = {
            "cognitoTokenNew": "eyJhbGciOiJIUzI1NiJ9.CANARY-BEARER-CREDENTIAL.sig",
            "sfPersonAccountId": "0011q00001CANARYAAA",
        }
        with self.assertLogs(
            "custom_components.addhon.client.transport.api", level="WARNING"
        ) as logs:
            self._census(poisoned)
        blob = "\n".join(logs.output)
        self.assertNotIn("CANARY-BEARER-CREDENTIAL", blob)
        self.assertNotIn("0011q00001CANARYAAA", blob)
        # ...and the finding still travels: the census counts the keys it refused to name.
        self.assertIn("auth_keys=2", blob)

    def test_the_summary_key_names_are_filtered_too(self) -> None:
        """`sorted(result.keys())` reads as obviously safe and is not.

        A mapping the vendor keyed BY an identity carries it in the key position, and
        this summary goes to the level meant for pasting into public issues. The shape
        line below it was fixed first; a summary that stayed raw would have leaked the
        same value one line earlier.
        """
        poisoned = reporter()
        poisoned["user#eu-west-1:CANARY-IDENTITY"] = {"x": 1}
        poisoned["modules"]["0011q00001CANARYAAA"] = {"y": 2}
        with self.assertLogs(
            "custom_components.addhon.client.transport.api", level="WARNING"
        ) as logs:
            self._census(poisoned)
        blob = "\n".join(logs.output)
        self.assertNotIn("CANARY-IDENTITY", blob)
        self.assertNotIn("0011q00001CANARYAAA", blob)
        # The real schema keys still print, or the summary stops being a summary.
        self.assertIn("modules", blob)
        self.assertIn("applianceList", blob)

    def test_a_failed_module_says_so_in_the_warning(self) -> None:
        # The reason the flag is worth logging at all: with `success: false` the list
        # is empty for a reason that is NOT an empty account, and until now the two
        # produced the same line.
        broken = reporter()
        broken["modules"]["applianceList"]["success"] = False
        with self.assertLogs(
            "custom_components.addhon.client.transport.api", level="WARNING"
        ) as logs:
            self._census(broken)
        summary = next(line for line in logs.output if "ADDHON-210" in line)
        self.assertIn("module_ok=False", summary)
        self.assertIn("envelope_ok=True", summary)

    def test_a_body_that_fights_back_never_takes_the_setup_down(self) -> None:
        """The last unguarded stretch of `load_appliances`, closed end to end.

        Three separate calls on the response object stood between a hostile body and a
        surviving setup: `parse_appliance_list` walking with `.get`, the ADDHON-210
        message reading `.keys()` to name the levels, and the redacted DEBUG dump. All
        three ran on an object the CLOUD supplied, and the middle one would have raised
        while building the very message that exists to explain the failure.

        The input is a dict subclass raising from `get`. Not reachable from
        `json.loads`, which is precisely why the old reasoning held that none of this
        could raise: the input is `await resp.json(content_type=None)` on a duck-typed
        response, so "a json.loads output is safe" and "this cannot abort a setup" are
        different claims, and only the second one matters inside `NativeHon.setup()`.

        What must survive is not just the absence of an exception: the census still has
        to say something, and ADDHON-210 still has to be logged, because that code is
        what a reporter is told to search for.
        """

        class _HostileGetBody(dict):
            def get(self, *args, **kwargs):
                raise RuntimeError("hostile mapping")

        with self.assertLogs(
            "custom_components.addhon.client.transport.api", level="WARNING"
        ) as logs:
            census = self._census(_HostileGetBody(healthy()))
        self.assertEqual([], _run(_call(_AuthedConnection(
            _HostileGetBody(healthy()), _id_token(OUR_ACCOUNT))).load_appliances()))
        # ADDHON-210 is still emitted, and the key names degrade to `unreadable`
        # rather than to the `n/a` that means "the shape was wrong": the two are
        # different findings and the log keeps them apart.
        self.assertIn("ADDHON-210", logs.output[0])
        self.assertIn("unreadable", logs.output[0])
        # The census is still a census. Note WHICH census: the probe walks with
        # `key in node` + `node[key]` and this subclass only poisons `get`, so the
        # probe sails through and reports the healthy walk it genuinely performed.
        # The result is a dump saying `count: 1` beside `"appliances": []`, and that
        # pair is not a contradiction to paper over -- it is the signature of this
        # exact failure, and the only one that names it. A response where the walk
        # finds a list and the parse cannot return it is neither "the cloud sent
        # nothing" nor "the chain changed", and before this it looked like the first.
        self.assertEqual("ok", census["outcome"])
        self.assertEqual(1, census["count"])
        self.assertEqual(200, census["status"])

    def test_the_healthy_capture_reads_as_healthy(self) -> None:
        census = self._census(healthy())
        self.assertEqual(200, census["status"])
        self.assertEqual("ok", census["outcome"])
        self.assertEqual(1, census["count"])
        self.assertIs(True, census["envelope_ok"])
        self.assertIs(True, census["module_ok"])
        self.assertEqual(0, census["auth_keys"])
        self.assertEqual("match", census["account"])

    def test_the_reporter_envelope_is_an_account_that_owns_nothing(self) -> None:
        # The verdict the field report deserved: the cloud declared success at BOTH
        # levels, the session resolved to the account we authenticated as, and that
        # account has no appliances. That is a finished diagnosis, and none of it was
        # readable from a dump before.
        with self.assertLogs(
            "custom_components.addhon.client.transport.api", level="WARNING"
        ):
            census = self._census(reporter())
        self.assertEqual(0, census["count"])
        self.assertIs(True, census["envelope_ok"])
        self.assertIs(True, census["module_ok"])
        self.assertEqual("no_appliances", census["account"])

    def test_a_failed_module_stops_looking_like_an_empty_account(self) -> None:
        # THE case the two flags exist for. `modules.applianceList.success: false` with
        # an empty payload is a state in which the official app shows zero appliances
        # too -- it does not read the flag either -- and in which every other field of
        # the census is byte-identical to a legitimately empty account.
        failed = reporter()
        failed["modules"]["applianceList"]["success"] = False
        with self.assertLogs(
            "custom_components.addhon.client.transport.api", level="WARNING"
        ):
            broken = self._census(failed)
        with self.assertLogs(
            "custom_components.addhon.client.transport.api", level="WARNING"
        ):
            legit = self._census(reporter())
        self.assertNotEqual(
            {k: v for k, v in broken.items() if k != "at"},
            {k: v for k, v in legit.items() if k != "at"},
        )
        self.assertIs(False, broken["module_ok"])
        self.assertIs(True, legit["module_ok"])

    def test_a_rotated_cognito_token_shows_up_as_a_key_count(self) -> None:
        # `authInfo` is empty on every healthy session captured, so it is a channel
        # that stays silent when things work: any content at all is a signal, and the
        # count is the whole of what may be published about it.
        rotating = healthy()
        rotating["modules"]["applianceList"]["authInfo"] = {
            "cognitoTokenNew": "eyJhbGciOiJIUzI1NiJ9.SECRET-TOKEN-VALUE.sig"
        }
        census = self._census(rotating)
        self.assertEqual(1, census["auth_keys"])
        blob = json.dumps(census, default=str)
        for leak in ("cognitoTokenNew", "SECRET-TOKEN-VALUE"):
            self.assertNotIn(leak, blob, leak)

    def test_a_session_that_resolved_elsewhere_is_visible(self) -> None:
        census = self._census(healthy(), claim=OTHER_ACCOUNT)
        self.assertEqual("mismatch", census["account"])
        self.assertEqual(1, census["count"])

    def test_a_connection_without_an_auth_object_records_no_claim(self) -> None:
        # `HonConnection.auth` RAISES when the connection was never created, and no
        # other double in this file has one. The census must answer, not abort: the
        # rule the `getattr` on `resp.status` follows, for the same reason.
        api = _call(FakeConnection(healthy()))
        self.assertEqual(1, len(_run(api.load_appliances())))
        self.assertEqual("no_claim", api.last_appliance_fetch["account"])
        # ...and the rest of the census is unaffected: the guard costs the identity
        # check, not the fetch.
        self.assertEqual("ok", api.last_appliance_fetch["outcome"])
        self.assertIs(True, api.last_appliance_fetch["module_ok"])

    def test_a_raising_auth_property_does_not_abort_the_fetch(self) -> None:
        class _Exploding(FakeConnection):
            @property
            def auth(self):
                raise RuntimeError("connection not created (create() is missing)")

        api = _call(_Exploding(healthy()))
        self.assertEqual(1, len(_run(api.load_appliances())))
        self.assertEqual("no_claim", api.last_appliance_fetch["account"])

    def test_both_census_paths_declare_the_same_keys(self) -> None:
        # `load_appliances` builds two SEPARATE dict literals, and the reader compares
        # two downloads of the same issue field by field: a key present on one path and
        # absent on the other turns "this got worse" into "this file is shaped
        # differently".
        served = self._census(healthy())
        refused = _call(_RaisingConnection(RuntimeError("hOn server error (status 503)")))
        with self.assertRaises(RuntimeError):
            _run(refused.load_appliances())
        self.assertEqual(set(served), set(refused.last_appliance_fetch))
        for key in ("envelope_ok", "module_ok", "auth_keys", "account"):
            with self.subTest(key=key):
                # Nothing downstream of a body can be reported when there was no body.
                self.assertIsNone(refused.last_appliance_fetch[key])

    def test_the_new_fields_reach_the_diagnostics_block_unchanged(self) -> None:
        # The whole path in one assertion: the writer here, the reader in
        # diagnostics.py, and no other file in the suite can exercise both (see the
        # docstring of test_both_paths_stamp_an_instant_the_dump_can_render above).
        # Split across two files, a rename on either side leaves both halves green
        # while the block quietly renders null.
        from datetime import timedelta
        from types import SimpleNamespace

        from custom_components.addhon import diagnostics
        from custom_components.addhon.const import DOMAIN

        rotated = healthy()
        rotated["modules"]["applianceList"]["authInfo"] = {"cognitoTokenNew": "SECRET"}
        rotated["modules"]["applianceList"]["success"] = False
        api = _call(_AuthedConnection(rotated, _id_token(OTHER_ACCOUNT)))
        _run(api.load_appliances())
        hass = SimpleNamespace(data={DOMAIN: {"e1": {"client": api}}})
        block = diagnostics._last_fetch(
            hass,
            SimpleNamespace(entry_id="e1"),
            now=api.last_appliance_fetch["at"] + timedelta(seconds=1),
        )
        self.assertEqual("recorded", block["state"])
        self.assertIs(True, block["envelope_ok"])
        self.assertIs(False, block["module_ok"])
        self.assertEqual(1, block["auth_keys"])
        self.assertEqual("mismatch", block["account"])
        blob = json.dumps(block, default=str)
        for leak in ("SECRET", "cognitoTokenNew", OUR_ACCOUNT, OTHER_ACCOUNT,
                     "AA:BB:CC:DD:EE:FF", "PLAINTEXT-SERIAL", "Kitchen Fridge"):
            self.assertNotIn(leak, blob, leak)


if __name__ == "__main__":
    unittest.main()
