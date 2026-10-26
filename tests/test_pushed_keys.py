"""A parameter a push names before any read seeded it is created from the push.

The hOn app's shadow takes every parameter of an `appliancestatus` push, known or not
(`Object.assign({}, lastShadow.parameters, data)`, apk2 decomp.txt @3217930-3217960);
addhOn dropped a new name until the next poll (issue #138). The engine now creates it
with the pushed value and no cloud instant: `attributes_received` says when HA got it.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import install_stubs  # noqa: E402

install_stubs()

from custom_components.addhon.client import factory  # noqa: E402
from custom_components.addhon.client.engine import appliance as appliance_mod  # noqa: E402
from custom_components.addhon.client.engine.attributes import HonAttribute  # noqa: E402
from test_follow_started_elsewhere import CloudDouble, _fixed  # noqa: E402
from test_running_cycle_name import _WM_COMMANDS, _parameter, _shadow  # noqa: E402


class _PushMidReadCloud(CloudDouble):
    """A cloud whose context read is overtaken by a push naming a key it never seeded."""

    def __init__(self, commands, context, pushed: dict) -> None:
        super().__init__(commands, [], context)
        self.pushed = pushed
        self.appliance = None

    async def load_attributes(self, appliance):
        for name, data in self.pushed.items():
            appliance.adopt_pushed_parameter(name, data)
        return await super().load_attributes(appliance)


def _appliance(cloud):
    app = factory._native_engine_appliance_cls()(
        cloud, {"applianceTypeName": "WM", "applianceModelId": 1, "macAddress": "aa"}, zone=0
    )
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app.load_commands())
        loop.run_until_complete(app.load_attributes())
    finally:
        loop.close()
    return app


class AdoptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _appliance(CloudDouble(_WM_COMMANDS, [], _shadow("115", "1")))

    def test_the_key_is_held_with_the_pushed_value_and_no_cloud_instant(self) -> None:
        held = self.app.adopt_pushed_parameter(
            "loosenStatus", {"parName": "loosenStatus", "parOldVal": "0", "parNewVal": "1"}
        )
        self.assertIs(held, self.app.attributes["parameters"]["loosenStatus"])
        self.assertIsInstance(held, HonAttribute)
        self.assertEqual("1", str(held))
        self.assertIsNone(held.last_update)
        self.assertEqual("mqtt", held.received_via)
        self.assertTrue(appliance_mod._pushed_since(
            held, datetime.now(timezone.utc) - timedelta(seconds=1)
        ))

    def test_a_known_key_is_not_replaced(self) -> None:
        before = self.app.attributes["parameters"]["prCode"]
        held = self.app.adopt_pushed_parameter("prCode", {"parName": "prCode", "parNewVal": "9"})
        self.assertIs(before, held)
        self.assertEqual("115", str(held))

    def test_a_read_overtaken_by_the_push_keeps_the_pushed_value(self) -> None:
        context = _shadow("115", "1")
        context["shadow"]["parameters"]["loosenStatus"] = _parameter("0")
        cloud = _PushMidReadCloud(
            _WM_COMMANDS, context, {"loosenStatus": {"parName": "loosenStatus", "parNewVal": "1"}}
        )
        app = _appliance(cloud)
        self.assertEqual("1", str(app.attributes["parameters"]["loosenStatus"]))


if __name__ == "__main__":
    unittest.main()
