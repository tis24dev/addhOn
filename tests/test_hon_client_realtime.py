# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the MQTT realtime wiring (#4): HonClient.subscribe_updates /
build_realtime_snapshot, plus source-guards for the async_setup_entry wiring.

A full async_setup_entry behavioural test is infeasible with the stub harness
(it runs the executor login, first refresh and platform forwarding), so the
cross-thread wiring in __init__.py is covered by source-guards (same approach as
test_coordinator_config_entry.py).
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
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

from custom_components.addhon.hon_client import HonClient  # noqa: E402
from custom_components.addhon.error_codes import HonCodedError  # noqa: E402


class FakeSession:
    """Stands in for the NativeHon session held as HonClient._hon_instance."""

    def __init__(self, appliances) -> None:
        self.appliances = appliances
        self._notify_function = None

    def subscribe_updates(self, fn) -> None:
        self._notify_function = fn

    def notify(self) -> None:
        if self._notify_function:
            self._notify_function(None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class FakeAppliance:
    def __init__(self, uid: str) -> None:
        self.unique_id = uid
        self.attributes = {"parameters": {}}
        self.settings = {"s": 1}
        self.statistics = {}
        self.nick_name = "Nick"


def _client(appliances=None):
    c = HonClient(email="e@x", password="p")
    c._hon_instance = FakeSession(appliances or [])
    return c


class AuthDiagnosticClientTest(unittest.TestCase):
    def test_constructor_creates_enabled_trace(self) -> None:
        client = HonClient(
            email="e@x",
            password="p",
            validation=True,
            auth_diagnostics=True,
        )
        self.assertTrue(client._auth_trace.enabled)

    def test_constructor_owns_dedicated_command_dispatcher(self) -> None:
        first = HonClient(email="first@x", password="p")
        second = HonClient(email="second@x", password="p")

        self.assertIsNot(first._command_dispatcher, second._command_dispatcher)

    def test_dispatch_patch_sync_delegates_once_to_dedicated_loop(self) -> None:
        client = HonClient(email="e@x", password="p")
        appliance = object()
        patch = object()
        dispatch_calls: list[tuple[object, object]] = []
        loop_calls: list[object] = []

        class Dispatcher:
            async def dispatch(
                self,
                observed_appliance: object,
                observed_patch: object,
            ) -> bool:
                dispatch_calls.append((observed_appliance, observed_patch))
                return True

        client._command_dispatcher = Dispatcher()  # type: ignore[assignment]

        def run_on_hon_loop(coro: object, timeout: float | None = None) -> bool:
            loop_calls.append(coro)
            return asyncio.run(coro)  # type: ignore[arg-type]

        client._run_on_hon_loop = run_on_hon_loop  # type: ignore[assignment]

        self.assertTrue(client.dispatch_patch_sync(appliance, patch))
        self.assertEqual([(appliance, patch)], dispatch_calls)
        self.assertEqual(1, len(loop_calls))

    def test_the_dedicated_loop_hands_back_the_same_object(self) -> None:
        """Drives the REAL _run_on_hon_loop, on a real background loop.

        Every other test in this suite replaces it, so nothing observed what it
        actually returns. The command adapter decides acceptance with
        `accepted is not True`, an IDENTITY check, and a hop that returned an equal
        value instead of the same one would make every successful purifier write
        raise "the service did not accept the command". That happened: a stray edit
        turned the return into `1 if _v is True else _v`, the whole suite stayed
        green, and only a probe against the real hop caught it. Truthiness is not
        enough here, so this asserts identity.
        """
        client = HonClient(email="e@x", password="p")
        client._start_hon_loop()
        try:
            for value in (True, False, None, 7, "ok"):
                async def _answer(result: object = value) -> object:
                    return result

                self.assertIs(value, client._run_on_hon_loop(_answer()))
        finally:
            loop = client._hon_loop
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            thread = client._hon_thread
            if thread is not None:
                thread.join(timeout=5)


class LoopWatchdogAttributionTest(unittest.TestCase):
    """What the dedicated-loop watchdog reports when it fires (issue #76).

    It used to throw the cancelled coroutine's exception away and rebuild a synthetic
    error out of phase + code, so a failure that had a NAME arrived nameless. That is
    why no hypothesis about #76 was falsifiable on a real install.
    """

    def _client(self) -> HonClient:
        client = HonClient(email="e@x", password="p")
        client._start_hon_loop()

        def _stop() -> None:
            loop = client._hon_loop
            if loop is not None:
                loop.call_soon_threadsafe(loop.stop)
            thread = client._hon_thread
            if thread is not None:
                thread.join(timeout=5)

        self.addCleanup(_stop)
        return client

    def test_genuine_stall_keeps_the_phase_code(self) -> None:
        from custom_components.addhon import error_codes as ec

        client = self._client()
        client._hon_instance = types.SimpleNamespace(
            current_phase="load_appliances", phase_summary=""
        )

        async def _forever() -> None:
            await asyncio.sleep(30)

        with self.assertLogs("custom_components.addhon.hon_client", level="ERROR"):
            with self.assertRaises(ec.HonCodedError) as ctx:
                client._run_on_hon_loop(_forever(), 0.2)
        self.assertIs(ec.NETWORK_TIMEOUT, ctx.exception.error_code)
        self.assertIsInstance(ctx.exception.__cause__, TimeoutError)

    def test_nested_auth_phase_is_attributed_to_the_auth_timeout(self) -> None:
        # A REAL PhaseTracker driven by REAL phase() scopes, not a frozen string. With
        # a types.SimpleNamespace whose `current_phase` is a constant, this test passed
        # while production still answered ADDHON-400: the scopes UNWIND when the task is
        # cancelled and every phase() restores the mirror on the way out, so a mirror
        # read after the drain is always "". Only a mirror that really unwinds can pin
        # that the read happens BEFORE the cancellation.
        from custom_components.addhon import error_codes as ec
        from custom_components.addhon.client.phase import PhaseTracker, phase

        tracker = PhaseTracker()

        class _Session:
            """What NativeHon exposes to the watchdog, backed by the live tracker."""

            _setup_phase = "load_appliances"

            @property
            def current_phase(self) -> str:
                return tracker.current

            @property
            def phase_summary(self) -> str:
                return tracker.summary()

        client = self._client()
        client._hon_instance = _Session()

        async def _stalls_inside_a_lazy_signin() -> None:
            # The exact #76 shape: the sign-in starts inside the appliance-list request.
            with phase("load_appliances", tracker):
                with phase("auth/refresh", tracker):
                    await asyncio.sleep(30)

        with self.assertLogs("custom_components.addhon.hon_client", level="ERROR"):
            with self.assertRaises(ec.HonCodedError) as ctx:
                client._run_on_hon_loop(_stalls_inside_a_lazy_signin(), 0.2)
        self.assertIs(ec.REFRESH_TIMEOUT, ctx.exception.error_code)
        self.assertEqual("load_appliances/auth/refresh", ctx.exception.phase)
        # The scopes really did unwind: the mirror is empty by now, so the code above
        # could only have come from a read taken before the cancellation.
        self.assertEqual("", tracker.current)

    def test_real_cause_survives_the_race_with_the_watchdog(self) -> None:
        from custom_components.addhon import error_codes as ec

        client = self._client()
        client._hon_instance = types.SimpleNamespace(
            current_phase="load_appliances", phase_summary=""
        )

        async def _reveals_its_error_on_cancel() -> None:
            # The lost race, made deterministic: the coroutine's real failure only
            # surfaces once the watchdog cancels it. Before, `_drain_task` swallowed it
            # into a DEBUG line and the caller got a synthetic, message-less timeout.
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise RuntimeError("hOn server error (status 503)") from None

        with self.assertLogs("custom_components.addhon.hon_client", level="ERROR"):
            with self.assertRaises(ec.HonCodedError) as ctx:
                client._run_on_hon_loop(_reveals_its_error_on_cancel(), 0.2)
        # The real cause, not a synthetic ADDHON-400/460.
        self.assertIs(ec.SERVER_ERROR, ctx.exception.error_code)
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

    def test_a_timeout_cause_that_loses_the_race_keeps_its_phase(self) -> None:
        # SAME branch as the test above, other flavour of cause. The recovered error is
        # classified with `classify(original, phase=phase)`, and for a TIMEOUT the phase
        # is the WHOLE answer: drop the keyword and `classify` resolves it without one,
        # turning the attributed ADDHON-406 back into the mute ADDHON-460 of #76 -- on a
        # failure that had a name. The sibling test uses a RuntimeError, which classify
        # resolves from the message alone, so it can never see that regression.
        #
        # The lost race, made deterministic: the coroutine yields once, then BLOCKS the
        # loop past the cap and raises a bare per-request TimeoutError. `_cancel_and_drain`
        # therefore runs only after the task has already finished, which is what selects
        # the drained-cause branch instead of the genuine-stall one.
        import time as _time

        from custom_components.addhon import error_codes as ec
        from custom_components.addhon.client.phase import PhaseTracker, phase

        tracker = PhaseTracker()

        class _Session:
            _setup_phase = "load_appliances"

            @property
            def current_phase(self) -> str:
                return tracker.current

            @property
            def phase_summary(self) -> str:
                return tracker.summary()

        client = self._client()
        client._hon_instance = _Session()

        async def _raises_a_bare_timeout_just_too_late() -> None:
            with phase("load_appliances", tracker):
                with phase("auth/refresh", tracker):
                    await asyncio.sleep(0.05)
                    _time.sleep(0.5)  # blocking: the drain cannot run before the raise
                    raise TimeoutError()

        with self.assertLogs("custom_components.addhon.hon_client", level="ERROR"):
            with self.assertRaises(ec.HonCodedError) as ctx:
                client._run_on_hon_loop(_raises_a_bare_timeout_just_too_late(), 0.2)
        self.assertIs(ec.REFRESH_TIMEOUT, ctx.exception.error_code)
        self.assertEqual("load_appliances/auth/refresh", ctx.exception.phase)
        self.assertIsInstance(ctx.exception.__cause__, TimeoutError)

    def test_a_teardown_gives_the_in_flight_caller_a_coded_error(self) -> None:
        # The other side of the narrow lifecycle lock. The wait is deliberately NOT held
        # under it (an unload used to queue behind the slowest in-flight call, minutes
        # with these caps), so `close_sync` can now cancel the task under a waiter, and
        # the waiter gets a bare, message-less concurrent.futures.CancelledError back
        # from the future -- which classify can only call ADDHON-999 "Unknown error",
        # with nothing anywhere saying the client was shutting down.
        #
        # This pins the decision between the two available semantics: the in-flight call
        # is ABORTED, never waited for (waiting is the ~5-minute unload the narrow lock
        # exists to prevent), and the abort is converted into an attributed, ordinary
        # Exception every guard in the integration already handles.
        import concurrent.futures

        from custom_components.addhon import error_codes as ec

        client = HonClient(email="e@x", password="p")
        client._start_hon_loop()
        client._hon_instance = types.SimpleNamespace(current_phase="", phase_summary="")
        entered = threading.Event()
        seen: dict[str, BaseException] = {}

        async def _slow() -> None:
            entered.set()
            await asyncio.sleep(30)

        def _wait() -> None:
            try:
                client._run_on_hon_loop(_slow(), 30)
            except BaseException as err:  # noqa: BLE001 - that is the point
                seen["err"] = err

        worker = threading.Thread(target=_wait, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 10)
        self.assertTrue(entered.wait(5), "the coroutine never started")

        started = time.monotonic()
        client._close_sync()
        # The teardown does NOT wait for the in-flight call (that is the rejected
        # alternative: up to APPLIANCE_POLL, ~5 minutes, with Home Assistant reporting
        # the unload as slow).
        self.assertLess(time.monotonic() - started, 15)
        worker.join(10)

        err = seen.get("err")
        self.assertIsInstance(err, ec.HonCodedError)
        self.assertIs(ec.CLIENT_SHUTDOWN, err.error_code)
        self.assertIsInstance(err.__cause__, concurrent.futures.CancelledError)
        # It routes as a transient failure, never as a reauth prompt...
        self.assertFalse(ec.CLIENT_SHUTDOWN.requires_reauth)
        # ...and a real task cancellation is NOT what was swallowed: since 3.8 the two
        # CancelledErrors are different classes, and only the futures one is caught.
        self.assertNotIsInstance(err.__cause__, asyncio.CancelledError)
        # Without the conversion this is all the caller could have been told.
        self.assertIs(ec.UNKNOWN, ec.classify(err.__cause__))

    def test_mfa_challenge_at_the_cap_propagates_naked(self) -> None:
        from custom_components.addhon.client.transport.auth import MFAChallengeRequired

        client = self._client()
        client._hon_instance = types.SimpleNamespace(
            current_phase="load_appliances", phase_summary=""
        )

        async def _challenges_on_cancel() -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise MFAChallengeRequired("otp") from None

        with self.assertLogs("custom_components.addhon.hon_client", level="ERROR"):
            # Wrapping it in a HonCodedError would break the interactive 2FA resume.
            with self.assertRaises(MFAChallengeRequired):
                client._run_on_hon_loop(_challenges_on_cancel(), 0.2)

    def test_a_timeout_raised_by_the_coroutine_is_not_the_watchdog(self) -> None:
        # Since Python 3.11 `concurrent.futures.TimeoutError IS asyncio.TimeoutError IS
        # TimeoutError`, so a BARE per-request timeout coming out of the coroutine is
        # the very type the cap raises. Caught by the cap's own clause, it produced a
        # false "watchdog fired after 0.0s" on a cap that never expired and handed the
        # caller the mute ADDHON-460 instead of the failure's own code -- with the
        # first-poll `load_commands` rehydration (an await nothing budgets) as a live
        # trigger. Told apart by STATE, not type: on a real expiry the future is still
        # pending, here it has already finished.
        client = self._client()
        client._hon_instance = types.SimpleNamespace(
            current_phase="load_appliance", phase_summary=""
        )

        async def _times_out_instantly() -> None:
            raise asyncio.TimeoutError()

        with self.assertNoLogs("custom_components.addhon.hon_client", level="ERROR"):
            with self.assertRaises(TimeoutError) as ctx:
                client._run_on_hon_loop(_times_out_instantly(), 300)
        self.assertNotIsInstance(ctx.exception, HonCodedError)

    def test_the_wait_does_not_hold_the_lifecycle_lock(self) -> None:
        # The wait is not a lifecycle transition, and the caps this file passes are now
        # minutes rather than the legacy 60s: holding the lock across it made an
        # unload/reload (`close_sync`) and a re-login queue behind the slowest in-flight
        # call, so a stalled poll blocked them for a whole APPLIANCE_POLL (~5 min) and
        # Home Assistant reported the unload as slow.
        client = self._client()
        client._hon_instance = types.SimpleNamespace(current_phase="", phase_summary="")
        entered = threading.Event()

        async def _slow() -> None:
            entered.set()
            await asyncio.sleep(1)

        worker = threading.Thread(
            target=lambda: client._run_on_hon_loop(_slow(), 5), daemon=True
        )
        worker.start()
        self.addCleanup(worker.join, 10)
        self.assertTrue(entered.wait(5), "the coroutine never started")
        acquired = client._lifecycle_lock.acquire(timeout=0.5)
        if acquired:
            client._lifecycle_lock.release()
        self.assertTrue(acquired, "the lifecycle lock was held across the wait")

    def test_watchdog_log_carries_the_phase_and_no_identity(self) -> None:
        from custom_components.addhon import error_codes as ec

        client = self._client()
        client._hon_instance = types.SimpleNamespace(
            current_phase="load_appliances/auth",
            phase_summary="load_appliances 0.2s timeout",
        )

        async def _forever() -> None:
            await asyncio.sleep(30)

        with self.assertLogs("custom_components.addhon.hon_client", level="ERROR") as cm:
            with self.assertRaises(ec.HonCodedError):
                client._run_on_hon_loop(_forever(), 0.2)
        blob = "\n".join(cm.output)
        self.assertIn("load_appliances/auth", blob)
        self.assertNotIn("@", blob)


class FirstPollRehydrationTest(unittest.TestCase):
    """The other half of the per-appliance transport fault boundary (issue #76).

    Containing a `load_commands` failure at setup is only legitimate if the commands
    come back BEFORE the platforms build their entities. They do not come back on
    their own: `update()` reloads the ATTRIBUTES, the platforms call
    `async_add_entities` exactly once from the first snapshot, and the integration has
    no dynamic discovery -- so without this the contained appliance shipped without
    select/number/switch/button/climate/fan until a manual reload.
    """

    class _Appliance:
        def __init__(self) -> None:
            self.unique_id = "APP-1"
            self.attributes = {"parameters": {"a": 1}}
            self.settings: dict = {}
            self.statistics: dict = {}
            self.commands: dict = {}
            self.calls: list[str] = []
            self.commands_error: BaseException | None = None

        async def update(self) -> None:
            self.calls.append("update")

        async def load_commands(self) -> None:
            self.calls.append("load_commands")
            if self.commands_error is not None:
                raise self.commands_error
            self.commands = {"startProgram": object()}

    class _Session:
        """What NativeHon answers: the REAL predicate, not a stubbed boolean."""

        def __init__(self, degraded) -> None:
            self._degraded = degraded

        def needs_rehydration(self, appliance) -> bool:
            return any(item is appliance for item in self._degraded)

    def _client(self, degraded) -> HonClient:
        c = HonClient(email="e@x", password="p")
        c._run_on_hon_loop = lambda coro, timeout=None: asyncio.run(coro)  # type: ignore[assignment]
        c._api = self._Session(degraded)
        return c

    def test_a_degraded_appliance_reloads_its_commands_on_the_first_poll(self) -> None:
        appliance = self._Appliance()
        self._client([appliance])._update_appliance_sync(appliance)
        self.assertEqual(["load_commands", "update"], appliance.calls)
        self.assertTrue(appliance.commands)

    def test_a_healthy_appliance_is_not_re_requested(self) -> None:
        appliance = self._Appliance()
        self._client([])._update_appliance_sync(appliance)
        self.assertEqual(["update"], appliance.calls)

    def test_after_the_first_poll_the_entities_are_decided_so_nothing_is_re_requested(
        self,
    ) -> None:
        appliance = self._Appliance()
        client = self._client([appliance])
        client._first_poll_done = True
        client._update_appliance_sync(appliance)
        self.assertEqual(["update"], appliance.calls)

    def test_a_second_failure_is_not_contained_a_second_time(self) -> None:
        # It must reach the strict first-poll branch -> ConfigEntryNotReady -> Home
        # Assistant retries the setup, the only path that can still produce a COMPLETE
        # entity inventory. Swallowing it here would ship the crippled entry after all.
        appliance = self._Appliance()
        appliance.commands_error = RuntimeError("commands endpoint is down")
        with self.assertRaises(RuntimeError):
            self._client([appliance])._update_appliance_sync(appliance)

    def test_a_second_failure_that_times_out_is_still_attributed(self) -> None:
        # The rehydration used to be the ONE await on the poll path with no budget and
        # no phase around it. A per-request aiohttp timeout therefore left it BARE, and
        # a bare TimeoutError carries no phase: `representative_failure` classified it
        # with nothing to go on and the user got the mute ADDHON-460 "Setup timed out"
        # from a cap that had not expired -- for a failure whose real name is
        # ADDHON-400. It still propagates (the containment is NOT repeated), but now
        # under its own code, on the phase that actually stalled.
        from custom_components.addhon import error_codes as ec

        appliance = self._Appliance()
        appliance.commands_error = asyncio.TimeoutError()
        with self.assertRaises(HonCodedError) as ctx:
            self._client([appliance])._update_appliance_sync(appliance)
        self.assertIs(ec.NETWORK_TIMEOUT, ctx.exception.error_code)
        self.assertEqual("load_appliance", ctx.exception.phase)
        self.assertIsInstance(ctx.exception.__cause__, TimeoutError)
        # And what the first poll would hand Home Assistant is that code, not the 460.
        self.assertIs(
            ec.NETWORK_TIMEOUT, ec.representative_failure([("x", ctx.exception)])[0]
        )


class SetupFailureRecordTest(unittest.TestCase):
    """What a failed setup_sync leaves behind for Download Diagnostics (issue #76).

    Three fields move together, and setup_sync says so out loud: the code, the phase and
    the per-phase LEDGER. The ledger is the artefact that makes a report like #76
    diagnosable without a live probe -- it is what finally answers "which phase burned
    the time". `diagnostics.py` publishes it and a test pins that READER, but nothing
    pinned the WRITER: delete the three lines in the client and the ledger silently
    disappears from every report with the suite still green.

    The phase must come from the ERROR (hierarchical, recorded where it was raised) and
    not from the flat auth mirror, which only knows the login steps -- preferring the
    mirror is how a lazy sign-in got called "load_appliances" in the first place.
    """

    class _Session:
        auth_phase = "login"
        phase_summary = "load_appliances 0.2s timeout"
        phase_ledger = [
            {"phase": "load_appliances/auth/refresh", "seconds": 0.2, "outcome": "timeout"}
        ]

        def __init__(self, error: BaseException | None) -> None:
            self._error = error

        async def __aenter__(self):
            if self._error is not None:
                raise self._error
            return self

        def subscribe_updates(self, fn) -> None:
            pass

    def _client(self, error: BaseException | None):
        import custom_components.addhon.client.factory as factory

        session = self._Session(error)
        original = factory.create_session
        factory.create_session = lambda email, password, **kw: session
        self.addCleanup(setattr, factory, "create_session", original)

        client = HonClient(email="e@x", password="p")
        client._start_hon_loop = lambda: None  # type: ignore[assignment]
        client._run_on_hon_loop = lambda coro, timeout=None: asyncio.run(coro)  # type: ignore[assignment]
        client._close_sync = lambda: None  # type: ignore[assignment]
        return client

    def test_a_failed_setup_publishes_the_phase_ledger(self) -> None:
        from custom_components.addhon import error_codes as ec

        client = self._client(
            ec.HonCodedError(ec.REFRESH_TIMEOUT, phase="load_appliances/auth/refresh")
        )
        with self.assertRaises(ec.HonCodedError):
            client.setup_sync()
        self.assertEqual(self._Session.phase_ledger, client.last_phase_ledger)
        self.assertIs(ec.REFRESH_TIMEOUT, client.last_error_code)

    def test_the_phase_comes_from_the_error_not_from_the_auth_mirror(self) -> None:
        # The session's flat mirror says "login"; the error knows it was the refresh
        # nested inside the appliance-list request. The hierarchical one wins, and the
        # code follows it (ADDHON-406, not the ADDHON-405 "login" would give).
        from custom_components.addhon import error_codes as ec

        client = self._client(
            ec.HonCodedError(ec.REFRESH_TIMEOUT, phase="load_appliances/auth/refresh")
        )
        with self.assertRaises(ec.HonCodedError):
            client.setup_sync()
        self.assertEqual("load_appliances/auth/refresh", client.last_error_phase)

    def test_a_fresh_attempt_never_shows_the_previous_ledger(self) -> None:
        # The three failure fields are cleared together at the top of setup_sync: a
        # SUCCESS must not leave a report carrying the phases of an earlier failure.
        from custom_components.addhon import error_codes as ec

        client = self._client(ec.HonCodedError(ec.REFRESH_TIMEOUT, phase="auth/refresh"))
        with self.assertRaises(ec.HonCodedError):
            client.setup_sync()
        self.assertIsNotNone(client.last_phase_ledger)

        import custom_components.addhon.client.factory as factory

        factory.create_session = lambda email, password, **kw: self._Session(None)
        client.setup_sync()
        self.assertIsNone(client.last_phase_ledger)
        self.assertIsNone(client.last_error_code)
        self.assertIsNone(client.last_error_phase)

    def test_a_fresh_attempt_never_shows_the_previous_session_poll_census(self) -> None:
        # Cleared in the same block as the three above but for its own reason: the
        # poll census describes ONE session's cycle, and this attempt builds a new
        # session. Kept across, it would let a dump answer "how did the last poll go"
        # with a cycle that ran on a session the client has since thrown away.
        client = self._client(None)
        client.last_poll_census = {"returned": 3, "kept": 3, "dropped": []}
        client.setup_sync()
        self.assertIsNone(client.last_poll_census)


class _FallbackAppliance:
    """No update() attribute -> _do_update takes the load_* fallback path directly.
    Records which loads ran; load_statistics can be made to raise a chosen error."""

    def __init__(self, stats_error=None) -> None:
        self.unique_id = "APP-1"
        self.attributes = {"parameters": {}}
        self.settings: dict = {}
        self.statistics: dict = {}
        self._stats_error = stats_error
        self.calls: list[str] = []

    async def load_attributes(self) -> None:
        self.calls.append("load_attributes")

    async def load_commands(self) -> None:
        self.calls.append("load_commands")

    async def load_statistics(self) -> None:
        self.calls.append("load_statistics")
        if self._stats_error is not None:
            raise self._stats_error


class FallbackLoadStatisticsToleranceTest(unittest.TestCase):
    """Finding 7: in the load_* FALLBACK path a failed load_statistics (consumption
    counters only) must be tolerated for non-auth/non-retryable errors -- exactly like
    the primary update() path -- instead of failing the whole appliance. Auth and
    retryable errors still propagate so reauth/backoff can act on them."""

    def _client_running(self) -> HonClient:
        c = HonClient(email="e@x", password="p")
        # Run _do_update inline instead of on the dedicated loop.
        c._run_on_hon_loop = lambda coro, timeout=None: asyncio.run(coro)  # type: ignore[assignment]
        return c

    def test_non_auth_load_statistics_failure_is_tolerated(self) -> None:
        app = _FallbackAppliance(stats_error=ValueError("stats parse boom"))
        c = self._client_running()
        c._update_appliance_sync(app)  # must NOT raise: attrs+commands loaded
        self.assertEqual(app.calls, ["load_attributes", "load_commands", "load_statistics"])

    def test_retryable_load_statistics_failure_still_raises(self) -> None:
        app = _FallbackAppliance(stats_error=TimeoutError())
        c = self._client_running()
        with self.assertRaises(RuntimeError):
            c._update_appliance_sync(app)

    def test_auth_load_statistics_failure_still_raises(self) -> None:
        app = _FallbackAppliance(stats_error=RuntimeError("401 unauthorized"))
        c = self._client_running()
        with self.assertRaises(RuntimeError):
            c._update_appliance_sync(app)

    def test_load_attributes_failure_is_still_fatal(self) -> None:
        # Regression guard: the tolerance is load_statistics-only. A broken
        # load_attributes (the real data) must still fail the appliance.
        class _BadAttrs(_FallbackAppliance):
            async def load_attributes(self) -> None:
                self.calls.append("load_attributes")
                raise ValueError("attrs boom")

        c = self._client_running()
        with self.assertRaises(RuntimeError):
            c._update_appliance_sync(_BadAttrs())


class HonClientRealtimeTest(unittest.TestCase):
    def test_subscribe_updates_forwarded(self) -> None:
        c = _client()
        cb = lambda _arg: None  # noqa: E731
        c.subscribe_updates(cb)
        self.assertIs(c._hon_instance._notify_function, cb)
        self.assertIs(c._notify_function, cb)  # also stored on the client

    def test_subscribe_updates_before_setup_is_stored_not_raised(self) -> None:
        # #28: no raise when there is no session yet; the callback is remembered and
        # applied by setup_sync. (Old contract raised RuntimeError here.)
        c = HonClient(email="e@x", password="p")  # no _hon_instance yet
        cb = lambda _a: None  # noqa: E731
        c.subscribe_updates(cb)  # must NOT raise
        self.assertIs(c._notify_function, cb)

    def test_subscribe_none_after_close_is_noop(self) -> None:
        # #28: the on-unload detach runs subscribe_updates(None) AFTER the client is
        # closed (_hon_instance None) -> must be a clean no-op, not RuntimeError.
        c = HonClient(email="e@x", password="p")  # post-close state
        c.subscribe_updates(None)  # must NOT raise
        self.assertIsNone(c._notify_function)

    def test_subscribe_updates_detach_with_none(self) -> None:
        c = _client()
        c.subscribe_updates(lambda _a: None)
        c.subscribe_updates(None)
        self.assertIsNone(c._hon_instance._notify_function)
        self.assertIsNone(c._notify_function)

    def test_callback_rewired_after_reauth(self) -> None:
        # #20: setup_sync (run at initial setup AND on re-auth, which rebuilds the
        # session) must re-apply the stored notify callback to the NEW session, else
        # the MQTT push dies permanently after a re-auth.
        import custom_components.addhon.client.factory as factory
        new_session = FakeSession([])
        orig_create = getattr(factory, "create_session", None)
        factory.create_session = lambda email, password, **kw: new_session
        try:
            c = HonClient(email="e@x", password="p")
            cb = lambda _a: None  # noqa: E731
            c.subscribe_updates(cb)  # stored on the client (no session yet)
            # run setup_sync offline: stub the dedicated-loop machinery
            c._start_hon_loop = lambda: None  # type: ignore[assignment]
            c._run_on_hon_loop = lambda coro, timeout=None: coro.close()  # type: ignore[assignment]
            c.setup_sync()
            self.assertIs(c._hon_instance, new_session)
            self.assertIs(new_session._notify_function, cb)  # re-applied to new session
        finally:
            if orig_create is not None:
                factory.create_session = orig_create

    def test_setup_sync_syncs_refresh_token_seed(self) -> None:
        # #1: a fresh login started with seed "" must adopt the live token after setup so a
        # later _async_reauth()/restart re-seeds from it (no full login / 2FA re-prompt).
        import custom_components.addhon.client.factory as factory
        sess = FakeSession([])
        sess.refresh_token = "RT_LIVE"
        orig_create = getattr(factory, "create_session", None)
        factory.create_session = lambda email, password, **kw: sess
        try:
            c = HonClient(email="e@x", password="p", refresh_token="")
            c._start_hon_loop = lambda: None  # type: ignore[assignment]
            c._run_on_hon_loop = lambda coro, timeout=None: coro.close()  # type: ignore[assignment]
            self.assertEqual("", c._refresh_token)
            c.setup_sync()
            self.assertEqual("RT_LIVE", c._refresh_token)  # seed adopted the live token
        finally:
            if orig_create is not None:
                factory.create_session = orig_create

    def test_setup_sync_failure_does_not_wipe_seed(self) -> None:
        # The seed-sync line is after __aenter__ success; a failed setup must NOT overwrite
        # a good seed with "".
        import custom_components.addhon.client.factory as factory
        boom = FakeSession([])
        async def _boom():
            raise RuntimeError("login down")
        boom.__aenter__ = _boom  # type: ignore[assignment]
        orig_create = getattr(factory, "create_session", None)
        factory.create_session = lambda email, password, **kw: boom
        try:
            c = HonClient(email="e@x", password="p", refresh_token="RT_OLD")
            c._start_hon_loop = lambda: None  # type: ignore[assignment]
            c._run_on_hon_loop = lambda coro, timeout=None: asyncio.run(coro)  # type: ignore[assignment]
            with self.assertRaises(RuntimeError):  # the injected setup failure, re-raised as-is
                c.setup_sync()
            self.assertEqual("RT_OLD", c._refresh_token)  # seed preserved on failure
        finally:
            if orig_create is not None:
                factory.create_session = orig_create

    def test_setup_sync_seeds_next_create_with_live_token(self) -> None:
        # #1 end-to-end: the SEED synced after the 1st login must be CONSUMED by the next
        # session build (the reauth re-seed). Capture the refresh_token kwarg passed to
        # create_session across two setup_sync calls on one client.
        import custom_components.addhon.client.factory as factory
        s1, s2 = FakeSession([]), FakeSession([])
        s1.refresh_token = "RT1"
        s2.refresh_token = "RT2"
        seen, it = [], iter([s1, s2])
        def _create(email, password, **kw):
            seen.append(kw.get("refresh_token"))
            return next(it)
        orig_create = getattr(factory, "create_session", None)
        factory.create_session = _create
        try:
            c = HonClient(email="e@x", password="p", refresh_token="")
            c._start_hon_loop = lambda: None  # type: ignore[assignment]
            c._run_on_hon_loop = lambda coro, timeout=None: coro.close()  # type: ignore[assignment]
            c.setup_sync()  # build #1 seeded with ""
            c.setup_sync()  # build #2 must be seeded with RT1 (the 1st session's token)
            self.assertEqual(["", "RT1"], seen)
        finally:
            if orig_create is not None:
                factory.create_session = orig_create

    def test_setup_sync_empty_live_token_keeps_seed(self) -> None:
        # The `or self._refresh_token` fallback: if the live token reads "" after setup, a
        # good existing seed must be preserved (never wiped).
        import custom_components.addhon.client.factory as factory
        sess = FakeSession([])  # no refresh_token attr -> property returns ""
        orig_create = getattr(factory, "create_session", None)
        factory.create_session = lambda email, password, **kw: sess
        try:
            c = HonClient(email="e@x", password="p", refresh_token="RT_OLD")
            c._start_hon_loop = lambda: None  # type: ignore[assignment]
            c._run_on_hon_loop = lambda coro, timeout=None: coro.close()  # type: ignore[assignment]
            c.setup_sync()
            self.assertEqual("RT_OLD", c._refresh_token)  # fallback kept the good seed
        finally:
            if orig_create is not None:
                factory.create_session = orig_create

    def test_poll_syncs_seed_on_mid_life_rotation(self) -> None:
        # #5: a runtime token rotation during a poll must be captured into the seed at the
        # poll boundary, so a later _async_reauth() re-seeds from the current token.
        c = HonClient(email="e@x", password="p", refresh_token="RT0")
        sess = FakeSession([])
        sess.refresh_token = "RTn"  # rotated mid-life
        c._api = sess
        c._hon_instance = sess
        asyncio.run(c.async_get_appliances_data())
        self.assertEqual("RTn", c._refresh_token)

    def test_poll_empty_live_token_keeps_seed(self) -> None:
        # The poll-path `or self._refresh_token` fallback must not wipe a good seed if the
        # live token momentarily reads "" (pins the poll-boundary wipe-protection).
        c = HonClient(email="e@x", password="p", refresh_token="RT_OLD")
        sess = FakeSession([])  # no refresh_token attr -> property returns ""
        c._api = sess
        c._hon_instance = sess
        asyncio.run(c.async_get_appliances_data())
        self.assertEqual("RT_OLD", c._refresh_token)

    def test_setup_sync_without_subscribe_does_not_crash(self) -> None:
        # The constructor MUST init _notify_function: setup_sync runs at initial
        # setup BEFORE subscribe_updates is ever called, and reads it for the
        # re-apply. Without the init this raises AttributeError.
        import custom_components.addhon.client.factory as factory
        new_session = FakeSession([])
        orig_create = getattr(factory, "create_session", None)
        factory.create_session = lambda email, password, **kw: new_session
        try:
            c = HonClient(email="e@x", password="p")  # never subscribed
            c._start_hon_loop = lambda: None  # type: ignore[assignment]
            c._run_on_hon_loop = lambda coro, timeout=None: coro.close()  # type: ignore[assignment]
            c.setup_sync()  # must NOT raise
            self.assertIs(c._hon_instance, new_session)
            self.assertIsNone(new_session._notify_function)  # nothing to apply
        finally:
            if orig_create is not None:
                factory.create_session = orig_create

    def test_setup_sync_failure_closes_and_keeps_callback(self) -> None:
        # On a failed (re)setup the session is closed (_hon_instance cleared) but the
        # stored callback PERSISTS on the client, ready for the next setup (#20).
        import custom_components.addhon.client.factory as factory

        class BoomSession(FakeSession):
            async def __aenter__(self):
                raise RuntimeError("login boom")

        boom = BoomSession([])
        orig_create = getattr(factory, "create_session", None)
        factory.create_session = lambda email, password, **kw: boom
        try:
            c = HonClient(email="e@x", password="p")
            cb = lambda _a: None  # noqa: E731
            c.subscribe_updates(cb)
            c._start_hon_loop = lambda: None  # type: ignore[assignment]
            c._run_on_hon_loop = lambda coro, timeout=None: asyncio.run(coro)  # type: ignore[assignment]
            with self.assertRaises(RuntimeError):
                c.setup_sync()
            self.assertIsNone(c._hon_instance)  # _close_sync ran on failure
            self.assertIs(c._notify_function, cb)  # callback survives for next setup
        finally:
            if orig_create is not None:
                factory.create_session = orig_create

    def test_build_realtime_snapshot_from_memory(self) -> None:
        app = FakeAppliance("ac-1")
        c = _client([app])
        snap = c.build_realtime_snapshot()
        self.assertIn("ac-1", snap)
        self.assertIs(snap["ac-1"]["appliance"], app)
        self.assertEqual(snap["ac-1"]["settings"], {"s": 1})

    def test_build_realtime_snapshot_empty_without_session(self) -> None:
        c = HonClient(email="e@x", password="p")  # _hon_instance is None
        self.assertEqual(c.build_realtime_snapshot(), {})

    def test_build_realtime_snapshot_skips_raising_appliance(self) -> None:
        # Runs on the awscrt thread: one appliance raising must be skipped, never
        # take down the whole snapshot.
        class BadAppliance:
            unique_id = "bad"
            nick_name = "x"
            statistics = {}

            @property
            def attributes(self):
                raise RuntimeError("boom")

        good = FakeAppliance("good")
        c = _client([BadAppliance(), good])
        snap = c.build_realtime_snapshot()
        self.assertIn("good", snap)
        self.assertNotIn("bad", snap)

    def test_build_appliance_entry_has_all_keys(self) -> None:
        # Pin the shared shape so the realtime snapshot and the HTTP poll never
        # diverge (the reason _build_appliance_entry was extracted).
        entry = HonClient._build_appliance_entry(FakeAppliance("x"))
        self.assertEqual(
            set(entry),
            {"appliance", "type", "name", "model", "serial", "mac",
             "attributes", "statistics", "settings"},
        )

    def test_notify_roundtrip_invokes_callback(self) -> None:
        # subscribe_updates registers on the session; session.notify() (called by the
        # MQTT push) must invoke the callback.
        c = _client()
        calls = []
        c.subscribe_updates(lambda _arg: calls.append(True))
        c._hon_instance.notify()
        self.assertEqual(calls, [True])


class DiscoveryLogRedactionTest(unittest.TestCase):
    """The poll/discovery DEBUG logs (incl. the 'Updated' line) must redact the
    MAC, the appliance_id (= MAC/serial) and the nick_name, never log them raw."""

    def test_discovery_and_updated_logs_redact_identity(self) -> None:
        mac = "AA:BB:CC:DD:EE:FF"
        nick = "NickSecret42"
        app = types.SimpleNamespace(
            mac_address=mac,
            unique_id=mac,
            appliance_type="REF",
            nick_name=nick,
            attributes={},
            settings={},
            statistics={},
        )
        c = HonClient(email="e@x", password="p")

        async def fake_get_appliances():
            return [app]

        c.async_get_appliances = fake_get_appliances  # type: ignore[assignment]
        c._update_appliance_sync = lambda a: None  # type: ignore[assignment]

        logger = "custom_components.addhon.hon_client"
        with self.assertLogs(logger, level="DEBUG") as cm:
            data = asyncio.run(c.async_get_appliances_data())
        blob = "\n".join(cm.output)
        self.assertNotIn(mac, blob)  # neither mac= nor id= leak the MAC
        self.assertNotIn(nick, blob)  # nick_name (= identity) must not leak either
        self.assertIn("***", blob)
        self.assertTrue(any("Updated" in line for line in cm.output))
        # the coordinator DATA still carries the real MAC + nick (data, not a log)
        self.assertIn(mac, data)
        self.assertEqual(data[mac]["name"], nick)

    def test_update_error_warning_redacts_nick_name(self) -> None:
        # The per-appliance error path logs a WARNING with the nick_name and (first
        # poll) re-raises a coded error (CR#6: HonCodedError preserving the cause).
        # Neither the WARNING nor the raised error must leak the nick.
        nick = "NickSecret42"
        app = types.SimpleNamespace(
            mac_address="AA:BB:CC:DD:EE:FF",
            unique_id="AA:BB:CC:DD:EE:FF",
            appliance_type="REF",
            nick_name=nick,
            attributes={},
            settings={},
            statistics={},
        )
        c = HonClient(email="e@x", password="p")

        async def fake_get_appliances():
            return [app]

        def boom(_a):
            raise ValueError("update boom")

        c.async_get_appliances = fake_get_appliances  # type: ignore[assignment]
        c._update_appliance_sync = boom  # type: ignore[assignment]

        logger = "custom_components.addhon.hon_client"
        with self.assertLogs(logger, level="WARNING") as cm:
            with self.assertRaises(HonCodedError) as ctx:
                asyncio.run(c.async_get_appliances_data())
        blob = "\n".join(cm.output)
        self.assertTrue(any("Error updating" in line for line in cm.output))
        self.assertNotIn(nick, blob)  # WARNING must not leak the nick
        self.assertNotIn(nick, str(ctx.exception))  # nor the raised coded error

    def test_steady_state_partial_failure_warning_redacts_nick(self) -> None:
        # Steady state (resilient): one appliance fails, the other succeeds. The
        # 'Partial update' WARNING joins the failed appliances' names -> must redact.
        nick_ok, nick_bad = "OkNick", "BadSecretNick99"
        app_ok = types.SimpleNamespace(
            mac_address="AA:BB:CC:DD:EE:01", unique_id="AA:BB:CC:DD:EE:01",
            appliance_type="REF", nick_name=nick_ok,
            attributes={}, settings={}, statistics={},
        )
        app_bad = types.SimpleNamespace(
            mac_address="AA:BB:CC:DD:EE:02", unique_id="AA:BB:CC:DD:EE:02",
            appliance_type="REF", nick_name=nick_bad,
            attributes={}, settings={}, statistics={},
        )
        c = HonClient(email="e@x", password="p")
        c._first_poll_done = True  # steady state -> skip a failed appliance, keep the rest

        async def fake_get_appliances():
            return [app_ok, app_bad]

        def update(a):
            if a is app_bad:
                raise ValueError("update boom")

        c.async_get_appliances = fake_get_appliances  # type: ignore[assignment]
        c._update_appliance_sync = update  # type: ignore[assignment]

        logger = "custom_components.addhon.hon_client"
        with self.assertLogs(logger, level="WARNING") as cm:
            data = asyncio.run(c.async_get_appliances_data())
        blob = "\n".join(cm.output)
        self.assertTrue(any("Partial update" in line for line in cm.output))
        self.assertNotIn(nick_bad, blob)  # neither the per-appliance nor the joined list
        self.assertIn(app_ok.unique_id, data)  # the healthy appliance survives

    def test_steady_state_total_failure_warning_redacts_nick(self) -> None:
        # Steady state, EVERY appliance fails (CR#6): the all-failed summary WARNING
        # joins the failed names (must redact) and the raised coded error carries no
        # identity in its message.
        nick1, nick2 = "SecretNickA1", "SecretNickB2"
        app1 = types.SimpleNamespace(
            mac_address="AA:BB:CC:DD:EE:11", unique_id="AA:BB:CC:DD:EE:11",
            appliance_type="REF", nick_name=nick1,
            attributes={}, settings={}, statistics={},
        )
        app2 = types.SimpleNamespace(
            mac_address="AA:BB:CC:DD:EE:12", unique_id="AA:BB:CC:DD:EE:12",
            appliance_type="REF", nick_name=nick2,
            attributes={}, settings={}, statistics={},
        )
        c = HonClient(email="e@x", password="p")
        c._first_poll_done = True  # steady state -> all fail -> all-failed branch

        async def fake_get_appliances():
            return [app1, app2]

        c.async_get_appliances = fake_get_appliances  # type: ignore[assignment]
        c._update_appliance_sync = lambda a: (_ for _ in ()).throw(ValueError("update boom"))

        logger = "custom_components.addhon.hon_client"
        with self.assertLogs(logger, level="WARNING") as cm:
            with self.assertRaises(HonCodedError) as ctx:
                asyncio.run(c.async_get_appliances_data())
        blob = "\n".join(cm.output)
        self.assertTrue(any("Update failed for all" in line for line in cm.output))
        self.assertNotIn(nick1, blob)  # summary WARNING must redact the joined names
        self.assertNotIn(nick2, blob)
        self.assertNotIn(nick1, str(ctx.exception))  # nor the raised coded error
        self.assertNotIn(nick2, str(ctx.exception))

    def test_the_all_failed_wrapper_forwards_the_phase_of_its_cause(self) -> None:
        # `__init__.py` reads `phase` off the wrapper and never walks `__cause__`, so a
        # wrapper that drops it files phase=null in Download Diagnostics for a cause that
        # knew where it died. The first-poll twin already forwards it; this is the
        # steady-state sibling that used to lose it.
        from custom_components.addhon import error_codes as ec

        app = types.SimpleNamespace(
            mac_address="AA:BB:CC:DD:EE:11", unique_id="AA:BB:CC:DD:EE:11",
            appliance_type="REF", nick_name="n",
            attributes={}, settings={}, statistics={},
        )
        c = HonClient(email="e@x", password="p")
        c._first_poll_done = True

        async def fake_get_appliances():
            return [app]

        coded = ec.HonCodedError(ec.NETWORK_TIMEOUT, phase="load_appliance/auth")

        def boom(_appliance):
            raise coded

        c.async_get_appliances = fake_get_appliances  # type: ignore[assignment]
        c._update_appliance_sync = boom  # type: ignore[assignment]

        with self.assertRaises(HonCodedError) as ctx:
            asyncio.run(c.async_get_appliances_data())
        self.assertIs(coded, ctx.exception.__cause__)
        self.assertEqual("load_appliance/auth", ctx.exception.phase)


class RealtimeWiringSourceGuard(unittest.TestCase):
    """The cross-thread wiring in async_setup_entry can't be exercised by the stub
    harness; guard its essential pieces at the source level."""

    _COMPONENT = REPO / "custom_components" / "addhon"

    def test_init_wires_push_via_call_soon_threadsafe(self) -> None:
        src = (self._COMPONENT / "__init__.py").read_text(encoding="utf-8")
        # push wired to the client...
        self.assertIn("subscribe_updates(", src)
        # ...and marshalled onto the HA loop (NOT a direct coordinator call from the
        # awscrt thread, which would be unsafe).
        self.assertIn("call_soon_threadsafe", src)
        self.assertIn("async_set_updated_data", src)
        # detached on unload
        self.assertIn("subscribe_updates(None)", src)

    def test_init_does_not_repoll_on_push(self) -> None:
        src = (self._COMPONENT / "__init__.py").read_text(encoding="utf-8")
        # The realtime publish must use the snapshot, not async_request_refresh
        # (which would re-trigger the slow HTTP poll on every push).
        self.assertIn("build_realtime_snapshot", src)
        # Scope the no-repoll guard to the realtime push wiring only: the
        # domain-wide addhon.refresh service legitimately calls
        # async_request_refresh elsewhere (the explicit "Refresh now" path), so a
        # whole-file ban would be a false positive. The region is from the
        # _publish_realtime definition up to the unload detachment that closes the
        # realtime wiring -- exactly the push path that must never re-poll.
        start = src.index("def _publish_realtime")
        end = src.index("subscribe_updates(None)", start)
        push_region = src[start:end]
        self.assertIn("async_set_updated_data", push_region)
        self.assertNotIn("async_request_refresh", push_region)

    def test_hon_client_exposes_realtime_api(self) -> None:
        src = (self._COMPONENT / "hon_client.py").read_text(encoding="utf-8")
        self.assertIn("def subscribe_updates", src)
        self.assertIn("def build_realtime_snapshot", src)


if __name__ == "__main__":
    unittest.main()
