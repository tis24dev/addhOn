"""Number Haier hOn (Tier 3): setpoint di temperatura scrivibili.

Cross-reference tra lo schema runtime di pyhОn (ground truth: dump del frigo
reale REF HDPW5620CNPK -> `settings`/`setParameters` con tempSelZ1[2..8],
tempSelZ2[-24..-16], tempSelZ3[0..5]) e la mappatura della app decompilata (§7,
superset: tempSelZ1..Z4, tempSelUZ/LZ, tempSel generico).

Ogni number è CAPABILITY-GATED: si crea solo se il device espone il parametro
in un comando di scrittura (`settings`/`setParameters`), e min/max/step sono
letti dal parametro REALE a runtime (non hardcoded), così è corretto per ogni
modello. La scrittura passa dal sender generico (hon_commands.async_send_command),
lo stesso meccanismo del comando `settings` dell'AC.

VINCOLO unique_id: la `key` di ogni description è il SUFFISSO di unique_id;
distinto dai sensori Tier 2 (es. number `target_temp_zone1` vs sensor
`temp_zone1`), quindi nessuna collisione.

CAVEAT: i tipi mappati non sono live-validati su device acceso (il frigo di test
è offline). Lo SCHEMA è validato dal dump; la scrittura live va in coda sul cloud
finché il device non è online.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

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
from .hon_commands import async_send_command, find_settings_param, param_range

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HonNumberEntityDescription(NumberEntityDescription):
    """Description di un number Haier hOn.

    - `key` = suffisso unique_id (nuovo, nessuna collisione coi sensori Tier 2).
    - `param` = nome del parametro hOn da leggere (stato) e scrivere (comando).
    - `fallback_min/max/step` = usati solo se pyhОn non espone il range sul
      parametro; di norma il range REALE è letto a runtime da param_range().
    """

    param: str
    fallback_min: float = 0.0
    fallback_max: float = 100.0
    fallback_step: float = 1.0


def _temp(key: str, name: str, param: str) -> HonNumberEntityDescription:
    """Setpoint di temperatura (°C)."""
    return HonNumberEntityDescription(
        key=key,
        name=name,
        param=param,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        mode=NumberMode.BOX,
        icon="mdi:thermometer",
    )


# Famiglia frigo (REF/FR/FRE): superset zone §7. Sul frigo reale appaiono Z1/Z2/Z3;
# Z4/UZ/LZ compaiono solo sui modelli che li espongono (gate).
_COOLING_NUMBERS: tuple[HonNumberEntityDescription, ...] = (
    _temp("target_temp_zone1", "Temperatura Zona 1", "tempSelZ1"),
    _temp("target_temp_zone2", "Temperatura Zona 2", "tempSelZ2"),
    _temp("target_temp_zone3", "Temperatura Zona 3", "tempSelZ3"),
    _temp("target_temp_zone4", "Temperatura Zona 4", "tempSelZ4"),
    _temp("target_temp_upper", "Temperatura Zona Superiore", "tempSelUZ"),
    _temp("target_temp_lower", "Temperatura Zona Inferiore", "tempSelLZ"),
)

# Cantinetta vino (WC): target per-zona + generico (§7).
_WINE_NUMBERS: tuple[HonNumberEntityDescription, ...] = (
    _temp("target_temp", "Temperatura", "tempSel"),
    _temp("target_temp_zone2", "Temperatura Zona 2", "tempSelZ2"),
    _temp("target_temp_zone3", "Temperatura Zona 3", "tempSelZ3"),
)

# Forno (OV): target cavità (§7).
_OVEN_NUMBERS: tuple[HonNumberEntityDescription, ...] = (
    _temp("target_temp", "Temperatura Forno", "tempSel"),
)

NUMBERS: dict[str, tuple[HonNumberEntityDescription, ...]] = {
    APPLIANCE_REF: _COOLING_NUMBERS,
    APPLIANCE_FR: _COOLING_NUMBERS,
    APPLIANCE_FRE: _COOLING_NUMBERS,
    APPLIANCE_WC: _WINE_NUMBERS,
    APPLIANCE_OV: _OVEN_NUMBERS,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Crea i number solo per i setpoint che il device espone come scrivibili."""
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
            entities.append(
                HonNumber(coordinator, appliance_id, description, command_name, param, client)
            )
            created.append(description.key)
        _LOGGER.debug(
            "Number debug: '%s' (type=%s, id=%s) -> %d/%d number %s",
            data.get("name", "Haier"),
            app_type,
            appliance_id,
            len(created),
            len(NUMBERS.get(app_type, ())),
            created,
        )
    async_add_entities(entities)


class HonNumber(HonBaseEntity, NumberEntity):
    """Number Haier hOn: scrive un parametro range del comando settings."""

    entity_description: HonNumberEntityDescription

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        description: HonNumberEntityDescription,
        command_name: str,
        param,
        client=None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self.entity_description = description
        self._command_name = command_name
        self._param = param
        device_name = self._appliance_data.get("name", "Haier")
        self._attr_name = f"{device_name} - {description.name}"
        self._attr_unique_id = f"{appliance_id}_{description.key}"
        # Snapshot del range come fallback; i bound vivi sono riletti dal
        # parametro a ogni accesso (le rule pyhОn possono cambiarli a runtime).
        self._fallback_range = param_range(param) or (
            description.fallback_min,
            description.fallback_max,
            description.fallback_step,
        )
        _LOGGER.debug(
            "Number debug: init '%s' id=%s param=%s cmd=%s range=%s",
            self._attr_name, appliance_id, description.param, command_name, self._live_range,
        )

    @property
    def _live_range(self) -> tuple[float, float, float]:
        """(min, max, step) letti dal parametro runtime, fallback allo snapshot."""
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
                "Number debug: native_value non numerico per %s raw=%r",
                self.entity_description.param, raw,
            )
            return None

    async def async_set_native_value(self, value: float) -> None:
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError("Number: appliance o client non disponibile")
        # Manda SEMPRE una stringa: pyhОn str_to_float fa `int(string)` e cattura
        # solo ValueError, quindi un float frazionario (5.5) verrebbe troncato a 5
        # SENZA errore. La stringa "5.5" invece resta 5.5 e il setter range valida
        # lo step (rifiuta i fuori-griglia). Intero -> "4" pulito (no "4.0").
        send_value = str(int(value)) if float(value).is_integer() else str(value)
        param = self.entity_description.param
        try:
            _LOGGER.debug(
                "Number debug: set %s=%s (cmd=%s) id=%s",
                param, send_value, self._command_name, self._appliance_id,
            )
            await async_send_command(
                self.hass, client, appliance, self._command_name, {param: send_value}
            )
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Number: errore set %s=%s: %s", param, send_value, err, exc_info=True)
            raise HomeAssistantError(f"Number: errore comando {param}: {err}") from err
