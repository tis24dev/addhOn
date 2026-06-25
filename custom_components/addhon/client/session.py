"""addhOn `NativeHon` session orchestration.

Coordinates the setup on top of the native transport (`transport.connection.HonConnection` +
`transport.api.HonApi`) and builds the native appliances (`engine.appliance.HonAppliance`)
via `factory.create_appliance`, into which it injects our `api`.

Boundary: appliance construction goes through `factory.create_appliance`; the MQTT is
NATIVE (`transport.mqtt.NativeMqttClient`, lazy import in `_make_mqtt`).
`NativeHon` satisfies the Protocol `interfaces.HonSession` and exposes `.api`/`.appliances`/
`subscribe_updates`/`notify` (the MQTT client reads exactly those members).

Setup sequence: create connection -> `api.load_appliances()` -> for each appliance
build the HonAppliance and load commands/attributes/statistics -> start MQTT. The order
matters: the load_* calls make the first HTTP requests that populate the tokens, so that
when MQTT starts `api.auth.id_token` is present.
"""
from __future__ import annotations

import logging
from types import TracebackType
from typing import Any

import aiohttp

from . import factory
from .transport.api import HonApi
from .transport.auth import MFAChallengeRequired, NativeAuthError
from .transport.connection import HonConnection
from ..error_codes import APPLIANCE_DATA_MALFORMED

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
        self._connection: HonConnection | None = None
        self._api: HonApi | None = None
        self._appliances: list[Any] = []
        self._mqtt_client: Any = None
        self._notify_function: Any = None
        # Coarse setup phase, read by HonClient when the dedicated-loop 60s cap fires
        # to attribute the (otherwise message-less) timeout to a stable error code.
        self._setup_phase: str = ""

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
        return self._appliances

    @appliances.setter
    def appliances(self, appliances: list[Any]) -> None:
        # NB: the MQTT client binds the list by reference at __init__. Do not rebind
        # after MQTT is started, or the subscriptions would not see the new list.
        self._appliances = appliances

    async def create(self) -> "NativeHon":
        try:
            self._connection = await HonConnection(
                self._email,
                self._password,
                session=self._session,
                mobile_id=self._mobile_id,
                refresh_token=self._refresh_token,
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
            await appliance.load_commands()
            await appliance.load_attributes()
            await appliance.load_statistics()
        except self._APPLIANCE_BUILD_ERRORS as error:
            # LOAD failure: the appliance object EXISTS but its data is partial. Keep
            # it appended (partial state -- the shipped behavior) and log. Broadened
            # from (KeyError, ValueError, IndexError) to also catch AttributeError
            # (load_attributes pops a non-dict "shadow" then .get on it) and TypeError,
            # which previously escaped this catch and aborted the whole loop.
            self._log_malformed(error, appliance_data)
        self._appliances.append(appliance)

    async def setup(self) -> None:
        self._setup_phase = "load_appliances"
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
        if self._enable_mqtt and not self._mqtt_client:
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
