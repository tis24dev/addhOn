# custom_components/addhon/water_heater.py
# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Heat-pump water heater (type HW, issue #113): temperature, mode, on/off.

Behind the experimental option until a real appliance confirms the commands. Every
write is a sparse patch built by `hpwh.py` and shaped by the `HPWH` send profile,
so it carries the keys the official app sends and nothing else.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.water_heater import (
    STATE_ECO,
    STATE_ELECTRIC,
    STATE_HEAT_PUMP,
    STATE_OFF,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity, coordinator_data_map
from .command_dispatch import async_dispatch_patch
from .const import APPLIANCE_HW, CONF_ENABLE_EXPERIMENTAL, DOMAIN
from .hon_commands import param_range
from .hpwh import (
    HPWH_MODE_CATEGORIES,
    HPWH_SETTINGS_COMMAND,
    HPWH_START_COMMAND,
    boost_auto_off_due,
    boost_patch,
    code,
    mode_block,
    mode_patch,
    power_patch,
    temperature_block,
    temperature_patch,
)

_LOGGER = logging.getLogger(__name__)

_MACHMODE_TO_OPERATION = {"1": STATE_HEAT_PUMP, "2": STATE_ECO, "3": STATE_ELECTRIC}
_OPERATIONS = [STATE_HEAT_PUMP, STATE_ECO, STATE_ELECTRIC, STATE_OFF]


def _settings(appliance):
    commands = getattr(appliance, "commands", None) or {}
    return commands.get(HPWH_SETTINGS_COMMAND)


def _mode_categories(appliance) -> dict[str, str] | None:
    """{HA operation: machMode fixed value}, or None if a category is missing."""
    commands = getattr(appliance, "commands", None) or {}
    start = commands.get(HPWH_START_COMMAND)
    categories = getattr(start, "categories", None) or {}
    result: dict[str, str] = {}
    for operation, category in HPWH_MODE_CATEGORIES.items():
        command = categories.get(category)
        parameter = getattr(command, "parameters", {}).get("machMode") if command else None
        value = code(getattr(parameter, "value", None))
        if not value:
            return None
        result[operation] = value
    return result


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if not bool(entry.options.get(CONF_ENABLE_EXPERIMENTAL, False)):
        return
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = []
    for appliance_id, data in coordinator_data_map(coordinator).items():
        if data.get("type") != APPLIANCE_HW:
            continue
        settings = _settings(data.get("appliance"))
        parameters = getattr(settings, "parameters", {}) if settings else {}
        if "onOffStatus" not in parameters or "tempSel" not in parameters:
            continue
        entities.append(HonHeatPumpWaterHeater(coordinator, appliance_id))
    async_add_entities(entities)


class HonHeatPumpWaterHeater(HonBaseEntity, WaterHeaterEntity):
    """The heat-pump water heater as one Home Assistant water heater."""

    _attr_translation_key = "heat_pump_water_heater"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    # The (temp, tempSel) of the boost episode already switched off, until boostStatus
    # stops being "1" (D7: one send per episode).
    _auto_off_sent_for: tuple | None = None

    def __init__(self, coordinator, appliance_id: str) -> None:
        super().__init__(coordinator, appliance_id)
        self._attr_unique_id = f"{appliance_id}_heat_pump_water_heater"

    @property
    def supported_features(self) -> WaterHeaterEntityFeature:
        features = WaterHeaterEntityFeature.TARGET_TEMPERATURE | WaterHeaterEntityFeature.ON_OFF
        if _mode_categories(self._appliance) is not None:
            features |= WaterHeaterEntityFeature.OPERATION_MODE
        return features

    def _range(self) -> tuple[float, float, float]:
        settings = _settings(self._appliance)
        parameter = getattr(settings, "parameters", {}).get("tempSel") if settings else None
        return param_range(parameter) or (35.0, 75.0, 1.0)

    @property
    def min_temp(self) -> float:
        return self._range()[0]

    @property
    def max_temp(self) -> float:
        return self._range()[1]

    @property
    def target_temperature_step(self) -> float:
        return self._range()[2]

    @property
    def current_temperature(self) -> float | None:
        return _number(self._get_attr("temp"))

    @property
    def target_temperature(self) -> float | None:
        return _number(self._get_attr("tempSel"))

    @property
    def operation_list(self) -> list[str]:
        return list(_OPERATIONS)

    @property
    def current_operation(self) -> str | None:
        if code(self._get_attr("onOffStatus")) != "1":
            return STATE_OFF
        return _MACHMODE_TO_OPERATION.get(code(self._get_attr("machMode")) or "")

    def _handle_coordinator_update(self) -> None:
        """Do what the app's dashboard does: boost off once the water is at the target."""
        get = self._get_attr
        if code(get("boostStatus")) != "1":
            self._auto_off_sent_for = None
        elif boost_auto_off_due(get):
            episode = (code(get("temp")), code(get("tempSel")))
            if episode != self._auto_off_sent_for:
                self._auto_off_sent_for = episode
                self.hass.async_create_task(self._auto_boost_off())
        super()._handle_coordinator_update()

    async def _auto_boost_off(self) -> None:
        try:
            await async_dispatch_patch(
                self.hass, self._hon_client, self._appliance, boost_patch(False)
            )
        except HomeAssistantError as err:
            # An automatic send has no user to show the error to; log it. The episode
            # stays recorded, so there is no retry loop.
            _LOGGER.warning("Heat-pump water heater: automatic boost off failed: %s", err)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send(power_patch(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(power_patch(False))

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        if operation_mode == STATE_OFF:
            await self._send(power_patch(False))
            return
        modes = _mode_categories(self._appliance) or {}
        if operation_mode not in modes:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="program_not_supported",
                translation_placeholders={"program": operation_mode},
            )
        self._refuse(mode_block(self._get_attr))
        await self._send(mode_patch(HPWH_MODE_CATEGORIES[operation_mode], modes[operation_mode]))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        value = kwargs.get(ATTR_TEMPERATURE)
        low, high, step = self._range()
        if (
            not isinstance(value, (int, float))
            or float(value) != int(value)
            or not low <= value <= high
            or (value - low) % step
        ):
            # The message is "Value {value} is not allowed (allowed: {allowed})."
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_setpoint",
                translation_placeholders={
                    "value": str(value),
                    "allowed": f"{low:g}-{high:g}, step {step:g}",
                },
            )
        self._refuse(temperature_block(self._get_attr))
        await self._send(temperature_patch(self._get_attr, int(value)))

    @staticmethod
    def _refuse(reason: str | None) -> None:
        """Raise the localized refusal `hpwh.py` names, if it names one.

        One literal raise per key: `test_translations` verifies raised keys against
        the JSON and refuses a computed `translation_key`.
        """
        if reason is None:
            return
        if reason == "hpwh_unavailable":
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="hpwh_unavailable"
            )
        if reason == "hpwh_switch_on_first":
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="hpwh_switch_on_first"
            )
        if reason == "hpwh_vacation_active":
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="hpwh_vacation_active"
            )
        if reason == "hpwh_sterilization_running":
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="hpwh_sterilization_running"
            )
        if reason == "hpwh_boost_at_target":
            # Not a water heater refusal: the boost switch raises it. It lives here
            # so every hpwh.py refusal is raised from one place.
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="hpwh_boost_at_target"
            )
        raise ValueError(f"Unhandled water heater refusal: {reason}")

    async def _send(self, patch) -> None:
        await async_dispatch_patch(self.hass, self._hon_client, self._appliance, patch)
        await self._async_request_command_refresh()


def _number(raw) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
