"""Binary sensor Haier hOn (gruppo lavaggio): porta, blocchi, allarmi manutenzione.

Le entità sono CAPABILITY-GATED: una description viene creata solo se il device
espone davvero quell'attributo (presente in coordinator.data[id]["attributes"]),
così non compaiono entità perennemente "unknown" sui modelli che non lo riportano
(es. doorLockStatus non è garantito sull'asciugatrice). Tutte le chiavi usate qui
sono attributi diretti 0/1, confermati live su HW80 (lavatrice) / HD100 (asciugatrice).
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonBaseEntity
from .const import (
    APPLIANCE_DW,
    APPLIANCE_FR,
    APPLIANCE_FRE,
    APPLIANCE_HO,
    APPLIANCE_HOB,
    APPLIANCE_IH,
    APPLIANCE_OV,
    APPLIANCE_REF,
    APPLIANCE_TD,
    APPLIANCE_WC,
    APPLIANCE_WD,
    APPLIANCE_WH,
    APPLIANCE_WM,
    DOMAIN,
    WM_ATTR_CHILD_LOCK,
    WM_ATTR_DOOR,
    WM_ATTR_DOOR_OPEN,
    WM_ATTR_DRUM_CLEAN,
    WM_ATTR_DRY_CLEAN_NEEDED,
    WM_ATTR_FILTER_CLEAN,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HonBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Description di un binary sensor Haier hOn.

    - `key` = suffisso unique_id (nuovo, nessuna entità storica con questi suffissi).
    - `attr_key` = chiave letta via HonBaseEntity._get_attr.
    - `on_value` = valore raw che corrisponde allo stato "on" (default "1").
    """

    attr_key: str
    on_value: str = "1"


_DOOR_OPEN = HonBinarySensorEntityDescription(
    key="door_open",
    name="Porta",
    attr_key=WM_ATTR_DOOR_OPEN,           # doorStatus: 1 = aperta
    device_class=BinarySensorDeviceClass.DOOR,
)
_DOOR_LOCK = HonBinarySensorEntityDescription(
    key="door_lock",
    name="Oblò Bloccato",
    icon="mdi:lock",
    attr_key=WM_ATTR_DOOR,                # doorLockStatus: 1 = bloccato
)
_CHILD_LOCK = HonBinarySensorEntityDescription(
    key="child_lock",
    name="Blocco Comandi",
    icon="mdi:lock-alert",
    attr_key=WM_ATTR_CHILD_LOCK,         # lockStatus: 1 = attivo
)
_DRUM_CLEAN = HonBinarySensorEntityDescription(
    key="drum_clean_needed",
    name="Pulizia Cestello",
    attr_key=WM_ATTR_DRUM_CLEAN,
    device_class=BinarySensorDeviceClass.PROBLEM,
)
_FILTER_CLEAN = HonBinarySensorEntityDescription(
    key="filter_clean_needed",
    name="Pulizia Filtro",
    attr_key=WM_ATTR_FILTER_CLEAN,
    device_class=BinarySensorDeviceClass.PROBLEM,
)
_DRY_CLEAN = HonBinarySensorEntityDescription(
    key="dry_clean_needed",
    name="Pulizia Condensatore",
    attr_key=WM_ATTR_DRY_CLEAN_NEEDED,
    device_class=BinarySensorDeviceClass.PROBLEM,
)

# Set per-tipo (candidati; il capability-gate scarta quelli non presenti sul device).
_WASH_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    _DOOR_OPEN, _DOOR_LOCK, _CHILD_LOCK, _DRUM_CLEAN, _FILTER_CLEAN, _DRY_CLEAN,
)
_DRY_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    _DOOR_OPEN, _DOOR_LOCK, _CHILD_LOCK,
)


# ─── Tier 2: binary sensor (capability-gated come tutti i binary) ────────────
# Chiavi inline = nomi-parametro hOn (telemetria 0/1) dei tipi mappati ma non
# validati live. Il gate per attributo (già attivo per tutti i binary sensor)
# scarta automaticamente quelli che un dato modello non riporta.


def _door(key: str, name: str, attr: str) -> HonBinarySensorEntityDescription:
    return HonBinarySensorEntityDescription(
        key=key, name=name, attr_key=attr, device_class=BinarySensorDeviceClass.DOOR,
    )


# Frigo / frigo-congelatore / congelatore (REF/FR/FRE).
_COOLING_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    _door("door_zone1", "Porta Zona 1", "doorStatusZ1"),
    _door("door2_zone1", "Porta 2 Zona 1", "door2StatusZ1"),
    _door("door_zone2", "Porta Zona 2", "doorStatusZ2"),
    _door("door_zone3", "Porta Zona 3", "doorStatusZ3"),
    HonBinarySensorEntityDescription(
        key="ice_maker",
        name="Produzione Ghiaccio",
        icon="mdi:snowflake",
        attr_key="icemakerOnOffStatus",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    HonBinarySensorEntityDescription(
        key="ice_box_full",
        name="Contenitore Ghiaccio Pieno",
        attr_key="icemakerIceboxFullStatus",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    HonBinarySensorEntityDescription(
        key="energy_saving",
        name="Risparmio Energetico",
        icon="mdi:leaf",
        attr_key="energySavingStatus",
    ),
)

# Forno (OV).
_OVEN_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    _door("door_open", "Porta", "doorStatus"),
    _door("door_zone1", "Porta Cavità 1", "doorStatusZ1"),
    _door("door_zone2", "Porta Cavità 2", "doorStatusZ2"),
)

# Lavastoviglie (DW).
_DISHWASHER_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    _door("door_open", "Porta", "doorStatus"),
)

# Cantinetta vino (WC).
_WINE_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    HonBinarySensorEntityDescription(
        key="light",
        name="Luce",
        attr_key="lightStatus",
        device_class=BinarySensorDeviceClass.LIGHT,
    ),
    HonBinarySensorEntityDescription(
        key="presence",
        name="Presenza",
        attr_key="humanSensingResult",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
    ),
)

# Piano cottura (IH/HOB): pentola rilevata per zona.
_HOB_BINARY: tuple[HonBinarySensorEntityDescription, ...] = tuple(
    HonBinarySensorEntityDescription(
        key=f"pan_zone{z}",
        name=f"Pentola Zona {z}",
        icon="mdi:pot-steam",
        attr_key=f"panStatusZ{z}",
    )
    for z in range(1, 7)
)

# Cappa (HO).
_HOOD_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    HonBinarySensorEntityDescription(
        key="light",
        name="Luce",
        attr_key="lightStatus",
        device_class=BinarySensorDeviceClass.LIGHT,
    ),
    HonBinarySensorEntityDescription(
        key="filter_clean_needed",
        name="Pulizia Filtro",
        attr_key="filterCleaningAlarmStatus",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
)

# Scaldabagno (WH).
_WATER_HEATER_BINARY: tuple[HonBinarySensorEntityDescription, ...] = (
    HonBinarySensorEntityDescription(
        key="light",
        name="Spia",
        attr_key="lightStatus",
        device_class=BinarySensorDeviceClass.LIGHT,
    ),
    HonBinarySensorEntityDescription(
        key="child_lock",
        name="Blocco Comandi",
        icon="mdi:lock-alert",
        attr_key="lockStatus",
    ),
)

BINARY_SENSORS: dict[str, tuple[HonBinarySensorEntityDescription, ...]] = {
    APPLIANCE_WM: _WASH_BINARY,
    APPLIANCE_WD: _WASH_BINARY,
    APPLIANCE_TD: _DRY_BINARY,
    # Tier 2 (read-only). FR/FRE riusano il set frigo, HOB il set piano cottura.
    APPLIANCE_REF: _COOLING_BINARY,
    APPLIANCE_FR: _COOLING_BINARY,
    APPLIANCE_FRE: _COOLING_BINARY,
    APPLIANCE_OV: _OVEN_BINARY,
    APPLIANCE_DW: _DISHWASHER_BINARY,
    APPLIANCE_WC: _WINE_BINARY,
    APPLIANCE_IH: _HOB_BINARY,
    APPLIANCE_HOB: _HOB_BINARY,
    APPLIANCE_HO: _HOOD_BINARY,
    APPLIANCE_WH: _WATER_HEATER_BINARY,
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Crea i binary sensor solo per le chiavi effettivamente esposte dal device."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[BinarySensorEntity] = []
    for appliance_id, data in coordinator.data.items():
        app_type = data.get("type", "")
        attributes = data.get("attributes", {})
        attributes = attributes if isinstance(attributes, dict) else {}
        created: list[str] = []
        for description in BINARY_SENSORS.get(app_type, ()):
            if description.attr_key not in attributes:
                _LOGGER.debug(
                    "Binary debug: skip '%s' su '%s' id=%s (chiave '%s' assente)",
                    description.key, data.get("name"), appliance_id, description.attr_key,
                )
                continue
            entities.append(HonBinarySensor(coordinator, appliance_id, description))
            created.append(description.key)
        _LOGGER.debug(
            "Binary debug: '%s' (type=%s, id=%s) -> %d binary sensor %s",
            data.get("name"), app_type, appliance_id, len(created), created,
        )
    async_add_entities(entities)


class HonBinarySensor(HonBaseEntity, BinarySensorEntity):
    """Binary sensor Haier hOn guidato da HonBinarySensorEntityDescription."""

    entity_description: HonBinarySensorEntityDescription

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        description: HonBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, appliance_id)
        self.entity_description = description
        device_name = self._appliance_data.get("name", "Haier")
        self._attr_name = f"{device_name} - {description.name}"
        self._attr_unique_id = f"{appliance_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        raw = self._get_attr(self.entity_description.attr_key)
        if raw is None:
            return None
        return str(raw) == self.entity_description.on_value
