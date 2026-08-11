# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The hierarchical, transported setup phase (issue #76).

Pure test: `client/phase.py` imports nothing but the stdlib, so no Home Assistant /
aiohttp stubs are needed here. The behaviour that matters is the RESTORE on exit --
that is what stops a nested re-login from leaving the phase pointing at the auth
layer for the rest of the setup.
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "tests") not in sys.path:
    sys.path.insert(0, str(REPO / "tests"))

from _golden import install_stubs  # noqa: E402

# phase.py itself is pure stdlib; the stubs are only needed because importing it goes
# through the package __init__, which imports Home Assistant.
install_stubs()

from custom_components.addhon.client.phase import (  # noqa: E402
    PhaseTracker,
    current_phase,
    phase,
)


class PhaseCompositionTest(unittest.TestCase):
    def test_nesting_composes_and_restores(self) -> None:
        tracker = PhaseTracker()
        self.assertEqual("", current_phase())
        with phase("load_appliances", tracker):
            self.assertEqual("load_appliances", current_phase())
            self.assertEqual("load_appliances", tracker.current)
            with phase("auth/refresh", tracker):
                # The exact shape #76 could not express: the lazy sign-in nested inside
                # the appliance-list request names ITSELF.
                self.assertEqual("load_appliances/auth/refresh", current_phase())
                self.assertEqual("load_appliances/auth/refresh", tracker.current)
            # Restored -- the whole point.
            self.assertEqual("load_appliances", current_phase())
            self.assertEqual("load_appliances", tracker.current)
        self.assertEqual("", current_phase())
        self.assertEqual("", tracker.current)

    def test_restore_happens_on_exception_too(self) -> None:
        tracker = PhaseTracker()
        with phase("load_appliances", tracker):
            with self.assertRaises(RuntimeError):
                with phase("auth", tracker):
                    raise RuntimeError("boom")
            self.assertEqual("load_appliances", current_phase())

    def test_step_refines_the_mirror_without_pushing(self) -> None:
        tracker = PhaseTracker()
        with phase("load_appliances", tracker):
            with phase("auth", tracker):
                tracker.step("introduce")
                self.assertEqual("load_appliances/auth/introduce", tracker.current)
                # The ContextVar is untouched: `step` is a mirror refinement only.
                self.assertEqual("load_appliances/auth", current_phase())
            # Leaving the scope wipes the refinement; it can never outlive its scope.
            self.assertEqual("load_appliances", tracker.current)

    def test_gather_siblings_do_not_pollute_each_other(self) -> None:
        tracker = PhaseTracker()
        seen: dict[str, str] = {}

        async def worker(name: str) -> None:
            with phase(name, tracker):
                await asyncio.sleep(0)
                seen[name] = current_phase()

        async def main() -> None:
            with phase("load_appliance", tracker):
                await asyncio.gather(worker("a"), worker("b"), worker("c"))
                # The ContextVar of THIS task is untouched by the children.
                self.assertEqual("load_appliance", current_phase())

        asyncio.run(main())
        self.assertEqual(
            {
                "a": "load_appliance/a",
                "b": "load_appliance/b",
                "c": "load_appliance/c",
            },
            seen,
        )


class EveryAuthEntryPointOpensAScopeTest(unittest.TestCase):
    """No auth entry point may refine the mirror without a scope to restore it.

    `PhaseTracker.step()` (which the auth layer's markers use) writes the mirror
    directly; only the enclosing `phase()` puts back what it found. An entry point that
    reaches the auth layer WITHOUT a scope therefore leaves the mirror pointing at its
    last step for the life of the client -- and `_run_on_hon_loop` prefers that
    hierarchical mirror to the flat one, so the stale value SHIELDS the accurate one and
    every later expiry collapses to the mute ADDHON-460 (issue #76 attribution).

    `resend_mfa_code` was that entry point, and it is not an edge case: the config flow
    calls it on EVERY entry into the 2FA step, not only on a resend.
    """

    def _connection(self, tracker: PhaseTracker):
        from custom_components.addhon.client.transport.connection import HonConnection

        class _Auth:
            """Only what the entry points touch, and the mirror write they really do."""

            cognito_token = "cog"
            id_token = "id"
            refresh_token = "rt"

            async def resend_mfa_code(self, context) -> None:
                tracker.step("mfa_send")

        connection = HonConnection("e@x", "p", session=object(), phase_tracker=tracker)
        connection._auth = _Auth()
        return connection

    def test_the_otp_resend_restores_the_mirror(self) -> None:
        tracker = PhaseTracker()
        connection = self._connection(tracker)

        asyncio.run(connection.resend_mfa_code(object()))

        self.assertEqual("", tracker.current)
        self.assertEqual("", current_phase())
        # It still HAPPENED, and under its own name: a later expiry inside the resend is
        # attributed to mfa_send, not borrowed from whatever ran before it.
        self.assertIn("auth/mfa_send", [entry["phase"] for entry in tracker.entries()])

    def test_a_later_scope_is_not_shielded_by_the_resend(self) -> None:
        tracker = PhaseTracker()
        connection = self._connection(tracker)

        asyncio.run(connection.resend_mfa_code(object()))
        with phase("load_appliances", tracker):
            self.assertEqual("load_appliances", tracker.current)
        self.assertEqual("", tracker.current)


class TheTrackerReachesTheAuthLayerTest(unittest.TestCase):
    """The mirror is a cross-thread channel only if it is the SAME object end to end.

    `NativeHon.current_phase` -- the value `HonClient._run_on_hon_loop` reads from
    ANOTHER thread to attribute an expired cap -- is `NativeHon._phase_tracker.current`.
    The login runs two layers down (session -> connection -> auth) and refines that
    mirror through `PhaseTracker.step()`. Give any layer a tracker of its own and the
    refinement lands where nobody reads it: the watchdog falls back to the FLAT mirror,
    which still says "load_appliances" for a stalled sign-in -- ADDHON-400, #76 itself.
    Each hop is one keyword argument, and none of the three was pinned.
    """

    def _session(self):
        from custom_components.addhon.client.session import NativeHon

        hon = NativeHon(
            email="e@x",
            password="p",
            session=object(),  # the network boundary, never touched here
            enable_mqtt=False,
            minimal=True,
        )

        async def _no_network() -> None:
            return None

        hon.setup = _no_network  # type: ignore[assignment]
        return hon

    def test_a_login_step_names_itself_on_the_session_mirror(self) -> None:
        hon = self._session()
        asyncio.run(hon.create())
        connection = hon._connection

        # Real NativeHon -> real HonConnection -> real HonAuth, one object throughout.
        self.assertIs(hon._phase_tracker, connection._phase_tracker)
        self.assertIs(hon._phase_tracker, connection.auth._phase_tracker)

        # And the refinement the auth layer really writes composes under whatever scope
        # the session has open, which is what makes a lazy sign-in say where it is.
        with phase("load_appliances", hon._phase_tracker):
            connection.auth._phase("introduce")
            self.assertEqual("load_appliances/introduce", hon.current_phase)
        self.assertEqual("", hon.current_phase)


class PhaseScopeTableIsCompleteTest(unittest.TestCase):
    """Freeze the SET and the NAMES of the phase scopes, the way the caps are frozen.

    The name is not cosmetic: `error_codes.phase_timeout_code` resolves it segment by
    segment from the leaf outwards, so "auth/refresh" is ADDHON-406 while a scope
    renamed "auth" is ADDHON-405, and a name nobody put in the table falls through to
    the mute ADDHON-460. A behavioural test only covers the scopes someone remembered;
    this covers the ones nobody did, and makes a NEW scope a deliberate choice.
    """

    EXPECTED = {
        "session.py": {
            "setup": ["load_appliances", "mqtt_start"],
            "_create_appliance": ["load_appliance"],
        },
        "connection.py": {
            "_check_headers": ["auth/refresh", "auth"],
            "_refresh_after_rejection": ["auth/refresh"],
            "_reauth_after_rejection": ["auth"],
            "submit_mfa_code": ["auth/mfa_verify"],
            "resend_mfa_code": ["auth/mfa_send"],
        },
        "hon_client.py": {
            # The first-poll rehydration, under the same name `_build_appliance` uses
            # for the identical call at setup.
            "_do_update": ["load_appliance"],
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
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    # `hon_client` imports it aliased, so the local `phase` variable it
                    # binds for the attribution cannot shadow the scope factory.
                    and child.func.id in ("phase", "phase_scope")
                    and child.args
                    and isinstance(child.args[0], ast.Constant)
                ):
                    found.setdefault(owner, []).append(child.args[0].value)
                visit(child, owner)

        visit(tree, "<module>")
        return found

    def test_every_phase_scope_is_pinned_to_its_name(self) -> None:
        from custom_components.addhon import hon_client as hon_client_mod
        from custom_components.addhon.client import session as session_mod
        from custom_components.addhon.client.transport import connection as conn_mod

        for module in (session_mod, conn_mod, hon_client_mod):
            name = Path(module.__file__).name
            with self.subTest(module=name):
                self.assertEqual(self.EXPECTED[name], self._scopes(module))

    def test_every_scope_name_resolves_to_a_code_of_its_own(self) -> None:
        # The other half: a name that the table does not know collapses to the mute
        # ADDHON-460 "Setup timed out" -- the answer #76 was filed about. Whatever the
        # table above says, every scope must resolve to something more specific.
        from custom_components.addhon import error_codes as ec

        names = {
            name
            for scopes in self.EXPECTED.values()
            for entries in scopes.values()
            for name in entries
        }
        for name in sorted(names):
            with self.subTest(scope=name):
                self.assertIsNot(ec.LOOP_TIMEOUT, ec.phase_timeout_code(name))


class PhaseLedgerTest(unittest.TestCase):
    def test_ledger_records_outcome_per_phase(self) -> None:
        tracker = PhaseTracker()
        with phase("load_appliances", tracker):
            pass
        with self.assertRaises(TimeoutError):
            with phase("load_appliance", tracker):
                raise TimeoutError()
        with self.assertRaises(ValueError):
            with phase("auth", tracker):
                raise ValueError("nope")

        outcomes = [(e["phase"], e["outcome"]) for e in tracker.entries()]
        self.assertEqual(
            [
                ("load_appliances", "ok"),
                ("load_appliance", "timeout"),
                ("auth", "error"),
            ],
            outcomes,
        )
        for entry in tracker.entries():
            self.assertIsInstance(entry["seconds"], float)

    def test_an_unnamed_budget_expiry_is_still_filed_as_a_timeout(self) -> None:
        # A budgeted scope converts its expiry INSIDE this scope, so `phase()` never
        # sees the bare TimeoutError -- it sees a HonCodedError and decides the outcome
        # from `PHASE_TIMEOUT_CODES`. LOOP_TIMEOUT is a member of that set for a reason:
        # it is what a scope whose name the table does not resolve produces, and it is
        # exactly the case a report needs to see as "timeout" rather than a generic
        # 'error' -- the ledger is the artefact that makes a #76 report diagnosable.
        from custom_components.addhon.error_codes import LOOP_TIMEOUT, HonCodedError

        tracker = PhaseTracker()
        with self.assertRaises(HonCodedError):
            with phase("something_new", tracker):
                raise HonCodedError(LOOP_TIMEOUT, phase="something_new")
        self.assertEqual(
            [("something_new", "timeout")],
            [(e["phase"], e["outcome"]) for e in tracker.entries()],
        )

    def test_ledger_is_leak_proof_and_bounded(self) -> None:
        tracker = PhaseTracker()
        for index in range(60):
            with phase(f"step{index}", tracker):
                pass
        entries = tracker.entries()
        self.assertLessEqual(len(entries), 40)
        blob = tracker.summary()
        self.assertNotIn("@", blob)
        self.assertNotIn("http", blob)


if __name__ == "__main__":
    unittest.main()
