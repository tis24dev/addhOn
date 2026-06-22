"""Tests for the AC write-path: climate setters, AC switches, and the shared
``ac_command`` sender.

This is the path that sends real commands to a physical air conditioner. Before
these tests it had NO coverage on the *send* (only translation-key and
parameter-name checks existed), so a mutant inverting ON/OFF, swapping a
mode/fan map key, truncating the setpoint, or flipping swing on/off would have
survived the whole suite. See diagnostics/addhon-deep-audit-2026-06-22.md #2.

Assertions are made on ``RecordingCommand.sent`` -- the payload frozen at the
moment ``send()`` is called, i.e. exactly what reaches the device -- not on the
parameter objects' residual internal state. Expected codes are hard-coded golden
values (from the real AS35PBPHRA-PRE dump in diagnostics/live-2026-06-22/), NOT
imported from const.py: importing the maps would let a map mutation pass
unnoticed because test and code would share the bug.

The Home Assistant stubs and the Fake* harness are reused from
``test_program_select`` (importing it installs the stubs at module load), with a
local ``RecordingCommand`` that also captures the sent payload.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Importing test_program_select runs _install_homeassistant_stubs() at module
# load, so the homeassistant.* modules exist before we import climate/switch.
from test_program_select import (  # noqa: E402
    FakeClient,
    FakeCoordinator,
    FakeHass,
    Param,
    _ac,
)

from homeassistant.components.climate.const import HVACMode  # noqa: E402
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

# test_program_select's CoordinatorEntity stub omits `available`, but
# HonBaseEntity.available calls super().available. The base class is bound when
# base_entity is first imported, so we must install a CoordinatorEntity that
# exposes `available` (like the real one: coordinator.last_update_success) BEFORE
# importing the addhon entities below -- otherwise this module, collected first,
# would bind HonBaseEntity to a base without `available` and break
# test_entity_availability. Mirrors the force-assign in test_entity_availability.
import homeassistant.helpers.update_coordinator as _uc  # noqa: E402


class _CoordinatorEntity:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self.hass = getattr(coordinator, "hass", None)

    def async_write_ha_state(self) -> None:
        self.state_writes = getattr(self, "state_writes", 0) + 1

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success


_uc.CoordinatorEntity = _CoordinatorEntity

from custom_components.addhon import ac_command, climate, switch  # noqa: E402
from custom_components.addhon.const import (  # noqa: E402
    AC_FAN_MAP_REVERSE,
    AC_MODE_MAP_REVERSE,
)

# The shared stub's ClimateEntityFeature lacks SWING_MODE (the existing climate
# test never builds a swing-capable AC), but capability-gated swing needs it.
# climate reads ClimateEntityFeature as a module global at construction time, so
# rebinding it here is enough; the flag values are irrelevant to these tests.
import enum as _enum  # noqa: E402


class _ClimateFeature(_enum.IntFlag):
    TARGET_TEMPERATURE = 1
    FAN_MODE = 2
    TURN_ON = 4
    TURN_OFF = 8
    SWING_MODE = 16


climate.ClimateEntityFeature = _ClimateFeature


class RecordingCommand:
    """Captures the payload at send() time (kept in sync with the harness in
    test_number_setpoints.RecordingCommand, plus a `.sent` snapshot)."""

    def __init__(self, parameters=None) -> None:
        self.parameters = parameters or {}
        self.send_calls = 0
        self.sent = None

    async def send(self) -> None:
        self.send_calls += 1
        self.sent = {k: p.value for k, p in self.parameters.items()}


class FailingCommand(RecordingCommand):
    """Records the send attempt, then fails -- to exercise the rollback path."""

    async def send(self) -> None:
        self.send_calls += 1
        raise RuntimeError("boom")


class DomainErrorCommand(RecordingCommand):
    """send() raises a HomeAssistantError with a specific translation_key, to
    check the `except HomeAssistantError: raise` re-raise (must NOT be rewrapped
    into a generic command_error)."""

    def __init__(self, parameters=None, key="boom_key") -> None:
        super().__init__(parameters)
        self._key = key

    async def send(self) -> None:
        self.send_calls += 1
        raise HomeAssistantError(translation_domain="addhon", translation_key=self._key)


class RangeParam:
    """Range parameter stub exposing min/max/step (Param has only value/values),
    so the climate entity can read the device's real setpoint range."""

    def __init__(self, value, *, mn, mx, step) -> None:
        self.value = value
        self.min = mn
        self.max = mx
        self.step = step


class RuleParam:
    """Parameter whose value setter ALSO mutates `.values` (like the real rules
    mutate siblings). Used to prove the rollback restores the full __dict__
    (values too), not just `.value`."""

    def __init__(self, value, values) -> None:
        self._value = value
        self.values = list(values)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v) -> None:
        self._value = v
        self.values = ["MUTATED"]


def _climate(params: dict, attributes: dict | None = None):
    """Build an AC climate entity whose `settings` command has `params`."""
    settings = RecordingCommand(params)
    coordinator = FakeCoordinator(_ac({"settings": settings}, attributes))
    entity = climate.HaierClimateEntity(coordinator, "ac-1", FakeClient())
    entity.hass = FakeHass()
    return entity, settings, coordinator


def _climate_failing(params: dict):
    settings = FailingCommand(params)
    coordinator = FakeCoordinator(_ac({"settings": settings}))
    entity = climate.HaierClimateEntity(coordinator, "ac-1", FakeClient())
    entity.hass = FakeHass()
    return entity, settings, coordinator


class AcClimateWritePathTest(unittest.IsolatedAsyncioTestCase):
    """climate.HaierClimateEntity send semantics."""

    async def test_turn_on_sends_cool(self) -> None:
        # machMode seeded with a junk value to prove the code overwrites it.
        entity, settings, coord = _climate(
            {"onOffStatus": Param("0"), "machMode": Param("9")}
        )
        await entity.async_turn_on()
        self.assertEqual({"onOffStatus": "1", "machMode": "1"}, settings.sent)
        self.assertEqual(1, settings.send_calls)
        self.assertEqual(1, coord.refreshes)

    async def test_turn_off_sends_only_onoff(self) -> None:
        # No machMode in the command: if turn_off tried to send it, async_send_command
        # would raise "Parameter(s) not found" and the assert would fail. So this also
        # proves OFF does NOT touch machMode.
        entity, settings, _ = _climate({"onOffStatus": Param("1")})
        await entity.async_turn_off()
        self.assertEqual({"onOffStatus": "0"}, settings.sent)
        self.assertEqual(1, settings.send_calls)

    async def test_set_hvac_mode_maps_each_mode(self) -> None:
        # Golden codes from AS35: auto=0, cool=1, dry=2, heat=4, fan_only=6.
        cases = [
            (HVACMode.COOL, "1"),
            (HVACMode.HEAT, "4"),
            (HVACMode.AUTO, "0"),
            (HVACMode.DRY, "2"),
            (HVACMode.FAN_ONLY, "6"),
        ]
        for mode, code in cases:
            with self.subTest(mode=mode):
                entity, settings, _ = _climate(
                    {"onOffStatus": Param("0"), "machMode": Param("0")}
                )
                await entity.async_set_hvac_mode(mode)
                self.assertEqual(
                    {"onOffStatus": "1", "machMode": code}, settings.sent
                )

    async def test_set_hvac_mode_off_branch(self) -> None:
        entity, settings, _ = _climate({"onOffStatus": Param("1")})
        await entity.async_set_hvac_mode(HVACMode.OFF)
        self.assertEqual({"onOffStatus": "0"}, settings.sent)

    async def test_set_hvac_mode_unknown_defaults_to_cool(self) -> None:
        # A mode whose .value is not in the map must fall back to "1" (cool),
        # exercising the AC_MODE_MAP_REVERSE.get(..., "1") default literal.
        entity, settings, _ = _climate(
            {"onOffStatus": Param("0"), "machMode": Param("0")}
        )
        await entity.async_set_hvac_mode(types.SimpleNamespace(value="not_a_real_mode"))
        self.assertEqual({"onOffStatus": "1", "machMode": "1"}, settings.sent)

    async def test_set_temperature_client_none_raises(self) -> None:
        # Appliance present but client None: the client check (distinct from the
        # appliance one) runs BEFORE the try, so it raises the specific key and
        # sends nothing. A mutant dropping the client check would instead fall
        # through and surface a generic command_error.
        settings = RecordingCommand({"tempSel": Param("16")})
        coordinator = FakeCoordinator(_ac({"settings": settings}))
        entity = climate.HaierClimateEntity(coordinator, "ac-1", None)
        entity.hass = FakeHass()
        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_set_temperature(temperature=22)
        self.assertEqual(
            "appliance_or_client_unavailable",
            getattr(ctx.exception, "translation_key", None),
        )
        self.assertEqual(0, settings.send_calls)

    async def test_swing_reraises_domain_error_unchanged(self) -> None:
        # A HomeAssistantError surfacing from the send must propagate with its own
        # translation_key, NOT be rewrapped into "command_error".
        settings = DomainErrorCommand(
            {"windDirectionVertical": Param("2", values=["2", "8"])}, key="inner_key"
        )
        coordinator = FakeCoordinator(_ac({"settings": settings}))
        entity = climate.HaierClimateEntity(coordinator, "ac-1", FakeClient())
        entity.hass = FakeHass()
        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_set_swing_mode("on")
        self.assertEqual("inner_key", getattr(ctx.exception, "translation_key", None))

    async def test_set_temperature_integer_sent_clean(self) -> None:
        entity, settings, _ = _climate({"tempSel": Param("16")})
        await entity.async_set_temperature(temperature=23.0)
        # Integer-valued: clean int string, never "23.0".
        self.assertEqual({"tempSel": "23"}, settings.sent)

    async def test_set_temperature_fractional_not_truncated(self) -> None:
        entity, settings, _ = _climate({"tempSel": Param("16")})
        await entity.async_set_temperature(temperature=23.5)
        # Fractional value keeps its decimals (no int() truncation to "23"); the
        # engine Range setter validates it against the device step/grid.
        self.assertEqual({"tempSel": "23.5"}, settings.sent)

    async def test_temp_range_read_from_device(self) -> None:
        entity, _, _ = _climate(
            {"tempSel": RangeParam("20", mn=18, mx=28, step=0.5)}
        )
        self.assertEqual(18.0, entity.min_temp)
        self.assertEqual(28.0, entity.max_temp)
        self.assertEqual(0.5, entity.target_temperature_step)

    async def test_temp_range_is_live_not_snapshot(self) -> None:
        # The range is read live from the parameter (like number.py), so a runtime
        # change to the device's min/max/step is reflected, not frozen at __init__.
        param = RangeParam("20", mn=18, mx=28, step=1)
        entity, _, _ = _climate({"tempSel": param})
        param.min, param.max, param.step = 10.0, 32.0, 0.5
        self.assertEqual(10.0, entity.min_temp)
        self.assertEqual(32.0, entity.max_temp)
        self.assertEqual(0.5, entity.target_temperature_step)

    async def test_temp_range_fallback_when_not_a_range(self) -> None:
        # tempSel present but without min/max/step -> fallback (16, 30, 1.0).
        entity, _, _ = _climate({"tempSel": Param("20")})
        self.assertEqual(16.0, entity.min_temp)
        self.assertEqual(30.0, entity.max_temp)
        self.assertEqual(1.0, entity.target_temperature_step)

    async def test_set_temperature_without_temperature_is_noop(self) -> None:
        entity, settings, coord = _climate({"tempSel": Param("16")})
        await entity.async_set_temperature(target_temp_high=25)
        self.assertEqual(0, settings.send_calls)
        self.assertIsNone(settings.sent)
        self.assertEqual(0, coord.refreshes)

    async def test_set_fan_mode_maps_each_speed(self) -> None:
        # Golden codes from AS35: high=1, medium=2, low=3, auto=5.
        for fan, code in [("auto", "5"), ("high", "1"), ("medium", "2"), ("low", "3")]:
            with self.subTest(fan=fan):
                entity, settings, _ = _climate({"windSpeed": Param("5")})
                await entity.async_set_fan_mode(fan)
                self.assertEqual({"windSpeed": code}, settings.sent)

    async def test_set_fan_mode_unknown_defaults_to_auto(self) -> None:
        entity, settings, _ = _climate({"windSpeed": Param("1")})
        await entity.async_set_fan_mode("nonsense")
        self.assertEqual({"windSpeed": "5"}, settings.sent)

    async def test_set_swing_on_sends_8(self) -> None:
        entity, settings, _ = _climate(
            {"windDirectionVertical": Param("2", values=["2", "4", "5", "8"])}
        )
        await entity.async_set_swing_mode("on")
        self.assertEqual({"windDirectionVertical": "8"}, settings.sent)

    async def test_set_swing_off_prefers_fixed_2(self) -> None:
        entity, settings, _ = _climate(
            {"windDirectionVertical": Param("8", values=["0", "2", "4", "8"])}
        )
        await entity.async_set_swing_mode("off")
        # OFF must pick a fixed position; never 8 (swing) and never 0.
        self.assertEqual({"windDirectionVertical": "2"}, settings.sent)

    async def test_set_swing_off_first_fixed_when_no_2(self) -> None:
        entity, settings, _ = _climate(
            {"windDirectionVertical": Param("8", values=["4", "5", "8"])}
        )
        await entity.async_set_swing_mode("off")
        self.assertEqual({"windDirectionVertical": "4"}, settings.sent)

    async def test_set_swing_not_supported_raises(self) -> None:
        # settings has no windDirectionVertical -> swing unsupported.
        entity, settings, _ = _climate({"onOffStatus": Param("0")})
        with self.assertRaises(Exception) as ctx:
            await entity.async_set_swing_mode("on")
        self.assertEqual("swing_not_supported", getattr(ctx.exception, "translation_key", None))
        self.assertEqual(0, settings.send_calls)

    async def test_set_swing_position_not_allowed_raises(self) -> None:
        # "8" requested for ON but not among the allowed values.
        entity, settings, _ = _climate(
            {"windDirectionVertical": Param("2", values=["2", "4", "5"])}
        )
        with self.assertRaises(Exception) as ctx:
            await entity.async_set_swing_mode("on")
        self.assertEqual(
            "swing_position_not_allowed", getattr(ctx.exception, "translation_key", None)
        )
        self.assertEqual(0, settings.send_calls)

    async def test_hvac_mode_send_failure_rolls_back(self) -> None:
        entity, settings, coord = _climate_failing(
            {"onOffStatus": Param("0"), "machMode": Param("0")}
        )
        with self.assertRaises(Exception):
            await entity.async_set_hvac_mode(HVACMode.COOL)
        self.assertEqual(1, settings.send_calls)
        # Rollback restored the pre-send values.
        self.assertEqual("0", settings.parameters["onOffStatus"].value)
        self.assertEqual("0", settings.parameters["machMode"].value)
        # No refresh after a failed command.
        self.assertEqual(0, coord.refreshes)

    async def test_hvac_modes_derived_from_enum(self) -> None:
        # Device exposes only auto+cool: HEAT/DRY/FAN_ONLY must NOT be offered.
        entity, _, _ = _climate({"machMode": Param("0", values=["0", "1"])})
        self.assertEqual([HVACMode.OFF, HVACMode.AUTO, HVACMode.COOL], entity._attr_hvac_modes)
        self.assertNotIn(HVACMode.HEAT, entity._attr_hvac_modes)

    async def test_hvac_modes_full_when_all_enum_values(self) -> None:
        entity, _, _ = _climate(
            {"machMode": Param("1", values=["0", "1", "2", "4", "6"])}
        )
        self.assertEqual(
            [
                HVACMode.OFF,
                HVACMode.AUTO,
                HVACMode.COOL,
                HVACMode.DRY,
                HVACMode.HEAT,
                HVACMode.FAN_ONLY,
            ],
            entity._attr_hvac_modes,
        )

    async def test_hvac_modes_fallback_when_enum_absent(self) -> None:
        # machMode present but no enum values -> full HA list (no regression).
        entity, _, _ = _climate({"machMode": Param("0")})
        self.assertEqual(6, len(entity._attr_hvac_modes))
        self.assertIn(HVACMode.HEAT, entity._attr_hvac_modes)

    async def test_hvac_modes_fallback_when_param_missing(self) -> None:
        entity, _, _ = _climate({"onOffStatus": Param("0")})  # no machMode
        self.assertIn(HVACMode.OFF, entity._attr_hvac_modes)
        self.assertIn(HVACMode.HEAT, entity._attr_hvac_modes)

    async def test_fan_modes_derived_from_enum(self) -> None:
        # windSpeed exposes only auto+high (enum order preserved).
        entity, _, _ = _climate({"windSpeed": Param("5", values=["5", "1"])})
        self.assertEqual(["auto", "high"], entity._attr_fan_modes)
        self.assertNotIn("medium", entity._attr_fan_modes)

    async def test_fan_modes_fallback_when_param_missing(self) -> None:
        entity, _, _ = _climate({"onOffStatus": Param("0")})  # no windSpeed
        self.assertEqual({"auto", "low", "medium", "high"}, set(entity._attr_fan_modes))

    async def test_appliance_unavailable_raises(self) -> None:
        coordinator = FakeCoordinator({})  # no appliance data
        entity = climate.HaierClimateEntity(coordinator, "ac-1", FakeClient())
        entity.hass = FakeHass()
        with self.assertRaises(Exception):
            await entity.async_set_hvac_mode(HVACMode.COOL)


class AcSwitchWritePathTest(unittest.IsolatedAsyncioTestCase):
    """switch.HonAcSwitch send semantics."""

    _DESC = switch.HonAcSwitchDescription(key="eco", param="echoStatus", icon="mdi:leaf")

    def _switch(self, params: dict, attributes: dict | None = None, *, failing=False):
        cmd = (FailingCommand if failing else RecordingCommand)(params)
        coordinator = FakeCoordinator(_ac({"settings": cmd}, attributes))
        entity = switch.HonAcSwitch(coordinator, "ac-1", self._DESC, FakeClient())
        entity.hass = FakeHass()
        return entity, cmd, coordinator

    async def test_turn_on_sends_1(self) -> None:
        entity, cmd, coord = self._switch({"echoStatus": Param("0")})
        await entity.async_turn_on()
        self.assertEqual({"echoStatus": "1"}, cmd.sent)
        self.assertEqual(1, cmd.send_calls)
        self.assertEqual(1, coord.refreshes)

    async def test_turn_off_sends_0(self) -> None:
        entity, cmd, _ = self._switch({"echoStatus": Param("1")})
        await entity.async_turn_off()
        self.assertEqual({"echoStatus": "0"}, cmd.sent)

    async def test_is_on_reads_param(self) -> None:
        on, _, _ = self._switch({"echoStatus": Param("1")}, {"echoStatus": "1"})
        off, _, _ = self._switch({"echoStatus": Param("0")}, {"echoStatus": "0"})
        absent, _, _ = self._switch({"echoStatus": Param("0")}, {})
        self.assertIs(True, on.is_on)
        self.assertIs(False, off.is_on)
        self.assertIsNone(absent.is_on)

    async def test_send_failure_rolls_back(self) -> None:
        entity, cmd, _ = self._switch({"echoStatus": Param("0")}, failing=True)
        with self.assertRaises(Exception):
            await entity.async_turn_on()
        self.assertEqual(1, cmd.send_calls)
        self.assertEqual("0", cmd.parameters["echoStatus"].value)

    async def test_set_param_client_none_raises(self) -> None:
        cmd = RecordingCommand({"echoStatus": Param("0")})
        coordinator = FakeCoordinator(_ac({"settings": cmd}))
        entity = switch.HonAcSwitch(coordinator, "ac-1", self._DESC, None)
        entity.hass = FakeHass()
        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_turn_on()
        self.assertEqual(
            "appliance_or_client_unavailable",
            getattr(ctx.exception, "translation_key", None),
        )
        self.assertEqual(0, cmd.send_calls)

    async def test_reraises_domain_error_unchanged(self) -> None:
        cmd = DomainErrorCommand({"echoStatus": Param("0")}, key="inner_sw")
        coordinator = FakeCoordinator(_ac({"settings": cmd}))
        entity = switch.HonAcSwitch(coordinator, "ac-1", self._DESC, FakeClient())
        entity.hass = FakeHass()
        with self.assertRaises(HomeAssistantError) as ctx:
            await entity.async_turn_on()
        self.assertEqual("inner_sw", getattr(ctx.exception, "translation_key", None))


class AcCommandUnitTest(unittest.IsolatedAsyncioTestCase):
    """Pure helpers of ac_command + the requested-value-wins integration."""

    def test_reverse_maps_are_exact(self) -> None:
        self.assertEqual(
            {"auto": "0", "cool": "1", "dry": "2", "heat": "4", "fan_only": "6"},
            AC_MODE_MAP_REVERSE,
        )
        self.assertEqual(
            {"auto": "5", "low": "3", "medium": "2", "high": "1"},
            AC_FAN_MAP_REVERSE,
        )

    def test_fixed_vertical_value(self) -> None:
        self.assertEqual("2", ac_command.fixed_vertical_value(["2", "4", "8"]))
        self.assertEqual("4", ac_command.fixed_vertical_value(["4", "5", "8"]))
        self.assertEqual("8", ac_command.fixed_vertical_value(["8"]))
        self.assertEqual("8", ac_command.fixed_vertical_value([]))

    def test_param_allowed_values(self) -> None:
        self.assertEqual(
            ["2", "4", "8"], ac_command.param_allowed_values(Param(values=["2", 4, "8"]))
        )
        self.assertEqual([], ac_command.param_allowed_values(Param(values=None)))

    def test_sanitize_vertical_resets_when_not_allowed(self) -> None:
        param = Param("0", values=["2", "4", "8"])
        ac_command.sanitize_wind_direction({"windDirectionVertical": param})
        self.assertEqual("2", param.value)

    def test_sanitize_keeps_allowed_value(self) -> None:
        param = Param("4", values=["2", "4", "8"])
        ac_command.sanitize_wind_direction({"windDirectionVertical": param})
        self.assertEqual("4", param.value)

    def test_sanitize_horizontal_picks_non_zero(self) -> None:
        # current "9" is NOT allowed -> horizontal branch picks the first non-"0".
        param = Param("9", values=["0", "3", "5"])
        ac_command.sanitize_wind_direction({"windDirectionHorizontal": param})
        self.assertEqual("3", param.value)

    async def test_requested_value_wins_over_sanitize(self) -> None:
        # Order-discriminant: the requested windDirectionVertical="8" is NOT among
        # the allowed values, so sanitize WOULD rewrite it. Only if pre_send runs
        # BEFORE the requested params are applied does "8" survive on the wire.
        # (If the order were swapped, pre_send would see "8", find it disallowed and
        # reset it to the fixed value "2".) The untouched windDirectionHorizontal=9
        # (not allowed) still gets sanitized to the first non-zero allowed ("3").
        wdv = Param("0", values=["2", "4"])  # no "8"
        wdh = Param("9", values=["0", "3"])
        settings = RecordingCommand(
            {"windDirectionVertical": wdv, "windDirectionHorizontal": wdh}
        )
        appliance = _ac({"settings": settings})["ac-1"]["appliance"]
        await ac_command.async_send_settings(
            FakeHass(), FakeClient(), appliance, {"windDirectionVertical": "8"}
        )
        self.assertEqual(
            {"windDirectionVertical": "8", "windDirectionHorizontal": "3"}, settings.sent
        )

    async def test_sanitize_skips_when_allowed_empty(self) -> None:
        # A wind-direction param with no enum values must be left untouched, never
        # silently forced to "8" (swing). The send carries another param so the
        # command actually goes out.
        wdv = Param("0", values=None)
        settings = RecordingCommand(
            {"windDirectionVertical": wdv, "tempSel": Param("16")}
        )
        appliance = _ac({"settings": settings})["ac-1"]["appliance"]
        await ac_command.async_send_settings(
            FakeHass(), FakeClient(), appliance, {"tempSel": "20"}
        )
        self.assertEqual("0", wdv.value)

    async def test_missing_param_raises(self) -> None:
        settings = RecordingCommand({"tempSel": Param("16")})
        appliance = _ac({"settings": settings})["ac-1"]["appliance"]
        with self.assertRaises(RuntimeError) as ctx:
            await ac_command.async_send_settings(
                FakeHass(), FakeClient(), appliance, {"ghostParam": "1"}
            )
        self.assertIn("not found", str(ctx.exception).lower())
        self.assertEqual(0, settings.send_calls)

    async def test_rollback_restores_full_dict_not_just_value(self) -> None:
        # The rollback restores __dict__ (value AND values/min/max), not only value.
        # RuleParam mutates .values on assignment; after a failed send both must
        # be back to their pre-send state.
        param = RuleParam("16", ["16", "17"])
        settings = FailingCommand({"tempSel": param})
        appliance = _ac({"settings": settings})["ac-1"]["appliance"]
        with self.assertRaises(Exception):
            await ac_command.async_send_settings(
                FakeHass(), FakeClient(), appliance, {"tempSel": "20"}
            )
        self.assertEqual("16", param.value)
        self.assertEqual(["16", "17"], param.values)


if __name__ == "__main__":
    unittest.main()
