# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the fridge (REF/FR/FRE) writable program/mode select, discussion #40.

The fridge modes (super cool, super freeze, holiday, iot_* presets) are startProgram
PROGRAMS, not writable booleans, cleared by a GLOBAL stopProgram. This select exposes
``off`` + the live ``startProgram.program`` enum and sends IMMEDIATELY: a program ->
``startProgram(program=X)`` (swap-aware), ``off`` -> ``stopProgram``. ``current_option`` is
read from the live device mode FLAGS, never from ``startProgram.program``.

Reuses the HA stub harness installed by test_program_select.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

# Importing this installs the homeassistant stubs and gives us the fakes.
from test_program_select import (  # noqa: E402
    FakeClient,
    FakeCoordinator,
    FakeEntry,
    FakeHass,
    Param,
    RecordingCommand,
)

# roberglezz's real fridge (HCW58F18EWMP) program enum, live order.
ROB_PROGRAMS = [
    "holiday",
    "iot_daily_use",
    "iot_extra_cold",
    "iot_extra_ice",
    "iot_high_efficiency",
    "iot_special_food_core",
    "super_cool",
    "super_freeze",
]


def _ref(commands: dict, attributes: dict | None = None, app_id: str = "ref-1") -> dict:
    return {
        app_id: {
            "type": "REF",
            "name": "Fridge",
            "appliance": types.SimpleNamespace(commands=commands),
            "attributes": attributes or {},
            "settings": {},
        }
    }


def _ref_commands(programs=None, with_stop=True, stop_params=None) -> dict:
    commands = {
        # The fridge's setParameters command (no program param): must be ignored as a
        # program source, exactly like a real REF.
        "settings": RecordingCommand({"tempSelZ1": Param("3", values=["1", "2", "3"])}),
        "startProgram": RecordingCommand(
            {"program": Param(values=list(programs if programs is not None else ROB_PROGRAMS))}
        ),
    }
    if with_stop:
        commands["stopProgram"] = RecordingCommand(
            stop_params
            if stop_params is not None
            else {
                "quickModeZ1": Param("0", values=["0"]),
                "quickModeZ2": Param("0", values=["0"]),
                "holidayMode": Param("0", values=["0"]),
            }
        )
    return commands


def _surviving_commands(programs=None) -> dict:
    """A fridge on which `select.ref_program` is still the RIGHT control (#93).

    Its `stopProgram` declares nothing this enum's flag modes could clear, so no mode
    switch is buildable, `has_replacement_controls` is false, and the single select stays
    the appliance's only stop control. This is the shape every setup-level assertion about
    the select has to use now that the default fixture -- three flag modes with their
    `stopProgram` parameters -- is precisely the shape that supersedes it.
    """
    return _ref_commands(
        programs=programs, stop_params={"onOffStatus": Param("0", values=["0"])}
    )


# rmxs's HFW7720EWMP (#93), live program enum, exactly as his dump prints it.
RMXS_PROGRAMS = [
    "auto_set", "fruit_and_veg", "holiday", "iot_daily_use", "iot_extra_cold",
    "iot_extra_ice", "iot_high_efficiency", "iot_special_food_core", "quick_cool",
    "super_cool", "super_freeze", "zero_fresh",
]


class FixedParam:
    """A program category's PINNED parameter: `typology: "fixed"` plus its value."""

    typology = "fixed"

    def __init__(self, value) -> None:
        self.value = value


class EnumParam:
    """A category's ancillary enum (`zone`, `programFamily`), engine-CLEANED.

    `HonParameterEnum.values` pushes every member through `clean_value` -- strip "[]",
    "|"->"_", lowercase (client/engine/parameter/enum.py) -- so the schema's `vtRoom1`
    reaches any reader as `vtroom1`. The fixtures carry the cleaned spelling because that
    is what production code sees, and it is why `ref_programs` folds case while
    `model_zones` does not.
    """

    typology = "enum"

    def __init__(self, values) -> None:
        self.values = [str(value) for value in values]
        self.value = self.values[0] if self.values else ""


class Category:
    def __init__(self, parameters=None) -> None:
        self.parameters = parameters or {}


def _favourite(base: Category) -> Category:
    """A user's saved favourite, built the way the engine builds one.

    `command_loader._add_favourites` copies a catalogue category, adds a `favourite`
    parameter to the copy and files it under `favouriteName` -- so it inherits the
    base's `programFamily`, its `zone` and its pinned parameters, and only that extra
    parameter tells the two apart.
    """
    return Category({**base.parameters, "favourite": FixedParam("1")})


class CategorisedStartProgram(RecordingCommand):
    """A startProgram carrying the per-program catalogue, like the real engine.

    `RecordingCommand` alone has `parameters` and nothing else, which is exactly the
    shape of a fridge whose catalogue we cannot see -- kept, because that is the shape
    every pre-existing test in this file uses.
    """

    def __init__(self, parameters, categories) -> None:
        super().__init__(parameters)
        self.categories = categories


def _dashboard(zone, params=None):
    return Category({
        **(params or {}),
        "zone": EnumParam(zone),
        "programFamily": EnumParam(["dashboard"]),
    })


def _download(zone, params):
    return Category({
        **params,
        "zone": EnumParam(zone),
        "programFamily": EnumParam(["download"]),
    })


def _rmxs_categories() -> dict:
    """The catalogue behind rmxs's enum.

    Two sources, because no single one has it all. The enum, the four stopProgram flags
    and the ancillary SHAPE come from his own dump
    (diagnostics/issue93-dumps/rmxs-HFW7720EWMP-2026-08-27.json, which prints only the
    ACTIVE category). The per-category parameters come from HTW7720ENMP, the 3-door twin
    embedded in the APK at decomp.txt:3495902 -- same `series`, same `option` string,
    same `sensor` string -- with its download presets filled in from
    apk/dump/ref_10136/commands.json, the only production catalogue we hold that has any.
    """
    return {
        "auto_set": _dashboard(
            ["fridge", "freezer"], {"intelligenceMode": FixedParam("1")}
        ),
        "super_cool": _dashboard(["fridge"], {"quickModeZ1": FixedParam("1")}),
        "super_freeze": _dashboard(["freezer"], {"quickModeZ2": FixedParam("1")}),
        # Holiday's zone DOES name the drawer and its rules DO force tempSelZ3 to 17 --
        # but through `programRules`, never as a parameter of its own, and the rules
        # engine only mutates a parameter the command already declares. Both halves of
        # the drawer gate reject it, independently.
        "holiday": _dashboard(["fridge", "vtroom1"], {"holidayMode": FixedParam("1")}),
        "zero_fresh": _dashboard(["vtroom1"], {"tempSelZ3": FixedParam("0")}),
        "quick_cool": _dashboard(["vtroom1"], {"tempSelZ3": FixedParam("2")}),
        "fruit_and_veg": _dashboard(["vtroom1"], {"tempSelZ3": FixedParam("5")}),
        "iot_daily_use": _download(
            ["fridge", "freezer", "vtroom1"],
            {"tempSelZ1": FixedParam("4"), "tempSelZ2": FixedParam("-18"),
             "tempSelZ3": FixedParam("5")},
        ),
        "iot_extra_cold": _download(
            ["fridge", "freezer", "vtroom1"],
            {"tempSelZ1": FixedParam("2"), "tempSelZ2": FixedParam("-24"),
             "tempSelZ3": FixedParam("2")},
        ),
        "iot_extra_ice": _download(["freezer"], {"tempSelZ2": FixedParam("-24")}),
        "iot_high_efficiency": _download(
            ["fridge", "freezer", "vtroom1"],
            {"tempSelZ1": FixedParam("6"), "tempSelZ2": FixedParam("-18"),
             "tempSelZ3": FixedParam("5")},
        ),
        # The ONE download preset zoned on the drawer alone. It pins tempSelZ3 and its
        # zone is exactly {vtRoom1}, so it passes two thirds of the drawer gate: this
        # entry is what makes the programFamily third load-bearing rather than decorative.
        "iot_special_food_core": _download(
            ["vtroom1"], {"tempSelZ3": FixedParam("5")}
        ),
    }


def _rmxs_commands(categories=None, programs=None, stop=True) -> dict:
    commands = {
        "settings": RecordingCommand({
            "tempSelZ1": Param("3", values=[str(v) for v in range(1, 10)]),
            "tempSelZ2": Param("-18", values=[str(v) for v in range(-24, -13)]),
        }),
        "startProgram": CategorisedStartProgram(
            {"program": Param(
                "auto_set",
                values=list(RMXS_PROGRAMS if programs is None else programs),
            )},
            _rmxs_categories() if categories is None else categories,
        ),
    }
    if stop:
        commands["stopProgram"] = RecordingCommand({
            "holidayMode": Param("0", values=["0"]),
            "intelligenceMode": Param("0", values=["0"]),
            "quickModeZ1": Param("0", values=["0"]),
            "quickModeZ2": Param("0", values=["0"]),
        })
    return commands


def _fridge(commands, attributes=None, zones="fridge|freezer|vtRoom1",
            app_type="REF", app_id="ref-1") -> dict:
    """`_ref` plus the model catalogue, which the drawer gate reads and `_ref` omits."""
    return {
        app_id: {
            "type": app_type,
            "name": "Fridge",
            "appliance": types.SimpleNamespace(
                commands=commands,
                model_attributes={} if zones is None else {"zones": zones},
            ),
            "attributes": attributes or {},
            "settings": {},
        }
    }


class RefProgramSelectSetupTest(unittest.IsolatedAsyncioTestCase):
    async def _setup(self, data) -> list:
        from custom_components.addhon.const import DOMAIN
        from custom_components.addhon import select

        coordinator = FakeCoordinator(data)
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}}
        )
        added: list = []
        await select.async_setup_entry(hass, FakeEntry(), added.extend)
        return added

    def _direct(self, commands):
        """Build the select WITHOUT the platform gate.

        The option list is unchanged behaviour and still worth pinning, but since #93 the
        gate refuses to create this entity on a fridge that also gets the per-mode
        controls -- which the default fixture now is. Constructing it directly keeps the
        two questions apart: what the options ARE, and whether the entity is built.
        """
        from custom_components.addhon.select import HonRefProgramSelect

        return HonRefProgramSelect(
            FakeCoordinator(_ref(commands)), "ref-1", FakeClient()
        )

    async def test_not_created_when_flag_switches_can_be_built(self) -> None:
        # THE REVERSAL (#93). This fixture offers `super_cool`/`super_freeze`/`holiday`
        # AND a `stopProgram` declaring all three flags, so each becomes an independent
        # switch that can be cleared on its own. Keeping the single select beside them
        # would put two controls on the same registers with opposite meanings of `off`:
        # the select's sends the four-flag reset the official app never sends.
        added = await self._setup(_ref(_ref_commands()))
        self.assertEqual([], added)

    async def test_created_where_no_per_mode_control_can_be_built(self) -> None:
        # The other side of the same gate: nothing better exists here, so the select is
        # still the right control and is still built.
        added = await self._setup(_ref(_surviving_commands()))
        self.assertEqual(1, len(added))
        self.assertEqual("ref_program", added[0]._attr_translation_key)

    async def test_options_are_off_first_plus_live_enum(self) -> None:
        # off is always the first option; the rest is exactly the live enum SET. (Runtime
        # HonParameterProgram.values sorts, so we assert the set, not the input order.)
        options = self._direct(_ref_commands())._attr_options
        self.assertEqual("off", options[0])
        self.assertEqual(set(ROB_PROGRAMS), set(options[1:]))

    async def test_options_follow_a_different_live_enum(self) -> None:
        # Built from the live enum, NOT hard-coded: a different model -> different options.
        options = self._direct(
            _ref_commands(programs=["super_cool", "auto_set"])
        )._attr_options
        self.assertEqual("off", options[0])
        self.assertEqual({"super_cool", "auto_set"}, set(options[1:]))

    async def test_not_created_without_stopprogram(self) -> None:
        added = await self._setup(_ref(_ref_commands(with_stop=False)))
        self.assertEqual([], added)

    async def test_not_created_without_program_enum(self) -> None:
        commands = {
            "startProgram": RecordingCommand({"program": Param(values=[])}),
            "stopProgram": RecordingCommand({"holidayMode": Param("0", values=["0"])}),
        }
        added = await self._setup(_ref(commands))
        self.assertEqual([], added)

    async def test_fr_and_fre_types_supported(self) -> None:
        for app_type in ("FR", "FRE"):
            data = _ref(_surviving_commands())
            data["ref-1"]["type"] = app_type
            added = await self._setup(data)
            self.assertEqual(1, len(added), f"type {app_type} should get a select")


class RefProgramSelectBehaviourTest(unittest.IsolatedAsyncioTestCase):
    def _entity(self, commands, attributes=None):
        from custom_components.addhon.select import HonRefProgramSelect

        coordinator = FakeCoordinator(_ref(commands, attributes))
        entity = HonRefProgramSelect(coordinator, "ref-1", FakeClient())
        entity.hass = FakeHass()
        return entity, coordinator

    async def test_select_program_sends_startprogram(self) -> None:
        commands = _ref_commands()
        entity, coordinator = self._entity(commands)

        await entity.async_select_option("super_cool")

        start = commands["startProgram"]
        self.assertEqual("super_cool", start.parameters["program"].value)
        self.assertEqual(1, start.send_calls)
        self.assertEqual(0, commands["stopProgram"].send_calls)
        self.assertEqual(1, coordinator.refreshes)

    async def test_select_off_sends_stopprogram_only(self) -> None:
        commands = _ref_commands()
        entity, coordinator = self._entity(commands)

        await entity.async_select_option("off")

        self.assertEqual(1, commands["stopProgram"].send_calls)
        # startProgram untouched.
        self.assertEqual(0, commands["startProgram"].send_calls)
        self.assertIsNone(commands["startProgram"].parameters["program"].value)
        self.assertEqual(1, coordinator.refreshes)

    async def test_off_sends_stopprogram_with_no_overrides(self) -> None:
        # Production must call async_send_command for stopProgram with an EMPTY params dict:
        # the device's own schema-fixed "0" flags do the global reset, and we must NOT
        # inject overrides (which would hit the "missing param" raise). Spying the real call
        # makes this mutation-proof against a regression that passed e.g. {"onOffStatus":"0"}.
        from custom_components.addhon import select as select_mod

        calls: list = []

        async def _spy(hass, client, appliance, command_name, params, **kwargs):
            calls.append((command_name, dict(params)))

        original = select_mod.async_send_command
        select_mod.async_send_command = _spy
        try:
            entity, _ = self._entity(_ref_commands())
            await entity.async_select_option("off")
        finally:
            select_mod.async_send_command = original

        self.assertEqual([("stopProgram", {})], calls)

    async def test_select_program_is_swap_aware(self) -> None:
        # Setting program swaps the active startProgram command; we must send the NEW one.
        from custom_components.addhon.select import HonRefProgramSelect

        appliance = types.SimpleNamespace(commands={})
        new_cmd = RecordingCommand({"program": Param("holiday")})

        class ProgramSwapParam:
            def __init__(self, values) -> None:
                self._value = None
                self.values = values

            @property
            def value(self):
                return self._value

            @value.setter
            def value(self, v) -> None:
                self._value = v
                appliance.commands["startProgram"] = new_cmd

        old_cmd = RecordingCommand({"program": ProgramSwapParam(["holiday", "super_cool"])})
        appliance.commands["startProgram"] = old_cmd
        appliance.commands["stopProgram"] = RecordingCommand(
            {"holidayMode": Param("0", values=["0"])}
        )

        coordinator = FakeCoordinator(
            {
                "ref-1": {
                    "type": "REF",
                    "name": "Fridge",
                    "appliance": appliance,
                    "attributes": {},
                    "settings": {},
                }
            }
        )
        entity = HonRefProgramSelect(coordinator, "ref-1", FakeClient())
        entity.hass = FakeHass()

        await entity.async_select_option("holiday")

        self.assertEqual("holiday", old_cmd.parameters["program"].value)
        self.assertEqual(1, new_cmd.send_calls)  # swapped command sent
        self.assertEqual(0, old_cmd.send_calls)  # stale one NOT sent

    async def test_send_failure_rolls_back_swap_and_program(self) -> None:
        # If startProgram.send() fails AFTER the category swap, async_send_program must
        # restore the pre-swap command object AND the program param value, so no unsent
        # local mutation leaks into later interactions.
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.addhon.select import HonRefProgramSelect

        appliance = types.SimpleNamespace(commands={})

        class FailingCommand(RecordingCommand):
            async def send(self) -> None:
                self.send_calls += 1
                raise RuntimeError("cloud rejected")

        new_cmd = FailingCommand({"program": Param("super_cool")})

        class ProgramSwapParam:
            def __init__(self, values) -> None:
                self._value = "ORIG"
                self.values = values

            @property
            def value(self):
                return self._value

            @value.setter
            def value(self, v) -> None:
                self._value = v
                appliance.commands["startProgram"] = new_cmd  # category swap

        swap_param = ProgramSwapParam(["super_cool", "holiday"])
        old_cmd = RecordingCommand({"program": swap_param})
        appliance.commands["startProgram"] = old_cmd
        appliance.commands["stopProgram"] = RecordingCommand(
            {"holidayMode": Param("0", values=["0"])}
        )

        coordinator = FakeCoordinator(
            {
                "ref-1": {
                    "type": "REF",
                    "name": "Fridge",
                    "appliance": appliance,
                    "attributes": {},
                    "settings": {},
                }
            }
        )
        entity = HonRefProgramSelect(coordinator, "ref-1", FakeClient())
        entity.hass = FakeHass()

        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_select_option("super_cool")
        self.assertEqual("command_error", ctx.exception.translation_key)
        self.assertEqual(1, new_cmd.send_calls)  # send was attempted
        # Rollback: the swap was undone and the staged program value reverted.
        self.assertIs(old_cmd, appliance.commands["startProgram"])
        self.assertEqual("ORIG", swap_param.value)
        self.assertEqual(0, coordinator.refreshes)  # no refresh on failed send

    async def test_invalid_option_raises(self) -> None:
        from homeassistant.exceptions import HomeAssistantError

        entity, coordinator = self._entity(_ref_commands())
        commands = coordinator.data["ref-1"]["appliance"].commands
        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_select_option("nonexistent_mode")
        self.assertEqual("program_not_found", ctx.exception.translation_key)
        self.assertEqual("nonexistent_mode", ctx.exception.translation_placeholders["program"])
        # An invalid option must not send nor refresh.
        self.assertEqual(0, commands["startProgram"].send_calls)
        self.assertEqual(0, commands["stopProgram"].send_calls)
        self.assertEqual(0, coordinator.refreshes)

    async def test_send_failure_wraps_command_error_and_skips_refresh(self) -> None:
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.addhon.select import HonRefProgramSelect

        class FailingClient:
            def run_command_sync(self, coro) -> None:
                coro.close()  # avoid "never awaited" warning
                raise RuntimeError("cloud rejected")

        coordinator = FakeCoordinator(_ref(_ref_commands()))
        entity = HonRefProgramSelect(coordinator, "ref-1", FailingClient())
        entity.hass = FakeHass()

        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_select_option("super_cool")
        self.assertEqual("command_error", ctx.exception.translation_key)
        self.assertIn("cloud rejected", ctx.exception.translation_placeholders["error"])
        # Refresh must NOT run after a failed send.
        self.assertEqual(0, coordinator.refreshes)

    async def test_unavailable_when_client_missing(self) -> None:
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.addhon.select import HonRefProgramSelect

        coordinator = FakeCoordinator(_ref(_ref_commands()))
        entity = HonRefProgramSelect(coordinator, "ref-1", client=None)
        entity.hass = FakeHass()

        # Spy both send helpers: the guard must fire at the select layer, BEFORE either
        # helper is reached (the helpers raise the same key as a backstop, so without this
        # we could not tell the select-level check is what fired).
        from custom_components.addhon import select as select_mod

        send_calls: list = []

        async def _spy_cmd(*a, **k):
            send_calls.append("cmd")

        async def _spy_prog(*a, **k):
            send_calls.append("prog")

        orig_cmd, orig_prog = select_mod.async_send_command, select_mod.async_send_program
        select_mod.async_send_command = _spy_cmd
        select_mod.async_send_program = _spy_prog
        try:
            with self.assertRaises(HomeAssistantError) as ctx:
                await entity.async_select_option("super_cool")
        finally:
            select_mod.async_send_command = orig_cmd
            select_mod.async_send_program = orig_prog
        self.assertEqual("appliance_or_client_unavailable", ctx.exception.translation_key)
        self.assertEqual([], send_calls)  # neither send helper was reached
        self.assertEqual(0, coordinator.refreshes)

    async def test_setup_log_redacts_appliance_id(self) -> None:
        # No-id-leak policy: the "Added REF program select" INFO log must redact the id
        # (here the appliance is keyed under a MAC, the exact identity that must not leak).
        from custom_components.addhon.const import DOMAIN
        from custom_components.addhon import select as select_mod

        mac = "AA:BB:CC:DD:EE:FF"
        data = _ref(_surviving_commands(), app_id=mac)
        coordinator = FakeCoordinator(data)
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}}
        )
        added: list = []
        with self.assertLogs(select_mod._LOGGER.name, level="INFO") as logs:
            await select_mod.async_setup_entry(hass, FakeEntry(), added.extend)
        blob = "\n".join(logs.output)
        self.assertEqual(1, len(added))
        self.assertTrue(any("Added REF program select" in ln for ln in logs.output))
        self.assertNotIn(mac, blob)
        self.assertNotIn("AA:BB", blob)

    async def test_current_option_off_when_flags_zero(self) -> None:
        # The read-back TRAP: startProgram.program defaults to "holiday" while the fridge is
        # idle (all flags 0). current_option must ignore it and return off.
        commands = _ref_commands()
        attributes = {
            "quickModeZ1": 0,
            "quickModeZ2": 0,
            "holidayMode": 0,
            "startProgram.program": "holiday",
            "programName": "No Program",
        }
        entity, _ = self._entity(commands, attributes)
        self.assertEqual("off", entity.current_option)

    async def test_current_option_reads_active_flag(self) -> None:
        cases = {
            "quickModeZ1": "super_cool",
            "quickModeZ2": "super_freeze",
            "holidayMode": "holiday",
        }
        for flag, expected in cases.items():
            entity, _ = self._entity(_ref_commands(), {flag: "1"})
            self.assertEqual(expected, entity.current_option, f"{flag} -> {expected}")

    async def test_current_option_gated_by_live_enum(self) -> None:
        # intelligenceMode=1 but this model's enum has no auto_set -> not reported -> off.
        entity, _ = self._entity(_ref_commands(), {"intelligenceMode": "1"})
        self.assertEqual("off", entity.current_option)

    async def test_current_option_auto_set_when_in_enum(self) -> None:
        commands = _ref_commands(programs=["holiday", "auto_set", "super_cool"])
        entity, _ = self._entity(commands, {"intelligenceMode": "1"})
        self.assertEqual("auto_set", entity.current_option)

    async def test_select_iot_preset_sends_program(self) -> None:
        commands = _ref_commands()
        entity, _ = self._entity(commands)
        await entity.async_select_option("iot_extra_cold")
        self.assertEqual(
            "iot_extra_cold", commands["startProgram"].parameters["program"].value
        )

    async def test_current_option_reads_iot_preset_from_programname(self) -> None:
        # iot_* presets set NO mode flag, but the cloud persists the active program in
        # programName (often as an i18n key); current_option reflects it from that real
        # feedback, both as a bare code and as a dotted i18n key.
        for raw in ("iot_extra_cold", "PROGRAMS.REF.IOT_EXTRA_COLD"):
            entity, _ = self._entity(_ref_commands(), {"programName": raw})
            self.assertEqual("iot_extra_cold", entity.current_option, raw)

    async def test_current_option_programname_via_prstr_and_case(self) -> None:
        # prStr is an accepted source too; matching is case-insensitive.
        entity, _ = self._entity(_ref_commands(), {"prStr": "Super_Cool"})
        self.assertEqual("super_cool", entity.current_option)

    async def test_current_option_off_without_feedback(self) -> None:
        # No flag and an idle programName -> off (nothing active).
        entity, _ = self._entity(_ref_commands(), {"programName": "No Program"})
        self.assertEqual("off", entity.current_option)

    async def test_current_option_numeric_prcode_is_harmless(self) -> None:
        # prCode is consulted but is an int needing a device map we do not have; a numeric
        # value must never false-match a snake_case code -> off (safe, not a wrong program).
        for raw in (3, "5", 0):
            entity, _ = self._entity(_ref_commands(), {"prCode": raw})
            self.assertEqual("off", entity.current_option, raw)

    async def test_current_option_programname_no_fuzzy_match(self) -> None:
        # A program name that is not EXACTLY an offered code must not be force-matched.
        entity, _ = self._entity(_ref_commands(), {"programName": "extra_cold"})
        self.assertEqual("off", entity.current_option)

    async def test_flag_wins_over_programname(self) -> None:
        # A live flag takes precedence (boost modes are the most reliable signal).
        entity, _ = self._entity(
            _ref_commands(),
            {"quickModeZ1": "1", "programName": "PROGRAMS.REF.IOT_DAILY_USE"},
        )
        self.assertEqual("super_cool", entity.current_option)

    async def test_modez_synthetic_field_not_surfaced(self) -> None:
        # modeZ1/modeZ2 are ENGINE-SYNTHETIC (client/engine/appliances/ref.py rewrites
        # them from the boost flags), so they can never carry an iot_* code on the real
        # engine and the select deliberately does NOT read them. Even if a modeZ field
        # somehow held a real offered code, it must not be surfaced. Guards against
        # re-adding modeZ1/modeZ2 to the active-program matcher.
        for attr in ("modeZ1", "modeZ2"):
            entity, _ = self._entity(_ref_commands(), {attr: "iot_extra_cold"})
            self.assertEqual("off", entity.current_option, attr)

    async def test_current_option_modez_no_mode_is_off(self) -> None:
        entity, _ = self._entity(
            _ref_commands(), {"modeZ1": "no_mode", "modeZ2": "no_mode"}
        )
        self.assertEqual("off", entity.current_option)
        self.assertNotIn("no_mode", entity._attr_options)

    async def test_current_option_modez_empty_is_off(self) -> None:
        entity, _ = self._entity(_ref_commands(), {"modeZ1": "", "modeZ2": ""})
        self.assertEqual("off", entity.current_option)

    async def test_current_option_modez_gated_by_live_enum(self) -> None:
        entity, _ = self._entity(_ref_commands(), {"modeZ1": "auto_set"})
        self.assertEqual("off", entity.current_option)

    async def test_flag_wins_over_modez(self) -> None:
        entity, _ = self._entity(
            _ref_commands(), {"quickModeZ1": "1", "modeZ1": "iot_daily_use"}
        )
        self.assertEqual("super_cool", entity.current_option)

    async def test_current_option_real_super_cool_dump(self) -> None:
        entity, _ = self._entity(
            _ref_commands(),
            {
                "quickModeZ1": "1",
                "modeZ1": "super_cool",
                "modeZ2": "no_mode",
                "programName": "No Program",
            },
        )
        self.assertEqual("super_cool", entity.current_option)

    async def test_iot_preset_not_surfaced_by_current_engine(self) -> None:
        # CAVEAT (load-bearing): current engine derives modeZ from flags only, so an active
        # iot_* preset (no flag) yields modeZ1=modeZ2="no_mode"; reading modeZ does NOT by
        # itself close the iot_* gap. This pins that reality.
        entity, _ = self._entity(
            _ref_commands(),
            {
                "quickModeZ1": "0",
                "quickModeZ2": "0",
                "holidayMode": "0",
                "intelligenceMode": "0",
                "modeZ1": "no_mode",
                "modeZ2": "no_mode",
                "programName": "No Program",
            },
        )
        self.assertEqual("off", entity.current_option)


class RefProgramStateTranslationTest(unittest.TestCase):
    """The ref_program state map must label every code the select can show: the read-back
    codes (what current_option returns) AND a real model's full program enum. A missing
    label only degrades to a raw key in the UI (no crash), but this guards intent."""

    def _state_keys(self, lang: str) -> set[str]:
        import json

        path = REPO_ROOT / "custom_components" / "addhon" / "translations" / f"{lang}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data["entity"]["select"]["ref_program"]["state"].keys())

    def test_state_covers_readback_and_real_enum(self) -> None:
        from custom_components.addhon.select import _REF_MODE_FLAG_TO_PROGRAM, REF_PROGRAM_OFF

        required = {REF_PROGRAM_OFF, *_REF_MODE_FLAG_TO_PROGRAM.values(), *ROB_PROGRAMS}
        for lang in ("en", "it"):
            keys = self._state_keys(lang)
            missing = required - keys
            self.assertFalse(missing, f"[{lang}] ref_program.state missing labels: {sorted(missing)}")

    def test_state_keys_identical_en_it(self) -> None:
        self.assertEqual(self._state_keys("en"), self._state_keys("it"))


class RefMyZoneSelectTest(unittest.IsolatedAsyncioTestCase):
    """The vtRoom1 drawer as a writable control (#93)."""

    async def _setup(self, data) -> list:
        from custom_components.addhon.const import DOMAIN
        from custom_components.addhon import select

        coordinator = FakeCoordinator(data)
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}}
        )
        added: list = []
        await select.async_setup_entry(hass, FakeEntry(), added.extend)
        return added

    async def _entity(self, attributes=None, **kwargs):
        added = await self._setup(
            _fridge(_rmxs_commands(), attributes or {}, **kwargs)
        )
        return next(
            (e for e in added if e._attr_unique_id == "ref-1_my_zone_mode"), None
        )

    async def test_options_are_the_three_drawer_programs_coldest_first(self) -> None:
        """Read off the catalogue, not off a list of slugs in this repository.

        The order is the PINNED VALUE's: the register is a temperature scale at firmware
        level (Holiday pins it to 17), so 0/2/5 is the drawer's own coldest-to-warmest
        order and the order the app's mode card shows.
        """
        entity = await self._entity()
        self.assertEqual(
            ["zero_fresh", "quick_cool", "fruit_and_veg"], entity._attr_options
        )

    async def test_my_zone_offers_no_off(self) -> None:
        """`stopProgram` declares the four flags and never touches `tempSelZ3`, so the
        drawer has no "no mode" and nothing could send one. An option is something the
        user can SELECT, and no command would carry this one."""
        entity = await self._entity()
        for absent in ("off", "none", "unknown"):
            self.assertNotIn(absent, entity._attr_options, absent)

    async def test_a_download_preset_zoned_on_the_drawer_is_not_a_drawer_mode(
        self,
    ) -> None:
        """`IOT_SPECIAL_FOOD_CORE` pins tempSelZ3=5 AND declares zone=[vtRoom1] and
        nothing else. It passes the pin test and the zone test and is still a QuickSet
        preset. This is the single counterexample the programFamily condition exists
        for -- delete that condition and this test is what fails."""
        entity = await self._entity()
        self.assertNotIn("iot_special_food_core", entity._attr_options)
        for code in ("iot_daily_use", "iot_extra_cold", "iot_high_efficiency"):
            self.assertNotIn(code, entity._attr_options, code)

    async def test_holiday_is_not_a_drawer_mode(self) -> None:
        """Its zone names the drawer and its rules force tempSelZ3 to 17, but it declares
        no tempSelZ3 parameter of its own -- and the rules engine only mutates a
        parameter the command already declares."""
        entity = await self._entity()
        self.assertNotIn("holiday", entity._attr_options)

    async def test_current_option_reads_the_live_register(self) -> None:
        for value, expected in (
            ("0", "zero_fresh"), (0, "zero_fresh"),
            ("2", "quick_cool"), ("5", "fruit_and_veg"),
        ):
            entity = await self._entity({"tempSelZ3": value})
            self.assertEqual(expected, entity.current_option, repr(value))

    async def test_the_reporters_own_dump_reads_zero_fresh(self) -> None:
        """His shadow: tempSelZ3 = 0, untouched since 2024, with every flag clear except
        intelligenceMode. Zero degrees is a MODE, and reading it as a target temperature
        is what issue #93 asked us to do and we declined."""
        entity = await self._entity({
            "tempSelZ3": 0, "intelligenceMode": 1, "quickModeZ1": 0,
            "quickModeZ2": 0, "holidayMode": 0, "programName": "No Program",
        })
        self.assertEqual("zero_fresh", entity.current_option)

    async def test_current_option_is_unknown_while_holiday_pins_seventeen(self) -> None:
        """17 is HOLIDAY's `programRules` value, outside the drawer's own vocabulary.
        While Holiday runs the drawer is in none of its modes: the app shows
        NO_MODE_SELECTED and `sensor.my_zone_mode` shows unknown, and so does this."""
        for value in ("17", 17, "1", "", "-5", None):
            entity = await self._entity({"tempSelZ3": value})
            self.assertIsNone(entity.current_option, repr(value))

    async def test_current_option_never_leaves_its_frozen_options(self) -> None:
        """The catalogue is read at every refresh while options are frozen at
        construction, so a category the cloud adds between two polls must fall through
        to unknown rather than push the entity into a state it never offered."""
        entity = await self._entity({"tempSelZ3": "0"})
        entity._attr_options = ["quick_cool"]
        self.assertIsNone(entity.current_option)

    async def test_select_sends_startprogram_for_the_chosen_mode(self) -> None:
        from custom_components.addhon.const import DOMAIN
        from custom_components.addhon import select

        commands = _rmxs_commands()
        data = _fridge(commands, {"tempSelZ3": "0"})
        coordinator = FakeCoordinator(data)
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}}
        )
        added: list = []
        await select.async_setup_entry(hass, FakeEntry(), added.extend)
        entity = next(e for e in added if e._attr_unique_id == "ref-1_my_zone_mode")
        entity.hass = hass

        await entity.async_select_option("fruit_and_veg")

        self.assertEqual(
            "fruit_and_veg", commands["startProgram"].parameters["program"].value
        )
        self.assertEqual(1, commands["startProgram"].send_calls)
        # stopProgram is never touched: the drawer has no off, and clearing the four
        # flags is not what "change the drawer's mode" means.
        self.assertEqual(0, commands["stopProgram"].send_calls)
        self.assertEqual(1, coordinator.refreshes)

    async def test_an_unoffered_mode_raises_and_sends_nothing(self) -> None:
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.addhon import select

        commands = _rmxs_commands()
        coordinator = FakeCoordinator(_fridge(commands))
        entity = select.HonRefMyZoneSelect(
            coordinator, "ref-1", ["zero_fresh", "quick_cool"], FakeClient()
        )
        entity.hass = FakeHass()
        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_select_option("iot_daily_use")
        self.assertEqual("program_not_found", ctx.exception.translation_key)
        self.assertEqual(0, commands["startProgram"].send_calls)
        self.assertEqual(0, coordinator.refreshes)

    async def test_the_model_must_declare_the_drawer(self) -> None:
        """Both gates, not either. `zones` is the criterion the app filters its fridge
        zone cards on, and it is model metadata rather than telemetry -- which is what
        lets it answer for a register that has not moved in two years."""
        for zones in ("fridge|freezer", None, ""):
            entity = await self._entity(zones=zones)
            self.assertIsNone(entity, repr(zones))

    async def test_no_select_when_the_catalogue_carries_no_drawer_program(self) -> None:
        """damigioanna's HDPW5620CNPK: `zones` DOES declare vtRoom1, five download
        presets DO pin tempSelZ3, and there is no drawer mode among them. It writes its
        drawer through a real `setParameters.tempSelZ3` range and already has
        `number.target_temp_zone3`; the two controls exclude each other from the data,
        with no rule anywhere naming the other entity."""
        drawer = ("zero_fresh", "quick_cool", "fruit_and_veg")
        catalogue = {
            code: category
            for code, category in _rmxs_categories().items()
            if code not in drawer
        }
        added = await self._setup(
            _fridge(
                _rmxs_commands(
                    categories=catalogue,
                    programs=[c for c in RMXS_PROGRAMS if c not in drawer],
                ),
                {"tempSelZ3": "5"},
            )
        )
        self.assertNotIn("ref-1_my_zone_mode", {e._attr_unique_id for e in added})

    async def test_a_drawer_program_absent_from_the_live_enum_is_not_offered(
        self,
    ) -> None:
        """The catalogue can carry more than the enum offers; the enum is the gate."""
        added = await self._setup(
            _fridge(
                _rmxs_commands(
                    programs=[c for c in RMXS_PROGRAMS if c != "fruit_and_veg"]
                )
            )
        )
        entity = next(e for e in added if e._attr_unique_id == "ref-1_my_zone_mode")
        self.assertEqual(["zero_fresh", "quick_cool"], entity._attr_options)

    async def test_reaches_fr_and_fre_too(self) -> None:
        for app_type in ("FR", "FRE"):
            added = await self._setup(_fridge(_rmxs_commands(), app_type=app_type))
            self.assertIn(
                "ref-1_my_zone_mode",
                {e._attr_unique_id for e in added},
                app_type,
            )


class RefProgramSelectSupersessionTest(unittest.IsolatedAsyncioTestCase):
    """`select.ref_program` survives only where nothing better exists (#93)."""

    async def _setup(self, data) -> list:
        from custom_components.addhon.const import DOMAIN
        from custom_components.addhon import select

        coordinator = FakeCoordinator(data)
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}}
        )
        added: list = []
        await select.async_setup_entry(hass, FakeEntry(), added.extend)
        return added

    async def test_the_reporters_fridge_loses_the_single_select(self) -> None:
        """HFW7720EWMP: four flag modes with all four stopProgram parameters, and three
        drawer modes. Both replacements really appear on this same device, so keeping
        the single select would leave two controls writing the same registers with
        opposite meanings of `off`."""
        added = await self._setup(_fridge(_rmxs_commands()))
        keys = {e._attr_translation_key for e in added}
        self.assertNotIn("ref_program", keys)
        self.assertIn("my_zone_mode", keys)

    async def test_a_fridge_with_only_download_presets_keeps_it(self) -> None:
        """The one shape where nothing better can be built. The presets become buttons
        with no state and no stop, so this select stays the appliance's only stop
        control and its only reading -- and the buttons coexist with it by design, the
        way the AC direction select coexists with the climate swing mode."""
        presets = [c for c in RMXS_PROGRAMS if c.startswith("iot_")]
        catalogue = {
            code: category
            for code, category in _rmxs_categories().items()
            if code in presets
        }
        added = await self._setup(
            _fridge(
                _rmxs_commands(categories=catalogue, programs=presets),
                zones="fridge|freezer",
            )
        )
        self.assertEqual(["ref_program"], [e._attr_translation_key for e in added])

    async def test_one_flag_alone_is_enough_to_supersede_it(self) -> None:
        """The predicate is "a replacement really appears on this device", not "all of
        them do": one switch that can be turned off on its own already makes the global
        `off` the wrong control."""
        catalogue = {"super_cool": _rmxs_categories()["super_cool"]}
        commands = _rmxs_commands(categories=catalogue, programs=["super_cool"])
        commands["stopProgram"] = RecordingCommand(
            {"quickModeZ1": Param("0", values=["0"])}
        )
        added = await self._setup(_fridge(commands, zones="fridge|freezer"))
        self.assertEqual([], added)

    async def test_a_flag_the_device_cannot_clear_does_not_supersede_it(self) -> None:
        """An ON with no OFF is not a switch. `stopProgram` here declares nothing this
        enum can turn off, so no switch is buildable and the select stays."""
        catalogue = {"super_cool": _rmxs_categories()["super_cool"]}
        commands = _rmxs_commands(categories=catalogue, programs=["super_cool"])
        commands["stopProgram"] = RecordingCommand(
            {"onOffStatus": Param("0", values=["0"])}
        )
        added = await self._setup(_fridge(commands, zones="fridge|freezer"))
        self.assertEqual(["ref_program"], [e._attr_translation_key for e in added])

    async def test_no_stopprogram_still_means_no_select_at_all(self) -> None:
        added = await self._setup(
            _fridge(_rmxs_commands(stop=False), zones="fridge|freezer")
        )
        self.assertEqual([], added)


class RefPresetButtonTest(unittest.IsolatedAsyncioTestCase):
    """The `iot_*` download presets as fire-and-forget buttons (#93)."""

    async def _setup(self, data) -> list:
        from custom_components.addhon.const import DOMAIN
        from custom_components.addhon import button

        coordinator = FakeCoordinator(data)
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}}
        )
        added: list = []
        await button.async_setup_entry(hass, FakeEntry(), added.extend)
        for entity in added:
            entity.hass = hass
        return [e for e in added if not getattr(e, "_addhon_account", False)]

    async def test_one_button_per_offered_preset(self) -> None:
        added = await self._setup(_fridge(_rmxs_commands()))
        self.assertEqual(
            [
                "ref-1_ref_preset_iot_daily_use",
                "ref-1_ref_preset_iot_extra_cold",
                "ref-1_ref_preset_iot_extra_ice",
                "ref-1_ref_preset_iot_high_efficiency",
                "ref-1_ref_preset_iot_special_food_core",
            ],
            [e._attr_unique_id for e in added],
        )

    async def test_a_preset_the_model_does_not_offer_gets_no_button(self) -> None:
        """`iot_extra_cold_water` is in this repository's vocabulary and not in his
        enum. The gate is the device, the tuple is only what we can name."""
        added = await self._setup(_fridge(_rmxs_commands()))
        self.assertNotIn(
            "ref-1_ref_preset_iot_extra_cold_water",
            {e._attr_unique_id for e in added},
        )

    async def test_dashboard_programs_never_become_buttons(self) -> None:
        """The four flag modes and the three drawer modes have their own controls; a
        second fire-and-forget copy of them would be a control with no off."""
        added = await self._setup(_fridge(_rmxs_commands()))
        blob = " ".join(e._attr_unique_id for e in added)
        for code in ("auto_set", "super_cool", "super_freeze", "holiday",
                     "zero_fresh", "quick_cool", "fruit_and_veg"):
            self.assertNotIn(code, blob, code)

    async def test_a_catalogue_less_fridge_gets_no_buttons(self) -> None:
        """No categories, no `programFamily`, no way to tell a preset from a mode. The
        enum alone is not evidence: `RecordingCommand` is the shape of every fridge whose
        catalogue we cannot read."""
        added = await self._setup(_ref(_ref_commands()))
        self.assertEqual([], added)

    async def test_press_sends_the_preset_and_refreshes(self) -> None:
        from custom_components.addhon import button

        commands = _rmxs_commands()
        coordinator = FakeCoordinator(_fridge(commands))
        entity = button.HonRefPresetButton(
            coordinator, "ref-1", "iot_extra_cold", FakeClient()
        )
        entity.hass = FakeHass()

        await entity.async_press()

        self.assertEqual(
            "iot_extra_cold", commands["startProgram"].parameters["program"].value
        )
        self.assertEqual(1, commands["startProgram"].send_calls)
        self.assertEqual(0, commands["stopProgram"].send_calls)
        self.assertEqual(1, coordinator.refreshes)

    async def test_press_failure_wraps_command_error_and_skips_refresh(self) -> None:
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.addhon import button

        class FailingClient:
            def run_command_sync(self, coro) -> None:
                coro.close()
                raise RuntimeError("cloud rejected")

        coordinator = FakeCoordinator(_fridge(_rmxs_commands()))
        entity = button.HonRefPresetButton(
            coordinator, "ref-1", "iot_daily_use", FailingClient()
        )
        entity.hass = FakeHass()

        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_press()
        self.assertEqual("command_error", ctx.exception.translation_key)
        self.assertIn("cloud rejected", ctx.exception.translation_placeholders["error"])
        self.assertEqual(0, coordinator.refreshes)

    async def test_presets_are_main_controls_not_configuration(self) -> None:
        """They change what the appliance does to the food, which is its primary
        function -- not its configuration and not a diagnostic. The app puts its QuickSet
        cards on the REF dashboard for the same reason."""
        added = await self._setup(_fridge(_rmxs_commands()))
        for entity in added:
            self.assertIsNone(
                getattr(entity, "_attr_entity_category", None), entity._attr_unique_id
            )

    async def test_the_unique_id_vocabulary_stays_closed(self) -> None:
        """`diagnostics._entity_section` rests the privacy of the whole `sources` map on
        every unique_id suffix being a constant of this repository. A favourite is filed
        under the name the USER typed, so a code taken straight from the catalogue could
        be a nickname; this pins that the suffix can only ever come from the tuple."""
        from custom_components.addhon.ref_programs import REF_DOWNLOAD_PRESETS

        added = await self._setup(_fridge(_rmxs_commands()))
        for entity in added:
            suffix = entity._attr_unique_id.removeprefix("ref-1_")
            self.assertIn(
                suffix.removeprefix("ref_preset_"), REF_DOWNLOAD_PRESETS, suffix
            )


class RefProgramClassificationTest(unittest.IsolatedAsyncioTestCase):
    """The three sorters in `ref_programs`, attacked one condition at a time.

    Every test here was written because a mutation of the production code left the whole
    suite green: the conditions were argued for in prose and measured by nobody.
    """

    async def _setup(self, data) -> list:
        from custom_components.addhon.const import DOMAIN
        from custom_components.addhon import select

        coordinator = FakeCoordinator(data)
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}}
        )
        added: list = []
        await select.async_setup_entry(hass, FakeEntry(), added.extend)
        return added

    async def _buttons(self, data) -> list:
        from custom_components.addhon.const import DOMAIN
        from custom_components.addhon import button

        coordinator = FakeCoordinator(data)
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}}
        )
        added: list = []
        await button.async_setup_entry(hass, FakeEntry(), added.extend)
        return [e for e in added if not getattr(e, "_addhon_account", False)]

    async def test_a_favourite_named_download_never_becomes_a_button(self) -> None:
        """The closed unique_id vocabulary, proven rather than asserted vacuously.

        `command_loader._add_favourites` files a favourite under `favouriteName` -- the
        string the USER typed -- into `startProgram`'s categories, inheriting the base
        command's `programFamily`. So a `download`-family category can genuinely carry a
        nickname, and `diagnostics._entity_section` rests the privacy of its whole
        `sources` map on the opposite: "every unique_id suffix is a constant written in
        this repository". Until this fixture carried one, dropping the intersection with
        `REF_DOWNLOAD_PRESETS` left the suite green.
        """
        catalogue = _rmxs_categories()
        catalogue["frigo di anna (casa al mare)"] = _download(
            ["fridge", "freezer"], {"tempSelZ1": FixedParam("4")}
        )
        added = await self._buttons(
            _fridge(
                _rmxs_commands(
                    categories=catalogue,
                    programs=[*RMXS_PROGRAMS, "frigo di anna (casa al mare)"],
                )
            )
        )
        suffixes = [e._attr_unique_id for e in added]
        self.assertNotIn("ref-1_ref_preset_frigo di anna (casa al mare)", suffixes)
        self.assertIn("ref-1_ref_preset_iot_daily_use", suffixes)

    def _with_favourites(self):
        """The reporter's catalogue plus two favourites and one real new preset."""
        catalogue = _rmxs_categories()
        catalogue["frigo di anna (casa al mare)"] = _favourite(
            catalogue["iot_daily_use"]
        )
        # The one a name-shape heuristic would have missed: lowercase, underscored,
        # `iot_`-prefixed -- and still a string the user typed.
        catalogue["iot_mio_preferito"] = _favourite(catalogue["iot_extra_cold"])
        catalogue["cassetto di casa"] = _favourite(catalogue["zero_fresh"])
        catalogue["iot_future_preset"] = _download(
            ["freezer"], {"tempSelZ2": FixedParam("-24")}
        )
        codes = [
            *RMXS_PROGRAMS,
            "frigo di anna (casa al mare)",
            "iot_mio_preferito",
            "cassetto di casa",
            "iot_future_preset",
        ]
        return _fridge(_rmxs_commands(categories=catalogue, programs=codes))

    async def test_a_saved_favourite_is_never_a_catalogue_program(self) -> None:
        """A favourite carries the user's own words, and must reach nothing (CWE-532).

        `command_loader._add_favourites` copies a catalogue category, inherits its
        `programFamily`, its `zone` and its pinned parameters, and files it under
        `favouriteName`. So it passes every classifier on shape alone and would become a
        select option, a `unique_id` and a log line carrying that text.

        The engine's own marker is what excludes it -- the `favourite` parameter it adds
        to the copy -- and not the shape of the name: `iot_mio_preferito` below is
        lowercase, underscored and `iot_`-prefixed, and a slug heuristic waves it through.
        """
        from custom_components.addhon import ref_programs

        appliance = self._with_favourites()["ref-1"]["appliance"]
        codes = ref_programs.program_categories(appliance)
        for typed in ("frigo di anna (casa al mare)", "iot_mio_preferito",
                      "cassetto di casa"):
            self.assertNotIn(typed, codes, typed)
        # ...while the catalogue itself is untouched.
        self.assertIn("zero_fresh", codes)
        self.assertIn("iot_daily_use", codes)

    async def test_no_favourite_reaches_an_entity_or_a_log(self) -> None:
        from custom_components.addhon import ref_programs

        data = self._with_favourites()
        appliance = data["ref-1"]["appliance"]
        typed = ("frigo di anna (casa al mare)", "iot_mio_preferito", "cassetto di casa")

        with self.assertLogs(ref_programs._LOGGER.name, level="DEBUG") as logs:
            ref_programs.download_codes(appliance)
        blob = "\n".join(logs.output)
        for name in typed:
            self.assertNotIn(name, blob, name)
        # The genuinely new catalogue preset IS named: that is the point of the line,
        # and the fix for it is one tuple entry plus two labels.
        self.assertIn("iot_future_preset", blob)

        buttons = await self._buttons(data)
        selects = await self._setup(data)
        surface = " ".join(
            [e._attr_unique_id for e in buttons]
            + [str(getattr(e, "_attr_options", "")) for e in selects]
        )
        for name in typed:
            self.assertNotIn(name, surface, name)

    async def test_a_named_preset_without_the_download_family_gets_no_button(
        self,
    ) -> None:
        """The family is REQUIRED for a button, not merely consulted. A category we can
        name but whose schema calls it a dashboard mode is not a QuickSet preset, and a
        fire-and-forget send is the one shape that cannot be corrected by its own state."""
        catalogue = _rmxs_categories()
        catalogue["iot_daily_use"] = _dashboard(
            ["fridge", "freezer"], {"tempSelZ1": FixedParam("4")}
        )
        added = await self._buttons(_fridge(_rmxs_commands(categories=catalogue)))
        self.assertNotIn(
            "ref-1_ref_preset_iot_daily_use", {e._attr_unique_id for e in added}
        )
        self.assertIn(
            "ref-1_ref_preset_iot_extra_cold", {e._attr_unique_id for e in added}
        )

    async def test_a_multi_zone_dashboard_program_is_not_a_drawer_mode(self) -> None:
        """The `zone` condition, finally measured.

        Nothing in the other fixtures exercises it: HOLIDAY is excluded by the pin test
        and the download presets by the family test, so the condition could be deleted
        with the suite still green. A dashboard-family program that pins `tempSelZ3` AND
        moves the fridge and the freezer is not the drawer being in a mode -- it is a
        whole-appliance program that happens to write the drawer on its way past.
        """
        catalogue = _rmxs_categories()
        catalogue["iot_daily_use"] = _dashboard(
            ["fridge", "freezer", "vtroom1"], {"tempSelZ3": FixedParam("5")}
        )
        added = await self._setup(_fridge(_rmxs_commands(categories=catalogue)))
        entity = next(e for e in added if e._attr_unique_id == "ref-1_my_zone_mode")
        self.assertNotIn("iot_daily_use", entity._attr_options)
        self.assertEqual(
            ["zero_fresh", "quick_cool", "fruit_and_veg"], entity._attr_options
        )

    async def test_a_download_preset_never_explains_the_drawers_value(self) -> None:
        """The read-side family filter, on the catalogue ORDER where it bites.

        `program_code_for_fixed_value` answers with the FIRST category that pins the
        number. Put a download preset pinning 2 ahead of `quick_cool` -- which is the
        order `apk/dump/ref_10136/commands.json` really has -- and without the filter the
        drawer is explained by a whole-appliance preset, the select's frozen options
        reject it, and the state falls to unknown on a drawer that IS in a mode.
        """
        ordered = {}
        ordered["iot_extra_cold"] = _rmxs_categories()["iot_extra_cold"]
        for code, category in _rmxs_categories().items():
            if code != "iot_extra_cold":
                ordered[code] = category
        added = await self._setup(
            _fridge(_rmxs_commands(categories=ordered), {"tempSelZ3": "2"})
        )
        entity = next(e for e in added if e._attr_unique_id == "ref-1_my_zone_mode")
        self.assertEqual("quick_cool", entity.current_option)

    async def test_the_drawer_alone_supersedes_the_single_select(self) -> None:
        """`has_replacement_controls`' drawer half, which nothing measured.

        Drop `or bool(my_zone_codes(...))` and every supersession fixture still passed,
        because they all have clearable flags. A fridge whose `stopProgram` can clear
        nothing but whose catalogue carries the drawer programs would then keep
        `select.ref_program` AND get `select.my_zone_mode`: two controls owning
        `tempSelZ3`, which is the conflict the predicate exists to prevent.
        """
        commands = _rmxs_commands()
        commands["stopProgram"] = RecordingCommand(
            {"onOffStatus": Param("0", values=["0"])}
        )
        added = await self._setup(_fridge(commands))
        keys = {e._attr_translation_key for e in added}
        self.assertEqual({"my_zone_mode"}, keys)

    async def test_a_model_that_declares_no_drawer_keeps_its_single_select(self) -> None:
        """THE DEFECT THE REFUTERS FOUND, pinned so it cannot come back.

        The model-zone test used to live in `select.async_setup_entry` while
        `has_replacement_controls` -- which decides whether `select.ref_program` steps
        aside -- never saw it. On the very catalogue this design was derived from
        (`decomp.txt:3495902`: `vtZone` declared, `zones` absent) the two disagreed: the
        predicate said a replacement existed, the select stepped aside, and the drawer
        select was never built. The appliance ended up unable to reach `zero_fresh`,
        `quick_cool` or `fruit_and_veg` at all.
        """
        commands = _rmxs_commands()
        commands["stopProgram"] = RecordingCommand(
            {"onOffStatus": Param("0", values=["0"])}
        )
        added = await self._setup(_fridge(commands, zones=None))
        # No drawer select -- the model does not declare the compartment...
        self.assertNotIn("my_zone_mode", {e._attr_translation_key for e in added})
        # ...and therefore no supersession either: the appliance keeps a control.
        self.assertEqual(
            ["ref_program"], [e._attr_translation_key for e in added]
        )


if __name__ == "__main__":
    unittest.main()
