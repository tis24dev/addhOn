"""Climate entity per Haier hOn - condizionatore AS35PBPHRA-PRE."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity
from .const import (
    APPLIANCE_AC,
    DOMAIN,
    AC_MODE_MAP,
    AC_MODE_MAP_REVERSE,
    AC_FAN_MAP,
    AC_FAN_MAP_REVERSE,
    AC_ATTR_MODE,
    AC_ATTR_TEMP,
    AC_ATTR_ON_OFF,
    AC_ATTR_CURRENT_TEMP,
    AC_ATTR_OUTDOOR_TEMP,
    AC_ATTR_FAN_SPEED,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Configura l'entità climate basandosi sul coordinator."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities = [
        HaierClimateEntity(coordinator, aid, client)
        for aid, data in coordinator.data.items()
        if data.get("type") == APPLIANCE_AC
    ]
    async_add_entities(entities)


class HaierClimateEntity(HonBaseEntity, ClimateEntity):
    """Rappresentazione del condizionatore Haier hOn."""

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        device_name = self._appliance_data.get("name", "Condizionatore Haier")
        self._attr_name = device_name
        self._attr_unique_id = f"{appliance_id}_climate"
        self._attr_temperature_unit = "°C"
        self._attr_target_temperature_step = 1.0
        self._attr_min_temp = 16.0
        self._attr_max_temp = 30.0
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE 
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        # Forziamo gli Enum nativi di HA per la plancia di comando
        self._attr_hvac_modes = [
            HVACMode.OFF,
            HVACMode.AUTO,
            HVACMode.COOL,
            HVACMode.DRY,
            HVACMode.HEAT,
            HVACMode.FAN_ONLY,
        ]
        self._attr_fan_modes = list(AC_FAN_MAP_REVERSE.keys())

    @property
    def hvac_mode(self) -> HVACMode:
        """Ritorna lo stato HVAC corrente traducendo la stringa di const.py nell'Enum di HA."""
        on_off = self._get_attr(AC_ATTR_ON_OFF, "0")
        if str(on_off) == "0":
            return HVACMode.OFF
            
        # Legge machMode (es. "2") usando la costante da const.py
        mode_val = str(self._get_attr(AC_ATTR_MODE, "1"))
        
        # Recupera il testo dal tuo const.py (es. "cool")
        mode_str = AC_MODE_MAP.get(mode_val, "cool")
        
        # Converte la stringa nell'Enum corretto di Home Assistant
        try:
            return HVACMode(str(mode_str).lower())
        except ValueError:
            return HVACMode.COOL

    @property
    def target_temperature(self) -> float | None:
        """Ritorna la temperatura impostata. None se non disponibile."""
        val = self._get_attr(AC_ATTR_TEMP)
        try:
            return float(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    @property
    def current_temperature(self) -> float | None:
        """Ritorna la temperatura della stanza."""
        val = self._get_attr(AC_ATTR_CURRENT_TEMP)
        try:
            return float(val) if val is not None else None
        except (ValueError, TypeError):
            return None

    @property
    def fan_mode(self) -> str | None:
        """Ritorna la velocità della ventilazione basata sulla mappa invertita."""
        val = str(self._get_attr(AC_ATTR_FAN_SPEED, "0"))
        return AC_FAN_MAP.get(val, "auto")

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Invia il cambio modalità convertendo l'HVACMode nell'esatto codice numerico hOn."""
        appliance = self._appliance
        if not appliance:
            raise HomeAssistantError(
                f"Climate: appliance non disponibile per {self._appliance_id}"
            )
        try:
            client = self._hon_client
            if client is None:
                raise HomeAssistantError("Climate: HonClient non disponibile")

            if hvac_mode == HVACMode.OFF:
                await self._send_command_in_executor(client, appliance, {"onOffStatus": "0"})
            else:
                # HVACMode è StrEnum: .value torna direttamente la stringa ("cool", "heat", ecc.)
                mode_str = hvac_mode.value
                
                # Cerca il codice numerico in AC_MODE_MAP_REVERSE
                mode_key = AC_MODE_MAP_REVERSE.get(mode_str, "1")
                
                await self._send_command_in_executor(
                    client, appliance, {"onOffStatus": "1", "machMode": str(mode_key)}
                )
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error("Climate: errore set_hvac_mode: %s", err, exc_info=True)
            raise HomeAssistantError(f"Climate: errore set_hvac_mode: {err}") from err

    async def async_turn_on(self) -> None:
        """Accende il condizionatore portandolo in modalità COOL."""
        await self.async_set_hvac_mode(HVACMode.COOL)

    async def async_turn_off(self) -> None:
        """Spegne il condizionatore."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs) -> None:
        """Invia la temperatura target."""
        temp = kwargs.get("temperature")
        if temp is None:
            return
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError("Climate: appliance o client non disponibile")
        try:
            await self._send_command_in_executor(client, appliance, {"tempSel": str(int(temp))})
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error("Climate: errore set_temperature: %s", err, exc_info=True)
            raise HomeAssistantError(f"Climate: errore set_temperature: {err}") from err

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Invia la velocità ventola basandosi sulla mappa del tuo const.py."""
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError("Climate: appliance o client non disponibile")
        try:
            speed_key = AC_FAN_MAP_REVERSE.get(fan_mode, "0")
            await self._send_command_in_executor(client, appliance, {"windSpeed": speed_key})
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error("Climate: errore set_fan_mode: %s", err, exc_info=True)
            raise HomeAssistantError(f"Climate: errore set_fan_mode: {err}") from err

    async def _send_command_in_executor(self, client, appliance, params: dict) -> None:
        """Invia un comando settings tramite pyhOn sul loop dedicato (in executor)."""
        def _do_send():
            async def _inner():
                commands = appliance.commands if isinstance(appliance.commands, dict) else {}
                command = commands.get("settings")
                if command is None:
                    raise RuntimeError("Comando 'settings' non trovato sul dispositivo AC")
                command_params = getattr(command, "parameters", {})
                missing_params = [key for key in params if key not in command_params]
                if missing_params:
                    raise RuntimeError(
                        "Parametro/i non trovato/i nel comando settings: "
                        + ", ".join(missing_params)
                    )
                previous_values = {}
                assigned_params = []
                try:
                    for key, value in params.items():
                        previous_values[key] = command_params[key].value
                        assigned_params.append(key)
                        command_params[key].value = value
                except Exception:
                    for key in reversed(assigned_params):
                        try:
                            command_params[key].value = previous_values[key]
                        except Exception as rollback_err:
                            _LOGGER.warning(
                                "Climate: impossibile ripristinare parametro '%s': %s",
                                key, rollback_err,
                            )
                    raise
                await command.send()

            client.run_command_sync(_inner())

        await self.hass.async_add_executor_job(_do_send)
