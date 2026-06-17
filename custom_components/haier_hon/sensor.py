"""Sensori Haier hOn, definiti per tipo di elettrodomestico via description table.

Il set di sensori dipende dal tipo (AC / WM / WD / TD): la lavatrice (WM) e la
lavasciuga (WD) hanno i sensori di acqua + energia; l'asciugatrice (TD) NON usa
acqua e non espone quei contatori, quindi prende solo stato, tempo rimanente e
cicli (da programsCounter). Il condizionatore (AC) ha temperature, umidita,
frequenza compressore ed energia.

VINCOLO: la `key` di ogni description coincide con il SUFFISSO di unique_id
storico (es. "temp_indoor", "total_energy", "state", "total_washes"): NON va
cambiata, altrimenti le entita gia registrate verrebbero duplicate/orfanate.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity
from .const import (
    APPLIANCE_AC,
    APPLIANCE_TD,
    APPLIANCE_WD,
    APPLIANCE_WM,
    AC_ATTR_COMPRESSOR_FREQ,
    AC_ATTR_CURRENT_TEMP,
    AC_ATTR_HUMIDITY_INDOOR,
    AC_ATTR_OUTDOOR_TEMP,
    AC_ATTR_TOTAL_ENERGY,
    DOMAIN,
    TD_ATTR_CYCLES,
    WM_ATTR_CURRENT_ENERGY,
    WM_ATTR_CURRENT_WATER,
    WM_ATTR_DELAY,
    WM_ATTR_DIRT_LEVEL,
    WM_ATTR_DRY_LEVEL,
    WM_ATTR_LOADING,
    WM_ATTR_PROGRAM_NAME,
    WM_ATTR_REMAINING,
    WM_ATTR_SPIN_SPEED,
    WM_ATTR_STATUS,
    WM_ATTR_TEMP,
    WM_ATTR_TOTAL_ENERGY,
    WM_ATTR_TOTAL_WASH,
    WM_ATTR_TOTAL_WATER,
    WM_STATE_MAP,
)

_LOGGER = logging.getLogger(__name__)


def _wm_state(raw) -> str:
    """Traduce machMode nel testo di stato (comportamento storico, invariato)."""
    if raw is None:
        return "Non disponibile"
    code = str(raw)
    return WM_STATE_MAP.get(code, f"Sconosciuto ({code})")


@dataclass(frozen=True, kw_only=True)
class HonSensorEntityDescription(SensorEntityDescription):
    """Description di un sensore Haier hOn.

    - `key` = suffisso unique_id storico (NON modificare).
    - `attr_key` = chiave attributo pyhOn letta via HonBaseEntity._get_attr.
    - `value_fn` opzionale trasforma il grezzo (es. mappa di stato testuale);
      senza value_fn il valore viene convertito a float (None se non numerico).
    """

    attr_key: str
    value_fn: Callable[[object], object] | None = None


# Stato + tempo rimanente: identici per lavatrice/lavasciuga/asciugatrice.
_STATE = HonSensorEntityDescription(
    key="state",
    name="Stato",
    icon="mdi:washing-machine",
    attr_key=WM_ATTR_STATUS,
    value_fn=_wm_state,
)
_REMAINING = HonSensorEntityDescription(
    key="remaining_time",
    name="Tempo Rimanente",
    attr_key=WM_ATTR_REMAINING,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    device_class=SensorDeviceClass.DURATION,
)

# Sensori consumo lavatrice/lavasciuga (usano acqua + energia).
_WASH_CONSUMPTION: tuple[HonSensorEntityDescription, ...] = (
    HonSensorEntityDescription(
        key="total_washes",
        name="Cicli Totali",
        attr_key=WM_ATTR_TOTAL_WASH,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    HonSensorEntityDescription(
        key="total_water",
        name="Acqua Totale Consumata",
        attr_key=WM_ATTR_TOTAL_WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    HonSensorEntityDescription(
        key="total_energy",
        name="Energia Totale Consumata",
        attr_key=WM_ATTR_TOTAL_ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    HonSensorEntityDescription(
        key="current_energy",
        name="Consumo Energetico Attuale",
        attr_key=WM_ATTR_CURRENT_ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
    ),
    HonSensorEntityDescription(
        key="current_water",
        name="Consumo Acqua Attuale",
        attr_key=WM_ATTR_CURRENT_WATER,
        native_unit_of_measurement=UnitOfVolume.LITERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL,
    ),
)

# Sensori extra gruppo lavaggio (chiavi confermate live su HW80 / HD100).
# `program_name` è testo (no conversione float); i livelli sporco/asciugatura sono
# valori interi grezzi (etichette demandate a uno step successivo).
def _as_text(raw) -> str | None:
    return None if raw is None else str(raw)


_PROGRAM_NAME = HonSensorEntityDescription(
    key="program_name",
    name="Programma",
    icon="mdi:format-list-bulleted",
    attr_key=WM_ATTR_PROGRAM_NAME,
    value_fn=_as_text,
)
_DELAY = HonSensorEntityDescription(
    key="delay_time",
    name="Ritardo Avvio",
    icon="mdi:timer-sand",
    attr_key=WM_ATTR_DELAY,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    device_class=SensorDeviceClass.DURATION,
)
_LOADING = HonSensorEntityDescription(
    key="loading_percentage",
    name="Carico",
    icon="mdi:weight",
    attr_key=WM_ATTR_LOADING,
    native_unit_of_measurement="%",
    state_class=SensorStateClass.MEASUREMENT,
)
_DRY_LEVEL = HonSensorEntityDescription(
    key="dry_level",
    name="Livello Asciugatura",
    icon="mdi:tumble-dryer",
    attr_key=WM_ATTR_DRY_LEVEL,
)
# Solo lavatrice/lavasciuga (lato lavaggio).
_WASH_EXTRA: tuple[HonSensorEntityDescription, ...] = (
    HonSensorEntityDescription(
        key="spin_speed",
        name="Centrifuga",
        icon="mdi:rotate-3d-variant",
        attr_key=WM_ATTR_SPIN_SPEED,
        native_unit_of_measurement="rpm",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="wash_temperature",
        name="Temperatura Lavaggio",
        attr_key=WM_ATTR_TEMP,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="dirty_level",
        name="Livello Sporco",
        icon="mdi:liquid-spot",
        attr_key=WM_ATTR_DIRT_LEVEL,
    ),
)

# Lavatrice (WM): stato/tempo + programma + extra lavaggio + carico/ritardo + consumi.
_WASHER: tuple[HonSensorEntityDescription, ...] = (
    _STATE, _REMAINING, _PROGRAM_NAME, *_WASH_EXTRA, _LOADING, _DELAY, *_WASH_CONSUMPTION,
)
# Lavasciuga (WD = WM + asciugatura): come la lavatrice + livello asciugatura.
_WASHER_DRYER: tuple[HonSensorEntityDescription, ...] = (
    _STATE, _REMAINING, _PROGRAM_NAME, *_WASH_EXTRA, _DRY_LEVEL, _LOADING, _DELAY, *_WASH_CONSUMPTION,
)

# Asciugatrice: niente acqua/energia (hOn non li espone per la TD). I cicli
# riusano il suffisso "total_washes" ma leggono programsCounter, cosi l'entita
# gia registrata (prima sempre vuota su totalWashCycle) viene ri-puntata a un
# dato reale senza cambiare entity_id.
_DRYER: tuple[HonSensorEntityDescription, ...] = (
    _STATE,
    _REMAINING,
    _PROGRAM_NAME,
    _DRY_LEVEL,
    _LOADING,
    _DELAY,
    HonSensorEntityDescription(
        key="total_washes",
        name="Cicli Totali",
        attr_key=TD_ATTR_CYCLES,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)

# Condizionatore. NOTA energia: hOn NON fornisce kWh cumulativi per gli AC di
# classe AS (totalElectricityUsed riporta 0 dal device stesso, non e un
# placeholder nostro). Manteniamo comunque il sensore (utile su AC che lo
# riportano); per un'energia reale serve un misuratore esterno.
_AC: tuple[HonSensorEntityDescription, ...] = (
    HonSensorEntityDescription(
        key="temp_indoor",
        name="Temperatura Interna",
        attr_key=AC_ATTR_CURRENT_TEMP,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="temp_outdoor",
        name="Temperatura Esterna",
        attr_key=AC_ATTR_OUTDOOR_TEMP,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="humidity_indoor",
        name="Umidità Interna",
        attr_key=AC_ATTR_HUMIDITY_INDOOR,
        native_unit_of_measurement="%",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="compressor_freq",
        name="Frequenza Compressore",
        attr_key=AC_ATTR_COMPRESSOR_FREQ,
        native_unit_of_measurement="Hz",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="total_energy",
        name="Energia Totale Condizionatore",
        attr_key=AC_ATTR_TOTAL_ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
)

SENSORS: dict[str, tuple[HonSensorEntityDescription, ...]] = {
    APPLIANCE_AC: _AC,
    APPLIANCE_WM: _WASHER,
    APPLIANCE_WD: _WASHER_DRYER,
    APPLIANCE_TD: _DRYER,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Crea i sensori in base al tipo di ciascun elettrodomestico."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[SensorEntity] = []
    for appliance_id, data in coordinator.data.items():
        app_type = data.get("type", "")
        descriptions = SENSORS.get(app_type, ())
        _LOGGER.debug(
            "Sensori debug: '%s' (type=%s, id=%s) -> %d sensori %s",
            data.get("name", "Haier"),
            app_type,
            appliance_id,
            len(descriptions),
            [d.key for d in descriptions],
        )
        for description in descriptions:
            entities.append(HonSensor(coordinator, appliance_id, description))
    async_add_entities(entities)


class HonSensor(HonBaseEntity, SensorEntity):
    """Sensore Haier hOn guidato da HonSensorEntityDescription."""

    entity_description: HonSensorEntityDescription

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        description: HonSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, appliance_id)
        self.entity_description = description
        device_name = self._appliance_data.get("name", "Haier")
        self._attr_name = f"{device_name} - {description.name}"
        self._attr_unique_id = f"{appliance_id}_{description.key}"

    @property
    def native_value(self):
        raw = self._get_attr(self.entity_description.attr_key)
        value_fn = self.entity_description.value_fn
        if value_fn is not None:
            return value_fn(raw)
        if raw is None:
            return None
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None
