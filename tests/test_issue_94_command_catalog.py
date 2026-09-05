# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end command-catalog regression for discussion #94.

The reported H4F306SDH1-shaped freezer already has generic FRE entity mappings.  This
test proves that a real HTTP envelope, parsed by the real transport and engine loader,
reaches those existing gates as a temperature number and a Super Freeze switch.  It
also keeps the cooling-mode write guard in the same path: an active Super Freeze mode
must still own the setpoint and prevent a command from reaching the transport.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import install_stubs  # noqa: E402

install_stubs()

from homeassistant.exceptions import HomeAssistantError

from custom_components.addhon import number, switch
from custom_components.addhon.client.engine.appliance import HonAppliance
from custom_components.addhon.client.transport.api import HonApi
from custom_components.addhon.const import APPLIANCE_FRE, DOMAIN


CATALOG = {
    "applianceModel": {
        "attributes": [{"parName": "zones", "parValue": "freezer"}]
    },
    "settings": {
        "setParameters": {
            "description": "Set parameters",
            "protocolType": "MQTT",
            "parameters": {
                "tempSelZ3": {
                    "typology": "range",
                    "category": "command",
                    "mandatory": 0,
                    "defaultValue": "-18",
                    "minimumValue": "-24",
                    "maximumValue": "-14",
                    "incrementValue": "1",
                }
            },
        }
    },
    "startProgram": {
        "PROGRAMS.REF.SUPER_FREEZE": {
            "description": "Super Freeze",
            "protocolType": "MQTT",
            "parameters": {
                "quickModeZ2": {
                    "typology": "fixed",
                    "category": "command",
                    "mandatory": 1,
                    "fixedValue": "1",
                }
            },
        }
    },
    "stopProgram": {
        "description": "Stop",
        "protocolType": "MQTT",
        "parameters": {
            "quickModeZ2": {
                "typology": "fixed",
                "category": "command",
                "mandatory": 0,
                "fixedValue": "0",
            }
        },
    },
}


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.status = 200

    async def json(self, content_type: Any = None) -> dict[str, Any]:
        return copy.deepcopy(self._body)

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _CatalogConnection:
    """Cloud boundary double: only network I/O is replaced, parsing stays real."""

    def __init__(self) -> None:
        self._catalog = {"payload": {**copy.deepcopy(CATALOG), "resultCode": "0"}}
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.get_calls.append((url, kwargs))
        if url.endswith("/commands/v1/retrieve"):
            return _Response(self._catalog)
        if url.endswith("/history"):
            return _Response({"payload": {"history": []}})
        if url.endswith("/favourite"):
            return _Response({"payload": {"favourites": []}})
        raise AssertionError(f"Unexpected GET endpoint: {url}")

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.post_calls.append((url, kwargs))
        return _Response({"payload": {"resultCode": "0"}})


class _Coordinator:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.hass = None
        self.last_update_success = True
        self.last_exception = None

    async def async_refresh(self) -> None:
        pass

    async def async_request_refresh(self) -> None:
        pass


class _Hass:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    async def async_add_executor_job(self, func, *args):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(func, *args).result(timeout=5)


class _Client:
    """Run command coroutines like HonClient's dedicated-loop bridge."""

    def run_command_sync(self, coroutine):
        return asyncio.run(coroutine)


class _Entry:
    entry_id = "issue-94"
    options: dict[str, Any] = {}


class Issue94CommandCatalogTest(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_creates_controls_without_bypassing_mode_guard(self) -> None:
        connection = _CatalogConnection()
        appliance = HonAppliance(
            HonApi(connection),
            {
                "applianceTypeName": APPLIANCE_FRE,
                "applianceModelId": "9401",
                "macAddress": "AA:BB:CC:DD:EE:94",
                "code": "H4F306SD",
                "modelName": "Upright freezer",
                "brand": "Haier",
                "eepromId": "eeprom-94",
                "fwVersion": "1.0",
                "series": "7",
                "seriesVersion": "1",
            },
        )
        await appliance.load_commands()

        appliance_id = appliance.unique_id
        coordinator = _Coordinator(
            {
                appliance_id: {
                    "type": APPLIANCE_FRE,
                    "name": "Freezer",
                    "model": "H4F306SDH1-shaped",
                    "attributes": {
                        "available": True,
                        "tempSelZ3": "-18",
                        "quickModeZ2": "1",
                    },
                    "appliance": appliance,
                    "settings": {},
                    "statistics": {},
                }
            }
        )
        hass = _Hass(
            {DOMAIN: {_Entry.entry_id: {"coordinator": coordinator, "client": _Client()}}}
        )
        coordinator.hass = hass
        number_entities: list[Any] = []
        switch_entities: list[Any] = []

        await number.async_setup_entry(hass, _Entry(), number_entities.extend)
        await switch.async_setup_entry(hass, _Entry(), switch_entities.extend)

        number_keys = {
            entity.entity_description.key
            for entity in number_entities
            if hasattr(entity, "entity_description")
        }
        switch_keys = {
            entity._attr_unique_id.removeprefix(f"{appliance_id}_")
            for entity in switch_entities
            if entity._attr_unique_id.startswith(f"{appliance_id}_")
        }
        self.assertIn("target_temp_zone3", number_keys)
        self.assertIn("super_freeze", switch_keys)

        target = next(
            entity
            for entity in number_entities
            if entity.entity_description.key == "target_temp_zone3"
        )
        with self.assertRaises(HomeAssistantError) as raised:
            await target.async_set_native_value(-19)

        self.assertEqual(raised.exception.translation_key, "setpoint_owned_by_mode")
        self.assertEqual(connection.post_calls, [])


if __name__ == "__main__":
    unittest.main()
