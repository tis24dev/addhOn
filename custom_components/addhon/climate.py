"""Climate entity for Haier hOn - air conditioner AS35PBPHRA-PRE."""
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
    AC_TEMP_PARAM,
    AC_MODE_PARAM,
    AC_FAN_PARAM,
    AC_ATTR_ON_OFF,
    AC_ATTR_CURRENT_TEMP,
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
from .hon_commands import param_range

_LOGGER = logging.getLogger(__name__)

# Full HA mode list, used as the fallback when the device's machMode enum is not
# readable (so a device we cannot introspect keeps offering every mode, as before).
_DEFAULT_HVAC_MODES = [
    HVACMode.OFF,
    HVACMode.AUTO,
    HVACMode.COOL,
    HVACMode.DRY,
    HVACMode.HEAT,
    HVACMode.FAN_ONLY,
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Configure the climate entity based on the coordinator."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities = []
    for aid, data in coordinator.data.items():
        appliance = data.get("appliance")
        _LOGGER.debug(
            "Climate debug: evaluating appliance '%s' id=%s type=%s commands=%s attributes=%d",
            data.get("name"),
            aid,
            data.get("type"),
            command_names(appliance),
            len(data.get("attributes", {})) if isinstance(data.get("attributes"), dict) else 0,
        )
        if data.get("type") == APPLIANCE_AC:
            entities.append(HaierClimateEntity(coordinator, aid, client))
            _LOGGER.debug("Climate debug: created climate entity for id=%s", aid)
    async_add_entities(entities)


class HaierClimateEntity(HonBaseEntity, ClimateEntity):
    """Representation of the Haier hOn air conditioner."""

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._attr_name = None
        self._attr_unique_id = f"{appliance_id}_climate"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        # Setpoint range/step read from the device's real tempSel parameter (see
        # min_temp/max_temp/target_temperature_step below), not hardcoded: a model
        # with a different range or half-degree step must be honoured so the UI
        # only offers values the device accepts. Fallback to 16-30/1.0 if absent.
        self._temp_param = settings_param(self._appliance, AC_TEMP_PARAM)
        self._temp_fallback_range = (
            param_range(self._temp_param) if self._temp_param is not None else None
        ) or (16.0, 30.0, 1.0)
        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.FAN_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        # Swing: exposed ONLY if the device actually has windDirectionVertical among
        # the settings command parameters (capability-gate). Avoids offering a
        # control that the model does not support.
        swing_param = settings_param(self._appliance, AC_SWING_V_PARAM)
        self._swing_supported = swing_param is not None
        if self._swing_supported:
            self._attr_supported_features |= ClimateEntityFeature.SWING_MODE
            self._attr_swing_modes = [AC_SWING_MODE_OFF, AC_SWING_MODE_ON]
        # hvac_modes / fan_modes derived from the device's real machMode/windSpeed
        # enum (capability-gate, like swing above): the UI must not offer a mode
        # the device would reject at runtime. When the enum is NOT readable (param
        # absent or no values, e.g. a model ported without a runtime schema) we
        # fall back to the full HA list to avoid hiding modes a device supports but
        # does not expose -- the engine enum setter still rejects an invalid value.
        self._attr_hvac_modes = self._derive_hvac_modes()
        self._attr_fan_modes = self._derive_fan_modes()
        _LOGGER.debug(
            "Climate debug: initialized '%s' id=%s hvac_modes=%s fan_modes=%s temp_range=%s-%s",
            self._attr_unique_id,
            appliance_id,
            self._attr_hvac_modes,
            self._attr_fan_modes,
            self.min_temp,
            self.max_temp,
        )

    def _derive_hvac_modes(self) -> list[HVACMode]:
        """Supported HVAC modes from the device's machMode enum (OFF always present).

        Falls back to the full HA list when the enum is unreadable (param absent or
        empty values), to avoid regressing devices we cannot introspect.
        """
        param = settings_param(self._appliance, AC_MODE_PARAM)
        values = param_allowed_values(param) if param is not None else []
        if not values:
            return list(_DEFAULT_HVAC_MODES)
        modes = [HVACMode.OFF]  # OFF is onOffStatus, never a machMode value
        for code in values:  # keep the device's enum order (stable)
            name = AC_MODE_MAP.get(str(code))
            if name is None:
                continue
            try:
                mode = HVACMode(name)
            except ValueError:
                continue
            if mode not in modes:
                modes.append(mode)
        # Only OFF resolved (enum present but none mapped): keep the full list.
        return modes if len(modes) > 1 else list(_DEFAULT_HVAC_MODES)

    def _derive_fan_modes(self) -> list[str]:
        """Supported fan modes from the device's windSpeed enum, full list as fallback."""
        param = settings_param(self._appliance, AC_FAN_PARAM)
        values = param_allowed_values(param) if param is not None else []
        if not values:
            return list(AC_FAN_MAP_REVERSE.keys())
        modes: list[str] = []
        for code in values:
            name = AC_FAN_MAP.get(str(code))
            if name and name not in modes:
                modes.append(name)
        return modes or list(AC_FAN_MAP_REVERSE.keys())

    @property
    def _live_temp_range(self) -> tuple[float, float, float]:
        """(min, max, step) read from the runtime tempSel parameter, fallback to snapshot."""
        return param_range(self._temp_param) or self._temp_fallback_range

    @property
    def min_temp(self) -> float:
        return self._live_temp_range[0]

    @property
    def max_temp(self) -> float:
        return self._live_temp_range[1]

    @property
    def target_temperature_step(self) -> float:
        return self._live_temp_range[2]

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC state, translating the const.py string into the HA enum."""
        on_off = self._get_attr(AC_ATTR_ON_OFF, "0")
        if str(on_off) == "0":
            _LOGGER.debug(
                "Climate debug: hvac_mode '%s' id=%s onOffStatus=%s -> OFF",
                self._attr_unique_id,
                self._appliance_id,
                on_off,
            )
            return HVACMode.OFF

        # Read machMode (e.g. "2") using the constant from const.py
        mode_val = str(self._get_attr(AC_ATTR_MODE, "1"))

        # Retrieve the text from const.py (e.g. "cool")
        mode_str = AC_MODE_MAP.get(mode_val, "cool")

        # Convert the string into the correct Home Assistant enum
        try:
            mode = HVACMode(str(mode_str).lower())
            _LOGGER.debug(
                "Climate debug: hvac_mode '%s' id=%s onOffStatus=%s machMode=%s -> %s",
                self._attr_unique_id,
                self._appliance_id,
                on_off,
                mode_val,
                mode,
            )
            return mode
        except ValueError:
            _LOGGER.debug(
                "Climate debug: machMode=%s translated to mode_str=%s invalid, fallback COOL",
                mode_val,
                mode_str,
            )
            return HVACMode.COOL

    @property
    def target_temperature(self) -> float | None:
        """Return the set temperature. None if not available."""
        val = self._get_attr(AC_ATTR_TEMP)
        try:
            result = float(val) if val is not None else None
            _LOGGER.debug("Climate debug: target_temperature raw=%r -> %s", val, result)
            return result
        except (ValueError, TypeError):
            _LOGGER.debug("Climate debug: target_temperature not numeric raw=%r", val)
            return None

    @property
    def current_temperature(self) -> float | None:
        """Return the room temperature."""
        val = self._get_attr(AC_ATTR_CURRENT_TEMP)
        try:
            result = float(val) if val is not None else None
            _LOGGER.debug("Climate debug: current_temperature raw=%r -> %s", val, result)
            return result
        except (ValueError, TypeError):
            _LOGGER.debug("Climate debug: current_temperature not numeric raw=%r", val)
            return None

    @property
    def fan_mode(self) -> str | None:
        """Return the fan speed based on the reversed map."""
        val = str(self._get_attr(AC_ATTR_FAN_SPEED, "0"))
        fan = AC_FAN_MAP.get(val, "auto")
        _LOGGER.debug("Climate debug: fan_mode raw=%s -> %s", val, fan)
        return fan

    @property
    def swing_mode(self) -> str | None:
        """Return 'on' if the vertical position is SWING (8), otherwise 'off'."""
        if not getattr(self, "_swing_supported", False):
            return None
        val = self._get_attr(AC_ATTR_SWING_V)
        if val is None:
            return None
        mode = AC_SWING_MODE_ON if str(val) == AC_SWING_V_ON else AC_SWING_MODE_OFF
        _LOGGER.debug("Climate debug: swing_mode windDirectionVertical=%s -> %s", val, mode)
        return mode

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Send the mode change, converting the HVACMode into the exact hOn numeric code."""
        appliance = self._appliance
        if not appliance:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        try:
            client = self._hon_client
            if client is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="appliance_or_client_unavailable",
                )

            if hvac_mode == HVACMode.OFF:
                _LOGGER.debug("Climate debug: set_hvac_mode OFF -> onOffStatus=0")
                await self._send_command_in_executor(client, appliance, {"onOffStatus": "0"})
            else:
                # HVACMode is a StrEnum: .value returns the string directly ("cool", "heat", etc.)
                mode_str = hvac_mode.value

                # Look up the numeric code in AC_MODE_MAP_REVERSE
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
            _LOGGER.error("Climate: set_hvac_mode error: %s", err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_turn_on(self) -> None:
        """Turn on the air conditioner, putting it in COOL mode."""
        await self.async_set_hvac_mode(HVACMode.COOL)

    async def async_turn_off(self) -> None:
        """Turn off the air conditioner."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs) -> None:
        """Send the target temperature."""
        temp = kwargs.get("temperature")
        if temp is None:
            _LOGGER.debug("Climate debug: set_temperature ignored, temperature absent kwargs=%s", kwargs)
            return
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        try:
            # Do NOT int()-truncate: an integer value stays a clean int string
            # ("23"), a fractional one keeps its decimals ("23.5") and the engine
            # Range setter validates it against the device's real step/grid
            # (mirrors number.py; the old int() silently dropped the half degree).
            send_value = str(int(temp)) if float(temp).is_integer() else str(temp)
            _LOGGER.debug("Climate debug: set_temperature %s -> tempSel=%s", temp, send_value)
            await self._send_command_in_executor(client, appliance, {"tempSel": send_value})
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error("Climate: set_temperature error: %s", err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Send the fan speed based on the map in const.py."""
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        try:
            speed_key = AC_FAN_MAP_REVERSE.get(fan_mode, "5")
            _LOGGER.debug("Climate debug: set_fan_mode %s -> windSpeed=%s", fan_mode, speed_key)
            await self._send_command_in_executor(client, appliance, {"windSpeed": speed_key})
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error("Climate: set_fan_mode error: %s", err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Enable/disable the vertical oscillation (windDirectionVertical).

        'on' -> 8 (swing). 'off' -> a fixed position ALLOWED by the device. 0 is
        NEVER sent: the valid values are read from the parameter .values.
        """
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        param = settings_param(appliance, AC_SWING_V_PARAM)
        if param is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="swing_not_supported",
            )
        allowed = param_allowed_values(param)
        if swing_mode == AC_SWING_MODE_ON:
            target = AC_SWING_V_ON
        else:
            target = fixed_vertical_value(allowed)
        if allowed and target not in allowed:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="swing_position_not_allowed",
                translation_placeholders={"position": str(target), "allowed": str(allowed)},
            )
        try:
            _LOGGER.debug(
                "Climate debug: set_swing_mode %s -> windDirectionVertical=%s (allowed=%s)",
                swing_mode, target, allowed,
            )
            await self._send_command_in_executor(
                client, appliance, {AC_SWING_V_PARAM: target}
            )
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Climate: set_swing_mode error: %s", err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err

    async def _send_command_in_executor(self, client, appliance, params: dict) -> None:
        """Send the AC settings command (windDirection sanitation included).

        Delegates to ac_command.async_send_settings, shared with the AC switches.
        """
        await async_send_settings(self.hass, client, appliance, params)
