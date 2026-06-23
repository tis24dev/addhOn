"""Base entity for Haier hOn."""
from __future__ import annotations

import logging

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .debug_utils import debug_key_sample, redact_id

_LOGGER = logging.getLogger(__name__)


def account_device_info(entry, sw_version: str | None = None) -> DeviceInfo:
    """DeviceInfo for the synthetic per-account "diagnostics" device.

    One device per config entry groups the integration's debug controls (the
    process-global loggers are shared, so these are NOT per appliance). The
    identifier suffix keeps it from ever colliding with an appliance device
    (those use ``{(DOMAIN, appliance_id)}``). ``DeviceEntryType.SERVICE`` renders
    it as a service helper, distinct from the physical appliances and out of the
    room dashboards.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_diagnostics")},
        entry_type=DeviceEntryType.SERVICE,
        # Name is translated (device.diagnostics.name in en/it.json) via
        # translation_key, so it follows the UI language like the entity names
        # instead of being a hardcoded string. model has no translation hook in HA,
        # so it stays English.
        translation_key="diagnostics",
        manufacturer="addhOn",
        model="Diagnostics & debug",
        sw_version=sw_version,
        configuration_url="https://github.com/tis24dev/addhOn",
    )


class HonAccountEntity:
    """Mixin for entities bound to the per-account "diagnostics" device.

    Unlike :class:`HonBaseEntity` these are NOT appliance/coordinator bound: they
    belong to one synthetic device per config entry that collects the debug
    controls. The ``_addhon_account`` marker lets the test helpers tell them apart
    from the appliance entities built by the same platform setup.
    """

    _attr_has_entity_name = True
    _addhon_account = True

    def __init__(self, entry, key: str, sw_version: str | None = None) -> None:
        self._entry = entry
        self._sw_version = sw_version
        entry_id = getattr(entry, "entry_id", "") or ""
        self._attr_unique_id = f"{entry_id}_diag_{key}"

    @property
    def _entry_options(self) -> dict:
        return getattr(self._entry, "options", None) or {}

    @property
    def device_info(self) -> DeviceInfo:
        return account_device_info(self._entry, self._sw_version)


class HonAccountCoordinatorEntity(HonAccountEntity, CoordinatorEntity):
    """Account-device entity that also reads the coordinator state (diagnostics)."""

    def __init__(self, coordinator, entry, key: str, sw_version: str | None = None) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        HonAccountEntity.__init__(self, entry, key, sw_version)

    @property
    def available(self) -> bool:
        """Always available: these are diagnostics that must keep reporting through
        a failed refresh (e.g. the "Update OK" sensor must be able to show OFF, and
        "Last refresh" must keep its last-good value). CoordinatorEntity.available
        would hide them exactly when they are most useful."""
        return True


class HonBaseEntity(CoordinatorEntity):
    """Base entity for all Haier hOn devices."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator)
        self._appliance_id = appliance_id
        self._client = client if client is not None else getattr(coordinator, "hon_client", None)

    @property
    def _hon_client(self):
        """Return the HonClient to run commands on the dedicated loop."""
        return self._client

    @property
    def _appliance_data(self) -> dict:
        return self.coordinator.data.get(self._appliance_id, {})

    @property
    def _attributes(self) -> dict:
        return self._appliance_data.get("attributes", {})

    @property
    def _statistics(self) -> dict:
        return self._appliance_data.get("statistics", {})

    @property
    def _appliance(self):
        return self._appliance_data.get("appliance")

    def _coordinator_store(self, name: str) -> dict:
        """Volatile store shared between entities, kept on the coordinator.

        Unlike coordinator.data (recreated on every refresh), an attribute on the
        coordinator survives updates, so different entities of the same device can
        share ephemeral state (e.g. the program chosen by the select but not yet
        started, read later by the "Start" button).
        """
        store = getattr(self.coordinator, name, None)
        if not isinstance(store, dict):
            store = {}
            setattr(self.coordinator, name, store)
            _LOGGER.debug(
                "BaseEntity debug: created coordinator store '%s' for appliance=%s",
                name,
                redact_id(self._appliance_id),
            )
        return store

    @property
    def device_info(self) -> DeviceInfo:
        data = self._appliance_data
        return DeviceInfo(
            identifiers={(DOMAIN, self._appliance_id)},
            name=data.get("name", "Haier Appliance"),
            manufacturer="Haier",
            model=data.get("model", "Unknown"),
            sw_version=None,
        )

    @property
    def available(self) -> bool:
        """Per-appliance availability, beyond the coordinator's global state.

        super().available (CoordinatorEntity) only reflects the overall outcome of
        the last refresh (last_update_success): it does NOT cover the case where
        the refresh succeeds but THIS appliance disappears from coordinator.data
        (device removed from the account or temporarily not returned by the API).
        Without this check the entity would stay "available" showing stale default
        values. We keep the AND with the coordinator state and an isinstance guard
        because `x in None` would raise TypeError.

        In addition (app model): if the DEVICE is disconnected (`available` derived
        by the engine from lastConnEvent.category) the entity becomes unavailable,
        instead of showing stale values. This replaces the old engine-side offline
        zeroing. Default True if the attribute is missing (device that errored on
        its first load): do not hide it without reason.

        NB: the connectivity binary_sensor excludes the `available` gate (it must
        stay available to signal 'disconnected'): it uses `_present` directly.
        """
        return self._present and bool(self._attributes.get("available", True))

    @property
    def _present(self) -> bool:
        """Coordinator ok + this appliance present in the data, WITHOUT the
        connectivity gate. Basis for `available` and for the entities that must
        stay available even offline (connectivity)."""
        return (
            super().available
            and isinstance(self.coordinator.data, dict)
            and self._appliance_id in self.coordinator.data
        )

    def _get_attr(self, key: str, default=None):
        """Retrieve a device attribute.

        The client returns attributes as HonAttribute (with .value) or as raw values
        depending on the version. We handle both.
        """
        def _extract_value(value):
            if value is None:
                return None
            # HonAttribute has .value, note: value.value can be 0, "", False (all valid!)
            if hasattr(value, "value"):
                inner = value.value
                # Empty string = data not available, treat it as None
                if inner == "":
                    return None
                return inner
            # Raw empty string = data not available
            if value == "":
                return None
            return value

        def _deep_get(container, path: str):
            current = container
            for part in path.split("."):
                if current is None:
                    return None
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    current = getattr(current, part, None)
            return current

        def _debug_lookup(source: str, raw_value, extracted_value) -> None:
            if not _LOGGER.isEnabledFor(logging.DEBUG):
                return
            attributes = self._attributes
            settings = self._appliance_data.get("settings")
            _LOGGER.debug(
                "BaseEntity debug: lookup '%s' for '%s' (id=%s) resolved from %s: "
                "raw=%r (%s), value=%r; attribute_keys=%d %s; settings_keys=%d %s",
                key,
                redact_id(getattr(self, "_attr_unique_id", None), self._appliance_id)
                or self.__class__.__name__,
                redact_id(self._appliance_id),
                source,
                raw_value,
                type(raw_value).__name__,
                extracted_value,
                len(attributes) if isinstance(attributes, dict) else 0,
                debug_key_sample(attributes) if isinstance(attributes, dict) else [],
                len(settings) if isinstance(settings, dict) else 0,
                debug_key_sample(settings) if isinstance(settings, dict) else [],
            )

        # 1) direct lookup (already "flattened" keys)
        val = self._attributes.get(key)
        if val is not None:
            extracted = _extract_value(val)
            _debug_lookup("attributes direct", val, extracted)
            return extracted

        # 1b) lookup in the separate statistics container (e.g. TD programsCounter).
        # Normally hon_client already merges it into attributes, but this fallback
        # avoids a separate payload in the coordinator leaving the sensor empty.
        statistics = self._statistics
        if isinstance(statistics, dict):
            val = statistics.get(key)
            if val is not None:
                extracted = _extract_value(val)
                _debug_lookup("statistics direct", val, extracted)
                return extracted

            val = _deep_get(statistics, key)
            if val is not None:
                extracted = _extract_value(val)
                _debug_lookup("statistics dotted path", val, extracted)
                return extracted

        # 2) support for the "settings." prefix (some models/old versions use it)
        if key.startswith("settings."):
            key_no_prefix = key.removeprefix("settings.")
            val = self._attributes.get(key_no_prefix)
            if val is not None:
                extracted = _extract_value(val)
                _debug_lookup("attributes without settings prefix", val, extracted)
                return extracted

            val = _deep_get(self._attributes, key_no_prefix)
            if val is not None:
                extracted = _extract_value(val)
                _debug_lookup("attributes deep without settings prefix", val, extracted)
                return extracted

            settings = self._appliance_data.get("settings")
            if isinstance(settings, dict):
                val = settings.get(key_no_prefix)
                if val is not None:
                    extracted = _extract_value(val)
                    _debug_lookup("settings direct", val, extracted)
                    return extracted
                val = _deep_get(settings, key_no_prefix)
                if val is not None:
                    extracted = _extract_value(val)
                    _debug_lookup("settings deep", val, extracted)
                    return extracted

        # 2b) support for the "startProgram." prefix (e.g. ecoMode that lives in startProgram)
        if key.startswith("startProgram."):
            key_no_prefix = key.removeprefix("startProgram.")
            val = self._attributes.get(key_no_prefix)
            if val is not None:
                extracted = _extract_value(val)
                _debug_lookup("attributes without startProgram prefix", val, extracted)
                return extracted

            start_program = self._appliance_data.get("startProgram")
            if isinstance(start_program, dict):
                val = start_program.get(key_no_prefix)
                if val is not None:
                    extracted = _extract_value(val)
                    _debug_lookup("startProgram direct", val, extracted)
                    return extracted
                val = _deep_get(start_program, key_no_prefix)
                if val is not None:
                    extracted = _extract_value(val)
                    _debug_lookup("startProgram deep", val, extracted)
                    return extracted

        # 3) fallback: try a "dotted path" lookup inside attributes
        val = _deep_get(self._attributes, key)
        if val is not None:
            extracted = _extract_value(val)
            _debug_lookup("attributes dotted path", val, extracted)
            return extracted

        if _LOGGER.isEnabledFor(logging.DEBUG):
            attributes = self._attributes
            settings = self._appliance_data.get("settings")
            _LOGGER.debug(
                "BaseEntity debug: lookup '%s' for '%s' (id=%s) not found, "
                "returning default=%r; attribute_keys=%d %s; settings_keys=%d %s",
                key,
                redact_id(getattr(self, "_attr_unique_id", None), self._appliance_id)
                or self.__class__.__name__,
                redact_id(self._appliance_id),
                default,
                len(attributes) if isinstance(attributes, dict) else 0,
                debug_key_sample(attributes) if isinstance(attributes, dict) else [],
                len(settings) if isinstance(settings, dict) else 0,
                debug_key_sample(settings) if isinstance(settings, dict) else [],
            )
        return default

    async def _async_request_command_refresh(self) -> None:
        """Refresh coordinator data after a command and fail if HA stored the error."""
        refresh = getattr(self.coordinator, "async_refresh", None)
        if refresh is None:
            refresh = self.coordinator.async_request_refresh
        _LOGGER.debug(
            "BaseEntity debug: refresh requested after command for appliance=%s entity=%s",
            redact_id(self._appliance_id),
            redact_id(getattr(self, "_attr_unique_id", None), self._appliance_id)
            or self.__class__.__name__,
        )
        await refresh()
        if getattr(self.coordinator, "last_update_success", True) is not False:
            _LOGGER.debug(
                "BaseEntity debug: refresh after command succeeded for appliance=%s entity=%s",
                redact_id(self._appliance_id),
                redact_id(getattr(self, "_attr_unique_id", None), self._appliance_id)
                or self.__class__.__name__,
            )
            return

        err = getattr(self.coordinator, "last_exception", None)
        if err is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="refresh_after_command_failed",
            )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="refresh_after_command_failed_error",
            translation_placeholders={"error": str(err)},
        ) from err
