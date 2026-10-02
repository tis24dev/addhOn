# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Keep Fresh phase as a read-only binary (#112).

The Keep Fresh switch is a setting for the next Start. The app shows the Keep Fresh
phase (dashboard title `PROGRAMS.WM_WD.ULTRA_FRESH`, apk2 decomp.txt:2612411-2612546 and
3983945-3983990) only while `freshAirStatus` is 1 AND the active phase is the end-of-cycle
tumbling, `prPhase` 20. A reporter's HQD washer sets `freshAirStatus` 1 at Start and never
clears it: in the dump of 2026-10-02 it is still 1 after the machine went idle
(`machMode` 1, `prPhase` 0, door open), so the flag alone cannot be the reading.

The active phase comes from `getAppliancePhaseFromParameters` (decomp.txt:1361041),
which answers without looking at `prPhase` when `machMode` is 0 or 1 (ready), 3 (pause),
5 (delayed start running) or 6 (error), and answers READY when `prPhase` is missing.
"""
from __future__ import annotations

import unittest

from tests.test_hpwh import _Coordinator, _Entry, _Hass, _install_package_stubs

_install_package_stubs()


def _shadow(**attributes) -> dict:
    return {
        "wm-1": {
            "type": "WM",
            "name": "HW90-B14387TU1-S",
            "attributes": dict(attributes),
            "settings": {},
            "appliance": None,
        }
    }


async def _keep_fresh(**attributes):
    from custom_components.addhon import binary_sensor
    from custom_components.addhon.const import DOMAIN

    coordinator = _Coordinator(_shadow(**attributes))
    hass = _Hass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": None}}})
    added: list = []
    await binary_sensor.async_setup_entry(hass, _Entry(), added.extend)
    return next((e for e in added if e._attr_unique_id == "wm-1_keep_fresh"), None)


class KeepFreshBinaryTest(unittest.TestCase):
    def _description(self, app_type: str):
        from custom_components.addhon.binary_sensor import BINARY_SENSORS

        return next(
            (d for d in BINARY_SENSORS.get(app_type, ()) if d.key == "keep_fresh"), None
        )

    def test_washers_read_fresh_air_status(self) -> None:
        for app_type in ("WM", "WD"):
            with self.subTest(app_type):
                description = self._description(app_type)
                self.assertIsNotNone(description)
                self.assertEqual("freshAirStatus", description.attr_key)

    def test_not_on_dryers(self) -> None:
        self.assertIsNone(self._description("TD"))


class KeepFreshPhaseTest(unittest.IsolatedAsyncioTestCase):
    async def test_on_during_the_end_of_cycle_tumbling(self) -> None:
        entity = await _keep_fresh(freshAirStatus="1", prPhase="20", machMode="7")
        self.assertIs(True, entity.is_on)

    async def test_numeric_readings_count_alike(self) -> None:
        entity = await _keep_fresh(freshAirStatus=1, prPhase=20, machMode=7)
        self.assertIs(True, entity.is_on)

    async def test_off_once_the_machine_is_idle_with_the_flag_left_at_one(self) -> None:
        # The reporter's shadow after the door was opened (dump of 2026-10-02).
        entity = await _keep_fresh(freshAirStatus="1", prPhase="0", machMode="1")
        self.assertIs(False, entity.is_on)

    async def test_off_while_washing(self) -> None:
        entity = await _keep_fresh(freshAirStatus="1", prPhase="2", machMode="2")
        self.assertIs(False, entity.is_on)

    async def test_off_without_keep_fresh(self) -> None:
        entity = await _keep_fresh(freshAirStatus="0", prPhase="20", machMode="7")
        self.assertIs(False, entity.is_on)

    async def test_off_in_the_modes_the_app_answers_before_the_phase(self) -> None:
        for mode in ("0", "1", "3", "5", "6"):
            with self.subTest(machMode=mode):
                entity = await _keep_fresh(freshAirStatus="1", prPhase="20", machMode=mode)
                self.assertIs(False, entity.is_on)

    async def test_on_in_the_modes_that_read_the_phase(self) -> None:
        for mode in ("2", "4", "7", "10"):
            with self.subTest(machMode=mode):
                entity = await _keep_fresh(freshAirStatus="1", prPhase="20", machMode=mode)
                self.assertIs(True, entity.is_on)

    async def test_without_mach_mode_the_phase_decides(self) -> None:
        entity = await _keep_fresh(freshAirStatus="1", prPhase="20")
        self.assertIs(True, entity.is_on)

    async def test_off_without_a_phase(self) -> None:
        entity = await _keep_fresh(freshAirStatus="1", machMode="7")
        self.assertIs(False, entity.is_on)

    async def test_unknown_without_the_flag(self) -> None:
        entity = await _keep_fresh(freshAirStatus="1", prPhase="20", machMode="7")
        del entity.coordinator.data["wm-1"]["attributes"]["freshAirStatus"]
        self.assertIsNone(entity.is_on)


if __name__ == "__main__":
    unittest.main()
