"""addhOn MQTT client (AWS IoT realtime push).

Realtime push client built on awscrt directly.

Receives the session (`NativeHon`) and reads its `api` (tokens: `load_aws_token` +
`auth.id_token`), `appliances`, `notify` (all duck-typed): the `appliance` objects are the
parser engine, touched only via its public interface.

Lifecycle and message-handling notes:
- `stop()` cancels and awaits the watchdog BEFORE stopping the client, so a `_start()`
  in flight does not recreate an orphan connection (which would leak one AWS IoT
  connection per reload);
- `_on_publish_received` is defensive: appliance not found for the topic / missing
  parameters -> skip instead of crash. A `parName` never seen before over MQTT is
  SKIPPED (it is recovered at the next HTTP poll); only parameters already present in
  `attributes["parameters"]` (seeded by the `load_attributes` HTTP poll) are updated.
  Creating the entry on the fly is an option not yet necessary.

awscrt/awsiot are imported at the top: the module is NOT importable dry; whoever uses
it (`NativeHon`) imports it lazily. The lifecycle INFO noise is governed by
`logging_utils` on this logger.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import secrets
from typing import Any

from awscrt import mqtt5
from awsiot import mqtt5_client_builder  # type: ignore[import-untyped]

from .device import MOBILE_ID
from ...debug_utils import redact_id, redact_identity, redact_topic
from ...error_codes import HonCodedError, MQTT_SUBSCRIBE_TIMEOUT

_LOGGER = logging.getLogger(__name__)

# AWS IoT endpoint/authorizer of the hOn cloud.
AWS_ENDPOINT = "a30f6tqw0oh1x0-ats.iot.eu-west-1.amazonaws.com"
AWS_AUTHORIZER = "candy-iot-authorizer"

_WATCHDOG_INTERVAL = 5  # seconds
_SUBSCRIBE_TIMEOUT = 10  # seconds
# Consecutive down ticks before the watchdog forces a full client rebuild. awscrt's
# mqtt5 client auto-reconnects on its own (with backoff); rebuilding every tick would
# tear that down and re-hit the AWS authorizer-token endpoint each cycle. Waiting for
# sustained downtime lets the native reconnect recover first.
_RECONNECT_AFTER_FAILED_TICKS = 3
# Additive backoff cap (seconds) applied to the watchdog cadence after consecutive
# rebuild failures, so a persistent 5xx from the AWS authorizer is not hammered
# every tick. Reset to 0 on recovery; never masks (each failure still logs WARNING).
_RECONNECT_BACKOFF_CAP = 60
# Consecutive in-place re-subscribe FAILURES (transport reports connected, but the
# SUBACK keeps stalling) before the watchdog stops retrying the subscribe and escalates
# to a full _start() rebuild. A half-open socket awscrt still believes is connected, or
# a stale/revoked AWS custom-authorizer session that never fires a disconnection
# callback: WITHOUT this cap the connected-but-unsubscribed branch never yields to the
# rebuild path, so the in-place re-subscribe loops forever and NEVER refreshes the AWS
# token / tears down the dead client (only _start() does that). Hitting the cap routes
# to a full rebuild PROMPTLY -- skipping the _RECONNECT_AFTER_FAILED_TICKS grace wait,
# since this connection is already known dead. Reset to 0 on ANY successful subscribe.
_MAX_RESUBSCRIBE_FAILURES = 3


def _subscribed_topics(appliance) -> list:
    """Subscribe topics of an appliance, tolerating null/non-dict info shapes.

    `info.get("topics", {})` returns {} only on a MISSING key, NOT on an explicit
    null value (the cloud routinely sends nested nulls): `None.get(...)` would then
    raise and, in the topic-match generator, drop the message for EVERY appliance.
    """
    info = getattr(appliance, "info", None)
    topics = info.get("topics") if isinstance(info, dict) else None
    sub = topics.get("subscribe") if isinstance(topics, dict) else None
    return sub if isinstance(sub, list) else []


class NativeMqttClient:
    """Realtime push via AWS IoT MQTT5 on top of the native session."""

    def __init__(self, hon: Any, mobile_id: str) -> None:
        self._hon = hon
        self._mobile_id = mobile_id or MOBILE_ID
        self._api = hon.api
        self._appliances = hon.appliances
        self._client: mqtt5.Client | None = None
        self._connection = False
        # Distinct from _connection: tracks whether _subscribe_appliances() last
        # completed without raising. _connection ("transport connected", set by the
        # awscrt connection-success callback) and "appliance topics actually
        # subscribed" are TWO states; conflating them made the watchdog treat a
        # connected-but-unsubscribed client as healthy and stop rebuilding, so realtime
        # pushes were silently dead until the next full AWS IoT disconnect (the 60s HTTP
        # poll masks it). Two failure modes leave us connected-but-unsubscribed:
        #   1. a rebuild whose _subscribe_appliances() hits _SUBSCRIBE_TIMEOUT (success
        #      callback already flipped _connection=True before the SUBACK stalled);
        #   2. awscrt's own auto-reconnect after a transient blip -- AWS IoT custom-
        #      authorizer websocket sessions are clean (no session_behavior is set in
        #      _start(), so subscriptions are NOT restored across a reconnect).
        # Both are recovered by the watchdog: see _watchdog().
        self._subscribed = False
        # Bumped on every _start(): the state-mutating lifecycle callbacks are bound
        # to the generation of the client that registered them, so a late event from a
        # client we already stopped cannot flip self._connection on the new one (awscrt
        # stop() is asynchronous). See _is_stale_generation.
        self._generation = 0
        self._watchdog_task: asyncio.Task[None] | None = None

    @property
    def client(self) -> mqtt5.Client:
        if self._client is None:
            raise AttributeError("MQTT client not started")
        return self._client

    def _set_setup_phase(self, phase: str) -> None:
        """Record the setup phase on the parent session so a dedicated-loop 60s
        timeout during the FIRST connect is attributed to the right MQTT step. Only
        set from create() (not the watchdog reconnect, which runs after setup)."""
        hon = self._hon
        if hon is not None:
            try:
                hon._setup_phase = phase
            except Exception:  # pragma: no cover - defensive
                pass

    async def create(self) -> "NativeMqttClient":
        try:
            self._set_setup_phase("mqtt_connect")
            await self._start()
            gen = self._generation
            self._set_setup_phase("mqtt_subscribe")
            await self._subscribe_appliances()
            # Subscriptions are now in place: mark healthy so the watchdog does not
            # treat the initial connect as connected-but-unsubscribed (see _watchdog).
            # Re-check connection AND generation AFTER the await, not unconditionally:
            # a disconnection callback (on the awscrt thread) can fire mid-subscribe and
            # set _connection=_subscribed=False; an unconditional `= True` here would
            # clobber that back, leaving a stuck "healthy on a dropped session" state
            # (the exact bug class this flag exists to kill). gen == _generation also
            # guards against a rebuild landing during the await.
            self._subscribed = self._connection and (gen == self._generation)
            await self._start_watchdog()
        except BaseException:
            # _start() has already started the awscrt client; if a later step fails
            # create() raises BEFORE NativeHon stores us (session._make_mqtt), so
            # NativeHon.close() can never reach the client -> the socket + native
            # worker threads leak. Tear it down here (stop() is idempotent and
            # exception-guarded). BaseException so a cancelled setup also cleans up;
            # we re-raise to preserve the original error/cancellation.
            await self.stop()
            raise
        return self

    async def stop(self) -> None:
        """Stop watchdog (cancel+await) and then the awscrt client. Idempotent."""
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.debug("addhOn: awaiting MQTT watchdog cancel failed: %s", err)
            self._watchdog_task = None
        if self._client is not None:
            try:
                self._client.stop()
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.debug("addhOn: MQTT client stop failed: %s", err)
            self._client = None

    # -- lifecycle callbacks ---------------------------------------------------
    # The callbacks that write self._connection are registered (in _start) bound to the
    # generation of their client. awscrt stop() is asynchronous, so the client we tear
    # down during a rebuild can still emit a late disconnection/failure AFTER the new
    # client reported success; without this guard that stale event would flip
    # self._connection back to False on a healthy connection and make the watchdog count
    # a false outage (and possibly force a superfluous rebuild). An event from a
    # non-current generation is ignored.
    def _is_stale_generation(self, generation: int) -> bool:
        if generation != self._generation:
            _LOGGER.debug(
                "MQTT: ignoring stale lifecycle event (gen %s != current %s)",
                generation,
                self._generation,
            )
            return True
        return False

    def _on_lifecycle_stopped(self, data: "mqtt5.LifecycleStoppedData") -> None:
        _LOGGER.info("Lifecycle Stopped: %s", data)

    def _on_lifecycle_connection_success(
        self, data: "mqtt5.LifecycleConnectSuccessData", generation: int
    ) -> None:
        if self._is_stale_generation(generation):
            return
        self._connection = True
        _LOGGER.info("Lifecycle Connection Success: %s", data)

    def _on_lifecycle_attempting_connect(
        self, data: "mqtt5.LifecycleAttemptingConnectData"
    ) -> None:
        _LOGGER.info("Lifecycle Attempting Connect: %s", data)

    def _on_lifecycle_connection_failure(
        self, data: "mqtt5.LifecycleConnectFailureData", generation: int
    ) -> None:
        if self._is_stale_generation(generation):
            return
        self._connection = False
        # A failed/dropped connection loses all subscriptions (clean session): clear
        # the flag so the watchdog re-subscribes once awscrt reconnects. The generation
        # guard above already prevents a late event from an old client clearing it.
        self._subscribed = False
        _LOGGER.info("Lifecycle Connection Failure: %s", data)

    def _on_lifecycle_disconnection(
        self, data: "mqtt5.LifecycleDisconnectData", generation: int
    ) -> None:
        if self._is_stale_generation(generation):
            return
        self._connection = False
        # See _on_lifecycle_connection_failure: a disconnect drops subscriptions, so
        # the watchdog must re-establish them after the auto-reconnect.
        self._subscribed = False
        _LOGGER.info("Lifecycle Disconnection: %s", data)

    def _on_publish_received(self, data: "mqtt5.PublishReceivedData") -> None:
        if not (data and data.publish_packet and data.publish_packet.payload):
            return
        topic = data.publish_packet.topic
        # Defensive (this runs on an awscrt callback thread): a malformed payload
        # must be skipped, not raised, or it would crash the callback and silence
        # every later push instead of just dropping this one. Two cases:
        #   1. not decodable / not JSON -> json.loads raises;
        #   2. valid JSON but not an object (e.g. a bare list/scalar) -> the later
        #      payload.get(...) calls would raise AttributeError.
        try:
            payload = json.loads(data.publish_packet.payload.decode())
        except (ValueError, UnicodeDecodeError) as err:
            _LOGGER.debug("MQTT: undecodable payload on %s: %s", redact_topic(topic), err)
            return
        if not isinstance(payload, dict):
            # Log only the TYPE, not the value: redact_identity masks dict keys but a
            # bare scalar (e.g. a JSON string that happens to be a MAC) would pass
            # through, so never echo a non-object payload's content.
            _LOGGER.debug(
                "MQTT: non-object payload on %s: type=%s",
                redact_topic(topic),
                type(payload).__name__,
            )
            return
        # The rest also runs on the awscrt callback thread: a VALID dict payload can
        # still make the engine raise (params[name].update, sync_params_to_command, or
        # self._hon.notify -> an arbitrary HA callback). An unhandled exception here
        # would crash the callback and silence every later push, so the whole body is
        # wrapped: log at WARNING (the MQTT logger defaults to WARNING, so debug/info
        # would be muted and would mask real engine bugs) and drop just this message.
        # State is reconciled at the next HTTP poll, so skipping is safe and idempotent.
        try:
            # Defensive: appliance not found for this topic -> exit.
            appliance = next(
                (a for a in self._appliances if topic in _subscribed_topics(a)),
                None,
            )
            if appliance is None:
                _LOGGER.debug(
                    "MQTT: topic with no matching appliance: %s", redact_topic(topic)
                )
                return
            if topic and "appliancestatus" in topic:
                params = appliance.attributes.get("parameters", {})
                raw_params = payload.get("parameters")
                if not isinstance(raw_params, list):
                    # The cloud may send parameters as null or a non-list; treat as
                    # empty (dirty data, DEBUG not WARNING) instead of letting it drop
                    # the whole message at the broad except below.
                    if raw_params is not None:
                        _LOGGER.debug(
                            "MQTT: appliancestatus parameters not a list (%s), skipping",
                            type(raw_params).__name__,
                        )
                    raw_params = []
                for parameter in raw_params:
                    # Skip a single malformed element (e.g. a null in the list) so the
                    # valid parameters in the same batch are still applied (and notify
                    # still fires), instead of dropping the entire message.
                    if not isinstance(parameter, dict):
                        # Log only the TYPE, never the value (CR#4): a malformed element
                        # is a raw cloud-controlled scalar; a bare MAC/serial would
                        # bypass redaction (redact_identity is key-based, a pass-through
                        # for a scalar). Mirrors the non-object-payload / not-a-list
                        # branches above. The value is not diagnostically useful anyway.
                        _LOGGER.debug(
                            "MQTT: skipping non-dict parameter element: type=%s",
                            type(parameter).__name__,
                        )
                        continue
                    name = parameter.get("parName")
                    # Only already-known parameters (seeded by load_attributes). A new
                    # parName is recovered at the next HTTP poll; creating it here would
                    # couple this transport module to the engine's HonAttribute.
                    if name in params:
                        params[name].update(parameter)
                appliance.sync_params_to_command("settings")
            elif topic and "disconnected" in topic:
                _LOGGER.info(
                    "Disconnected %s: %s",
                    redact_id(appliance.nick_name),
                    payload.get("disconnectReason"),
                )
                appliance.connection = False
            elif topic and "connected" in topic:
                appliance.connection = True
                _LOGGER.info("Connected %s", redact_id(appliance.nick_name))
            elif topic and "discovery" in topic:
                _LOGGER.info("Discovered %s", redact_id(appliance.nick_name))
            self._hon.notify()
            _LOGGER.info("%s - %s", redact_topic(topic), redact_identity(payload))
        except Exception:
            # Broad on purpose: the body spans transport -> engine -> HA coordinator and
            # may raise arbitrary types; we must never let one reach the awscrt thread.
            _LOGGER.warning("MQTT: handler failed on %s", redact_topic(topic), exc_info=True)
            return

    # -- connection / subscribe / watchdog -------------------------------------
    async def _start(self) -> None:
        # The watchdog calls _start() to reconnect: overwriting self._client
        # without stopping the previous one leaks its native AWS IoT connection
        # (socket + worker threads are NOT released by GC, only by .stop()), so a
        # sustained disconnection would spawn a new orphan client every cycle.
        if self._client is not None:
            try:
                self._client.stop()
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.debug("addhOn: stopping previous MQTT client failed: %s", err)
            self._client = None
        # A fresh client carries over no subscriptions: clear the flag here (the caller
        # -- create() or the watchdog rebuild path -- sets it True only after
        # _subscribe_appliances() succeeds).
        self._subscribed = False
        # Reset _connection too so it tracks the CURRENT client: the new one is not up
        # until ITS generation-tagged success callback fires below. A rebuild reached via
        # the escalation branch (connected-but-unsubscribed) would otherwise carry over
        # _connection=True from the just-stopped client. That is NOT a false-healthy
        # (_subscribed above is already False, so the watchdog never treats it as
        # healthy), but a rebuild whose _subscribe_appliances() then fails would leave a
        # stale connected-but-unsubscribed state on a client that is not actually up yet,
        # sending the next tick down the in-place re-subscribe path against a dead client
        # instead of rebuilding. The only late callback the stopped client can still emit
        # is a disconnection (-> False), never a spurious success, and the generation bump
        # below rejects every other stale event; so this reset cannot be clobbered True.
        self._connection = False
        # Tag this client's state-mutating callbacks with a fresh generation so a late
        # event from the client just stopped cannot flip self._connection (see
        # _is_stale_generation).
        self._generation += 1
        generation = self._generation
        self._client = mqtt5_client_builder.websockets_with_custom_authorizer(
            endpoint=AWS_ENDPOINT,
            auth_authorizer_name=AWS_AUTHORIZER,
            auth_authorizer_signature=await self._api.load_aws_token(),
            auth_token_key_name="token",
            auth_token_value=self._api.auth.id_token,
            client_id=f"{self._mobile_id}_{secrets.token_hex(8)}",
            on_lifecycle_stopped=self._on_lifecycle_stopped,
            on_lifecycle_connection_success=functools.partial(
                self._on_lifecycle_connection_success, generation=generation
            ),
            on_lifecycle_attempting_connect=self._on_lifecycle_attempting_connect,
            on_lifecycle_connection_failure=functools.partial(
                self._on_lifecycle_connection_failure, generation=generation
            ),
            on_lifecycle_disconnection=functools.partial(
                self._on_lifecycle_disconnection, generation=generation
            ),
            on_publish_received=self._on_publish_received,
        )
        self.client.start()

    async def _subscribe_appliances(self) -> None:
        for appliance in self._appliances:
            await self._subscribe(appliance)

    async def _subscribe(self, appliance: Any) -> None:
        for topic in _subscribed_topics(appliance):
            # awscrt subscribe() returns a concurrent.futures.Future; await it via
            # wrap_future instead of a blocking .result(), so the hon_loop is not
            # frozen up to _SUBSCRIBE_TIMEOUT per topic. Order is preserved (await
            # each before the next); the timeout bound is unchanged.
            future = self.client.subscribe(
                mqtt5.SubscribePacket([mqtt5.Subscription(topic)])
            )
            try:
                await asyncio.wait_for(asyncio.wrap_future(future), _SUBSCRIBE_TIMEOUT)
            except asyncio.TimeoutError as err:
                # Attribute the stall to a stable code. No identity in the message
                # (the topic embeds the MAC) -> the bare timeout str only.
                raise HonCodedError(MQTT_SUBSCRIBE_TIMEOUT, str(err)) from err
            _LOGGER.info("Subscribed to topic %s", redact_topic(topic))

    async def _start_watchdog(self) -> None:
        if not self._watchdog_task or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog())

    async def _watchdog(self) -> None:
        failed_ticks = 0
        backoff = 0
        # Consecutive in-place re-subscribe FAILURES (Issue 1): a transport that awscrt
        # keeps reporting connected but whose SUBACK never lands (half-open socket /
        # stale custom-authorizer session that never fires a disconnection callback)
        # would otherwise loop the in-place re-subscribe forever and never refresh the
        # AWS token via _start(). After _MAX_RESUBSCRIBE_FAILURES we escalate to a full
        # rebuild. Reset to 0 on ANY successful subscribe (re-subscribe or rebuild).
        resubscribe_failures = 0
        while True:
            # The rebuild (load_aws_token / subscribe) can raise transiently; without
            # this guard one exception would end the task and kill realtime until a
            # reload. Re-raise CancelledError FIRST (stop() cancels+awaits us, so
            # swallowing it would deadlock shutdown); on any other error log and keep
            # looping with an additive backoff (capped, reset on recovery). A re-subscribe
            # failure (Issue 1) increments resubscribe_failures via attempted_resubscribe.
            attempted_resubscribe = False
            try:
                await asyncio.sleep(_WATCHDOG_INTERVAL + backoff)
                # Healthy ONLY when the transport is connected AND the appliance topics
                # are subscribed. Checking _connection alone treated a connected-but-
                # unsubscribed client as healthy and stopped rebuilding (the original
                # bug), so realtime pushes died silently until the next full disconnect.
                if self._connection and self._subscribed:
                    failed_ticks = 0
                    backoff = 0
                    resubscribe_failures = 0
                    continue
                # Connected but the subscription is missing (a rebuild whose subscribe
                # timed out, OR awscrt's own auto-reconnect on a clean session): the
                # transport is up, so a full rebuild would needlessly tear down a working
                # connection and re-hit the AWS authorizer endpoint. Re-subscribe in
                # place instead -- UNLESS the in-place re-subscribe has already failed
                # _MAX_RESUBSCRIBE_FAILURES times in a row (Issue 1): a connection awscrt
                # still believes is up but that never SUBACKs is dead at the application
                # layer, and only _start() can refresh the token + rebuild the client, so
                # we fall through to the rebuild path below instead of looping forever.
                if (
                    self._connection
                    and not self._subscribed
                    and resubscribe_failures < _MAX_RESUBSCRIBE_FAILURES
                ):
                    _LOGGER.info("Re-subscribe mqtt topics")
                    gen = self._generation
                    # Mark BEFORE the await so a raise (e.g. MQTT_SUBSCRIBE_TIMEOUT)
                    # landing in the shared `except` is counted as a re-subscribe failure
                    # (Issue 1 escalation). It is cleared again right after a successful
                    # subscribe so a successful tick never counts.
                    attempted_resubscribe = True
                    await self._subscribe_appliances()
                    attempted_resubscribe = False
                    # Re-check connection AND generation AFTER the await, not an
                    # unconditional True (Issue 3): a disconnection callback can land on
                    # the awscrt thread during the subscribe and set
                    # _connection=_subscribed=False; clobbering it back to True here would
                    # leave a stuck "healthy on a dropped session". gen == _generation
                    # also rejects a rebuild that landed mid-await.
                    self._subscribed = self._connection and (gen == self._generation)
                    if not self._subscribed:
                        # The connection dropped (or a new client generation landed)
                        # during the subscribe: do NOT treat this tick as a recovery.
                        # Leave the counters untouched and let the next tick's
                        # not-connected path count the outage normally.
                        continue
                    resubscribe_failures = 0  # subscribe succeeded
                    # Issue 2: a genuine recovery resets the outage counters, same as the
                    # healthy branch. Leaving failed_ticks set across a successful
                    # re-subscribe would force a full rebuild one tick early on a flapping
                    # connection, breaking the _RECONNECT_AFTER_FAILED_TICKS contract.
                    failed_ticks = 0
                    backoff = 0  # re-subscribe succeeded
                    continue
                # Not connected (or re-subscribe escalated to a rebuild): sustained
                # downtime only forces a rebuild. Give awscrt's own auto-reconnect a
                # chance first (see _RECONNECT_AFTER_FAILED_TICKS) -- but an escalation
                # (resubscribe_failures exceeded) skips the wait, the connection is
                # already known dead.
                escalated = (
                    self._connection
                    and not self._subscribed
                    and resubscribe_failures >= _MAX_RESUBSCRIBE_FAILURES
                )
                if not escalated:
                    failed_ticks += 1
                    if failed_ticks < _RECONNECT_AFTER_FAILED_TICKS:
                        continue
                failed_ticks = 0
                _LOGGER.info("Restart mqtt connection")
                await self._start()
                gen = self._generation
                await self._subscribe_appliances()
                # Rebuild + subscribe both succeeded: mark healthy. _start() reset
                # _subscribed to False, so leaving it unset on a subscribe failure (which
                # raises before this line) keeps the recovery loop honest. Re-check
                # connection AND generation AFTER the await (Issue 3), not an
                # unconditional True, so a disconnect or newer generation landing during
                # the subscribe is not clobbered.
                self._subscribed = self._connection and (gen == self._generation)
                resubscribe_failures = 0  # the rebuild re-subscribed
                backoff = 0  # rebuild succeeded
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempted_resubscribe:
                    # Issue 1: an in-place re-subscribe attempt raised. Count it so a
                    # connection awscrt keeps reporting up but that never SUBACKs is
                    # escalated to a full rebuild after _MAX_RESUBSCRIBE_FAILURES.
                    resubscribe_failures += 1
                backoff = min(backoff + _WATCHDOG_INTERVAL, _RECONNECT_BACKOFF_CAP)
                _LOGGER.warning(
                    "MQTT watchdog: reconnect failed, retrying in %ss",
                    _WATCHDOG_INTERVAL + backoff,
                    exc_info=True,
                )
