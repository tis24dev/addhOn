# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The induction hob's intake-limit select (#84.1).

`settings.powerManagement` is the ONE parameter a hob declares as writable: there
is no remote way to switch a zone on or set its power, and this control does not
pretend otherwise. It caps what the whole hob may DRAW, in kW, which is why every
label it produces says limit.

The schema below is transcribed from the diagnostics dump of the reporting user's
HA2MTSJ68MC, its categorised `settings` shape included.
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_hood_entities import (  # noqa: E402  (installs the platform stubs)
    RecordingApi,
    RecordingHass,
    RefreshingCoordinator,
    RunningClient,
    FakeEntry,
)

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.addhon.const import APPLIANCE_IH  # noqa: E402

# `powerManagement` as the hob declares it: a mandatory range 0..5, which the
# engine materialises into six selectable levels.
HOB_SETTINGS_PARAMS = {
    "powerManagement": {
        "typology": "range", "category": "command", "mandatory": 1,
        "minimumValue": "0", "maximumValue": "5", "incrementValue": "1",
        "defaultValue": "0",
    },
}

HOB_ATTRIBUTES = {
    "available": True,
    "powerManagement": 0,
    "remoteCtrValid": 0,
}


class _HobAppliance:
    def __init__(self, series: str) -> None:
        self.zone = 0
        self.options: dict[str, str] = {}
        self.commands: dict[str, object] = {}
        self.unique_id = "ih-1"
        self.api = RecordingApi()
        self.model_attributes = {"series": series, "zone": "4", "power": "15"}

    def sync_command_to_params(self, name: str) -> None:
        pass


def _appliance(series: str = "series6", params: dict | None = None):
    """A hob whose `settings` command is CATEGORISED, as the real one is.

    That shape is what makes the engine attach the synthetic `category`
    parameter, and it is the ingredient the payload assertion below denies ever
    reaches the wire.
    """
    from custom_components.addhon.client.engine.commands import HonCommand

    appliance = _HobAppliance(series)
    categories: dict[str, HonCommand] = {}
    settings = HonCommand(
        "settings",
        {"parameters": copy.deepcopy(HOB_SETTINGS_PARAMS if params is None else params)},
        appliance,
        categories=categories,
        category_name="setParameters",
    )
    categories["setParameters"] = settings
    categories["setConfig"] = HonCommand(
        "settings",
        {"parameters": {"httpEndpoint": {"typology": "fixed", "category": "command",
                                         "mandatory": 0, "fixedValue": ""}}},
        appliance,
        categories=categories,
        category_name="setConfig",
    )
    appliance.commands["settings"] = settings
    return appliance


async def _build(appliance=None, attributes=None, client=None) -> list:
    from custom_components.addhon import select
    from custom_components.addhon.const import DOMAIN

    data = {
        "ih-1": {
            "type": APPLIANCE_IH,
            "name": "Hob",
            "model": "HA2MTSJ68MC",
            "attributes": copy.deepcopy(HOB_ATTRIBUTES) if attributes is None else attributes,
            "appliance": _appliance() if appliance is None else appliance,
            "settings": {},
            "statistics": {},
        }
    }
    coordinator = RefreshingCoordinator(data)
    hass = RecordingHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": client}}})
    added: list = []
    await select.async_setup_entry(hass, FakeEntry(), added.extend)
    for entity in added:
        entity.hass = hass
    return added


class PowerLimitScaleTest(unittest.TestCase):
    """The level-to-kW dictionaries, which the app picks by model series."""

    def test_a_normal_hob_uses_the_six_step_scale(self) -> None:
        from custom_components.addhon.hob import power_limit_levels

        levels = power_limit_levels(_appliance("series6"), "series6")
        self.assertEqual(
            {"0": "no_limit", "1": "kw_2_5", "2": "kw_3_5",
             "3": "kw_4_5", "4": "kw_5_5", "5": "kw_6_5"},
            levels,
        )

    def test_a_supernova_hob_uses_a_different_scale_entirely(self) -> None:
        # Not an extension of the other one: level 1 is 2.0 kW here and 2.5 kW
        # there, so picking the wrong dictionary mislabels every option.
        from custom_components.addhon.hob import power_limit_levels

        levels = power_limit_levels(_appliance("invisible"), "invisible")
        self.assertEqual("kw_2_0", levels["1"])
        self.assertEqual("kw_3_0", levels["3"])

    def test_the_series_test_matches_the_apps_three_values(self) -> None:
        from custom_components.addhon.hob import is_supernova

        for supernova in ("invisible", "double", "tft", "TFT", " Double "):
            self.assertTrue(is_supernova(supernova), supernova)
        for normal in ("series4", "series6", "series6tft", "timeless", "", None):
            self.assertFalse(is_supernova(normal), normal)

    def test_a_level_the_scale_cannot_name_is_dropped(self) -> None:
        # A normal-series hob declaring 0..8 would reach levels 6, 7 and 8, which
        # its own scale has no kW figure for. An unlabelled step is not a choice.
        params = copy.deepcopy(HOB_SETTINGS_PARAMS)
        params["powerManagement"]["maximumValue"] = "8"
        from custom_components.addhon.hob import power_limit_levels

        levels = power_limit_levels(_appliance("series6", params), "series6")
        self.assertEqual(["0", "1", "2", "3", "4", "5"], list(levels))

    def test_a_hob_without_the_parameter_gets_no_control(self) -> None:
        from custom_components.addhon.hob import power_limit_levels

        self.assertIsNone(power_limit_levels(_appliance("series6", {}), "series6"))

    def test_a_pinned_single_level_is_not_a_choice(self) -> None:
        params = copy.deepcopy(HOB_SETTINGS_PARAMS)
        params["powerManagement"]["maximumValue"] = "0"
        from custom_components.addhon.hob import power_limit_levels

        self.assertIsNone(power_limit_levels(_appliance("series6", params), "series6"))

    def test_every_option_key_the_scales_can_produce_is_declared(self) -> None:
        # `HOB_POWER_LIMIT_OPTIONS` is what the translations are checked against,
        # so it must cover both dictionaries and nothing else.
        from custom_components.addhon.hob import (
            HOB_POWER_LIMIT_OPTIONS,
            power_limit_levels,
        )

        produced = set()
        for series in ("series6", "invisible"):
            params = copy.deepcopy(HOB_SETTINGS_PARAMS)
            params["powerManagement"]["maximumValue"] = "8"
            produced |= set(power_limit_levels(_appliance(series, params), series).values())
        self.assertEqual(set(HOB_POWER_LIMIT_OPTIONS), produced)


class PowerLimitEntityTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_hob_gets_exactly_one_select(self) -> None:
        added = await _build()
        self.assertEqual(["ih-1_power_limit"], [e._attr_unique_id for e in added])

    async def test_the_options_are_offered_in_device_order(self) -> None:
        # The schema lists the levels from no limit upwards, which is the order
        # the app shows and the order a user expects on a slider-like picker.
        added = await _build()
        self.assertEqual(
            ["no_limit", "kw_2_5", "kw_3_5", "kw_4_5", "kw_5_5", "kw_6_5"],
            added[0].options,
        )

    async def test_it_is_a_configuration_control(self) -> None:
        from homeassistant.const import EntityCategory

        added = await _build()
        # `_attr_entity_category` is what real Home Assistant's Entity reads; the
        # stub SelectEntity here has no property over it.
        self.assertEqual(EntityCategory.CONFIG, added[0]._attr_entity_category)

    async def test_the_live_level_reads_back_as_its_option(self) -> None:
        added = await _build(attributes=dict(HOB_ATTRIBUTES, powerManagement=3))
        self.assertEqual("kw_4_5", added[0].current_option)

    async def test_an_unnameable_level_reads_unknown_and_never_raises(self) -> None:
        added = await _build(attributes=dict(HOB_ATTRIBUTES, powerManagement=9))
        self.assertIsNone(added[0].current_option)

    async def test_a_hob_without_the_parameter_gets_no_entity(self) -> None:
        self.assertEqual([], await _build(_appliance("series6", {})))

    async def test_selecting_writes_the_level_on_the_settings_command(self) -> None:
        appliance = _appliance()
        added = await _build(appliance, client=RunningClient())
        await added[0].async_select_option("kw_5_5")
        sent = appliance.api.sent[-1]
        self.assertEqual("settings", sent["command"])
        self.assertEqual({"powerManagement": "4"}, sent["parameters"])

    async def test_the_write_never_ships_a_selector_key(self) -> None:
        # `category` and `program` are the dispatcher's selector keys: naming one
        # makes the engine replace `appliance.commands['settings']` permanently.
        appliance = _appliance()
        settings = appliance.commands["settings"]
        self.assertIn("category", settings.parameters, "the fixture carries no selector")
        added = await _build(appliance, client=RunningClient())
        await added[0].async_select_option("kw_2_5")
        sent = appliance.api.sent[-1]
        self.assertNotIn("category", sent["parameters"])
        self.assertNotIn("program", sent["parameters"])
        self.assertIs(settings, appliance.commands["settings"])

    async def test_the_write_is_never_a_start_program(self) -> None:
        appliance = _appliance()
        added = await _build(appliance, client=RunningClient())
        await added[0].async_select_option("kw_2_5")
        self.assertNotIn("startProgram", [s["command"] for s in appliance.api.sent])

    async def test_selecting_refreshes_the_state(self) -> None:
        added = await _build(client=RunningClient())
        await added[0].async_select_option("kw_2_5")
        self.assertEqual(1, added[0].coordinator.refreshes)

    async def test_an_unknown_option_is_refused_with_the_allowed_list(self) -> None:
        added = await _build(client=RunningClient())
        with self.assertRaises(HomeAssistantError) as caught:
            await added[0].async_select_option("kw_9_9")
        self.assertEqual("invalid_setpoint", caught.exception.translation_key)

    async def test_a_missing_client_raises_the_localized_error(self) -> None:
        added = await _build(client=None)
        with self.assertRaises(HomeAssistantError) as caught:
            await added[0].async_select_option("kw_2_5")
        self.assertEqual(
            "appliance_or_client_unavailable", caught.exception.translation_key
        )

    async def test_a_refusal_surfaces_as_a_command_error(self) -> None:
        # `remoteCtrValid` is 0 on the reporting hob and the cloud may well refuse
        # this write. The refusal is allowed to SURFACE rather than being
        # pre-empted by hiding the control: that is the policy every writable
        # entity of this integration follows.
        added = await _build(client=RunningClient(fail=RuntimeError("rejected")))
        with self.assertRaises(HomeAssistantError) as caught:
            await added[0].async_select_option("kw_2_5")
        self.assertEqual("command_error", caught.exception.translation_key)

    async def test_the_control_is_not_hidden_by_remote_control_being_off(self) -> None:
        added = await _build(attributes=dict(HOB_ATTRIBUTES, remoteCtrValid=0))
        self.assertEqual(1, len(added))


class PowerLimitLabellingTest(unittest.TestCase):
    """The name is the only place a user learns what this control does."""

    @staticmethod
    def _block(lang):
        import json

        path = REPO_ROOT / "custom_components" / "addhon" / "translations" / f"{lang}.json"
        return json.loads(path.read_text(encoding="utf-8"))["entity"]["select"]["power_limit"]

    def test_every_option_key_is_labelled_in_both_languages(self) -> None:
        from custom_components.addhon.hob import HOB_POWER_LIMIT_OPTIONS

        for lang in ("en", "it"):
            self.assertEqual(
                set(HOB_POWER_LIMIT_OPTIONS), set(self._block(lang)["state"]), lang
            )

    def test_the_labels_carry_the_kilowatt_unit(self) -> None:
        for lang in ("en", "it"):
            states = self._block(lang)["state"]
            for key, label in states.items():
                if key == "no_limit":
                    continue
                self.assertTrue(label.endswith(" kW"), f"{lang} {key}: {label}")

    def test_the_name_says_limit_and_never_power_alone(self) -> None:
        # `powerManagement` caps intake; `model_attributes.power` is the panel
        # steps of one zone; the per-zone `power_zone*` sensors are a third thing.
        # A name reading just "Power" would be read as one of the other two.
        expected = {"en": "limit", "it": "limite"}
        for lang, word in expected.items():
            self.assertIn(word, self._block(lang)["name"].lower(), lang)


class PowerLimitCoverageTest(unittest.TestCase):
    def test_the_hob_control_is_no_longer_reported_unmapped(self) -> None:
        from custom_components.addhon import diagnostics

        mapped_attrs, mapped_params, sources, _ = diagnostics._mapped_sets(APPLIANCE_IH)
        self.assertIn("powerManagement", mapped_attrs)
        self.assertIn("powerManagement", mapped_params)
        self.assertEqual(["powerManagement"], sources["select.power_limit"]["write"])

    def test_another_type_did_not_inherit_it(self) -> None:
        from custom_components.addhon import diagnostics

        _attrs, mapped_params, sources, _ = diagnostics._mapped_sets("OV")
        self.assertNotIn("powerManagement", mapped_params)
        self.assertNotIn("select.power_limit", sources)


if __name__ == "__main__":
    unittest.main()
