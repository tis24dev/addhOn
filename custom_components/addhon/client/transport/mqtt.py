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
        # Distinct from _connection: the SET of topic strings currently subscribed
        # (acked). _connection ("transport connected", set by the awscrt connection-
        # success callback) and "appliance topics actually subscribed" are TWO states;
        # conflating them made the watchdog treat a connected-but-unsubscribed client as
        # healthy and stop rebuilding, so realtime pushes were silently dead until the
        # next full AWS IoT disconnect (the 60s HTTP poll masks it). Two failure modes
        # leave us connected-but-unsubscribed:
        #   1. a rebuild whose subscribe hits _SUBSCRIBE_TIMEOUT (success callback
        #      already flipped _connection=True before the SUBACK stalled);
        #   2. awscrt's own auto-reconnect after a transient blip -- AWS IoT custom-
        #      authorizer websocket sessions are clean (no session_behavior is set in
        #      _start(), so subscriptions are NOT restored across a reconnect).
        # Both are recovered by the watchdog: see _watchdog().
        #
        # A SET (per topic) rather than a single binary flag gives PER-TOPIC isolation:
        # one appliance whose SUBACK stalls no longer blocks the rest (H1 starvation),
        # and the watchdog can distinguish "one bad appliance" (some topics subscribed,
        # transport alive -> keep retrying the missing, do NOT rebuild) from "dead
        # connection" (no topic subscribes at all -> escalate to a full rebuild).
        self._subscribed_topics_set: set[str] = set()
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
            await self._subscribe_missing()
            # Subscriptions are now in place: commit the set so the watchdog does not
            # treat the initial connect as connected-but-unsubscribed (see _watchdog).
            # Re-check connection AND generation AFTER the await, not unconditionally:
            # a disconnection callback (on the awscrt thread) can fire mid-subscribe and
            # clear the set (via the lifecycle callback); committing it unconditionally
            # here would resurrect a "healthy on a dropped session" state (the exact bug
            # class this set exists to kill). gen == _generation also guards against a
            # rebuild landing during the await.
            if not (self._connection and gen == self._generation):
                self._subscribed_topics_set = set()
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
        # the set so the watchdog re-subscribes once awscrt reconnects. Rebind (atomic)
        # rather than .clear() so a concurrent reader on the hon_loop never observes a
        # half-emptied set. The generation guard above already prevents a late event
        # from an old client clearing it.
        self._subscribed_topics_set = set()
        _LOGGER.info("Lifecycle Connection Failure: %s", data)

    def _on_lifecycle_disconnection(
        self, data: "mqtt5.LifecycleDisconnectData", generation: int
    ) -> None:
        if self._is_stale_generation(generation):
            return
        self._connection = False
        # See _on_lifecycle_connection_failure: a disconnect drops subscriptions, so
        # the watchdog must re-establish them after the auto-reconnect. Rebind (atomic).
        self._subscribed_topics_set = set()
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
            if topic and topic.startswith("$aws/events/presence/"):
                # AWS-IoT SESSION presence of OUR MQTT client (clientId-scoped; payload
                # carries clientId/sessionIdentifier/principalIdentifier). It is matched to
                # whichever single appliance subscribes the topic but says NOTHING about
                # that appliance's connectivity. It must NOT arm (connected) NOR clear
                # (disconnected) the appliance's stale-disconnect protection: our client
                # reconnects/drops on its own schedule, so arming would pin a genuinely
                # offline appliance online on every reconnect, and clearing would knock a
                # live, streaming appliance offline on a transient session blip (both
                # non-self-correcting while the cloud's lastConnEvent stays stale).
                # Appliance connectivity comes only from device-scoped events
                # (haier/things/<mac>/event/...) and the REST lastConnEvent.
                _LOGGER.debug(
                    "MQTT: client session presence on %s, ignored for connectivity",
                    redact_topic(topic),
                )
            elif topic and "appliancestatus" in topic:
                # Realtime traffic is authoritative connectivity evidence (the hOn app
                # trusts it): mark the appliance connected and remember WHEN, using the
                # cloud-stamped payload `timestamp` (skew-free vs lastConnEvent), BEFORE
                # parsing parameters so the liveness is recorded even if a later param is
                # dirty. mark_realtime_seen is positive-only and never raises.
                appliance.mark_realtime_seen(payload.get("timestamp"))
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
                # Device-scoped disconnect (haier/things/<mac>/event/disconnected): the
                # APPLIANCE itself reports offline -- authoritative. Session-presence
                # disconnects ($aws/events/presence/...) were already intercepted above, so
                # this never fires on OUR client's session drop. Route through
                # mark_realtime_disconnected so it CLEARS the realtime liveness marks too --
                # otherwise a stale REST lastConnEvent=DISCONNECTED (older than the last
                # traffic) would resurrect the appliance at the next poll. Falls back to the
                # plain setter for any appliance object that predates the method.
                mark = getattr(appliance, "mark_realtime_disconnected", None)
                if callable(mark):
                    mark()
                else:  # pragma: no cover - production appliances have the method
                    appliance.connection = False
            elif topic and "connected" in topic:
                # Device-scoped connect (haier/things/<mac>/event/connected): the appliance
                # reports online. Session-presence connects ($aws/events/presence/...) were
                # intercepted above, so this is not OUR client's session event. Keep the
                # transient behavior (connection True, self-corrected at the next REST poll);
                # we deliberately do NOT arm the realtime liveness here -- only true
                # mac-scoped `appliancestatus` traffic records liveness, so a bare connect
                # event cannot over-extend the stale-disconnect protection window.
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
        # A fresh client carries over no subscriptions: clear the set here (the caller
        # -- create() or the watchdog rebuild path -- re-populates it only after the
        # subscribe succeeds). Rebind (atomic) rather than .clear().
        self._subscribed_topics_set = set()
        # Reset _connection too so it tracks the CURRENT client: the new one is not up
        # until ITS generation-tagged success callback fires below. A rebuild reached via
        # the escalation branch (connected-but-unsubscribed) would otherwise carry over
        # _connection=True from the just-stopped client. That is NOT a false-healthy
        # (the set above is already empty, so the watchdog never treats it as
        # healthy), but a rebuild whose subscribe then fails would leave a
        # stale connected-but-unsubscribed state on a client that is not actually up yet,
        # sending the next tick down the in-place re-subscribe path against a dead client
        # instead of rebuilding. The only late callback the stopped client can still emit
        # is a disconnection (-> False), never a spurious success, and the generation bump
        # below rejects every other stale event; so this reset cannot be flipped True.
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

    def _all_topics(self) -> set[str]:
        """Union of the subscribe topics of every appliance (the target set)."""
        topics: set[str] = set()
        for appliance in self._appliances:
            topics.update(_subscribed_topics(appliance))
        return topics

    async def _subscribe_topic(self, topic: str) -> None:
        # awscrt subscribe() returns a concurrent.futures.Future; await it via
        # wrap_future instead of a blocking .result(), so the hon_loop is not
        # frozen up to _SUBSCRIBE_TIMEOUT. The timeout bound is unchanged.
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

    async def _subscribe_missing(self) -> None:
        # Per-topic isolation (H1): subscribe ONLY the topics not already in the set,
        # and a single topic's failure does NOT abort the others. The earlier
        # sequential "raise on the first timeout" loop starved every appliance AFTER a
        # slow one (it never got subscribed), so a single bad appliance early in the
        # list permanently killed realtime push for the rest. Here a failed topic is
        # logged and skipped; it is simply retried on the next watchdog tick, while the
        # healthy topics stay subscribed.
        for topic in self._all_topics():
            if topic in self._subscribed_topics_set:
                continue
            try:
                await self._subscribe_topic(topic)
            except asyncio.CancelledError:
                # stop() cancels+awaits the watchdog: a cancellation must propagate,
                # never be swallowed as a per-topic failure (it is not one).
                raise
            except Exception as err:
                # Isolate EVERY non-cancellation failure, not just the HonCodedError
                # timeout: client.subscribe()/the awscrt future can also raise transport
                # or awscrt errors, and letting those escape would abort the loop and
                # re-open the exact H1 starvation (every later topic skipped this pass).
                # A failed topic stays missing and is retried next tick; if NOTHING
                # subscribes the watchdog's blackout escalation still fires.
                _LOGGER.warning(
                    "MQTT: subscribe failed for one topic, continuing: %s", err
                )
                continue
            self._subscribed_topics_set.add(topic)

    # Thin compatibility wrappers over the per-topic primitives above. _subscribe_missing
    # is the path used by create()/the watchdog; these keep the per-appliance call shape
    # for callers/tests that still drive a single appliance (note: unlike
    # _subscribe_missing they propagate a HonCodedError so the per-topic timeout
    # contract stays testable).
    async def _subscribe(self, appliance: Any) -> None:
        for topic in _subscribed_topics(appliance):
            await self._subscribe_topic(topic)

    async def _subscribe_appliances(self) -> None:
        for appliance in self._appliances:
            await self._subscribe(appliance)

    async def _start_watchdog(self) -> None:
        if not self._watchdog_task or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog())

    async def _watchdog(self) -> None:
        failed_ticks = 0
        backoff = 0
        # Consecutive TOTAL-BLACKOUT re-subscribe ticks (Issue 1): a transport that awscrt
        # keeps reporting connected but on which NOT A SINGLE topic subscribes (half-open
        # socket / stale custom-authorizer session that never fires a disconnection
        # callback) would otherwise loop the in-place re-subscribe forever and never
        # refresh the AWS token via _start(). After _MAX_RESUBSCRIBE_FAILURES we escalate
        # to a full rebuild. Reset to 0 on ANY subscribe progress (a non-empty set after
        # the pass) or a rebuild. NOTE the change vs the binary-flag design: this counts
        # ONLY a blackout, NOT a partial miss -- one chronically broken appliance topic
        # (transport alive, other topics subscribed) is appliance-specific and must NOT
        # escalate to a rebuild that would drop the healthy subscriptions too (H1).
        resubscribe_failures = 0
        while True:
            # The rebuild (load_aws_token / subscribe) can raise transiently; without
            # this guard one exception would end the task and kill realtime until a
            # reload. Re-raise CancelledError FIRST (stop() cancels+awaits us, so
            # swallowing it would deadlock shutdown); on any other error log and keep
            # looping with an additive backoff (capped, reset on recovery).
            try:
                await asyncio.sleep(_WATCHDOG_INTERVAL + backoff)
                # Healthy ONLY when the transport is connected AND every target topic is
                # in the subscribed set (no missing topics). Checking _connection alone
                # treated a connected-but-unsubscribed client as healthy and stopped
                # rebuilding (the original bug), so realtime pushes died silently until
                # the next full disconnect.
                if self._connection and not (
                    self._all_topics() - self._subscribed_topics_set
                ):
                    failed_ticks = 0
                    backoff = 0
                    resubscribe_failures = 0
                    continue
                # Connected but at least one topic is missing (a rebuild whose subscribe
                # timed out, awscrt's own auto-reconnect on a clean session, OR one
                # chronically broken appliance topic): the transport is up, so a full
                # rebuild would needlessly tear down a working connection and re-hit the
                # AWS authorizer endpoint -- AND drop the topics that ARE subscribed.
                # Re-subscribe the MISSING topics in place instead -- UNLESS we have hit
                # _MAX_RESUBSCRIBE_FAILURES total-blackout ticks (Issue 1): a connection
                # awscrt still reports up but on which nothing SUBACKs at all is dead at
                # the application layer, and only _start() can refresh the token + rebuild
                # the client, so we fall through to the rebuild path below.
                if self._connection and resubscribe_failures < _MAX_RESUBSCRIBE_FAILURES:
                    _LOGGER.info("Re-subscribe mqtt topics")
                    gen = self._generation
                    await self._subscribe_missing()
                    # Re-check connection AND generation AFTER the await, not an
                    # unconditional commit (Issue 3): a disconnection callback can land on
                    # the awscrt thread during the subscribe and clear the set (and
                    # _connection); trusting the set here would leave a stuck "healthy on
                    # a dropped session". gen == _generation also rejects a rebuild that
                    # landed mid-await.
                    if not (self._connection and gen == self._generation):
                        self._subscribed_topics_set = set()
                        # The connection dropped (or a new client generation landed)
                        # during the subscribe: do NOT treat this tick as recovery. Leave
                        # the counters untouched and let the next tick's not-connected
                        # path count the outage normally.
                        continue
                    if self._subscribed_topics_set:
                        # At least one topic subscribed -> the connection is ALIVE. Any
                        # topics still missing are appliance-specific (one bad SUBACK),
                        # NOT a dead connection: do NOT escalate to a rebuild (it would
                        # drop the healthy subscriptions). Reset the outage counters as a
                        # genuine recovery (Issue 2 symmetry with the healthy branch) and
                        # keep retrying the missing topics on the next tick.
                        resubscribe_failures = 0
                        failed_ticks = 0
                        backoff = 0
                        continue
                    # Total blackout: NOTHING subscribed though awscrt reports connected.
                    # Count toward the escalation cap and back off (capped). Always
                    # `continue`: the escalation fires on the NEXT tick, where the
                    # re-subscribe branch is skipped (resubscribe_failures >= the cap) and
                    # `escalated` routes straight to the rebuild that refreshes the token
                    # and tears down the dead client. (Mirrors the old except-driven
                    # increment timing: the rebuild lands one tick past the cap.)
                    resubscribe_failures += 1
                    backoff = min(backoff + _WATCHDOG_INTERVAL, _RECONNECT_BACKOFF_CAP)
                    continue
                # Not connected (or re-subscribe escalated to a rebuild): sustained
                # downtime only forces a rebuild. Give awscrt's own auto-reconnect a
                # chance first (see _RECONNECT_AFTER_FAILED_TICKS) -- but an escalation
                # (blackout cap reached) skips the wait, the connection is already known
                # dead.
                escalated = (
                    self._connection
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
                await self._subscribe_missing()
                # Rebuild + subscribe done: commit the set. _start() reset it to empty, so
                # leaving it empty on a subscribe failure keeps the recovery loop honest.
                # Re-check connection AND generation AFTER the await (Issue 3), not an
                # unconditional commit, so a disconnect or newer generation landing during
                # the subscribe is not trusted.
                if not (self._connection and gen == self._generation):
                    self._subscribed_topics_set = set()
                resubscribe_failures = 0  # the rebuild re-subscribed
                backoff = 0  # rebuild succeeded
            except asyncio.CancelledError:
                raise
            except Exception:
                # Only the REBUILD path (load_aws_token / _start / the rebuild's
                # _subscribe_missing) can land here now: the in-place re-subscribe uses
                # _subscribe_missing(), which swallows a per-topic HonCodedError (the
                # blackout escalation is driven inline by resubscribe_failures, not by a
                # raise). Back off (capped, reset on recovery) so a persistent 5xx from
                # the AWS authorizer is not hammered every tick.
                backoff = min(backoff + _WATCHDOG_INTERVAL, _RECONNECT_BACKOFF_CAP)
                _LOGGER.warning(
                    "MQTT watchdog: reconnect failed, retrying in %ss",
                    _WATCHDOG_INTERVAL + backoff,
                    exc_info=True,
                )
