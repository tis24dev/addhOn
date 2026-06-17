"""Switch per Haier hOn: pausa lavatrice/asciugatrice + toggle del condizionatore."""
from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ac_command import async_send_settings, settings_param
from .base_entity import HonBaseEntity
from .const import APPLIANCE_AC, APPLIANCE_WASH_GROUP, DOMAIN, WM_ATTR_STATUS

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HonAcSwitchDescription:
    """Switch booleano dell'AC che agisce su un parametro 0/1 del comando settings.

    `param` è sia il nome del parametro nel comando `settings` (scrittura) sia
    l'attributo diretto 0/1 letto via _get_attr (lettura).
    """

    key: str            # suffisso unique_id
    name: str
    param: str
    icon: str | None = None


# Switch AC: parametri 0/1 confermati nel comando settings dell'AC di Roberto.
# Capability-gated: ciascuno è creato solo se il device espone davvero il parametro.
_AC_SWITCHES: tuple[HonAcSwitchDescription, ...] = (
    HonAcSwitchDescription(key="sleep", name="Modalità Notte", param="silentSleepStatus", icon="mdi:power-sleep"),
    HonAcSwitchDescription(key="mute", name="Muto", param="muteStatus", icon="mdi:volume-off"),
    HonAcSwitchDescription(key="eco", name="Eco", param="echoStatus", icon="mdi:leaf"),
    HonAcSwitchDescription(key="rapid", name="Rapido", param="rapidMode", icon="mdi:fan-plus"),
    HonAcSwitchDescription(key="health", name="Health", param="healthMode", icon="mdi:heart-pulse"),
    HonAcSwitchDescription(key="self_clean", name="Autopulizia", param="selfCleaningStatus", icon="mdi:spray-bottle"),
    HonAcSwitchDescription(key="self_clean_56", name="Autopulizia 56°C", param="selfCleaning56Status", icon="mdi:spray"),
    HonAcSwitchDescription(key="display", name="Display", param="screenDisplayStatus", icon="mdi:monitor"),
    HonAcSwitchDescription(key="light", name="Luce", param="lightStatus", icon="mdi:lightbulb"),
    HonAcSwitchDescription(key="ten_degree_heating", name="Riscaldamento 10°C", param="10degreeHeatingStatus", icon="mdi:snowflake-melt"),
    HonAcSwitchDescription(key="child_lock", name="Blocco Bambini", param="lockStatus", icon="mdi:lock"),
    HonAcSwitchDescription(key="human_sensing", name="Sensore Presenza", param="humanSensingStatus", icon="mdi:motion-sensor"),
    HonAcSwitchDescription(key="electric_heating", name="Riscaldamento Elettrico", param="electricHeatingStatus", icon="mdi:radiator"),
    HonAcSwitchDescription(key="fresh_air", name="Aria Fresca", param="freshAirStatus", icon="mdi:air-filter"),
    HonAcSwitchDescription(key="half_degree", name="Mezzo Grado", param="halfDegreeSettingStatus", icon="mdi:thermometer-lines"),
    HonAcSwitchDescription(key="energy_saving", name="Risparmio Energetico", param="energySavingStatus", icon="mdi:meter-electric"),
)


def _command_names(appliance) -> list[str]:
    commands = getattr(appliance, "commands", None)
    return sorted(commands.keys()) if isinstance(commands, dict) else []


def _param_snapshot(params) -> dict:
    if not isinstance(params, dict):
        return {"<non-dict>": type(params).__name__}
    return {
        str(name): getattr(param, "value", None)
        for name, param in params.items()
    }


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
        app_type = data.get("type")
        appliance = data.get("appliance")
        _LOGGER.debug(
            "Switch debug: valuto appliance '%s' id=%s type=%s commands=%s",
            data.get("name"),
            appliance_id,
            app_type,
            _command_names(appliance),
        )
        if app_type in APPLIANCE_WASH_GROUP:
            if appliance and hasattr(appliance, "commands"):
                cmds = getattr(appliance, "commands", None)
                cmds = cmds if isinstance(cmds, dict) else {}
                if "pauseProgram" in cmds and "resumeProgram" in cmds:
                    _LOGGER.debug("Switch debug: creo switch pausa per id=%s", appliance_id)
                    entities.append(HonWashingMachinePauseSwitch(coordinator, appliance_id, client))
                    _LOGGER.info("Aggiunto switch: %s", data.get("name"))
                else:
                    _LOGGER.debug(
                        "Switch debug: switch pausa non creato per id=%s; pause/resume mancanti",
                        appliance_id,
                    )
        elif app_type == APPLIANCE_AC:
            created: list[str] = []
            for desc in _AC_SWITCHES:
                # capability-gate: solo se il parametro esiste nel comando settings
                if settings_param(appliance, desc.param) is None:
                    continue
                entities.append(HonAcSwitch(coordinator, appliance_id, desc, client))
                created.append(desc.key)
            _LOGGER.debug(
                "Switch debug: AC '%s' id=%s -> %d switch %s",
                data.get("name"), appliance_id, len(created), created,
            )
        else:
            _LOGGER.debug("Switch debug: appliance id=%s ignorato, type=%s", appliance_id, app_type)
    async_add_entities(entities)

class HonWashingMachinePauseSwitch(HonBaseEntity, SwitchEntity):
    """Switch per mettere in pausa / riprendere il programma lavatrice."""

    _attr_icon = "mdi:pause-circle"

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        device_name = self._appliance_data.get("name", "Lavatrice")
        self._attr_unique_id = f"{appliance_id}_pause"
        self._attr_name = f"{device_name} - Pausa"
        _LOGGER.debug("Switch debug: inizializzato '%s' id=%s", self._attr_name, appliance_id)

    @property
    def is_on(self) -> bool:
        val = self._get_attr(WM_ATTR_STATUS, "0")
        is_paused = str(val) == "2"
        _LOGGER.debug(
            "Switch debug: is_on '%s' id=%s machMode=%s -> %s",
            self._attr_name,
            self._appliance_id,
            val,
            is_paused,
        )
        return is_paused

    async def _send_pause_command(self, command_name: str, pause_value: str) -> None:
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError("Pausa: appliance o client non disponibile")
        _LOGGER.debug(
            "Switch debug: invio comando pausa '%s' value=%s id=%s commands=%s",
            command_name,
            pause_value,
            self._appliance_id,
            _command_names(appliance),
        )
        try:
            def _do():
                async def _inner():
                    commands = getattr(appliance, "commands", None)
                    commands = commands if isinstance(commands, dict) else {}
                    command = commands.get(command_name)
                    if not command:
                        raise RuntimeError(f"Comando '{command_name}' non trovato")
                    params = getattr(command, "parameters", {})
                    _LOGGER.debug(
                        "Switch debug: command '%s' params prima=%s",
                        command_name,
                        _param_snapshot(params),
                    )
                    if isinstance(params, dict) and "pause" in params:
                        previous = getattr(params["pause"], "value", None)
                        params["pause"].value = pause_value
                        _LOGGER.debug(
                            "Switch debug: parametro pause impostato a %s (previous=%s)",
                            pause_value,
                            previous,
                        )
                    else:
                        _LOGGER.debug(
                            "Switch debug: command '%s' senza parametro pause; invio senza modifica",
                            command_name,
                        )
                    await command.send()
                    _LOGGER.debug("Switch debug: command '%s' send completato", command_name)
                client.run_command_sync(_inner())

            await self.hass.async_add_executor_job(_do)
            _LOGGER.info("Pausa: %s inviato", command_name)
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error("Pausa %s: Errore: %s", command_name, err, exc_info=True)
            raise HomeAssistantError(f"Pausa {command_name}: errore comando: {err}") from err

    async def async_turn_on(self, **kwargs) -> None:
        await self._send_pause_command("pauseProgram", "1")

    async def async_turn_off(self, **kwargs) -> None:
        await self._send_pause_command("resumeProgram", "0")


class HonAcSwitch(HonBaseEntity, SwitchEntity):
    """Switch booleano del condizionatore su un parametro del comando settings."""

    def __init__(self, coordinator, appliance_id: str, description: HonAcSwitchDescription, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._desc = description
        device_name = self._appliance_data.get("name", "Condizionatore")
        self._attr_name = f"{device_name} - {description.name}"
        self._attr_unique_id = f"{appliance_id}_{description.key}"
        if description.icon:
            self._attr_icon = description.icon
        _LOGGER.debug(
            "Switch debug: inizializzato AC switch '%s' id=%s param=%s",
            self._attr_name, appliance_id, description.param,
        )

    @property
    def is_on(self) -> bool | None:
        raw = self._get_attr(self._desc.param)
        if raw is None:
            return None
        return str(raw) == "1"

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_param("1")

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_param("0")

    async def _set_param(self, value: str) -> None:
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError("Switch AC: appliance o client non disponibile")
        param = self._desc.param
        try:
            _LOGGER.debug("Switch debug: AC set %s=%s id=%s", param, value, self._appliance_id)
            await async_send_settings(self.hass, client, appliance, {param: value})
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Switch AC: errore set %s=%s: %s", param, value, err, exc_info=True)
            raise HomeAssistantError(f"Switch AC: errore comando {param}: {err}") from err
