# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-appliance resilience of the coordinator poll (async_get_appliances_data).

A non-auth failure on ONE appliance (a transient cloud 5xx that outlived the
retries, a malformed payload, ...) must NOT blank EVERY device: the failed
appliance is simply absent from the snapshot while the others stay live. Only a
TOTAL failure (every appliance errored) re-raises so the coordinator marks the
cycle failed instead of publishing an empty snapshot.

Uses the same HA-stub harness as test_hon_client_realtime.py (the real
homeassistant package is not importable in the unit env).
"""
from __future__ import annotations

import asyncio
import json
import sys
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


_install_stubs()

from custom_components.addhon.hon_client import (  # noqa: E402
    HonClient,
    _representative_failure,
    _requires_reauth,
)
from custom_components.addhon.error_codes import (  # noqa: E402
    APPLIANCE_LOAD_FAILED,
    AUTH_REFRESH,
    DECODE_ERROR,
    UNKNOWN,
    HonCodedError,
    classify,
)


class FakeApi:
    def __init__(self, appliances) -> None:
        self.appliances = appliances


class FakeAppliance:
    def __init__(self, uid: str) -> None:
        self.unique_id = uid
        self.attributes = {"parameters": {}, "available": True}
        self.settings = {"s": 1}
        self.statistics = {}
        self.nick_name = uid


def _client(appliances):
    c = HonClient(email="e@x", password="p")
    c._api = FakeApi(appliances)
    return c


class CoordinatorResilienceTest(unittest.TestCase):
    def test_first_poll_is_strict_one_failure_raises(self) -> None:
        # On the FIRST poll, a per-appliance failure must re-raise (NOT skip): platform
        # setup iterates the first snapshot once with no dynamic discovery, so a skipped
        # appliance would get zero entities until a reload. Raising -> ConfigEntryNotReady
        # -> HA retries setup until the full inventory loads.
        good, bad = FakeAppliance("g1"), FakeAppliance("bad")
        c = _client([good, bad])  # fresh client: _first_poll_done is False

        def _update(appliance):
            if appliance is bad:
                raise RuntimeError("boom")  # non-auth, non-retryable

        c._update_appliance_sync = _update
        # CR#6: it now raises a coded error preserving the real cause (boom is opaque ->
        # APPLIANCE_LOAD_FAILED) and stays NON-auth, so routing is UpdateFailed ->
        # ConfigEntryNotReady (HA retries setup), not a reauth.
        with self.assertRaises(HonCodedError) as ctx:
            asyncio.run(c.async_get_appliances_data())
        self.assertIs(classify(ctx.exception), APPLIANCE_LOAD_FAILED)
        self.assertFalse(ctx.exception.error_code.requires_reauth)
        self.assertFalse(c._first_poll_done)  # never completed -> still strict

    def test_first_poll_full_success_flips_flag(self) -> None:
        c = _client([FakeAppliance("g1"), FakeAppliance("g2")])
        c._update_appliance_sync = lambda appliance: None
        data = asyncio.run(c.async_get_appliances_data())
        self.assertEqual(set(data), {"g1", "g2"})
        self.assertTrue(c._first_poll_done)  # steady-state resilience now armed

    def test_steady_state_partial_update_keeps_the_others(self) -> None:
        good1, bad, good2 = FakeAppliance("g1"), FakeAppliance("bad"), FakeAppliance("g2")
        c = _client([good1, bad, good2])
        c._first_poll_done = True  # simulate a healthy first poll already happened

        def _update(appliance):
            if appliance is bad:
                raise RuntimeError("boom")  # non-auth, non-retryable

        c._update_appliance_sync = _update
        data = asyncio.run(c.async_get_appliances_data())

        self.assertIn("g1", data)
        self.assertIn("g2", data)
        self.assertNotIn("bad", data)  # the failed appliance is skipped, not fatal

    def test_steady_state_total_failure_raises(self) -> None:
        bad1, bad2 = FakeAppliance("b1"), FakeAppliance("b2")
        c = _client([bad1, bad2])
        c._first_poll_done = True
        c._update_appliance_sync = lambda appliance: (_ for _ in ()).throw(RuntimeError("boom"))
        # Every appliance failed -> surface a failed update (do NOT publish empty data).
        # CR#6: a coded error carrying a meaningful NON-auth code (boom is opaque ->
        # APPLIANCE_LOAD_FAILED), never the old UNKNOWN.
        with self.assertRaises(HonCodedError) as ctx:
            asyncio.run(c.async_get_appliances_data())
        self.assertIs(classify(ctx.exception), APPLIANCE_LOAD_FAILED)
        self.assertIsNot(classify(ctx.exception), UNKNOWN)
        self.assertFalse(ctx.exception.error_code.requires_reauth)

    def test_total_failure_preserves_meaningful_code(self) -> None:
        # CR#6: a real per-appliance cause must reach the catalog, not UNKNOWN.
        bad1, bad2 = FakeAppliance("b1"), FakeAppliance("b2")
        c = _client([bad1, bad2])
        c._first_poll_done = True
        c._update_appliance_sync = lambda appliance: (_ for _ in ()).throw(
            RuntimeError("decode error")  # non-retryable -> classify DECODE_ERROR
        )
        with self.assertRaises(HonCodedError) as ctx:
            asyncio.run(c.async_get_appliances_data())
        self.assertIs(classify(ctx.exception), DECODE_ERROR)

    def test_total_failure_carried_coded_error_surfaced(self) -> None:
        bad1, bad2 = FakeAppliance("b1"), FakeAppliance("b2")
        c = _client([bad1, bad2])
        c._first_poll_done = True
        c._update_appliance_sync = lambda appliance: (_ for _ in ()).throw(
            HonCodedError(DECODE_ERROR)
        )
        with self.assertRaises(HonCodedError) as ctx:
            asyncio.run(c.async_get_appliances_data())
        self.assertIs(ctx.exception.error_code, DECODE_ERROR)

    def test_total_failure_mixed_picks_first_meaningful(self) -> None:
        # First failure opaque (UNKNOWN), second meaningful -> the meaningful one wins.
        b1, b2 = FakeAppliance("b1"), FakeAppliance("b2")
        c = _client([b1, b2])
        c._first_poll_done = True

        def _update(appliance):
            if appliance is b1:
                raise RuntimeError("boom")        # UNKNOWN -> skipped by the picker
            raise RuntimeError("decode error")    # DECODE_ERROR -> chosen

        c._update_appliance_sync = _update
        with self.assertRaises(HonCodedError) as ctx:
            asyncio.run(c.async_get_appliances_data())
        self.assertIs(classify(ctx.exception), DECODE_ERROR)

    def test_total_failure_all_unknown_falls_back_to_load_failed(self) -> None:
        b1, b2 = FakeAppliance("b1"), FakeAppliance("b2")
        c = _client([b1, b2])
        c._first_poll_done = True
        c._update_appliance_sync = lambda appliance: (_ for _ in ()).throw(RuntimeError("boom"))
        with self.assertRaises(HonCodedError) as ctx:
            asyncio.run(c.async_get_appliances_data())
        self.assertIs(ctx.exception.error_code, APPLIANCE_LOAD_FAILED)

    def test_total_failure_routing_stays_non_auth(self) -> None:
        b1 = FakeAppliance("b1")
        c = _client([b1])
        c._first_poll_done = True
        c._update_appliance_sync = lambda appliance: (_ for _ in ()).throw(RuntimeError("boom"))
        with self.assertRaises(HonCodedError) as ctx:
            asyncio.run(c.async_get_appliances_data())
        # the consumer routes a non-reauth code as a transient UpdateFailed, not a reauth
        self.assertFalse(_requires_reauth(ctx.exception))

    def test_representative_failure_rejects_reauth_code(self) -> None:
        # The picker must NOT surface a reauth code (it would flip the transient
        # UpdateFailed into a ConfigEntryAuthFailed). classify() is substring-based and
        # could name an auth code for an error the non-auth gate already let through, so
        # a reauth-classified failure falls back to the non-auth APPLIANCE_LOAD_FAILED.
        code, _cause = _representative_failure([("n", HonCodedError(AUTH_REFRESH))])
        self.assertIs(code, APPLIANCE_LOAD_FAILED)
        self.assertFalse(code.requires_reauth)

    def test_first_poll_failure_preserves_meaningful_code(self) -> None:
        good, bad = FakeAppliance("g1"), FakeAppliance("bad")
        c = _client([good, bad])  # _first_poll_done False

        def _update(appliance):
            if appliance is bad:
                raise RuntimeError("decode error")  # DECODE_ERROR, non-retryable

        c._update_appliance_sync = _update
        with self.assertRaises(HonCodedError) as ctx:
            asyncio.run(c.async_get_appliances_data())
        self.assertIs(classify(ctx.exception), DECODE_ERROR)
        self.assertFalse(c._first_poll_done)

    def test_first_poll_failure_keeps_the_phase_the_cause_knows(self) -> None:
        # The strict first-poll wrapper is the twin of the all-failed raise in
        # `NativeHon.setup()`, which passes phase="load_appliance". This one did not, so
        # the ONE failure the hierarchical phase was introduced to name arrived with
        # phase=None: __init__.py reads `getattr(err, "phase", None)` off THIS wrapper
        # (the __cause__ chain below it is never consulted) and Download Diagnostics
        # showed a null phase for a fault that knew exactly where it happened.
        # DECODE_ERROR rather than a timeout for the same reason as the test above: a
        # retryable code would spend the 15s of server backoff before reaching the
        # branch under test. What is being pinned is the wrapper forwarding the phase,
        # which does not depend on which non-auth code the cause carries.
        from custom_components.addhon.error_codes import DECODE_ERROR

        bad = FakeAppliance("bad")
        c = _client([bad])  # _first_poll_done False -> the strict branch

        def _update(appliance):
            raise HonCodedError(
                DECODE_ERROR, "unreadable command payload", phase="load_appliance"
            )

        c._update_appliance_sync = _update
        with self.assertRaises(HonCodedError) as ctx:
            asyncio.run(c.async_get_appliances_data())
        self.assertIs(DECODE_ERROR, ctx.exception.error_code)
        self.assertEqual("load_appliance", ctx.exception.phase)

    def test_zero_appliances_returns_empty_without_raising(self) -> None:
        c = _client([])
        c._update_appliance_sync = lambda appliance: None
        self.assertEqual(asyncio.run(c.async_get_appliances_data()), {})

    def test_reauth_error_still_triggers_reauth(self) -> None:
        # Regression: an auth/"session unavailable" error must NOT be silently
        # skipped like a generic one -> it still drives re-authentication and a retry.
        app = FakeAppliance("ac")
        c = _client([app])
        calls = {"update": 0, "reauth": 0}

        def _update(appliance):
            calls["update"] += 1
            if calls["update"] == 1:
                raise RuntimeError("hOn session unavailable")  # _requires_reauth -> True

        async def _reauth():
            calls["reauth"] += 1
            return True

        c._update_appliance_sync = _update
        c._async_reauth = _reauth
        data = asyncio.run(c.async_get_appliances_data())

        self.assertEqual(calls["reauth"], 1)       # reauth was attempted
        self.assertGreaterEqual(calls["update"], 2)  # and the poll retried
        self.assertIn("ac", data)                  # recovered after reauth


class PollCensusTest(unittest.TestCase):
    """The census the poll records for the downloadable diagnostics.

    A real 5.9.3 field dump reads `"appliances": []` with a null `last_error` both
    for an account that owns nothing and for a cycle that dropped every appliance
    the cloud returned. The `Partial update: %d/%d` WARNING is the only place those
    two are separated today, and a WARNING never reaches the file the reporter
    attaches, so the two bugs arrive byte-identical.
    """

    def test_clean_poll_records_every_appliance_as_kept(self) -> None:
        c = _client([FakeAppliance("g1"), FakeAppliance("g2")])
        c._update_appliance_sync = lambda appliance: None
        asyncio.run(c.async_get_appliances_data())
        self.assertEqual(
            {"returned": 2, "kept": 2, "dropped": []}, c.last_poll_census
        )

    def test_zero_appliances_records_a_census_not_a_silence(self) -> None:
        # The whole point: "returned 0" must be a statement in the dump, not the
        # absence of one, because the alternative reading of an empty dump is a poll
        # that returned appliances and kept none.
        c = _client([])
        c._update_appliance_sync = lambda appliance: None
        asyncio.run(c.async_get_appliances_data())
        self.assertEqual(
            {"returned": 0, "kept": 0, "dropped": []}, c.last_poll_census
        )

    def test_partial_failure_names_the_cause_of_each_drop(self) -> None:
        good, bad = FakeAppliance("g1"), FakeAppliance("bad")
        c = _client([good, bad])
        c._first_poll_done = True  # steady state: the failed one is skipped

        def _update(appliance):
            if appliance is bad:
                raise RuntimeError("decode error")  # -> DECODE_ERROR, non-retryable

        c._update_appliance_sync = _update
        asyncio.run(c.async_get_appliances_data())
        census = c.last_poll_census
        self.assertEqual(2, census["returned"])
        self.assertEqual(1, census["kept"])
        self.assertEqual(
            [DECODE_ERROR.label], [drop["code"] for drop in census["dropped"]]
        )

    def test_total_failure_still_records_the_cycle_it_lost(self) -> None:
        # The branch that raises is the one whose dump needs this most: the
        # coordinator keeps its previous snapshot (or none at all on the first
        # cycle), so without a census "returned 2, kept 0" is indistinguishable
        # from "returned 0" -- two different bugs.
        b1, b2 = FakeAppliance("b1"), FakeAppliance("b2")
        c = _client([b1, b2])
        c._first_poll_done = True
        c._update_appliance_sync = lambda appliance: (_ for _ in ()).throw(
            RuntimeError("decode error")
        )
        with self.assertRaises(HonCodedError):
            asyncio.run(c.async_get_appliances_data())
        census = c.last_poll_census
        self.assertEqual(2, census["returned"])
        self.assertEqual(0, census["kept"])
        self.assertEqual(
            [DECODE_ERROR.label, DECODE_ERROR.label],
            [drop["code"] for drop in census["dropped"]],
        )

    def test_first_poll_abort_records_no_census(self) -> None:
        # The strict first-poll branch re-raises from INSIDE the loop, so the
        # appliances after the failing one were never attempted. A census counting
        # them as survivors would be a false statement, and no census at all is the
        # honest one: nothing completed.
        bad, never_reached = FakeAppliance("bad"), FakeAppliance("g1")
        c = _client([bad, never_reached])  # fresh client -> strict, and bad is first

        def _update(appliance):
            if appliance is bad:
                raise RuntimeError("decode error")

        c._update_appliance_sync = _update
        with self.assertRaises(HonCodedError):
            asyncio.run(c.async_get_appliances_data())
        self.assertIsNone(c.last_poll_census)

    def test_census_carries_no_identity_and_no_raw_exception_text(self) -> None:
        # The redacted name belongs to the WARNING and nowhere else, and an exception
        # message is not a closed domain: this one carries the nickname and the MAC,
        # which is what a cloud 5xx body or a URL in a transport error looks like.
        bad = FakeAppliance("AA:BB:CC:DD:EE:FF")
        bad.nick_name = "Kitchen Washer"
        c = _client([FakeAppliance("g1"), bad])
        c._first_poll_done = True

        def _update(appliance):
            if appliance is bad:
                raise RuntimeError(
                    "decode error for Kitchen Washer at AA:BB:CC:DD:EE:FF"
                )

        c._update_appliance_sync = _update
        asyncio.run(c.async_get_appliances_data())
        blob = json.dumps(c.last_poll_census)
        for leak in (
            "Kitchen Washer",
            "AA:BB:CC:DD:EE:FF",
            "decode error for",
            "***",  # not even the redacted name: the census has no name field
        ):
            self.assertNotIn(leak, blob, leak)
        # Positive control: the drop IS reported, so the assertions above are not
        # passing on an empty census.
        self.assertEqual(
            [DECODE_ERROR.label], [drop["code"] for drop in c.last_poll_census["dropped"]]
        )


if __name__ == "__main__":
    unittest.main()
