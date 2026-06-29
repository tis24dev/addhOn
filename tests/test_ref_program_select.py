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


def _ref_commands(programs=None, with_stop=True) -> dict:
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
            {
                "quickModeZ1": Param("0", values=["0"]),
                "quickModeZ2": Param("0", values=["0"]),
                "holidayMode": Param("0", values=["0"]),
            }
        )
    return commands


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

    async def test_created_for_ref_with_program_and_stopprogram(self) -> None:
        added = await self._setup(_ref(_ref_commands()))
        self.assertEqual(1, len(added))
        self.assertEqual("ref_program", added[0]._attr_translation_key)

    async def test_options_are_off_first_plus_live_enum(self) -> None:
        # off is always the first option; the rest is exactly the live enum SET. (Runtime
        # HonParameterProgram.values sorts, so we assert the set, not the input order.)
        added = await self._setup(_ref(_ref_commands()))
        options = added[0]._attr_options
        self.assertEqual("off", options[0])
        self.assertEqual(set(ROB_PROGRAMS), set(options[1:]))

    async def test_options_follow_a_different_live_enum(self) -> None:
        # Built from the live enum, NOT hard-coded: a different model -> different options.
        added = await self._setup(_ref(_ref_commands(programs=["super_cool", "auto_set"])))
        options = added[0]._attr_options
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
            data = _ref(_ref_commands())
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
        data = _ref(_ref_commands(), app_id=mac)
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


if __name__ == "__main__":
    unittest.main()
