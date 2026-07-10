# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contract test for build_auth_headers (spec: HHT-sec4).

Oracle = the header contract authored in docs/protocol/HAIER-HON-TRANSPORT.md (sec4),
NOT a transcription of pyhOn. Every authenticated request carries user-agent +
Content-Type (values sourced from values.py) plus the two Haier-mandated token headers
`cognito-token` / `id-token`, with `extra` and the tokens overriding the base.

The header VALUES now live in values.py, so headers.py imports them and can no longer
be loaded in isolation; this test imports it through the package (HA stubbed).
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

from custom_components.addhon.client.transport.headers import (  # noqa: E402
    BASE_HEADERS,
    build_auth_headers,
)
from custom_components.addhon.client.transport.values import (  # noqa: E402
    CONTENT_TYPE,
    USER_AGENT,
)


class BuildAuthHeadersTest(unittest.TestCase):
    def test_pinned_contract(self) -> None:
        # sec4: base (user-agent + Content-Type) + the two tokens.
        self.assertEqual(
            build_auth_headers("C", "I"),
            {
                "user-agent": USER_AGENT,
                "Content-Type": CONTENT_TYPE,
                "cognito-token": "C",
                "id-token": "I",
            },
        )

    def test_base_values_come_from_single_source(self) -> None:
        # The header values have ONE home (values.py); headers.py must not re-inline.
        self.assertEqual(BASE_HEADERS["user-agent"], USER_AGENT)
        self.assertEqual(BASE_HEADERS["Content-Type"], CONTENT_TYPE)

    def test_extra_overrides_base_but_tokens_win(self) -> None:
        # sec4.2 merge order: extra overrides base; the two tokens override everything.
        h = build_auth_headers("REAL_COG", "REAL_ID", {
            "user-agent": "OVERRIDE/1.0",           # extra beats the base UA
            "cognito-token": "fake", "id-token": "fake",  # tokens still win
            "x-extra": "1",
        })
        self.assertEqual(h["user-agent"], "OVERRIDE/1.0")
        self.assertEqual(h["x-extra"], "1")
        self.assertEqual(h["cognito-token"], "REAL_COG")
        self.assertEqual(h["id-token"], "REAL_ID")

    def test_empty_extra_and_none(self) -> None:
        self.assertEqual(build_auth_headers("", "", {}), build_auth_headers("", "", None))


if __name__ == "__main__":
    unittest.main()
