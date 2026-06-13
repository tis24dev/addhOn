"""Client asincrono per le API hOn di Haier."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any

from .debug_utils import debug_key_sample

_LOGGER = logging.getLogger(__name__)

# Guardie per applicare la patch BABYCARE di HonParameterEnum una sola volta per
# processo, in modo thread-safe tra più config entry (vedi _ensure_enum_patch).
_ENUM_PATCH_LOCK = threading.Lock()
_ENUM_PATCH_APPLIED = False

_SERIAL_ATTRS = ("serial_number", "serialNumber", "mac_address", "macAddress", "code")
_CONSUMPTION_ATTRS = (
    "totalElectricityUsed",
    "currentElectricityUsed",
    "totalWaterUsed",
    "currentWaterUsed",
    "totalWashCycle",
)


def _debug_container_to_dict(container, label: str) -> dict:
    """Best-effort conversione di container pyhOn per logging diagnostico."""
    if container is None:
        return {}
    if isinstance(container, dict):
        return dict(container)
    try:
        return dict(container)
    except Exception as err:
        _LOGGER.debug(
            "Consumi debug: impossibile convertire %s (%s): %s",
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
    """Log corposo per capire dove spariscono i contatori consumo."""
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return

    stats = _debug_container_to_dict(getattr(appliance, "statistics", None), "statistics")
    raw_attrs = _debug_container_to_dict(getattr(appliance, "attributes", None), "attributes")
    settings = _debug_container_to_dict(getattr(appliance, "settings", None), "settings")
    merged_attrs = attributes if attributes is not None else _get_attributes(appliance)
    commands = getattr(appliance, "commands", None)
    command_names = sorted(commands.keys()) if isinstance(commands, dict) else []

    _LOGGER.debug(
        "Consumi debug [%s] '%s' type=%s id=%s: "
        "statistics_type=%s statistics_keys=%d %s statistics_values=%s; "
        "raw_attribute_keys=%d %s; settings_keys=%d %s; "
        "merged_keys=%d %s merged_values=%s; "
        "load_statistics=%s update=%s commands=%s",
        stage,
        _get_name(appliance),
        _get_type(appliance),
        getattr(appliance, "unique_id", None) or _get_serial(appliance) or "<no-id>",
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
    """Estrae gli attributi dal device, cercando in statistics, attributes e settings."""
    attributes = {}

    # I contatori di consumo (totalElectricityUsed, totalWaterUsed,
    # totalWashCycle, currentElectricityUsed, currentWaterUsed, ...) vivono nel
    # container pyhOn `statistics`, popolato da load_statistics() ma finora MAI
    # esposto ai sensori. Lo uniamo per primo, così attributi real-time e
    # settings vincono in caso di chiavi in conflitto.
    stats = getattr(appliance, "statistics", None)
    if isinstance(stats, dict):
        attributes.update(stats)
    elif stats is not None:
        try:
            attributes.update(dict(stats))
        except Exception as err:
            _LOGGER.debug("Errore lettura statistics: %s", err)

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
                    _LOGGER.debug("Errore lettura parameters: %s", e)
    elif hasattr(raw, "parameters"):
        try:
            attributes.update(dict(raw.parameters))
        except Exception:
            pass

    if hasattr(appliance, "settings"):
        try:
            attributes.update(dict(appliance.settings))
        except Exception as err:
            _LOGGER.error("Errore lettura settings: %s", err)

    return attributes


def _error_text(err: BaseException) -> str:
    return str(err).lower()


def _is_auth_error(err: BaseException) -> bool:
    err_str = _error_text(err)
    return any(k in err_str for k in (
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
        "session non disponibile",
        "session unavailable",
    ))


def _requires_reauth(err: BaseException) -> bool:
    return (
        _is_auth_error(err) or _is_missing_session_error(err)
    ) and not _is_retryable_server_error(err)


def _ensure_enum_patch() -> None:
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
            from pyhon.parameter.enum import HonParameterEnum as _HonEnum

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


class HonClient:
    """Gestisce la connessione alle API Haier hOn tramite pyhOn.

    Strategia loop:
    - Manteniamo un singolo event loop dedicato (_hon_loop) che gira su un
      thread di background (_hon_thread).
    - TUTTE le chiamate a pyhOn (setup, update, comandi) vengono eseguite su
      quel loop tramite asyncio.run_coroutine_threadsafe(), così la sessione
      aiohttp non cambia mai loop e non va mai in errore.
    - L'event loop di HA non viene mai bloccato.
    """

    _RUN_TIMEOUT = 60
    _CANCEL_TIMEOUT = 5

    def __init__(self, email: str, password: str) -> None:
        self._email = email
        self._password = password
        self._hon_instance = None
        self._api = None
        self._hon_loop: asyncio.AbstractEventLoop | None = None
        self._hon_thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()

    # ── Gestione loop dedicato ────────────────────────────────────────────────

    def _start_hon_loop(self) -> None:
        """Avvia il loop dedicato su un thread di background."""
        self._hon_loop = asyncio.new_event_loop()
        self._hon_thread = threading.Thread(
            target=self._hon_loop.run_forever,
            name="haier_hon_loop",
            daemon=True,
        )
        self._hon_thread.start()
        _LOGGER.debug("Loop dedicato hOn avviato su thread '%s'", self._hon_thread.name)

    def _run_on_hon_loop(self, coro) -> Any:
        """Esegue una coroutine sul loop dedicato e aspetta il risultato.

        Chiamare solo da un thread non-loop (es. executor di HA).
        """
        with self._lifecycle_lock:
            loop = self._hon_loop
            if loop is None or not loop.is_running():
                if hasattr(coro, "close"):
                    coro.close()
                raise RuntimeError("Loop dedicato hOn non attivo")
            if threading.current_thread() is self._hon_thread:
                if hasattr(coro, "close"):
                    coro.close()
                raise RuntimeError("Chiamata sincrona sul loop hOn non consentita")

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
            except concurrent.futures.TimeoutError:
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
                            _LOGGER.debug("Errore durante cancellazione task hOn: %s", err)
                        if not future.done():
                            future.cancel()
                        if not drain_future.done():
                            drain_future.set_result(None)

                    loop.create_task(_drain_task())

                try:
                    loop.call_soon_threadsafe(_cancel_and_drain)
                    drain_future.result(timeout=self._CANCEL_TIMEOUT)
                except Exception as err:
                    _LOGGER.debug("Timeout durante cancellazione task hOn: %s", err)
                raise

    def _cancel_pending_tasks(self, loop: asyncio.AbstractEventLoop) -> None:
        """Cancella task residui prima di fermare il loop dedicato."""

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
            _LOGGER.debug("Errore cancellazione task hOn pendenti: %s", err)

    def _stop_hon_loop(self) -> None:
        """Ferma il loop dedicato e il thread."""
        loop = self._hon_loop
        thread = self._hon_thread

        if loop and loop.is_running() and thread is not threading.current_thread():
            self._cancel_pending_tasks(loop)
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=10)
        if thread and thread.is_alive():
            _LOGGER.warning("Thread dedicato hOn non terminato entro il timeout")
            return
        if loop and not loop.is_closed():
            try:
                loop.close()
            except Exception as err:
                _LOGGER.warning("Errore chiusura loop hOn: %s", err)
                return
        self._hon_loop = None
        self._hon_thread = None

    def _close_sync(self) -> None:
        """Chiude sessione pyhOn e loop dedicato in modo idempotente."""
        with self._lifecycle_lock:
            hon = self._hon_instance
            self._hon_instance = None
            self._api = None

            if hon is not None:
                try:
                    self._run_on_hon_loop(hon.__aexit__(None, None, None))
                except Exception as err:
                    _LOGGER.debug("Errore chiusura sessione hOn: %s", err)
            self._stop_hon_loop()

    # ── Setup ─────────────────────────────────────────────────────────────────

    def setup_sync(self) -> None:
        """Setup completo di pyhOn in executor (NON sull'event loop di HA).

        Avvia il loop dedicato, crea l'istanza Hon e completa il login.
        La sessione aiohttp viene creata sul loop dedicato e vi rimane
        legata per tutta la durata del client.
        """
        try:
            from pyhon import Hon
        except ImportError as err:
            raise ImportError("La libreria pyhOn non è installata.") from err

        with self._lifecycle_lock:
            try:
                if self._hon_loop is None or not self._hon_loop.is_running():
                    self._start_hon_loop()

                # Patch BABYCARE per il bug enum di pyhOn: best-effort e applicata
                # una sola volta per processo (vedi _ensure_enum_patch).
                _ensure_enum_patch()

                self._hon_instance = Hon(email=self._email, password=self._password)
                _LOGGER.debug("Istanza Hon creata")

                # Login + init sessione aiohttp — sul loop dedicato
                self._api = self._run_on_hon_loop(self._hon_instance.__aenter__())
                _LOGGER.info("Connessione a hOn riuscita per %s", self._email)
            except Exception:
                self._close_sync()
                raise

    async def async_complete_setup(self) -> None:
        """Verifica che il setup sia andato a buon fine."""
        if self._api is None:
            raise RuntimeError("setup_sync() non ha completato il login hOn")

    def run_command_sync(self, coro) -> Any:
        """Esegue una coroutine pyhOn (es. command.send()) sul loop dedicato.

        Da chiamare in executor — non sull'event loop di HA.
        """
        return self._run_on_hon_loop(coro)

    # ── Appliances ───────────────────────────────────────────────────────────

    async def async_get_appliances(self) -> list:
        if self._api is None:
            raise RuntimeError("hOn session non disponibile")
        try:
            return self._api.appliances
        except Exception as err:
            _LOGGER.error("Errore recupero elettrodomestici: %s", err)
            raise RuntimeError(f"Errore recupero elettrodomestici: {err}") from err

    def _update_appliance_sync(self, appliance) -> None:
        """Aggiorna un appliance sul loop dedicato (sincrono, chiamato in executor)."""

        async def _do_update():
            update_returned_empty = False
            _debug_appliance_consumption("prima update", appliance)

            # Tentativo 1: update() standard
            if hasattr(appliance, "update") and callable(appliance.update):
                try:
                    await appliance.update()
                    attrs_after_update = _get_attributes(appliance)
                    _debug_appliance_consumption("dopo update()", appliance, attrs_after_update)
                    if attrs_after_update:
                        _LOGGER.debug(
                            "Consumi debug: update() ha prodotto %d attributi per '%s' "
                            "(type=%s); fallback load_attributes/load_commands/"
                            "load_statistics non eseguito in questo ciclo.",
                            len(attrs_after_update),
                            _get_name(appliance),
                            _get_type(appliance),
                        )
                        return
                    update_returned_empty = True
                    _LOGGER.debug("update() completato senza dati — provo load_*")
                except Exception as err:
                    if _requires_reauth(err) or _is_retryable_server_error(err):
                        raise
                    _LOGGER.debug("update() fallito: %s — provo load_*", err or "<no msg>")

            # Tentativo 2: load_attributes / load_commands / load_statistics
            loaded = False
            for method_name in ("load_attributes", "load_commands", "load_statistics"):
                method = getattr(appliance, method_name, None)
                if method and callable(method):
                    try:
                        await method()
                        loaded = True
                        _LOGGER.debug("Fallback OK: %s", method_name)
                        _debug_appliance_consumption(f"dopo {method_name}", appliance)
                    except Exception as err:
                        _LOGGER.debug("Fallback %s fallito: %s", method_name, err)
                        raise RuntimeError(f"Fallback {method_name} fallito: {err}") from err

            if not loaded:
                if update_returned_empty:
                    raise RuntimeError(
                        "update() completato senza dati e fallback load_* non disponibili"
                    )
                raise RuntimeError(
                    "Nessun metodo di aggiornamento disponibile — "
                    "verifica la versione di pyhOn installata."
                )

        self._run_on_hon_loop(_do_update())

    # ── Re-auth ───────────────────────────────────────────────────────────────

    async def _async_reauth(self) -> bool:
        """Ri-autentica in caso di token scaduto."""
        _LOGGER.info("Tentativo re-autenticazione hOn...")
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._close_sync)
            await loop.run_in_executor(None, self.setup_sync)
            _LOGGER.info("Re-autenticazione hOn riuscita")
            return True
        except Exception as err:
            _LOGGER.error("Re-autenticazione hOn fallita: %s", err)
            return False

    # ── Polling dati ──────────────────────────────────────────────────────────

    async def async_get_appliances_data(self) -> dict[str, Any]:
        reauth_attempted = False

        while True:
            data: dict[str, Any] = {}
            retry_after_reauth = False
            try:
                appliances = await self.async_get_appliances()
            except Exception as err:
                if _requires_reauth(err) and not reauth_attempted:
                    _LOGGER.warning("Errore auth Haier recuperando dispositivi — avvio re-autenticazione")
                    if not await self._async_reauth():
                        raise RuntimeError(
                            f"Errore auth Haier durante recupero dispositivi: {err}"
                        ) from err
                    reauth_attempted = True
                    continue
                raise
            _LOGGER.debug("Trovati %d dispositivi hOn", len(appliances))

            for appliance in appliances:
                try:
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
                                    "Errore server Haier (tentativo %d/3), riprovo tra %ds: %s",
                                    attempt + 1, wait, err,
                                )
                                await asyncio.sleep(wait)
                            elif _requires_reauth(err):
                                break
                            else:
                                break

                    if last_err is not None:
                        raise last_err

                    appliance_id = (
                        getattr(appliance, "unique_id", None)
                        or _get_serial(appliance)
                        or str(id(appliance))
                    )
                    attributes = _get_attributes(appliance)
                    name = _get_name(appliance)
                    app_type = _get_type(appliance)

                    data[appliance_id] = {
                        "appliance": appliance,
                        "type": app_type,
                        "name": name,
                        "model": _get_model(appliance),
                        "serial": _get_serial(appliance),
                        "attributes": attributes,
                        "settings": dict(appliance.settings) if hasattr(appliance, "settings") else {},
                    }
                    _debug_appliance_consumption("snapshot coordinator", appliance, attributes)
                    _LOGGER.debug(
                        "Aggiornato '%s' (type=%s, id=%s) — %d attributi",
                        name, app_type, appliance_id, len(attributes),
                    )

                except Exception as err:
                    _LOGGER.warning(
                        "Errore aggiornamento '%s' (type=%s): %s",
                        _get_name(appliance), _get_type(appliance), err,
                        exc_info=True,
                    )
                    if _requires_reauth(err):
                        if reauth_attempted:
                            raise RuntimeError(
                                f"Errore auth Haier durante aggiornamento "
                                f"'{_get_name(appliance)}': {err}"
                            ) from err
                        _LOGGER.warning("Errore auth Haier — avvio re-autenticazione")
                        if not await self._async_reauth():
                            raise RuntimeError(
                                f"Errore auth Haier durante aggiornamento "
                                f"'{_get_name(appliance)}': {err}"
                            ) from err
                        reauth_attempted = True
                        retry_after_reauth = True
                        break
                    raise RuntimeError(
                        f"Errore aggiornamento '{_get_name(appliance)}': {err}"
                    ) from err

            if retry_after_reauth:
                continue

            _LOGGER.info("Caricati %d dispositivi hOn con dati", len(data))
            return data

    # ── Chiusura ──────────────────────────────────────────────────────────────

    async def async_close(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._close_sync)
