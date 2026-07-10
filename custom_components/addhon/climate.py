# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

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
    AC_ON_OFF_PARAM,
    AC_ATTR_ON_OFF,
    AC_ATTR_CURRENT_TEMP,
    AC_ATTR_FAN_SPEED,
    AC_ATTR_SWING_V,
    AC_SWING_V_PARAM,
    AC_SWING_V_ON,
    AC_SWING_MODE_ON,
    AC_SWING_MODE_OFF,
    AC_PROGRAM_MAP,
    AC_PROGRAM_SIMPLE_START,
    PROGRAM_PARAM_NAMES,
)
from .debug_utils import command_names, redact_id
from .ac_command import (
    async_send_settings,
    fixed_vertical_value,
    param_allowed_values,
    settings_param,
)
from .hon_commands import async_send_command, param_range, param_values
from .program_options import async_send_program, startprogram_command

# startProgram/stopProgram are the two program-based AC write commands (see
# AC_PROGRAM_MAP in const.py). The names are stable across program category swaps.
STOPPROGRAM_COMMAND = "stopProgram"

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
            redact_id(aid),
            data.get("type"),
            command_names(appliance),
            len(data.get("attributes", {})) if isinstance(data.get("attributes"), dict) else 0,
        )
        if data.get("type") == APPLIANCE_AC:
            entities.append(HaierClimateEntity(coordinator, aid, client))
            _LOGGER.debug("Climate debug: created climate entity for id=%s", redact_id(aid))
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
            redact_id(self._attr_unique_id, appliance_id),
            redact_id(appliance_id),
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
    def hvac_mode(self) -> HVACMode | None:
        """Return the current HVAC state, translating the const.py string into the HA enum."""
        on_off = self._get_attr(AC_ATTR_ON_OFF, "0")
        if str(on_off) == "0":
            _LOGGER.debug(
                "Climate debug: hvac_mode '%s' id=%s onOffStatus=%s -> OFF",
                redact_id(self._attr_unique_id, self._appliance_id),
                redact_id(self._appliance_id),
                on_off,
            )
            return HVACMode.OFF

        # Read machMode (e.g. "2") using the constant from const.py. NO default: a
        # powered-on unit whose current mode is absent (settings.machMode missing) must
        # report unknown (None), not a guessed mode. Defaulting to "1" here coerced an
        # absent mode into COOL, defeating the unknown-state contract below.
        raw_mode = self._get_attr(AC_ATTR_MODE)
        if raw_mode is None:
            _LOGGER.debug(
                "Climate debug: machMode missing id=%s -> hvac_mode None",
                redact_id(self._appliance_id),
            )
            return None
        mode_val = str(raw_mode)

        # Retrieve the text from const.py (e.g. "cool"). No default: an unmapped raw
        # value must not be coerced into a guessed mode -- HA rejects a current option
        # outside hvac_modes ("is not a valid option") and logs a warning. Report
        # unknown (None) instead, which HA represents honestly.
        mode_str = AC_MODE_MAP.get(mode_val)
        if mode_str is None:
            _LOGGER.debug(
                "Climate debug: machMode=%s not in AC_MODE_MAP, hvac_mode -> None",
                mode_val,
            )
            return None

        # Convert the string into the correct Home Assistant enum
        try:
            mode = HVACMode(str(mode_str).lower())
        except ValueError:
            _LOGGER.debug(
                "Climate debug: machMode=%s translated to mode_str=%s invalid, hvac_mode -> None",
                mode_val,
                mode_str,
            )
            return None

        # Never report a mode outside the advertised capability list (HA would ignore
        # it and warn). With the full-list fallback this can only trigger when the
        # device exposed a restricted machMode enum that excludes the current value.
        if mode not in self._attr_hvac_modes:
            _LOGGER.debug(
                "Climate debug: hvac_mode %s not in advertised modes %s -> None",
                mode,
                self._attr_hvac_modes,
            )
            return None

        _LOGGER.debug(
            "Climate debug: hvac_mode '%s' id=%s onOffStatus=%s machMode=%s -> %s",
            redact_id(self._attr_unique_id, self._appliance_id),
            redact_id(self._appliance_id),
            on_off,
            mode_val,
            mode,
        )
        return mode

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
        # No default: an unmapped raw value, or one outside the advertised fan_modes,
        # must report unknown (None) rather than a guessed speed HA would reject.
        fan = AC_FAN_MAP.get(val)
        if fan is None or fan not in (self._attr_fan_modes or []):
            _LOGGER.debug("Climate debug: fan_mode raw=%s not advertised -> None", val)
            return None
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

    def _is_program_based(self) -> bool:
        """True for AC models that drive power/mode via startProgram/stopProgram.

        Two AC write models exist (see AC_PROGRAM_MAP in const.py):
        - settings-based (e.g. AS35PBPHRA-PRE): the `settings` command carries
          onOffStatus + machMode, so power/mode are written into `settings`.
        - program-based (e.g. AD71S2SM3FA(H)): the `settings` command has NO
          onOffStatus; ON goes through startProgram (program enum, onOffStatus fixed
          "1") and OFF through stopProgram (onOffStatus fixed "0"). Writing
          onOffStatus/machMode into `settings` on such a model raises
          "Parameter(s) not found" before the request ever reaches the cloud, which
          is exactly why every on/off/mode command looked "ignored".

        The gate: NO onOffStatus among the settings params AND a startProgram command
        exists. Temperature/fan stay on the settings path in BOTH models (tempSel /
        windSpeed live on `settings` either way), so only power/mode is rerouted.
        """
        if settings_param(self._appliance, AC_ON_OFF_PARAM) is not None:
            return False
        return startprogram_command(self._appliance) is not None

    def _startprogram_programs(self) -> list[str]:
        """Program codes the device declares in its startProgram enum, or [].

        Reads the `program`/`prCode` parameter's .values off the startProgram
        command. Used to capability-gate a mapped program BEFORE sending it.
        """
        command = startprogram_command(self._appliance)
        params = getattr(command, "parameters", None) if command is not None else None
        if not isinstance(params, dict):
            return []
        for pname in PROGRAM_PARAM_NAMES:
            param = params.get(pname)
            if param is not None:
                return param_values(param)
        return []

    def _program_for_mode(self, hvac_mode: HVACMode) -> str:
        """Map a (non-OFF) HVAC mode to its startProgram code, capability-gated.

        FAN_ONLY special-cases to iot_fan (NOT iot_fan_only). Raises
        program_not_supported when the mode has no program mapping, or when the
        mapped program is absent from the device's live startProgram enum -- never
        silently no-op nor fall back to the settings path (which lacks onOffStatus).
        Called BEFORE the send try-block so the specific key is not rewrapped.
        """
        program_code = AC_PROGRAM_MAP.get(hvac_mode.value)
        if program_code is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="program_not_supported",
                translation_placeholders={"program": str(hvac_mode.value)},
            )
        self._assert_program_available(program_code)
        return program_code

    def _assert_program_available(self, program_code: str) -> None:
        """Guard: raise program_not_supported if the device's startProgram enum is
        readable AND does not declare `program_code`. When the enum is unreadable
        (empty) we let it through (the engine enum setter still rejects a bad value),
        mirroring the hvac_modes/fan_modes full-list fallback."""
        available = self._startprogram_programs()
        if available and program_code not in available:
            _LOGGER.debug(
                "Climate debug: program %s not in startProgram enum %s",
                program_code,
                available,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="program_not_supported",
                translation_placeholders={"program": str(program_code)},
            )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Send the mode change.

        Settings-based model: onOffStatus (+ machMode) into the `settings` command,
        exactly as before. Program-based model: OFF -> stopProgram; a concrete mode ->
        startProgram with the mapped iot_<mode> program.
        """
        appliance = self._appliance
        client = self._hon_client
        # Both checks BEFORE the try: a missing appliance/client must surface the
        # specific key, not be rewrapped into command_error by the except below
        # (consistent with set_temperature/fan/swing).
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        program_based = self._is_program_based()
        # Resolve + capability-gate the program BEFORE the try, so a
        # program_not_supported surfaces with its own key (like the swing gate) rather
        # than being rewrapped into command_error by the except below.
        program_code = None
        if program_based and hvac_mode != HVACMode.OFF:
            program_code = self._program_for_mode(hvac_mode)
        try:
            if hvac_mode == HVACMode.OFF:
                if program_based:
                    # Program-based OFF: stopProgram carries the mandatory fixed
                    # onOffStatus="0"; the engine serializes it, no override needed.
                    _LOGGER.debug("Climate debug: set_hvac_mode OFF -> stopProgram")
                    await async_send_command(
                        self.hass, client, appliance, STOPPROGRAM_COMMAND, {}
                    )
                else:
                    _LOGGER.debug("Climate debug: set_hvac_mode OFF -> onOffStatus=0")
                    await self._send_command_in_executor(
                        client, appliance, {"onOffStatus": "0"}
                    )
            elif program_based:
                _LOGGER.debug(
                    "Climate debug: set_hvac_mode %s -> startProgram %s",
                    hvac_mode,
                    program_code,
                )
                await async_send_program(self.hass, client, appliance, program_code)
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
        """Turn the AC back on, restoring its last mode.

        HA convention for TURN_ON is to resume the previous operating state, not
        force a fixed mode. Settings-based model: send only onOffStatus=1, so the
        device keeps its stored machMode and resumes the last mode (the old code
        forced COOL). Program-based model: send startProgram iot_simple_start, the
        program the device uses to resume its last mode.
        """
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        program_based = self._is_program_based()
        # Capability-gate simple-start BEFORE the try (same reasoning as set_hvac_mode).
        if program_based:
            self._assert_program_available(AC_PROGRAM_SIMPLE_START)
        try:
            if program_based:
                _LOGGER.debug(
                    "Climate debug: turn_on -> startProgram %s", AC_PROGRAM_SIMPLE_START
                )
                await async_send_program(
                    self.hass, client, appliance, AC_PROGRAM_SIMPLE_START
                )
            else:
                _LOGGER.debug("Climate debug: turn_on -> onOffStatus=1 (mode preserved)")
                await self._send_command_in_executor(
                    client, appliance, {"onOffStatus": "1"}
                )
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error("Climate: turn_on error: %s", err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err

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
            if target == AC_SWING_V_ON:
                # No genuine fixed (non-swing) position exists for this model, so
                # fixed_vertical_value fell back to the swing-ON code (8). Sending it
                # for an OFF request would START oscillation -- the opposite of what
                # was asked. Refuse instead of transmitting the wrong command.
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="swing_position_not_allowed",
                    translation_placeholders={"position": str(target), "allowed": str(allowed)},
                )
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
