# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Heat-pump water heater (type HW, issue #113): temperature, mode, on/off.

Behind the experimental option until a real appliance confirms the commands. Every
write is a sparse patch built by `hpwh.py` and shaped by the `HPWH` send profile,
so it carries the keys the official app sends and nothing else.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from functools import partial
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
    appliance_series,
    boost_auto_off_due,
    boost_block,
    boost_patch,
    code,
    controls_supported,
    mode_block,
    mode_patch,
    power_patch,
    raise_refusal,
    temperature_block,
    temperature_patch,
)

_LOGGER = logging.getLogger(__name__)

_MACHMODE_TO_OPERATION = {"1": STATE_HEAT_PUMP, "2": STATE_ECO, "3": STATE_ELECTRIC}
_OPERATIONS = [STATE_HEAT_PUMP, STATE_ECO, STATE_ELECTRIC, STATE_OFF]
# The `set_temperature` service field that can carry a mode with the temperature.
_ATTR_OPERATION_MODE = "operation_mode"


def _settings(appliance):
    commands = getattr(appliance, "commands", None) or {}
    return commands.get(HPWH_SETTINGS_COMMAND)


def _has_boost(appliance) -> bool:
    """The boost switch's own gate: the schema declares the parameter the app writes."""
    settings = _settings(appliance)
    return "boostStatus" in (getattr(settings, "parameters", None) or {})


def _read(attributes: object, key: str) -> object:
    """One shadow attribute, read like `HonBaseEntity._get_attr`'s first lookup step:
    `attributes[key]`, its `.value` when it has one, and "" as None."""
    if not isinstance(attributes, Mapping):
        return None
    value = attributes.get(key)
    if value is None:
        return None
    if hasattr(value, "value"):
        value = value.value
    return None if value == "" else value


class _BoostAutoOff:
    """Switch boost off once the water is at the target, as the app's dashboard does.

    The app does it in a `useEffect` of `HPWHDashboard` (apk2
    decomp.txt:2327096-2327135), so only while that screen is open. Here it runs on
    every coordinator update, at any hour.

    It belongs to the config entry, not to an entity, so it keeps working when the
    water heater entity is disabled or was never created.

    D7, one successful send per episode. An episode is the water standing at one
    numeric target. It ends only when the water leaves the target (`temp != tempSel`,
    or either missing or unreadable) or when `tempSel` changes. A local `boostStatus`
    "0" does NOT end it: the dispatcher writes that "0" into the shadow itself when a
    send succeeds, so ending the episode there made a stale cloud "1" look like a new
    boost and switched it off again on every poll.

    A failed send is retried on the following updates, at most `_MAX_ATTEMPTS` sends
    per episode and never two at once (PR #117 review): one transient failure no longer
    leaves the boost on, and a cloud that keeps refusing costs three warnings, not one
    a minute.

    It sends only where the manual boost switch would (`boost_block`: no active error,
    remote control allowed, heater on, no vacation), which the app's own effect does
    not check (PR #117 review). A refused update costs no attempt.
    """

    _MAX_ATTEMPTS = 3

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator, client) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._client = client
        # {appliance_id: the episode of the numeric tempSel the water stands at}
        self._episodes: dict[str, _Episode] = {}

    def handle_update(self) -> None:
        for appliance_id, data in coordinator_data_map(self._coordinator).items():
            if not isinstance(data, Mapping) or data.get("type") != APPLIANCE_HW:
                continue
            appliance = data.get("appliance")
            if not _has_boost(appliance) or not controls_supported(appliance_series(appliance)):
                continue
            get = partial(_read, data.get("attributes"))
            temp, target = _number(get("temp")), _number(get("tempSel"))
            if temp is None or target is None or temp != target:
                self._episodes.pop(appliance_id, None)
                continue
            # Before the boost check (PR #118 review): a new target ends the episode
            # whatever boostStatus says, or a later boost back at the old target would
            # find that episode still done.
            episode = self._episodes.get(appliance_id)
            if episode is None or episode.target != target:
                episode = self._episodes[appliance_id] = _Episode(target)
            if not boost_auto_off_due(get):
                continue
            if episode.done or episode.in_flight or episode.attempts >= self._MAX_ATTEMPTS:
                continue
            if boost_block(get, turning_on=False) is not None:
                continue
            episode.in_flight = True
            episode.attempts += 1
            self._entry.async_create_background_task(
                self._hass, self._send(appliance, episode), name="addhon_hpwh_boost_auto_off"
            )

    async def _send(self, appliance, episode: _Episode) -> None:
        try:
            await async_dispatch_patch(
                self._hass, self._client, appliance, boost_patch(False)
            )
        except Exception as err:  # noqa: BLE001 - nobody awaits this task
            # An automatic send has no user to show the error to; log it.
            if episode.attempts >= self._MAX_ATTEMPTS:
                _LOGGER.warning(
                    "Heat-pump water heater: automatic boost off failed (attempt %d of %d, "
                    "giving up until the water leaves the target): %s",
                    episode.attempts, self._MAX_ATTEMPTS, err,
                )
            else:
                _LOGGER.warning(
                    "Heat-pump water heater: automatic boost off failed (attempt %d of %d, "
                    "retried on the next update): %s",
                    episode.attempts, self._MAX_ATTEMPTS, err,
                )
        else:
            episode.done = True
        finally:
            episode.in_flight = False


class _Episode:
    """One stay of the water at one target: how the automatic boost off went there."""

    __slots__ = ("target", "attempts", "in_flight", "done")

    def __init__(self, target: float) -> None:
        self.target = target
        self.attempts = 0
        self.in_flight = False
        self.done = False


def _mode_categories(appliance) -> dict[str, str] | None:
    """{HA operation: machMode fixed value}, or None if a category is missing.

    The value comes from the schema, not from the parameter's live `value`: the
    command-history recovery, a favourite or a start can write into a category in
    memory, and issue #115 showed one mode's category carrying another mode's
    machMode.
    """
    commands = getattr(appliance, "commands", None) or {}
    start = commands.get(HPWH_START_COMMAND)
    categories = getattr(start, "categories", None) or {}
    result: dict[str, str] = {}
    for operation, category in HPWH_MODE_CATEGORIES.items():
        command = categories.get(category)
        parameter = getattr(command, "parameters", {}).get("machMode") if command else None
        value = code(getattr(parameter, "schema_value", None))
        if not value:
            return None
        result[operation] = value
    return result


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if not bool(entry.options.get(CONF_ENABLE_EXPERIMENTAL, False)):
        return
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    # Registered before, and independently of, the entities below.
    auto_off = _BoostAutoOff(hass, entry, coordinator, entry_data.get("client"))
    entry.async_on_unload(coordinator.async_add_listener(auto_off.handle_update))
    entities = []
    for appliance_id, data in coordinator_data_map(coordinator).items():
        # The same guard as `_BoostAutoOff.handle_update`: one malformed entry must not
        # abort the setup and cost every water heater of the entry.
        if not isinstance(data, Mapping) or data.get("type") != APPLIANCE_HW:
            continue
        appliance = data.get("appliance")
        if not controls_supported(appliance_series(appliance)):
            continue
        settings = _settings(appliance)
        parameters = getattr(settings, "parameters", {}) if settings else {}
        if "onOffStatus" not in parameters or "tempSel" not in parameters:
            continue
        entities.append(HonHeatPumpWaterHeater(coordinator, appliance_id))
    async_add_entities(entities)


class HonHeatPumpWaterHeater(HonBaseEntity, WaterHeaterEntity):
    """The heat-pump water heater as one Home Assistant water heater."""

    _attr_translation_key = "heat_pump_water_heater"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

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
        """`tempSel` on the schema's grid (whole °C), half a step rounding up.

        Some M7 boilers publish a fractional target (52.2, 59.2, 62.8: issue #115
        research). Shown as published, every +/- of Home Assistant's card asked for
        53.2 or 51.2, which `async_set_temperature` refuses. Only the display moves:
        what is sent is still exactly the whole degree asked for.
        """
        target = _number(self._get_attr("tempSel"))
        if target is None or not math.isfinite(target):
            return target
        low, _high, step = self._range()
        return low + math.floor((target - low) / step + 0.5) * step

    @property
    def operation_list(self) -> list[str]:
        return list(_OPERATIONS)

    @property
    def current_operation(self) -> str | None:
        power = code(self._get_attr("onOffStatus"))
        if power == "0":
            return STATE_OFF
        if power != "1":
            return None  # missing or not a power state: unknown, not off
        return _MACHMODE_TO_OPERATION.get(code(self._get_attr("machMode")) or "")

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
        raise_refusal(mode_block(self._get_attr))
        if operation_mode == STATE_ECO and code(self._get_attr("machMode")) == modes[STATE_ECO]:
            # ECO chosen while ECO is active: the app sends no startProgram, it goes on
            # to the eco time-slot writes (apk2 decomp.txt:4506227-4506231), which are
            # not offered here. AUTO and ELEC are sent again, as the app does.
            return
        await self._send(mode_patch(HPWH_MODE_CATEGORIES[operation_mode], modes[operation_mode]))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if _ATTR_OPERATION_MODE in kwargs:
            # Two writes the app refuses separately; one at a time, each through
            # its own rules.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="hpwh_set_temperature_operation_mode",
            )
        value = kwargs.get(ATTR_TEMPERATURE)
        low, high, step = self._range()
        number = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
        if number and self._fahrenheit() and _whole_fahrenheit(value):
            # Home Assistant converts a Fahrenheit setpoint to Celsius before it
            # gets here, and a whole °F falls between two whole °C (105 °F is
            # 40.56 °C): the nearest whole degree is the one meant. Only a WHOLE °F
            # is rounded (PR #117 review): 104.7 °F is refused below, as 40.4 °C is
            # on a Celsius installation, instead of being changed silently.
            value = round(value)
        if (
            not number
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
        raise_refusal(temperature_block(self._get_attr))
        await self._send(temperature_patch(self._get_attr, int(value)))

    def _fahrenheit(self) -> bool:
        return self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT

    async def _send(self, patch) -> None:
        await async_dispatch_patch(self.hass, self._hon_client, self._appliance, patch)
        await self._async_request_command_refresh()


def _number(raw) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _whole_fahrenheit(celsius: float) -> bool:
    """True when `celsius` is Home Assistant's conversion of a whole °F.

    The tolerance absorbs the float error of the round trip (°F -> °C here -> °F),
    which is of the order of 1e-13; a real fractional °F is off by at least 0.1.

    False when the round trip overflows (a finite 1e308 °F becomes an infinite °F
    here, and `round` raises on infinity): the value then reaches the range check
    unrounded and is refused there with the translated error (PR #117 review).
    """
    fahrenheit = celsius * 9 / 5 + 32
    if not math.isfinite(fahrenheit):
        return False
    return abs(fahrenheit - round(fahrenheit)) < 1e-6
