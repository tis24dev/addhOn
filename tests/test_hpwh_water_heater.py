# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The heat-pump water heater platform (type HW, issue #113)."""
import asyncio
import enum
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.test_hpwh import _install_package_stubs, _mod  # same stubs, one place

_install_package_stubs()

water_heater = _mod("homeassistant.components.water_heater")


class _Feature(enum.IntFlag):
    TARGET_TEMPERATURE = 1
    OPERATION_MODE = 2
    AWAY_MODE = 4
    ON_OFF = 8


water_heater.WaterHeaterEntityFeature = getattr(water_heater, "WaterHeaterEntityFeature", _Feature)
water_heater.WaterHeaterEntity = getattr(water_heater, "WaterHeaterEntity", type("WaterHeaterEntity", (), {}))
for _name, _value in (("STATE_ECO", "eco"), ("STATE_ELECTRIC", "electric"),
                      ("STATE_HEAT_PUMP", "heat_pump"), ("STATE_OFF", "off")):
    setattr(water_heater, _name, getattr(water_heater, _name, _value))
_mod("homeassistant.components").water_heater = water_heater
_const = _mod("homeassistant.const")
_const.ATTR_TEMPERATURE = getattr(_const, "ATTR_TEMPERATURE", "temperature")
_switch = _mod("homeassistant.components.switch")
_switch.SwitchEntity = getattr(_switch, "SwitchEntity", type("SwitchEntity", (), {}))

from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from custom_components.addhon import water_heater as platform  # noqa: E402
from custom_components.addhon.const import CONF_ENABLE_EXPERIMENTAL, DOMAIN  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "hw_hp110m8" / "attributes.json"
SENT: list = []


def _range(value, low, high, step=1):
    return types.SimpleNamespace(value=value, min=low, max=high, step=step)


def _appliance():
    settings = types.SimpleNamespace(parameters={
        "tempSel": _range(40, 35, 75), "onOffStatus": _range(0, 0, 1),
        "boostStatus": _range(0, 0, 1),
    })
    categories = {
        name: types.SimpleNamespace(parameters={"machMode": types.SimpleNamespace(value=code)})
        for name, code in (("auto", "1"), ("eco", "2"), ("elec", "3"), ("vac", "4"))
    }
    start = types.SimpleNamespace(categories=categories, parameters={})
    return types.SimpleNamespace(
        commands={"settings": settings, "startProgram": start},
        model_attributes={"series": "m8"},
    )


class _Coordinator:
    def __init__(self, data) -> None:
        self.data = data
        self.hass = None
        self.last_update_success = True


class _Entry:
    entry_id = "entry-1"

    def __init__(self, experimental: bool) -> None:
        self.options = {CONF_ENABLE_EXPERIMENTAL: experimental}


async def _setup(module, experimental: bool, overrides: dict) -> list:
    SENT.clear()
    attributes = dict(json.loads(FIXTURE.read_text(encoding="utf-8"))["attributes"])
    attributes.update(overrides)
    data = {"hw-1": {"type": "HW", "name": "Boiler", "attributes": attributes,
                     "settings": {}, "appliance": _appliance()}}
    hass = types.SimpleNamespace(
        data={DOMAIN: {"entry-1": {"coordinator": _Coordinator(data), "client": None}}},
        async_create_task=lambda coro: asyncio.ensure_future(coro),
    )
    added: list = []
    await module.async_setup_entry(hass, _Entry(experimental), added.extend)
    for entity in added:
        entity.hass = hass
    return added


async def _record(hass, client, appliance, patch) -> None:
    SENT.append(patch)


_PATCHERS: list = []


def setUpModule() -> None:
    # Every write is recorded instead of sent; the refresh after it is a no-op.
    # pytest does not run unittest's module cleanups, so tearDownModule stops them.
    _PATCHERS.append(mock.patch.object(platform, "async_dispatch_patch", _record))
    _PATCHERS.append(
        mock.patch(
            "custom_components.addhon.base_entity.HonBaseEntity._async_request_command_refresh",
            mock.AsyncMock(),
        )
    )
    for patcher in _PATCHERS:
        patcher.start()


def tearDownModule() -> None:
    while _PATCHERS:
        _PATCHERS.pop().stop()


async def _build(experimental: bool, **overrides) -> list:
    return await _setup(platform, experimental, overrides)


def _set_attr(entity, name: str, value) -> None:
    entity.coordinator.data["hw-1"]["attributes"][name] = value


async def _drain_tasks() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class WaterHeaterTest(unittest.IsolatedAsyncioTestCase):
    async def test_not_created_without_the_experimental_option(self) -> None:
        self.assertEqual(await _build(experimental=False), [])

    async def test_reads_the_reporters_appliance(self) -> None:
        entity = (await _build(experimental=True))[0]
        self.assertEqual(entity.current_temperature, 33.0)
        self.assertEqual(entity.target_temperature, 40.0)
        self.assertEqual((entity.min_temp, entity.max_temp), (35.0, 75.0))
        self.assertEqual(entity.current_operation, "off")
        self.assertEqual(entity.operation_list, ["heat_pump", "eco", "electric", "off"])

    async def test_mode_change_while_off_is_refused_and_nothing_is_sent(self) -> None:
        entity = (await _build(experimental=True))[0]
        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_set_operation_mode("eco")
        self.assertEqual(ctx.exception.translation_key, "hpwh_switch_on_first")
        self.assertEqual(SENT, [])

    async def test_mode_change_sends_machmode_only(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        await entity.async_set_operation_mode("eco")
        patch = SENT[0]
        self.assertEqual((patch.command_name, dict(patch.values)), ("startProgram", {"machMode": "2"}))
        self.assertEqual(patch.program_name, "")

    async def test_off_mode_switches_off(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        await entity.async_set_operation_mode("off")
        self.assertEqual(dict(SENT[0].values), {"onOffStatus": "0"})

    async def test_turn_on(self) -> None:
        entity = (await _build(experimental=True))[0]
        await entity.async_turn_on()
        self.assertEqual(dict(SENT[0].values), {"onOffStatus": "1"})

    async def test_temperature(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        await entity.async_set_temperature(temperature=45)
        self.assertEqual(dict(SENT[0].values), {"tempSel": "45"})

    async def test_temperature_out_of_range_or_fractional_is_refused(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1))[0]
        for value in (30, 80, 45.5):
            with self.assertRaises(HomeAssistantError) as ctx:
                await entity.async_set_temperature(temperature=value)
            self.assertEqual(ctx.exception.translation_key, "invalid_setpoint", value)
        self.assertEqual(SENT, [])

    async def test_vacation_is_unknown(self) -> None:
        entity = (await _build(experimental=True, onOffStatus=1, machMode=4))[0]
        self.assertIsNone(entity.current_operation)
