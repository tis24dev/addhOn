"""Climate entity per Haier hOn - condizionatore AS35PBPHRA-PRE."""
from __future__ import annotations

import logging

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
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
    AC_ATTR_SWING_V,
    AC_SWING_V_PARAM,
    AC_SWING_V_ON,
    AC_SWING_MODE_ON,
    AC_SWING_MODE_OFF,
)
from .debug_utils import command_names
from .ac_command import (
    async_send_settings,
    fixed_vertical_value,
    param_allowed_values,
    settings_param,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Configura l'entità climate basandosi sul coordinator."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities = []
    for aid, data in coordinator.data.items():
        appliance = data.get("appliance")
        _LOGGER.debug(
            "Climate debug: valuto appliance '%s' id=%s type=%s commands=%s attributes=%d",
            data.get("name"),
            aid,
            data.get("type"),
            command_names(appliance),
            len(data.get("attributes", {})) if isinstance(data.get("attributes"), dict) else 0,
        )
        if data.get("type") == APPLIANCE_AC:
            entities.append(HaierClimateEntity(coordinator, aid, client))
            _LOGGER.debug("Climate debug: creata entity climate per id=%s", aid)
    async_add_entities(entities)


class HaierClimateEntity(HonBaseEntity, ClimateEntity):
    """Rappresentazione del condizionatore Haier hOn."""

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        device_name = self._appliance_data.get("name", "Condizionatore Haier")
        self._attr_name = device_name
        self._attr_unique_id = f"{appliance_id}_climate"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_target_temperature_step = 1.0
        self._attr_min_temp = 16.0
        self._attr_max_temp = 30.0
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        # Swing: esposto SOLO se il device ha davvero windDirectionVertical tra i
        # parametri del comando settings (capability-gate). Evita di offrire un
        # controllo che il modello non supporta.
        swing_param = settings_param(self._appliance, AC_SWING_V_PARAM)
        self._swing_supported = swing_param is not None
        if self._swing_supported:
            self._attr_supported_features |= ClimateEntityFeature.SWING_MODE
            self._attr_swing_modes = [AC_SWING_MODE_OFF, AC_SWING_MODE_ON]
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
        _LOGGER.debug(
            "Climate debug: inizializzato '%s' id=%s hvac_modes=%s fan_modes=%s temp_range=%s-%s",
            self._attr_name,
            appliance_id,
            self._attr_hvac_modes,
            self._attr_fan_modes,
            self._attr_min_temp,
            self._attr_max_temp,
        )

    @property
    def hvac_mode(self) -> HVACMode:
        """Ritorna lo stato HVAC corrente traducendo la stringa di const.py nell'Enum di HA."""
        on_off = self._get_attr(AC_ATTR_ON_OFF, "0")
        if str(on_off) == "0":
            _LOGGER.debug(
                "Climate debug: hvac_mode '%s' id=%s onOffStatus=%s -> OFF",
                self._attr_name,
                self._appliance_id,
                on_off,
            )
            return HVACMode.OFF
            
        # Legge machMode (es. "2") usando la costante da const.py
        mode_val = str(self._get_attr(AC_ATTR_MODE, "1"))
        
        # Recupera il testo dal tuo const.py (es. "cool")
        mode_str = AC_MODE_MAP.get(mode_val, "cool")
        
        # Converte la stringa nell'Enum corretto di Home Assistant
        try:
            mode = HVACMode(str(mode_str).lower())
            _LOGGER.debug(
                "Climate debug: hvac_mode '%s' id=%s onOffStatus=%s machMode=%s -> %s",
                self._attr_name,
                self._appliance_id,
                on_off,
                mode_val,
                mode,
            )
            return mode
        except ValueError:
            _LOGGER.debug(
                "Climate debug: machMode=%s tradotto in mode_str=%s non valido, fallback COOL",
                mode_val,
                mode_str,
            )
            return HVACMode.COOL

    @property
    def target_temperature(self) -> float | None:
        """Ritorna la temperatura impostata. None se non disponibile."""
        val = self._get_attr(AC_ATTR_TEMP)
        try:
            result = float(val) if val is not None else None
            _LOGGER.debug("Climate debug: target_temperature raw=%r -> %s", val, result)
            return result
        except (ValueError, TypeError):
            _LOGGER.debug("Climate debug: target_temperature non numerica raw=%r", val)
            return None

    @property
    def current_temperature(self) -> float | None:
        """Ritorna la temperatura della stanza."""
        val = self._get_attr(AC_ATTR_CURRENT_TEMP)
        try:
            result = float(val) if val is not None else None
            _LOGGER.debug("Climate debug: current_temperature raw=%r -> %s", val, result)
            return result
        except (ValueError, TypeError):
            _LOGGER.debug("Climate debug: current_temperature non numerica raw=%r", val)
            return None

    @property
    def fan_mode(self) -> str | None:
        """Ritorna la velocità della ventilazione basata sulla mappa invertita."""
        val = str(self._get_attr(AC_ATTR_FAN_SPEED, "0"))
        fan = AC_FAN_MAP.get(val, "auto")
        _LOGGER.debug("Climate debug: fan_mode raw=%s -> %s", val, fan)
        return fan

    @property
    def swing_mode(self) -> str | None:
        """Ritorna 'on' se la posizione verticale è SWING (8), altrimenti 'off'."""
        if not getattr(self, "_swing_supported", False):
            return None
        val = self._get_attr(AC_ATTR_SWING_V)
        if val is None:
            return None
        mode = AC_SWING_MODE_ON if str(val) == AC_SWING_V_ON else AC_SWING_MODE_OFF
        _LOGGER.debug("Climate debug: swing_mode windDirectionVertical=%s -> %s", val, mode)
        return mode

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
                _LOGGER.debug("Climate debug: set_hvac_mode OFF -> onOffStatus=0")
                await self._send_command_in_executor(client, appliance, {"onOffStatus": "0"})
            else:
                # HVACMode è StrEnum: .value torna direttamente la stringa ("cool", "heat", ecc.)
                mode_str = hvac_mode.value
                
                # Cerca il codice numerico in AC_MODE_MAP_REVERSE
                mode_key = AC_MODE_MAP_REVERSE.get(mode_str, "1")
                _LOGGER.debug(
                    "Climate debug: set_hvac_mode %s -> onOffStatus=1 machMode=%s",
                    hvac_mode,
                    mode_key,
                )

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
            _LOGGER.debug("Climate debug: set_temperature ignorato, temperature assente kwargs=%s", kwargs)
            return
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError("Climate: appliance o client non disponibile")
        try:
            _LOGGER.debug("Climate debug: set_temperature %s -> tempSel=%s", temp, int(temp))
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
            _LOGGER.debug("Climate debug: set_fan_mode %s -> windSpeed=%s", fan_mode, speed_key)
            await self._send_command_in_executor(client, appliance, {"windSpeed": speed_key})
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error("Climate: errore set_fan_mode: %s", err, exc_info=True)
            raise HomeAssistantError(f"Climate: errore set_fan_mode: {err}") from err

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Attiva/disattiva l'oscillazione verticale (windDirectionVertical).

        'on' -> 8 (swing). 'off' -> una posizione fissa AMMESSA dal device. Non
        viene MAI inviato 0: i valori validi sono letti da .values del parametro.
        """
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError("Climate: appliance o client non disponibile")
        param = settings_param(appliance, AC_SWING_V_PARAM)
        if param is None:
            raise HomeAssistantError(
                "Climate: il dispositivo non espone windDirectionVertical"
            )
        allowed = param_allowed_values(param)
        if swing_mode == AC_SWING_MODE_ON:
            target = AC_SWING_V_ON
        else:
            target = fixed_vertical_value(allowed)
        if allowed and target not in allowed:
            raise HomeAssistantError(
                f"Climate: posizione swing {target} non ammessa (ammessi: {allowed})"
            )
        try:
            _LOGGER.debug(
                "Climate debug: set_swing_mode %s -> windDirectionVertical=%s (ammessi=%s)",
                swing_mode, target, allowed,
            )
            await self._send_command_in_executor(
                client, appliance, {AC_SWING_V_PARAM: target}
            )
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Climate: errore set_swing_mode: %s", err, exc_info=True)
            raise HomeAssistantError(f"Climate: errore set_swing_mode: {err}") from err

    async def _send_command_in_executor(self, client, appliance, params: dict) -> None:
        """Invia il comando settings dell'AC (sanitazione windDirection inclusa).

        Delega a ac_command.async_send_settings, condiviso con gli switch AC.
        """
        await async_send_settings(self.hass, client, appliance, params)
