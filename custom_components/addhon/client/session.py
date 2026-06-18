"""Orchestrazione `Hon` nativa di addhОn (Fase 3 piece 3).

Riscrive `_vendor/pyhon/hon.Hon` (il "chi coordina il setup") sopra il NOSTRO
transport nativo (`transport.connection.HonConnection` + `transport.api.HonApi`),
RIUSANDO il motore parser di pyhОn (`HonAppliance`/`HonCommandLoader`/parser),
a cui inietta il nostro `api`. È il penultimo passo dello strangler: quando regge
live, il piece 4 fa puntare `pyhon_adapter.create_session` qui e cancella
`_vendor/connection/`.

Confine: questo file è `_vendor`-free. La costruzione degli oggetti-motore di
pyhОn (HonAppliance, MQTTClient) passa dall'UNICO ponte `pyhon_adapter`
(MIGRATION.md regola 1). `NativeHon` soddisfa il Protocol `interfaces.HonSession`
ed espone `.api`/`.appliances`/`subscribe_updates`/`notify` come il `Hon` di pyhОn
(il `MQTTClient` riusato legge proprio quei membri).

Sequenza di setup (fedele a pyhОn): create connessione → `api.load_appliances()`
→ per ogni appliance costruisci HonAppliance e carica commands/attributes/statistics
(motore pyhОn col nostro api) → avvia MQTT. L'ordine conta: i load_* fanno le prime
richieste HTTP che popolano i token, così quando MQTT parte `api.auth.id_token` c'è.
"""
from __future__ import annotations

import logging
from types import TracebackType
from typing import Any

import aiohttp

from . import pyhon_adapter
from .transport.api import HonApi
from .transport.auth import NativeAuthError
from .transport.connection import HonConnection

_LOGGER = logging.getLogger(__name__)


class NativeHon:
    """Sessione hОn nativa: auth+transport NOSTRI, motore parser di pyhОn.

    Drop-in di `pyhon.Hon` per l'integrazione: context manager async che espone
    `.appliances` (e `.api` per il MQTT). `enable_mqtt=False` salta il push AWS
    (utile a test/validatori; la produzione lo lascia attivo come pyhОn).
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
            raise NativeAuthError("sessione non creata (manca create())")
        return self._api

    @property
    def appliances(self) -> list[Any]:
        return self._appliances

    @appliances.setter
    def appliances(self, appliances: list[Any]) -> None:
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
        appliance = pyhon_adapter.create_appliance(self._api, appliance_data, zone=zone)
        if appliance.mac_address == "":
            return
        try:
            await appliance.load_commands()
            await appliance.load_attributes()
            await appliance.load_statistics()
        except (KeyError, ValueError, IndexError) as error:
            # Come pyhОn: un appliance con dati malformati non deve far saltare
            # l'intero setup; lo si tiene comunque (stato parziale) e si logga.
            _LOGGER.exception(error)
            _LOGGER.error("Device data - %s", appliance_data)
        self._appliances.append(appliance)

    async def setup(self) -> None:
        appliances = await self.api.load_appliances()
        for appliance in appliances:
            if (zones := int(appliance.get("zone", "0"))) > 1:
                for zone in range(zones):
                    await self._create_appliance(appliance.copy(), zone=zone + 1)
            await self._create_appliance(appliance)
        if self._enable_mqtt and not self._mqtt_client:
            self._mqtt_client = await pyhon_adapter.create_mqtt(self, self._mobile_id)

    def subscribe_updates(self, notify_function: Any) -> None:
        self._notify_function = notify_function

    def notify(self) -> None:
        if self._notify_function:
            self._notify_function(None)

    async def close(self) -> None:
        # Ferma il MQTT PRIMA della connessione (il watchdog non deve ritentare su
        # una sessione in chiusura). pyhОn non lo faceva (leak): lo facciamo noi.
        if self._mqtt_client is not None:
            await pyhon_adapter.stop_mqtt(self._mqtt_client)
            self._mqtt_client = None
        if self._api is not None:
            await self._api.close()
