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
import copy
import logging
import re
import sys
import types
import unittest
from pathlib import Path

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
    def __init__(self, body, text="<text>") -> None:
        self._body = body
        self._text = text

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

    def __init__(self, body, text="<text>", mobile_id="pyhOn") -> None:
        self._body = body
        self._text = text
        self.calls: list = []
        self.device = _device.HonDevice(mobile_id)

    def _ctx(self, method, url, kwargs):
        return _ReqCtx(self, method, url, kwargs, FakeResponse(self._body, self._text))

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

    def test_identical_to_pyhon_on_common_path(self) -> None:
        # microseconds != 0: our output and the pyhOn formula [:-3]+"Z" coincide.
        self._frozen("2026-06-18T12:34:56.789012")
        pyhon_formula = "2026-06-18T12:34:56.789012"[:-3] + "Z"
        self.assertEqual(api_mod._command_timestamp(), pyhon_formula)
        self.assertEqual(api_mod._command_timestamp(), "2026-06-18T12:34:56.789Z")

    def test_truncates_not_rounds_at_boundary(self) -> None:
        # Guard: timespec="milliseconds" TRUNCATES (like pyhOn's [:-3]), it does not round.
        # .789999 -> .789Z (not .790Z). Hardens against a future change of semantics.
        self._frozen("2026-06-18T12:34:56.789999")
        self.assertEqual(api_mod._command_timestamp(), "2026-06-18T12:34:56.789Z")
        self.assertEqual(
            api_mod._command_timestamp(), "2026-06-18T12:34:56.789999"[:-3] + "Z"
        )

    def test_fixes_pyhon_bug_when_microsecond_zero(self) -> None:
        # microseconds == 0: pyhOn would produce "...T12:34Z" (seconds lost); we do not.
        self._frozen("2026-06-18T12:34:56")
        pyhon_buggy = "2026-06-18T12:34:56"[:-3] + "Z"  # -> "2026-06-18T12:34Z"
        self.assertEqual(pyhon_buggy, "2026-06-18T12:34Z")  # documents the pyhOn bug
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
        return _ReqCtx(self, method, url, kwargs, _StrictResponse(self._body, self._text))


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


if __name__ == "__main__":
    unittest.main()
