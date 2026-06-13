"""Entità base per Haier hOn."""
from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


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

        # 1) lookup diretto (chiavi già "flattened")
        val = self._attributes.get(key)
        if val is not None:
            return _extract_value(val)

        # 2) supporto prefisso "settings." (alcuni modelli/vecchie versioni lo usano)
        if key.startswith("settings."):
            key_no_prefix = key.removeprefix("settings.")
            val = self._attributes.get(key_no_prefix)
            if val is not None:
                return _extract_value(val)

            val = _deep_get(self._attributes, key_no_prefix)
            if val is not None:
                return _extract_value(val)

            settings = self._appliance_data.get("settings")
            if isinstance(settings, dict):
                val = settings.get(key_no_prefix)
                if val is not None:
                    return _extract_value(val)
                val = _deep_get(settings, key_no_prefix)
                if val is not None:
                    return _extract_value(val)

        # 2b) supporto prefisso "startProgram." (es. ecoMode che vive in startProgram)
        if key.startswith("startProgram."):
            key_no_prefix = key.removeprefix("startProgram.")
            val = self._attributes.get(key_no_prefix)
            if val is not None:
                return _extract_value(val)

            start_program = self._appliance_data.get("startProgram")
            if isinstance(start_program, dict):
                val = start_program.get(key_no_prefix)
                if val is not None:
                    return _extract_value(val)
                val = _deep_get(start_program, key_no_prefix)
                if val is not None:
                    return _extract_value(val)

        # 3) fallback: prova lookup "dotted path" dentro attributes
        val = _deep_get(self._attributes, key)
        if val is not None:
            return _extract_value(val)

        return default

    async def _async_request_command_refresh(self) -> None:
        """Refresh coordinator data after a command and fail if HA stored the error."""
        refresh = getattr(self.coordinator, "async_refresh", None)
        if refresh is None:
            refresh = self.coordinator.async_request_refresh
        await refresh()
        if getattr(self.coordinator, "last_update_success", True) is not False:
            return

        err = getattr(self.coordinator, "last_exception", None)
        if err is None:
            raise HomeAssistantError("Refresh dopo comando fallito")
        raise HomeAssistantError(f"Refresh dopo comando fallito: {err}") from err
