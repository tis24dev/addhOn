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
from .transport.auth import NativeAuthError
from .transport.connection import HonConnection
from ..debug_utils import redact_identity

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
    ) -> None:
        self._email = email
        self._password = password
        self._session = session
        self._mobile_id = mobile_id
        self._refresh_token = refresh_token
        self._enable_mqtt = enable_mqtt
        self._connection: HonConnection | None = None
        self._api: HonApi | None = None
        self._appliances: list[Any] = []
        self._mqtt_client: Any = None
        self._notify_function: Any = None

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
        self._connection = await HonConnection(
            self._email,
            self._password,
            session=self._session,
            mobile_id=self._mobile_id,
            refresh_token=self._refresh_token,
        ).create()
        self._api = HonApi(self._connection)
        await self.setup()
        return self

    async def _create_appliance(self, appliance_data: dict, zone: int = 0) -> None:
        appliance = factory.create_appliance(self._api, appliance_data, zone=zone)
        if appliance.mac_address == "":
            return
        try:
            await appliance.load_commands()
            await appliance.load_attributes()
            await appliance.load_statistics()
        except (KeyError, ValueError, IndexError) as error:
            # An appliance with malformed data must not break the others; it is
            # kept anyway (partial state) and logged. appliance_data is the RAW
            # device dict (macAddress/serialNumber in cleartext) and this ERROR is
            # never gated by the debug toggles -> it lands in home-assistant.log,
            # the file users attach to issues. Redact identity before logging (the
            # traceback carries no credentials, so _LOGGER.exception stays). The
            # full (redacted) dict is available via Download Diagnostics.
            _LOGGER.exception(error)
            _LOGGER.error("Device data - %s", redact_identity(appliance_data))
        self._appliances.append(appliance)

    async def setup(self) -> None:
        appliances = await self.api.load_appliances()
        for appliance in appliances:
            if (zones := int(appliance.get("zone", "0"))) > 1:
                for zone in range(zones):
                    await self._create_appliance(appliance.copy(), zone=zone + 1)
            await self._create_appliance(appliance)
        if self._enable_mqtt and not self._mqtt_client:
            self._mqtt_client = await self._make_mqtt()

    async def _make_mqtt(self) -> Any:
        # Lazy import: transport.mqtt imports awscrt/awsiot (absent dry/CI).
        from .transport.mqtt import NativeMqttClient

        return await NativeMqttClient(self, self._mobile_id).create()

    def subscribe_updates(self, notify_function: Any) -> None:
        self._notify_function = notify_function

    def notify(self) -> None:
        if self._notify_function:
            self._notify_function(None)

    async def close(self) -> None:
        # Stop the MQTT BEFORE the connection (the watchdog must not retry on
        # a session being closed); we close it to avoid leaking it.
        if self._mqtt_client is not None:
            await self._mqtt_client.stop()
            self._mqtt_client = None
        if self._api is not None:
            await self._api.close()
