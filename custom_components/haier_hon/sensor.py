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
    APPLIANCE_DW,
    APPLIANCE_FR,
    APPLIANCE_FRE,
    APPLIANCE_HO,
    APPLIANCE_HOB,
    APPLIANCE_IH,
    APPLIANCE_KT,
    APPLIANCE_OV,
    APPLIANCE_REF,
    APPLIANCE_RVC,
    APPLIANCE_TD,
    APPLIANCE_WC,
    APPLIANCE_WD,
    APPLIANCE_WH,
    APPLIANCE_WM,
    AC_ATTR_CH2O,
    AC_ATTR_CO2,
    AC_ATTR_COMPRESSOR_FREQ,
    AC_ATTR_CURRENT_TEMP,
    AC_ATTR_HUMIDITY_INDOOR,
    AC_ATTR_OUTDOOR_TEMP,
    AC_ATTR_PM25,
    AC_ATTR_TOTAL_ENERGY,
    DOMAIN,
    DW_LEVEL_MAP,
    MACHINE_MODE_MAP,
    RVC_POWER_MAP,
    RVC_STATE_MAP,
    TD_ATTR_CYCLES,
    TUMBLE_DRYER_PHASE_MAP,
    WASHING_PHASE_MAP,
    WH_PHASE_MAP,
    WM_ATTR_CURRENT_ENERGY,
    WM_ATTR_CURRENT_WATER,
    WM_ATTR_DELAY,
    WM_ATTR_DIRT_LEVEL,
    WM_ATTR_DRY_LEVEL,
    WM_ATTR_ERRORS,
    WM_ATTR_LOADING,
    WM_ATTR_PROGRAM_NAME,
    WM_ATTR_PROGRAM_PHASE,
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
    - `gated` = se True il sensore è CAPABILITY-GATED: viene creato solo se il
      device espone davvero `attr_key` (presente in coordinator.data[id]
      ["attributes"]). Usato per i tipi Tier 2, mappati dalla app ma non
      validati live, così un parametro assente non genera entità "unknown".
      I tipi storici (AC/WM/WD/TD) restano gated=False (sempre creati).
    """

    attr_key: str
    value_fn: Callable[[object], object] | None = None
    gated: bool = False


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


def _phase_wash(raw) -> str | None:
    """prPhase -> etichetta fase (lavatrice/lavasciuga)."""
    if raw is None:
        return None
    return WASHING_PHASE_MAP.get(str(raw), f"Fase {raw}")


def _phase_dry(raw) -> str | None:
    """prPhase -> etichetta fase (asciugatrice)."""
    if raw is None:
        return None
    return TUMBLE_DRYER_PHASE_MAP.get(str(raw), f"Fase {raw}")


_PROGRAM_NAME = HonSensorEntityDescription(
    key="program_name",
    name="Programma",
    icon="mdi:format-list-bulleted",
    attr_key=WM_ATTR_PROGRAM_NAME,
    value_fn=_as_text,
)
_PHASE_WASH = HonSensorEntityDescription(
    key="program_phase",
    name="Fase",
    icon="mdi:washing-machine",
    attr_key=WM_ATTR_PROGRAM_PHASE,
    value_fn=_phase_wash,
)
_PHASE_DRY = HonSensorEntityDescription(
    key="program_phase",
    name="Fase",
    icon="mdi:tumble-dryer",
    attr_key=WM_ATTR_PROGRAM_PHASE,
    value_fn=_phase_dry,
)
_ERRORS = HonSensorEntityDescription(
    key="errors",
    name="Errori",
    icon="mdi:alert-circle-outline",
    attr_key=WM_ATTR_ERRORS,
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
    _STATE, _REMAINING, _PROGRAM_NAME, _PHASE_WASH, *_WASH_EXTRA, _LOADING, _DELAY,
    _ERRORS, *_WASH_CONSUMPTION,
)
# Lavasciuga (WD = WM + asciugatura): come la lavatrice + livello asciugatura.
_WASHER_DRYER: tuple[HonSensorEntityDescription, ...] = (
    _STATE, _REMAINING, _PROGRAM_NAME, _PHASE_WASH, *_WASH_EXTRA, _DRY_LEVEL, _LOADING,
    _DELAY, _ERRORS, *_WASH_CONSUMPTION,
)

# Asciugatrice: niente acqua/energia (hOn non li espone per la TD). I cicli
# riusano il suffisso "total_washes" ma leggono programsCounter, cosi l'entita
# gia registrata (prima sempre vuota su totalWashCycle) viene ri-puntata a un
# dato reale senza cambiare entity_id.
_DRYER: tuple[HonSensorEntityDescription, ...] = (
    _STATE,
    _REMAINING,
    _PROGRAM_NAME,
    _PHASE_DRY,
    _DRY_LEVEL,
    _LOADING,
    _DELAY,
    _ERRORS,
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
    HonSensorEntityDescription(
        key="pm25",
        name="PM2.5",
        attr_key=AC_ATTR_PM25,
        native_unit_of_measurement="µg/m³",
        device_class=SensorDeviceClass.PM25,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="co2",
        name="CO2",
        attr_key=AC_ATTR_CO2,
        native_unit_of_measurement="ppm",
        device_class=SensorDeviceClass.CO2,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    HonSensorEntityDescription(
        key="ch2o",
        name="Formaldeide",
        icon="mdi:molecule",
        attr_key=AC_ATTR_CH2O,
        native_unit_of_measurement="mg/m³",
        state_class=SensorStateClass.MEASUREMENT,
    ),
)

# ─── Tier 2: sensori read-only (capability-gated) ────────────────────────────
# Tipi mappati dalla app ufficiale ma non validati su device reali. Ogni
# description ha gated=True: l'entità viene creata solo se il device espone
# l'attributo (vedi async_setup_entry). Le `attr_key` sono i nomi-parametro hOn
# (telemetria diretta), usati una sola volta qui, quindi restano stringhe inline
# (a differenza dei tipi storici, che condividono le chiavi tra più piattaforme).


def _mapped(mapping: dict[str, str], prefix: str) -> Callable[[object], object]:
    """Costruisce una value_fn che traduce il grezzo via `mapping`.

    Valore None -> None; valore non in mappa -> "<prefix> <raw>" (così un codice
    inatteso resta visibile invece di sparire)."""

    def _fn(raw):
        if raw is None:
            return None
        return mapping.get(str(raw), f"{prefix} {raw}")

    return _fn


def _g_temp(key: str, name: str, attr: str) -> HonSensorEntityDescription:
    return HonSensorEntityDescription(
        key=key,
        name=name,
        attr_key=attr,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    )


def _g_minutes(key: str, name: str, attr: str) -> HonSensorEntityDescription:
    return HonSensorEntityDescription(
        key=key,
        name=name,
        attr_key=attr,
        icon="mdi:timer-outline",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        device_class=SensorDeviceClass.DURATION,
        gated=True,
    )


def _g_text(key: str, name: str, attr: str, icon: str | None = None,
            value_fn: Callable[[object], object] | None = _as_text) -> HonSensorEntityDescription:
    return HonSensorEntityDescription(
        key=key, name=name, attr_key=attr, icon=icon, value_fn=value_fn, gated=True,
    )


# Frigo / frigo-congelatore / congelatore (REF/FR/FRE): temperature per zona +
# ambiente. Le porte / ice-maker / eco sono binary sensor (binary_sensor.py).
_COOLING: tuple[HonSensorEntityDescription, ...] = (
    _g_temp("temp_zone1", "Temperatura Zona 1", "tempZ1"),
    _g_temp("temp_zone2", "Temperatura Zona 2", "tempZ2"),
    _g_temp("temp_zone3", "Temperatura Zona 3", "tempZ3"),
    _g_temp("temp_zone4", "Temperatura Zona 4", "tempZ4"),
    _g_temp("temp_upper", "Temperatura Zona Superiore", "tempUZ"),
    _g_temp("temp_lower", "Temperatura Zona Inferiore", "tempLZ"),
    _g_temp("temp_ambient", "Temperatura Ambiente", "tempEnv"),
    HonSensorEntityDescription(
        key="humidity_ambient",
        name="Umidità Ambiente",
        attr_key="humidityEnv",
        native_unit_of_measurement="%",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
)

# Forno (OV): stato, temperatura cavità, tempo rimanente, sonde carne.
_OVEN: tuple[HonSensorEntityDescription, ...] = (
    _g_text("state", "Stato", "machMode", icon="mdi:stove",
            value_fn=_mapped(MACHINE_MODE_MAP, "Modo")),
    _g_temp("temp_cavity", "Temperatura Forno", "temp"),
    _g_minutes("remaining_time", "Tempo Rimanente", "remainingTimeMM"),
    _g_temp("probe_temp_1", "Temperatura Sonda 1", "tempEmployedProbe1"),
    _g_temp("probe_temp_2", "Temperatura Sonda 2", "tempEmployedProbe2"),
)

# Lavastoviglie (DW): stato, programma, tempo, livelli sale/brillantante,
# temperatura, errori. La porta è binary sensor.
_DISHWASHER: tuple[HonSensorEntityDescription, ...] = (
    _g_text("state", "Stato", "machMode", icon="mdi:dishwasher",
            value_fn=_mapped(MACHINE_MODE_MAP, "Modo")),
    _g_text("program_name", "Programma", "programName", icon="mdi:format-list-bulleted"),
    _g_minutes("remaining_time", "Tempo Rimanente", "remainingTimeMM"),
    _g_text("salt_level", "Livello Sale", "saltStatus", icon="mdi:shaker-outline",
            value_fn=_mapped(DW_LEVEL_MAP, "Livello")),
    _g_text("rinse_aid_level", "Livello Brillantante", "rinseAidStatus",
            icon="mdi:water-opacity", value_fn=_mapped(DW_LEVEL_MAP, "Livello")),
    HonSensorEntityDescription(
        key="wash_temperature",
        name="Temperatura Lavaggio",
        attr_key="temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
    _g_text("errors", "Errori", "errors", icon="mdi:alert-circle-outline"),
)

# Cantinetta vino (WC): temperatura ambiente + zona. Luce/presenza sono binary.
_WINE: tuple[HonSensorEntityDescription, ...] = (
    _g_temp("temp_ambient", "Temperatura Ambiente", "tempEnv"),
    _g_temp("temp_zone2", "Temperatura Zona 2", "tempZ2"),
    _g_minutes("remaining_time", "Tempo Rimanente", "remainingTimeMM"),
)

# Piano cottura a induzione (IH/HOB): temperatura per zona di cottura. Il
# rilevamento pentola è binary sensor.
_HOB: tuple[HonSensorEntityDescription, ...] = (
    _g_temp("temp_zone1", "Temperatura Zona 1", "sensorTempZ1"),
    _g_temp("temp_zone2", "Temperatura Zona 2", "sensorTempZ2"),
    _g_temp("temp_zone3", "Temperatura Zona 3", "sensorTempZ3"),
    _g_temp("temp_zone4", "Temperatura Zona 4", "sensorTempZ4"),
    _g_temp("temp_zone5", "Temperatura Zona 5", "sensorTempZ5"),
)

# Cappa (HO): velocità ventola. Luce/allarme filtro sono binary sensor.
_HOOD: tuple[HonSensorEntityDescription, ...] = (
    HonSensorEntityDescription(
        key="fan_speed",
        name="Velocità Ventola",
        attr_key="windSpeed",
        icon="mdi:fan",
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
)

# Macchina caffè / bollitore (KT): potenza istantanea + contatori cicli.
_COFFEE: tuple[HonSensorEntityDescription, ...] = (
    HonSensorEntityDescription(
        key="current_power",
        name="Potenza",
        attr_key="currentPower",
        native_unit_of_measurement="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
    HonSensorEntityDescription(
        key="descaling_cycles",
        name="Cicli a Decalcificazione",
        attr_key="descalingCycleCounter",
        icon="mdi:counter",
        gated=True,
    ),
    HonSensorEntityDescription(
        key="lifetime_cycles",
        name="Cicli Totali",
        attr_key="lifetimeCycleCounter",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        gated=True,
    ),
)

# Scaldabagno (WH): temperature acqua/ingresso/uscita, potenza, volume
# disponibile, tempo al target, fase. Luce/blocco sono binary sensor.
_WATER_HEATER: tuple[HonSensorEntityDescription, ...] = (
    _g_temp("water_temp", "Temperatura Acqua", "temp"),
    _g_temp("temp_inlet", "Temperatura Ingresso", "tempIn"),
    _g_temp("temp_outlet", "Temperatura Uscita", "tempOut"),
    HonSensorEntityDescription(
        key="power",
        name="Potenza",
        attr_key="power",
        native_unit_of_measurement="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
    HonSensorEntityDescription(
        key="water_volume",
        name="Acqua Disponibile",
        attr_key="waterVolume",
        icon="mdi:water",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
    _g_minutes("heating_remaining", "Tempo al Target", "remainingTimeMMHeating"),
    _g_text("program_phase", "Fase", "prPhase", icon="mdi:water-boiler",
            value_fn=_mapped(WH_PHASE_MAP, "Fase")),
)

# Robot aspirapolvere (RVC): batteria, stato, tempo, potenza, aree, errori.
_VACUUM: tuple[HonSensorEntityDescription, ...] = (
    HonSensorEntityDescription(
        key="battery",
        name="Batteria",
        attr_key="batteryStatus",
        native_unit_of_measurement="%",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
    _g_text("state", "Stato", "prPhase", icon="mdi:robot-vacuum",
            value_fn=_mapped(RVC_STATE_MAP, "Stato")),
    _g_minutes("remaining_time", "Tempo Rimanente", "remainingTimeMM"),
    _g_text("power_mode", "Potenza Aspirazione", "power", icon="mdi:fan",
            value_fn=_mapped(RVC_POWER_MAP, "Potenza")),
    HonSensorEntityDescription(
        key="last_work_area",
        name="Area Ultima Pulizia",
        attr_key="lastWorkArea",
        icon="mdi:ruler-square",
        native_unit_of_measurement="m²",
        state_class=SensorStateClass.MEASUREMENT,
        gated=True,
    ),
    HonSensorEntityDescription(
        key="total_work_area",
        name="Area Totale Pulita",
        attr_key="totalWorkArea",
        icon="mdi:ruler-square",
        native_unit_of_measurement="m²",
        state_class=SensorStateClass.TOTAL_INCREASING,
        gated=True,
    ),
    _g_text("errors", "Errori", "errors", icon="mdi:alert-circle-outline"),
)

SENSORS: dict[str, tuple[HonSensorEntityDescription, ...]] = {
    APPLIANCE_AC: _AC,
    APPLIANCE_WM: _WASHER,
    APPLIANCE_WD: _WASHER_DRYER,
    APPLIANCE_TD: _DRYER,
    # Tier 2 (read-only, capability-gated). FR/FRE riusano il set frigo, HOB
    # riusa il set piano cottura (codici alias dello stesso device).
    APPLIANCE_REF: _COOLING,
    APPLIANCE_FR: _COOLING,
    APPLIANCE_FRE: _COOLING,
    APPLIANCE_OV: _OVEN,
    APPLIANCE_DW: _DISHWASHER,
    APPLIANCE_WC: _WINE,
    APPLIANCE_IH: _HOB,
    APPLIANCE_HOB: _HOB,
    APPLIANCE_HO: _HOOD,
    APPLIANCE_KT: _COFFEE,
    APPLIANCE_WH: _WATER_HEATER,
    APPLIANCE_RVC: _VACUUM,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Crea i sensori in base al tipo di ciascun elettrodomestico."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[SensorEntity] = []
    for appliance_id, data in coordinator.data.items():
        app_type = data.get("type", "")
        attributes = data.get("attributes", {})
        attributes = attributes if isinstance(attributes, dict) else {}
        descriptions = SENSORS.get(app_type, ())
        created: list[str] = []
        for description in descriptions:
            # Capability-gating (solo Tier 2): salta i sensori il cui attributo
            # non è esposto dal device. I tipi storici (gated=False) restano
            # sempre creati, come prima.
            if description.gated and description.attr_key not in attributes:
                continue
            entities.append(HonSensor(coordinator, appliance_id, description))
            created.append(description.key)
        _LOGGER.debug(
            "Sensori debug: '%s' (type=%s, id=%s) -> %d/%d sensori %s",
            data.get("name", "Haier"),
            app_type,
            appliance_id,
            len(created),
            len(descriptions),
            created,
        )
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
