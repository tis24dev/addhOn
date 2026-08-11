# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-phase budgets and the attribution they buy (issue #76).

The reported symptom was "Validation failed [ADDHON-400]: ADDHON-400: Network
timeout contacting hOn" from the config flow. It was produced by ONE cumulative 60s
cap covering the whole setup, expiring while the LAZY sign-in was still running, and
being attributed to `load_appliances` -- the label the caller had written down before
the request that triggered the login.

EVERY reproduction here enters from `NativeHon.setup()`. That is not a stylistic
preference: the first attempt at #76 shipped with a green suite because its
"reproduction" called `connection._check_headers` under a bare `phase()` scope,
WITHOUT the `budgeted(APPLIANCE_LIST)` that `setup()` wraps around it -- so it
verified a nesting production never builds, while production went on answering
ADDHON-400. The nesting IS the contract, so the test has to build it the way
`setup()` does, from `setup()`.

The doubles stop at the network: a fake `aiohttp.ClientSession` and a stalling
`HonAuth`. Everything between `setup()` and them -- `HonApi`, `HonConnection._intercept`,
`_check_headers`, the phase scopes, the budgets -- is the shipped code.
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

from _golden import install_stubs  # noqa: E402

install_stubs()

from custom_components.addhon import error_codes as ec  # noqa: E402
from custom_components.addhon.client import budget  # noqa: E402
from custom_components.addhon.client import session as session_mod  # noqa: E402
from custom_components.addhon.client.budget import budgeted  # noqa: E402
from custom_components.addhon.client.transport import connection as conn_mod  # noqa: E402
from custom_components.addhon.client.transport.api import HonApi  # noqa: E402
from custom_components.addhon.client.transport.connection import (  # noqa: E402
    HonConnection,
)
from custom_components.addhon.client.session import NativeHon  # noqa: E402

# Everything is scaled by the SAME factor, so the ORDERING of the budgets -- the only
# thing under test -- is preserved while a stall costs milliseconds instead of minutes.
# A stall is expressed as a sleep far longer than ANY budget (STALL below), so what a
# test measures is which budget fires, never how long the fake slept.
STALL = 30
SCALE = 0.005
# A slow POST for the case ADDHON-400 was always right for. Far over APPLIANCE_LIST
# (40 * SCALE = 0.2s) so the budget is what fires, and short enough that DELETING that
# budget turns the reproduction into a fast failure instead of a hang.
SLOW_POST = 1.0


def _run(coro):
    return asyncio.run(coro)


class _StallingAuth:
    """The Salesforce flow, reduced to its timing. Doubles the NETWORK, nothing else."""

    def __init__(
        self,
        refresh_seconds: float = 0.0,
        auth_seconds: float = 0.0,
        *,
        signed_in: bool = False,
    ) -> None:
        # `signed_in`: usable tokens already in RAM, so `_check_headers` opens NO scope
        # of its own and whatever happens next is the 401 recovery ladder alone.
        self.cognito_token = "cog" if signed_in else ""
        self.id_token = "id" if signed_in else ""
        self.refresh_token = "rt"
        self.token_expires_soon = False
        self.token_is_expired = False
        self._refresh_seconds = refresh_seconds
        self._auth_seconds = auth_seconds
        self.calls: list[str] = []

    async def refresh(self, refresh_token: str = "") -> bool:
        self.calls.append("refresh")
        if self._refresh_seconds:
            await asyncio.sleep(self._refresh_seconds)
        self.cognito_token = "cog"
        self.id_token = "id"
        return True

    async def authenticate(self) -> None:
        self.calls.append("authenticate")
        if self._auth_seconds:
            await asyncio.sleep(self._auth_seconds)
        self.cognito_token = "cog"
        self.id_token = "id"


class _Resp:
    """An appliance-list response with one appliance; `status`/`delay` script the wire."""

    def __init__(self, status: int = 200, delay: float = 0.0) -> None:
        self.status = status
        self._delay = delay

    async def json(self, content_type=None):
        return {
            "modules": {
                "applianceList": {
                    "payload": {
                        "appliances": [
                            {"macAddress": "AA", "applianceTypeName": "WM"}
                        ]
                    }
                }
            }
        }

    async def __aenter__(self):
        # The stall belongs HERE: aiohttp hands back the response object once the
        # headers are in, so a slow endpoint is slow to enter, not slow to decode.
        if self._delay:
            await asyncio.sleep(self._delay)
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """Fake aiohttp.ClientSession: the real boundary, and the only other double.

    `statuses` scripts the wire per request -- a 401 is what drives the recovery ladder
    in `_intercept` (refresh, then re-login), the path a runtime token rejection takes.
    """

    def __init__(self, statuses: tuple[int, ...] = (), delay: float = 0.0) -> None:
        self.requests: list[str] = []
        self._statuses = list(statuses)
        self._delay = delay

    def _resp(self, url, **kw):
        self.requests.append(str(url))
        status = self._statuses.pop(0) if self._statuses else 200
        return _Resp(status=status, delay=self._delay)

    def get(self, url, **kw):
        return self._resp(url, **kw)

    def post(self, url, **kw):
        return self._resp(url, **kw)


class _KeepsItsAuthOnCreate(HonConnection):
    """A connection whose `create()` does not mint a real `HonAuth`.

    The re-login rung of the 401 ladder calls `create()`, which would replace the
    injected double with a real `HonAuth` driving the fake session through a Salesforce
    flow it does not script. Overriding it keeps the double where every other test in
    this file puts it -- at the network -- while `_intercept`, the ladder, the phase
    scopes and the budgets remain the shipped code.
    """

    async def create(self) -> "HonConnection":
        return self


class SetupHarness:
    """A REAL NativeHon over a real HonApi/HonConnection, with the network faked."""

    def __init__(
        self,
        test: unittest.TestCase,
        auth: _StallingAuth,
        refresh_token: str,
        *,
        session: _Session | None = None,
        connection_class: type = HonConnection,
    ):
        test.scale_budgets()
        self.session = session if session is not None else _Session()
        self.auth = auth
        self.hon = NativeHon(
            email="e@x", password="p", enable_mqtt=False, minimal=True
        )
        connection = connection_class(
            "e@x", "p", session=self.session, phase_tracker=self.hon._phase_tracker
        )
        connection._auth = auth
        connection._refresh_token = refresh_token
        self.hon._connection = connection
        self.hon._api = HonApi(connection)

    def setup(self):
        return _run(self.hon.setup())


class _ScaledBudgets(unittest.TestCase):
    """Shrinks every budget by SCALE, in every module that imported one by name."""

    def scale_budgets(self) -> None:
        for module, names in (
            (conn_mod, ("AUTH_FULL", "AUTH_REFRESH", "MFA_RESUME")),
            (session_mod, ("APPLIANCE_LIST", "APPLIANCE_ONE", "MQTT_START")),
        ):
            for name in names:
                original = getattr(module, name)
                setattr(module, name, original * SCALE)
                self.addCleanup(setattr, module, name, original)


class BudgetModelTest(unittest.TestCase):
    """Freeze the formula and the two-kinds-of-number model, not typed constants."""

    def test_formula(self) -> None:
        self.assertEqual(40, budget.budget(1))
        self.assertEqual(60, budget.budget(3))
        self.assertEqual(120, budget.budget(9))
        # The retry term is what keeps the budget from killing the retry just before
        # it is needed (transport/retry.py shares these constants).
        self.assertEqual(
            budget.budget(9) + 2 * (budget.TOTAL_TIMEOUT + budget.RETRY_DELAY),
            budget.AUTH_FULL,
        )

    def test_budget_never_fires_before_a_single_hop(self) -> None:
        # The invariant the tail margin exists for: one stuck request must expire on
        # its OWN aiohttp timeout (attributed, with a message), never on the budget.
        for value in (
            budget.AUTH_FULL,
            budget.AUTH_REFRESH,
            budget.APPLIANCE_LIST,
            budget.APPLIANCE_ONE,
            budget.MFA_RESUME,
        ):
            self.assertGreater(value, budget.TOTAL_TIMEOUT)

    def test_lazy_auth_is_the_whole_sign_in_ladder(self) -> None:
        # _check_headers tries the refresh and then falls back to the full login, in
        # that order, so what a lazy sign-in can cost is their SUM.
        self.assertEqual(budget.AUTH_REFRESH + budget.AUTH_FULL, budget.LAZY_AUTH)

    def test_every_cap_contains_one_lazy_sign_in(self) -> None:
        # A cap is waited on from ANOTHER thread (a concurrent.futures.Future), so it
        # cannot be suspended the way a scope budget is: it has to CONTAIN the sign-in
        # a request may start, or it fires first and destroys the attribution -- the
        # #76 failure mode, one level up.
        for name in ("VALIDATION_CAP", "SETUP_CAP", "COMMAND", "APPLIANCE_POLL"):
            with self.subTest(cap=name):
                self.assertGreaterEqual(
                    getattr(budget, name), budget.LAZY_AUTH + budget.budget(1)
                )
        # Teardown is the exception on purpose: it must never wait like a setup.
        self.assertLess(budget.CLOSE, budget.VALIDATION_CAP)

    def test_the_command_cap_no_longer_truncates_the_refresh_it_waits_for(self) -> None:
        # It was budget(1) = 40s, TIGHTER than the AUTH_REFRESH (50s) that a rejected
        # token makes those very call sites open. A 45s refresh that completed under
        # the old 60s run cap was being killed at 40s.
        self.assertGreater(budget.COMMAND, budget.AUTH_REFRESH)
        self.assertGreater(budget.COMMAND, budget.AUTH_FULL)

    def test_teardown_is_shorter_than_anything_it_can_interrupt(self) -> None:
        # CLOSE is the ONE constant in the module that is invented rather than derived
        # from budget(), so nothing but this fixes its VALUE: the call-site tests only
        # check that `_close_sync` passes `budget.CLOSE`, never what it is worth, and a
        # drift back to the legacy 60s left the whole suite green. What the call site
        # claims is "teardown must never wait as long as a setup", and an unload that
        # sits through even ONE full request is the slow-unload symptom the narrow
        # lifecycle lock exists to remove.
        self.assertLess(budget.CLOSE, budget.TOTAL_TIMEOUT)
        for name in (
            "APPLIANCE_LIST",
            "APPLIANCE_ONE",
            "AUTH_REFRESH",
            "AUTH_FULL",
            "MFA_RESUME",
            "MQTT_START",
            "COMMAND",
            "VALIDATION_CAP",
            "SETUP_CAP",
            "APPLIANCE_POLL",
        ):
            with self.subTest(budget=name):
                self.assertLess(budget.CLOSE, getattr(budget, name))

    def test_setup_cap_is_the_declared_sum(self) -> None:
        # Arithmetic stated out loud, because it has been mis-stated before.
        self.assertEqual(284, budget.VALIDATION_CAP)
        self.assertEqual(574, budget.SETUP_CAP)
        self.assertEqual(
            budget.VALIDATION_CAP + budget.MQTT_START + 4 * budget.APPLIANCE_ONE,
            budget.SETUP_CAP,
        )


class CallSiteWatchdogTest(unittest.TestCase):
    """Freeze the site -> watchdog table, so adding a call site forces a choice."""

    def _client(self, seen: list, **client_kwargs):
        import custom_components.addhon.client.factory as factory
        from custom_components.addhon.hon_client import HonClient

        class _FakeSession:
            refresh_token = "rt"

            async def __aenter__(self):
                return self

            def subscribe_updates(self, fn):
                pass

        original = factory.create_session
        factory.create_session = lambda email, password, **kw: _FakeSession()
        self.addCleanup(setattr, factory, "create_session", original)

        client = HonClient(email="e@x", password="p", **client_kwargs)
        client._start_hon_loop = lambda: None  # type: ignore[assignment]

        def run(coro, timeout=None):
            seen.append(timeout)
            if hasattr(coro, "close"):
                coro.close()

        client._run_on_hon_loop = run  # type: ignore[assignment]
        return client

    def test_validation_gets_the_tighter_watchdog(self) -> None:
        # A human is waiting on the config-flow form, so the validation path must not
        # inherit the runtime watchdog sized for N appliances plus MQTT.
        seen: list = []
        self._client(seen, validation=True).setup_sync()
        self.assertEqual(budget.VALIDATION_CAP, seen[0])

    def test_runtime_setup_gets_the_wider_watchdog(self) -> None:
        seen: list = []
        self._client(seen).setup_sync()
        self.assertEqual(budget.SETUP_CAP, seen[0])

    def test_a_user_command_gets_the_command_watchdog(self) -> None:
        # A command is one POST, but a rejected token makes it sign in inline, and this
        # cap is waited on from another thread so it cannot be suspended: handing this
        # site a smaller constant truncates exactly the re-login it is waiting for.
        seen: list = []
        client = self._client(seen)

        async def _send() -> None:
            return None

        client.run_command_sync(_send())
        self.assertEqual(budget.COMMAND, seen[0])

    def test_a_patch_dispatch_gets_the_command_watchdog(self) -> None:
        from custom_components.addhon.command_dispatch import CommandPatch

        seen: list = []
        client = self._client(seen)
        client.dispatch_patch_sync(
            object(), CommandPatch(command_name="startProgram", values={}, action="send")
        )
        self.assertEqual(budget.COMMAND, seen[0])

    def test_the_otp_resend_gets_the_command_watchdog(self) -> None:
        seen: list = []
        client = self._client(seen)

        class _Pending:
            async def resend_mfa_code(self, context):
                return None

        client._hon_instance = _Pending()
        client.resend_mfa_code_sync(object())
        self.assertEqual(budget.COMMAND, seen[0])

    def test_teardown_gets_the_short_watchdog(self) -> None:
        # Teardown must never wait like a setup: on the wrong constant an unload would
        # hold Home Assistant for minutes instead of seconds.
        seen: list = []
        client = self._client(seen)

        class _Open:
            async def __aexit__(self, *exc):
                return False

        client._hon_instance = _Open()
        client._close_sync()
        self.assertEqual(budget.CLOSE, seen[0])

    def test_a_polled_appliance_gets_the_poll_watchdog(self) -> None:
        # Where the F8 regression actually lives: not in the arithmetic of the
        # constants but in the site that spends them. APPLIANCE_ONE here (60s) would put
        # the poll watchdog back UNDER the sign-in a rejected token starts inline --
        # the #76 shape, one level up, on the path that runs every minute.
        seen: list = []
        client = self._client(seen)
        client._update_appliance_sync(object())
        self.assertEqual(budget.APPLIANCE_POLL, seen[0])

    def test_the_2fa_resume_follows_the_same_split_as_setup(self) -> None:
        # The 2FA step of the CONFIG FLOW was handed the runtime watchdog (654s), which
        # contradicts the reason the two were split in the first place.
        for validation, expected in (
            (True, budget.MFA_RESUME + budget.VALIDATION_CAP),
            (False, budget.MFA_RESUME + budget.SETUP_CAP),
        ):
            with self.subTest(validation=validation):
                seen: list = []
                client = self._client(seen, validation=validation)

                class _Pending:
                    async def submit_mfa_code(self, context, code):
                        return None

                client._hon_instance = _Pending()
                client.submit_mfa_code_sync(object(), "000000")
                self.assertEqual(expected, seen[0])


class CallSiteTableIsCompleteTest(unittest.TestCase):
    """The other half of the promise above: a NEW call site cannot slip in unpinned.

    The tests above pin the constant each KNOWN site passes. Nothing pinned the SET of
    sites, so `_run_on_hon_loop(coro)` with no watchdog at all -- which silently means
    the legacy 60s, the single cumulative cap #76 is about -- would have been added
    green. Reading the source is the point: a behavioural test can only cover the sites
    someone remembered to write one for.
    """

    EXPECTED = {
        "_close_sync": "budget.CLOSE",
        "setup_sync": "budget.VALIDATION_CAP if self._validation else budget.SETUP_CAP",
        "submit_mfa_code_sync": (
            "budget.MFA_RESUME + "
            "(budget.VALIDATION_CAP if self._validation else budget.SETUP_CAP)"
        ),
        "resend_mfa_code_sync": "budget.COMMAND",
        "run_command_sync": "budget.COMMAND",
        "dispatch_patch_sync": "budget.COMMAND",
        "_update_appliance_sync": "budget.APPLIANCE_POLL",
    }

    @staticmethod
    def _call_sites() -> dict:
        import ast

        from custom_components.addhon import hon_client as hon_client_mod

        tree = ast.parse(Path(hon_client_mod.__file__).read_text(encoding="utf-8"))
        sites: dict[str, list[ast.Call]] = {}

        def visit(node, owner: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name)
                    continue
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "_run_on_hon_loop"
                ):
                    sites.setdefault(owner, []).append(child)
                visit(child, owner)

        visit(tree, "<module>")
        return sites

    def test_every_call_site_passes_an_explicit_watchdog(self) -> None:
        import ast

        sites = self._call_sites()
        self.assertEqual(sorted(self.EXPECTED), sorted(sites))
        for owner, calls in sites.items():
            with self.subTest(call_site=owner):
                self.assertEqual(1, len(calls))
                call = calls[0]
                # A site that omits the argument inherits the legacy 60s cap, which is
                # the single cumulative number #76 was filed against.
                self.assertEqual(2, len(call.args), f"{owner} chose no watchdog")
                expected = ast.unparse(
                    ast.parse(self.EXPECTED[owner], mode="eval").body
                )
                self.assertEqual(expected, ast.unparse(call.args[1]))


class BudgetedScopeTableIsCompleteTest(unittest.TestCase):
    """The same promise as the table above, for the SCOPES rather than the caps.

    Every phase inside a cap is supposed to bound itself -- that is the whole model:
    "a cap only has to catch a loop that stopped progressing at all". Nothing verified
    that the set of `budgeted()` scopes was complete, and it was not: the MQTT start was
    SUMMED into SETUP_CAP while never opening a scope, so a stalled first connect ran for
    the whole 574s cap instead of its own 50s. Nothing pinned which budget each scope
    passes either, so a scope could quietly be given the teardown number.

    `transport/connection.py` is covered by AuthScopesSuspendTheCallerTest below, which
    also pins the suspension flag those scopes need.
    """

    EXPECTED = {
        # module basename -> innermost function -> budgets, in source order
        "session.py": {
            "setup": ["APPLIANCE_LIST", "MQTT_START"],
            "_create_appliance": ["APPLIANCE_ONE"],
        },
        "hon_client.py": {
            # The first-poll rehydration, under the same pair `_build_appliance` opens.
            "_do_update": ["budget.APPLIANCE_ONE"],
        },
    }

    @staticmethod
    def _scopes(module) -> dict:
        import ast

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        found: dict[str, list[str]] = {}

        def visit(node, owner: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name)
                    continue
                if isinstance(child, ast.Call) and (
                    (isinstance(child.func, ast.Name) and child.func.id == "budgeted")
                    or (
                        isinstance(child.func, ast.Attribute)
                        and child.func.attr == "budgeted"
                    )
                ):
                    found.setdefault(owner, []).append(ast.unparse(child.args[0]))
                visit(child, owner)

        visit(tree, "<module>")
        return found

    def test_every_budgeted_scope_is_pinned_to_its_phase(self) -> None:
        from custom_components.addhon import hon_client as hon_client_mod

        for module in (session_mod, hon_client_mod):
            name = Path(module.__file__).name
            with self.subTest(module=name):
                self.assertEqual(self.EXPECTED[name], self._scopes(module))


class AuthScopesSuspendTheCallerTest(unittest.TestCase):
    """Every auth scope in the transport is work the CALLER never asked for.

    The sign-in is LAZY: it starts inside whatever request happened to need a token,
    so its budget nests inside a caller's budget that is deliberately smaller. Each of
    these scopes therefore has to suspend the scopes it interrupts, or the outer one
    fires first and the login is reported as "load_appliances timed out" -- #76 itself.
    Two of them are on the 401 recovery ladder and one is the 2FA resume, and a
    behavioural test can only reach a scope the production nesting can build today:
    the table is what makes DROPPING the flag at any of the five a test failure.
    """

    EXPECTED = {
        "_check_headers": [("AUTH_REFRESH", True), ("AUTH_FULL", True)],
        "_refresh_after_rejection": [("AUTH_REFRESH", True)],
        "_reauth_after_rejection": [("AUTH_FULL", True)],
        "submit_mfa_code": [("MFA_RESUME", True)],
    }

    def test_every_transport_auth_scope_suspends_its_caller(self) -> None:
        import ast

        tree = ast.parse(Path(conn_mod.__file__).read_text(encoding="utf-8"))
        found: dict[str, list[tuple[str, bool]]] = {}

        def visit(node, owner: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(child, child.name)
                    continue
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "_budgeted"
                ):
                    suspends = False
                    for keyword in child.keywords:
                        if keyword.arg == "suspends_caller":
                            suspends = getattr(keyword.value, "value", False) is True
                    found.setdefault(owner, []).append(
                        (ast.unparse(child.args[0]), suspends)
                    )
                visit(child, owner)

        visit(tree, "<module>")
        self.assertEqual(self.EXPECTED, found)


class SuspendedBudgetTest(unittest.IsolatedAsyncioTestCase):
    """The mechanism the whole model rests on, exercised directly."""

    async def test_a_nested_sign_in_does_not_spend_the_callers_budget(self) -> None:
        # Enclosing 0.4s, nested "sign-in" budgeted 1.5s that really takes 0.5s. Under
        # plain nested timeouts the outer one fires at 0.4s; suspended, it is charged
        # only the 0.5s the interruption cost and still has its own work to do.
        async with budgeted(0.4):
            async with budgeted(1.5, suspends_caller=True):
                await asyncio.sleep(0.5)
            await asyncio.sleep(0.2)  # the caller's OWN work, after the interruption

    async def test_the_caller_still_expires_on_its_own_work(self) -> None:
        # The suspension gives back what the sign-in did not use, so it cannot be
        # abused as an unlimited extension of the caller's budget.
        #
        # The numbers are DIFFERENTIAL on purpose. The caller has 0.4s and lends 1.5s;
        # the sign-in returns after 0.05s, so with the give-back it is left with ~0.35s
        # and 0.8s of own work must expire, while WITHOUT it the deadline would sit at
        # 0.4+1.5=1.9s and 0.85s would sail through. With the 2s of own work this test
        # used to do, both sides raised and deleting the give-back left the suite green
        # -- the whole mechanism of the fix was unprotected.
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertRaises(ec.HonCodedError):
            async with budgeted(0.4):
                async with budgeted(1.5, suspends_caller=True):
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.8)
        self.assertLess(loop.time() - started, 1.0)

    async def test_the_nested_budget_still_bounds_the_sign_in(self) -> None:
        # Suspending the caller must not make the inner scope unbounded. The elapsed
        # assertion is what makes this about the INNER scope: the 2.0s outer one raises
        # the very same HonCodedError, one and a half seconds later.
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertRaises(ec.HonCodedError):
            async with budgeted(2.0):
                async with budgeted(0.2, suspends_caller=True):
                    await asyncio.sleep(5)
        self.assertLess(loop.time() - started, 1.0)

    async def test_a_closed_scope_leaves_no_deadline_behind(self) -> None:
        # `budgeted()` is an @asynccontextmanager, so its `_ACTIVE.set()` lands in the
        # CALLER's context (an async generator gets no context of its own). Drop the
        # matching reset and a FINISHED asyncio.Timeout stays on the stack for the rest
        # of the task: `current_deadline()` then answers an instant already in the PAST,
        # and the login retry gate that reads it refuses every retry from then on --
        # round 1's F3 defect, in the opposite direction.
        #
        # Reachable in one straight line: setup() closes budgeted(APPLIANCE_LIST) and
        # then awaits _create_appliance, which opens budgeted(APPLIANCE_ONE), in the
        # very same coroutine.
        async with budgeted(0.5):
            self.assertEqual(1, len(budget._ACTIVE.get()))
        self.assertEqual((), budget._ACTIVE.get())
        self.assertIsNone(budget.current_deadline())
        # ...and the NEXT scope's deadline is its own, not one inherited from a corpse.
        async with budgeted(10):
            remaining = budget.current_deadline() - time.monotonic()
        self.assertGreater(remaining, 5)

    async def test_a_sign_in_suspends_every_enclosing_scope_not_just_its_parent(
        self,
    ) -> None:
        # `_ACTIVE` is a STACK, and it has to be: a sign-in suspends the scopes it
        # interrupts, plural. Collapse it to a single slot and only the immediate parent
        # is suspended, so a grandparent truncates the caller exactly the way
        # APPLIANCE_LIST truncated AUTH_FULL in #76. Every other suspension test here is
        # two levels deep, and two levels cannot tell a stack from one slot.
        #
        # 0.60 outer / 0.55 middle; a sign-in budgeted 0.30 that really takes 0.25, then
        # 0.45s of the caller's own work. Suspended properly both enclosing deadlines end
        # at ~0.85/0.80 against 0.70 elapsed; with only the parent suspended the outer one
        # still sits at 0.60 and fires.
        async with budgeted(0.60):
            self.assertEqual(1, len(budget._ACTIVE.get()))
            async with budgeted(0.55):
                # The shape, stated directly: the enclosing scope is still on the stack.
                self.assertEqual(2, len(budget._ACTIVE.get()))
                async with budgeted(0.30, suspends_caller=True):
                    await asyncio.sleep(0.25)
                await asyncio.sleep(0.45)

    async def test_a_plain_nested_scope_does_not_suspend_its_caller(self) -> None:
        # The default of `suspends_caller` IS the scope-budget/CAP distinction the whole
        # fix rests on: "a scope budget does not suspend unless it is work the caller
        # never asked for". Flipping it to True is inert TODAY only because the three
        # sites that omit the flag happen to be outermost in their task -- so nothing
        # noticed, and the invariant rested on nobody ever nesting a plain scope.
        import inspect

        self.assertIs(
            False, inspect.signature(budgeted).parameters["suspends_caller"].default
        )
        async with budgeted(0.6):
            enclosing = budget._ACTIVE.get()[0]
            before = enclosing.when()
            async with budgeted(0.2):
                self.assertEqual(before, enclosing.when())
            self.assertEqual(before, enclosing.when())

    async def test_current_deadline_reports_the_tightest_open_scope(self) -> None:
        # This is what the login retry gate must read. Rebuilding it from AUTH_FULL
        # made the gate compare against a deadline nobody enforced.
        self.assertIsNone(budget.current_deadline())
        async with budgeted(10):
            outer = budget.current_deadline()
            self.assertIsNotNone(outer)
            async with budgeted(0.5):
                inner = budget.current_deadline()
                self.assertLess(inner, outer)


class Issue76ReproductionTest(_ScaledBudgets):
    """#76 driven through NativeHon.setup(), the way production builds the nesting."""

    def test_slow_login_inside_setup_is_reported_as_a_sign_in_timeout(self) -> None:
        # THE #76 REPRODUCTION. Before: the outer APPLIANCE_LIST scope fired at 40s,
        # `current_phase()` had already unwound to "load_appliances", and the user was
        # told the NETWORK had timed out while the LOGIN was still running.
        harness = SetupHarness(
            self, _StallingAuth(auth_seconds=STALL), refresh_token=""
        )
        with self.assertRaises(ec.HonCodedError) as ctx:
            harness.setup()
        self.assertIs(ec.AUTH_TIMEOUT, ctx.exception.error_code)
        self.assertEqual("load_appliances/auth", ctx.exception.phase)
        # Not a credentials problem: it must stay retryable, never open a reauth.
        self.assertFalse(ctx.exception.error_code.requires_reauth)

    def test_slow_refresh_inside_setup_is_reported_as_a_refresh_timeout(self) -> None:
        harness = SetupHarness(
            self, _StallingAuth(refresh_seconds=STALL), refresh_token="rt"
        )
        with self.assertRaises(ec.HonCodedError) as ctx:
            harness.setup()
        self.assertIs(ec.REFRESH_TIMEOUT, ctx.exception.error_code)
        self.assertEqual("load_appliances/auth/refresh", ctx.exception.phase)

    def test_a_login_slower_than_the_appliance_list_budget_still_completes(self) -> None:
        # The REGRESSION the first attempt introduced: a 45s login fitted under the old
        # single 60s cap and then stopped fitting under APPLIANCE_LIST=40s -- the same
        # user, the same network, a NEW failure. A sign-in is entitled to the sign-in
        # budget wherever it happens to start.
        slow = 45 * SCALE
        self.assertGreater(slow, budget.APPLIANCE_LIST * SCALE)
        self.assertLess(slow, budget.AUTH_FULL * SCALE)
        harness = SetupHarness(self, _StallingAuth(auth_seconds=slow), refresh_token="")
        harness.setup()
        self.assertEqual(1, len(harness.hon.appliances))

    def test_no_bare_timeout_escapes_a_budgeted_scope(self) -> None:
        # The conversion rule: a bare TimeoutError reaching the outer cap would carry
        # no phase and collapse to the mute ADDHON-460.
        harness = SetupHarness(
            self, _StallingAuth(auth_seconds=STALL), refresh_token=""
        )
        with self.assertRaises(ec.HonCodedError) as ctx:
            harness.setup()
        self.assertIsInstance(ctx.exception.__cause__, TimeoutError)

    def test_the_ledger_files_a_budget_expiry_as_a_timeout(self) -> None:
        # `budgeted` converts the expiry into a coded error while still INSIDE the
        # phase scope, so the ledger only ever saw a non-TimeoutError: the 'timeout'
        # outcome that diagnostics documents was unreachable in production, and every
        # expiry looked like an application error.
        harness = SetupHarness(
            self, _StallingAuth(refresh_seconds=STALL), refresh_token="rt"
        )
        with self.assertRaises(ec.HonCodedError):
            harness.setup()
        outcomes = {
            entry["phase"]: entry["outcome"] for entry in harness.hon.phase_ledger
        }
        self.assertEqual("timeout", outcomes["load_appliances/auth/refresh"])
        self.assertEqual("timeout", outcomes["load_appliances"])

    def test_a_slow_appliance_list_still_reports_400(self) -> None:
        # No regression on the case ADDHON-400 was always right for -- driven, not
        # looked up. The tokens are already usable, so no sign-in is involved: the POST
        # itself is slow, APPLIANCE_LIST is the budget that must fire and the phase must
        # stay "load_appliances". This used to be `assertIs(NETWORK_TIMEOUT,
        # phase_timeout_code("load_appliances"))`, a re-read of a table another file
        # already covers, and deleting the APPLIANCE_LIST scope from setup() left the
        # suite green.
        harness = SetupHarness(
            self,
            _StallingAuth(signed_in=True),
            refresh_token="",
            session=_Session(delay=SLOW_POST),
        )
        with self.assertRaises(ec.HonCodedError) as ctx:
            harness.setup()
        self.assertIs(ec.NETWORK_TIMEOUT, ctx.exception.error_code)
        self.assertEqual("load_appliances", ctx.exception.phase)
        self.assertEqual([], harness.auth.calls)

    def test_a_rejected_token_refreshing_inside_setup_is_a_refresh_timeout(self) -> None:
        # The 401 RECOVERY LADDER, which is how a token gets rejected in the field:
        # the refresh it opens is a sign-in like any other and must suspend the request
        # that triggered it. Without the suspension the APPLIANCE_LIST scope (40s) cuts
        # the AUTH_REFRESH (50s) short and the user is told the network timed out --
        # #76, on the recovery path.
        harness = SetupHarness(
            self,
            _StallingAuth(refresh_seconds=STALL, signed_in=True),
            refresh_token="",
            session=_Session(statuses=(401,)),
        )
        with self.assertRaises(ec.HonCodedError) as ctx:
            harness.setup()
        self.assertIs(ec.REFRESH_TIMEOUT, ctx.exception.error_code)
        self.assertEqual("load_appliances/auth/refresh", ctx.exception.phase)
        self.assertEqual(["refresh"], harness.auth.calls)

    def test_a_second_rejection_inside_setup_is_a_sign_in_timeout(self) -> None:
        # One rung further: the refresh worked but the retry is rejected too, so the
        # ladder re-logins. Same rule, the widest scope of the three (AUTH_FULL=184s
        # inside an APPLIANCE_LIST of 40s), and the same #76 symptom if it does not
        # suspend its caller.
        harness = SetupHarness(
            self,
            _StallingAuth(auth_seconds=STALL, signed_in=True),
            refresh_token="",
            session=_Session(statuses=(401, 401)),
            connection_class=_KeepsItsAuthOnCreate,
        )
        with self.assertRaises(ec.HonCodedError) as ctx:
            harness.setup()
        self.assertIs(ec.AUTH_TIMEOUT, ctx.exception.error_code)
        self.assertEqual("load_appliances/auth", ctx.exception.phase)
        self.assertEqual(["refresh", "authenticate"], harness.auth.calls)

    def test_one_request_can_open_three_sign_ins(self) -> None:
        # What `cap()` deliberately does NOT contain, stated by driving it. A single
        # request signs in lazily, then the ladder refreshes and re-logins: three
        # sign-ins, AUTH_FULL + AUTH_REFRESH + AUTH_FULL = 418s of allowance against
        # COMMAND = 284s. Sizing every cap for that chain would put a user command at
        # ~12 minutes and the setup watchdog past what Home Assistant waits for, to
        # cover the case where the tokens are rejected twice in a row.
        #
        # The stop is safe because of WHERE the cap then fires: inside the sign-in,
        # where the phase mirror reads "auth[/refresh]", so the user gets the attributed
        # and retryable ADDHON-405/406 -- never the ADDHON-400 of #76.
        harness = SetupHarness(
            self,
            _StallingAuth(),
            refresh_token="",
            session=_Session(statuses=(401, 401)),
            connection_class=_KeepsItsAuthOnCreate,
        )
        harness.setup()
        self.assertEqual(
            ["authenticate", "refresh", "authenticate"], harness.auth.calls
        )
        self.assertGreater(
            budget.AUTH_FULL + budget.AUTH_REFRESH + budget.AUTH_FULL, budget.COMMAND
        )
        self.assertIs(ec.AUTH_TIMEOUT, ec.phase_timeout_code("auth"))
        self.assertIs(ec.REFRESH_TIMEOUT, ec.phase_timeout_code("auth/refresh"))
        self.assertFalse(ec.AUTH_TIMEOUT.requires_reauth)

    def test_a_stalled_mqtt_start_expires_on_its_own_budget(self) -> None:
        # MQTT_START was SUMMED into SETUP_CAP and never applied as a scope: the one
        # phase inside that cap with no budget of its own. A first connect that stalled
        # was therefore bounded only by SETUP_CAP -- the config entry took ~10 minutes
        # to fail on a phase whose own number says 50s -- and the model's premise ("a
        # cap only has to catch a loop that stopped progressing at all") was false
        # exactly here. Now it fails on its own budget, named and coded.
        harness = SetupHarness(self, _StallingAuth(signed_in=True), "rt")
        hon = harness.hon
        hon._enable_mqtt = True

        async def _never_connects() -> None:
            await asyncio.sleep(STALL)

        hon._make_mqtt = _never_connects  # type: ignore[assignment]
        with self.assertRaises(ec.HonCodedError) as ctx:
            harness.setup()
        self.assertIs(ec.MQTT_CONNECT_TIMEOUT, ctx.exception.error_code)
        self.assertEqual("mqtt_start", ctx.exception.phase)
        # ...and the ledger says which phase burned the time, which is the whole point
        # of having a phase at all.
        self.assertIn(
            ("mqtt_start", "timeout"),
            [(entry["phase"], entry["outcome"]) for entry in hon.phase_ledger],
        )


if __name__ == "__main__":
    unittest.main()
