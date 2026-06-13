"""Button per azioni esplicite Haier hOn."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity
from .const import APPLIANCE_WASH_GROUP, DOMAIN, PROGRAM_PARAM_NAMES

_LOGGER = logging.getLogger(__name__)

# Deve combaciare con select.PENDING_STORE: programma scelto dal select ma non
# ancora avviato. Lo applichiamo a startProgram al momento dell'avvio.
PENDING_STORE = "pending_programs"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configura i button per azioni fisiche esplicite."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities = []
    for appliance_id, data in coordinator.data.items():
        if data.get("type") not in APPLIANCE_WASH_GROUP:
            continue
        appliance = data.get("appliance")
        commands = appliance.commands if appliance and isinstance(appliance.commands, dict) else {}
        if "startProgram" in commands:
            entities.append(
                HonProgramCommandButton(
                    coordinator,
                    appliance_id,
                    client,
                    command_name="startProgram",
                    unique_suffix="start_program",
                    name_suffix="Avvia programma",
                    icon="mdi:play-circle",
                )
            )
        if "stopProgram" in commands:
            entities.append(
                HonProgramCommandButton(
                    coordinator,
                    appliance_id,
                    client,
                    command_name="stopProgram",
                    unique_suffix="stop_program",
                    name_suffix="Ferma programma",
                    icon="mdi:stop-circle",
                    command_parameters={"onOffStatus": "0"},
                )
            )
    async_add_entities(entities)


class HonProgramCommandButton(HonBaseEntity, ButtonEntity):
    """Button per inviare comandi start/stop chiaramente espliciti."""

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        client=None,
        *,
        command_name: str,
        unique_suffix: str,
        name_suffix: str,
        icon: str,
        command_parameters: dict[str, str] | None = None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        device_name = self._appliance_data.get("name", "Lavatrice")
        self._command_name = command_name
        self._command_parameters = command_parameters or {}
        self._attr_unique_id = f"{appliance_id}_{unique_suffix}"
        self._attr_name = f"{device_name} - {name_suffix}"
        self._attr_icon = icon

    async def async_press(self) -> None:
        """Invia il comando fisico esplicito."""
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError("Button: appliance o client non disponibile")

        # Avvio: applichiamo il programma scelto dal select (se presente).
        # Lo leggiamo qui sull'event loop di HA e lo passiamo dentro _inner.
        store = self._coordinator_store(PENDING_STORE)
        pending_program = (
            store.get(self._appliance_id)
            if self._command_name == "startProgram"
            else None
        )
        try:
            def _do():
                async def _inner():
                    commands = appliance.commands if isinstance(appliance.commands, dict) else {}
                    command = commands.get(self._command_name)
                    if not command:
                        raise RuntimeError(
                            f"Comando '{self._command_name}' non trovato. "
                            f"Disponibili: {list(commands.keys())}"
                        )
                    params = getattr(command, "parameters", {})
                    if pending_program is not None and isinstance(params, dict):
                        for pname in PROGRAM_PARAM_NAMES:
                            if pname in params:
                                params[pname].value = pending_program
                                break
                    for name, value in self._command_parameters.items():
                        if name in params:
                            params[name].value = value
                    await command.send()

                client.run_command_sync(_inner())

            await self.hass.async_add_executor_job(_do)
            # Avvio riuscito: il programma è ora "reale", svuotiamo la scelta in
            # attesa così il select torna a riflettere lo stato del device.
            if pending_program is not None:
                store.pop(self._appliance_id, None)
            _LOGGER.info("Button: comando '%s' inviato", self._command_name)
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error(
                "Button %s: errore comando: %s",
                self._command_name, err, exc_info=True,
            )
            raise HomeAssistantError(
                f"Button {self._command_name}: errore comando: {err}"
            ) from err
