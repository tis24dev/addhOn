# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-phase time budgets for the hOn setup (issue #76).

A SINGLE cumulative cap of 60s used to cover everything the dedicated loop ran:
the full login (9 sequential round-trips), the appliance list, every per-appliance
load and the MQTT start. One constant for three workloads that differ by an order
of magnitude -- and when it fired, the error was attributed to whichever phase the
outermost caller had last written down. A user with a merely slow network saw
"ADDHON-400: Network timeout contacting hOn" from a login that had not finished.

Budgets here are derived, never invented:

    budget(hops) = hops * SLOW_HOP + TOTAL_TIMEOUT

* `SLOW_HOP` is what we grant a round-trip that is slow but alive. It is our own
  `CONNECT_TIMEOUT`, not a new number.
* `+ TOTAL_TIMEOUT` is the tail margin that guarantees the invariant this module
  exists for: ONE stuck hop must expire on its OWN aiohttp timeout (attributed,
  with a message) and never on the budget (opaque). A budget must never be the
  first thing to fire on a single-hop stall.
* `retries=` adds `n * (TOTAL_TIMEOUT + RETRY_DELAY)`. Whoever changes the retry
  policy in `transport/retry.py` MUST keep this term in sync, or the budget kills
  the retry exactly when it is needed.

`hops` counts SEQUENTIAL round-trips on the critical path, not requests: three
requests fired in one `asyncio.gather` are one hop.

TWO KINDS OF NUMBER, and confusing them is what made the first attempt at #76 WORSE
than the bug it fixed:

* a SCOPE BUDGET (`budgeted()`) measures the OWN work of one phase. Scopes NEST --
  the sign-in is lazy, so `AUTH_FULL` runs *inside* the `APPLIANCE_LIST` scope of the
  request that triggered it. Two independent `asyncio.timeout`s in that shape mean the
  smaller OUTER one always fires first: `APPLIANCE_LIST`(40s) killed `AUTH_FULL`(184s)
  at 40s and reported it as "load_appliances" -- the #76 string, now 20s SOONER than
  the 60s cap it replaced. The fix is not to inflate every outer number until it
  contains every inner one (the containment chain is unbounded once the 401 recovery
  ladder can re-login twice); it is for the nested sign-in to SUSPEND the scopes it
  interrupts, so each budget keeps measuring its own work. See `budgeted()`.
* a CAP (`cap()`) is the outer watchdog of a call site, waited on by
  `HonClient._run_on_hon_loop` from ANOTHER thread. It cannot be suspended, so it must
  contain what it may hold: its own work PLUS one lazy sign-in.

Pure module: NO Home Assistant / aiohttp import, so transport, session and client
can all import it. It also OWNS the per-request aiohttp timeouts (which used to
live in `transport/connection.py`) so a budget can never be derived from a number
someone changes elsewhere.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from ..error_codes import HonCodedError, phase_timeout_code
from .phase import current_phase

# Per-request HTTP timeouts on the session WE own. Without them aiohttp defaults to
# a 300s total, so a dead/blocked endpoint only failed when the dedicated-loop cap
# fired, as an opaque message-less timeout (issue #30).
CONNECT_TIMEOUT = 10  # TCP connect + TLS handshake to one endpoint
TOTAL_TIMEOUT = 30  # whole request incl. response read
SOCK_READ_TIMEOUT = 20  # gap between received chunks

# Retry policy (transport/retry.py) -- kept here because the budgets must account
# for it. Fixed delay, never exponential: only a constant delay makes the added
# worst case computable in advance, which is what keeps it under the budget.
RETRY_DELAY = 2.0
RETRY_MAX_EXTRA = 2

SLOW_HOP = CONNECT_TIMEOUT


def budget(hops: int, *, tail: float = 0.0, retries: int = 0) -> float:
    """Time budget for a phase of `hops` sequential round-trips."""
    return hops * SLOW_HOP + TOTAL_TIMEOUT + tail + retries * (TOTAL_TIMEOUT + RETRY_DELAY)


# Full Salesforce login = 9 sequential round-trips: introduce, two manual redirects,
# login page, login POST, three GETs inside _get_token, api_auth POST. The retry term
# covers the extra attempts transport/retry.py may spend on the idempotent steps.
AUTH_FULL = budget(9, retries=RETRY_MAX_EXTRA)
# Refresh = token POST + api_auth POST. Never retried (the refresh token rotates and
# is single-use), so no retry term.
AUTH_REFRESH = budget(2)
# 2FA resume = two remoting calls, the finish call, the resume-token GET and api_auth.
MFA_RESUME = budget(5)
# The appliance list is a single POST.
APPLIANCE_LIST = budget(1)
# One appliance = 2 sequential waves after #76: the gather of 3 in load_commands, then
# load_attributes. load_statistics moved to the first coordinator refresh, which redoes
# it anyway. The budget keeps a third hop of headroom for a lazy re-auth in between.
APPLIANCE_ONE = budget(3)

# What ONE lazy sign-in can add to the request that triggers it: `_check_headers`
# tries the refresh first and, when that leaves the tokens unusable, falls back to the
# full login -- sequentially, so the worst case is their sum. Scope budgets do NOT need
# to contain this (a sign-in suspends them); caps do.
LAZY_AUTH = AUTH_REFRESH + AUTH_FULL

# Outer watchdogs for HonClient._run_on_hon_loop. These are NOT budgets: every phase
# below bounds itself and converts its expiry into an attributed coded error, so a cap
# only has to catch a loop that stopped progressing at all (a deadlock on the refresh
# lock, a task that never gets scheduled).
_CAP_MARGIN = SLOW_HOP


def cap(work: float) -> float:
    """Outer watchdog for a call site: its own work PLUS one lazy sign-in.

    A cap is waited on from ANOTHER thread (a `concurrent.futures.Future`), so unlike
    a scope budget it cannot be suspended while a nested sign-in runs -- it has to
    contain it, or it fires first and destroys the attribution (issue #76).

    ONE sign-in is what it contains, and that is a DELIBERATE stop, not the worst case.
    A single request can open three: `_check_headers` signs in lazily, then the 401
    ladder in `_intercept` adds a refresh and a second full login
    (`AUTH_FULL + AUTH_REFRESH + AUTH_FULL` = 418s against COMMAND=284s). Sizing every
    cap for that chain would put a user command at ~12min and the setup watchdog far
    beyond what Home Assistant will wait for, to cover a case that means the tokens are
    being rejected twice in a row. When it does happen the cap fires INSIDE the sign-in,
    where the phase mirror reads "auth[/refresh]": the user gets the attributed,
    retryable ADDHON-405/406, never the ADDHON-400 of #76. The degradation is bounded
    and named -- that is the property, and `test_setup_budgets.py` pins it.
    """
    return work + LAZY_AUTH + _CAP_MARGIN


CLOSE = 15
# A command is one POST, but a rejected token makes it re-login inline, so it gets the
# sign-in allowance too. It used to be the bare 60s run cap, which truncated exactly
# the re-login it was waiting for.
COMMAND = cap(budget(1))
# One appliance polled by the coordinator (hon_client._update_appliance_sync).
APPLIANCE_POLL = cap(APPLIANCE_ONE)
# Config-flow validation: minimal=True, no MQTT, no per-appliance loads -> a lazy
# refresh attempt, the full login and the appliance list.
VALIDATION_CAP = cap(APPLIANCE_LIST)
# Runtime setup adds MQTT and the per-appliance hydration. Sized for the inventory
# beyond which a stalled setup is a stall rather than slowness.
_WATCHDOG_APPLIANCES = 4
MQTT_START = budget(1, tail=10)
SETUP_CAP = VALIDATION_CAP + MQTT_START + _WATCHDOG_APPLIANCES * APPLIANCE_ONE


# --- Scope budgets ------------------------------------------------------------

# Budget scopes currently open in THIS task, outermost first. Two things read it: a
# nested sign-in, to suspend the scopes it interrupts, and the login retry gate, to
# know the deadline that is actually in force instead of re-deriving one from a
# constant that may not be the tightest (issue #76).
_ACTIVE: ContextVar[tuple[asyncio.Timeout, ...]] = ContextVar(
    "addhon_budget_scopes", default=()
)


def current_deadline() -> float | None:
    """`time.monotonic()` instant at which the TIGHTEST open budget expires.

    None outside any budgeted scope (direct use, unit tests). Returned on the
    `time.monotonic()` clock -- asyncio deadlines live on the loop clock, which is
    built on `time.monotonic()` but is not required to be the same origin -- so
    callers keep using the one clock they already use.
    """
    deadlines = [
        when for scope in _ACTIVE.get() if (when := scope.when()) is not None
    ]
    if not deadlines:
        return None
    return time.monotonic() + (min(deadlines) - asyncio.get_running_loop().time())


def _shift(scopes: tuple[asyncio.Timeout, ...], delta: float) -> None:
    """Push the deadline of every scope in `scopes` by `delta` seconds."""
    if not delta:
        return
    for scope in scopes:
        when = scope.when()
        if when is None:
            continue
        try:
            scope.reschedule(when + delta)
        except RuntimeError:
            # Already expired, or not entered: there is nothing left to suspend.
            continue


@asynccontextmanager
async def budgeted(
    seconds: float, *, suspends_caller: bool = False
) -> AsyncIterator[None]:
    """Bound a phase and turn its expiry into an ATTRIBUTED coded error.

    Mandatory conversion rule: a bare TimeoutError must NEVER leave a budgeted scope.
    It would reach the dedicated-loop cap with no phase attached and be mapped to the
    mute ADDHON-460, which is what made transporting the phase pointless before (#76).
    Converted HERE, i.e. still INSIDE the `phase()` scope the caller opened, so
    `current_phase()` names the innermost step that actually stalled. A per-request
    aiohttp timeout raised from inside is converted the same way and keeps its own
    exception as `__cause__`.

    `suspends_caller=True` marks work the CALLER did not ask for: the sign-in that
    `_check_headers` starts lazily inside somebody else's request. Its budget is pushed
    onto every enclosing scope on entry and the unused remainder is taken back on exit,
    so an enclosing budget keeps measuring its OWN work and can neither truncate the
    sign-in (the #76 regression: `APPLIANCE_LIST`=40s killing `AUTH_FULL`=184s and
    calling it "load_appliances") nor be spent by it. This is what lets every number
    above stay the size of the work it names instead of growing to contain an unbounded
    chain of nested re-logins.
    """
    loop = asyncio.get_running_loop()
    enclosing = _ACTIVE.get()
    if suspends_caller:
        _shift(enclosing, seconds)
    started = loop.time()
    try:
        async with asyncio.timeout(seconds) as scope:
            token = _ACTIVE.set((*enclosing, scope))
            try:
                yield
            finally:
                _ACTIVE.reset(token)
    except TimeoutError as err:
        stalled = current_phase()
        raise HonCodedError(phase_timeout_code(stalled), phase=stalled) from err
    finally:
        if suspends_caller:
            # Give back what the sign-in did NOT use, so the caller's remaining budget
            # is exactly what it had minus the time the interruption really cost.
            _shift(enclosing, -(seconds - (loop.time() - started)))
