"""Haier hOn numbers (Tier 3): writable temperature setpoints.

Cross-reference between the runtime schema (ground truth: dump of the real
fridge REF HDPW5620CNPK -> `settings`/`setParameters` with tempSelZ1[2..8],
tempSelZ2[-24..-16], tempSelZ3[0..5]) and the mapping of the decompiled app (S7,
superset: tempSelZ1..Z4, tempSelUZ/LZ, generic tempSel).

Each number is CAPABILITY-GATED: it is created only if the device exposes the
parameter in a write command (`settings`/`setParameters`), and min/max/step are
read from the REAL parameter at runtime (not hardcoded), so it is correct for
every model. The write goes through the generic sender
(hon_commands.async_send_command), the same mechanism as the AC `settings`
command.

unique_id CONSTRAINT: the `key` of each description is the SUFFIX of unique_id;
distinct from the Tier 2 sensors (e.g. number `target_temp_zone1` vs sensor
`temp_zone1`), so no collision.

CAVEAT: the mapped types are not live-validated on a powered-on device (the test
fridge is offline). The SCHEMA is validated from the dump; the live write is
queued on the cloud until the device comes online.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import math

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity
from .const import (
    APPLIANCE_FR,
    APPLIANCE_FRE,
    APPLIANCE_OV,
    APPLIANCE_REF,
    APPLIANCE_WC,
    DOMAIN,
)
from .debug_utils import redact_id
from .hon_commands import (
    async_send_command,
    find_settings_param,
    param_range,
    param_values,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HonNumberEntityDescription(NumberEntityDescription):
    """Description of a Haier hOn number.

    - `key` = unique_id suffix (new, no collision with the Tier 2 sensors).
    - `param` = name of the hOn parameter to read (state) and write (command).
    - `fallback_min/max/step` = used only if the client does not expose the range on
      the parameter; normally the REAL range is read at runtime from param_range().
    """

    param: str
    fallback_min: float = 0.0
    fallback_max: float = 100.0
    fallback_step: float = 1.0


def _temp(key: str, param: str, translation_key=None) -> HonNumberEntityDescription:
    """Temperature setpoint (C)."""
    return HonNumberEntityDescription(
        key=key,
        param=param,
        translation_key=translation_key,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        mode=NumberMode.BOX,
        icon="mdi:thermometer",
    )


# Fridge family (REF/FR/FRE): zone superset S7. On the real fridge Z1/Z2/Z3 appear;
# Z4/UZ/LZ appear only on the models that expose them (gate).
_COOLING_NUMBERS: tuple[HonNumberEntityDescription, ...] = (
    _temp("target_temp_zone1", "tempSelZ1"),
    _temp("target_temp_zone2", "tempSelZ2"),
    _temp("target_temp_zone3", "tempSelZ3"),
    _temp("target_temp_zone4", "tempSelZ4"),
    _temp("target_temp_upper", "tempSelUZ"),
    _temp("target_temp_lower", "tempSelLZ"),
)

# Wine cellar (WC): per-zone target + generic (S7).
_WINE_NUMBERS: tuple[HonNumberEntityDescription, ...] = (
    _temp("target_temp", "tempSel"),
    _temp("target_temp_zone2", "tempSelZ2"),
    _temp("target_temp_zone3", "tempSelZ3"),
)

# Oven (OV): cavity target (S7). Oven-appropriate fallback range (50-280 C step 5,
# from the app device dictionary) used only when the device does not expose its own
# range at runtime; a 0-100 default would be wrong for an oven.
_OVEN_NUMBERS: tuple[HonNumberEntityDescription, ...] = (
    HonNumberEntityDescription(
        key="target_temp",
        param="tempSel",
        translation_key="target_temp_oven",
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        mode=NumberMode.BOX,
        icon="mdi:thermometer",
        fallback_min=50.0,
        fallback_max=280.0,
        fallback_step=5.0,
    ),
)

NUMBERS: dict[str, tuple[HonNumberEntityDescription, ...]] = {
    APPLIANCE_REF: _COOLING_NUMBERS,
    APPLIANCE_FR: _COOLING_NUMBERS,
    APPLIANCE_FRE: _COOLING_NUMBERS,
    APPLIANCE_WC: _WINE_NUMBERS,
    APPLIANCE_OV: _OVEN_NUMBERS,
}


def _is_enum_param(param) -> bool:
    """True if the parameter is an enum (no numeric range), not a range parameter.

    Mirrors param_range()'s internal duck-type: a range parameter exposes min/max/step,
    an enum does not. We test this directly (instead of `param_values()` being non-empty)
    because HonParameterRange ALSO has a `.values` property - and on a malformed range it
    can loop forever - so `.values` cannot discriminate enum from range.
    """
    return not all(hasattr(param, attr) for attr in ("min", "max", "step"))


def _numeric_enum_set(param) -> list[float] | None:
    """Sorted distinct NUMERIC values of an enum param, or None.

    Returns None if the enum has no values or ANY value is non-numeric (a mode-style
    enum like ['low','high'] is not a sensible number control -> the caller skips it
    rather than fabricating bounds).
    """
    out: list[float] = []
    for value in param_values(param):
        try:
            out.append(float(str(value).replace(",", ".")))
        except (TypeError, ValueError):
            return None
    if not out:
        return None
    return sorted(set(out))


def _enum_step(values: list[float]) -> float:
    """A step that TILES a discrete numeric set so every member is reachable from min.

    For an integer-valued set this is the gcd of the gaps (e.g. {0,2,5} -> gcd(2,3)=1,
    so HA offers 0..5 and the membership check rejects 1/3/4; {0,2,4} -> 2, an exact
    tiling). For a non-integer set it falls back to the smallest gap. With NumberMode.BOX
    the step is mostly HA-side validation; the authoritative guard is the membership
    check in async_set_native_value.
    """
    if len(values) < 2:
        return 1.0
    diffs = [round(b - a, 6) for a, b in zip(values, values[1:])]
    if all(float(v).is_integer() for v in values):
        gcd = 0
        for diff in diffs:
            gcd = math.gcd(gcd, int(round(diff)))
        return float(gcd) if gcd else 1.0
    smallest = min(diffs)
    return smallest if smallest > 0 else 1.0


def _value_in_set(value: float, enum_set: list[float]) -> bool:
    """Membership in a discrete numeric set with a small float tolerance."""
    try:
        wanted = float(value)
    except (TypeError, ValueError):
        return False
    return any(abs(wanted - allowed) < 1e-6 for allowed in enum_set)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create the numbers only for the setpoints the device exposes as writable."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities: list[NumberEntity] = []
    for appliance_id, data in coordinator.data.items():
        app_type = data.get("type", "")
        appliance = data.get("appliance")
        created: list[str] = []
        for description in NUMBERS.get(app_type, ()):
            found = find_settings_param(appliance, description.param)
            if found is None:
                continue
            command_name, param = found
            # An enum-typed setpoint (e.g. tempSelZ3 = ['0','2','5'] on some multidoor
            # models) has no min/max/step, so a plain number would fabricate 0..100
            # bounds and the cloud enum setter would reject every legitimate pick. Derive
            # the discrete numeric set instead; a non-numeric enum is not a number control
            # at all -> skip it (gate off) rather than offer fabricated bounds.
            enum_set: list[float] | None = None
            if _is_enum_param(param):
                enum_set = _numeric_enum_set(param)
                if enum_set is None:
                    _LOGGER.debug(
                        "Number debug: skip non-numeric enum setpoint '%s' (param=%s)",
                        description.key,
                        description.param,
                    )
                    continue
            entities.append(
                HonNumber(
                    coordinator,
                    appliance_id,
                    description,
                    command_name,
                    param,
                    client,
                    enum_set,
                )
            )
            created.append(description.key)
        _LOGGER.debug(
            "Number debug: '%s' (type=%s, id=%s) -> %d/%d numbers %s",
            data.get("name", "Haier"),
            app_type,
            redact_id(appliance_id),
            len(created),
            len(NUMBERS.get(app_type, ())),
            created,
        )
    async_add_entities(entities)


class HonNumber(HonBaseEntity, NumberEntity):
    """Haier hOn number: writes a range parameter of the settings command."""

    entity_description: HonNumberEntityDescription

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        description: HonNumberEntityDescription,
        command_name: str,
        param,
        client=None,
        enum_set: list[float] | None = None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self.entity_description = description
        self._command_name = command_name
        self._param = param
        # Discrete numeric set for an enum-typed setpoint (None for a normal range):
        # fixes the bounds AND the picked value is validated against it before sending.
        self._enum_set = enum_set
        self._attr_translation_key = description.translation_key or description.key
        self._attr_unique_id = f"{appliance_id}_{description.key}"
        # Range snapshot used as fallback; the live bounds are re-read from the
        # parameter on each access (the engine rules can change them at runtime). For an
        # enum the "range" is derived from the discrete set (min/max + a tiling step),
        # never the fabricated 0..100 default.
        if enum_set:
            self._fallback_range = (enum_set[0], enum_set[-1], _enum_step(enum_set))
        else:
            self._fallback_range = param_range(param) or (
                description.fallback_min,
                description.fallback_max,
                description.fallback_step,
            )
        _LOGGER.debug(
            "Number debug: init '%s' id=%s param=%s cmd=%s range=%s",
            redact_id(self._attr_unique_id, appliance_id), redact_id(appliance_id), description.param, command_name, self._live_range,
        )

    @property
    def _live_range(self) -> tuple[float, float, float]:
        """(min, max, step) read from the runtime parameter, fallback to the snapshot."""
        return param_range(self._param) or self._fallback_range

    @property
    def native_min_value(self) -> float:
        return self._live_range[0]

    @property
    def native_max_value(self) -> float:
        return self._live_range[1]

    @property
    def native_step(self) -> float:
        return self._live_range[2]

    @property
    def native_value(self) -> float | None:
        raw = self._get_attr(self.entity_description.param)
        if raw is None:
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            _LOGGER.debug(
                "Number debug: native_value not numeric for %s raw=%r",
                self.entity_description.param, raw,
            )
            return None

    async def async_set_native_value(self, value: float) -> None:
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        # Enum setpoint: reject a value outside the discrete set up front (clear message,
        # no pointless cloud round-trip) instead of letting the cloud enum setter raise an
        # opaque ValueError that would surface as a generic command_error.
        if self._enum_set is not None and not _value_in_set(value, self._enum_set):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="invalid_setpoint",
                translation_placeholders={
                    "value": str(value),
                    "allowed": ", ".join(
                        str(int(v)) if float(v).is_integer() else str(v)
                        for v in self._enum_set
                    ),
                },
            )
        # ALWAYS send a string: the client's str_to_float does `int(string)` and catches
        # only ValueError, so a fractional float (5.5) would be truncated to 5
        # WITHOUT error. The string "5.5" instead stays 5.5 and the range setter
        # validates the step (rejects off-grid values). Integer -> clean "4" (no "4.0").
        send_value = str(int(value)) if float(value).is_integer() else str(value)
        param = self.entity_description.param
        try:
            _LOGGER.debug(
                "Number debug: set %s=%s (cmd=%s) id=%s",
                param, send_value, self._command_name, redact_id(self._appliance_id),
            )
            await async_send_command(
                self.hass, client, appliance, self._command_name, {param: send_value}
            )
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Number: set error %s=%s: %s", param, send_value, err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err
