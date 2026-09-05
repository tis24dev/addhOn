# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Home Assistant storage lifecycle for validated command catalogs."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = "addhon_command_catalog_"
_REMOVE_ATTEMPTS = 3
_REMOVE_RETRY_DELAY_SECONDS = 0.1


class CommandCatalogStore:
    """Persist one config entry's catalog repository on Home Assistant's loop."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        from homeassistant.helpers.storage import Store

        self._hass = hass
        # private=True: the records are keyed by MAC address and carry the appliance
        # code, firmware/series identifiers and the raw cloud catalog. Every other
        # surface in this integration redacts that identity (logs, diagnostics,
        # census), and this file is the one place it lands on disk -- and in every
        # Home Assistant backup.
        self._store = Store(
            hass,
            STORAGE_VERSION,
            f"{STORAGE_KEY_PREFIX}{entry_id}",
            private=True,
        )
        self._saved_generation = 0

    async def async_load(self) -> dict[str, Any]:
        """Load a plain cache document, degrading corrupt storage to empty."""
        try:
            loaded = await self._store.async_load()
        except Exception as error:  # noqa: BLE001 - a corrupt cache degrades, never raises
            _LOGGER.debug(
                "Command catalog cache load failed (%s)", type(error).__name__
            )
            return {}
        return loaded if isinstance(loaded, dict) else {}

    async def async_sync(self, client: Any) -> None:
        """Persist a new repository generation without crossing event-loop owners."""
        try:
            snapshot = await self._hass.async_add_executor_job(
                client.command_catalog_cache_snapshot, self._saved_generation
            )
            generation = snapshot.generation
            document = snapshot.document
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a cache read must not fail a poll
            _LOGGER.debug(
                "Command catalog cache snapshot failed (%s)", type(error).__name__
            )
            return
        # `document is None` is the repository saying "unchanged since the generation
        # you passed", which is the common case on every poll after the first.
        if document is None or generation == self._saved_generation:
            return
        try:
            await self._store.async_save(document)
        except Exception as error:  # noqa: BLE001 - an unwritable cache is not an outage
            _LOGGER.debug(
                "Command catalog cache save failed (%s)", type(error).__name__
            )
            return
        self._saved_generation = generation

    async def async_remove(self) -> None:
        """Remove only this config entry's command-catalog document."""
        for attempt in range(1, _REMOVE_ATTEMPTS + 1):
            try:
                await self._store.async_remove()
                return
            except Exception as error:  # noqa: BLE001 - removal must not block teardown
                if attempt == _REMOVE_ATTEMPTS:
                    _LOGGER.warning(
                        "Command catalog cache removal failed after %d attempts (%s)",
                        _REMOVE_ATTEMPTS,
                        type(error).__name__,
                    )
                    raise RuntimeError(
                        "Command catalog cache removal failed"
                    ) from None
                _LOGGER.debug(
                    "Command catalog cache removal failed; retrying "
                    "(attempt %d/%d, %s)",
                    attempt,
                    _REMOVE_ATTEMPTS,
                    type(error).__name__,
                )
                await asyncio.sleep(_REMOVE_RETRY_DELAY_SECONDS)
