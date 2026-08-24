# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Asynchronous client for Haier's hOn APIs."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from typing import Any

from .client import budget
from .client.auth_diagnostics import (
    AuthDiagnosticTrace,
    classify_failure_reason,
)
# Aliased: `_run_on_hon_loop` binds a LOCAL named `phase` for the attribution it
# samples, and shadowing the scope factory there would be a trap for the next edit.
from .client.phase import phase as phase_scope
from .command_dispatch import CommandDispatcher, CommandPatch
from .debug_utils import debug_key_sample, redact_email, redact_id, redact_mac
from .error_codes import (
    CLIENT_SHUTDOWN,
    MFA_REQUIRED,
    HonCodedError,
    HonErrorCode,
    classify,
    error_detail,
    is_rate_limited_text,
    is_server_failure_text,
    phase_timeout_code,
    # Moved to error_codes so the SETUP path can apply the same rule (the session
    # cannot import this module). Kept under its original private name: it is the
    # name every call site and its test already use.
    representative_failure as _representative_failure,
)

_LOGGER = logging.getLogger(__name__)

# The hOn client is entirely native (client/): the session comes from
# client.factory.create_session. The BABYCARE fix is native in the enum class.

_SERIAL_ATTRS = ("serial_number", "serialNumber", "mac_address", "macAddress", "code")
_CONSUMPTION_ATTRS = (
    "totalElectricityUsed",
    "currentElectricityUsed",
    "totalWaterUsed",
    "currentWaterUsed",
    "totalWashCycle",
    "programsCounter",
)


def _debug_container_to_dict(container, label: str) -> dict:
    """Best-effort conversion of a client container for diagnostic logging."""
    if container is None:
        return {}
    if isinstance(container, dict):
        return dict(container)
    try:
        return dict(container)
    except Exception as err:
        _LOGGER.debug(
            "Consumption debug: unable to convert %s (%s): %s",
            label,
            type(container).__name__,
            err,
        )
        return {}


def _debug_extract_value(value):
    if hasattr(value, "value"):
        return value.value
    return value


def _debug_consumption_values(values: dict) -> dict[str, Any]:
    return {
        key: _debug_extract_value(values[key]) if key in values else "<missing>"
        for key in _CONSUMPTION_ATTRS
    }


def _debug_appliance_consumption(stage: str, appliance, attributes: dict | None = None) -> None:
    """Verbose log to understand where the consumption counters disappear."""
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return

    stats = _debug_container_to_dict(getattr(appliance, "statistics", None), "statistics")
    raw_attrs = _debug_container_to_dict(getattr(appliance, "attributes", None), "attributes")
    settings = _debug_container_to_dict(getattr(appliance, "settings", None), "settings")
    merged_attrs = attributes if attributes is not None else _get_attributes(appliance)
    commands = getattr(appliance, "commands", None)
    command_names = sorted(commands.keys()) if isinstance(commands, dict) else []

    _LOGGER.debug(
        "Consumption debug [%s] '%s' type=%s id=%s: "
        "statistics_type=%s statistics_keys=%d %s statistics_values=%s; "
        "raw_attribute_keys=%d %s; settings_keys=%d %s; "
        "merged_keys=%d %s merged_values=%s; "
        "load_statistics=%s update=%s commands=%s",
        stage,
        redact_id(_get_name(appliance)),
        _get_type(appliance),
        redact_mac(getattr(appliance, "unique_id", None) or _get_serial(appliance)) or "<no-id>",
        type(getattr(appliance, "statistics", None)).__name__,
        len(stats),
        debug_key_sample(stats),
        _debug_consumption_values(stats),
        len(raw_attrs),
        debug_key_sample(raw_attrs),
        len(settings),
        debug_key_sample(settings),
        len(merged_attrs),
        debug_key_sample(merged_attrs),
        _debug_consumption_values(merged_attrs),
        callable(getattr(appliance, "load_statistics", None)),
        callable(getattr(appliance, "update", None)),
        command_names,
    )


def _get_serial(appliance) -> str:
    for attr in _SERIAL_ATTRS:
        val = getattr(appliance, attr, None)
        if val:
            return str(val)
    return ""


def _get_mac(appliance) -> str:
    """MAC address of the appliance, the identifier used in the device_registry."""
    for attr in ("mac_address", "macAddress", "mac"):
        val = getattr(appliance, attr, None)
        if val:
            return str(val)
    return ""


def _get_name(appliance) -> str:
    for attr in ("nick_name", "nickName", "model_name", "modelName", "name"):
        val = getattr(appliance, attr, None)
        if val:
            return str(val)
    return "Haier Appliance"


def _get_model(appliance) -> str:
    for attr in ("model_name", "modelName", "model", "typology"):
        val = getattr(appliance, attr, None)
        if val:
            return str(val)
    return "Unknown"


def _get_type(appliance) -> str:
    for attr in ("appliance_type", "applianceType", "type_name", "category"):
        val = getattr(appliance, attr, None)
        if val:
            return str(val).upper()
    return "UNKNOWN"


def _get_attributes(appliance) -> dict:
    """Extract the attributes from the device, looking in statistics, attributes and settings."""
    attributes = {}

    # The consumption counters (totalElectricityUsed, totalWaterUsed,
    # totalWashCycle, currentElectricityUsed, currentWaterUsed, ...) live in the
    # the `statistics` container, populated by load_statistics() but so far NEVER
    # exposed to the sensors. We merge it first, so real-time attributes and
    # settings win in case of conflicting keys.
    stats = getattr(appliance, "statistics", None)
    if isinstance(stats, dict):
        attributes.update(stats)
    elif stats is not None:
        try:
            attributes.update(dict(stats))
        except Exception as err:
            _LOGGER.debug("Error reading statistics: %s", err)

    raw = getattr(appliance, "attributes", {})
    if isinstance(raw, dict):
        attributes.update(raw)
        params = raw.get("parameters", None)
        if params is not None:
            if isinstance(params, dict):
                attributes.update(params)
            elif hasattr(params, "__iter__"):
                try:
                    attributes.update(dict(params))
                except Exception as e:
                    _LOGGER.debug("Error reading parameters: %s", e)
    elif hasattr(raw, "parameters"):
        try:
            attributes.update(dict(raw.parameters))
        except Exception:
            pass

    if hasattr(appliance, "settings"):
        try:
            attributes.update(dict(appliance.settings))
        except Exception as err:
            _LOGGER.error("Error reading settings: %s", err)

    return attributes


def _error_text(err: BaseException) -> str:
    return str(err).lower()


def _is_auth_error(err: BaseException) -> bool:
    # Check both the message AND the exception class name: the login-flow errors
    # (e.g. our NativeAuthError) contain "auth" in the NAME even when the message
    # does not (e.g. wrong password -> "login: failed"/"Can't login"), so they are
    # classified as invalid_auth by name without importing those classes. The
    # "retryable 5xx" check in _requires_reauth keeps priority: an auth error that
    # nonetheless carries a 500/timeout goes into retry, not reauth.
    haystack = f"{_error_text(err)} {type(err).__name__.lower()}"
    return any(k in haystack for k in (
        "personaccountid",
        "unauthorized",
        "401",
        "403",
        "token",
        "auth",
        "credential",
    ))


def _is_retryable_server_error(err: BaseException) -> bool:
    if isinstance(err, (asyncio.TimeoutError, concurrent.futures.TimeoutError, TimeoutError)):
        return True
    err_str = _error_text(err)
    return (
        is_rate_limited_text(err_str)
        or is_server_failure_text(err_str)
        or "timeout" in err_str
        or "timed out" in err_str
    )


def _is_missing_session_error(err: BaseException) -> bool:
    err_str = _error_text(err)
    return any(k in err_str for k in (
        "session unavailable",
    ))


def _requires_reauth(err: BaseException) -> bool:
    # A coded error already decided its routing (e.g. a phase-attributed loop timeout
    # is non-reauth; an auth-step code is reauth). Duck-typed so this stays decoupled
    # from error_codes.HonErrorCode and keeps working under the test stubs.
    code = getattr(err, "error_code", None)
    if code is not None and hasattr(code, "requires_reauth"):
        return bool(code.requires_reauth)
    return (
        _is_auth_error(err) or _is_missing_session_error(err)
    ) and not _is_retryable_server_error(err)


def _must_propagate(err: BaseException) -> bool:
    """True when an error must reach the caller instead of being tolerated.

    A load_statistics failure is normally non-fatal -- it only carries the
    consumption counters -- so both the primary update() path and the load_*
    fallback loop swallow it. But an error that needs reauth, or a retryable
    server error that needs the backoff, MUST still surface. Keeping that single
    predicate here enforces the "both paths tolerate the same set" invariant by
    code rather than by a comment that can drift.
    """
    return _requires_reauth(err) or _is_retryable_server_error(err)


def _poll_census(
    returned: int, kept: int, failures: list[tuple[str, Exception]]
) -> dict[str, Any]:
    """Leak-proof account of ONE completed poll cycle, for the downloadable diagnostics.

    A dump of a broken account reads `"appliances": []` whether the cloud returned
    nothing or the poll dropped every appliance it was handed, and the only thing that
    ever separated the two is the `Partial update: %d/%d` WARNING below, which no dump
    carries. The two counts answer that; each drop then carries the ADDHON label of its
    cause and NOTHING else -- the redacted names in `failures` belong to that WARNING,
    and an exception message routinely carries a MAC, a nickname or a URL, so neither
    is read here.

    The label comes from the same picker the total-failure raise uses, so a census and
    the `last_error` beside it in the dump can never name one failure two ways. `kept`
    plus the drops normally account for `returned`; a shortfall is a finding of its own
    (two appliances sharing an id collapse into a single snapshot entry).
    """
    dropped: list[dict[str, Any]] = []
    for _name, err in failures:
        # The name is dropped on the floor rather than forwarded: the picker never
        # reads it, and passing "" keeps identity out of this call by construction
        # instead of by trusting a helper never to grow a use for it.
        code, _cause = _representative_failure([("", err)])
        dropped.append({"code": code.label, "reason": code.reason_en})
    return {"returned": returned, "kept": kept, "dropped": dropped}


class HonClient:
    """Manages the connection to the Haier hOn APIs via the native client.

    Loop strategy:
    - We keep a single dedicated event loop (_hon_loop) running on a background
      thread (_hon_thread).
    - ALL client calls (setup, update, commands) are executed on that loop via
      asyncio.run_coroutine_threadsafe(), so the aiohttp session never changes
      loop and never errors out.
    - HA's event loop is never blocked.
    """

    _RUN_TIMEOUT = 60
    _CANCEL_TIMEOUT = 5

    def __init__(
        self,
        email: str,
        password: str,
        validation: bool = False,
        refresh_token: str = "",
        auth_diagnostics: bool = False,
    ) -> None:
        self._email = email
        self._password = password
        # Persisted OAuth refresh token: lets runtime setup refresh instead of doing a
        # full login, which is what skips the 2FA prompt on every restart (an account
        # with email-OTP would otherwise re-challenge each time). "" = full login.
        self._refresh_token = refresh_token
        # validation=True (config-flow): authenticate + count appliances only, no MQTT
        # and no per-appliance loads (issue #30). Runtime keeps the full setup.
        self._validation = validation
        self._auth_trace = AuthDiagnosticTrace(enabled=auth_diagnostics)
        # Last classified error code (for the downloadable diagnostics / log parity).
        self.last_error_code: Any = None
        # Login phase reached at the last failure ("authenticate"/"mfa_verify"/...), and a
        # leak-proof 2FA summary, surfaced in the downloadable diagnostics for triage.
        self.last_error_phase: str | None = None
        self.last_mfa_summary: dict | None = None
        # Per-phase duration+outcome of the last setup attempt (leak-proof primitives:
        # phase names, rounded seconds, ok/error/timeout). This is the artefact that
        # makes a report like #76 diagnosable without a live probe.
        self.last_phase_ledger: list[dict] | None = None
        # The appliance-list census as it stood when setup_sync FAILED, snapshotted
        # before _close_sync tears the session down. Part of the failure record with
        # the three fields above, and cleared with them.
        #
        # This is deliberately the mirror `last_appliance_fetch` (:840) argues against,
        # and the argument does not reach it: what that property refuses is a mirror of
        # a LIVE census, which drifts because the session keeps writing after the copy.
        # This one is written once, on the path that has just decided there will be no
        # more writes, and is read by __init__ into a hass.data record that is never
        # updated afterwards. Without it the census of a setup that raised inside
        # load_appliances dies with the session, and `outcome: "raised"` -- the state
        # that says "the call never reached a body" -- stays unreachable in a dump.
        self.last_setup_fetch: dict | None = None
        # Census of the last COMPLETED poll cycle (see _poll_census): how many
        # appliances the cloud returned, how many survived, one catalog label per
        # drop. None until a cycle finishes, which is the third state the dump has
        # to distinguish -- no poll ever ran.
        self.last_poll_census: dict | None = None
        self._hon_instance = None
        self._api = None
        self._hon_loop: asyncio.AbstractEventLoop | None = None
        self._hon_thread: threading.Thread | None = None
        self._command_dispatcher = CommandDispatcher()
        self._lifecycle_lock = threading.RLock()
        # Flipped True after the first poll that returns data. Until then the poll is
        # STRICT (any per-appliance error re-raises -> ConfigEntryNotReady -> HA retries
        # setup), because platform setup iterates the FIRST snapshot once and there is
        # no dynamic discovery: an appliance missing from that snapshot would get NO
        # entities until a reload. Steady-state polls are resilient (skip the failed
        # appliance, keep the others).
        self._first_poll_done = False
        # Realtime notify callback, kept on the CLIENT (not the session): the
        # session is rebuilt on every setup/re-auth with its own _notify_function
        # reset to None, so storing it here lets setup_sync re-apply it and the
        # MQTT push survive a re-auth (#20).
        self._notify_function: Any = None

    # -- Dedicated loop management ---------------------------------------------

    def _start_hon_loop(self) -> None:
        """Start the dedicated loop on a background thread."""
        self._hon_loop = asyncio.new_event_loop()
        self._hon_thread = threading.Thread(
            target=self._hon_loop.run_forever,
            name="addhon_loop",
            daemon=True,
        )
        self._hon_thread.start()
        _LOGGER.debug("Dedicated hOn loop started on thread '%s'", self._hon_thread.name)

    def _run_on_hon_loop(self, coro, timeout: float | None = None) -> Any:
        """Run a coroutine on the dedicated loop and wait for the result.

        Call only from a non-loop thread (e.g. HA's executor).

        `timeout` is the WATCHDOG for this call site, not a budget: every phase inside
        bounds itself (client/budget.py) and converts its own expiry into an attributed
        coded error. A single constant used to cover the login, the appliance list, the
        per-appliance loads and a one-appliance poll alike -- workloads an order of
        magnitude apart -- which is why it fired first and produced the opaque
        ADDHON-400 of #76. Omitted -> the legacy 60s.
        """
        cap = self._RUN_TIMEOUT if timeout is None else timeout
        # The lock covers the LIFECYCLE read (which loop/thread we are talking to) and
        # the scheduling, NOT the wait. Waiting under it made every caller of
        # `close_sync`/`setup_sync` queue behind the SLOWEST in-flight call, and the
        # caps this file now passes are minutes rather than the legacy 60s: a stalled
        # poll (APPLIANCE_POLL) would have blocked an unload/reload for ~5 minutes and
        # Home Assistant would report the unload as slow. Nothing in the wait touches
        # the lifecycle fields, so it does not belong inside the lock.
        with self._lifecycle_lock:
            loop = self._hon_loop
            if loop is None or not loop.is_running():
                if hasattr(coro, "close"):
                    coro.close()
                raise RuntimeError("Dedicated hOn loop not active")
            if threading.current_thread() is self._hon_thread:
                if hasattr(coro, "close"):
                    coro.close()
                raise RuntimeError("Synchronous call on the hOn loop not allowed")

            future: concurrent.futures.Future = concurrent.futures.Future()
            task_holder: dict[str, asyncio.Task] = {}

            def _schedule_task() -> None:
                try:
                    if future.cancelled():
                        if hasattr(coro, "close"):
                            coro.close()
                        return

                    task = loop.create_task(coro)
                    task_holder["task"] = task
                except Exception as err:
                    if not future.done():
                        future.set_exception(err)
                    return

                def _copy_result(done_task: asyncio.Task) -> None:
                    if future.done():
                        return
                    try:
                        future.set_result(done_task.result())
                    except asyncio.CancelledError:
                        future.cancel()
                    except concurrent.futures.InvalidStateError:
                        pass
                    except Exception as err:
                        try:
                            future.set_exception(err)
                        except concurrent.futures.InvalidStateError:
                            pass

                task.add_done_callback(_copy_result)

            try:
                loop.call_soon_threadsafe(_schedule_task)
            except Exception:
                if hasattr(coro, "close"):
                    coro.close()
                raise

        started = time.monotonic()
        try:
            return future.result(timeout=cap)
        except concurrent.futures.CancelledError as cancelled:
            # TEARDOWN, not a timeout: an unload/reload stopped the dedicated loop while
            # this call was in flight, so `_cancel_pending_tasks` cancelled the task and
            # `_copy_result` cancelled the future under us. Reachable because the wait
            # above is deliberately NOT under the lifecycle lock -- holding it made every
            # unload queue behind the SLOWEST in-flight call, minutes with these caps.
            #
            # Without this clause the caller gets a bare, message-less CancelledError
            # that `classify` can only map to ADDHON-999 (and `representative_failure`
            # to ADDHON-220): the user's command or poll fails with "Unknown error" and
            # no line anywhere says the client was shutting down -- precisely the kind of
            # unfalsifiable report #76 was filed as. It is NOT re-raised as a
            # cancellation because it is not one: THIS thread is a plain executor thread
            # that was never cancelled (`concurrent.futures.CancelledError` has been a
            # separate, ordinary Exception from `asyncio.CancelledError` since 3.8, so
            # nothing here swallows a real task cancellation). Waiting for the in-flight
            # call instead is the slow unload this shape exists to avoid.
            raise HonCodedError(
                CLIENT_SHUTDOWN, "The hOn client was shut down while the call was running"
            ) from cancelled
        except concurrent.futures.TimeoutError as timeout_err:
            if future.done():
                # NOT our cap: since Python 3.11 `concurrent.futures.TimeoutError IS
                # asyncio.TimeoutError IS TimeoutError`, so a BARE TimeoutError raised BY
                # the coroutine (an aiohttp per-request timeout that no budgeted scope
                # converted -- e.g. the first-poll `load_commands` rehydration) arrives
                # here as the very type the cap raises. Telling them apart by TYPE is
                # impossible; by STATE it is exact: on a real cap expiry the future is
                # still PENDING, while a coroutine that raised has already FINISHED.
                # Without this, a fault that had a name was logged as "watchdog fired
                # after 0.0s" and delivered as the mute ADDHON-460 instead of its own
                # code. Re-raise it untouched (or hand back a result that landed in the
                # microsecond race) and let the caller classify it.
                return future.result()
            # Phase attribution, read BEFORE anything is cancelled. The
            # hierarchical mirror (client/phase.py) names the innermost step
            # actually running -- a lazy sign-in inside the appliance-list request
            # reads "load_appliances/auth/..." and maps to ADDHON-405/406 instead
            # of borrowing the caller's label and producing the misleading
            # ADDHON-400 of #76. It MUST be sampled here: cancelling the task
            # unwinds the phase scopes, and every `phase()` restores the mirror on
            # the way out, so a read taken after the drain finds "" and silently
            # falls back to the flat mirror -- which is the label that was wrong in
            # the first place. Flat mirror as the fallback (the MQTT layer still
            # writes only that one). ("" -> LOOP_TIMEOUT.)
            phase = (
                getattr(self._hon_instance, "current_phase", "")
                or getattr(self._hon_instance, "_setup_phase", "")
                or ""
            )
            drain_future: concurrent.futures.Future = concurrent.futures.Future()
            # The real cause, recovered from the task we are about to cancel. Without
            # it, a coroutine that failed for a NAMED reason a moment after the cap
            # expired was reported as a synthetic, message-less timeout -- which is
            # what made every hypothesis about #76 unfalsifiable on a real install.
            drained: dict[str, BaseException] = {}

            def _cancel_and_drain() -> None:
                task = task_holder.get("task")
                if task is None:
                    future.cancel()
                    if not drain_future.done():
                        drain_future.set_result(None)
                    return

                # Lost race: the task finished (with an exception) between the cap
                # expiring and this callback running. Take its error before cancel().
                if task.done() and not task.cancelled():
                    done_err = task.exception()
                    if done_err is not None:
                        drained["error"] = done_err

                async def _drain_task() -> None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception as err:
                        drained["error"] = err
                    if not future.done():
                        future.cancel()
                    if not drain_future.done():
                        drain_future.set_result(None)

                loop.create_task(_drain_task())

            try:
                loop.call_soon_threadsafe(_cancel_and_drain)
                drain_future.result(timeout=self._CANCEL_TIMEOUT)
            except Exception as err:
                _LOGGER.debug("Timeout while cancelling hOn task: %s", err)
            elapsed = round(time.monotonic() - started, 1)
            original = drained.get("error")
            _LOGGER.error(
                "hOn loop watchdog fired after %ss (phase=%s, cause=%s) [%s]",
                elapsed,
                phase or "?",
                type(original).__name__ if original is not None else "stall",
                getattr(self._hon_instance, "phase_summary", "") or "no ledger",
            )
            if original is not None:
                # A 2FA challenge that landed across the cap must stay NAKED: wrapping
                # it in a HonCodedError would break the interactive resume branch.
                from .client.transport.auth import MFAChallengeRequired

                if isinstance(original, MFAChallengeRequired):
                    raise original
                raise HonCodedError(
                    classify(original, phase=phase), phase=phase
                ) from original
            # Genuine stall: the drain produced only a cancellation, so the phase
            # code is all we know.
            raise HonCodedError(phase_timeout_code(phase), phase=phase) from timeout_err

    def _cancel_pending_tasks(self, loop: asyncio.AbstractEventLoop) -> None:
        """Cancel leftover tasks before stopping the dedicated loop."""

        async def _cancel_pending() -> None:
            current = asyncio.current_task()
            pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
            if not pending:
                return
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        try:
            future = asyncio.run_coroutine_threadsafe(_cancel_pending(), loop)
            future.result(timeout=self._CANCEL_TIMEOUT)
        except Exception as err:
            _LOGGER.debug("Error cancelling pending hOn tasks: %s", err)

    def _stop_hon_loop(self) -> None:
        """Stop the dedicated loop and the thread."""
        loop = self._hon_loop
        thread = self._hon_thread

        if loop and loop.is_running() and thread is not threading.current_thread():
            self._cancel_pending_tasks(loop)
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=10)
        if thread and thread.is_alive():
            _LOGGER.warning("Dedicated hOn thread did not terminate within the timeout")
            return
        if loop and not loop.is_closed():
            try:
                loop.close()
            except Exception as err:
                _LOGGER.warning("Error closing hOn loop: %s", err)
                return
        self._hon_loop = None
        self._hon_thread = None

    def _close_sync(self) -> None:
        """Close the hOn session and the dedicated loop idempotently."""
        with self._lifecycle_lock:
            hon = self._hon_instance
            self._hon_instance = None
            self._api = None

            if hon is not None:
                try:
                    # Teardown must never wait as long as a setup.
                    self._run_on_hon_loop(
                        hon.__aexit__(None, None, None), budget.CLOSE
                    )
                except Exception as err:
                    _LOGGER.debug("Error closing hOn session: %s", err)
            self._stop_hon_loop()

    # -- Setup -----------------------------------------------------------------

    def setup_sync(self) -> None:
        """Full client setup in executor (NOT on HA's event loop).

        Starts the dedicated loop, creates the Hon instance and completes the
        login. The aiohttp session is created on the dedicated loop and stays
        bound to it for the whole lifetime of the client.
        """
        # The hOn session comes from the native factory (client/).
        from .client.factory import create_session
        from .client.transport.auth import MFAChallengeRequired

        with self._lifecycle_lock:
            # Fresh attempt: clear any stale failure record so (a) a success leaves the
            # diagnostics last_error empty, and (b) a new failure is never shown with a
            # phase/mfa-summary left over from a prior attempt (the three move together).
            self.last_error_code = None
            self.last_error_phase = None
            self.last_mfa_summary = None
            self.last_phase_ledger = None
            self.last_setup_fetch = None
            # Not part of that failure record: a census is a statement about ONE
            # session's poll, and this attempt builds a new session. Carrying it over
            # would let a dump answer "how did the last poll go" with a cycle that ran
            # on a session this client has since thrown away.
            self.last_poll_census = None
            try:
                if self._hon_loop is None or not self._hon_loop.is_running():
                    self._start_hon_loop()

                self._hon_instance = create_session(
                    self._email,
                    self._password,
                    enable_mqtt=not self._validation,
                    minimal=self._validation,
                    refresh_token=self._refresh_token,
                    auth_trace=self._auth_trace,
                )
                _LOGGER.debug("Hon instance created")

                # Login + aiohttp session init, on the dedicated loop. The validation
                # path (minimal, no MQTT, no per-appliance loads) gets the tighter of
                # the two watchdogs: a human is waiting on the config-flow form.
                self._api = self._run_on_hon_loop(
                    self._hon_instance.__aenter__(),
                    budget.VALIDATION_CAP if self._validation else budget.SETUP_CAP,
                )
                _LOGGER.info("Connection to hOn succeeded for %s", redact_email(self._email))
                # Re-apply the realtime notify callback to the freshly built session
                # (rebuilt on every setup/re-auth with _notify_function=None);
                # without this the MQTT push is a permanent no-op after a re-auth (#20).
                if self._notify_function is not None:
                    self._hon_instance.subscribe_updates(self._notify_function)
                # Sync the SEED to the live (possibly just-minted/rotated) token so a later
                # _async_reauth()/restart re-seeds the new session from the current token
                # instead of the stale one -> no needless full login / 2FA re-prompt; also
                # keeps diagnostics had_refresh_token accurate. (#1)
                self._refresh_token = self.refresh_token or self._refresh_token
            except MFAChallengeRequired as err:
                # 2FA email-OTP challenge: KEEP the dedicated loop + half-open session
                # alive so submit_mfa_code_sync() can resume on the SAME session. Do NOT
                # _close_sync(). The interactive config flow drives the resume; a
                # background caller (async_setup_entry) closes the client itself and
                # routes to the reauth flow. last_error_code set for diagnostics parity.
                self.last_error_code = MFA_REQUIRED
                self.last_error_phase = "mfa_challenge"
                ctx = getattr(err, "context", None)
                self.last_mfa_summary = {
                    "challenge_kind": getattr(ctx, "challenge_kind", None),
                    "can_resend": getattr(ctx, "can_resend", None),
                }
                _LOGGER.info("hOn login needs 2FA verification [%s]", MFA_REQUIRED.label)
                raise
            except Exception as err:
                # Prefer the phase the error CARRIES (hierarchical, recorded where it
                # was raised) over the auth mirror, which only knows the login steps.
                self.last_error_phase = (
                    getattr(err, "phase", None)
                    or getattr(self._hon_instance, "auth_phase", "")
                    or None
                )
                self.last_error_code = classify(err, phase=self.last_error_phase)
                self.last_phase_ledger = (
                    getattr(self._hon_instance, "phase_ledger", None) or None
                )
                _LOGGER.error(
                    # error_detail() strips a leading "ADDHON-NNN: " so the code is not
                    # printed twice ("Validation failed [ADDHON-400]: ADDHON-400: ..."
                    # is the doubled line reported in #76).
                    "hOn setup failed [%s] (phase=%s): %s [%s]",
                    self.last_error_code.label,
                    self.last_error_phase or "?",
                    error_detail(err),
                    getattr(self._hon_instance, "phase_summary", "") or "no ledger",
                )
                self.emit_auth_diagnostics(
                    self.last_error_code,
                    self.last_error_phase or "setup",
                    classify_failure_reason(err),
                )
                # BEFORE _close_sync, which nulls _hon_instance and with it the only
                # reader of the census (`last_appliance_fetch`). This is the whole
                # reason the failure record can say "the POST never reached a body":
                # after the next line that fact exists nowhere else in the process.
                self.last_setup_fetch = self.last_appliance_fetch
                self._close_sync()
                raise

    async def async_complete_setup(self) -> None:
        """Verify that the setup completed successfully."""
        if self._api is None:
            raise RuntimeError("setup_sync() did not complete the hOn login")

    def emit_auth_diagnostics(
        self, code: Any, phase: str, reason: str = "unexpected"
    ) -> None:
        """Flush the opt-in authentication trace once using controlled fields."""
        self._auth_trace.flush(
            _LOGGER,
            code=getattr(code, "label", code),
            phase=phase,
            reason=reason,
        )

    def discard_auth_diagnostics(self) -> None:
        """Discard an opt-in trace after successful validation or flow cleanup."""
        self._auth_trace.discard()

    # -- Two-factor (email OTP) resume -----------------------------------------

    @property
    def refresh_token(self) -> str:
        """The current OAuth refresh token after a successful login (for persistence)."""
        hon = self._hon_instance
        return getattr(hon, "refresh_token", "") if hon is not None else ""

    def submit_mfa_code_sync(self, context: Any, code: str) -> None:
        """Resume a paused 2FA login with the user's OTP, on the dedicated loop.

        Runs setup to completion on the SAME (kept-alive) session. Call in executor."""
        with self._lifecycle_lock:
            if self._hon_instance is None:
                raise RuntimeError("no pending MFA challenge")
            try:
                # The resume verifies the OTP and then runs the full setup, so it gets
                # the OTP exchange PLUS the same setup watchdog setup_sync chose --
                # including the tighter validation one when a human is waiting on the
                # config-flow form, which is the whole reason the two were split.
                self._api = self._run_on_hon_loop(
                    self._hon_instance.submit_mfa_code(context, code),
                    budget.MFA_RESUME
                    + (budget.VALIDATION_CAP if self._validation else budget.SETUP_CAP),
                )
            except Exception as err:
                # Record the precise code/phase so the form + diagnostics reflect the real
                # cause (wrong code vs service error vs token-after-verify), not a stale one.
                self.last_error_phase = getattr(self._hon_instance, "auth_phase", "") or "mfa_verify"
                self.last_error_code = classify(err, phase=self.last_error_phase)
                raise
            # 2FA resolved: clear the challenge record set by setup_sync's MFA branch so a
            # later (unrelated) failure is never shown with the stale 2FA phase/summary.
            self.last_error_code = None
            self.last_error_phase = None
            self.last_mfa_summary = None
            # Sync the seed to the token just minted by the 2FA flow so a later reauth/
            # restart does not re-trigger the OTP (#1).
            self._refresh_token = self.refresh_token or self._refresh_token
            _LOGGER.info("hOn 2FA verification succeeded for %s", redact_email(self._email))
            # Re-apply the realtime notify callback to the now-completed session (#20).
            if self._notify_function is not None:
                self._hon_instance.subscribe_updates(self._notify_function)

    def resend_mfa_code_sync(self, context: Any) -> None:
        """(Re)send the email OTP for a pending challenge, on the dedicated loop."""
        with self._lifecycle_lock:
            if self._hon_instance is None:
                raise RuntimeError("no pending MFA challenge")
            try:
                self._run_on_hon_loop(
                    self._hon_instance.resend_mfa_code(context), budget.COMMAND
                )
            except Exception as err:
                self.last_error_phase = "mfa_send"
                self.last_error_code = classify(err, phase=self.last_error_phase)
                raise

    def run_command_sync(self, coro) -> Any:
        """Run a client coroutine (e.g. command.send()) on the dedicated loop.

        To be called in executor, not on HA's event loop.
        """
        return self._run_on_hon_loop(coro, budget.COMMAND)

    def dispatch_patch_sync(self, appliance, patch: CommandPatch) -> bool:
        return self._run_on_hon_loop(
            self._command_dispatcher.dispatch(appliance, patch), budget.COMMAND
        )

    # -- Appliances -----------------------------------------------------------

    async def async_get_appliances(self) -> list:
        if self._api is None:
            raise RuntimeError("hOn session unavailable")
        try:
            return self._api.appliances
        except Exception as err:
            _LOGGER.error("Error fetching appliances: %s", err)
            raise RuntimeError(f"Error fetching appliances: {err}") from err

    @property
    def last_appliance_fetch(self) -> dict | None:
        """The live session's appliance-list fetch census, or None if there is none.

        Read LIVE off the session rather than mirrored into a field of this client. A
        mirror would need a clear beside the ones at the top of setup_sync and a second
        write on the MFA-resume path, and forgetting either is how a census ends up
        describing a session the client has already thrown away. Reading through
        `_hon_instance` cannot drift: `_close_sync` nulls it under the lifecycle lock,
        so the answer is either this session's census or None.

        getattr, not an attribute read: `_hon_instance` is None before the first setup
        and after a close, and a session double is free not to carry the field.
        """
        return getattr(self._hon_instance, "last_appliance_fetch", None)

    @property
    def appliance_count(self) -> int | None:
        """How many appliances the live session actually built (None: no session).

        The same number as `last_poll.returned` whenever both describe the same
        session -- that one is a recount of this list at the end of a poll cycle
        (`_poll_census`, written at :1300) -- but the two have INDEPENDENT
        resets, and the window where that matters is a SUCCESSFUL runtime re-auth:
        `last_poll_census` is nulled at the top of every setup_sync (:660) and
        rewritten only when a whole cycle closes, so between the end of setup_sync
        and the end of the first poll `last_poll` is null and this is the only
        number in the dump that says how many appliances the setup built. That
        window is exactly when someone downloads a dump saying "I reloaded and my
        entities are gone".

        isinstance before len(): `_hon_instance` can be a session double, and a
        length taken off an arbitrary object is how a convenience field in a dump
        learns to raise.
        """
        appliances = getattr(self._hon_instance, "appliances", None)
        return len(appliances) if isinstance(appliances, list) else None

    @property
    def setup_expanded(self) -> int | None:
        """How many appliance objects the live session's setup accounted for.

        Read off the session for the reason `last_appliance_fetch` above gives at
        length: a copy kept on this client would need a clear that someone will
        eventually forget, and would then describe a session already discarded.
        With `built` beside it the reader can check
        `built + sum(skipped.values()) == expanded` and know the census accounts
        for every appliance the cloud sent.
        """
        return getattr(self._hon_instance, "setup_expanded", None)

    @property
    def setup_drops(self) -> dict[str, int] | None:
        """Reason -> count for the appliances the live setup dropped, or None.

        The session already reduced this to tokens it declares itself
        (`client/session.py:SETUP_DROP_REASONS`), so no identity crosses this
        boundary and the diagnostics reader still filters it against its own
        allowlist rather than trusting the mapping it receives.
        """
        return getattr(self._hon_instance, "setup_drops", None)

    @property
    def degraded_census(self) -> dict[str, int] | None:
        """ADDHON label -> count for the appliances the live setup KEPT broken.

        The other half of the account beside `setup_drops`, and the half that
        answers the commoner report: `setup_drops` counts appliances that never
        became entities at all, this counts the ones that exist with an empty
        `commands` map and therefore lost their select/number/switch/button/
        climate/fan entities until a manual reload (`client/session.py:393-394`).
        Read together they say whether a shrunken entity list is missing devices or
        missing capabilities, which nothing in the dump could distinguish before.

        Deliberately NOT `degraded_appliances`, which is the same information keyed
        by f"{mac}#{zone}". The session publishes the reduced form itself
        (`client/session.py:581`) so a plaintext MAC is never handed across this
        boundary for a reader to remember not to write down; the diagnostics reader
        then still shape-checks every label it receives (`_degraded_census`) instead
        of trusting the mapping, for the same reason `setup_drops` is filtered above.
        """
        return getattr(self._hon_instance, "degraded_census", None)

    def _needs_rehydration(self, appliance) -> bool:
        """Did setup append this appliance without its commands, for a retryable reason?

        getattr so a session double (or an older session object) simply answers "no"
        instead of breaking the poll.
        """
        asker = getattr(self._api, "needs_rehydration", None)
        return bool(callable(asker) and asker(appliance))

    def _update_appliance_sync(self, appliance) -> None:
        """Update an appliance on the dedicated loop (synchronous, called in executor)."""

        async def _do_update():
            update_returned_empty = False
            _debug_appliance_consumption("before update", appliance)

            # Attempt 0, first poll only: RE-HYDRATE an appliance the setup contained a
            # transport fault for (client/session.py::needs_rehydration). update() only
            # reloads the ATTRIBUTES, so without this the appliance would sail through
            # the first snapshot with empty `commands` -- and since the platforms call
            # async_add_entities exactly once, from that snapshot, and the integration
            # has no dynamic discovery, its select/number/switch/button/climate/fan
            # entities would never exist until a MANUAL reload. That is the half of the
            # fault boundary that makes containing the failure legitimate rather than a
            # silently crippled entry. After the first poll it is pointless (the
            # entities are already decided), so it costs at most one request per
            # degraded appliance, once.
            if not self._first_poll_done and self._needs_rehydration(appliance):
                loader = getattr(appliance, "load_commands", None)
                if callable(loader):
                    # NOT tolerated: a second failure propagates into the strict
                    # first-poll branch -> ConfigEntryNotReady -> Home Assistant retries
                    # the whole setup, the only path that can still produce a COMPLETE
                    # entity inventory.
                    #
                    # Under the SAME scope pair `NativeHon._create_appliance` opens for
                    # the identical call at setup (client/session.py). Without it this
                    # was the one await on the poll path with no budget and no phase, so
                    # a per-request aiohttp timeout left it BARE -- and a bare
                    # TimeoutError carries no phase, so `representative_failure` maps it
                    # to the mute ADDHON-460 "Setup timed out" instead of the
                    # ADDHON-400 the failure actually is. Converted here it is attributed
                    # (issue #76), and the budget stops an unbounded retry of the very
                    # request that already failed once from spending the whole cap.
                    with phase_scope(
                        "load_appliance",
                        # Same attribute `_needs_rehydration` asked above: both names
                        # hold the one NativeHon (`__aenter__` returns self), and reading
                        # one of them here keeps the guard and the scope from drifting.
                        getattr(self._api, "_phase_tracker", None),
                    ):
                        async with budget.budgeted(budget.APPLIANCE_ONE):
                            await loader()
                    _debug_appliance_consumption("after rehydrating commands", appliance)

            # Attempt 1: standard update()
            if hasattr(appliance, "update") and callable(appliance.update):
                try:
                    await appliance.update()
                    attrs_after_update = _get_attributes(appliance)
                    _debug_appliance_consumption("after update()", appliance, attrs_after_update)
                    if attrs_after_update:
                        stats_method = getattr(appliance, "load_statistics", None)
                        if callable(stats_method):
                            try:
                                await stats_method()
                                attrs_after_update = _get_attributes(appliance)
                                _debug_appliance_consumption(
                                    "after load_statistics post-update",
                                    appliance,
                                    attrs_after_update,
                                )
                            except Exception as err:
                                if _must_propagate(err):
                                    raise
                                _LOGGER.debug(
                                    "load_statistics after update() failed for '%s' "
                                    "(type=%s): %s",
                                    redact_id(_get_name(appliance)),
                                    _get_type(appliance),
                                    err,
                                )
                        _LOGGER.debug(
                            "Consumption debug: update() produced %d attributes for '%s' "
                            "(type=%s); statistics reloaded if available; "
                            "load_attributes/load_commands fallback not run in this cycle.",
                            len(attrs_after_update),
                            redact_id(_get_name(appliance)),
                            _get_type(appliance),
                        )
                        return
                    update_returned_empty = True
                    _LOGGER.debug("update() completed with no data, trying load_*")
                except Exception as err:
                    if _must_propagate(err):
                        raise
                    _LOGGER.debug("update() failed: %s, trying load_*", err or "<no msg>")

            # Attempt 2: load_attributes / load_commands / load_statistics
            loaded = False
            for method_name in ("load_attributes", "load_commands", "load_statistics"):
                method = getattr(appliance, method_name, None)
                if method and callable(method):
                    try:
                        await method()
                        loaded = True
                        _LOGGER.debug("Fallback OK: %s", method_name)
                        _debug_appliance_consumption(f"after {method_name}", appliance)
                    except Exception as err:
                        # Match the primary update() path via the shared _must_propagate
                        # predicate: a failed load_statistics is non-fatal -- it only
                        # carries the consumption counters -- UNLESS it is an auth/retryable
                        # error the caller must act on. Without this, a single stats hiccup
                        # made the WHOLE appliance unavailable in the fallback path while the
                        # primary path tolerated it (inconsistent). load_attributes and
                        # load_commands stay fatal: that IS the appliance's data.
                        if method_name == "load_statistics" and not _must_propagate(err):
                            _LOGGER.debug(
                                "Fallback load_statistics tolerated (non-auth): %s", err
                            )
                            continue
                        _LOGGER.debug("Fallback %s failed: %s", method_name, err)
                        raise RuntimeError(f"Fallback {method_name} failed: {err}") from err

            if not loaded:
                if update_returned_empty:
                    raise RuntimeError(
                        "update() completed with no data and load_* fallbacks not available"
                    )
                raise RuntimeError(
                    "No update method available, "
                    "check the integration version."
                )

        # One appliance = the waves client/budget.py sizes APPLIANCE_ONE on, plus the
        # lazy sign-in a rejected token can start inline. A cap is waited on from THIS
        # thread and cannot be suspended the way a scope budget is, so it has to
        # contain the sign-in instead (client/budget.py::cap).
        self._run_on_hon_loop(_do_update(), budget.APPLIANCE_POLL)

    # -- Re-auth ---------------------------------------------------------------

    async def _async_reauth(self) -> bool:
        """Re-authenticate in case of an expired token."""
        _LOGGER.info("hOn re-authentication attempt...")
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._close_sync)
            await loop.run_in_executor(None, self.setup_sync)
            _LOGGER.info("hOn re-authentication succeeded")
            return True
        except Exception as err:
            # A background re-auth cannot prompt for a 2FA code: setup_sync keeps the
            # half-open session alive on MFAChallengeRequired (for the interactive
            # resume that does not exist here), so close it to avoid leaking the
            # loop/session. The failure routes to the reauth flow via the caller.
            _LOGGER.error("hOn re-authentication failed: %s", err)
            try:
                await loop.run_in_executor(None, self._close_sync)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                _LOGGER.debug("hOn re-auth cleanup close failed", exc_info=True)
            return False

    # -- Realtime push (MQTT) --------------------------------------------------

    @staticmethod
    def _appliance_id(appliance: Any) -> str:
        return (
            getattr(appliance, "unique_id", None)
            or _get_serial(appliance)
            or str(id(appliance))
        )

    @staticmethod
    def _build_appliance_entry(appliance: Any) -> dict[str, Any]:
        """Coordinator entry for one appliance from its CURRENT in-memory state.

        Shared by the HTTP poll (async_get_appliances_data) and the realtime
        snapshot so the two never diverge in shape. Reads only, no network.
        """
        return {
            "appliance": appliance,
            "type": _get_type(appliance),
            "name": _get_name(appliance),
            "model": _get_model(appliance),
            "serial": _get_serial(appliance),
            "mac": _get_mac(appliance),
            "attributes": _get_attributes(appliance),
            "statistics": _debug_container_to_dict(
                getattr(appliance, "statistics", None), "statistics"
            ),
            "settings": dict(appliance.settings) if hasattr(appliance, "settings") else {},
        }

    def build_realtime_snapshot(self) -> dict[str, Any]:
        """Coordinator snapshot from the appliances already mutated in-memory by the
        MQTT push (NO HTTP poll). Built on the awscrt thread by the notify callback;
        a failing appliance is skipped, never the whole snapshot."""
        hon = self._hon_instance
        appliances = getattr(hon, "appliances", None) or [] if hon is not None else []
        data: dict[str, Any] = {}
        for appliance in appliances:
            try:
                data[self._appliance_id(appliance)] = self._build_appliance_entry(appliance)
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.debug("Realtime snapshot: skipping an appliance: %s", err)
        return data

    def subscribe_updates(self, notify_function: Any) -> None:
        """Register (or clear with None) the realtime notify callback.

        Stored on the client so it survives a re-auth (which rebuilds the session):
        setup_sync re-applies it to the new session (#20). Forwarded to the current
        session when one exists; if none exists (before setup, or after close on the
        unload detach) it is just remembered -- NO raise, so subscribe_updates(None)
        on unload is a clean no-op (#28)."""
        self._notify_function = notify_function
        hon = self._hon_instance
        if hon is not None:
            hon.subscribe_updates(notify_function)

    # -- Data polling ----------------------------------------------------------

    async def async_get_appliances_data(self) -> dict[str, Any]:
        reauth_attempted = False

        while True:
            data: dict[str, Any] = {}
            failed_appliances: list[tuple[str, Exception]] = []
            retry_after_reauth = False
            try:
                appliances = await self.async_get_appliances()
            except Exception as err:
                if _requires_reauth(err) and not reauth_attempted:
                    _LOGGER.warning("Haier auth error while fetching devices, starting re-authentication")
                    if not await self._async_reauth():
                        raise RuntimeError(
                            f"Haier auth error while fetching devices: {err}"
                        ) from err
                    reauth_attempted = True
                    continue
                raise
            _LOGGER.debug("Found %d hOn devices", len(appliances))
            if appliances:
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "Discovery: appliance inventory from the cloud - %s",
                        "; ".join(
                            f"type={_get_type(a)} mac={redact_mac(_get_mac(a)) or '<no-mac>'} "
                            f"name={redact_id(_get_name(a))}"
                            for a in appliances
                        ),
                    )
            else:
                # INFO, not DEBUG: it is the pointer to the WARNING that carries the
                # whole diagnosis, and a pointer that only appears when debug is on is
                # a pointer nobody follows. It fires only on the empty-list branch, so
                # a working install never sees it.
                _LOGGER.info(
                    "Discovery: the hOn cloud returned 0 appliances for this "
                    "account (request OK). With the unified-api endpoint the list "
                    "also includes offline devices, so 0 = a truly empty/not-shared "
                    "account OR a new API change (it is NOT 'almost always on the "
                    "account side', see the 0-appliance/v2.7.1 bug history). "
                    "Details in the WARNING of the "
                    "custom_components.addhon.client.transport.api logger."
                )

            for idx, appliance in enumerate(appliances, 1):
                try:
                    _LOGGER.debug(
                        "Discovery: processing appliance %d/%d - type=%s mac=%s name=%s",
                        idx,
                        len(appliances),
                        _get_type(appliance),
                        redact_mac(_get_mac(appliance)) or "<no-mac>",
                        redact_id(_get_name(appliance)),
                    )
                    last_err = None
                    for attempt in range(3):
                        try:
                            await asyncio.get_running_loop().run_in_executor(
                                None, self._update_appliance_sync, appliance
                            )
                            last_err = None
                            break
                        except Exception as err:
                            last_err = err
                            if _is_retryable_server_error(err) and attempt < 2:
                                wait = 5 * (attempt + 1)
                                _LOGGER.warning(
                                    "Haier server error (attempt %d/3), retrying in %ds: %s",
                                    attempt + 1, wait, err,
                                )
                                await asyncio.sleep(wait)
                            elif _requires_reauth(err):
                                break
                            else:
                                break

                    if last_err is not None:
                        raise last_err

                    appliance_id = self._appliance_id(appliance)
                    attributes = _get_attributes(appliance)
                    name = _get_name(appliance)
                    app_type = _get_type(appliance)
                    data[appliance_id] = self._build_appliance_entry(appliance)
                    _debug_appliance_consumption("coordinator snapshot", appliance, attributes)
                    _LOGGER.debug(
                        "Updated '%s' (type=%s, mac=%s, id=%s) - %d attributes",
                        redact_id(name), app_type, redact_mac(_get_mac(appliance)) or "<no-mac>",
                        redact_mac(appliance_id), len(attributes),
                    )

                except Exception as err:
                    _LOGGER.warning(
                        "Error updating '%s' (type=%s): %s",
                        redact_id(_get_name(appliance)), _get_type(appliance), err,
                        exc_info=True,
                    )
                    if _requires_reauth(err):
                        if reauth_attempted:
                            raise RuntimeError(
                                f"Haier auth error while updating "
                                f"'{redact_id(_get_name(appliance))}': {err}"
                            ) from err
                        _LOGGER.warning("Haier auth error, starting re-authentication")
                        if not await self._async_reauth():
                            raise RuntimeError(
                                f"Haier auth error while updating "
                                f"'{redact_id(_get_name(appliance))}': {err}"
                            ) from err
                        reauth_attempted = True
                        retry_after_reauth = True
                        break
                    # FIRST poll: STRICT. Platform setup iterates this snapshot once and
                    # there is no dynamic-discovery path, so an appliance absent from the
                    # first snapshot would get NO entities until a reload. Re-raise so the
                    # first refresh fails -> ConfigEntryNotReady -> HA retries setup until
                    # the full inventory loads. (Also surfaces genuine setup-time bugs.)
                    if not self._first_poll_done:
                        # Same code-preservation as the all-failed path (CR#6): a bare
                        # RuntimeError would classify to UNKNOWN. Reuse the helper (with
                        # a single failure) so the real non-auth cause is surfaced while
                        # the STRICT first-poll semantics are unchanged (it still
                        # re-raises -> UpdateFailed -> ConfigEntryNotReady -> HA retries).
                        code, cause = _representative_failure(
                            [(redact_id(_get_name(appliance)), err)]
                        )
                        raise HonCodedError(
                            code,
                            "Error updating an appliance on the first poll",
                            # Carry the phase the cause already knows (its twin in
                            # session.py::setup passes phase="load_appliance"). Without
                            # it Download Diagnostics showed phase=null on exactly the
                            # failure the hierarchical phase was introduced to name:
                            # __init__.py reads `getattr(err, "phase", None)` off THIS
                            # wrapper, and the chain below it is not consulted.
                            phase=getattr(cause, "phase", None),
                        ) from cause
                    # Steady state: per-appliance resilience. A non-auth failure on ONE
                    # appliance (a transient cloud 5xx that outlived the retries, a
                    # malformed payload, ...) must NOT blank EVERY device. Record it and
                    # move on: this appliance is simply absent from the snapshot (its
                    # entities go unavailable until the next poll succeeds) while the
                    # others stay live. A TOTAL failure (all errored -> empty data) is
                    # re-raised below so the coordinator marks the cycle failed instead of
                    # publishing an empty snapshot that silently blanks everything.
                    failed_appliances.append((redact_id(_get_name(appliance)), err))
                    continue

            if retry_after_reauth:
                continue

            # Every appliance has been attempted, so THIS is the cycle the census
            # describes -- including the total failure just below, which raises and
            # would otherwise leave the dump with nothing but a stale snapshot to
            # explain itself. Deliberately not recorded on the strict first-poll
            # re-raise above: that one aborts mid-loop, and counting the appliances
            # the loop never reached as survivors would be a false statement about
            # exactly the dump this exists to explain.
            self.last_poll_census = _poll_census(
                len(appliances), len(data), failed_appliances
            )

            if appliances and not data and failed_appliances:
                # Every appliance failed this cycle: surface a failed update (the
                # coordinator keeps its last good snapshot and retries) instead of
                # returning an empty one that would blank all devices at once. Carry a
                # representative NON-AUTH code so the ADDHON-NNN catalog reports the real
                # cause instead of UNKNOWN (CR#6); the redacted names go to the WARNING,
                # never into the HonCodedError message (its contract forbids identity).
                code, cause = _representative_failure(failed_appliances)
                _LOGGER.warning(
                    "[%s] Update failed for all %d appliances: %s",
                    code.label,
                    len(failed_appliances),
                    ", ".join(name for name, _err in failed_appliances),
                )
                raise HonCodedError(
                    code,
                    f"Update failed for all {len(failed_appliances)} appliance(s)",
                    # Same reason as the first-poll wrapper below: `__init__.py` reads
                    # `phase` off THIS object and never walks the `__cause__` chain, so
                    # without forwarding it a total-failure cycle files phase=null in
                    # Download Diagnostics even when the cause knows its phase.
                    phase=getattr(cause, "phase", None),
                ) from cause

            if failed_appliances:
                _LOGGER.warning(
                    "Partial update: %d/%d appliances updated this cycle, "
                    "skipped (unavailable until next poll): %s",
                    len(data),
                    len(appliances),
                    ", ".join(name for name, _err in failed_appliances),
                )

            _LOGGER.info("Loaded %d hOn devices with data", len(data))
            # Keep the SEED current with a mid-life token rotation (a runtime auth.refresh()
            # during this poll rotates the live token but not the seed): without this, an
            # _async_reauth() that fires after a rotation would re-seed the new session from
            # a consumed token and fall back to a full login / 2FA re-prompt. The restart
            # path is already covered by entry.data persistence; this covers in-process
            # reauth after a rotation. (#1 residual)
            self._refresh_token = self.refresh_token or self._refresh_token
            # From now on the poll is resilient (skip a failed appliance, keep the rest):
            # all entities have been created from this first complete snapshot.
            self._first_poll_done = True
            return data

    # -- Closing ---------------------------------------------------------------

    async def async_close(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._close_sync)

    def close_sync(self) -> None:
        """Blocking close, for a caller that has no running loop to await on.

        Public counterpart of :meth:`async_close` (which is this exact teardown handed to
        an executor). Callers outside this module use THIS name: reaching for the private
        one degrades to a silent no-op the day it is renamed. Blocking: it waits on the
        dedicated loop and joins its thread, so never call it from an event loop."""
        self._close_sync()
