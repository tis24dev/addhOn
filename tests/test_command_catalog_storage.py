# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the Home Assistant-owned command-catalog storage adapter."""

from __future__ import annotations

import copy
import logging
import sys
import types
import unittest


class FakeStore:
    """Shared in-memory stand-in for Home Assistant's per-key Store."""

    constructed: tuple[object, int, str] | None = None
    documents: dict[str, object] = {}
    load_error: Exception | None = None
    save_errors: list[Exception | None] = []
    remove_error: Exception | None = None
    saves: list[tuple[str, object]] = []
    removals: list[str] = []

    def __init__(self, hass: object, version: int, key: str) -> None:
        type(self).constructed = (hass, version, key)
        self._key = key

    async def async_load(self) -> object:
        if type(self).load_error is not None:
            raise type(self).load_error
        return copy.deepcopy(type(self).documents.get(self._key))

    async def async_save(self, document: object) -> None:
        error = type(self).save_errors.pop(0) if type(self).save_errors else None
        if error is not None:
            raise error
        saved = copy.deepcopy(document)
        type(self).saves.append((self._key, saved))
        type(self).documents[self._key] = saved

    async def async_remove(self) -> None:
        if type(self).remove_error is not None:
            raise type(self).remove_error
        type(self).removals.append(self._key)
        type(self).documents.pop(self._key, None)

    @classmethod
    def reset(cls) -> None:
        cls.constructed = None
        cls.documents = {}
        cls.load_error = None
        cls.save_errors = []
        cls.remove_error = None
        cls.saves = []
        cls.removals = []


def _module(name: str) -> types.ModuleType:
    return sys.modules.setdefault(name, types.ModuleType(name))


def _install_storage_stub() -> None:
    homeassistant = _module("homeassistant")
    config_entries = _module("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )
    core = _module("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exceptions = _module("homeassistant.exceptions")
    base_error = getattr(exceptions, "HomeAssistantError", Exception)
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions,
        "ConfigEntryAuthFailed",
        type("ConfigEntryAuthFailed", (base_error,), {}),
    )
    exceptions.ConfigEntryNotReady = getattr(
        exceptions,
        "ConfigEntryNotReady",
        type("ConfigEntryNotReady", (base_error,), {}),
    )
    helpers = _module("homeassistant.helpers")
    coordinator = _module("homeassistant.helpers.update_coordinator")
    coordinator.DataUpdateCoordinator = getattr(
        coordinator,
        "DataUpdateCoordinator",
        type("DataUpdateCoordinator", (), {}),
    )
    coordinator.UpdateFailed = getattr(
        coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {})
    )
    storage = sys.modules.setdefault(
        "homeassistant.helpers.storage",
        types.ModuleType("homeassistant.helpers.storage"),
    )
    homeassistant.config_entries = config_entries
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    homeassistant.helpers = helpers
    helpers.update_coordinator = coordinator
    helpers.storage = storage


_install_storage_stub()

from custom_components.addhon.command_catalog_store import (  # noqa: E402
    CommandCatalogStore,
)


class FakeHass:
    def __init__(self) -> None:
        self.executor_jobs: list[object] = []

    async def async_add_executor_job(self, target, *args):
        self.executor_jobs.append(target)
        return target(*args)


class FakeClient:
    def __init__(self, *snapshots: object) -> None:
        self._snapshots = list(snapshots)
        self.snapshot_reads = 0

    def command_catalog_cache_snapshot(self) -> object:
        self.snapshot_reads += 1
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]


def _snapshot(generation: int, marker: str) -> object:
    return types.SimpleNamespace(
        generation=generation,
        document={"version": 1, "records": {marker: {}}},
    )


class CommandCatalogStoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeStore.reset()
        self.hass = FakeHass()
        storage = sys.modules["homeassistant.helpers.storage"]
        previous = getattr(storage, "Store", None)
        existed = hasattr(storage, "Store")
        storage.Store = FakeStore

        def _restore_store() -> None:
            if existed:
                storage.Store = previous
            else:
                delattr(storage, "Store")

        self.addCleanup(_restore_store)

    async def test_store_key_and_load_are_scoped_to_the_config_entry(self) -> None:
        persisted_document = {"version": 1, "records": {"private": {}}}
        FakeStore.documents["addhon_command_catalog_entry-1"] = persisted_document

        adapter = CommandCatalogStore(self.hass, "entry-1")

        self.assertEqual(
            (self.hass, 1, "addhon_command_catalog_entry-1"),
            FakeStore.constructed,
        )
        self.assertEqual(persisted_document, await adapter.async_load())

    async def test_invalid_or_unreadable_load_degrades_to_an_empty_document(self) -> None:
        FakeStore.documents["addhon_command_catalog_entry-1"] = ["not", "a", "dict"]
        adapter = CommandCatalogStore(self.hass, "entry-1")
        self.assertEqual({}, await adapter.async_load())

        FakeStore.load_error = RuntimeError("private store detail")
        with self.assertLogs(
            "custom_components.addhon.command_catalog_store", logging.DEBUG
        ) as captured:
            self.assertEqual({}, await adapter.async_load())
        self.assertEqual(
            [
                "DEBUG:custom_components.addhon.command_catalog_store:"
                "Command catalog cache load failed (RuntimeError)"
            ],
            captured.output,
        )

    async def test_snapshot_runs_in_executor_and_zero_or_repeated_generation_is_skipped(
        self,
    ) -> None:
        adapter = CommandCatalogStore(self.hass, "entry-1")
        client = FakeClient(
            _snapshot(0, "zero"),
            _snapshot(1, "fresh"),
            _snapshot(1, "repeated"),
        )

        await adapter.async_sync(client)
        await adapter.async_sync(client)
        await adapter.async_sync(client)

        self.assertEqual(3, client.snapshot_reads)
        self.assertEqual(
            [client.command_catalog_cache_snapshot] * 3,
            self.hass.executor_jobs,
        )
        self.assertEqual(
            [
                (
                    "addhon_command_catalog_entry-1",
                    {"version": 1, "records": {"fresh": {}}},
                )
            ],
            FakeStore.saves,
        )

    async def test_failed_save_retries_the_same_generation(self) -> None:
        adapter = CommandCatalogStore(self.hass, "entry-1")
        client = FakeClient(_snapshot(1, "fresh"))
        FakeStore.save_errors = [RuntimeError("private store detail"), None]

        with self.assertLogs(
            "custom_components.addhon.command_catalog_store", logging.DEBUG
        ) as captured:
            await adapter.async_sync(client)
        self.assertEqual([], FakeStore.saves)
        self.assertEqual(
            [
                "DEBUG:custom_components.addhon.command_catalog_store:"
                "Command catalog cache save failed (RuntimeError)"
            ],
            captured.output,
        )

        await adapter.async_sync(client)
        await adapter.async_sync(client)

        self.assertEqual(3, client.snapshot_reads)
        self.assertEqual(
            [
                (
                    "addhon_command_catalog_entry-1",
                    {"version": 1, "records": {"fresh": {}}},
                )
            ],
            FakeStore.saves,
        )

    async def test_remove_deletes_only_this_entries_store(self) -> None:
        FakeStore.documents = {
            "addhon_command_catalog_entry-1": {"version": 1},
            "addhon_command_catalog_entry-2": {"version": 1},
        }
        adapter = CommandCatalogStore(self.hass, "entry-1")

        await adapter.async_remove()

        self.assertEqual(["addhon_command_catalog_entry-1"], FakeStore.removals)
        self.assertNotIn("addhon_command_catalog_entry-1", FakeStore.documents)
        self.assertIn("addhon_command_catalog_entry-2", FakeStore.documents)

    async def test_remove_failure_is_best_effort_and_redacted(self) -> None:
        adapter = CommandCatalogStore(self.hass, "entry-1")
        FakeStore.remove_error = RuntimeError("private store detail")

        with self.assertLogs(
            "custom_components.addhon.command_catalog_store", logging.DEBUG
        ) as captured:
            await adapter.async_remove()

        self.assertEqual([], FakeStore.removals)
        self.assertEqual(
            [
                "DEBUG:custom_components.addhon.command_catalog_store:"
                "Command catalog cache removal failed (RuntimeError)"
            ],
            captured.output,
        )


if __name__ == "__main__":
    unittest.main()
