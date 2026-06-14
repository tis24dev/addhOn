"""Entità base per Haier hOn."""
from __future__ import annotations

import logging

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .debug_utils import debug_key_sample

_LOGGER = logging.getLogger(__name__)


class HonBaseEntity(CoordinatorEntity):
    """Entità base per tutti i dispositivi Haier hOn."""

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._client = client if client is not None else getattr(coordinator, "hon_client", None)

    @property
    def _hon_client(self):
        """Ritorna il HonClient per eseguire comandi sul loop dedicato."""
        return self._client

    @property
    def _appliance_data(self) -> dict:
        return self.coordinator.data.get(self._appliance_id, {})

    @property
    def _attributes(self) -> dict:
        return self._appliance_data.get("attributes", {})

    @property
    def _statistics(self) -> dict:
        return self._appliance_data.get("statistics", {})

    @property
    def _appliance(self):
        return self._appliance_data.get("appliance")

    def _coordinator_store(self, name: str) -> dict:
        """Store volatile condiviso tra le entità, tenuto sul coordinator.

        A differenza di coordinator.data (ricreato a ogni refresh), un attributo
        sul coordinator sopravvive agli aggiornamenti, così entità diverse dello
        stesso device possono condividere stato effimero (es. il programma
        scelto dal select ma non ancora avviato, letto poi dal button "Avvia").
        """
        store = getattr(self.coordinator, name, None)
        if not isinstance(store, dict):
            store = {}
            setattr(self.coordinator, name, store)
            _LOGGER.debug(
                "BaseEntity debug: creato coordinator store '%s' per appliance=%s",
                name,
                self._appliance_id,
            )
        return store

    @property
    def device_info(self) -> DeviceInfo:
        data = self._appliance_data
        return DeviceInfo(
            identifiers={(DOMAIN, self._appliance_id)},
            name=data.get("name", "Haier Appliance"),
            manufacturer="Haier",
            model=data.get("model", "Unknown"),
            sw_version=None,
        )

    @property
    def available(self) -> bool:
        """Disponibilità per-appliance, oltre allo stato globale del coordinator.

        super().available (CoordinatorEntity) riflette solo l'esito complessivo
        dell'ultimo refresh (last_update_success): NON copre il caso in cui il
        refresh va a buon fine ma QUESTO appliance sparisce da coordinator.data
        (dispositivo rimosso dall'account o temporaneamente non restituito
        dall'API). Senza questo check l'entità resterebbe "available" mostrando
        valori di default stantii. Manteniamo l'AND con lo stato del coordinator
        e una guardia isinstance perché `x in None` solleverebbe TypeError.
        """
        return (
            super().available
            and isinstance(self.coordinator.data, dict)
            and self._appliance_id in self.coordinator.data
        )

    def _get_attr(self, key: str, default=None):
        """Recupera un attributo del dispositivo.
        
        pyhOn restituisce gli attributi come HonAttribute (con .value)
        oppure come valori raw a seconda della versione. Gestiamo entrambi.
        """
        def _extract_value(value):
            if value is None:
                return None
            # HonAttribute ha .value — nota: value.value può essere 0, "", False (tutti validi!)
            if hasattr(value, "value"):
                inner = value.value
                # Stringa vuota = dato non disponibile, trattala come None
                if inner == "":
                    return None
                return inner
            # Stringa vuota raw = dato non disponibile
            if value == "":
                return None
            return value

        def _deep_get(container, path: str):
            current = container
            for part in path.split("."):
                if current is None:
                    return None
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    current = getattr(current, part, None)
            return current

        def _debug_lookup(source: str, raw_value, extracted_value) -> None:
            if not _LOGGER.isEnabledFor(logging.DEBUG):
                return
            attributes = self._attributes
            settings = self._appliance_data.get("settings")
            _LOGGER.debug(
                "BaseEntity debug: lookup '%s' per '%s' (id=%s) risolto da %s: "
                "raw=%r (%s), value=%r; attribute_keys=%d %s; settings_keys=%d %s",
                key,
                getattr(self, "_attr_name", self.__class__.__name__),
                self._appliance_id,
                source,
                raw_value,
                type(raw_value).__name__,
                extracted_value,
                len(attributes) if isinstance(attributes, dict) else 0,
                debug_key_sample(attributes) if isinstance(attributes, dict) else [],
                len(settings) if isinstance(settings, dict) else 0,
                debug_key_sample(settings) if isinstance(settings, dict) else [],
            )

        # 1) lookup diretto (chiavi già "flattened")
        val = self._attributes.get(key)
        if val is not None:
            extracted = _extract_value(val)
            _debug_lookup("attributes diretto", val, extracted)
            return extracted

        # 1b) lookup nel container statistics separato (es. TD programsCounter).
        # Normalmente hon_client lo fonde gia' in attributes, ma questo fallback
        # evita che un payload separato nel coordinator renda il sensore vuoto.
        statistics = self._statistics
        if isinstance(statistics, dict):
            val = statistics.get(key)
            if val is not None:
                extracted = _extract_value(val)
                _debug_lookup("statistics diretto", val, extracted)
                return extracted

            val = _deep_get(statistics, key)
            if val is not None:
                extracted = _extract_value(val)
                _debug_lookup("statistics dotted path", val, extracted)
                return extracted

        # 2) supporto prefisso "settings." (alcuni modelli/vecchie versioni lo usano)
        if key.startswith("settings."):
            key_no_prefix = key.removeprefix("settings.")
            val = self._attributes.get(key_no_prefix)
            if val is not None:
                extracted = _extract_value(val)
                _debug_lookup("attributes senza prefisso settings", val, extracted)
                return extracted

            val = _deep_get(self._attributes, key_no_prefix)
            if val is not None:
                extracted = _extract_value(val)
                _debug_lookup("attributes deep senza prefisso settings", val, extracted)
                return extracted

            settings = self._appliance_data.get("settings")
            if isinstance(settings, dict):
                val = settings.get(key_no_prefix)
                if val is not None:
                    extracted = _extract_value(val)
                    _debug_lookup("settings diretto", val, extracted)
                    return extracted
                val = _deep_get(settings, key_no_prefix)
                if val is not None:
                    extracted = _extract_value(val)
                    _debug_lookup("settings deep", val, extracted)
                    return extracted

        # 2b) supporto prefisso "startProgram." (es. ecoMode che vive in startProgram)
        if key.startswith("startProgram."):
            key_no_prefix = key.removeprefix("startProgram.")
            val = self._attributes.get(key_no_prefix)
            if val is not None:
                extracted = _extract_value(val)
                _debug_lookup("attributes senza prefisso startProgram", val, extracted)
                return extracted

            start_program = self._appliance_data.get("startProgram")
            if isinstance(start_program, dict):
                val = start_program.get(key_no_prefix)
                if val is not None:
                    extracted = _extract_value(val)
                    _debug_lookup("startProgram diretto", val, extracted)
                    return extracted
                val = _deep_get(start_program, key_no_prefix)
                if val is not None:
                    extracted = _extract_value(val)
                    _debug_lookup("startProgram deep", val, extracted)
                    return extracted

        # 3) fallback: prova lookup "dotted path" dentro attributes
        val = _deep_get(self._attributes, key)
        if val is not None:
            extracted = _extract_value(val)
            _debug_lookup("attributes dotted path", val, extracted)
            return extracted

        if _LOGGER.isEnabledFor(logging.DEBUG):
            attributes = self._attributes
            settings = self._appliance_data.get("settings")
            _LOGGER.debug(
                "BaseEntity debug: lookup '%s' per '%s' (id=%s) non trovato, "
                "ritorno default=%r; attribute_keys=%d %s; settings_keys=%d %s",
                key,
                getattr(self, "_attr_name", self.__class__.__name__),
                self._appliance_id,
                default,
                len(attributes) if isinstance(attributes, dict) else 0,
                debug_key_sample(attributes) if isinstance(attributes, dict) else [],
                len(settings) if isinstance(settings, dict) else 0,
                debug_key_sample(settings) if isinstance(settings, dict) else [],
            )
        return default

    async def _async_request_command_refresh(self) -> None:
        """Refresh coordinator data after a command and fail if HA stored the error."""
        refresh = getattr(self.coordinator, "async_refresh", None)
        if refresh is None:
            refresh = self.coordinator.async_request_refresh
        _LOGGER.debug(
            "BaseEntity debug: refresh richiesto dopo comando per appliance=%s entity=%s",
            self._appliance_id,
            getattr(self, "_attr_name", self.__class__.__name__),
        )
        await refresh()
        if getattr(self.coordinator, "last_update_success", True) is not False:
            _LOGGER.debug(
                "BaseEntity debug: refresh dopo comando riuscito per appliance=%s entity=%s",
                self._appliance_id,
                getattr(self, "_attr_name", self.__class__.__name__),
            )
            return

        err = getattr(self.coordinator, "last_exception", None)
        if err is None:
            raise HomeAssistantError("Refresh dopo comando fallito")
        raise HomeAssistantError(f"Refresh dopo comando fallito: {err}") from err
