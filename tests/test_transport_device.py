# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contract test of the native device descriptor: `client/transport/device.HonDevice`.

Pins the identity payload sent to the hOn cloud (appVersion/mobileId/os/osVersion/
deviceModel) and the `mobile=True` -> `mobileOs` rename (spec: HHT-sec3), so the
wire SHAPE does not drift. The identity VALUES live in values.py with their
provenance, so the expected payload is built from that single source rather than
re-hardcoded here; device.py now imports them and can no longer be loaded in
isolation, so this test imports it through the package (HA stubbed), copying the
pattern in test_transport_headers.py.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _install_stubs() -> None:
    def _mod(name: str) -> types.ModuleType:
        m = sys.modules.get(name)
        if m is None:
            m = types.ModuleType(name)
            sys.modules[name] = m
        return m

    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    core.ServiceCall = getattr(core, "ServiceCall", type("ServiceCall", (), {}))
    core.callback = getattr(core, "callback", lambda f: f)
    exc = _mod("homeassistant.exceptions")
    base = getattr(exc, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exc.HomeAssistantError = base
    exc.ConfigEntryNotReady = getattr(exc, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base,), {}))
    exc.ConfigEntryAuthFailed = getattr(exc, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base,), {}))
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))
    ha = _mod("homeassistant")
    ha.config_entries, ha.core, ha.exceptions = ce, core, exc
    ha.helpers = _mod("homeassistant.helpers")
    ha.helpers.update_coordinator = uc


_install_stubs()

from custom_components.addhon.client.transport.device import HonDevice  # noqa: E402
from custom_components.addhon.client.transport.values import (  # noqa: E402
    APP_VERSION,
    DEVICE_MODEL,
    MOBILE_ID,
    OS,
    OS_VERSION,
)

# Expected wire SHAPE, filled from the single provenance-tracked source (values.py)
# so the pin tracks the contract, not a re-hardcoded copy of the version numbers.
_DEFAULT = {
    "appVersion": APP_VERSION,
    "mobileId": MOBILE_ID,
    "os": OS,
    "osVersion": OS_VERSION,
    "deviceModel": DEVICE_MODEL,
}
_DEFAULT_MOBILE = {
    "appVersion": APP_VERSION,
    "mobileId": MOBILE_ID,
    "osVersion": OS_VERSION,
    "deviceModel": DEVICE_MODEL,
    "mobileOs": OS,
}
_CUSTOM = {**_DEFAULT, "mobileId": "ABC123"}
_CUSTOM_MOBILE = {**_DEFAULT_MOBILE, "mobileId": "ABC123"}


class TransportDeviceTest(unittest.TestCase):
    def test_payload_matches_frozen_contract(self) -> None:
        self.assertEqual(HonDevice().payload(False), _DEFAULT)
        self.assertEqual(HonDevice().payload(True), _DEFAULT_MOBILE)
        self.assertEqual(HonDevice("ABC123").payload(False), _CUSTOM)
        self.assertEqual(HonDevice("ABC123").payload(True), _CUSTOM_MOBILE)

    def test_mobile_renames_os(self) -> None:
        # sec3: the mobile calls rename `os` -> `mobileOs` (same value, key change).
        mobile = HonDevice().payload(True)
        self.assertNotIn("os", mobile)
        self.assertEqual(mobile["mobileOs"], OS)

    def test_empty_mobile_id_falls_back_to_default(self) -> None:
        self.assertEqual(HonDevice("").mobile_id, MOBILE_ID)


if __name__ == "__main__":
    unittest.main()
