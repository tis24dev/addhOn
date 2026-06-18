"""API HTTP nativa del cloud hОn (transport addhОn, Fase 3 piece 2).

Riscrittura (NON copia) dei metodi autenticati di `_vendor/pyhon/connection/api.HonAPI`
sopra il nostro `HonConnection` (che inietta i token per-richiesta e gestisce il
retry su scadenza/401-403). Ogni metodo ritorna la STESSA shape JSON che il motore
parser/command_loader di pyhОn si aspetta: è il contratto del piece 3 (l'orchestrazione
nativa riusa `HonAppliance`/`HonCommandLoader` di pyhОn iniettando QUESTO api).

DUE filosofie, deliberate (come parse.py/tokens.py):
  * COSTRUZIONE RICHIESTA (verbo, path, params, body) = EXACT-PRESERVING: va al
    cloud byte-identica a pyhОn (le quirk contano, es. il timestamp di send_command).
  * ESTRAZIONE RISPOSTA = difensiva (come parse.py): dove pyhОn solleverebbe
    KeyError/AttributeError su una risposta malformata, noi ricadiamo sul default
    vuoto sicuro. Su ogni risposta ben formata il risultato è IDENTICO a pyhОn
    (lo verifica il differential test); le divergenze sono solo sul ramo malformato.

Metodi ANONIMI (appliance_configuration / app_config / translation_keys) NON sono
qui: usano un handler senza auth e non entrano nel flusso di setup dei nostri
appliance; restano a pyhОn finché non servono.

`appliance` è duck-typed (`Any`): leggiamo solo `.appliance_type`, `.appliance_model_id`,
`.mac_address`, `.code`, `.info` (dict), `.options`. Così `transport/` resta
_vendor-free (nessun import di pyhОn), com'è per tutto lo strato nativo.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pprint import pformat
from typing import Any

from . import device as _device
from .connection import HonConnection
from .parse import parse_appliance_list

_LOGGER = logging.getLogger(__name__)

# Base URL del cloud hОn (pyhОn const.API_URL).
API_URL = "https://api-iot.he.services"


def _command_timestamp() -> str:
    """Timestamp UTC del comando in millisecondi + "Z" (es. 2026-06-18T12:34:56.789Z).

    pyhОn usa `datetime.utcnow().isoformat()[:-3] + "Z"`: lo slice `[:-3]` taglia i
    microsecondi (6 cifre) a millisecondi (3), MA quando `microsecond == 0`
    `isoformat()` omette del tutto la parte frazionaria, così `[:-3]` mangia i
    secondi e produce un timestamp MALFORMATO ("...T12:34Z"). È un bug di pyhОn
    (raro, ~1/1e6 invii). Qui usiamo `isoformat(timespec="milliseconds")` che rende
    SEMPRE esattamente 3 cifre frazionarie: byte-identico a pyhОn sul percorso
    normale (microsecondi != 0) e corretto ("...56.000Z") sul caso che pyhОn rompe.
    `replace(tzinfo=None)` evita il suffisso "+00:00" (manteniamo il valore UTC naive,
    come l'ormai deprecato `utcnow()`).
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return f"{now.isoformat(timespec='milliseconds')}Z"


class HonApi:
    """Metodi HTTP del cloud hОn sopra una `HonConnection` autenticata.

    Drop-in del `HonAPI` di pyhОn dal punto di vista del motore parser: stesse
    firme (appliance duck-typed) e stesse shape di ritorno.
    """

    def __init__(self, connection: HonConnection) -> None:
        self._connection = connection

    @property
    def auth(self) -> Any:
        """L'oggetto auth della connessione (come pyhОn `HonAPI.auth`)."""
        return self._connection.auth

    async def load_appliances(self) -> list:
        # L'app hОn legge la lista appliance dall'aggregatore unified-api via POST
        # (fix v2.7.1: il vecchio GET commands/v1/appliance ritorna [] per ogni
        # account). L'estrazione difensiva vive in parse.parse_appliance_list.
        device_id = self._connection.device.mobile_id or _device.MOBILE_ID
        async with self._connection.post(
            f"{API_URL}/unified-api/v1/view/appliance-list",
            json={"deviceId": device_id},
        ) as resp:
            result = await resp.json()
        appliances = parse_appliance_list(result)
        if not appliances:
            # Request/auth OK ma 0 appliance: logga la struttura della risposta per
            # distinguere un account davvero vuoto da un cambio API (la lista
            # unified-api include anche gli offline). Diagnostica portata da pyhОn.
            modules = result.get("modules") if isinstance(result, dict) else None
            _LOGGER.warning(
                "hОn API: 0 appliance (request OK). result keys=%s; modules keys=%s. "
                "Se gli apparecchi compaiono nell'app hОn, è probabile un cambio API "
                "più che un account vuoto/non condiviso.",
                sorted(result.keys()) if isinstance(result, dict) else "n/a",
                sorted(modules.keys()) if isinstance(modules, dict) else "n/a",
            )
            _LOGGER.debug("hОn risposta appliance grezza: %s", result)
        return appliances

    async def load_commands(self, appliance: Any) -> dict:
        params: dict[str, Any] = {
            "applianceType": appliance.appliance_type,
            "applianceModelId": appliance.appliance_model_id,
            "macAddress": appliance.mac_address,
            "os": _device.OS,
            "appVersion": _device.APP_VERSION,
            "code": appliance.code,
        }
        if firmware_id := appliance.info.get("eepromId"):
            params["firmwareId"] = firmware_id
        if firmware_version := appliance.info.get("fwVersion"):
            params["fwVersion"] = firmware_version
        if series := appliance.info.get("series"):
            params["series"] = series
        url = f"{API_URL}/commands/v1/retrieve"
        async with self._connection.get(url, params=params) as response:
            data = await response.json()
        payload = data.get("payload") if isinstance(data, dict) else None
        # pyhОn fa `result.pop("resultCode")` (KeyError su payload senza resultCode,
        # TypeError su payload non-dict); qui ramo-errore su qualsiasi forma non
        # valida -> {}, identico al ben formato. Il pop RIMUOVE resultCode dal dict
        # ritornato (il parser non lo vuole nelle voci comando): preservato.
        if not isinstance(payload, dict) or not payload:
            _LOGGER.error("hОn load_commands: payload invalido: %s", data)
            return {}
        if payload.pop("resultCode", None) != "0":
            _LOGGER.error("hОn load_commands: resultCode != 0: %s", data)
            return {}
        return payload

    async def load_command_history(self, appliance: Any) -> list:
        url = f"{API_URL}/commands/v1/appliance/{appliance.mac_address}/history"
        async with self._connection.get(url) as response:
            result = await response.json()
        if not isinstance(result, dict) or not result.get("payload"):
            return []
        payload = result["payload"]
        history = payload.get("history", []) if isinstance(payload, dict) else []
        return history if isinstance(history, list) else []

    async def load_favourites(self, appliance: Any) -> list:
        url = f"{API_URL}/commands/v1/appliance/{appliance.mac_address}/favourite"
        async with self._connection.get(url) as response:
            result = await response.json()
        if not isinstance(result, dict) or not result.get("payload"):
            return []
        payload = result["payload"]
        favourites = payload.get("favourites", []) if isinstance(payload, dict) else []
        return favourites if isinstance(favourites, list) else []

    async def load_last_activity(self, appliance: Any) -> dict:
        url = f"{API_URL}/commands/v1/retrieve-last-activity"
        params = {"macAddress": appliance.mac_address}
        async with self._connection.get(url, params=params) as response:
            result = await response.json()
        if isinstance(result, dict):
            activity = result.get("attributes")
            if isinstance(activity, dict) and activity:
                return activity
        return {}

    async def load_appliance_data(self, appliance: Any) -> dict:
        url = f"{API_URL}/commands/v1/appliance-model"
        params = {"code": appliance.code, "macAddress": appliance.mac_address}
        async with self._connection.get(url, params=params) as response:
            result = await response.json()
        if isinstance(result, dict):
            payload = result.get("payload")
            if isinstance(payload, dict):
                data = payload.get("applianceModel", {})
                return data if isinstance(data, dict) else {}
        return {}

    async def load_attributes(self, appliance: Any) -> dict:
        params = {
            "macAddress": appliance.mac_address,
            "applianceType": appliance.appliance_type,
            "category": "CYCLE",
        }
        url = f"{API_URL}/commands/v1/context"
        async with self._connection.get(url, params=params) as response:
            data = await response.json()
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        return payload if isinstance(payload, dict) else {}

    async def load_statistics(self, appliance: Any) -> dict:
        params = {
            "macAddress": appliance.mac_address,
            "applianceType": appliance.appliance_type,
        }
        url = f"{API_URL}/commands/v1/statistics"
        async with self._connection.get(url, params=params) as response:
            data = await response.json()
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        return payload if isinstance(payload, dict) else {}

    async def load_maintenance(self, appliance: Any) -> dict:
        url = f"{API_URL}/commands/v1/maintenance-cycle"
        params = {"macAddress": appliance.mac_address}
        async with self._connection.get(url, params=params) as response:
            data = await response.json()
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        return payload if isinstance(payload, dict) else {}

    async def load_aws_token(self) -> str:
        url = f"{API_URL}/auth/v1/introspection"
        async with self._connection.get(url) as response:
            data = await response.json()
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        token = payload.get("tokenSigned", "") if isinstance(payload, dict) else ""
        return token if isinstance(token, str) else ""

    async def send_command(
        self,
        appliance: Any,
        command: str,
        parameters: dict[str, Any],
        ancillary_parameters: dict[str, Any],
        program_name: str = "",
    ) -> bool:
        timestamp = _command_timestamp()
        data: dict[str, Any] = {
            "macAddress": appliance.mac_address,
            "timestamp": timestamp,
            "commandName": command,
            "transactionId": f"{appliance.mac_address}_{timestamp}",
            "applianceOptions": appliance.options,
            "device": self._connection.device.payload(mobile=True),
            "attributes": {
                "channel": "mobileApp",
                "origin": "standardProgram",
                "energyLabel": "0",
            },
            "ancillaryParameters": ancillary_parameters,
            "parameters": parameters,
            "applianceType": appliance.appliance_type,
        }
        if command == "startProgram" and program_name:
            data["programName"] = program_name.upper()
        url = f"{API_URL}/commands/v1/send"
        async with self._connection.post(url, json=data) as response:
            json_data = await response.json()
            payload = json_data.get("payload") if isinstance(json_data, dict) else None
            if isinstance(payload, dict) and payload.get("resultCode") == "0":
                return True
            _LOGGER.error("hОn send_command fallito: %s", await response.text())
            _LOGGER.error("%s - Payload:\n%s", url, pformat(data))
        return False

    async def close(self) -> None:
        await self._connection.close()
