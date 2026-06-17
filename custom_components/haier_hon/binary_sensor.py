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
    APPLIANCE_TD,
    APPLIANCE_WD,
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

BINARY_SENSORS: dict[str, tuple[HonBinarySensorEntityDescription, ...]] = {
    APPLIANCE_WM: _WASH_BINARY,
    APPLIANCE_WD: _WASH_BINARY,
    APPLIANCE_TD: _DRY_BINARY,
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
