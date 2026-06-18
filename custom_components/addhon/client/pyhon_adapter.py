"""Adattatore-ponte verso il pyhОn vendorizzato (transizione).

Durante la migrazione questo è l'UNICO file di `client/` che importa
`_vendor.pyhon` (vedi MIGRATION.md, regola 1). Il corpo dell'integrazione
(`hon_client.py`) ottiene la sessione hОn DA QUI, non più con un import diretto
di `_vendor.pyhon`: così è disaccoppiato da pyhОn dietro questa funzione, e
quando arriverà il transport nativo si cambia solo qui.

`create_session` ritorna un oggetto conforme a `interfaces.HonSession`
(oggi: `pyhon.Hon`; domani: il client nativo). Qui vive anche la patch BABYCARE
di HonParameterEnum (anch'essa tocca `_vendor`, quindi sta nel ponte).
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Stato della patch BABYCARE: globale di processo e thread-safe tra config entry
# (la classe HonParameterEnum di pyhОn è condivisa). Vive qui perché questo è
# l'unico file che importa _vendor.pyhon.parameter.enum.
_ENUM_PATCH_LOCK = threading.Lock()
_ENUM_PATCH_APPLIED = False

# FLIP dell'auth: stato del monkeypatch che fa usare alla macchina pyhОn il
# NOSTRO HonAuth nativo (drop-in). Idempotente, thread-safe.
_NATIVE_AUTH_LOCK = threading.Lock()
_NATIVE_AUTH_INSTALLED = False


def install_native_auth() -> None:
    """LEGACY (pre-flip): sostituisce l'HonAuth di pyhОn col NOSTRO auth nativo.

    Superato dal FLIP completo: `create_session` ora ritorna `NativeHon`, che usa
    il nostro auth/transport nativamente, senza iniettarsi in pyhОn. Mantenuto
    (non chiamato da create_session) per il percorso ibrido e i test.

    Il nostro `client.transport.auth.HonAuth` è un drop-in (stessa interfaccia:
    cognito_token/id_token/refresh_token/authenticate/refresh/clear/token_*). Lo
    iniettiamo nel namespace dell'handler di pyhОn (`HonConnectionHandler.create`
    fa `self._auth = HonAuth(...)` con lookup del nome a runtime), così il login
    di produzione gira sul NOSTRO flusso (validato live), tenendo api+parser di
    pyhОn. Stesso meccanismo della patch enum; idempotente e best-effort.
    """
    global _NATIVE_AUTH_INSTALLED
    with _NATIVE_AUTH_LOCK:
        if _NATIVE_AUTH_INSTALLED:
            return
        try:
            from .._vendor.pyhon.connection.handler import hon as _hon_handler
            from .transport.auth import HonAuth as _NativeHonAuth

            _hon_handler.HonAuth = _NativeHonAuth
            _NATIVE_AUTH_INSTALLED = True
            _LOGGER.info("addhОn: auth nativo iniettato in pyhОn (flip)")
        except Exception as err:  # pragma: no cover - difensivo
            _LOGGER.warning("addhОn: impossibile installare l'auth nativo: %s", err)


def create_session(email: str, password: str) -> Any:
    """Crea la sessione hОn NATIVA (`client.session.NativeHon`).

    FLIP COMPLETO del transport (Fase 3 piece 4): auth, connessione, api e
    orchestrazione sono NOSTRI; di pyhОn resta solo il motore parser
    (HonAppliance/HonCommandLoader, riusato dentro NativeHon) + il MQTTClient.
    Prima qui si creava un `pyhon.Hon` col nostro auth INIETTATO
    (`install_native_auth`, ora superato): il transport pyhОn non gira più in
    produzione. Il chiamante la usa identica a prima (`__aenter__()` → `.appliances`).

    Import lazy di `NativeHon`: evita il ciclo (session.py importa questo modulo) e
    tiene `pyhon_adapter` importabile a secco (gli import di _vendor restano lazy,
    nei factory `create_appliance`/`create_mqtt`/`ensure_enum_patch`).
    """
    from .session import NativeHon

    return NativeHon(email=email, password=password)


def create_appliance(api: Any, appliance_data: dict, zone: int = 0) -> Any:
    """Costruisce un HonAppliance di pyhОn (il MOTORE parser che ancora riusiamo).

    Il `Hon` nativo (`client/session.py`) orchestra il setup ma RIUSA questo
    motore, iniettandogli il NOSTRO `api` (transport.api.HonApi). Tenere la
    costruzione qui mantiene `pyhon_adapter` l'UNICO file di `client/` che importa
    `_vendor.pyhon` (MIGRATION.md regola 1). L'oggetto ritornato è conforme al
    Protocol `interfaces.Appliance` (duck-typing). Import lazy.
    """
    from .._vendor.pyhon.appliance import HonAppliance

    return HonAppliance(api, appliance_data, zone=zone)


async def create_mqtt(hon: Any, mobile_id: str) -> Any:
    """Avvia il MQTTClient di pyhОn (push background AWS IoT) per la sessione nativa.

    pyhОn lo crea in `Hon.setup()`; lo riusiamo finché non riscriviamo/decidiamo
    il transport MQTT (è in `_vendor/connection/`, bersaglio del piece 4b). Import
    lazy: `mqtt.py` importa awscrt/awsiot, assenti negli ambienti di test offline.
    `MQTTClient` legge `hon.api`, `hon.appliances`, `hon.notify` dall'oggetto passato.
    """
    from .._vendor.pyhon.connection.mqtt import MQTTClient

    return await MQTTClient(hon, mobile_id).create()


async def stop_mqtt(mqtt_client: Any) -> None:
    """Ferma best-effort il MQTTClient di pyhОn (watchdog + websocket awscrt).

    pyhОn NON lo fa in `Hon.close()`: a ogni reload della config entry resta una
    connessione AWS IoT orfana (il task watchdog viene poi cancellato dal teardown
    del loop dedicato, ma la connessione nativa awscrt no). Ora che l'orchestrazione
    è nostra (`NativeHon.close()`), la chiudiamo. pyhОn non espone un metodo stop
    ufficiale → tocchiamo gli internals con cautela, tutto best-effort e guardato.
    """
    if mqtt_client is None:
        return
    # Cancella E ATTENDI il watchdog PRIMA di leggere/fermare il client: se fosse
    # mid-`_start()` ricreerebbe `_client` (ne perderemmo uno). Awaitarlo garantisce
    # che il coroutine si sia srotolato (ogni `_start()` in volo è concluso/abortito).
    task = getattr(mqtt_client, "_watchdog_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as err:  # pragma: no cover - difensivo
            _LOGGER.debug("addhОn: attesa cancel watchdog MQTT fallita: %s", err)
    client = getattr(mqtt_client, "_client", None)
    if client is not None:
        try:
            client.stop()
        except Exception as err:  # pragma: no cover - difensivo
            _LOGGER.debug("addhОn: stop client MQTT fallito: %s", err)


def ensure_enum_patch() -> None:
    """Applica una sola volta per processo la patch BABYCARE di HonParameterEnum.

    pyhOn crasha su load_commands() dell'asciugatrice TD perché il valore
    "BABYCARE" è nell'elenco dei valori ammessi ma il confronto stringa fallisce
    per un bug interno del setter HonParameterEnum.value. La patch accetta il
    valore se è già presente in _values.

    È best-effort e idempotente: protetta da un lock di modulo (la classe pyhOn è
    globale e condivisa tra tutte le config entry) e applicata al più una volta,
    catturando il setter ORIGINALE una sola volta per non annidare le closure a
    ogni reauth. In caso di errore il flag resta False, così un setup successivo
    può ritentare.
    """
    global _ENUM_PATCH_APPLIED
    with _ENUM_PATCH_LOCK:
        if _ENUM_PATCH_APPLIED:
            return
        try:
            from .._vendor.pyhon.parameter.enum import HonParameterEnum as _HonEnum

            _orig_setter = _HonEnum.value.fset

            def _patched_setter(instance, value):
                try:
                    _orig_setter(instance, value)
                except ValueError:
                    # Accetta il valore se è già presente nella lista (case-sensitive)
                    if value in instance._values:
                        instance._value = value
                        _LOGGER.debug("Patch enum BABYCARE applicata per valore: %s", value)
                    else:
                        raise

            _HonEnum.value = property(
                _HonEnum.value.fget, _patched_setter, _HonEnum.value.fdel
            )
            _ENUM_PATCH_APPLIED = True
            _LOGGER.debug("Patch HonParameterEnum applicata")
        except Exception as patch_err:
            # Best-effort: non impostiamo il flag così un setup successivo ritenta.
            _LOGGER.warning("Impossibile applicare la patch HonParameterEnum: %s", patch_err)
