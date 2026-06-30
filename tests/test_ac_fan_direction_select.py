"""Tests for the AC manual fan-direction position selects (discussion #37).

Two capability-gated selects per AC -- vertical + horizontal louver position --
whose options come from the device's LIVE per-model enum (never hard-coded), which
send immediately through ac_command.async_send_settings (so a requested position
survives the windDirection sanitizer), and whose current_option maps the reported
value (None when out-of-enum, never raising). Ground truth:
apk/dump/ac_mik/config_entry_diag.json (AS68PDAHRA / AS35RBAHRA-3 / AS35PBPHRA-PRE)
cross-validated by apk/dump/ac_roberto (AS35PBPHRA-PRE, H actively 5).

Drives the real async_setup_entry end-to-end (gating + registration), reusing the HA
stubs + Fake* harness from test_program_select (importing it installs the stubs at
module load). A local DirParam adds `typology` (the shared Param lacks it) and a local
RecordingCommand captures the sent payload. Golden enums/keys are hard-coded (NOT
imported from const) so a const mutation cannot pass unnoticed.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_program_select import (  # noqa: E402
    FakeClient,
    FakeCoordinator,
    FakeEntry,
    FakeHass,
    _ac,
)

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.addhon import select  # noqa: E402
from custom_components.addhon.const import DOMAIN  # noqa: E402


class DirParam:
    """settings enum/fixed param stub: value + values + typology (the shared Param has
    no typology, which the fixed-typology gate needs)."""

    def __init__(self, value=None, values=None, typology="enum") -> None:
        self.value = value
        self.values = values
        self.typology = typology


class RecordingCommand:
    """Captures the payload frozen at send() time (what reaches the device)."""

    def __init__(self, parameters=None) -> None:
        self.parameters = parameters or {}
        self.send_calls = 0
        self.sent = None

    async def send(self) -> None:
        self.send_calls += 1
        self.sent = {k: p.value for k, p in self.parameters.items()}


# Golden per-model enums (apk/dump/ac_mik + ac_roberto), NOT imported from const.
V_FULL = ["2", "4", "5", "6", "7", "8"]      # AS68PDAHRA / AS35PBPHRA-PRE
V_NO_7 = ["2", "4", "5", "6", "8"]           # AS35RBAHRA-3 (no 7)
H_ENUM = ["0", "3", "4", "5", "6", "7"]      # AS35RBAHRA-3 / AS35PBPHRA-PRE
H_FIXED = ["0"]                              # AS68PDAHRA (typology=fixed)


class AcFanDirectionSelectTest(unittest.IsolatedAsyncioTestCase):
    async def _setup(self, settings_params, attributes=None):
        settings = RecordingCommand(settings_params)
        coordinator = FakeCoordinator(_ac({"settings": settings}, attributes))
        hass = FakeHass(
            {DOMAIN: {"entry-1": {"coordinator": coordinator, "client": FakeClient()}}}
        )
        added: list = []
        await select.async_setup_entry(hass, FakeEntry(), added.extend)
        for entity in added:
            entity.hass = hass
        return added, settings, coordinator

    @staticmethod
    def _maybe(added, translation_key):
        for entity in added:
            if getattr(entity, "_attr_translation_key", None) == translation_key:
                return entity
        return None

    def _by_key(self, added, translation_key):
        entity = self._maybe(added, translation_key)
        self.assertIsNotNone(entity, f"missing select {translation_key}")
        return entity

    async def test_vertical_options_from_live_enum(self) -> None:
        added, _, _ = await self._setup(
            {
                "windDirectionVertical": DirParam("5", values=V_FULL),
                "windDirectionHorizontal": DirParam("3", values=H_ENUM),
            }
        )
        v = self._by_key(added, "fan_direction_vertical")
        self.assertEqual(
            ["position_2", "position_4", "position_5", "position_6", "position_7", "swing"],
            v._attr_options,
        )

    async def test_vertical_per_model_no_7(self) -> None:
        added, _, _ = await self._setup(
            {"windDirectionVertical": DirParam("5", values=V_NO_7)}
        )
        v = self._by_key(added, "fan_direction_vertical")
        self.assertEqual(
            ["position_2", "position_4", "position_5", "position_6", "swing"],
            v._attr_options,
        )
        self.assertNotIn("position_7", v._attr_options)

    async def test_horizontal_options_from_live_enum(self) -> None:
        added, _, _ = await self._setup(
            {
                "windDirectionVertical": DirParam("5", values=V_FULL),
                "windDirectionHorizontal": DirParam("5", values=H_ENUM),
            }
        )
        h = self._by_key(added, "fan_direction_horizontal")
        # Horizontal mirrors vertical: fixed positions (0,3,4,5,6) plus exactly one swing
        # value, which on the horizontal axis is 7 (FAN_DIR_H_LABELS); order follows the
        # device enum, so "swing" (7) lands last.
        self.assertEqual(
            ["position_0", "position_3", "position_4", "position_5", "position_6", "swing"],
            h._attr_options,
        )
        self.assertNotIn("position_7", h._attr_options)   # 7 is swing, not a position
        self.assertIn("swing", h._attr_options)

    async def test_both_created_for_enum_model(self) -> None:
        added, _, _ = await self._setup(
            {
                "windDirectionVertical": DirParam("5", values=V_FULL),
                "windDirectionHorizontal": DirParam("0", values=H_ENUM),
            }
        )
        self.assertEqual(
            {"fan_direction_vertical", "fan_direction_horizontal"},
            {e._attr_translation_key for e in added},
        )

    async def test_horizontal_gated_out_on_fixed_typology(self) -> None:
        added, _, _ = await self._setup(
            {
                "windDirectionVertical": DirParam("6", values=V_FULL),
                "windDirectionHorizontal": DirParam("0", values=H_FIXED, typology="fixed"),
            }
        )
        self.assertIsNone(self._maybe(added, "fan_direction_horizontal"))
        self.assertIsNotNone(self._maybe(added, "fan_direction_vertical"))

    async def test_fixed_typology_gates_independent_of_count(self) -> None:
        added, _, _ = await self._setup(
            {
                "windDirectionVertical": DirParam("6", values=V_FULL),
                "windDirectionHorizontal": DirParam("0", values=["0", "3", "4"], typology="fixed"),
            }
        )
        self.assertIsNone(self._maybe(added, "fan_direction_horizontal"))

    async def test_single_value_enum_gated_out(self) -> None:
        added, _, _ = await self._setup(
            {"windDirectionVertical": DirParam("5", values=["5"])}
        )
        self.assertEqual([], added)

    async def test_no_selects_when_params_absent(self) -> None:
        added, _, _ = await self._setup({"onOffStatus": DirParam("0", values=["0", "1"])})
        self.assertEqual([], added)

    async def test_current_option_maps_live_value(self) -> None:
        added, _, _ = await self._setup(
            {"windDirectionHorizontal": DirParam("5", values=H_ENUM)},
            attributes={"settings.windDirectionHorizontal": "5"},
        )
        # 5 is a fixed horizontal position -> position_5.
        self.assertEqual(
            "position_5", self._by_key(added, "fan_direction_horizontal").current_option
        )

    async def test_current_option_horizontal_swing(self) -> None:
        # 7 is the ONE horizontal swing value -> "swing" (mirror of vertical 8).
        added, _, _ = await self._setup(
            {"windDirectionHorizontal": DirParam("7", values=H_ENUM)},
            attributes={"settings.windDirectionHorizontal": "7"},
        )
        self.assertEqual(
            "swing", self._by_key(added, "fan_direction_horizontal").current_option
        )

    async def test_current_option_swing(self) -> None:
        added, _, _ = await self._setup(
            {"windDirectionVertical": DirParam("8", values=V_FULL)},
            attributes={"settings.windDirectionVertical": "8"},
        )
        self.assertEqual(
            "swing", self._by_key(added, "fan_direction_vertical").current_option
        )

    async def test_current_option_none_when_out_of_enum(self) -> None:
        added, _, _ = await self._setup(
            {
                "windDirectionVertical": DirParam("0", values=V_FULL),
                "windDirectionHorizontal": DirParam("0", values=H_ENUM),
            },
            attributes={
                "settings.windDirectionVertical": "0",
                "settings.windDirectionHorizontal": "2",
            },
        )
        self.assertIsNone(self._by_key(added, "fan_direction_vertical").current_option)
        self.assertIsNone(self._by_key(added, "fan_direction_horizontal").current_option)

    async def test_current_option_none_when_absent(self) -> None:
        added, _, _ = await self._setup(
            {"windDirectionVertical": DirParam("5", values=V_FULL)}
        )
        self.assertIsNone(self._by_key(added, "fan_direction_vertical").current_option)

    async def test_non_canonical_enum_value_normalized(self) -> None:
        # A device advertising a non-canonical enum value ("5.0") must still build the
        # clean option key, read back through current_option, and carry the canonical
        # code as the send value -- the option map is normalized in __init__ so it stays
        # symmetric with current_option()'s normalize_code lookup (CodeRabbit, PR #42).
        added, _, _ = await self._setup(
            {"windDirectionVertical": DirParam("5.0", values=["2", "4", "5.0", "6", "8"])},
            attributes={"settings.windDirectionVertical": "5.0"},
        )
        v = self._by_key(added, "fan_direction_vertical")
        self.assertIn("position_5", v._attr_options)
        self.assertNotIn("5.0", v._attr_options)
        self.assertEqual("position_5", v.current_option)
        self.assertEqual("5", v._key_to_raw["position_5"])

    async def test_select_sends_setparameters(self) -> None:
        added, settings, coord = await self._setup(
            {
                "windDirectionVertical": DirParam("5", values=V_FULL),
                "windDirectionHorizontal": DirParam("3", values=H_ENUM),
            }
        )
        await self._by_key(added, "fan_direction_vertical").async_select_option("position_6")
        self.assertEqual("6", settings.sent["windDirectionVertical"])
        self.assertEqual(1, settings.send_calls)
        self.assertEqual(1, coord.refreshes)

    async def test_select_swing_sends_8(self) -> None:
        added, settings, _ = await self._setup(
            {"windDirectionVertical": DirParam("5", values=V_FULL)}
        )
        await self._by_key(added, "fan_direction_vertical").async_select_option("swing")
        self.assertEqual("8", settings.sent["windDirectionVertical"])

    async def test_select_horizontal_sends(self) -> None:
        added, settings, _ = await self._setup(
            {
                "windDirectionVertical": DirParam("5", values=V_FULL),
                "windDirectionHorizontal": DirParam("0", values=H_ENUM),
            }
        )
        await self._by_key(added, "fan_direction_horizontal").async_select_option("position_5")
        self.assertEqual("5", settings.sent["windDirectionHorizontal"])

    async def test_select_horizontal_swing_sends_7(self) -> None:
        added, settings, _ = await self._setup(
            {
                "windDirectionVertical": DirParam("5", values=V_FULL),
                "windDirectionHorizontal": DirParam("0", values=H_ENUM),
            }
        )
        await self._by_key(added, "fan_direction_horizontal").async_select_option("swing")
        self.assertEqual("7", settings.sent["windDirectionHorizontal"])

    async def test_requested_position_wins_over_sanitizer(self) -> None:
        added, settings, _ = await self._setup(
            {
                "windDirectionVertical": DirParam("0", values=V_FULL),
                "windDirectionHorizontal": DirParam("9", values=H_ENUM),
            }
        )
        await self._by_key(added, "fan_direction_vertical").async_select_option("position_6")
        self.assertEqual("6", settings.sent["windDirectionVertical"])
        self.assertEqual("3", settings.sent["windDirectionHorizontal"])

    async def test_invalid_option_raises_without_send(self) -> None:
        added, settings, _ = await self._setup(
            {"windDirectionVertical": DirParam("5", values=V_FULL)}
        )
        with self.assertRaises(HomeAssistantError) as ctx:
            await self._by_key(added, "fan_direction_vertical").async_select_option("nope")
        self.assertEqual("invalid_setpoint", getattr(ctx.exception, "translation_key", None))
        self.assertEqual(0, settings.send_calls)
        self.assertIsNone(settings.sent)


class AcFanDirectionI18nTest(unittest.TestCase):
    """Every offered option key must be translated in BOTH en.json and it.json, with
    identical state-key sets (project rule addhon-i18n-eng-ita; no strings.json)."""

    def _select(self, lang):
        base = REPO_ROOT / "custom_components" / "addhon" / "translations"
        return json.loads((base / f"{lang}.json").read_text(encoding="utf-8"))["entity"]["select"]

    def test_completeness_and_parity(self) -> None:
        from custom_components.addhon.const import FAN_DIR_H_LABELS, FAN_DIR_V_LABELS

        en = self._select("en")
        it = self._select("it")
        for key, label_map in (
            ("fan_direction_vertical", FAN_DIR_V_LABELS),
            ("fan_direction_horizontal", FAN_DIR_H_LABELS),
        ):
            self.assertIn("name", en[key])
            self.assertIn("name", it[key])
            en_state = set(en[key]["state"])
            it_state = set(it[key]["state"])
            for opt_key in label_map.values():
                self.assertIn(opt_key, en_state, f"en missing {key}.{opt_key}")
                self.assertIn(opt_key, it_state, f"it missing {key}.{opt_key}")
            self.assertEqual(en_state, it_state, f"en/it parity {key}")

    def test_each_axis_has_one_swing_key(self) -> None:
        # The two axes are symmetric: each carries exactly one "swing" key (vertical value
        # 8, horizontal value 7) plus fixed "position_N" keys; no "fixed" key on either.
        # Holds in both languages.
        for lang in ("en", "it"):
            sel = self._select(lang)
            v_state = sel["fan_direction_vertical"]["state"]
            h_state = sel["fan_direction_horizontal"]["state"]
            self.assertIn("swing", v_state)
            self.assertIn("swing", h_state)
            self.assertNotIn("fixed", v_state)
            self.assertNotIn("fixed", h_state)
            self.assertTrue(
                all(k == "swing" or k.startswith("position_") for k in v_state),
                f"{lang}: vertical keys must be swing or position_N",
            )
            self.assertTrue(
                all(k == "swing" or k.startswith("position_") for k in h_state),
                f"{lang}: horizontal keys must be swing or position_N",
            )


if __name__ == "__main__":
    unittest.main()
