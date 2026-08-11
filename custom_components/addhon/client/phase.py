# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Hierarchical setup phase, transported with the operation (issue #76).

Before this module the phase was a plain attribute written by the OUTERMOST
caller (`NativeHon._setup_phase`) plus a second, unrelated tracker inside the auth
layer (`HonAuth._current_phase`). The login is LAZY -- it starts inside
`connection._check_headers`, triggered by the very request that `load_appliances`
issues -- so a stalled sign-in was attributed to `load_appliances` and mapped to
ADDHON-400 "Network timeout contacting hOn": the exact string reported in #76.

Here the phase is a ContextVar composed by nesting, so an inner step wins over the
outer one (`load_appliances/auth/refresh`) and the scope is RESTORED on exit -- a
nested re-login no longer leaves the phase pointing at the auth layer forever.

CROSS-THREAD CONSTRAINT (do not "simplify" this away): the ContextVar lives on the
dedicated hOn loop, while the waiter that must attribute an expired cap
(`HonClient._run_on_hon_loop`) runs on ANOTHER thread and cannot read it. The
`PhaseTracker` string mirror is that channel, not a leftover. Under
`asyncio.gather` each task gets a COPY of the context, so the mirror reflects the
last sibling that wrote it; that is acceptable because the precise value matters to
whoever RAISES (it reads its own `current_phase()` inside its own task) while the
mirror only serves the external attribution.

Pure module: NO Home Assistant / aiohttp / awscrt import (same discipline as
`error_codes`, which is the only thing it imports, and `debug_utils`). The ledger
holds phase names, rounded seconds and a closed-domain outcome only -- never
identity, URL or payload.
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from ..error_codes import PHASE_TIMEOUT_CODES, HonCodedError

_PHASE: ContextVar[str] = ContextVar("addhon_phase", default="")

# Bounded so a long-lived session cannot grow it without limit; 40 entries cover a
# full setup (login steps + appliance list + per-appliance loads + MQTT) with room
# to spare.
_LEDGER_MAX = 40


def current_phase() -> str:
    """The composed phase of the operation running in THIS task ('' outside any)."""
    return _PHASE.get()


class PhaseTracker:
    """Cross-thread mirror of the phase stack plus a bounded timing ledger.

    One instance per `NativeHon`, shared down into the connection and the auth layer
    (the auth object is REPLACED on every re-login, so the tracker cannot live there).
    """

    def __init__(self) -> None:
        self.current: str = ""
        self._ledger: deque[tuple[str, float, str]] = deque(maxlen=_LEDGER_MAX)

    def step(self, name: str) -> None:
        """Refine the mirror with a sub-step of the ACTIVE scope (no ContextVar push).

        Used by the auth layer, whose `_phase()` markers are plain calls rather than
        scopes. The enclosing `phase()` restores the mirror on exit, so a refinement
        can never outlive its scope.
        """
        base = _PHASE.get()
        self.current = f"{base}/{name}" if base else name

    def record(self, name: str, seconds: float, outcome: str) -> None:
        self._ledger.append((name, round(seconds, 1), outcome))

    def entries(self) -> list[dict]:
        """The ledger as closed-domain primitives (for Download Diagnostics)."""
        return [
            {"phase": name, "seconds": seconds, "outcome": outcome}
            for name, seconds, outcome in self._ledger
        ]

    def summary(self) -> str:
        """One-line ledger for the logs, e.g. 'auth 2.1s ok, load_appliances 58.4s timeout'."""
        return ", ".join(
            f"{name} {seconds}s {outcome}" for name, seconds, outcome in self._ledger
        )


@contextmanager
def phase(segment: str, tracker: PhaseTracker | None = None) -> Iterator[str]:
    """Enter a phase scope: compose, mirror, time, then RESTORE on exit.

    `segment` may itself contain '/' (e.g. "auth/refresh") when a step is naturally
    two levels deep.
    """
    parent = _PHASE.get()
    composed = f"{parent}/{segment}" if parent else segment
    token = _PHASE.set(composed)
    previous = tracker.current if tracker is not None else ""
    if tracker is not None:
        tracker.current = composed
    started = time.monotonic()
    outcome = "ok"
    try:
        yield composed
    except TimeoutError:
        # asyncio.TimeoutError and concurrent.futures.TimeoutError are both
        # TimeoutError since Python 3.11, so this one clause covers a per-request
        # aiohttp timeout that nothing budgeted has converted yet.
        outcome = "timeout"
        raise
    except HonCodedError as err:
        # A budgeted scope converts its expiry into a coded error while it is still
        # INSIDE this scope -- that is what lets it name the innermost step -- so the
        # clause above never sees a budget expiry. Without this branch every budget
        # expiry was filed as a plain 'error' and the 'timeout' outcome that
        # diagnostics documents was unreachable in production.
        outcome = "timeout" if err.error_code in PHASE_TIMEOUT_CODES else "error"
        raise
    except BaseException:
        outcome = "error"
        raise
    finally:
        _PHASE.reset(token)
        if tracker is not None:
            tracker.current = previous
            tracker.record(composed, time.monotonic() - started, outcome)
