# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contract tests for command-catalog request and structural extraction."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import types
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any


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
    coordinator = _mod("homeassistant.helpers.update_coordinator")
    coordinator.DataUpdateCoordinator = getattr(
        coordinator,
        "DataUpdateCoordinator",
        type("DataUpdateCoordinator", (), {}),
    )
    coordinator.UpdateFailed = getattr(
        coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {})
    )
    homeassistant = _mod("homeassistant")
    homeassistant.config_entries = config_entries
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    homeassistant.helpers = _mod("homeassistant.helpers")
    homeassistant.helpers.update_coordinator = coordinator
    yarl = _mod("yarl")
    if not hasattr(yarl, "URL"):
        yarl.URL = type("URL", (), {"__init__": lambda self, s, encoded=False: None})
    aiohttp = _mod("aiohttp")
    aiohttp.ClientSession = getattr(
        aiohttp, "ClientSession", type("ClientSession", (), {})
    )
    aiohttp.ClientResponse = getattr(
        aiohttp, "ClientResponse", type("ClientResponse", (), {})
    )
    aiohttp.ContentTypeError = getattr(
        aiohttp, "ContentTypeError", type("ContentTypeError", (Exception,), {})
    )


_install_stubs()

from custom_components.addhon.client.transport import device as _device
from custom_components.addhon.client.transport.api import HonApi
from custom_components.addhon.client.transport.command_catalog import (
    CATALOG_OUTCOMES,
    CATALOG_RESULT_CATEGORIES,
    CommandCatalogRequest,
    CommandCatalogResponseError,
    extract_command_catalog,
    legacy_command_catalog_fetch,
    normalize_catalog_language,
)


class FakeResponse:
    """Minimal aiohttp response shape used by :class:`HonApi`."""

    def __init__(self, body: Any, *, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def json(self, content_type: Any = None) -> Any:
        return self._body

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class FakeConnection:
    """Record GET calls and return one fixed response."""

    def __init__(self, body: Any, *, status: int = 200) -> None:
        self._response = FakeResponse(body, status=status)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        return self._response


class FakeAppliance:
    """Real appliance attribute shape consumed by the transport builder."""

    def __init__(self, **info: Any) -> None:
        self.appliance_type = "REF"
        self.appliance_model_id = "4321"
        self.mac_address = "AA:BB:CC:DD:EE:FF"
        self.code = "CODE123"
        self.info = info


def _request() -> CommandCatalogRequest:
    return CommandCatalogRequest.from_appliance(
        FakeAppliance(
            eepromId="EE", fwVersion="1.2", series="S", seriesVersion="V7"
        ),
        "pt-BR",
    )


class CommandCatalogRequestTest(unittest.TestCase):
    def test_official_request_contains_series_version_and_base_language(self) -> None:
        request = _request()

        self.assertEqual(
            request.params(),
            {
                "applianceType": "REF",
                "applianceModelId": "4321",
                "macAddress": "AA:BB:CC:DD:EE:FF",
                "code": "CODE123",
                "os": _device.OS,
                "appVersion": _device.APP_VERSION,
                "firmwareId": "EE",
                "fwVersion": "1.2",
                "series": "S",
                "seriesVersion": "V7",
                "lang": "pt",
            },
        )
        self.assertEqual(
            request.presence(),
            {
                "firmware": True,
                "firmware_version": True,
                "series": True,
                "series_version": True,
                "language": True,
            },
        )

    def test_optional_discriminators_are_omitted_when_falsey(self) -> None:
        request = CommandCatalogRequest.from_appliance(
            FakeAppliance(eepromId="", fwVersion=0, series=None, seriesVersion=False),
            None,
        )

        params = request.params()
        self.assertEqual(params["lang"], "en")
        for name in ("firmwareId", "fwVersion", "series", "seriesVersion"):
            self.assertNotIn(name, params)
        self.assertEqual(
            request.presence(),
            {
                "firmware": False,
                "firmware_version": False,
                "series": False,
                "series_version": False,
                "language": True,
            },
        )

    def test_request_is_frozen_and_params_returns_a_fresh_mapping(self) -> None:
        request = _request()
        first = request.params()
        first["lang"] = "secret"

        self.assertEqual(request.params()["lang"], "pt")
        with self.assertRaises(FrozenInstanceError):
            request.language = "it"  # type: ignore[misc]

    def test_language_normalization_is_closed_to_a_base_fallback(self) -> None:
        self.assertEqual(normalize_catalog_language(" IT-it "), "it")
        # The app resolves the tag against the languages it SHIPS, not against the
        # device locale (decomp.txt:507774-507838 over the table at 483593-483650), so a
        # regional tag survives only when the table declares it. `zh-hk` is declared;
        # `pt-BR` is not, and the app sends `pt` there -- which truncation already gives.
        self.assertEqual(normalize_catalog_language("zh-HK"), "zh-hk")
        self.assertEqual(normalize_catalog_language(" ZH-hk "), "zh-hk")
        self.assertEqual(normalize_catalog_language("pt-BR"), "pt")
        self.assertEqual(normalize_catalog_language("zh-Hant"), "zh")
        for value in (None, "", "-regional", 4):
            with self.subTest(value=value):
                self.assertEqual(normalize_catalog_language(value), "en")


class CommandCatalogProbeTest(unittest.TestCase):
    def test_structural_failures_have_distinct_closed_outcomes(self) -> None:
        cases = (
            ({"payload": ["not", "a", "mapping"]}, "invalid_payload", "other"),
            ({"payload": {}}, "empty_payload", "missing"),
            ({"payload": {"settings": {}}}, "missing_result", "missing"),
            (
                {"payload": {"resultCode": "7", "message": "SERVER SECRET"}},
                "nonzero_result",
                "nonzero",
            ),
        )

        for body, outcome, result_category in cases:
            with self.subTest(outcome=outcome):
                with self.assertRaises(CommandCatalogResponseError) as raised:
                    extract_command_catalog(body, status=502, request=_request())
                probe = raised.exception.probe
                self.assertEqual(probe.outcome, outcome)
                self.assertEqual(probe.result_category, result_category)
                self.assertIn(probe.outcome, CATALOG_OUTCOMES)
                self.assertIn(probe.result_category, CATALOG_RESULT_CATEGORIES)

    def test_valid_payload_is_copied_without_result_code(self) -> None:
        original = {
            "payload": {
                "resultCode": "0",
                "applianceModel": {"attributes": []},
                "settings": {
                    "setParameters": {
                        "description": "d",
                        "protocolType": "MQTT",
                    }
                },
                "startProgram": {},
                "stopProgram": {},
            }
        }
        before = copy.deepcopy(original)

        fetch = extract_command_catalog(original, status=200, request=_request())

        self.assertEqual(original, before)
        self.assertNotIn("resultCode", fetch.payload)
        self.assertEqual(fetch.probe.outcome, "ok")
        self.assertEqual(fetch.probe.result_category, "zero")
        self.assertTrue(fetch.probe.has_appliance_model)
        self.assertTrue(fetch.probe.has_settings)
        self.assertTrue(fetch.probe.has_set_parameters)
        self.assertTrue(fetch.probe.has_start_program)
        self.assertTrue(fetch.probe.has_stop_program)
        self.assertEqual(fetch.probe.payload_entries, 4)

    def test_result_code_only_is_empty_payload(self) -> None:
        with self.assertRaises(CommandCatalogResponseError) as raised:
            extract_command_catalog(
                {"payload": {"resultCode": "0"}}, status=200, request=_request()
            )
        self.assertEqual(raised.exception.probe.outcome, "empty_payload")
        self.assertEqual(raised.exception.probe.result_category, "zero")

    def test_serialized_probe_contains_no_identity_or_server_schema_values(self) -> None:
        body = {
            "payload": {
                "resultCode": "0",
                "applianceModel": {"model": "MODEL-SECRET"},
                "settings": {
                    "secretCommand": {
                        "description": "SCHEMA-SECRET",
                        "protocolType": "MQTT",
                    }
                },
            },
            "message": "SERVER-SECRET",
        }

        serialized = json.dumps(
            extract_command_catalog(body, status=200, request=_request()).probe.as_dict()
        )

        for secret in (
            "AA:BB:CC:DD:EE:FF",
            "CODE123",
            "4321",
            "secretCommand",
            "SCHEMA-SECRET",
            "SERVER-SECRET",
            "MODEL-SECRET",
        ):
            self.assertNotIn(secret, serialized)

    def test_legacy_adapter_copies_only_nonempty_plain_mappings(self) -> None:
        payload = {"settings": {"x": 1}}
        fetch = legacy_command_catalog_fetch(payload, _request())
        payload["settings"]["x"] = 2
        self.assertEqual(fetch.payload, {"settings": {"x": 1}})
        self.assertEqual(fetch.probe.outcome, "ok")

        for invalid, outcome in (({}, "empty_payload"), ([], "invalid_payload")):
            with self.subTest(outcome=outcome):
                with self.assertRaises(CommandCatalogResponseError) as raised:
                    legacy_command_catalog_fetch(invalid, _request())  # type: ignore[arg-type]
                self.assertEqual(raised.exception.probe.outcome, outcome)

    def test_typed_api_fetch_returns_payload_and_http_probe(self) -> None:
        body = {"payload": {"resultCode": "0", "settings": {"x": 1}}}
        connection = FakeConnection(body, status=206)

        fetch = asyncio.run(HonApi(connection).fetch_command_catalog(_request()))

        self.assertEqual(fetch.payload, {"settings": {"x": 1}})
        self.assertEqual(fetch.probe.status, 206)
        self.assertEqual(connection.calls[0][1]["params"], _request().params())


if __name__ == "__main__":
    unittest.main()
