# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Retry for the IDEMPOTENT transport steps only (issue #76).

The validation path had NO retry at all: the login is 9 sequential round-trips, so
the odds that at least one is hit by a transient blip are 9x those of a single
request -- and any one of them turned straight into a permanent user-facing error.
The only backoff that existed lived downstream, in the appliance poll.

TWO RULES make this safe, and both are load-bearing:

1. INCLUSION, never a decorator. Every retried step is wrapped explicitly at its
   call site. A step that submits credentials, consumes a single-use hand-off URL,
   mints a fresh MFA context, or spends a rotating refresh token must NEVER be
   retried: a duplicate delivery there costs a second OTP email, an invalid session,
   or a permanently burnt refresh token. The list of what is wrapped (and why the
   rest is not) is in `HonAuth.authenticate`.
2. A SHARED budget with a deadline gate. The extra attempts belong to the whole
   login, not to each step, so the added worst case is bounded and computable:
   `RETRY_MAX_EXTRA * (TOTAL_TIMEOUT + RETRY_DELAY)`. The gate refuses a retry that
   the remaining time cannot absorb, so the retry can never be the reason the phase
   budget expires. That accounting is mirrored in `client/budget.py::AUTH_FULL`.
   The deadline MUST be the one really in force -- `budget.current_deadline()`, read
   from the innermost open scope. Rebuilding it from a constant made the gate
   unfalsifiable: it compared against a deadline nobody enforced, allowed every
   retry, and the retries then spent a shorter, real budget.

Fixed delay, never exponential: a constant delay is the only one whose worst case is
known in advance, which is exactly what the budget needs.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from ..budget import RETRY_DELAY, RETRY_MAX_EXTRA, TOTAL_TIMEOUT
from ...error_codes import (
    CONNECTION_REFUSED,
    DNS_FAILURE,
    LOOP_TIMEOUT,
    NETWORK_TIMEOUT,
    classify,
)

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

# We retry ONLY when nothing was received: a timeout, a name that did not resolve, a
# connection that was refused/reset. A RECEIVED HTTP response is never retried here --
# 5xx/429 already have the appliance-layer backoff and would be counted twice, 401/403
# are a rejection, and a TLS failure does not fix itself in two seconds. Single source
# of truth so the predicate cannot drift as `classify` gains structural branches.
RETRYABLE_CODES = frozenset({NETWORK_TIMEOUT, DNS_FAILURE, CONNECTION_REFUSED, LOOP_TIMEOUT})


class RetryBudget:
    """Extra attempts shared by every retried step of ONE login, plus a deadline."""

    def __init__(
        self, extra: int = RETRY_MAX_EXTRA, deadline: float | None = None
    ) -> None:
        self.extra = extra
        # Absolute time.monotonic() at which the enclosing phase budget expires, as
        # reported by `budget.current_deadline()` -- the tightest scope actually open,
        # never a number re-derived from a constant. None (direct use / unit tests)
        # leaves only the counter: acceptable there, NOT in production, where a retry
        # without the gate could be the very thing that burns the budget.
        self.deadline = deadline

    def allows(self) -> bool:
        if self.extra <= 0:
            return False
        if self.deadline is None:
            return True
        return self.deadline - time.monotonic() >= TOTAL_TIMEOUT + RETRY_DELAY

    def consume(self) -> None:
        self.extra -= 1


def _is_retryable(err: BaseException) -> bool:
    return classify(err) in RETRYABLE_CODES


async def retry_transport(
    budget: RetryBudget | None,
    endpoint: str,
    factory: Callable[[], Awaitable[T]],
) -> T:
    """Await `factory()`, retrying a no-response transport failure after a fixed delay.

    `endpoint` is a label from the closed vocabulary in `auth_diagnostics` and is only
    used for logging (no URL, no identity).
    """
    attempt = 1
    while True:
        try:
            return await factory()
        except Exception as err:  # noqa: BLE001 - re-raised unless clearly retryable
            if budget is None or not _is_retryable(err) or not budget.allows():
                raise
            budget.consume()
            _LOGGER.warning(
                "addhOn: transient transport failure on %s (attempt %d), retrying in %ss",
                endpoint,
                attempt,
                RETRY_DELAY,
            )
        attempt += 1
        await asyncio.sleep(RETRY_DELAY)
