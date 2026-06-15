"""Select per Haier hOn - selezione programma lavatrice."""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity
from .const import (
    APPLIANCE_WASH_GROUP,
    DOMAIN,
    PROGRAM_PARAM_NAMES,
    PROGRAM_PENDING_STORE,
)

_LOGGER = logging.getLogger(__name__)

# Comandi "safe" (non avviano un ciclo) da cui leggere/scrivere il programma.
PROGRAM_SELECT_COMMANDS = ("settings", "setProgram", "setProgramme", "programSettings")
# Comandi da cui ATTINGERE l'elenco programmi. Includiamo anche startProgram
# come sorgente di metadati: la selezione è disaccoppiata dall'avvio (vedi
# async_select_option), quindi leggere le opzioni da startProgram NON fa partire
# l'elettrodomestico. Senza questo, le lavatrici/asciugatrici che espongono il
# programma solo via startProgram restavano senza select (entità orfana
# "unavailable").
PROGRAM_SOURCE_COMMANDS = PROGRAM_SELECT_COMMANDS + ("startProgram",)


def _command_names(appliance) -> list[str]:
    commands = getattr(appliance, "commands", None)
    return sorted(commands.keys()) if isinstance(commands, dict) else []


def _param_names(command) -> list[str]:
    params = getattr(command, "parameters", None)
    return sorted(params.keys()) if isinstance(params, dict) else []


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # FIX: accesso coerente alla struttura hass.data[DOMAIN][entry_id]["coordinator"]
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities = []
    for appliance_id, data in coordinator.data.items():
        appliance = data.get("appliance")
        app_type = data.get("type")
        _LOGGER.debug(
            "Select debug: valuto appliance '%s' id=%s type=%s commands=%s",
            data.get("name"),
            appliance_id,
            app_type,
            _command_names(appliance),
        )
        if app_type not in APPLIANCE_WASH_GROUP:
            _LOGGER.debug("Select debug: appliance id=%s ignorato, type=%s", appliance_id, app_type)
            continue
        if HonProgramSelect.supports_appliance(appliance):
            entities.append(HonProgramSelect(coordinator, appliance_id, client))
            _LOGGER.info("Aggiunto select programma: %s", data.get("name"))
        else:
            _LOGGER.debug(
                "Select debug: nessun select programma per '%s' id=%s; "
                "nessun comando sorgente con parametri %s",
                data.get("name"),
                appliance_id,
                PROGRAM_PARAM_NAMES,
            )
    async_add_entities(entities)


class HonProgramSelect(HonBaseEntity, SelectEntity):
    """Select per la selezione del programma lavatrice/asciugatrice."""

    _attr_icon = "mdi:format-list-bulleted"

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        device_name = self._appliance_data.get("name", "Lavatrice")
        self._attr_unique_id = f"{appliance_id}_program"
        self._attr_name = f"{device_name} - Programma"

        self._program_map: dict[str, str] = {}
        appliance = self._appliance
        if appliance is not None:
            self._program_map = self._load_programs(appliance)

        self._program_reverse: dict[str, str] = {v: k for k, v in self._program_map.items()}
        self._attr_options = list(self._program_reverse.keys())
        _LOGGER.debug(
            "Select debug: inizializzato '%s' id=%s programmi=%d map=%s",
            self._attr_name,
            appliance_id,
            len(self._program_map),
            self._program_map,
        )

    @classmethod
    def supports_appliance(cls, appliance) -> bool:
        """True se esiste un comando (incluso startProgram) con un parametro
        programma valorizzato da cui costruire l'elenco delle opzioni."""
        command_info = cls._find_program_command(appliance)
        if command_info is None:
            _LOGGER.debug(
                "Select debug: supports_appliance=False, nessun comando programma. commands=%s",
                _command_names(appliance),
            )
            return False
        _, command, param_name = command_info
        values = cls._program_values(command, param_name)
        _LOGGER.debug(
            "Select debug: supports_appliance command param=%s values_count=%d params=%s",
            param_name,
            len(values),
            _param_names(command),
        )
        return bool(values)

    @staticmethod
    def _find_program_command(appliance):
        if appliance is None:
            return None
        commands = getattr(appliance, "commands", None)
        commands = commands if isinstance(commands, dict) else {}
        for command_name in PROGRAM_SOURCE_COMMANDS:
            command = commands.get(command_name)
            if command is None:
                _LOGGER.debug("Select debug: comando sorgente '%s' assente", command_name)
                continue
            params = getattr(command, "parameters", None)
            if not isinstance(params, dict):
                _LOGGER.debug(
                    "Select debug: comando sorgente '%s' senza parameters dict: %s",
                    command_name,
                    type(params).__name__,
                )
                continue
            for param_name in PROGRAM_PARAM_NAMES:
                if param_name in params:
                    _LOGGER.debug(
                        "Select debug: trovato comando programma '%s' parametro '%s' params=%s",
                        command_name,
                        param_name,
                        sorted(params.keys()),
                    )
                    return command_name, command, param_name
        return None

    @staticmethod
    def _program_values(command, param_name: str) -> dict[str, str]:
        params = getattr(command, "parameters", {})
        prog_param = params.get(param_name) if isinstance(params, dict) else None
        if prog_param is None:
            _LOGGER.debug("Select debug: parametro programma '%s' assente", param_name)
            return {}
        for attr in ("values", "value_list", "options"):
            raw = getattr(prog_param, attr, None)
            if isinstance(raw, dict):
                values = {str(code): str(label) for code, label in raw.items()}
                _LOGGER.debug(
                    "Select debug: valori programmi da attr '%s' dict count=%d values=%s",
                    attr,
                    len(values),
                    values,
                )
                return values
            if isinstance(raw, (list, tuple)):
                values = {str(value): str(value) for value in raw}
                _LOGGER.debug(
                    "Select debug: valori programmi da attr '%s' list count=%d values=%s",
                    attr,
                    len(values),
                    values,
                )
                return values
        _LOGGER.debug("Select debug: nessun values/value_list/options per parametro '%s'", param_name)
        return {}

    @staticmethod
    def _load_programs(appliance) -> dict[str, str]:
        try:
            command_info = HonProgramSelect._find_program_command(appliance)
            if command_info is None:
                _LOGGER.debug("Select debug: _load_programs senza command_info")
                return {}
            command_name, command, param_name = command_info
            values = HonProgramSelect._program_values(command, param_name)
            if not values:
                _LOGGER.debug(
                    "Select debug: _load_programs comando '%s' parametro '%s' senza valori",
                    command_name,
                    param_name,
                )
                return {}
            programs = {
                str(code): str(label) if label else str(code)
                for code, label in values.items()
            }
            _LOGGER.debug(
                "Select debug: _load_programs da comando '%s' parametro '%s': %s",
                command_name,
                param_name,
                programs,
            )
            return programs
        except Exception as err:
            _LOGGER.debug("Errore caricamento programmi dinamici: %s", err)
            return {}

    @property
    def current_option(self) -> str | None:
        # 1) Scelta in attesa di avvio ("imposta e basta"): la mostriamo subito,
        #    finché l'utente non avvia il ciclo col pulsante "Avvia programma".
        pending = self._coordinator_store(PROGRAM_PENDING_STORE).get(self._appliance_id)
        if pending is not None:
            label = self._program_map.get(str(pending))
            if label is not None:
                _LOGGER.debug(
                    "Select debug: current_option usa pending id=%s code=%s label=%s",
                    self._appliance_id,
                    pending,
                    label,
                )
                return label
            _LOGGER.debug(
                "Select debug: current_option pending id=%s code=%s non presente in map=%s",
                self._appliance_id,
                pending,
                self._program_map,
            )

        # 2) Stato reale dal device. Proviamo sia il nome programma sia il codice
        #    (prCode/program) e usiamo il primo che corrisponde a un'opzione nota.
        #    FIX: controllare is not None invece di 'or' che scarterebbe lo 0.
        #    Ordine: prima le chiavi che espongono il NOME del programma (mappabile
        #    quando l'elenco opzioni è costruito da una lista di nomi, come sui
        #    modelli reali), poi i codici numerici prCode. Così, quando il device
        #    pubblica un prCode numerico non presente nella mappa per-nome, la
        #    risoluzione avviene direttamente sul nome senza generare rumore DEBUG
        #    "non mappato" per un codice che non sarebbe comunque mappabile.
        for key in (
            "programName",
            "settings.program",
            "startProgram.program",
            "program",
            "settings.prCode",
            "startProgram.prCode",
            "prCode",
        ):
            val = self._get_attr(key)
            if val is None:
                _LOGGER.debug("Select debug: current_option key '%s' assente", key)
                continue
            token = str(val)
            # token può essere un codice (chiave della mappa) oppure già
            # un'etichetta (es. programName espone il nome del programma).
            if token in self._program_map:
                _LOGGER.debug(
                    "Select debug: current_option key '%s' token codice=%s label=%s",
                    key,
                    token,
                    self._program_map[token],
                )
                return self._program_map[token]
            if token in self._program_reverse:
                _LOGGER.debug(
                    "Select debug: current_option key '%s' token label=%s",
                    key,
                    token,
                )
                return token
            _LOGGER.debug(
                "Select debug: current_option key '%s' token=%s non mappato; map=%s",
                key,
                token,
                self._program_map,
            )
        _LOGGER.debug("Select debug: current_option non disponibile per id=%s", self._appliance_id)
        return None

    async def async_select_option(self, option: str) -> None:
        code = self._program_reverse.get(option)
        if code is None:
            raise HomeAssistantError(f"Select: programma '{option}' non trovato nella mappa")
        # "Imposta e basta": memorizziamo la scelta SENZA inviare alcun comando.
        # Selezionare un programma non deve mai far partire l'elettrodomestico;
        # l'avvio avviene col pulsante "Avvia programma", che legge questo
        # programma in attesa e lo applica al comando startProgram.
        store = self._coordinator_store(PROGRAM_PENDING_STORE)
        _LOGGER.debug(
            "Select debug: prima selezione option=%s code=%s store=%s",
            option,
            code,
            dict(store),
        )
        store[self._appliance_id] = code
        _LOGGER.info(
            "Select: programma '%s' (code=%s) impostato; avvialo con 'Avvia programma'",
            option, code,
        )
        _LOGGER.debug("Select debug: dopo selezione store=%s", dict(store))
        self.async_write_ha_state()
