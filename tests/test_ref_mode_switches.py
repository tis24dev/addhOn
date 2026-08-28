# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fridge boost modes as INDEPENDENT switches (issue #93, follow-up).

The schema below is transcribed from two ground truths, not invented: the `settings` and
`stopProgram` commands are verbatim from the diagnostics dump of the reporter's
HFW7720EWMP (`diagnostics/issue93-dumps/rmxs-HFW7720EWMP-2026-08-27.json`), and the seven
`startProgram` categories are verbatim from the app's own fixture for the same model
family (`apk/decomp.txt:3495902`, HTW7720ENMP -- identical `option` string, identical
seven categories). The one-key OFF payload asserted here is the payload the OFFICIAL app
is recorded sending in `apk/dump/ref_10136/attributes.json`.

Stdlib unittest on the shared conftest stubs; no real Home Assistant install.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import json
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


def _install_platform_stubs() -> None:
    """Seed what conftest does not: the modules `custom_components.addhon` imports.

    getattr-guarded throughout, so whichever test module lands first wins and this one
    never narrows a shared stub (see test_stub_hygiene).
    """
    ha = _mod("homeassistant")

    config_entries = _mod("homeassistant.config_entries")
    ha.config_entries = config_entries
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )

    core = _mod("homeassistant.core")
    ha.core = core
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    core.callback = getattr(core, "callback", lambda func: func)

    exceptions = _mod("homeassistant.exceptions")
    ha.exceptions = exceptions
    base_error = exceptions.HomeAssistantError
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_error,), {})
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions,
        "ConfigEntryAuthFailed",
        type("ConfigEntryAuthFailed", (base_error,), {}),
    )
    exceptions.Unauthorized = getattr(
        exceptions, "Unauthorized", type("Unauthorized", (base_error,), {})
    )

    # `conftest` declares the binary-sensor DESCRIPTION and device class but not the
    # entity base, so importing `binary_sensor.py` works only when some other test module
    # happened to install it first. Declared here, getattr-guarded, so this file's own
    # binary-sensor assertions do not depend on collection order.
    binary_mod = _mod("homeassistant.components.binary_sensor")
    _mod("homeassistant.components").binary_sensor = binary_mod
    binary_mod.BinarySensorEntity = getattr(
        binary_mod, "BinarySensorEntity", type("BinarySensorEntity", (), {})
    )

    entity_platform = _mod("homeassistant.helpers.entity_platform")
    sys.modules["homeassistant.helpers"].entity_platform = entity_platform
    entity_platform.AddEntitiesCallback = getattr(
        entity_platform, "AddEntitiesCallback", object
    )


_install_platform_stubs()

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.addhon.const import APPLIANCE_REF  # noqa: E402

# --- the live schema of the reporting fridge --------------------------------------

# `settings`, verbatim from the #93 dump. tempSelZ3 is ABSENT on this model: its My Zone
# is written only through startProgram, which is why no number entity exists for it.
REF_SETTINGS_PARAMS = {
    "tempSelZ1": {"typology": "range", "category": "command", "mandatory": 0,
                  "minimumValue": "1", "maximumValue": "9", "incrementValue": "1",
                  "defaultValue": "5"},
    "tempSelZ2": {"typology": "range", "category": "command", "mandatory": 0,
                  "minimumValue": "-24", "maximumValue": "-14", "incrementValue": "1",
                  "defaultValue": "-18"},
}

# `stopProgram`, verbatim. FOUR flags, every one `mandatory: 0`, no programRules. That
# shape is the whole reason a sparse patch comes out as a single key, and
# `test_the_fixture_declares_the_full_four_flag_reset` refuses to let it be trimmed.
REF_STOP_PARAMS = {
    "holidayMode": {"typology": "fixed", "category": "command", "mandatory": 0,
                    "fixedValue": "0"},
    "intelligenceMode": {"typology": "fixed", "category": "command", "mandatory": 0,
                         "fixedValue": "0"},
    "quickModeZ1": {"typology": "fixed", "category": "command", "mandatory": 0,
                    "fixedValue": "0"},
    "quickModeZ2": {"typology": "fixed", "category": "command", "mandatory": 0,
                    "fixedValue": "0"},
}


def _dashboard_ancillary(zones: list[str]) -> dict:
    return {
        "programFamily": {"category": "cluster", "typology": "enum", "mandatory": 1,
                          "enumValues": ["dashboard"], "defaultValue": "[dashboard]"},
        "remoteActionable": {"category": "general", "typology": "fixed",
                             "mandatory": 0, "fixedValue": "1"},
        "remoteVisible": {"category": "general", "typology": "fixed",
                          "mandatory": 0, "fixedValue": "1"},
        "zone": {"category": "cluster", "typology": "enum", "mandatory": 1,
                 "enumValues": zones,
                 "defaultValue": "[" + "|".join(zones) + "]"},
    }


def _flag_program(flag: str, zones: list[str]) -> dict:
    """A dashboard category that writes ONE flag -- the shape of all four boost modes."""
    return {
        "parameters": {
            flag: {"category": "command", "typology": "fixed", "mandatory": 1,
                   "fixedValue": "1"},
        },
        "ancillaryParameters": _dashboard_ancillary(zones),
    }


def _my_zone_program(value: str) -> dict:
    """A My Zone category: writes tempSelZ3 and NO flag, so it is never a switch."""
    return {
        "parameters": {
            "tempSelZ3": {"category": "command", "typology": "fixed", "mandatory": 1,
                          "fixedValue": value},
        },
        "ancillaryParameters": _dashboard_ancillary(["vtRoom1"]),
    }


# The seven categories, keyed by the RAW cloud key exactly as the loader receives them.
REF_START_CATEGORIES = {
    "PROGRAMS.REF.AUTO_SET": _flag_program("intelligenceMode", ["fridge", "freezer"]),
    "PROGRAMS.REF.SUPER_COOL": _flag_program("quickModeZ1", ["fridge"]),
    "PROGRAMS.REF.SUPER_FREEZE": _flag_program("quickModeZ2", ["freezer"]),
    "PROGRAMS.REF.HOLIDAY": _flag_program("holidayMode", ["fridge", "vtRoom1"]),
    "PROGRAMS.REF.QUICK_COOL": _my_zone_program("2"),
    "PROGRAMS.REF.ZERO_FRESH": _my_zone_program("0"),
    "PROGRAMS.REF.FRUIT_AND_VEG": _my_zone_program("5"),
}

# The shadow the fridge publishes while idle, trimmed to what the entities read. All four
# flags are 0 here; the #93 dump had intelligenceMode = 1, which
# `test_it_reads_the_flag_back` exercises directly.
REF_ATTRIBUTES = {
    "available": True,
    "tempSelZ1": 3,
    "tempSelZ2": -18,
    "tempSelZ3": 0,
    "tempZ1": 5,
    "tempZ2": -18,
    "humidityEnv": 43,
    "doorStatusZ1": 0,
    "door2StatusZ1": 0,
    "errors": "00",
    "holidayMode": 0,
    "intelligenceMode": 0,
    "quickModeZ1": 0,
    "quickModeZ2": 0,
    "programName": "No Program",
}


class RecordingApi:
    """Stands in for the transport: records exactly what would go on the wire."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_command(
        self, appliance, command, parameters, ancillary_parameters, program_name=""
    ) -> bool:
        self.sent.append(
            {
                "command": command,
                "parameters": dict(parameters),
                "ancillary": dict(ancillary_parameters),
                "program_name": program_name,
            }
        )
        return True


class _RefAppliance:
    def __init__(self) -> None:
        self.zone = 0
        self.options: dict[str, str] = {}
        self.commands: dict[str, object] = {}
        self.unique_id = "ref-1"
        self.api = RecordingApi()
        self.synced: list[str] = []
        self.attributes: dict[str, dict] = {"parameters": {}}
        self.synced_payloads: list[dict] = []
        self.model_attributes = {"zones": "fridge|freezer|vtRoom1"}

    def sync_command_to_params(self, name: str) -> None:
        self.synced.append(name)

    def sync_payload_to_params(self, params) -> None:
        self.synced_payloads.append(dict(params))


def _appliance(
    *,
    categories: dict | None = None,
    stop_params: dict | None = None,
    with_stop: bool = True,
    active: str = "PROGRAMS.REF.AUTO_SET",
):
    """A fridge carrying the live schema of the reporting device.

    `startProgram` is built CATEGORISED, which is the shape the loader really produces and
    the only one in which the program parameter exists at all: `_create_parameters`
    attaches the synthetic `HonParameterProgram` because the category name contains
    "PROGRAM", and that parameter's `.values` is what both the gate and
    `async_send_program` read. A flat fixture would make every gating assertion below pass
    for the wrong reason.

    `active` is the category `appliance.commands["startProgram"]` starts on, mirroring the
    #93 dump, whose active category was `auto_set`. Anything that turns a DIFFERENT mode on
    has to swap off it first, which is what makes the swap real rather than incidental.
    """
    from custom_components.addhon.client.engine.commands import HonCommand

    appliance = _RefAppliance()

    settings_categories: dict[str, HonCommand] = {}
    settings = HonCommand(
        "settings",
        {"parameters": copy.deepcopy(REF_SETTINGS_PARAMS)},
        appliance,
        categories=settings_categories,
        category_name="setParameters",
    )
    settings_categories["setParameters"] = settings
    settings_categories["setConfig"] = HonCommand(
        "settings",
        {"parameters": {
            "httpEndpoint": {"typology": "fixed", "category": "command",
                             "mandatory": 0, "fixedValue": ""},
        }},
        appliance,
        categories=settings_categories,
        category_name="setConfig",
    )
    appliance.commands["settings"] = settings

    if with_stop:
        appliance.commands["stopProgram"] = HonCommand(
            "stopProgram",
            {"parameters": copy.deepcopy(
                REF_STOP_PARAMS if stop_params is None else stop_params
            )},
            appliance,
        )

    raw = copy.deepcopy(REF_START_CATEGORIES if categories is None else categories)
    start_categories: dict[str, HonCommand] = {}
    for key, body in raw.items():
        # Filed under the CLEANED name, exactly like `HonCommandLoader`: the raw key goes
        # to `category_name` and the lowercased last dot-segment is the categories key.
        start_categories[key.split(".")[-1].lower()] = HonCommand(
            "startProgram", body, appliance,
            categories=start_categories, category_name=key,
        )
    appliance.commands["startProgram"] = start_categories[
        active.split(".")[-1].lower()
    ]
    return appliance


class RunningClient:
    """Runs whatever the two senders hand it, like the real client's loop.

    Both entry points are REAL: `run_command_sync` drives the program sender's coroutine
    and `dispatch_patch_sync` drives the actual `CommandDispatcher`, so every payload
    assertion below reads what the transport would have received -- not what a fake
    dispatcher decided to record.
    """

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.patches: list = []

    def run_command_sync(self, coro):
        if self.fail is not None:
            coro.close()
            raise self.fail
        return asyncio.run(coro)

    def dispatch_patch_sync(self, appliance, patch):
        from custom_components.addhon.command_dispatch import CommandDispatcher

        if self.fail is not None:
            raise self.fail
        self.patches.append(patch)
        return asyncio.run(CommandDispatcher().dispatch(appliance, patch))


class RecordingHass:
    def __init__(self, data: dict) -> None:
        self.data = data

    async def async_add_executor_job(self, func, *args):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(func, *args).result(timeout=5)


class RefreshingCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.last_update_success = True
        self.refreshes = 0

    async def async_refresh(self) -> None:
        self.refreshes += 1

    async def async_request_refresh(self) -> None:
        self.refreshes += 1


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1", options: dict | None = None) -> None:
        self.entry_id = entry_id
        self.options = dict(options or {})


def _entry_data(attributes: dict | None = None, appliance=None) -> dict:
    return {
        "ref-1": {
            "type": APPLIANCE_REF,
            "name": "Fridge",
            "model": "HFW7720EWMP",
            "attributes": (
                copy.deepcopy(REF_ATTRIBUTES) if attributes is None else attributes
            ),
            "appliance": _appliance() if appliance is None else appliance,
            "settings": {},
            "statistics": {},
        }
    }


async def _build(platform_name: str, data: dict, client=None, options=None) -> list:
    from importlib import import_module

    from custom_components.addhon.const import DOMAIN

    platform = import_module(f"custom_components.addhon.{platform_name}")
    coordinator = RefreshingCoordinator(data)
    hass = RecordingHass(
        {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": client}}}
    )
    added: list = []
    await platform.async_setup_entry(hass, FakeEntry(options=options), added.extend)
    for entity in added:
        entity.hass = hass
    return [e for e in added if not getattr(e, "_addhon_account", False)]


async def _switches(appliance=None, attributes=None, client=None) -> dict:
    client = RunningClient() if client is None else client
    added = await _build(
        "switch", _entry_data(attributes, appliance=appliance), client=client
    )
    return {e._attr_unique_id: e for e in added}


class RefModeSwitchFixtureTest(unittest.TestCase):
    """Anti-vacuity. Everything below asserts what a payload does NOT contain, and each of
    those assertions is equally satisfied by a fixture that never had it."""

    def test_the_fixture_declares_the_full_four_flag_reset(self) -> None:
        # If `stopProgram` carried one flag, "the off body has one key" would be a fact
        # about the fixture instead of about the code.
        stop = _appliance().commands["stopProgram"]
        self.assertEqual(
            {"holidayMode", "intelligenceMode", "quickModeZ1", "quickModeZ2"},
            set(stop.parameters),
        )

    def test_the_fixture_marks_no_stop_flag_mandatory(self) -> None:
        # A mandatory flag rides along whatever the entity asks for
        # (`command_dispatch.py`), so this is the schema property the one-key body rests
        # on. Both real REF dumps and the app fixture agree on it.
        for name, param in _appliance().commands["stopProgram"].parameters.items():
            self.assertFalse(param.mandatory, name)

    def test_the_fixture_start_program_really_swaps(self) -> None:
        # The gate and the ON path both depend on the categorised shape. Exercise the swap
        # directly so the fixture is proven capable of what the tests below rely on.
        appliance = _appliance()
        start = appliance.commands["startProgram"]
        self.assertEqual("PROGRAMS.REF.AUTO_SET", start.category)
        start.parameters["program"].value = "super_cool"
        self.assertIsNot(start, appliance.commands["startProgram"])
        self.assertEqual(
            "PROGRAMS.REF.SUPER_COOL", appliance.commands["startProgram"].category
        )

    def test_the_program_enum_is_the_seven_the_app_declares(self) -> None:
        from custom_components.addhon.ref_programs import offered_codes

        self.assertEqual(
            {"auto_set", "super_cool", "super_freeze", "holiday",
             "quick_cool", "zero_fresh", "fruit_and_veg"},
            set(offered_codes(_appliance())),
        )


class RefModeSwitchGatingTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_four_modes_become_four_switches(self) -> None:
        self.assertEqual(
            ["ref-1_auto_set", "ref-1_super_cool", "ref-1_super_freeze",
             "ref-1_holiday_mode"],
            list(await _switches()),
        )

    async def test_the_my_zone_programs_are_not_switches(self) -> None:
        # quick_cool / zero_fresh / fruit_and_veg write tempSelZ3 and `stopProgram` does
        # not touch that register: the drawer has no off, so it is a select elsewhere.
        for key in ("quick_cool", "zero_fresh", "fruit_and_veg"):
            self.assertNotIn(f"ref-1_{key}", await _switches(), key)

    async def test_a_mode_absent_from_the_live_enum_gets_no_switch(self) -> None:
        # Built from the device's OWN catalogue, never hard-coded: a fridge with no
        # Holiday program gets three switches, not a fourth that writes into nothing.
        categories = {
            key: body for key, body in REF_START_CATEGORIES.items()
            if key != "PROGRAMS.REF.HOLIDAY"
        }
        added = await _switches(_appliance(categories=categories))
        self.assertNotIn("ref-1_holiday_mode", added)
        self.assertIn("ref-1_super_cool", added)

    async def test_a_mode_absent_from_stop_program_gets_no_switch(self) -> None:
        # The OFF half of the gate. A mode we could start and never stop would be
        # strandable from Home Assistant: the user would have to walk to the appliance.
        stop = {
            name: body for name, body in REF_STOP_PARAMS.items()
            if name != "quickModeZ2"
        }
        added = await _switches(_appliance(stop_params=stop))
        self.assertNotIn("ref-1_super_freeze", added)
        self.assertIn("ref-1_super_cool", added)

    async def test_a_fridge_without_stop_program_gets_no_mode_switches(self) -> None:
        self.assertEqual({}, await _switches(_appliance(with_stop=False)))

    async def test_the_switch_survives_a_silent_shadow(self) -> None:
        # The gate is on the WRITE schema and NOT on the reported attribute: a fridge that
        # was disconnected at setup publishes nothing, and a read gate would remove four
        # CONTROLS until the next reload. The state is honestly unknown instead.
        added = await _switches(attributes={"available": True})
        self.assertEqual(4, len(added))
        self.assertIsNone(added["ref-1_super_cool"].is_on)

    async def test_fr_and_fre_get_them_too(self) -> None:
        for app_type in ("FR", "FRE"):
            data = _entry_data()
            data["ref-1"]["type"] = app_type
            added = await _build("switch", data, client=RunningClient())
            self.assertEqual(
                4, len(added), f"type {app_type} should get four switches"
            )

    async def test_another_type_does_not_inherit_them(self) -> None:
        data = _entry_data()
        data["ref-1"]["type"] = "OV"
        self.assertEqual([], await _build("switch", data, client=RunningClient()))


class RefModeSwitchReadTest(unittest.IsolatedAsyncioTestCase):
    async def test_it_reads_the_flag_back(self) -> None:
        attributes = copy.deepcopy(REF_ATTRIBUTES)
        attributes["intelligenceMode"] = 1
        added = await _switches(attributes=attributes)
        self.assertIs(True, added["ref-1_auto_set"].is_on)
        self.assertIs(False, added["ref-1_super_cool"].is_on)

    async def test_an_unreadable_flag_is_unknown_rather_than_off(self) -> None:
        # A fridge that stopped reporting the flag has not told us the mode is off, and an
        # entity that claims "off" invites an automation to "fix" it.
        attributes = copy.deepcopy(REF_ATTRIBUTES)
        attributes.pop("quickModeZ1")
        entity = (await _switches(attributes=attributes))["ref-1_super_cool"]
        self.assertIsNone(entity.is_on)

    async def test_each_switch_reads_its_own_register(self) -> None:
        # The bug in reverse: four entities that shared a reading would look right in any
        # single-flag test.
        for flag, key in (
            ("intelligenceMode", "ref-1_auto_set"),
            ("quickModeZ1", "ref-1_super_cool"),
            ("quickModeZ2", "ref-1_super_freeze"),
            ("holidayMode", "ref-1_holiday_mode"),
        ):
            attributes = copy.deepcopy(REF_ATTRIBUTES)
            attributes[flag] = 1
            added = await _switches(attributes=attributes)
            on = {uid for uid, entity in added.items() if entity.is_on}
            self.assertEqual({key}, on, flag)


class RefModeSwitchOffPayloadTest(unittest.IsolatedAsyncioTestCase):
    """THE REGRESSION THIS WHOLE CHANGE EXISTS TO PREVENT.

    `select.ref_program` clears a mode with a full-command `stopProgram`, which serialises
    every parameter the command declares -- on this family all four flags -- so switching
    Super Cool off also switched Auto-set, Super Freeze and Holiday off. The official app
    never does that: `apk/dump/ref_10136/attributes.json` records it sending
    `{"commandName": "stopProgram", "parameters": {"intelligenceMode": "0"}}` and nothing
    else. Every assertion here is about the SIZE of the key set, not about one key: a test
    that read one key out of the dict would be equally satisfied by a dict with four.
    """

    async def test_switching_a_mode_off_carries_that_flag_alone(self) -> None:
        appliance = _appliance()
        added = await _switches(appliance)
        await added["ref-1_super_cool"].async_turn_off()
        sent = appliance.api.sent[-1]
        self.assertEqual("stopProgram", sent["command"])
        self.assertEqual({"quickModeZ1": "0"}, sent["parameters"])

    async def test_no_mode_off_ever_clears_another_mode(self) -> None:
        # Stated as the HARM rather than as a key set, so whichever direction a future
        # edit moves back to the full-command sender, this fails.
        for key, flag in (
            ("ref-1_auto_set", "intelligenceMode"),
            ("ref-1_super_cool", "quickModeZ1"),
            ("ref-1_super_freeze", "quickModeZ2"),
            ("ref-1_holiday_mode", "holidayMode"),
        ):
            appliance = _appliance()
            added = await _switches(appliance)
            await added[key].async_turn_off()
            payload = appliance.api.sent[-1]["parameters"]
            self.assertEqual(1, len(payload), (key, payload))
            self.assertEqual({flag}, set(payload), key)
            self.assertEqual("0", payload[flag], key)

    async def test_an_off_carries_no_ancillary_block(self) -> None:
        # `stopProgram` declares no ancillaryParameters on this family; anything here
        # would be a value we invented.
        appliance = _appliance()
        added = await _switches(appliance)
        await added["ref-1_holiday_mode"].async_turn_off()
        self.assertEqual({}, appliance.api.sent[-1]["ancillary"])

    async def test_an_off_never_ships_a_program_name(self) -> None:
        appliance = _appliance()
        added = await _switches(appliance)
        await added["ref-1_auto_set"].async_turn_off()
        self.assertEqual("", appliance.api.sent[-1]["program_name"])

    async def test_an_off_does_not_swap_the_start_program_command(self) -> None:
        # The half the wire cannot show: the OFF must not touch the program selector, or
        # the engine would REPLACE `appliance.commands['startProgram']` permanently.
        appliance = _appliance()
        start = appliance.commands["startProgram"]
        added = await _switches(appliance)
        await added["ref-1_super_freeze"].async_turn_off()
        self.assertIs(start, appliance.commands["startProgram"])


class RefModeSwitchOnPayloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_switching_a_mode_on_is_the_body_the_device_executed(self) -> None:
        # Byte-for-byte the `startProgram` the reporter's own `commandHistory` carries with
        # both a `timestampAccepted` and a `timestampExecuted`, sent by THIS integration
        # (`deviceModel: "addhon"`) on 2026-08-20.
        appliance = _appliance()
        added = await _switches(appliance)
        await added["ref-1_auto_set"].async_turn_on()
        sent = appliance.api.sent[-1]
        self.assertEqual("startProgram", sent["command"])
        self.assertEqual({"intelligenceMode": "1"}, sent["parameters"])
        self.assertEqual("PROGRAMS.REF.AUTO_SET", sent["program_name"])

    async def test_an_on_is_swap_aware(self) -> None:
        # The fixture starts on `auto_set`, so turning Super Cool on has to move to
        # another category and send THAT one, not the stale object it started from.
        appliance = _appliance()
        added = await _switches(appliance)
        await added["ref-1_super_cool"].async_turn_on()
        sent = appliance.api.sent[-1]
        self.assertEqual({"quickModeZ1": "1"}, sent["parameters"])
        self.assertEqual("PROGRAMS.REF.SUPER_COOL", sent["program_name"])
        self.assertEqual(
            "PROGRAMS.REF.SUPER_COOL", appliance.commands["startProgram"].category
        )

    async def test_an_on_never_ships_the_program_selector(self) -> None:
        # `program` is one of the dispatcher's `_SELECTOR_KEYS`. Naming it would send it
        # verbatim to the cloud inside `parameters`, a field the app never carries. The
        # program sender writes the parameter to swap the category and puts only the
        # category's own `parameters` group on the wire.
        appliance = _appliance()
        added = await _switches(appliance)
        for key in ("ref-1_super_freeze", "ref-1_holiday_mode"):
            await added[key].async_turn_on()
        for sent in appliance.api.sent:
            self.assertNotIn("program", sent["parameters"], sent)
            self.assertNotIn("category", sent["parameters"], sent)
            self.assertNotIn("program", sent["ancillary"], sent)
            self.assertNotIn("category", sent["ancillary"], sent)

    async def test_turning_one_mode_on_writes_no_other_flag(self) -> None:
        # The read side of the reporter's claim: "You can have my zone at 0 and also
        # select super cool." Two modes coexist because neither start clears the other.
        appliance = _appliance()
        added = await _switches(appliance)
        await added["ref-1_auto_set"].async_turn_on()
        await added["ref-1_super_cool"].async_turn_on()
        bodies = [s["parameters"] for s in appliance.api.sent]
        self.assertEqual([{"intelligenceMode": "1"}, {"quickModeZ1": "1"}], bodies)
        self.assertEqual(
            ["startProgram", "startProgram"],
            [s["command"] for s in appliance.api.sent],
        )


class RefModeSwitchErrorTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_write_refreshes_the_state(self) -> None:
        added = await _switches()
        entity = added["ref-1_super_cool"]
        await entity.async_turn_on()
        self.assertEqual(1, entity.coordinator.refreshes)
        await entity.async_turn_off()
        self.assertEqual(2, entity.coordinator.refreshes)

    async def test_a_missing_client_raises_the_localized_error(self) -> None:
        added = await _build("switch", _entry_data(), client=None)
        switch_entity = next(e for e in added if e._attr_unique_id == "ref-1_auto_set")
        with self.assertRaises(HomeAssistantError) as caught:
            await switch_entity.async_turn_on()
        self.assertEqual(
            "appliance_or_client_unavailable", caught.exception.translation_key
        )

    async def test_a_transport_failure_becomes_a_localized_command_error(self) -> None:
        for direction in ("async_turn_on", "async_turn_off"):
            added = await _switches(client=RunningClient(fail=RuntimeError("boom")))
            entity = added["ref-1_holiday_mode"]
            with self.assertRaises(HomeAssistantError) as caught:
                await getattr(entity, direction)()
            self.assertEqual(
                "command_error", caught.exception.translation_key, direction
            )
            # No refresh, and no state invented: a refused command leaves the entity
            # reporting whatever the appliance last said.
            self.assertEqual(0, entity.coordinator.refreshes, direction)


class RefModeBinarySensorTest(unittest.IsolatedAsyncioTestCase):
    """The four readings STAY, and start disabled."""

    async def test_the_four_readings_are_still_created(self) -> None:
        added = await _build("binary_sensor", _entry_data())
        ids = {e._attr_unique_id for e in added}
        for key in ("quick_cool", "quick_freeze", "auto_set", "holiday_mode"):
            self.assertIn(f"ref-1_{key}", ids, key)

    @staticmethod
    def _hidden(entity) -> bool:
        """What Home Assistant would read off the entity, resolved core's way.

        `Entity.entity_registry_enabled_default` is a property that returns
        `self._attr_entity_registry_enabled_default` when the instance set one and falls
        back to the description; the stub base here has neither, so the order is
        reproduced. Reading the description alone would miss the whole point of #93's
        correction: the decision is per DEVICE, and the description is shared.
        """
        if hasattr(entity, "_attr_entity_registry_enabled_default"):
            return not entity._attr_entity_registry_enabled_default
        return not getattr(
            entity.entity_description, "entity_registry_enabled_default", True
        )

    async def test_the_four_readings_are_hidden_where_a_switch_replaces_them(
        self,
    ) -> None:
        # Not removed: every dashboard and automation built on them since 5.x keeps
        # working, and Home Assistant reads this flag only at FIRST registration, so an
        # existing user is untouched while a new one is spared the duplicate.
        added = await _build("binary_sensor", _entry_data())
        by_id = {e._attr_unique_id: e for e in added}
        for key in ("quick_cool", "quick_freeze", "auto_set", "holiday_mode"):
            self.assertTrue(self._hidden(by_id[f"ref-1_{key}"]), key)

    async def test_a_reading_with_no_switch_stays_visible(self) -> None:
        """THE CORRECTION. The hiding is per DEVICE, not per description row.

        The switch is gated on the write schema and the reading on the shadow, so the two
        sets do not coincide. On a fridge whose `stopProgram` declares only `quickModeZ1`
        exactly one switch is built -- and a static flag on the row would have hidden all
        four readings anyway, leaving that appliance with one control and nothing to
        watch, permanently, because Home Assistant applies the flag only at first
        registration.
        """
        stop = {"quickModeZ1": REF_STOP_PARAMS["quickModeZ1"]}
        appliance = _appliance(stop_params=stop)
        switches = await _switches(appliance)
        self.assertEqual(["ref-1_super_cool"], list(switches))

        added = await _build(
            "binary_sensor", _entry_data(appliance=_appliance(stop_params=stop))
        )
        by_id = {e._attr_unique_id: e for e in added}
        self.assertTrue(self._hidden(by_id["ref-1_quick_cool"]), "has a switch")
        for key in ("quick_freeze", "auto_set", "holiday_mode"):
            self.assertFalse(self._hidden(by_id[f"ref-1_{key}"]), key)

    async def test_the_other_fridge_readings_are_untouched(self) -> None:
        # Only the four registers a switch owns are hidden; the door and the connectivity
        # sensor have no control beside them and must stay visible.
        added = await _build("binary_sensor", _entry_data())
        by_id = {e._attr_unique_id: e for e in added}
        for key in ("door_zone1", "door2_zone1", "connectivity"):
            self.assertFalse(self._hidden(by_id[f"ref-1_{key}"]), key)

    def test_no_description_row_hides_itself(self) -> None:
        # The decision may not migrate back onto the shared table: a row that hid itself
        # would hide on every appliance of the type, including the ones with no switch.
        from custom_components.addhon import binary_sensor

        for app_type, descs in binary_sensor.BINARY_SENSORS.items():
            for desc in descs:
                self.assertTrue(
                    getattr(desc, "entity_registry_enabled_default", True),
                    f"{app_type}.{desc.key}",
                )


class RefModeSwitchTranslationTest(unittest.TestCase):
    """Every mode switch must be named in both languages, and the two must agree."""

    def _switch_keys(self, path: Path) -> set[str]:
        return set(json.loads(path.read_text(encoding="utf-8"))["entity"]["switch"])

    def test_every_mode_key_is_translated(self) -> None:
        from custom_components.addhon import switch

        required = {desc.key for desc in switch._REF_MODE_SWITCHES}
        component = REPO_ROOT / "custom_components" / "addhon"
        for path in (
            component / "strings.json",
            component / "translations" / "en.json",
            component / "translations" / "it.json",
        ):
            missing = required - self._switch_keys(path)
            self.assertFalse(missing, f"{path.name}: missing {sorted(missing)}")

    def test_the_switch_and_its_reading_are_told_apart(self) -> None:
        """The pair must not render as two entities with one name.

        `_attr_has_entity_name` is True (base_entity.py), so a shared translated name
        makes both rows read literally `HFW7720EWMP Auto-set` on the same device -- and
        `_REF_MODE_ICONS` copies the readings' icons deliberately, so the icon does not
        tell them apart either. Four such pairs appear on every fridge that already has
        the readings enabled, because Home Assistant applies
        `entity_registry_enabled_default` only at FIRST registration.

        The reading is the one renamed, not the switch: the switch is the control a new
        user reaches for, and it inherits the name the reading used to carry.
        """
        component = REPO_ROOT / "custom_components" / "addhon"
        for lang in ("en", "it"):
            data = json.loads(
                (component / "translations" / f"{lang}.json").read_text(encoding="utf-8")
            )
            entity = data["entity"]
            for switch_key, binary_key in (
                ("auto_set", "auto_set"),
                ("super_cool", "quick_cool"),
                ("super_freeze", "quick_freeze"),
                ("holiday_mode", "holiday_mode"),
            ):
                switch_name = entity["switch"][switch_key]["name"]
                reading_name = entity["binary_sensor"][binary_key]["name"]
                self.assertNotEqual(
                    reading_name, switch_name, f"[{lang}] {switch_key}"
                )
                # ...and told apart by a SUFFIX, so the pair still reads as one subject:
                # the reading is the switch's name plus a qualifier, never a new word.
                self.assertTrue(
                    reading_name.startswith(switch_name),
                    f"[{lang}] {binary_key}: {reading_name!r} should extend "
                    f"{switch_name!r}",
                )

    def test_no_two_fridge_entities_share_a_translated_name(self) -> None:
        """The general rule, over every platform this release touches.

        Asserted across the whole fridge surface rather than on the four pairs alone:
        the duplicate was introduced by adding a control beside an existing reading, and
        nothing stopped the next one from doing it again.
        """
        component = REPO_ROOT / "custom_components" / "addhon"
        keys = {
            "switch": ("auto_set", "super_cool", "super_freeze", "holiday_mode"),
            "binary_sensor": ("auto_set", "quick_cool", "quick_freeze", "holiday_mode",
                              "door_zone1", "door2_zone1", "door_zone2", "door_zone3",
                              "door_zone4", "connectivity"),
            "select": ("my_zone_mode",),
            # `sensor.my_zone_mode` shares the select's name and is deliberately absent
            # here: the two never coexist. `sensor.async_setup_entry` skips the sensor on
            # exactly the appliances where the writable select is built, which is what
            # `RefMyZoneSuppressionTest` in tests/test_ref_program_select.py pins. If that
            # suppression is ever removed, that test fails first and this sweep would
            # otherwise report the same defect twice.
            "sensor": ("errors", "temp_zone1", "temp_zone2"),
            "number": ("target_temp_zone1", "target_temp_zone2"),
        }
        for lang in ("en", "it"):
            entity = json.loads(
                (component / "translations" / f"{lang}.json").read_text(encoding="utf-8")
            )["entity"]
            seen: dict[str, str] = {}
            for platform, names in keys.items():
                for key in names:
                    name = entity[platform][key]["name"]
                    self.assertNotIn(
                        name, seen,
                        f"[{lang}] {platform}.{key} and {seen.get(name)} both render "
                        f"as {name!r} on the same device",
                    )
                    seen[name] = f"{platform}.{key}"


class RefSupersessionInvariantTest(unittest.IsolatedAsyncioTestCase):
    """The predicate and the platforms answer one question, measured on both platforms.

    This lives here and not beside the other supersession tests because it is the only
    file that can build the switch platform AND the select platform against one
    appliance: the invariant is about what the two of them together really create, and
    an assertion that recomputes `has_replacement_controls`' own definition -- as the
    first version of this test did -- moves both sides together and can never fail.

    What it protects: `select.ref_program` steps aside on the strength of that predicate,
    so a shape where the predicate says yes and NEITHER platform builds anything leaves
    the appliance with no mode control at all. That is exactly what happened while the
    model-zone test lived in the select's own setup instead of inside `my_zone_codes`.
    """

    async def _controls(self, appliance) -> tuple[set[str], set[str]]:
        data = _entry_data(appliance=appliance)
        selects = await _build("select", data, client=RunningClient())
        switches = await _build(
            "switch", _entry_data(appliance=appliance), client=RunningClient()
        )
        return (
            {e._attr_translation_key for e in selects},
            {e._attr_translation_key for e in switches},
        )

    def _shapes(self) -> dict:
        """Four fridges, chosen so each half of the predicate is exercised alone."""
        no_zones = _appliance()
        no_zones.model_attributes = {}
        flags_only = _appliance()
        flags_only.model_attributes = {"zones": "fridge|freezer"}
        drawer_only = _appliance(
            stop_params={"onOffStatus": {
                "typology": "fixed", "category": "command", "mandatory": 0,
                "fixedValue": "0",
            }}
        )
        neither = _appliance(
            stop_params={"onOffStatus": {
                "typology": "fixed", "category": "command", "mandatory": 0,
                "fixedValue": "0",
            }}
        )
        neither.model_attributes = {"zones": "fridge|freezer"}
        return {
            "flags and drawer": _appliance(),
            "no zones attribute at all": no_zones,
            "zones without the drawer": flags_only,
            "drawer but no clearable flag": drawer_only,
            "neither": neither,
        }

    async def test_a_superseded_fridge_always_gets_a_replacement(self) -> None:
        from custom_components.addhon.ref_programs import has_replacement_controls

        for label, appliance in self._shapes().items():
            selects, switches = await self._controls(appliance)
            replaced = has_replacement_controls(appliance)
            built = bool(switches) or ("my_zone_mode" in selects)
            self.assertEqual(replaced, built, label)

    async def test_no_fridge_is_left_without_a_mode_control(self) -> None:
        """The harm, asserted directly. Whatever the predicate answers, the appliance
        ends up with something: the per-mode controls, or the single select."""
        for label, appliance in self._shapes().items():
            selects, switches = await self._controls(appliance)
            self.assertTrue(
                switches or ("my_zone_mode" in selects) or ("ref_program" in selects),
                f"{label}: no mode control at all",
            )

    async def test_the_single_select_and_its_replacements_never_coexist(self) -> None:
        for label, appliance in self._shapes().items():
            selects, switches = await self._controls(appliance)
            if switches or "my_zone_mode" in selects:
                self.assertNotIn("ref_program", selects, label)
            else:
                self.assertIn("ref_program", selects, label)


class RefModeDiagnosticsRowTest(unittest.TestCase):
    """What the dump says the four switches touch, and on what terms.

    The rows were added with `read`, `write` and `write_command` and nothing looked at
    any of them: changing `write_command` to `"settings"` silently dropped all four
    writes from every dump (a fridge's `settings` declares only the `tempSel*`), and
    deleting the `write` halves outright was equally invisible -- the drift guard counts
    names and the `read` halves alone keep both the count and the domain intact.
    """

    _EXPECTED = {
        "switch.auto_set": "intelligenceMode",
        "switch.super_cool": "quickModeZ1",
        "switch.super_freeze": "quickModeZ2",
        "switch.holiday_mode": "holidayMode",
    }

    def _row(self, tag: str) -> dict:
        from custom_components.addhon import diagnostics

        for entry in diagnostics._CUSTOM_ENTITY_SOURCES:
            if entry.get("tag") == tag:
                return entry
        raise AssertionError(f"no {tag} row in _CUSTOM_ENTITY_SOURCES")

    def test_each_switch_declares_its_one_flag_on_both_halves(self) -> None:
        # Read and write are the SAME single register, which is the whole shape of these
        # entities: the shadow reports it, a sparse `stopProgram` clears it by name.
        for tag, flag in self._EXPECTED.items():
            row = self._row(tag)
            self.assertEqual((flag,), tuple(row.get("read") or ()), tag)
            self.assertEqual((flag,), tuple(row.get("write") or ()), tag)

    def test_the_write_is_declared_under_stop_program(self) -> None:
        # Named against the command that really carries the parameter. Point it at
        # `settings` and `_declared_write_only` narrows against a command that declares
        # only the setpoints, so every write disappears from every dump and the four
        # switches read as if they could not act.
        from custom_components.addhon import diagnostics

        for tag in self._EXPECTED:
            self.assertEqual(
                "stopProgram", diagnostics._FIXED_WRITE_COMMANDS.get(tag), tag
            )

    def test_the_write_survives_a_device_that_declares_the_flag(self) -> None:
        from custom_components.addhon import diagnostics

        declared = {"stopProgram": set(REF_STOP_PARAMS), "settings": {"tempSelZ1"}}
        for tag, flag in self._EXPECTED.items():
            narrowed = diagnostics._declared_write_only(
                self._row(tag), tag, declared
            )
            self.assertEqual([flag], narrowed.get("write"), tag)

    def test_the_write_is_dropped_on_a_device_that_does_not(self) -> None:
        # The honest half: a fridge whose `stopProgram` cannot clear this flag gets no
        # switch either, so a dump claiming the write would send a reader looking for a
        # control that was never built.
        from custom_components.addhon import diagnostics

        declared = {"stopProgram": {"quickModeZ1"}, "settings": {"tempSelZ1"}}
        narrowed = diagnostics._declared_write_only(
            self._row("switch.super_freeze"), "switch.super_freeze", declared
        )
        self.assertNotIn("write", narrowed)
        self.assertEqual(("quickModeZ2",), tuple(narrowed.get("read")))

    def test_an_unread_schema_narrows_nothing(self) -> None:
        # `None` is "not looked", which must never be printed as "the device does not
        # declare it" -- the distinction `_declared_command_params` exists to keep.
        from custom_components.addhon import diagnostics

        narrowed = diagnostics._declared_write_only(
            self._row("switch.auto_set"), "switch.auto_set", None
        )
        self.assertEqual(("intelligenceMode",), tuple(narrowed.get("write")))

    def test_the_my_zone_select_declares_the_register_it_reads(self) -> None:
        # Deleting this row was invisible: `select` stays in the REF domain set through
        # `ref_program`, and one name is inside the count floor's slack.
        row = self._row("select.my_zone_mode")
        self.assertEqual(("tempSelZ3",), tuple(row.get("read") or ()))
        # No write half, deliberately: the select changes `tempSelZ3` by STARTING a
        # program whose category pins it, and on the appliance it was built for
        # `tempSelZ3` is not a `settings` parameter at all.
        self.assertNotIn("write", row)


if __name__ == "__main__":
    unittest.main()
