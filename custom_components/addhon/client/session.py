# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""addhOn `NativeHon` session orchestration.

Coordinates the setup on top of the native transport (`transport.connection.HonConnection` +
`transport.api.HonApi`) and builds the native appliances (`engine.appliance.HonAppliance`)
via `factory.create_appliance`, into which it injects our `api`.

Boundary: appliance construction goes through `factory.create_appliance`; the MQTT is
NATIVE (`transport.mqtt.NativeMqttClient`, lazy import in `_make_mqtt`).
`NativeHon` satisfies the Protocol `interfaces.HonSession` and exposes `.api`/`.appliances`/
`subscribe_updates`/`notify` (the MQTT client reads exactly those members).

Setup sequence: create connection -> `api.load_appliances()` -> for each appliance
build the HonAppliance and load commands/attributes -> start MQTT. The order matters:
the load_* calls make the first HTTP requests that populate the tokens, so that when
MQTT starts `api.auth.id_token` is present. The statistics are NOT loaded here (issue
#76): the first coordinator refresh redoes them anyway, before any platform is
forwarded.
"""
from __future__ import annotations

import logging
from types import TracebackType
from typing import Any

import aiohttp

from . import factory
from ..debug_utils import redact_mac
from .budget import APPLIANCE_LIST, APPLIANCE_ONE, MQTT_START, budgeted
from .phase import PhaseTracker, phase
from .transport.api import HonApi
from .transport.auth import MFAChallengeRequired, NativeAuthError
from .transport.connection import HonConnection
from ..error_codes import (
    APPLIANCE_DATA_MALFORMED,
    HonCodedError,
    classify,
    representative_failure,
)

_LOGGER = logging.getLogger(__name__)


class NativeHon:
    """Native hOn session: OUR auth, transport and parser engine.

    Async context manager that exposes `.appliances` (and `.api` for MQTT) to the
    integration. `enable_mqtt=False` skips the AWS push (useful for tests/validators;
    production leaves it active).
    """

    def __init__(
        self,
        email: str = "",
        password: str = "",
        session: aiohttp.ClientSession | None = None,
        mobile_id: str = "",
        refresh_token: str = "",
        enable_mqtt: bool = True,
        minimal: bool = False,
        auth_trace: Any = None,
    ) -> None:
        self._email = email
        self._password = password
        self._session = session
        self._mobile_id = mobile_id
        self._refresh_token = refresh_token
        self._enable_mqtt = enable_mqtt
        # minimal=True (config-flow validation): authenticate + load_appliances only,
        # skip the per-appliance command/attribute/statistics loads (issue #30). The
        # full setup runs at runtime (minimal=False).
        self._minimal = minimal
        self._auth_trace = auth_trace
        self._connection: HonConnection | None = None
        self._api: HonApi | None = None
        self._appliances: list[Any] = []
        self._mqtt_client: Any = None
        self._notify_function: Any = None
        # Coarse setup phase, read by HonClient when the dedicated-loop cap fires
        # to attribute the (otherwise message-less) timeout to a stable error code.
        # Kept FLAT ("load_appliances", "" when done) -- it is the shipped mirror the
        # MQTT layer writes and the tests pin; `current_phase` below is the new,
        # hierarchical one.
        self._setup_phase: str = ""
        # Hierarchical phase mirror + timing ledger, shared down into the connection and
        # the auth layer so a lazy sign-in names itself (client/phase.py, issue #76).
        self._phase_tracker = PhaseTracker()
        # "mac#zone" -> classified code for appliances whose hydration failed, for BOTH
        # reasons an appliance can be appended half-built: a transport fault and a
        # malformed payload. An entry means "the device exists but its data is
        # partial", not "setup failed". The ZONE is part of the key because a
        # multi-zone appliance is built once per zone under the SAME mac; without it
        # the all-failed guard below would under-count the failures and ship a fully
        # broken entry in silence.
        self._hydration_failures: dict[str, Any] = {}
        # The same failures with their exception, in order: the all-failed guard
        # re-raises the REAL cause instead of a generic ADDHON-220 (the loss of cause
        # CR#6 had already fixed on the poll path).
        self._hydration_causes: list[tuple[str, Exception]] = []
        # The subset a RETRY could still fix: appliances left partial by a transport
        # fault, held by identity. Two readers, and both are the reason the fault
        # boundary is not just a swallowed exception -- the all-failed guard below and
        # `needs_rehydration`, which makes the first coordinator refresh re-run
        # load_commands before any entity is created.
        self._retryable_partials: list[Any] = []

    async def __aenter__(self) -> "NativeHon":
        return await self.create()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def api(self) -> HonApi:
        if self._api is None:
            raise NativeAuthError("session not created (create() is missing)")
        return self._api

    @property
    def appliances(self) -> list[Any]:
        # Read-only on purpose: no setter. The MQTT client binds this list by
        # reference at __init__, so the inventory is mutated IN PLACE (setup()
        # clears + appends) and never rebound -- a `session.appliances = [...]`
        # would swap the object out from under the live subscriptions.
        return self._appliances

    async def create(self) -> "NativeHon":
        try:
            self._connection = await HonConnection(
                self._email,
                self._password,
                session=self._session,
                mobile_id=self._mobile_id,
                refresh_token=self._refresh_token,
                auth_trace=self._auth_trace,
                phase_tracker=self._phase_tracker,
            ).create()
            self._api = HonApi(self._connection)
            await self.setup()
        except MFAChallengeRequired:
            # Interactive 2FA: the email-OTP challenge surfaced during setup(). The
            # connection/session/api are kept ALIVE (no close()) so submit_mfa_code()
            # can resume on the SAME session (its cookies bind the verification). The
            # caller (config flow) drives the resume; a background setup that cannot
            # prompt closes the client itself and routes to the reauth flow.
            raise
        except BaseException:
            # setup() makes the first HTTP calls (and may start MQTT) and can raise
            # (network/auth). When a caller uses `async with NativeHon(...)`, a failure
            # in __aenter__/create() means __aexit__ is NEVER run, so close() would not
            # fire and the owned aiohttp.ClientSession (+ any started MQTT client) would
            # leak. Tear down here (close() is idempotent); BaseException so a cancelled
            # setup also cleans up, and re-raise to preserve the original error. (#31,
            # symmetric to the #21 guard on NativeMqttClient.create().)
            await self.close()
            raise
        return self

    # Exception families raised while building/loading ONE appliance from cloud
    # data: the original (KeyError, ValueError, IndexError) plus TypeError and
    # AttributeError -- a non-dict element's .get(), a malformed info["attributes"]
    # comprehension in the constructor ({v["parName"]: v["parValue"] ...}), or
    # load_attributes() popping a non-dict "shadow"/"parameters". Deliberately NOT
    # Exception/BaseException: a genuine transport/auth error must still bubble to the
    # setup classifier, and asyncio.CancelledError (a BaseException) must propagate so
    # a cancelled setup is never mistaken for malformed data and swallowed.
    _APPLIANCE_BUILD_ERRORS = (KeyError, ValueError, IndexError, TypeError, AttributeError)

    def _log_malformed(self, error: BaseException, appliance_data: Any) -> None:
        # A malformed appliance must not break the others (CR#2): it is logged and
        # skipped (no usable object) or kept-partial (load failure). This ERROR is
        # NEVER gated by the debug toggles -> it lands in home-assistant.log (the file
        # users attach to issues), so it must be LEAK-PROOF BY CONSTRUCTION. Malformed
        # cloud data hides identity where key-name redaction cannot reach it -- a
        # serial as an attributes[].parValue (the real hOn shape), a MAC as a nested
        # key, or an identity as the value of a benign key (e.g. zone) -- and the
        # exception message itself echoes the offending raw value (int("AA:BB:..")
        # -> "invalid literal for int(): 'AA:BB:..'"). So we log ONLY non-identity
        # STRUCTURE: the exception TYPE name and the top-level field NAMES present
        # (cloud schema names, not values) -- never a value, the raw dict, or the
        # exception message/traceback. The full redacted dict (for the maintainer) is
        # available via Download Diagnostics, which redacts at a different layer.
        if isinstance(appliance_data, dict):
            _LOGGER.error(
                "[%s] Malformed appliance skipped (%s): fields=%s",
                APPLIANCE_DATA_MALFORMED.label,
                type(error).__name__,
                # str() the keys BEFORE sorting: this logger runs inside the except
                # handlers, so it must NEVER raise (a raise would escape and abort the
                # whole setup loop -- the exact failure CR#2 fixes). sorted() on
                # mixed-type keys raises TypeError; cloud JSON keys are always str so
                # this is belt-and-suspenders, but the boundary must hold by construction.
                sorted(map(str, appliance_data.keys())),
            )
        else:
            _LOGGER.error(
                "[%s] Malformed appliance skipped (%s): non-dict element type=%s",
                APPLIANCE_DATA_MALFORMED.label,
                type(error).__name__,
                type(appliance_data).__name__,
            )

    async def _create_appliance(self, appliance_data: dict, zone: int = 0) -> None:
        # Per-appliance fault boundary (CR#2 -- distinct from the steady-state
        # coordinator/polling resilience): a single malformed device must be
        # logged-and-skipped so the OTHER appliances still load and setup completes.
        #
        # CONSTRUCTION failures leave no usable object -> SKIP (do not append).
        # factory.create_appliance runs HonAppliance.__init__, which flattens
        # info["attributes"] and raises TypeError/KeyError on a bad shape; this ran
        # BEFORE the per-device try in the old code, so it aborted setup of ALL
        # appliances. mac_address is read here too (a property over the parsed info).
        try:
            appliance = factory.create_appliance(self._api, appliance_data, zone=zone)
            mac_empty = appliance.mac_address == ""
        except self._APPLIANCE_BUILD_ERRORS as error:
            self._log_malformed(error, appliance_data)
            return
        if mac_empty:
            return
        if self._minimal:
            # Validation only: the appliance is built (so .appliances is populated and
            # the config flow can count + type it) but its per-appliance loads are
            # skipped (issue #30); the full hydrate happens at runtime.
            self._appliances.append(appliance)
            return
        try:
            with phase("load_appliance", self._phase_tracker):
                async with budgeted(APPLIANCE_ONE):
                    await appliance.load_commands()
                    await appliance.load_attributes()
                # load_statistics() is NOT called here on purpose (issue #76): it is 2
                # more sequential round-trips per appliance, and the first coordinator
                # refresh -- which runs BEFORE any platform is forwarded -- redoes them
                # unconditionally (hon_client._update_appliance_sync). Loading them at
                # setup only spends them twice, inside the budget we are trying to fit.
        except self._APPLIANCE_BUILD_ERRORS as error:
            # LOAD failure: the appliance object EXISTS but its data is partial. Keep
            # it appended (partial state -- the shipped behavior) and log. Broadened
            # from (KeyError, ValueError, IndexError) to also catch AttributeError
            # (load_attributes pops a non-dict "shadow" then .get on it) and TypeError,
            # which previously escaped this catch and aborted the whole loop.
            self._log_malformed(error, appliance_data)
            # Counted as partial too, though NOT as retryable: an appliance appended by
            # THIS branch is just as unusable as one appended by the transport branch
            # below, and the all-failed guard has to see both or it under-counts a
            # mixed inventory (one malformed + one timed out = "1 failure out of 2"
            # and an entry with zero working devices shipped in silence).
            self._record_partial(
                appliance, zone, APPLIANCE_DATA_MALFORMED, error, retryable=False
            )
        except Exception as error:  # noqa: BLE001 - re-raised below when fatal
            # TRANSPORT fault boundary (issue #76, cause 4). aiohttp.ClientError and
            # TimeoutError are not in _APPLIANCE_BUILD_ERRORS (TimeoutError derives from
            # OSError, not from any of those five), so a single slow appliance used to
            # escape here, unwind setup() and tear the whole config entry down.
            #
            # An AUTH rejection still MUST be fatal: it has to reach _raise_setup_error
            # and open the reauth flow instead of quietly shipping a broken entry.
            # asyncio.CancelledError is a BaseException and is outside this catch by
            # construction, so a cancelled setup still propagates.
            code = classify(error)
            if code.requires_reauth:
                raise
            self._record_partial(appliance, zone, code, error, retryable=True)
            # Leak-proof: the code label and a count only. The redacted mac goes to
            # DEBUG, never to home-assistant.log at WARNING (same rule as
            # _log_malformed).
            _LOGGER.warning(
                "[%s] Appliance kept with partial data after a transport failure "
                "(%d so far this setup); the first coordinator refresh re-runs "
                "load_commands before any entity is created",
                code.label,
                len(self._hydration_failures),
            )
            _LOGGER.debug(
                "addhOn: partial hydration for %s (%s)",
                redact_mac(appliance.mac_address),
                type(error).__name__,
            )
        self._appliances.append(appliance)

    def _record_partial(
        self,
        appliance: Any,
        zone: int,
        code: Any,
        error: Exception,
        *,
        retryable: bool,
    ) -> None:
        """Remember an appliance that was appended without complete data.

        Containing a TRANSPORT failure here is only half a fault boundary:
        `load_commands` is what CREATES the command entities, and the integration has
        no dynamic discovery, so an appliance left with empty `commands` would stay
        without select/number/switch/button/climate/fan entities until a MANUAL
        reload. The other half is `needs_rehydration` below, which makes
        `hon_client._update_appliance_sync` re-run `load_commands` BEFORE the first
        snapshot the platforms are built from, and lets a second failure abort the
        first refresh -- so Home Assistant retries the setup instead of shipping a
        crippled entry.

        `retryable=False` (a malformed payload) is recorded but NOT queued for that:
        re-requesting a payload the parser cannot read produces the same payload, and
        failing the setup forever would leave the user with LESS than the degraded
        entry that ships today.
        """
        self._hydration_failures[f"{appliance.mac_address}#{zone}"] = code
        self._hydration_causes.append((redact_mac(appliance.mac_address), error))
        if retryable:
            self._retryable_partials.append(appliance)

    def needs_rehydration(self, appliance: Any) -> bool:
        """True if this appliance was appended without its commands by a TRANSPORT fault.

        By identity, not by mac: the caller holds the very object this session built.
        """
        return any(pending is appliance for pending in self._retryable_partials)

    async def setup(self) -> None:
        # Drop any partial inventory from an earlier setup() that a mid-setup MFA
        # challenge interrupted: submit_mfa_code() resumes by calling setup() again, and
        # without this the appliances built before the challenge would be appended a
        # second time -- duplicate appliance objects, each updated every poll (the
        # coordinator dedupes by id, so no double entities, just wasted work). Clear
        # IN PLACE, never rebind: the MQTT client binds this list by reference.
        self._appliances.clear()
        self._hydration_failures.clear()
        self._hydration_causes.clear()
        self._retryable_partials.clear()
        self._setup_phase = "load_appliances"
        # The hierarchical scope is what makes a LAZY sign-in nested in this request name
        # itself ("load_appliances/auth/...") instead of borrowing this label -- the
        # misattribution reported in #76. The flat mirror above is kept untouched.
        # APPLIANCE_LIST budgets THIS POST only: a sign-in triggered from inside it
        # suspends this scope for as long as it runs (client/budget.py), so the number
        # here stays the size of the work it names.
        with phase("load_appliances", self._phase_tracker):
            async with budgeted(APPLIANCE_LIST):
                appliances = await self.api.load_appliances()
        self._setup_phase = "load_appliance"
        for appliance in appliances:
            # Guard a non-dict element BEFORE appliance.get(...)/appliance.copy() can
            # raise AttributeError: parse_appliance_list returns the cloud list
            # verbatim with no per-element dict guarantee, so a schema-drift entry must
            # be logged-and-skipped, not abort the whole loop (CR#2).
            if not isinstance(appliance, dict):
                self._log_malformed(
                    TypeError(
                        f"appliance entry is {type(appliance).__name__}, expected dict"
                    ),
                    appliance,
                )
                continue
            # Zone parse is INSIDE the per-appliance boundary: a non-numeric "zone"
            # raises ValueError (or TypeError on a non-str/int), which must skip only
            # this device, not the rest.
            try:
                zones = int(appliance.get("zone", "0"))
            except (TypeError, ValueError) as error:
                self._log_malformed(error, appliance)
                continue
            if zones > 1:
                for zone in range(zones):
                    await self._create_appliance(appliance.copy(), zone=zone + 1)
            await self._create_appliance(appliance)
        # Anti-illusion guard, symmetric with the all-failed rule the poll already
        # applies: containing ONE broken appliance is a degradation, containing ALL of
        # them is a masked failure. An entry that starts with nothing usable must fail
        # loudly so Home Assistant retries, not ship an empty integration.
        #
        # Counting: EVERY partial appliance counts as unusable, whatever left it that
        # way -- otherwise a mixed inventory (one malformed + one timed out) reads as
        # "1 of 2 failed" and ships. Triggering: at least one of them must be
        # RETRYABLE, because that is the premise of raising -- Home Assistant retries.
        # An inventory that is only malformed will parse exactly the same way next
        # time, so failing forever would give the user less than the degraded entry
        # that ships today.
        if (
            self._appliances
            and self._retryable_partials
            and len(self._hydration_failures) >= len(self._appliances)
        ):
            # Re-raise the REAL cause, chained: with a single appliance (the common
            # case) a bare APPLIANCE_LOAD_FAILED told the user "could not load
            # appliance data" and threw away the ADDHON-400/430/450 that says why.
            # Same helper the poll path uses, so the two cannot drift.
            code, cause = representative_failure(self._hydration_causes)
            raise HonCodedError(
                code,
                f"No usable appliance after loading all {len(self._appliances)}",
                phase="load_appliance",
            ) from cause
        if self._enable_mqtt and not self._mqtt_client:
            # NativeMqttClient owns MQTT recovery. On a retryable AWS token/transport
            # outage create() returns a retained, temporarily-disconnected client whose
            # watchdog retries in the background; unexpected programming/configuration
            # errors still propagate instead of being silently converted to polling-only.
            #
            # The scope is the invariant, not a decoration: MQTT_START is SUMMED into
            # SETUP_CAP (client/budget.py) yet this was the one phase inside that cap
            # with no budget of its own, so a first connect that stalled was bounded
            # only by SETUP_CAP -- the config entry took ~10 minutes to fail on a phase
            # whose own number says 50s, and "every phase limits itself, the cap only
            # catches a loop that stopped progressing" was false exactly here. The MQTT
            # layer keeps REFINING the mirror inside this scope (mqtt_connect,
            # mqtt_subscribe), so the attribution stays as precise as it was.
            with phase("mqtt_start", self._phase_tracker):
                async with budgeted(MQTT_START):
                    self._mqtt_client = await self._make_mqtt()
        # Setup done: clear the phase so a later (non-setup) loop timeout is not
        # mis-attributed to a setup step.
        self._setup_phase = ""

    async def _make_mqtt(self) -> Any:
        # Lazy import: transport.mqtt imports awscrt/awsiot (absent dry/CI).
        from .transport.mqtt import NativeMqttClient

        return await NativeMqttClient(self, self._mobile_id).create()

    @property
    def refresh_token(self) -> str:
        """Current OAuth refresh token (for persistence), or '' if not yet logged in."""
        conn = self._connection
        if conn is None:
            return ""
        try:
            return conn.auth.refresh_token
        except Exception:  # noqa: BLE001 - no auth yet
            return ""

    @property
    def current_phase(self) -> str:
        """Hierarchical phase of the operation in flight, e.g. 'load_appliances/auth'.

        This is the CROSS-THREAD channel: `HonClient._run_on_hon_loop` waits on another
        thread and cannot read the ContextVar that carries the phase inside the loop.
        """
        return self._phase_tracker.current

    @property
    def phase_ledger(self) -> list[dict]:
        """Per-phase duration+outcome for the last operations (leak-proof primitives)."""
        return self._phase_tracker.entries()

    @property
    def phase_summary(self) -> str:
        """One-line ledger for the logs, e.g. 'auth 2.1s ok, load_appliances 58.4s timeout'."""
        return self._phase_tracker.summary()

    @property
    def degraded_appliances(self) -> dict[str, Any]:
        """'mac#zone' -> code for appliances kept with partial data (diagnostics only)."""
        return dict(self._hydration_failures)

    @property
    def auth_phase(self) -> str:
        """Last login phase the auth layer reached (for diagnostics attribution)."""
        conn = self._connection
        if conn is None:
            return ""
        try:
            return getattr(conn.auth, "_current_phase", "") or ""
        except Exception:  # noqa: BLE001 - no auth yet
            return ""

    async def submit_mfa_code(self, context: Any, code: str) -> "NativeHon":
        """Resume a paused 2FA login: verify the OTP, then finish setup (load the
        appliances + start MQTT at runtime) on the same session."""
        if self._connection is None:
            raise NativeAuthError("no pending MFA challenge")
        await self._connection.submit_mfa_code(context, code)
        await self.setup()
        return self

    async def resend_mfa_code(self, context: Any) -> None:
        """(Re)send the email OTP for a pending challenge."""
        if self._connection is None:
            raise NativeAuthError("no pending MFA challenge")
        await self._connection.resend_mfa_code(context)

    def subscribe_updates(self, notify_function: Any) -> None:
        self._notify_function = notify_function

    def notify(self) -> None:
        if self._notify_function:
            self._notify_function(None)

    async def close(self) -> None:
        # Stop the MQTT BEFORE the connection (the watchdog must not retry on
        # a session being closed); we close it to avoid leaking it.
        #
        # Best-effort + idempotent: close() runs on normal teardown AND from the
        # create() failure path, so (a) a cleanup error must NEVER mask the original
        # setup exception being re-raised (it would flip the config-entry
        # classification, e.g. hide a reauth-needed error), and (b) a second close()
        # (setup_sync also calls it after a failed create()) must be a no-op. Each step
        # is guarded and the reference is cleared before awaiting.
        if self._mqtt_client is not None:
            mqtt, self._mqtt_client = self._mqtt_client, None
            try:
                await mqtt.stop()
            except Exception:  # noqa: BLE001 - cleanup must not mask the real error
                _LOGGER.debug("addhOn: MQTT stop during close failed", exc_info=True)
        if self._api is not None:
            api, self._api = self._api, None
            try:
                await api.close()
            except Exception:  # noqa: BLE001 - cleanup must not mask the real error
                _LOGGER.debug("addhOn: api close during close failed", exc_info=True)
