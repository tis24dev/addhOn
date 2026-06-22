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
import json
import logging
import secrets
from typing import Any

from awscrt import mqtt5
from awsiot import mqtt5_client_builder  # type: ignore[import-untyped]

from .device import MOBILE_ID

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


class NativeMqttClient:
    """Realtime push via AWS IoT MQTT5 on top of the native session."""

    def __init__(self, hon: Any, mobile_id: str) -> None:
        self._hon = hon
        self._mobile_id = mobile_id or MOBILE_ID
        self._api = hon.api
        self._appliances = hon.appliances
        self._client: mqtt5.Client | None = None
        self._connection = False
        self._watchdog_task: asyncio.Task[None] | None = None

    @property
    def client(self) -> mqtt5.Client:
        if self._client is None:
            raise AttributeError("MQTT client not started")
        return self._client

    async def create(self) -> "NativeMqttClient":
        await self._start()
        self._subscribe_appliances()
        await self._start_watchdog()
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
    def _on_lifecycle_stopped(self, data: "mqtt5.LifecycleStoppedData") -> None:
        _LOGGER.info("Lifecycle Stopped: %s", data)

    def _on_lifecycle_connection_success(
        self, data: "mqtt5.LifecycleConnectSuccessData"
    ) -> None:
        self._connection = True
        _LOGGER.info("Lifecycle Connection Success: %s", data)

    def _on_lifecycle_attempting_connect(
        self, data: "mqtt5.LifecycleAttemptingConnectData"
    ) -> None:
        _LOGGER.info("Lifecycle Attempting Connect: %s", data)

    def _on_lifecycle_connection_failure(
        self, data: "mqtt5.LifecycleConnectFailureData"
    ) -> None:
        self._connection = False
        _LOGGER.info("Lifecycle Connection Failure: %s", data)

    def _on_lifecycle_disconnection(
        self, data: "mqtt5.LifecycleDisconnectData"
    ) -> None:
        self._connection = False
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
            _LOGGER.debug("MQTT: undecodable payload on %s: %s", topic, err)
            return
        if not isinstance(payload, dict):
            _LOGGER.debug("MQTT: non-object payload on %s: %r", topic, payload)
            return
        # Defensive: appliance not found for this topic -> exit.
        appliance = next(
            (
                a
                for a in self._appliances
                if topic in a.info.get("topics", {}).get("subscribe", [])
            ),
            None,
        )
        if appliance is None:
            _LOGGER.debug("MQTT: topic with no matching appliance: %s", topic)
            return
        if topic and "appliancestatus" in topic:
            params = appliance.attributes.get("parameters", {})
            for parameter in payload.get("parameters", []):
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
                appliance.nick_name,
                payload.get("disconnectReason"),
            )
            appliance.connection = False
        elif topic and "connected" in topic:
            appliance.connection = True
            _LOGGER.info("Connected %s", appliance.nick_name)
        elif topic and "discovery" in topic:
            _LOGGER.info("Discovered %s", appliance.nick_name)
        self._hon.notify()
        _LOGGER.info("%s - %s", topic, payload)

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
        self._client = mqtt5_client_builder.websockets_with_custom_authorizer(
            endpoint=AWS_ENDPOINT,
            auth_authorizer_name=AWS_AUTHORIZER,
            auth_authorizer_signature=await self._api.load_aws_token(),
            auth_token_key_name="token",
            auth_token_value=self._api.auth.id_token,
            client_id=f"{self._mobile_id}_{secrets.token_hex(8)}",
            on_lifecycle_stopped=self._on_lifecycle_stopped,
            on_lifecycle_connection_success=self._on_lifecycle_connection_success,
            on_lifecycle_attempting_connect=self._on_lifecycle_attempting_connect,
            on_lifecycle_connection_failure=self._on_lifecycle_connection_failure,
            on_lifecycle_disconnection=self._on_lifecycle_disconnection,
            on_publish_received=self._on_publish_received,
        )
        self.client.start()

    def _subscribe_appliances(self) -> None:
        for appliance in self._appliances:
            self._subscribe(appliance)

    def _subscribe(self, appliance: Any) -> None:
        for topic in appliance.info.get("topics", {}).get("subscribe", []):
            self.client.subscribe(
                mqtt5.SubscribePacket([mqtt5.Subscription(topic)])
            ).result(_SUBSCRIBE_TIMEOUT)
            _LOGGER.info("Subscribed to topic %s", topic)

    async def _start_watchdog(self) -> None:
        if not self._watchdog_task or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog())

    async def _watchdog(self) -> None:
        failed_ticks = 0
        while True:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            if self._connection:
                failed_ticks = 0
                continue
            # Sustained downtime only: give awscrt's own auto-reconnect a chance
            # before forcing a rebuild (see _RECONNECT_AFTER_FAILED_TICKS).
            failed_ticks += 1
            if failed_ticks < _RECONNECT_AFTER_FAILED_TICKS:
                continue
            failed_ticks = 0
            _LOGGER.info("Restart mqtt connection")
            await self._start()
            self._subscribe_appliances()
