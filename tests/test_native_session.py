# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline test of the native `Hon` orchestration (NativeHon, Phase 3 piece 3).

Verifies the setup SEQUENCE faithful to pyhOn `Hon.setup` (load_appliances ->
per appliance load_commands/attributes/statistics -> MQTT last), zone handling,
the empty-mac skip, per-appliance error tolerance, MQTT gating, close, and
conformance to the `HonSession` Protocol.

The pyhOn engine (HonAppliance) and MQTT are mocked via the `factory`
factories (the only bridge to `_vendor`): no `_vendor` import, no network, no
awscrt. aiohttp/yarl/homeassistant are stubbed.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _mod(name: str) -> types.ModuleType:
    m = sys.modules.get(name)
    if m is None:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return m


def _install_stubs() -> None:
    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
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
    yarl = _mod("yarl")
    if not hasattr(yarl, "URL"):
        yarl.URL = type("URL", (), {"__init__": lambda self, s, encoded=False: None})
    aio = _mod("aiohttp")
    aio.ClientSession = getattr(aio, "ClientSession", type("ClientSession", (), {}))
    aio.ClientResponse = getattr(aio, "ClientResponse", type("ClientResponse", (), {}))
    aio.ContentTypeError = getattr(aio, "ContentTypeError", type("ContentTypeError", (Exception,), {}))


_install_stubs()

from custom_components.addhon import error_codes as ec  # noqa: E402
from custom_components.addhon.client import factory  # noqa: E402
from custom_components.addhon.client import session as session_mod  # noqa: E402
from custom_components.addhon.client.session import NativeHon  # noqa: E402
from custom_components.addhon.client.interfaces import HonSession  # noqa: E402
from custom_components.addhon.client.transport.auth import NativeAuthError  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class FakeAppliance:
    def __init__(self, api, data, zone, events, fail=False) -> None:
        self._api = api
        self._data = data
        self.zone = zone
        self.events = events
        self.fail = fail
        self.mac_address = data.get("macAddress", "")
        self.appliance_type = data.get("applianceTypeName", "?")

    async def load_commands(self) -> None:
        self.events.append(f"cmd:{self.mac_address}:{self.zone}")
        if self.fail:
            raise KeyError("boom")

    async def load_attributes(self) -> None:
        self.events.append(f"attr:{self.mac_address}:{self.zone}")

    async def load_statistics(self) -> None:
        self.events.append(f"stat:{self.mac_address}:{self.zone}")


class FakeApi:
    def __init__(self, appliances, events) -> None:
        self._appliances = appliances
        self.events = events
        self.closed = False

    async def load_appliances(self):
        self.events.append("load_appliances")
        return [dict(a) for a in self._appliances]

    async def close(self):
        self.closed = True


class FakeMqtt:
    def __init__(self, harness) -> None:
        self._harness = harness
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True
        self._harness.stop_calls.append(self)


class AuthTracePropagationTest(unittest.TestCase):
    def test_factory_forwards_trace_to_native_session(self) -> None:
        from custom_components.addhon.client.auth_diagnostics import (
            AuthDiagnosticTrace,
        )

        trace = AuthDiagnosticTrace(enabled=True)
        captured = {}
        original = session_mod.NativeHon

        def fake_native_hon(*args, **kwargs):
            captured.update(kwargs)
            return object()

        session_mod.NativeHon = fake_native_hon
        self.addCleanup(setattr, session_mod, "NativeHon", original)
        factory.create_session("u@x", "p", auth_trace=trace)
        self.assertIs(captured["auth_trace"], trace)

    def test_native_session_forwards_trace_to_connection(self) -> None:
        from custom_components.addhon.client.auth_diagnostics import (
            AuthDiagnosticTrace,
        )

        trace = AuthDiagnosticTrace(enabled=True)
        captured = {}
        original_connection = session_mod.HonConnection
        original_api = session_mod.HonApi

        class FakeConnection:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

            async def create(self):
                return self

            async def close(self):
                pass

        class FakeApiForTrace:
            def __init__(self, connection):
                self.auth = types.SimpleNamespace()

            async def load_appliances(self):
                return []

            async def close(self):
                pass

        session_mod.HonConnection = FakeConnection
        session_mod.HonApi = FakeApiForTrace
        self.addCleanup(setattr, session_mod, "HonConnection", original_connection)
        self.addCleanup(setattr, session_mod, "HonApi", original_api)

        hon = NativeHon(
            email="u@x",
            password="p",
            enable_mqtt=False,
            minimal=True,
            auth_trace=trace,
        )
        _run(hon.create())
        self.assertIs(captured["auth_trace"], trace)
        _run(hon.close())


class _Harness:
    """Patches create_appliance (factory) + NativeHon._make_mqtt + HonConnection/HonApi."""

    def __init__(self, test, appliances, fail_macs=()):
        self.test = test
        self.events: list = []
        self.appliances_data = appliances
        self.fail_macs = set(fail_macs)
        self.api = FakeApi(appliances, self.events)
        self.mqtt_calls: list = []
        self.stop_calls: list = []
        self.mqtt_instance = None

    def install(self):
        h = self  # harness (avoids collision with self=NativeHon in the patched methods)
        t = self.test
        events = self.events

        def fake_create_appliance(api, data, zone=0):
            return FakeAppliance(api, data, zone, events, fail=data.get("macAddress") in h.fail_macs)

        async def fake_make_mqtt(hon):  # hon = NativeHon instance (bound method)
            events.append("mqtt")
            m = FakeMqtt(h)
            h.mqtt_calls.append((hon, hon._mobile_id))
            h.mqtt_instance = m
            return m

        t._patch(factory, "create_appliance", fake_create_appliance)
        t._patch(NativeHon, "_make_mqtt", fake_make_mqtt)


class NativeSessionSetupTest(unittest.TestCase):
    def _patch(self, obj, name, value):
        real = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, real))

    def _nh_with_api(self, harness, **kw):
        nh = NativeHon("u@x", "p", **kw)
        nh._api = harness.api  # bypass connection creation, test setup()
        return nh

    def test_setup_loads_each_appliance_then_mqtt_last(self) -> None:
        data = [
            {"macAddress": "A", "applianceTypeName": "REF"},
            {"macAddress": "B", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        # 2 appliances built + loaded, mqtt LAST
        self.assertEqual([a.mac_address for a in nh.appliances], ["A", "B"])
        self.assertEqual(h.events[0], "load_appliances")
        self.assertEqual(h.events[-1], "mqtt")
        # for each appliance: cmd -> attr, and all BEFORE mqtt. load_statistics is NOT
        # part of setup any more (#76): the first coordinator refresh redoes it before
        # any platform is forwarded, so loading it here only spent 2 extra sequential
        # round-trips per appliance inside the budget we were overflowing.
        self.assertEqual(
            h.events,
            ["load_appliances",
             "cmd:A:0", "attr:A:0",
             "cmd:B:0", "attr:B:0",
             "mqtt"],
        )

    def test_setup_never_calls_load_statistics(self) -> None:
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual([e for e in h.events if e.startswith("stat:")], [])

    def test_zone_appliance_split(self) -> None:
        data = [{"macAddress": "Z", "applianceTypeName": "AC", "zone": "2"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        # zones=2 -> zone1, zone2, then base(zone0) = 3 appliances (like pyhOn)
        self.assertEqual([a.zone for a in nh.appliances], [1, 2, 0])

    def test_zero_appliances_still_creates_mqtt(self) -> None:
        # 0 appliances: load_appliances (=1 authenticated POST that populates the tokens)
        # still happens, then MQTT still starts -> auth ready even without appliances.
        h = _Harness(self, [])
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual(nh.appliances, [])
        self.assertEqual(h.events, ["load_appliances", "mqtt"])
        self.assertEqual(len(h.mqtt_calls), 1)

    def test_mqtt_created_once_across_two_setups(self) -> None:
        # the `not self._mqtt_client` gate prevents a second MQTT creation.
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        _run(nh.setup())
        self.assertEqual(h.events.count("mqtt"), 1)
        self.assertEqual(len(h.mqtt_calls), 1)

    def test_setup_twice_does_not_duplicate_appliances(self) -> None:
        # Finding 9: a mid-setup MFA challenge makes submit_mfa_code() resume by calling
        # setup() again. setup() must clear the (possibly partial) inventory first, or the
        # appliances loaded before the challenge get appended a second time -> duplicates.
        data = [
            {"macAddress": "A", "applianceTypeName": "REF"},
            {"macAddress": "B", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        first = nh.appliances  # the list object; must be cleared in place, not rebound
        _run(nh.setup())
        _run(nh.setup())
        # No duplicates across the two setups.
        self.assertEqual([a.mac_address for a in nh.appliances], ["A", "B"])
        # Cleared IN PLACE (same object) -- the MQTT client holds this list by reference.
        self.assertIs(nh.appliances, first)

    def test_mixed_zoned_and_normal_ordering(self) -> None:
        # a multi-zone appliance followed by a normal one: zone1,zone2,base(0) then normal(0),
        # all loaded BEFORE mqtt (which is last).
        data = [
            {"macAddress": "Z", "applianceTypeName": "AC", "zone": "2"},
            {"macAddress": "N", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual(
            [(a.mac_address, a.zone) for a in nh.appliances],
            [("Z", 1), ("Z", 2), ("Z", 0), ("N", 0)],
        )
        self.assertEqual(h.events[0], "load_appliances")
        self.assertEqual(h.events[-1], "mqtt")
        # no load after mqtt
        self.assertEqual(h.events.index("mqtt"), len(h.events) - 1)

    def test_an_induction_hob_is_never_split(self) -> None:
        # #84.2: the hob's `zone` counts COOKING ZONES of one appliance, not
        # appliances. Five Home Assistant devices carrying byte-identical
        # attributes is what the reporting user saw.
        data = [{"macAddress": "H", "applianceTypeName": "IH", "zone": "4"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual([a.zone for a in nh.appliances], [0])
        self.assertEqual([a.mac_address for a in nh.appliances], ["H"])

    def test_the_hob_alias_is_guarded_too(self) -> None:
        data = [{"macAddress": "H", "applianceTypeName": "HOB", "zone": "4"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual([a.zone for a in nh.appliances], [0])

    def test_the_surviving_hob_keeps_the_identity_it_already_had(self) -> None:
        """What makes this a removal of duplicates and not a rename.

        The base object was already built with zone=0 before this change, and
        `_check_name_zone` returns the bare attribute for zone 0. So the id of the
        device that survives is byte-for-byte the id it already had, and only the
        four clone ids disappear -- which is why the migration is a purge of
        orphans rather than a re-registration of everything the user owns.

        Asserted on the REAL engine class: the session fixture's appliance double
        does not carry an identity, and a double cannot demonstrate this.
        """
        from custom_components.addhon.client.engine.appliance import HonAppliance

        info = {
            "macAddress": "11-22-33-44-55-66",
            "applianceTypeName": "IH",
            "applianceModelId": "42",
        }
        base = HonAppliance(None, dict(info), zone=0)
        clone = HonAppliance(None, dict(info), zone=1)
        self.assertEqual("11-22-33-44-55-66", base.unique_id)
        self.assertEqual("11-22-33-44-55-66_z1", clone.unique_id)

    def test_another_type_with_two_zones_still_expands(self) -> None:
        # THE non-regression test. `session.setup` is on the setup path of every
        # appliance of every account: a guard that leaked would make devices
        # vanish for users who have nothing to do with a hob. An oven is the
        # concrete candidate -- the app's own demo appliance list carries `zone`
        # on an OV -- and nothing has ever proved its clones are duplicates.
        data = [{"macAddress": "O", "applianceTypeName": "OV", "zone": "2"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual([a.zone for a in nh.appliances], [1, 2, 0])

    def test_an_entry_without_a_type_still_expands(self) -> None:
        # `str(None)` is "None", not a member of the set: an entry the cloud
        # sends without a type keeps the behaviour it had.
        data = [{"macAddress": "U", "zone": "2"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual([a.zone for a in nh.appliances], [1, 2, 0])

    def test_the_guard_is_case_insensitive(self) -> None:
        # The type is echoed from the cloud list verbatim; nothing normalises it
        # before this point.
        data = [{"macAddress": "H", "applianceTypeName": "ih", "zone": "3"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual([a.zone for a in nh.appliances], [0])

    def test_a_hob_entry_with_a_broken_zone_is_still_dropped(self) -> None:
        # The guard acts on the expansion branch only: the zone is still parsed,
        # and an unparseable one is still counted and skipped as before.
        data = [{"macAddress": "H", "applianceTypeName": "IH", "zone": "many"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual(nh.appliances, [])
        self.assertEqual(nh.setup_drops.get("bad_zone"), 1)

    def test_a_hob_census_reconciles_without_the_clones(self) -> None:
        data = [{"macAddress": "H", "applianceTypeName": "IH", "zone": "4"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual(nh.setup_expanded, 1)
        self.assertEqual(
            len(nh.appliances) + sum(nh.setup_drops.values()), nh.setup_expanded
        )

    def test_the_local_type_set_matches_the_integration_constants(self) -> None:
        """The client package cannot import from the integration above it, so the
        two type codes are duplicated in session.py. This is what stops the copies
        from drifting apart -- a rename on one side fails here, loudly, instead of
        quietly turning the guard off."""
        from custom_components.addhon.client.session import ZONE_IS_NOT_A_DEVICE
        from custom_components.addhon.const import APPLIANCE_HOB, APPLIANCE_IH

        self.assertEqual({APPLIANCE_IH, APPLIANCE_HOB}, set(ZONE_IS_NOT_A_DEVICE))

    def test_zone_one_not_split(self) -> None:
        data = [{"macAddress": "Z", "applianceTypeName": "AC", "zone": "1"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual([a.zone for a in nh.appliances], [0])

    def test_empty_mac_skipped(self) -> None:
        data = [
            {"macAddress": "", "applianceTypeName": "GHOST"},
            {"macAddress": "B", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual([a.mac_address for a in nh.appliances], ["B"])
        # the appliance without a mac is not loaded
        self.assertNotIn("cmd::0", h.events)

    def test_appliance_load_error_still_appended(self) -> None:
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data, fail_macs={"A"})
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        # load_commands raises KeyError but the appliance stays (partial state, like pyhOn)
        self.assertEqual([a.mac_address for a in nh.appliances], ["A"])

    def test_appliance_load_error_redacts_identity_in_log(self) -> None:
        # #19/CR#2: the malformed-appliance path logs at ERROR (never gated by the
        # debug toggles). It logs STRUCTURE ONLY -- the exception type + the top-level
        # field NAMES -- never a VALUE, so no macAddress/serialNumber/modelName value
        # (nor anything else) can reach home-assistant.log. The full redacted dict is
        # in Download Diagnostics (redacted at a different layer).
        data = [{
            "macAddress": "AA:BB:CC:DD:EE:FF",
            "serialNumber": "SN-SECRET-123",
            "applianceTypeName": "REF",
            "modelName": "HDPW5620CNPK",
        }]
        h = _Harness(self, data, fail_macs={"AA:BB:CC:DD:EE:FF"})
        h.install()
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR") as cm:
            _run(nh.setup())
        blob = "\n".join(cm.output)
        # NO value leaks (identity or otherwise)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", blob)
        self.assertNotIn("SN-SECRET-123", blob)
        self.assertNotIn("HDPW5620CNPK", blob)
        # structure IS present: field NAMES (not values) + the error type + the code
        self.assertIn("macAddress", blob)   # the field NAME, not its value
        self.assertIn("KeyError", blob)     # the exception TYPE name
        self.assertIn(session_mod.APPLIANCE_DATA_MALFORMED.label, blob)
        self.assertEqual([a.mac_address for a in nh.appliances], ["AA:BB:CC:DD:EE:FF"])

    def test_malformed_log_does_not_leak_nested_identity(self) -> None:
        # CR#2 / Refuter-2: redact_identity is recursive and masks two ways -- a VALUE
        # whose KEY name is identity-shaped (at any depth), AND any MAC embedded in a
        # string leaf (regex, even under a benign key). The residual gap is a NON-MAC
        # identity (e.g. a serial) carried as a VALUE under a non-identity key: a nested
        # attributes[].parValue (the real hOn shape) or a benign top-level key like
        # modelName -- no identity key name, no MAC pattern -> it survives redaction.
        # The malformed-appliance log therefore logs STRUCTURE only (field names + error
        # type), never values, so even that residual cannot leak.
        # zone is valid (numeric) so the LOAD path runs (fail_macs -> KeyError),
        # carrying the raw appliance_data (with nested + benign-key identity) into
        # _log_malformed -- the exact pre-existing path Refuter-2 flagged.
        data = [{
            "macAddress": "AA:BB:CC:DD:EE:FF",
            "applianceTypeName": "REF",
            "zone": "0",
            "attributes": [
                {"parName": "serialNumber", "parValue": "SN-NESTED-SECRET"},
            ],
            "modelName": "IDENTITY-UNDER-BENIGN-KEY",
        }]
        h = _Harness(self, data, fail_macs={"AA:BB:CC:DD:EE:FF"})
        h.install()
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR") as cm:
            _run(nh.setup())
        blob = "\n".join(cm.output)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", blob)          # top-level identity value
        self.assertNotIn("SN-NESTED-SECRET", blob)           # nested attributes[].parValue
        self.assertNotIn("IDENTITY-UNDER-BENIGN-KEY", blob)  # value under a benign key
        self.assertIn(session_mod.APPLIANCE_DATA_MALFORMED.label, blob)

    # --- CR#2: setup-path per-appliance isolation -----------------------------
    # A single malformed appliance must be logged-and-skipped (redacted) without
    # aborting setup of the OTHER appliances. The fault boundary now spans the WHOLE
    # per-appliance build: non-dict element + zone parse + constructor + load_* trio.

    def test_non_dict_element_skipped_others_load(self) -> None:
        # parse_appliance_list gives no per-element dict guarantee. A bare-string
        # element (here MAC-shaped) must be logged-and-skipped -- it would otherwise
        # raise AttributeError on appliance.get(...) and abort the whole loop -- and
        # ONLY its type is logged, never the raw value (redact_identity passes a bare
        # scalar through, so a MAC-string element would leak if echoed).
        good = {"macAddress": "B", "applianceTypeName": "WM"}
        h = _Harness(self, [good])
        h.install()

        async def mixed_load_appliances():
            h.events.append("load_appliances")
            return ["AA:BB:CC:DD:EE:FF", dict(good)]

        h.api.load_appliances = mixed_load_appliances
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR") as cm:
            _run(nh.setup())
        self.assertEqual([a.mac_address for a in nh.appliances], ["B"])
        self.assertEqual(h.events[-1], "mqtt")
        blob = "\n".join(cm.output)
        self.assertIn(session_mod.APPLIANCE_DATA_MALFORMED.label, blob)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", blob)  # raw scalar never echoed
        self.assertIn("type=str", blob)  # only its type is logged

    def test_unparseable_zone_skips_only_that_appliance(self) -> None:
        # int(appliance.get("zone","0")) raises ValueError on a non-numeric zone;
        # only THAT appliance is skipped, the next one loads and setup completes.
        data = [
            {"macAddress": "BAD", "applianceTypeName": "AC", "zone": "not-a-number"},
            {"macAddress": "OK", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR") as cm:
            _run(nh.setup())
        self.assertEqual([a.mac_address for a in nh.appliances], ["OK"])
        self.assertNotIn("cmd:BAD:0", h.events)
        self.assertIn("cmd:OK:0", h.events)
        self.assertEqual(h.events[-1], "mqtt")
        self.assertIn(session_mod.APPLIANCE_DATA_MALFORMED.label, "\n".join(cm.output))

    def test_constructor_failure_skips_only_that_appliance(self) -> None:
        # factory.create_appliance (HonAppliance.__init__) raises TypeError on a
        # malformed info["attributes"] -- this ran BEFORE the per-device try and
        # aborted ALL. Now the bad device is skipped (no usable object) and the good
        # one loads.
        bad = {"macAddress": "BAD", "applianceTypeName": "REF"}
        good = {"macAddress": "OK", "applianceTypeName": "WM"}
        h = _Harness(self, [bad, good])

        def fake_create_appliance(api, data, zone=0):
            if data.get("macAddress") == "BAD":
                raise TypeError("malformed attributes in constructor")
            return FakeAppliance(api, data, zone, h.events)

        async def fake_make_mqtt(hon):
            h.events.append("mqtt")
            m = FakeMqtt(h)
            h.mqtt_calls.append((hon, hon._mobile_id))
            return m

        self._patch(factory, "create_appliance", fake_create_appliance)
        self._patch(NativeHon, "_make_mqtt", fake_make_mqtt)
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR") as cm:
            _run(nh.setup())
        self.assertEqual([a.mac_address for a in nh.appliances], ["OK"])
        self.assertEqual(h.events[-1], "mqtt")
        self.assertIn(session_mod.APPLIANCE_DATA_MALFORMED.label, "\n".join(cm.output))

    def test_load_attributes_attributeerror_keeps_partial_others_load(self) -> None:
        # load_attributes() raises AttributeError (a non-dict "shadow") -- previously
        # OUTSIDE the (KeyError, ValueError, IndexError) catch, so it aborted the loop.
        # Now caught: the failing appliance is kept (partial state) AND the next one
        # still loads fully.
        class AttrFailAppliance(FakeAppliance):
            async def load_attributes(self) -> None:
                self.events.append(f"attr:{self.mac_address}:{self.zone}")
                raise AttributeError("'list' object has no attribute 'get'")

        bad = {"macAddress": "BAD", "applianceTypeName": "REF"}
        good = {"macAddress": "OK", "applianceTypeName": "WM"}
        h = _Harness(self, [bad, good])

        def fake_create_appliance(api, data, zone=0):
            cls = AttrFailAppliance if data.get("macAddress") == "BAD" else FakeAppliance
            return cls(api, data, zone, h.events)

        async def fake_make_mqtt(hon):
            h.events.append("mqtt")
            return FakeMqtt(h)

        self._patch(factory, "create_appliance", fake_create_appliance)
        self._patch(NativeHon, "_make_mqtt", fake_make_mqtt)
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR") as cm:
            _run(nh.setup())
        self.assertEqual([a.mac_address for a in nh.appliances], ["BAD", "OK"])
        self.assertIn("cmd:OK:0", h.events)
        self.assertEqual(h.events[-1], "mqtt")
        self.assertIn(session_mod.APPLIANCE_DATA_MALFORMED.label, "\n".join(cm.output))

    def test_setup_does_not_swallow_cancelled_error(self) -> None:
        # The broadened catch is (KeyError, ValueError, IndexError, TypeError,
        # AttributeError) -- NOT BaseException -- so an asyncio.CancelledError raised
        # during a per-appliance load must PROPAGATE (cooperative cancellation), not
        # be mistaken for malformed data and swallowed.
        class CancelAppliance(FakeAppliance):
            async def load_commands(self) -> None:
                raise asyncio.CancelledError()

        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)

        def fake_create_appliance(api, data, zone=0):
            return CancelAppliance(api, data, zone, h.events)

        async def fake_make_mqtt(hon):
            h.events.append("mqtt")
            return FakeMqtt(h)

        self._patch(factory, "create_appliance", fake_create_appliance)
        self._patch(NativeHon, "_make_mqtt", fake_make_mqtt)
        nh = self._nh_with_api(h)
        with self.assertRaises(asyncio.CancelledError):
            _run(nh.setup())

    def test_transport_error_on_one_appliance_keeps_the_others(self) -> None:
        # #76 cause 4. asyncio.TimeoutError derives from OSError, not from any of the
        # five _APPLIANCE_BUILD_ERRORS, so it used to escape _create_appliance, unwind
        # setup() and tear the whole config entry down. One slow device must now be
        # contained: it is kept with partial data and the others still load.
        class SlowAppliance(FakeAppliance):
            async def load_commands(self) -> None:
                self.events.append(f"cmd:{self.mac_address}:{self.zone}")
                raise asyncio.TimeoutError()

        bad = {"macAddress": "BAD", "applianceTypeName": "REF"}
        good = {"macAddress": "OK", "applianceTypeName": "WM"}
        h = _Harness(self, [bad, good])

        def fake_create_appliance(api, data, zone=0):
            cls = SlowAppliance if data.get("macAddress") == "BAD" else FakeAppliance
            return cls(api, data, zone, h.events)

        async def fake_make_mqtt(hon):
            h.events.append("mqtt")
            return FakeMqtt(h)

        self._patch(factory, "create_appliance", fake_create_appliance)
        self._patch(NativeHon, "_make_mqtt", fake_make_mqtt)
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="WARNING") as cm:
            _run(nh.setup())
        self.assertEqual([a.mac_address for a in nh.appliances], ["BAD", "OK"])
        self.assertIn("cmd:OK:0", h.events)
        self.assertEqual(h.events[-1], "mqtt")
        self.assertEqual(["BAD#0"], list(nh.degraded_appliances))
        # The WARNING is not gated by the debug toggles -> it lands in
        # home-assistant.log and must be leak-proof: code label + count, no mac.
        blob = "\n".join(cm.output)
        self.assertNotIn("BAD", blob)

    def test_auth_error_during_hydration_still_aborts_setup(self) -> None:
        # Containing a transport fault must NOT swallow a credentials rejection: it has
        # to reach _raise_setup_error and open the reauth flow.
        class RejectedAppliance(FakeAppliance):
            async def load_commands(self) -> None:
                raise NativeAuthError("api_auth: status 401")

        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)

        def fake_create_appliance(api, data, zone=0):
            return RejectedAppliance(api, data, zone, h.events)

        self._patch(factory, "create_appliance", fake_create_appliance)
        self._patch(NativeHon, "_make_mqtt", lambda hon: None)
        nh = self._nh_with_api(h)
        with self.assertRaises(NativeAuthError):
            _run(nh.setup())

    def test_all_appliances_failing_hydration_raises_the_real_cause(self) -> None:
        # Containing ONE appliance is a degradation; containing ALL of them would be a
        # masked failure that ships an empty integration. And it must carry the REAL
        # cause: a generic ADDHON-220 with no __cause__ threw away the only code that
        # tells the user (and Download Diagnostics) what actually happened -- the loss
        # CR#6 had already fixed on the poll path.
        class SlowAppliance(FakeAppliance):
            async def load_commands(self) -> None:
                raise asyncio.TimeoutError()

        data = [
            {"macAddress": "A", "applianceTypeName": "REF"},
            {"macAddress": "B", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)

        def fake_create_appliance(api, data, zone=0):
            return SlowAppliance(api, data, zone, h.events)

        self._patch(factory, "create_appliance", fake_create_appliance)
        self._patch(NativeHon, "_make_mqtt", lambda hon: None)
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="WARNING"):
            with self.assertRaises(session_mod.HonCodedError) as ctx:
                _run(nh.setup())
        self.assertIs(ec.NETWORK_TIMEOUT, ctx.exception.error_code)
        # The chain reaches the original exception, not a synthetic replacement.
        chain, cause = [], ctx.exception.__cause__
        while cause is not None and len(chain) < 5:
            chain.append(cause)
            cause = cause.__cause__
        self.assertTrue(
            any(isinstance(link, asyncio.TimeoutError) for link in chain),
            f"the real cause is gone: {chain}",
        )
        # Never identity, not even in the "which appliances" message.
        self.assertNotIn("A", str(ctx.exception).replace("ADDHON", ""))
        # And it says WHERE. `HonClient.setup_sync` prefers the phase the error carries
        # over the flat auth mirror, and `__init__.py` reads it straight off this
        # exception -- drop the keyword and the one report that could have named the
        # step shows a null phase instead.
        self.assertEqual("load_appliance", ctx.exception.phase)

    def test_a_mixed_inventory_of_partial_appliances_still_fails(self) -> None:
        # The under-count the guard used to have: one appliance MALFORMED (counted
        # nowhere) plus one killed by a transport fault read as "1 failure out of 2"
        # and shipped an entry whose every device was unusable.
        class Broken(FakeAppliance):
            async def load_commands(self) -> None:
                if self.mac_address == "A":
                    raise KeyError("malformed payload")
                raise asyncio.TimeoutError()

        data = [
            {"macAddress": "A", "applianceTypeName": "REF"},
            {"macAddress": "B", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)

        def fake_create_appliance(api, data, zone=0):
            return Broken(api, data, zone, h.events)

        self._patch(factory, "create_appliance", fake_create_appliance)
        self._patch(NativeHon, "_make_mqtt", lambda hon: None)
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="WARNING"):
            with self.assertRaises(session_mod.HonCodedError):
                _run(nh.setup())

    def test_an_only_malformed_inventory_still_ships_degraded(self) -> None:
        # The other side of the same rule: raising means "Home Assistant, retry". A
        # payload the parser cannot read will parse the same way next time, so failing
        # forever would leave the user with LESS than the degraded entry that ships
        # today (attributes still work, only the commands are missing).
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data, fail_macs={"A"})
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual(1, len(nh.appliances))
        self.assertFalse(nh.needs_rehydration(nh.appliances[0]))

    def test_a_transport_degraded_appliance_is_queued_for_rehydration(self) -> None:
        # Containing the fault is only legitimate because the first poll re-runs
        # load_commands before any entity is created (hon_client).
        class SlowAppliance(FakeAppliance):
            async def load_commands(self) -> None:
                if self.mac_address == "B":
                    raise asyncio.TimeoutError()
                await super().load_commands()

        data = [
            {"macAddress": "A", "applianceTypeName": "REF"},
            {"macAddress": "B", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)

        async def no_mqtt(hon):
            return None

        def fake_create_appliance(api, data, zone=0):
            return SlowAppliance(api, data, zone, h.events)

        self._patch(factory, "create_appliance", fake_create_appliance)
        self._patch(NativeHon, "_make_mqtt", no_mqtt)
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="WARNING"):
            _run(nh.setup())
        by_mac = {app.mac_address: app for app in nh.appliances}
        self.assertTrue(nh.needs_rehydration(by_mac["B"]))
        self.assertFalse(nh.needs_rehydration(by_mac["A"]))

    def test_setup_exposes_a_phase_ledger(self) -> None:
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        phases = [entry["phase"] for entry in nh.phase_ledger]
        self.assertIn("load_appliances", phases)
        self.assertIn("load_appliance", phases)
        self.assertTrue(all(entry["outcome"] == "ok" for entry in nh.phase_ledger))
        # Cleared after a clean setup: the hierarchical mirror follows the flat one.
        self.assertEqual("", nh.current_phase)
        self.assertNotIn("@", nh.phase_summary)

    def test_log_malformed_tolerates_unorderable_keys(self) -> None:
        # _log_malformed runs INSIDE the except handlers, so it must NEVER raise: a
        # raise there would escape and abort the whole setup loop -- the very failure
        # CR#2 fixes. sorted() on a dict with mixed-type top-level keys raises
        # TypeError, so the helper str()s the keys first. (Not reachable from JSON
        # cloud data -- keys are always str -- but the fault boundary must hold by
        # construction.) Drive a malformed appliance whose dict has an int key.
        good = {"macAddress": "OK", "applianceTypeName": "WM"}
        bad = {"macAddress": "BAD", 1: "x", "applianceTypeName": "REF"}
        h = _Harness(self, [bad, good], fail_macs={"BAD"})
        h.install()
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR"):
            _run(nh.setup())  # must NOT raise TypeError from sorted()
        # the helper did not abort the loop -> the good appliance still loaded + mqtt
        self.assertIn("OK", [a.mac_address for a in nh.appliances])
        self.assertEqual(h.events[-1], "mqtt")

    def test_multizone_one_zone_failure_isolated(self) -> None:
        # A multi-zone appliance whose ONE zone fails to BUILD must lose only that
        # zone, not its sibling zones or the next appliance (each per-zone
        # _create_appliance is independently isolated).
        zoned = {"macAddress": "Z", "applianceTypeName": "AC", "zone": "2"}
        nxt = {"macAddress": "N", "applianceTypeName": "WM"}
        h = _Harness(self, [zoned, nxt])

        def fake_create_appliance(api, data, zone=0):
            if data.get("macAddress") == "Z" and zone == 1:
                raise TypeError("zone-1 constructor boom")
            return FakeAppliance(api, data, zone, h.events)

        async def fake_make_mqtt(hon):
            h.events.append("mqtt")
            return FakeMqtt(h)

        self._patch(factory, "create_appliance", fake_create_appliance)
        self._patch(NativeHon, "_make_mqtt", fake_make_mqtt)
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR"):
            _run(nh.setup())
        # zone1 dropped (constructor failed, no object); zone2 + base Z(0) + N survive
        self.assertEqual(
            [(a.mac_address, a.zone) for a in nh.appliances],
            [("Z", 2), ("Z", 0), ("N", 0)],
        )
        self.assertEqual(h.events[-1], "mqtt")

    def test_mqtt_disabled(self) -> None:
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h, enable_mqtt=False)
        _run(nh.setup())
        self.assertNotIn("mqtt", h.events)
        self.assertEqual(h.mqtt_calls, [])
        self.assertIsNone(nh._mqtt_client)

    def test_unexpected_mqtt_start_failure_propagates(self) -> None:
        h = _Harness(self, [])
        h.install()

        async def mqtt_boom(_hon):
            raise TypeError("builder contract regression")

        self._patch(NativeHon, "_make_mqtt", mqtt_boom)
        nh = self._nh_with_api(h, enable_mqtt=True)
        with self.assertRaisesRegex(TypeError, "builder contract regression"):
            _run(nh.setup())

        self.assertIsNone(nh._mqtt_client)

    def test_mqtt_start_cancellation_still_propagates(self) -> None:
        h = _Harness(self, [])
        h.install()

        async def mqtt_cancelled(_hon):
            raise asyncio.CancelledError()

        self._patch(NativeHon, "_make_mqtt", mqtt_cancelled)
        nh = self._nh_with_api(h, enable_mqtt=True)
        with self.assertRaises(asyncio.CancelledError):
            _run(nh.setup())
        self.assertIsNone(nh._mqtt_client)

    def test_minimal_skips_per_appliance_loads_and_mqtt(self) -> None:
        # #30: config-flow validation builds + counts the appliances but does NOT run
        # the per-appliance load_commands/attributes/statistics, and never starts MQTT.
        data = [
            {"macAddress": "A", "applianceTypeName": "REF"},
            {"macAddress": "B", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h, enable_mqtt=False, minimal=True)
        _run(nh.setup())
        # appliances are built (so the flow can count + type them) ...
        self.assertEqual([a.mac_address for a in nh.appliances], ["A", "B"])
        # ... but only load_appliances ran: no per-appliance cmd/attr/stat, no mqtt.
        self.assertEqual(h.events, ["load_appliances"])
        self.assertEqual(h.mqtt_calls, [])
        self.assertIsNone(nh._mqtt_client)
        self.assertEqual(nh._setup_phase, "")  # cleared after a clean setup

    def test_minimal_empty_mac_still_skipped(self) -> None:
        data = [
            {"macAddress": "", "applianceTypeName": "GHOST"},
            {"macAddress": "B", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h, enable_mqtt=False, minimal=True)
        _run(nh.setup())
        self.assertEqual([a.mac_address for a in nh.appliances], ["B"])

    def test_create_builds_connection_api_then_setup(self) -> None:
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)
        h.install()

        created = {}

        class FakeConn:
            def __init__(self, *a, **kw):
                created["args"] = (a, kw)

            async def create(self):
                created["conn_created"] = True
                return self

            async def close(self):
                created["conn_closed"] = True

        def fake_honapi(conn):
            created["api_conn"] = conn
            return h.api

        self._patch(session_mod, "HonConnection", FakeConn)
        self._patch(session_mod, "HonApi", fake_honapi)

        nh = NativeHon("u@x", "p", mobile_id="MID", enable_mqtt=False)
        out = _run(nh.create())
        self.assertIs(out, nh)
        self.assertTrue(created["conn_created"])
        self.assertIsInstance(created["api_conn"], FakeConn)
        self.assertEqual([a.mac_address for a in nh.appliances], ["A"])
        self.assertFalse(h.api.closed)  # success path must NOT close

    def _patch_conn_api_with_failing_setup(self, h):
        # HonConnection.create() succeeds, HonApi is the harness api, but setup()
        # fails (load_appliances raises) so create() must self-clean.
        class FakeConn:
            async def create(self):
                return self

            async def close(self):
                pass

        async def boom():
            raise RuntimeError("setup boom")

        h.api.load_appliances = boom
        self._patch(session_mod, "HonConnection", lambda *a, **k: FakeConn())
        self._patch(session_mod, "HonApi", lambda conn: h.api)

    def test_create_failure_closes_session_no_leak(self) -> None:
        # #31: if setup() raises, create() must close() so the owned ClientSession
        # (via api.close() -> connection.close()) is released, not leaked.
        h = _Harness(self, [])
        h.install()
        self._patch_conn_api_with_failing_setup(h)
        nh = NativeHon("u@x", "p", enable_mqtt=False)
        with self.assertRaises(RuntimeError):
            _run(nh.create())
        self.assertTrue(h.api.closed)  # close() ran on the failed create()

    def test_async_with_create_failure_still_closes(self) -> None:
        # The documented hazard: `async with NativeHon(...)` does NOT run __aexit__
        # when __aenter__/create() raises, so create() itself must clean up.
        h = _Harness(self, [])
        h.install()
        self._patch_conn_api_with_failing_setup(h)

        async def body():
            async with NativeHon("u@x", "p", enable_mqtt=False):
                pass

        with self.assertRaises(RuntimeError):
            _run(body())
        self.assertTrue(h.api.closed)

    def test_create_baseexception_in_setup_still_closes(self) -> None:
        # #31: the guard is `except BaseException` ON PURPOSE so a CANCELLED setup
        # (asyncio.CancelledError is a BaseException, NOT an Exception) also tears
        # down the owned session. `except Exception` would let it leak -> kill that
        # mutant. setup() raises CancelledError here (via load_appliances).
        h = _Harness(self, [])
        h.install()

        class FakeConn:
            async def create(self):
                return self

            async def close(self):
                pass

        async def cancel_boom():
            raise asyncio.CancelledError()

        h.api.load_appliances = cancel_boom
        self._patch(session_mod, "HonConnection", lambda *a, **k: FakeConn())
        self._patch(session_mod, "HonApi", lambda conn: h.api)

        nh = NativeHon("u@x", "p", enable_mqtt=False)
        with self.assertRaises(asyncio.CancelledError):
            _run(nh.create())
        self.assertTrue(h.api.closed)  # close() ran even on a BaseException

    def test_create_failure_in_connection_create_closes_with_no_api(self) -> None:
        # #31: failure point BEFORE _api is set (HonConnection.create() raises).
        # create()'s except still runs close(), which must tolerate _api is None
        # (no AttributeError) AND must close the partially-built connection so its
        # owned ClientSession is not leaked. Exercises the `_api is None` guard in
        # close() on the create() error path + connection cleanup.
        class FakeConn:
            async def create(self):
                raise RuntimeError("connection create boom")

            async def close(self):
                pass

        api_built = {"n": 0}

        def fake_honapi(conn):
            api_built["n"] += 1
            return object()

        self._patch(session_mod, "HonConnection", lambda *a, **k: FakeConn())
        self._patch(session_mod, "HonApi", fake_honapi)

        nh = NativeHon("u@x", "p", enable_mqtt=False)
        with self.assertRaises(RuntimeError):
            _run(nh.create())  # must NOT raise AttributeError from close()
        self.assertEqual(api_built["n"], 0)  # failed before _api was built
        self.assertIsNone(nh._api)

    def test_create_failure_after_mqtt_started_stops_mqtt_and_closes_api(self) -> None:
        # #31: deeper failure point. load_appliances succeeds, MQTT is built, then
        # something AFTER that raises. close() must stop the started MQTT (no leak)
        # AND close the api. Tests the cleanup path with a live _mqtt_client.
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)
        h.install()

        class FakeConn:
            async def create(self):
                return self

            async def close(self):
                pass

        self._patch(session_mod, "HonConnection", lambda *a, **k: FakeConn())
        self._patch(session_mod, "HonApi", lambda conn: h.api)

        nh = NativeHon("u@x", "p", enable_mqtt=True)

        real_setup = nh.setup

        async def setup_then_boom():
            await real_setup()  # builds appliances + starts MQTT
            assert nh._mqtt_client is h.mqtt_instance
            raise RuntimeError("post-mqtt boom")

        nh.setup = setup_then_boom  # type: ignore[assignment]

        with self.assertRaises(RuntimeError):
            _run(nh.create())
        self.assertTrue(h.mqtt_instance.stopped)  # started MQTT was stopped
        self.assertIsNone(nh._mqtt_client)
        self.assertTrue(h.api.closed)  # api closed too

    def test_create_failure_cleanup_error_does_not_mask_original(self) -> None:
        # #31 (refuter): if close()'s teardown itself raises, it must NOT mask the
        # ORIGINAL setup exception (the config-entry classifier keys off it, e.g. an
        # auth error must surface as ConfigEntryAuthFailed, not be hidden by a
        # cleanup ConnectionResetError). close() is exception-guarded -> original wins.
        h = _Harness(self, [])
        h.install()

        class FakeConn:
            async def create(self):
                return self

            async def close(self):
                pass

        async def setup_boom():
            raise ValueError("ORIGINAL setup error")

        async def close_boom():
            raise RuntimeError("cleanup boom")

        h.api.load_appliances = setup_boom
        h.api.close = close_boom
        self._patch(session_mod, "HonConnection", lambda *a, **k: FakeConn())
        self._patch(session_mod, "HonApi", lambda conn: h.api)

        nh = NativeHon("u@x", "p", enable_mqtt=False)
        with self.assertRaises(ValueError) as ctx:
            _run(nh.create())
        self.assertIn("ORIGINAL", str(ctx.exception))  # cleanup error did not mask it

    def test_close_closes_api(self) -> None:
        h = _Harness(self, [])
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.close())
        self.assertTrue(h.api.closed)

    def test_close_stops_mqtt_then_api(self) -> None:
        # close() stops the MQTT (no leak) and then closes the api; _mqtt_client cleared.
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertIs(nh._mqtt_client, h.mqtt_instance)
        _run(nh.close())
        self.assertEqual(h.stop_calls, [h.mqtt_instance])
        self.assertTrue(h.mqtt_instance.stopped)
        self.assertIsNone(nh._mqtt_client)
        self.assertTrue(h.api.closed)

    def test_close_without_mqtt_no_stop(self) -> None:
        # enable_mqtt=False: no mqtt -> close() does not stop any client.
        h = _Harness(self, [])
        h.install()
        nh = self._nh_with_api(h, enable_mqtt=False)
        _run(nh.setup())
        _run(nh.close())
        self.assertEqual(h.stop_calls, [])
        self.assertTrue(h.api.closed)

    def test_api_property_raises_before_create(self) -> None:
        nh = NativeHon("u@x", "p")
        with self.assertRaises(NativeAuthError):
            _ = nh.api

    def test_subscribe_and_notify(self) -> None:
        nh = NativeHon("u@x", "p")
        got = []
        nh.subscribe_updates(lambda payload: got.append(payload))
        nh.notify()
        self.assertEqual(got, [None])

    def test_notify_noop_without_subscriber(self) -> None:
        nh = NativeHon("u@x", "p")
        nh.notify()  # must not raise

    def test_satisfies_hon_session_protocol(self) -> None:
        nh = NativeHon("u@x", "p")
        self.assertIsInstance(nh, HonSession)
        # the members that the MQTTClient/integration read (via dir: the `api`
        # property raises if evaluated before create(), which is the right behavior)
        for member in ("api", "appliances", "subscribe_updates", "notify", "close"):
            self.assertIn(member, dir(nh))
        # appliances is readable right away (empty list), api is not (not created)
        self.assertEqual(nh.appliances, [])


class SetupDropCensusTest(unittest.TestCase):
    """`setup_drops` / `setup_expanded`: the appliances the cloud sent and we did not.

    The class of failure this pins is the one no log can describe: an account whose
    inventory arrives and is thrown away renders in the diagnostics dump, and in
    home-assistant.log, exactly like an account that owns nothing -- and for the empty
    mac (session.py:293-302) that is literal, since that branch writes no log line at
    all. The census is the only artefact that can separate them, so its arithmetic has
    to be pinned rather than assumed:

        len(appliances) + sum(setup_drops.values()) == setup_expanded

    Every test below is a term of that sum. `test_a_load_failure_is_NOT_counted_as_a_
    drop` is the one that keeps it honest from the other side: the second
    `except self._APPLIANCE_BUILD_ERRORS` KEEPS its appliance, and counting it would
    put the same object on both sides of the equation.

    The last two tests belong to `degraded_census`, the census of what that same
    branch KEEPS. It is here rather than in a class of its own because the two
    populations are defined by contrast -- an appliance is in exactly one of them,
    and `test_a_load_failure_is_NOT_counted_as_a_drop` is the hinge between the two.
    """

    def _patch(self, obj, name, value):
        real = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(lambda: setattr(obj, name, real))

    def _nh_with_api(self, harness, **kw):
        nh = NativeHon("u@x", "p", **kw)
        nh._api = harness.api  # bypass connection creation, drive setup() directly
        return nh

    def test_a_silent_mac_drop_is_counted(self) -> None:
        # The root cause the whole census exists for: the appliance arrived, was built,
        # and was dropped for an empty mac WITHOUT a log line. assertNoLogs is part of
        # the assertion, not tidiness -- it states the premise ("the log cannot answer
        # this") that makes the counter worth its lines.
        data = [{"macAddress": "", "applianceTypeName": "GHOST"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        with self.assertNoLogs(session_mod._LOGGER, level="DEBUG"):
            _run(nh.setup())
        self.assertEqual([], nh.appliances)
        self.assertEqual({"mac_empty": 1}, nh.setup_drops)
        self.assertEqual(1, nh.setup_expanded)

    def test_a_non_dict_element_is_counted(self) -> None:
        # Dropped in setup() BEFORE _create_appliance is reached, so this is also the
        # test that the expansion counter is charged for an entry that never expands:
        # without that, an inventory of schema-drift entries would report expanded == 0
        # with a non-empty census and the reconciliation would fail on healthy data.
        good = {"macAddress": "B", "applianceTypeName": "WM"}
        h = _Harness(self, [good])
        h.install()

        async def mixed_load_appliances():
            h.events.append("load_appliances")
            return ["AA:BB:CC:DD:EE:FF", dict(good)]

        h.api.load_appliances = mixed_load_appliances
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR"):
            _run(nh.setup())
        self.assertEqual({"not_a_dict": 1}, nh.setup_drops)
        self.assertEqual(2, nh.setup_expanded)
        self.assertEqual(["B"], [a.mac_address for a in nh.appliances])

    def test_an_unparseable_zone_is_counted(self) -> None:
        data = [{"macAddress": "BAD", "applianceTypeName": "AC", "zone": "abc"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR"):
            _run(nh.setup())
        self.assertEqual({"bad_zone": 1}, nh.setup_drops)
        self.assertEqual(1, nh.setup_expanded)
        self.assertEqual([], nh.appliances)

    def test_a_construction_error_is_counted(self) -> None:
        # factory.create_appliance raising leaves no usable object, so this branch
        # returns without appending: it is a DROP and the census must say so.
        data = [{"macAddress": "BAD", "applianceTypeName": "REF"}]
        h = _Harness(self, data)

        def raising_create_appliance(api, data, zone=0):
            raise KeyError("malformed attributes in constructor")

        async def fake_make_mqtt(hon):
            h.events.append("mqtt")
            return FakeMqtt(h)

        self._patch(factory, "create_appliance", raising_create_appliance)
        self._patch(NativeHon, "_make_mqtt", fake_make_mqtt)
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR"):
            _run(nh.setup())
        self.assertEqual({"construction_error": 1}, nh.setup_drops)
        self.assertEqual(1, nh.setup_expanded)
        self.assertEqual([], nh.appliances)

    def test_a_load_failure_is_NOT_counted_as_a_drop(self) -> None:
        # The trap this class exists to disarm. There are TWO
        # `except self._APPLIANCE_BUILD_ERRORS` branches over the SAME tuple: the first
        # (construction) drops, the second (load) KEEPS the appliance and falls through
        # to the append. Counting the second would put one object in both `built` and
        # `skipped`, and the invariant would then be broken by the most ordinary event
        # in this file -- an appliance whose commands failed to load, which ships every
        # day as a degraded entry. Without this test that breakage is silent.
        data = [{"macAddress": "A", "applianceTypeName": "REF"}]
        h = _Harness(self, data, fail_macs={"A"})
        h.install()
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR"):
            _run(nh.setup())
        self.assertEqual({}, nh.setup_drops)
        self.assertEqual(1, len(nh.appliances))
        self.assertEqual(1, nh.setup_expanded)
        # It is degraded, not absent: the OTHER census (C3) is the one that owns it.
        self.assertEqual(1, len(nh.degraded_appliances))

    def test_two_appliances_dropped_leave_an_empty_inventory_and_a_full_census(
        self,
    ) -> None:
        # State (D) of the design's decision table: the cloud sent an inventory and we
        # threw all of it away. Today this dump is byte-identical to an empty account;
        # the three numbers below are what tell them apart.
        data = [
            {"macAddress": "", "applianceTypeName": "REF"},
            {"macAddress": "", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual([], nh.appliances)
        self.assertEqual({"mac_empty": 2}, nh.setup_drops)
        self.assertEqual(2, nh.setup_expanded)

    def test_a_zoned_entry_expands_the_census_not_the_count(self) -> None:
        # Blocker B3: ONE cloud entry with zone=2 becomes THREE appliance objects
        # (zone 1, zone 2, base), so a census counted on the length of the cloud list
        # could never reconcile -- `built > count` is a HEALTHY reading for a
        # multi-zone fridge, not a bug. `expanded` is the term that makes the sum work.
        data = [{"macAddress": "Z", "applianceTypeName": "AC", "zone": "2"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual(3, len(nh.appliances))
        self.assertEqual(3, nh.setup_expanded)
        self.assertEqual({}, nh.setup_drops)
        # One cloud entry, three objects: the gap the raw count cannot explain.
        self.assertEqual(1, len(data))

    def test_a_dropped_zoned_entry_is_counted_per_object(self) -> None:
        # The same expansion on the failing side: the drop is charged once per OBJECT,
        # never once per entry, or the sum stops closing exactly when the account is
        # both zoned and broken.
        data = [{"macAddress": "", "applianceTypeName": "AC", "zone": "2"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual([], nh.appliances)
        self.assertEqual({"mac_empty": 3}, nh.setup_drops)
        self.assertEqual(3, nh.setup_expanded)

    def test_the_census_invariant_holds(self) -> None:
        # The whole point, on a table that mixes every branch at once: healthy, zoned,
        # silently dropped, schema drift, unreadable zone. This is the arithmetic a
        # maintainer performs on the dump to decide whether the census accounts for
        # everything the cloud sent, so it is asserted rather than left to the reader.
        good = {"macAddress": "A", "applianceTypeName": "REF"}
        zoned = {"macAddress": "Z", "applianceTypeName": "AC", "zone": "2"}
        headless = {"macAddress": "", "applianceTypeName": "GHOST"}
        unreadable = {"macAddress": "B", "applianceTypeName": "WM", "zone": "abc"}
        entries = [dict(good), dict(zoned), dict(headless), "x", dict(unreadable)]
        h = _Harness(self, [good])
        h.install()

        async def mixed_load_appliances():
            h.events.append("load_appliances")
            return [dict(e) if isinstance(e, dict) else e for e in entries]

        h.api.load_appliances = mixed_load_appliances
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR"):
            _run(nh.setup())
        self.assertEqual(
            nh.setup_expanded, len(nh.appliances) + sum(nh.setup_drops.values())
        )
        # Stated again as literals, so a change that keeps the sum balanced by moving
        # an appliance from one side to the other still fails here.
        self.assertEqual(7, nh.setup_expanded)
        self.assertEqual(4, len(nh.appliances))
        self.assertEqual(
            {"mac_empty": 1, "not_a_dict": 1, "bad_zone": 1}, nh.setup_drops
        )
        # The second half of the declared invariant: one object per cloud entry at
        # minimum, more only where an entry was expanded into zones.
        self.assertGreaterEqual(nh.setup_expanded, len(entries))

    def test_setup_drops_are_cleared_on_a_new_setup(self) -> None:
        # setup() is re-entered on the SAME session when a mid-setup MFA challenge is
        # resumed, so a census that survived would describe two setups at once and the
        # invariant would fail against a single inventory. Rebound, not cleared in
        # place: a diagnostics download reads this map from another thread.
        data = [{"macAddress": "", "applianceTypeName": "GHOST"}]
        h = _Harness(self, data)
        h.install()
        nh = self._nh_with_api(h)
        _run(nh.setup())
        self.assertEqual({"mac_empty": 1}, nh.setup_drops)
        # The PRIVATE object, not `nh.setup_drops`. The property returns
        # `dict(self._setup_drops)` -- already a detached copy -- so an assertion on
        # what it returned survives a `.clear()` just as happily as a rebind and
        # cannot distinguish the two. This is the only assertion in the change that
        # claims to pin the rebind, so it has to hold the object the writer touches.
        before = nh._setup_drops
        data[:] = [{"macAddress": "A", "applianceTypeName": "REF"}]
        _run(nh.setup())
        self.assertEqual({}, nh.setup_drops)
        self.assertEqual(1, nh.setup_expanded)
        self.assertEqual(1, len(nh.appliances))
        # The reset REBOUND rather than mutating: a reader on Home Assistant's loop
        # that took the mapping mid-download still holds a complete census of the
        # setup it was reading, not an emptied one.
        self.assertIsNot(before, nh._setup_drops)
        self.assertEqual({"mac_empty": 1}, before)

    def test_count_drop_rebinds_instead_of_mutating(self) -> None:
        # The other half of the same discipline, and the half no other test reaches:
        # `_count_drop` is called once per dropped appliance while a Download
        # Diagnostics may already hold the mapping. A `self._setup_drops[reason] += 1`
        # would pass every behavioural test in this class -- the counts are identical
        # -- and would silently change what a concurrent reader sees.
        nh = NativeHon("u@x", "p")
        held = nh._setup_drops
        nh._count_drop("mac_empty")
        self.assertIsNot(held, nh._setup_drops)
        self.assertEqual({}, held)
        self.assertEqual({"mac_empty": 1}, nh.setup_drops)
        # And again on a non-empty census, because a rebind that only happened on the
        # first drop would still lose the property from the second one on.
        held = nh._setup_drops
        nh._count_drop("mac_empty")
        self.assertIsNot(held, nh._setup_drops)
        self.assertEqual({"mac_empty": 1}, held)
        self.assertEqual({"mac_empty": 2}, nh.setup_drops)

    def test_setup_drop_reasons_are_exhaustive(self) -> None:
        # A fifth way out of the loop must arrive with a token, or the dump will drop
        # its count on the floor: diagnostics.py iterates SETUP_DROP_REASONS instead of
        # the mapping it receives, precisely so a key it does not know cannot reach a
        # public issue. That safety has a cost -- an uncounted reason is invisible --
        # and this test is what makes the cost visible at the moment it is incurred.
        observed: set = set()
        for entries, patch_factory in (
            ([{"macAddress": "", "applianceTypeName": "GHOST"}], False),
            (["not-a-dict"], False),
            ([{"macAddress": "B", "applianceTypeName": "WM", "zone": "abc"}], False),
            ([{"macAddress": "C", "applianceTypeName": "REF"}], True),
        ):
            h = _Harness(self, [])
            h.install()
            if patch_factory:
                def raising_create_appliance(api, data, zone=0):
                    raise KeyError("constructor boom")

                self._patch(factory, "create_appliance", raising_create_appliance)

            async def load_appliances(captured=entries):
                return [dict(e) if isinstance(e, dict) else e for e in captured]

            h.api.load_appliances = load_appliances
            nh = self._nh_with_api(h)
            _run(nh.setup())
            observed |= set(nh.setup_drops)
        self.assertEqual(set(session_mod.SETUP_DROP_REASONS), observed)

    def test_degraded_census_counts_labels_never_macs(self) -> None:
        # The commoner half of the report this whole block exists for: nothing was
        # dropped, both appliances shipped, and both shipped WITHOUT commands -- so
        # the user sees two devices and none of their select/number/switch/button/
        # climate/fan entities (session.py:378-380). The raw bookkeeping that knows
        # this is keyed by identity, so the two assertions below are one statement:
        # the session holds "AA:BB:CC:DD:EE:FF#0" and publishes "ADDHON-230".
        data = [
            {"macAddress": "AA:BB:CC:DD:EE:FF", "applianceTypeName": "REF"},
            {"macAddress": "11:22:33:44:55:66", "applianceTypeName": "WM"},
        ]
        h = _Harness(self, data, fail_macs={a["macAddress"] for a in data})
        h.install()
        nh = self._nh_with_api(h)
        with self.assertLogs(session_mod._LOGGER, level="ERROR"):
            _run(nh.setup())
        # Kept, not dropped: the other census must stay empty or the same appliance
        # would be counted as both absent and present.
        self.assertEqual(2, len(nh.appliances))
        self.assertEqual({}, nh.setup_drops)
        self.assertEqual({"ADDHON-230": 2}, nh.degraded_census)
        # Two identities collapse into ONE row, which is what makes the reduction a
        # reduction rather than a rename of the mapping beside it.
        self.assertEqual(
            ["AA:BB:CC:DD:EE:FF#0", "11:22:33:44:55:66#0"],
            list(nh.degraded_appliances),
        )
        # The leak test, on the writer side, where section 3.5 of the design puts
        # it: the census is what crosses into the dump, and a dump is pasted into a
        # public issue. "#" is asserted as well as the two MACs because a key that
        # leaked in a form we did not anticipate would still carry the separator.
        blob = json.dumps(nh.degraded_census)
        self.assertNotIn("AA:BB:CC:DD:EE:FF", blob)
        self.assertNotIn("11:22:33:44:55:66", blob)
        self.assertNotIn("#", blob)

    def test_the_census_separates_the_two_causes_it_is_fed(self) -> None:
        # Beyond the design's list, and the reason is that the test above cannot
        # tell "groups by label" apart from "emits the one label this branch always
        # produces": with a single cause in play, a census that ignored `code`
        # entirely would pass it. The two branches that call `_record_partial` pass
        # DIFFERENT codes -- APPLIANCE_DATA_MALFORMED at session.py:332 and
        # `classify(error)` at :344 -- and a maintainer reading the dump acts on
        # which one it is: ADDHON-230 is a payload this build cannot parse and will
        # parse the same way after a reload, ADDHON-400 is a network timeout that a
        # reload may well clear.
        good = {"macAddress": "A", "applianceTypeName": "REF"}
        malformed = {"macAddress": "B", "applianceTypeName": "WM"}
        timed_out = {"macAddress": "C", "applianceTypeName": "AC"}
        h = _Harness(self, [good, malformed, timed_out], fail_macs={"B"})
        h.install()
        built = factory.create_appliance  # the harness fake, already installed

        def create_with_a_timeout(api, data, zone=0):
            appliance = built(api, data, zone=zone)
            if data.get("macAddress") == "C":
                async def load_commands():
                    raise TimeoutError("hOn did not answer in time")

                appliance.load_commands = load_commands
            return appliance

        self._patch(factory, "create_appliance", create_with_a_timeout)
        nh = self._nh_with_api(h)
        # WARNING, not ERROR: the transport branch logs at WARNING and the malformed
        # one at ERROR, and both have to be captured or the second escapes to the
        # root logger and prints through the run.
        with self.assertLogs(session_mod._LOGGER, level="WARNING"):
            _run(nh.setup())
        self.assertEqual(3, len(nh.appliances))
        # ADDHON-400 rather than the 460 a bare `classify(TimeoutError())` returns:
        # `budgeted` converts every TimeoutError that leaves its scope into a coded
        # error attributed to the innermost phase (client/budget.py:227-229), and
        # inside "load_appliance" that resolves to NETWORK_TIMEOUT instead of the
        # mute "setup timed out". The census reports what the session recorded and
        # re-derives nothing, which is what keeps it and `last_error` from naming one
        # failure two ways.
        self.assertEqual({"ADDHON-230": 1, "ADDHON-400": 1}, nh.degraded_census)
        # One healthy appliance is in neither census: `degraded` counts what is
        # broken, never what was loaded.
        self.assertEqual({}, nh.setup_drops)
        self.assertEqual(3, nh.setup_expanded)


class DegradedCensusThreadSafetyTest(unittest.TestCase):
    """`degraded_census` is read from a DIFFERENT thread than the one that fills it.

    `_record_partial` mutates `_hydration_failures` IN PLACE (session.py) on the
    dedicated hOn loop thread, while a Download Diagnostics reads this property on
    Home Assistant's event loop -- during setup, and during the runtime re-auth that
    re-runs setup in an executor with the config entry still loaded.

    The property loops in PYTHON over the values, and a Python-level `for` gives up
    the GIL between items, so without the `dict()` copy it raises `RuntimeError:
    dictionary changed size during iteration`. `_last_fetch` catches that and renders
    the whole block `{"state": "unreadable"}` -- the block the download was for.

    This is the ONLY guard in the census whose failure mechanism reproduces on demand:
    the sibling copy in `setup_drops` is a single C-level `dict()` call that never
    yields, measured at 0 failures in 50_000 reads with the same writer, which is why
    it is pinned by an identity assertion (`test_count_drop_rebinds_instead_of_
    mutating`) rather than by a race. The one defended by a measurement was the one
    tested by nothing, and this closes that.
    """

    def _hammer(self, reader, *, keys: int, reads: int):
        """Run `reader` against a writer thread churning `_hydration_failures`.

        Returns (escaped exception or None, results). The switch interval is dropped
        so the interpreter preempts inside a Python-level loop rather than once every
        few milliseconds; it is restored unconditionally.
        """
        nh = NativeHon("u@x", "p")
        code = ec.APPLIANCE_DATA_MALFORMED
        for i in range(keys):
            nh._hydration_failures[f"AA:BB:CC:DD:EE:{i:02X}#0"] = code
        stop = threading.Event()
        failures: list = []

        def writer() -> None:
            # Grow by a batch, then drop the batch, rather than add/remove one key at
            # a time: an insert immediately undone leaves the size wrong for two
            # bytecodes and a reader almost never lands there. `_record_partial` is
            # itself a burst -- one write per degraded appliance as the setup loop
            # walks the inventory -- so the batch is also the truer shape.
            i = 0
            try:
                while not stop.is_set():
                    nh._hydration_failures[f"11:22:33:44:55:{i % 64:02X}#1"] = code
                    if i % 64 == 63:
                        for j in range(64):
                            nh._hydration_failures.pop(f"11:22:33:44:55:{j:02X}#1", None)
                    i += 1
            except Exception as err:  # pragma: no cover - writer must stay clean
                failures.append(err)

        # Registered BEFORE the interval is touched and before the thread exists, so
        # neither can leak into the rest of the run no matter where this fails. A
        # switch interval left at 1e-6 slows every Python-level operation in the
        # process, which is how a threaded test takes an unrelated wall-clock test
        # down with it several files later.
        # Cleanups run LIFO, so these are registered in reverse of the order they must
        # execute: signal the writer to stop, THEN join it, THEN restore the interval.
        previous = sys.getswitchinterval()
        self.addCleanup(sys.setswitchinterval, previous)
        sys.setswitchinterval(1e-6)
        thread = threading.Thread(target=writer, daemon=True)
        self.addCleanup(thread.join, 5)
        self.addCleanup(stop.set)
        thread.start()
        try:
            escaped = None
            results = []
            for _ in range(reads):
                try:
                    results.append(reader(nh))
                except Exception as err:
                    escaped = err
                    break
        finally:
            stop.set()
            thread.join(timeout=5)
            sys.setswitchinterval(previous)
        self.assertFalse(thread.is_alive())
        self.assertEqual(previous, sys.getswitchinterval())
        self.assertEqual([], failures)
        return escaped, results

    def test_a_concurrent_writer_cannot_make_the_census_raise(self) -> None:
        escaped, results = self._hammer(
            lambda nh: nh.degraded_census, keys=400, reads=2000
        )
        self.assertIsNone(escaped, f"degraded_census raised: {escaped!r}")
        self.assertEqual(2000, len(results))
        # Not merely "did not raise": every snapshot is still a shape-checked label
        # mapped to a count, so a copy that traded the race for a torn read would be
        # caught here too. The count floats with the writer's batch, hence the bound
        # rather than an equality.
        for row in results[:: max(1, len(results) // 50)]:
            self.assertEqual({"ADDHON-230"}, set(row))
            self.assertGreaterEqual(row["ADDHON-230"], 400)

    def test_the_reader_this_test_stands_in_for_really_does_tear(self) -> None:
        # The control. Without it this class proves nothing: a guard test that stays
        # green because the race is unreachable on this interpreter is indistinguish-
        # able from one that stays green because the guard works. This runs the
        # UNGUARDED shape -- the same Python-level loop over the live mapping that
        # `degraded_census` would be if the `dict()` copy were dropped -- and asserts
        # it does tear, so a future reader knows the copy above is load-bearing here
        # and not cargo.
        def unguarded(nh):
            out: dict = {}
            for value in nh._hydration_failures.values():
                label = getattr(value, "label", None)
                if isinstance(label, str):
                    out[label] = out.get(label, 0) + 1
            return out

        escaped, _ = self._hammer(unguarded, keys=400, reads=2000)
        self.assertIsInstance(
            escaped,
            RuntimeError,
            "the unguarded loop did NOT tear on this interpreter, so the test above "
            "proves nothing about the dict() copy in degraded_census. Re-derive "
            "whether that copy is still load-bearing here before deleting either "
            "test -- do not delete this guard to make the run green.",
        )
        self.assertIn("changed size during iteration", str(escaped))


class FetchCensusLifetimeTest(unittest.TestCase):
    """How far the appliance-list fetch census actually travels, stated as behaviour.

    `HonApi.load_appliances` records `outcome: "raised"` when the POST dies before a
    body (a 429, any >= 500, a non-JSON CDN page). The obvious reading of that branch
    -- that a diagnostics dump will therefore show `outcome: "raised"` -- is WRONG,
    and this class exists so nobody has to rediscover why by instrumenting a running
    Home Assistant.

    The census lives on the transport api, the api lives on the session, and the raise
    that produced the census also destroys the session: it propagates out of setup(),
    `create()`'s `except BaseException` calls `close()`, and `close()` nulls `_api`.
    From that point the session answers `None`, `HonClient._close_sync` nulls the
    session, and `async_setup_entry` pops the entry bucket -- so the dump for that
    entry reads `client_absent`.

    That is by design, not by oversight: making a FAILED SETUP diagnosable is refused
    for this change (it would change the meaning of `_last_error`'s existing
    `client_absent` answer and opens an ownership question of its own), and the shape
    that will carry this census when it lands is inscribed in the design note. Until
    then the branch is correct where it is written and unreachable where it is read,
    and that is exactly what the two tests below say.
    """

    def test_the_census_records_the_raise_on_the_api_that_made_the_call(self) -> None:
        nh = NativeHon("u@x", "p")
        api = FakeApi([], [])

        async def failing_load_appliances():
            api.last_appliance_fetch = {
                "at": None,
                "status": None,
                "code": "ADDHON-450",
                "outcome": "raised",
                "stopped_at": None,
                "node_type": None,
                "siblings": None,
                "count": None,
            }
            raise RuntimeError("hOn server error (status 503)")

        api.last_appliance_fetch = None
        api.load_appliances = failing_load_appliances
        nh._api = api
        with self.assertRaises(RuntimeError):
            _run(nh.setup())
        # Written, and readable through the session -- for as long as the session has
        # an api to read it from.
        self.assertEqual("raised", nh.last_appliance_fetch["outcome"])

    def test_the_raise_that_writes_the_census_also_destroys_its_reader(self) -> None:
        # The half that bounds the feature. close() nulls `_api`, so the census the
        # branch above just wrote stops being reachable from the session, and every
        # reader downstream of the session (HonClient -> diagnostics) reads None.
        nh = NativeHon("u@x", "p")
        api = FakeApi([], [])
        nh._api = api
        api.last_appliance_fetch = {"outcome": "raised", "code": "ADDHON-450"}
        self.assertEqual("raised", nh.last_appliance_fetch["outcome"])
        _run(nh.close())
        self.assertIsNone(nh._api)
        # `never_ran` in the dump, and after the entry bucket is popped,
        # `client_absent`. Neither says "raised", and that is the documented cost.
        self.assertIsNone(nh.last_appliance_fetch)


if __name__ == "__main__":
    unittest.main()
