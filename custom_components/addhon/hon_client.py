"""Asynchronous client for Haier's hOn APIs."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any

from .debug_utils import debug_key_sample, redact_email, redact_id, redact_mac
from .error_codes import (
    APPLIANCE_LOAD_FAILED,
    MFA_REQUIRED,
    UNKNOWN,
    HonCodedError,
    HonErrorCode,
    classify,
    phase_timeout_code,
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
    return any(k in err_str for k in (
        "internal server error",
        "server error",
        "500",
        "502",
        "503",
        "504",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "too many requests",
        "429",
    ))


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


def _representative_failure(
    failures: list[tuple[str, Exception]]
) -> tuple[HonErrorCode, Exception | None]:
    """Pick a representative (code, error) from per-appliance update failures (CR#6).

    The all-failed (and first-poll) paths used to raise a bare RuntimeError, which
    classify() maps to UNKNOWN (ADDHON-999) -- losing the real cause from the logs,
    the UpdateFailed message and Download Diagnostics. This surfaces a MEANINGFUL,
    NON-AUTH code instead: deterministically, the FIRST failure (in poll order) whose
    classify() is neither UNKNOWN nor a reauth code; if none qualifies, fall back to
    APPLIANCE_LOAD_FAILED (ADDHON-220) paired with the first error.

    Rejecting reauth codes is what keeps routing correct. Every error here already
    passed the non-auth gate (_requires_reauth was False at the call site), but
    classify() is substring-based and could still name an auth code (e.g. a message
    that merely contains "login")  -- surfacing it would flip the transient
    UpdateFailed into a reauth (ConfigEntryAuthFailed). APPLIANCE_LOAD_FAILED is
    requires_reauth=False, so the fallback stays non-auth too.
    """
    chosen: Exception | None = None  # the first failure, kept as the fallback cause
    for _name, err in failures:
        if chosen is None:
            chosen = err
        code = classify(err)
        if code is not UNKNOWN and not code.requires_reauth:
            return code, err
    # No meaningful non-auth code found (or -- defensively -- an empty list, which the
    # two gated call sites never pass): fall back to APPLIANCE_LOAD_FAILED, NEVER UNKNOWN.
    return APPLIANCE_LOAD_FAILED, chosen


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
        # Last classified error code (for the downloadable diagnostics / log parity).
        self.last_error_code: Any = None
        # Login phase reached at the last failure ("authenticate"/"mfa_verify"/...), and a
        # leak-proof 2FA summary, surfaced in the downloadable diagnostics for triage.
        self.last_error_phase: str | None = None
        self.last_mfa_summary: dict | None = None
        self._hon_instance = None
        self._api = None
        self._hon_loop: asyncio.AbstractEventLoop | None = None
        self._hon_thread: threading.Thread | None = None
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

    def _run_on_hon_loop(self, coro) -> Any:
        """Run a coroutine on the dedicated loop and wait for the result.

        Call only from a non-loop thread (e.g. HA's executor).
        """
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

            try:
                return future.result(timeout=self._RUN_TIMEOUT)
            except concurrent.futures.TimeoutError as timeout_err:
                drain_future: concurrent.futures.Future = concurrent.futures.Future()

                def _cancel_and_drain() -> None:
                    task = task_holder.get("task")
                    if task is None:
                        future.cancel()
                        if not drain_future.done():
                            drain_future.set_result(None)
                        return

                    async def _drain_task() -> None:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        except Exception as err:
                            _LOGGER.debug("Error while cancelling hOn task: %s", err)
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
                # The bare concurrent.futures.TimeoutError has no message and the
                # cancelled coroutine's own exception is gone, so re-raise it as a
                # phase-attributed coded error: the dedicated loop runs setup() on the
                # hOn session, which records where it stalled (auth / appliance list /
                # MQTT) in _setup_phase. This is what turns the #30 "spins then
                # cannot_connect" into a precise ADDHON-NNN. (phase "" -> LOOP_TIMEOUT.)
                phase = getattr(self._hon_instance, "_setup_phase", "") or ""
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
                    self._run_on_hon_loop(hon.__aexit__(None, None, None))
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
            try:
                if self._hon_loop is None or not self._hon_loop.is_running():
                    self._start_hon_loop()

                self._hon_instance = create_session(
                    self._email,
                    self._password,
                    enable_mqtt=not self._validation,
                    minimal=self._validation,
                    refresh_token=self._refresh_token,
                )
                _LOGGER.debug("Hon instance created")

                # Login + aiohttp session init, on the dedicated loop
                self._api = self._run_on_hon_loop(self._hon_instance.__aenter__())
                _LOGGER.info("Connection to hOn succeeded for %s", redact_email(self._email))
                # Re-apply the realtime notify callback to the freshly built session
                # (rebuilt on every setup/re-auth with _notify_function=None);
                # without this the MQTT push is a permanent no-op after a re-auth (#20).
                if self._notify_function is not None:
                    self._hon_instance.subscribe_updates(self._notify_function)
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
                self.last_error_phase = getattr(self._hon_instance, "auth_phase", "") or None
                self.last_error_code = classify(err)
                _LOGGER.error(
                    "hOn setup failed [%s] (phase=%s): %s",
                    self.last_error_code.label, self.last_error_phase or "?", err,
                )
                self._close_sync()
                raise

    async def async_complete_setup(self) -> None:
        """Verify that the setup completed successfully."""
        if self._api is None:
            raise RuntimeError("setup_sync() did not complete the hOn login")

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
                self._api = self._run_on_hon_loop(
                    self._hon_instance.submit_mfa_code(context, code)
                )
            except Exception as err:
                # Record the precise code/phase so the form + diagnostics reflect the real
                # cause (wrong code vs service error vs token-after-verify), not a stale one.
                self.last_error_phase = getattr(self._hon_instance, "auth_phase", "") or "mfa_verify"
                self.last_error_code = classify(err)
                raise
            # 2FA resolved: clear the challenge record set by setup_sync's MFA branch so a
            # later (unrelated) failure is never shown with the stale 2FA phase/summary.
            self.last_error_code = None
            self.last_error_phase = None
            self.last_mfa_summary = None
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
                self._run_on_hon_loop(self._hon_instance.resend_mfa_code(context))
            except Exception as err:
                self.last_error_phase = "mfa_send"
                self.last_error_code = classify(err)
                raise

    def run_command_sync(self, coro) -> Any:
        """Run a client coroutine (e.g. command.send()) on the dedicated loop.

        To be called in executor, not on HA's event loop.
        """
        return self._run_on_hon_loop(coro)

    # -- Appliances -----------------------------------------------------------

    async def async_get_appliances(self) -> list:
        if self._api is None:
            raise RuntimeError("hOn session unavailable")
        try:
            return self._api.appliances
        except Exception as err:
            _LOGGER.error("Error fetching appliances: %s", err)
            raise RuntimeError(f"Error fetching appliances: {err}") from err

    def _update_appliance_sync(self, appliance) -> None:
        """Update an appliance on the dedicated loop (synchronous, called in executor)."""

        async def _do_update():
            update_returned_empty = False
            _debug_appliance_consumption("before update", appliance)

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
                                if _requires_reauth(err) or _is_retryable_server_error(err):
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
                    if _requires_reauth(err) or _is_retryable_server_error(err):
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

        self._run_on_hon_loop(_do_update())

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
                _LOGGER.debug(
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
                            code, "Error updating an appliance on the first poll"
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
            # From now on the poll is resilient (skip a failed appliance, keep the rest):
            # all entities have been created from this first complete snapshot.
            self._first_poll_done = True
            return data

    # -- Closing ---------------------------------------------------------------

    async def async_close(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._close_sync)
