# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The options of the running cycle come from its activity (issue #138).

A washer's shadow never reports `permanentPressStatus` (Keep Fresh) or `stainType`
during a cycle: it keeps the 0 of rest. A cycle started with Keep Fresh on showed it on
for the 10 s the local write is shielded, then off from the next poll on. The official
app shows a running cycle's options from the cycle's activity, only while `machMode` is
2, 3 or 5 (`slot143`, apk2 decomp.txt @2588626-2588744; the running card,
@2591986-2592000). The decisions of 2026-10-26:

- D3: the engine keeps the start's options with the activity; they answer only for a
  cycle not started on the panel, with the activity's, the kept cycle's and the
  shadow's prCode equal, while `machMode` is in `cycle_modes`;
- D4: outside those modes (the end 7, the Keep Fresh phase after it) the shadow again;
- D5: only `permanentPressStatus` and `stainType`, the two the shadow does not report;
- C1: only while the context last read still has the kept activity open;
- C2: washers and washer-dryers only.

The activity carries the start's parameters next to what the app adds
(`principalUserId`, `deviceModel`, `prStrDisp`), as the dumps show; the schema is the
HW90-B14387TU1-S's (`stainType` 0..26, `permanentPressStatus` 0/1).
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_follow_started_elsewhere import _fixed  # noqa: E402
from test_running_cycle_name import _activity, _Harness, _parameter  # noqa: E402
from test_wash_option_params import _WM_HW90_FIXTURE  # noqa: E402

# getattr-guarded, as the other sensor test modules do: conftest stubs the descriptions
# and the classes, not the entity.
_sensor_platform = sys.modules["homeassistant.components.sensor"]
_sensor_platform.SensorEntity = getattr(
    _sensor_platform, "SensorEntity", type("SensorEntity", (), {})
)

from custom_components.addhon import select, sensor, switch  # noqa: E402
from custom_components.addhon.client import factory  # noqa: E402
from custom_components.addhon.client.engine.attributes import HonAttribute  # noqa: E402
from custom_components.addhon.const import (  # noqa: E402
    PROGRAM_OPTION_SOURCES,
    PROGRAM_PENDING_OPTIONS,
    PROGRAM_PENDING_STORE,
)
from custom_components.addhon.program_options import (  # noqa: E402
    CYCLE_OPTION_PARAMS,
    cycle_option_raw,
)

_HW90 = json.loads(_WM_HW90_FIXTURE.read_text(encoding="utf-8"))["startprogram_params"]


def _category(pr_code):
    parameters = json.loads(json.dumps(_HW90))
    parameters["prCode"] = _fixed(pr_code)
    return {"description": "d", "protocolType": "MQTT", "parameters": parameters}


_COMMANDS = {
    "applianceModel": {"options": {}},
    "startProgram": {
        "PROGRAMS.WM_WD.IOT_WASH_BED_LINEN": _category("115"),
        "PROGRAMS.WM_WD.HQD_ECO_40_60": _category("136"),
    },
    "dictionaryId": 1,
}


def _rest(mach_mode="1", **values):
    """The context of a washer on 115, with the shadow's 0 for both options."""
    parameters = {
        "prCode": "115", "machMode": mach_mode, "permanentPressStatus": "0", "stainType": "0",
        **values,
    }
    return {
        "shadow": {"parameters": {key: _parameter(value) for key, value in parameters.items()}},
        "activity": {},
        "commandHistory": None,
    }


def _start(channel="mobileApp", pr_code="115", **options):
    """The activity of a cycle started with Keep Fresh on and the sweat stain (19)."""
    values = {
        "permanentPressStatus": "1", "stainType": "19", "temp": "40", "spinSpeed": "1400",
        "principalUserId": "user-0", "deviceModel": "model-0", "prStrDisp": "Bed%20linen",
        **options,
    }
    return _activity("IOT_WASH_BED_LINEN", channel, pr_code=pr_code, **values)


class _CycleHarness(_Harness):
    COMMANDS = _COMMANDS

    def _washer(self, **values):
        return self._appliance([], _rest(**values))

    def _cycle(self, mach_mode="2", **activity):
        """A washer whose context was just read with the cycle's activity open."""
        app = self._washer()
        self._read(app, {"activity": _start(**activity)}, machMode=mach_mode)
        return app


class CycleOptionTest(_CycleHarness):
    """`HonAppliance.cycle_option`: what the running cycle's start sent, or None."""

    def test_an_app_cycle_gives_what_its_start_sent(self) -> None:
        for mode in ("2", "3", "5"):
            with self.subTest(machMode=mode):
                app = self._cycle(mode)
                self.assertEqual("1", app.cycle_option("permanentPressStatus"))
                self.assertEqual("19", app.cycle_option("stainType"))

    def test_numbers_in_the_activity_come_back_as_text(self) -> None:
        app = self._cycle(stainType=19, permanentPressStatus=1)
        self.assertEqual("19", app.cycle_option("stainType"))
        self.assertEqual("1", app.cycle_option("permanentPressStatus"))

    def test_a_panel_cycle_gives_nothing(self) -> None:
        # A panel start's activity is a picture of the shadow, not what was chosen.
        app = self._cycle(channel="appliance")
        self.assertIsNotNone(app.cycle_activity)
        self.assertIsNone(app.cycle_option("permanentPressStatus"))
        self.assertIsNone(app.cycle_option("stainType"))

    def test_an_activity_on_another_code_gives_nothing(self) -> None:
        # As the app's running card checks the activity's prCode (apk2 decomp.txt
        # @2591727-2591733).
        app = self._cycle(pr_code="116")
        self.assertEqual(116, app.cycle_activity.activity_code)
        self.assertIsNone(app.cycle_option("permanentPressStatus"))

    def test_outside_the_cycle_modes_gives_nothing(self) -> None:
        for mode in ("7", "1"):
            with self.subTest(machMode=mode):
                app = self._cycle()
                self._push(app, machMode=mode)
                self.assertIsNone(app.cycle_option("permanentPressStatus"))
                self.assertIsNone(app.cycle_option("stainType"))

    def test_a_closed_activity_gives_nothing(self) -> None:  # C1
        app = self._cycle()
        self._push(app, machMode="7")
        # The end: the activity empties, the shadow keeps 115, the cycle stays kept for
        # its name (S1 of issue #130).
        self.assertEqual("iot_wash_bed_linen", self._read(app, {"activity": {}}))
        self.assertIsNotNone(app.cycle_activity)
        # The next cycle on the same code, before the read that brings its activity.
        self._push(app, machMode="2")
        self.assertIsNone(app.cycle_option("permanentPressStatus"))
        self.assertIsNone(app.cycle_option("stainType"))

    def test_a_push_off_the_code_gives_nothing(self) -> None:
        app = self._cycle()
        self._push(app, prCode="136")
        self.assertIsNone(app.cycle_option("permanentPressStatus"))

    def test_only_start_parameters_are_kept(self) -> None:
        options = self._cycle().cycle_activity.options
        for key in ("principalUserId", "deviceModel", "prStrDisp", "channel", "origin",
                    "programName"):
            self.assertNotIn(key, options)
        self.assertEqual(
            {"prCode": "115", "permanentPressStatus": "1", "stainType": "19", "temp": "40",
             "spinSpeed": "1400"},
            dict(options),
        )
        with self.assertRaises(TypeError):
            options["stainType"] = "0"  # read by the push thread: never written in place

    def test_a_reload_mid_cycle_answers_after_its_first_read(self) -> None:
        self._cycle()
        app = factory._native_engine_appliance_cls()(
            self.cloud,
            {"applianceTypeName": "WM", "applianceModelId": 1, "macAddress": "aa"},
            zone=0,
        )
        self.loop.run_until_complete(app.load_commands())
        self.assertIsNone(app.cycle_option("permanentPressStatus"))
        self.loop.run_until_complete(app.load_attributes())
        self.assertEqual("1", app.cycle_option("permanentPressStatus"))


class _EntityHarness(_CycleHarness):
    """The option entities over a coordinator double fed by the engine's appliance."""

    def _coordinator(self, app, kind="WM"):
        coordinator = types.SimpleNamespace(data={}, hass=None)
        self._refresh(coordinator, app, kind)
        return coordinator

    @staticmethod
    def _refresh(coordinator, app, kind="WM"):
        """What a coordinator refresh hands the entities: the attributes flattened."""
        attributes = {**app.attributes, **app.attributes["parameters"], **app.settings}
        coordinator.data = {
            "wm1": {"type": kind, "appliance": app, "name": "Washer", "attributes": attributes,
                    "settings": dict(app.settings), "statistics": {}},
        }

    @staticmethod
    def _keep_fresh(coordinator):
        desc = next(d for d in switch._PROGRAM_OPTION_SWITCHES if d.key == "permanent_press")
        return switch.HonProgramOptionSwitch(coordinator, "wm1", desc, None)

    @staticmethod
    def _stain_select(coordinator):
        desc = next(d for d in select._PROGRAM_OPTION_SELECTS if d.key == "stain_type")
        return select.HonProgramOptionSelect(coordinator, "wm1", desc, None)

    @staticmethod
    def _stain_sensor(coordinator):
        desc = next(d for d in sensor._WASH_EXTRA if d.key == "stain_type")
        return sensor.HonSensor(coordinator, "wm1", desc)


class CycleOptionEntityTest(_EntityHarness):

    def test_a_buffered_or_prescribed_value_wins_over_the_cycle(self) -> None:
        # A choice made during the cycle is for the next start.
        coordinator = self._coordinator(self._cycle())
        setattr(coordinator, PROGRAM_PENDING_OPTIONS, {"wm1": {"permanentPressStatus": "0"}})
        self.assertFalse(self._keep_fresh(coordinator).is_on)
        coordinator = self._coordinator(self._cycle())
        setattr(coordinator, PROGRAM_PENDING_STORE, {"wm1": "iot_wash_bed_linen"})
        self.assertFalse(self._keep_fresh(coordinator).is_on)
        self.assertEqual("none", self._stain_select(coordinator).current_option)

    def test_current_raw_records_where_its_value_came_from(self) -> None:
        # Issue #138: the dump says which link of `_current_raw` gave the value shown.
        param = "permanentPressStatus"

        def source(coordinator, entity):
            entity._current_raw()
            return getattr(coordinator, PROGRAM_OPTION_SOURCES)["wm1"][param]

        coordinator = self._coordinator(self._cycle())
        entity = self._keep_fresh(coordinator)
        self.assertEqual("cycle", source(coordinator, entity))
        setattr(coordinator, PROGRAM_PENDING_OPTIONS, {"wm1": {param: "0"}})
        self.assertEqual("pending", source(coordinator, entity))
        coordinator = self._coordinator(self._cycle())
        setattr(coordinator, PROGRAM_PENDING_STORE, {"wm1": "iot_wash_bed_linen"})
        self.assertEqual("prescribed", source(coordinator, self._keep_fresh(coordinator)))
        coordinator = self._coordinator(self._washer())
        entity = self._keep_fresh(coordinator)
        self.assertEqual("shadow", source(coordinator, entity))
        with mock.patch.object(
            entity, "_get_attr", side_effect=lambda name: "1" if "." in name else None
        ):
            self.assertEqual("command", source(coordinator, entity))
        with mock.patch.object(entity, "_get_attr", return_value=None):
            self.assertEqual("none", source(coordinator, entity))
            self.assertIsNone(entity._current_raw())

    def test_a_store_that_cannot_be_written_does_not_stop_the_read(self) -> None:
        coordinator = self._coordinator(self._washer())
        entity = self._keep_fresh(coordinator)
        real = entity._coordinator_store

        def store(name):
            if name == PROGRAM_OPTION_SOURCES:
                raise RuntimeError("broken store")
            return real(name)

        with mock.patch.object(entity, "_coordinator_store", side_effect=store):
            self.assertEqual(0, entity._current_raw())

    def test_the_cycle_wins_over_the_shadow(self) -> None:
        app = self._cycle()
        coordinator = self._coordinator(app)
        self.assertEqual(0, app.attributes["parameters"]["permanentPressStatus"].value)
        self.assertTrue(self._keep_fresh(coordinator).is_on)
        self.assertEqual("sweat", self._stain_select(coordinator).current_option)

    def test_only_keep_fresh_and_the_stain_read_the_cycle(self) -> None:  # D5
        app = self._cycle(temp="60")
        entry = self._coordinator(app).data["wm1"]
        self.assertEqual("60", app.cycle_option("temp"))
        self.assertIsNone(cycle_option_raw(entry, "temp"))
        self.assertEqual(frozenset({"permanentPressStatus", "stainType"}), CYCLE_OPTION_PARAMS)

    def test_keep_fresh_stays_on_after_the_shield(self) -> None:
        # The reported case: started from Home Assistant with Keep Fresh on.
        app = self._washer()
        coordinator = self._coordinator(app)
        switch_entity = self._keep_fresh(coordinator)
        self.assertFalse(switch_entity.is_on)
        # The cloud accepted the start: the local write of what was sent, shielded.
        app.sync_payload_to_params({"prCode": "115", "permanentPressStatus": "1", "stainType": "19"})
        self._push(app, machMode="2")
        # The read the start asked for, inside the shield: the activity, the shadow's 0
        # refused by the lock.
        self._read(app, {"activity": _start()}, machMode="2")
        self._refresh(coordinator, app)
        self.assertTrue(switch_entity.is_on)
        # The shield expires and the next poll brings the shadow's 0.
        with mock.patch.object(HonAttribute, "_LOCK_TIMEOUT", -1):
            self._read(app)
        self._refresh(coordinator, app)
        self.assertEqual(0, app.attributes["parameters"]["permanentPressStatus"].value)
        self.assertTrue(switch_entity.is_on)
        # The end of the cycle: the shadow again (D4).
        self._push(app, machMode="7")
        self._refresh(coordinator, app)
        self.assertFalse(switch_entity.is_on)

    def test_an_unrenderable_cycle_value_falls_to_the_shadow(self) -> None:
        app = self._washer(stainType="1")
        self._read(app, {"activity": _start(stainType="NaN")}, machMode="2")
        coordinator = self._coordinator(app)
        self.assertEqual("NaN", app.cycle_option("stainType"))
        self.assertEqual("wine", self._stain_select(coordinator).current_option)

    def test_a_raising_reader_falls_to_the_shadow(self) -> None:
        app = self._washer(stainType="1")
        self._read(app, {"activity": _start(stainType="19")}, machMode="2")
        coordinator = self._coordinator(app)
        self.assertEqual("sweat", self._stain_select(coordinator).current_option)
        with mock.patch.object(type(app), "cycle_option", side_effect=RuntimeError("boom")):
            self.assertIsNone(cycle_option_raw(coordinator.data["wm1"], "stainType"))
            self.assertEqual("wine", self._stain_select(coordinator).current_option)

    def test_a_dryer_keep_fresh_reads_as_before(self) -> None:  # C2
        self.TYPE = "TD"
        app = self._cycle()
        coordinator = self._coordinator(app, kind="TD")
        self.assertEqual("1", app.cycle_option("permanentPressStatus"))
        self.assertIsNone(cycle_option_raw(coordinator.data["wm1"], "permanentPressStatus"))
        self.assertFalse(self._keep_fresh(coordinator).is_on)

    def test_the_stain_sensor_falls_to_the_shadow_on_an_unmapped_cycle_code(self) -> None:
        app = self._washer(stainType="0")
        self._read(app, {"activity": _start(stainType="99")}, machMode="2")
        coordinator = self._coordinator(app)
        self.assertEqual("99", app.cycle_option("stainType"))
        self.assertEqual("none", self._stain_sensor(coordinator).native_value)

    def test_the_stain_sensor_reads_the_cycle(self) -> None:
        app = self._cycle()
        coordinator = self._coordinator(app)
        stain = self._stain_sensor(coordinator)
        self.assertTrue(stain.entity_description.cycle_option)
        self.assertEqual("sweat", stain.native_value)
        self._push(app, machMode="7")
        self.assertEqual("none", stain.native_value)
        # The washer's other sensors read the shadow alone.
        self.assertFalse(any(
            d.cycle_option for d in sensor._WASH_EXTRA if d.key != "stain_type"
        ))


if __name__ == "__main__":
    unittest.main()
