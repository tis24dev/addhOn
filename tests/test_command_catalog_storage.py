# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the Home Assistant-owned command-catalog storage adapter."""

from __future__ import annotations

import asyncio
import copy
import logging
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


class FakeStore:
    """Shared in-memory stand-in for Home Assistant's per-key Store."""

    constructed: tuple[object, int, str] | None = None
    private: bool | None = None
    documents: dict[str, object] = {}
    load_error: Exception | None = None
    save_errors: list[Exception | None] = []
    remove_error: Exception | None = None
    saves: list[tuple[str, object]] = []
    removals: list[str] = []

    def __init__(
        self, hass: object, version: int, key: str, *, private: bool = False
    ) -> None:
        type(self).constructed = (hass, version, key)
        type(self).private = private
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
        cls.private = None
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
        self.since_values: list[object] = []

    def command_catalog_cache_snapshot(self, since: object = None) -> object:
        self.snapshot_reads += 1
        self.since_values.append(since)
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
        # The document is keyed by MAC and carries firmware/series identifiers plus the
        # raw cloud catalog, so the file must be marked private. Pinned here because
        # nothing else in the suite would notice the flag being dropped.
        self.assertIs(True, FakeStore.private)
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
        # The already-persisted generation is handed to the repository so an unchanged
        # one can answer without deep-copying every cached catalog. Poll 1 has nothing
        # saved yet (0); polls 2 and 3 ask about what they just saved (1).
        self.assertEqual([0, 0, 1], client.since_values)
        self.assertEqual(
            [
                (
                    "addhon_command_catalog_entry-1",
                    {"version": 1, "records": {"fresh": {}}},
                )
            ],
            FakeStore.saves,
        )

    async def test_an_unchanged_repository_is_not_saved_again(self) -> None:
        # What the `since` hand-off buys: the repository answers "nothing new" with no
        # document at all, and the store must treat that as a no-op rather than writing
        # None over a good file.
        adapter = CommandCatalogStore(self.hass, "entry-1")
        client = FakeClient(
            _snapshot(1, "fresh"),
            types.SimpleNamespace(generation=1, document=None),
        )

        await adapter.async_sync(client)
        await adapter.async_sync(client)

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

    async def test_snapshot_failure_is_best_effort_and_redacted(self) -> None:
        class FailingSnapshotClient:
            def command_catalog_cache_snapshot(self, since: object = None) -> object:
                raise RuntimeError("private snapshot detail")

        adapter = CommandCatalogStore(self.hass, "entry-1")

        with self.assertLogs(
            "custom_components.addhon.command_catalog_store", logging.DEBUG
        ) as captured:
            await adapter.async_sync(FailingSnapshotClient())

        self.assertEqual([], FakeStore.saves)
        self.assertEqual(
            [
                "DEBUG:custom_components.addhon.command_catalog_store:"
                "Command catalog cache snapshot failed (RuntimeError)"
            ],
            captured.output,
        )
        self.assertNotIn("private snapshot detail", "\n".join(captured.output))

    async def test_snapshot_cancellation_propagates(self) -> None:
        class CancelledSnapshotClient:
            def command_catalog_cache_snapshot(self, since: object = None) -> object:
                raise asyncio.CancelledError

        adapter = CommandCatalogStore(self.hass, "entry-1")

        with self.assertRaises(asyncio.CancelledError):
            await adapter.async_sync(CancelledSnapshotClient())

    async def test_cancelled_initial_sync_closes_started_client(self) -> None:
        from custom_components import addhon

        class Entry:
            entry_id = "entry-1"
            title = "Account"
            options: dict[str, object] = {}
            data = {"email": "user@example.com", "password": "secret"}

        class Hass:
            config = types.SimpleNamespace(language="en")

            async def async_add_executor_job(self, target, *args):
                return target(*args)

        class Client:
            instance = None

            def __init__(self, **_kwargs) -> None:
                type(self).instance = self
                self.closed = False

            def setup_sync(self) -> None:
                return None

            async def async_close(self) -> None:
                self.closed = True

        class StoreAdapter:
            def __init__(self, _hass, _entry_id) -> None:
                return None

            async def async_load(self) -> dict[str, object]:
                return {}

            async def async_sync(self, _client) -> None:
                raise asyncio.CancelledError

        with (
            patch.object(addhon, "_async_register_services"),
            patch.object(addhon, "_apply_debug_options"),
            patch("custom_components.addhon.hon_client.HonClient", Client),
            patch(
                "custom_components.addhon.command_catalog_store.CommandCatalogStore",
                StoreAdapter,
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await addhon.async_setup_entry(Hass(), Entry())

        self.assertIsNotNone(Client.instance)
        assert Client.instance is not None
        self.assertTrue(Client.instance.closed)

    async def test_a_successful_poll_persists_the_cache_through_the_real_setup(
        self,
    ) -> None:
        """Execute the poll closure `async_setup_entry` actually builds.

        The lifecycle guards in tests/test_coordinator_config_entry.py parse the source
        and compare line numbers, so they hold for any arrangement of the same tokens:
        wrapping the sync in `if False:`, moving it into an `except` branch or dropping
        the `await` leaves them green while the on-disk cache silently stops updating.
        This runs the closure instead, and the assertion is a real save.
        """
        from custom_components import addhon

        captured: dict[str, object] = {}

        class _CoordinatorBuilt(BaseException):
            """Stops setup once the poll closure exists; not a failure mode.

            BaseException on purpose: setup wraps the coordinator construction in a
            broad `except Exception` that would file this sentinel as a setup failure.
            """

        class CapturingCoordinator:
            def __init__(self, _hass, _logger, **kwargs) -> None:
                captured["update_method"] = kwargs["update_method"]
                raise _CoordinatorBuilt

        class Entry:
            entry_id = "entry-1"
            title = "Account"
            options: dict[str, object] = {}
            data = {"email": "user@example.com", "password": "secret"}

        class Hass:
            config = types.SimpleNamespace(language="en")
            data: dict = {}

            async def async_add_executor_job(self, target, *args):
                return target(*args)

        class Client:
            def __init__(self, **_kwargs) -> None:
                self._generation = 0

            def setup_sync(self) -> None:
                return None

            async def async_close(self) -> None:
                return None

            async def async_get_appliances_data(self) -> dict:
                # A poll that changed something: the repository generation advances.
                self._generation += 1
                return {}

            def command_catalog_cache_snapshot(self, since: object = None) -> object:
                if since == self._generation:
                    return types.SimpleNamespace(
                        generation=self._generation, document=None
                    )
                return types.SimpleNamespace(
                    generation=self._generation,
                    document={
                        "version": 1,
                        "records": {"poll-%d" % self._generation: {}},
                    },
                )

        with (
            patch.object(addhon, "_async_register_services"),
            patch.object(addhon, "_apply_debug_options"),
            patch.object(addhon, "_persist_refresh_token"),
            patch.object(addhon, "DataUpdateCoordinator", CapturingCoordinator),
            patch("custom_components.addhon.hon_client.HonClient", Client),
        ):
            with self.assertRaises(_CoordinatorBuilt):
                await addhon.async_setup_entry(Hass(), Entry())

            # Setup syncs too, but nothing is cached yet at generation 0, so the
            # repository correctly reports "nothing new" and no file is written.
            self.assertEqual([], FakeStore.saves)

            update_method = captured["update_method"]
            assert callable(update_method)
            await update_method()

        # THE assertion: one poll, one advanced generation, one real save.
        self.assertEqual(
            [
                (
                    "addhon_command_catalog_entry-1",
                    {"version": 1, "records": {"poll-1": {}}},
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

    async def test_remove_retries_a_transient_failure(self) -> None:
        adapter = CommandCatalogStore(self.hass, "entry-1")
        remove = AsyncMock(side_effect=[RuntimeError("private store detail"), None])
        adapter._store.async_remove = remove

        with self.assertLogs(
            "custom_components.addhon.command_catalog_store", logging.DEBUG
        ) as captured:
            await adapter.async_remove()

        self.assertEqual(2, remove.await_count)
        self.assertEqual(
            [
                "DEBUG:custom_components.addhon.command_catalog_store:"
                "Command catalog cache removal failed; retrying "
                "(attempt 1/3, RuntimeError)"
            ],
            captured.output,
        )
        self.assertNotIn("private store detail", "\n".join(captured.output))

    async def test_remove_propagates_sanitized_error_after_three_failures(self) -> None:
        adapter = CommandCatalogStore(self.hass, "entry-1")
        remove = AsyncMock(side_effect=RuntimeError("private store detail"))
        adapter._store.async_remove = remove

        with self.assertLogs(
            "custom_components.addhon.command_catalog_store", logging.WARNING
        ) as captured:
            with self.assertRaisesRegex(
                RuntimeError, "^Command catalog cache removal failed$"
            ):
                await adapter.async_remove()

        self.assertEqual(3, remove.await_count)
        self.assertEqual(
            [
                "WARNING:custom_components.addhon.command_catalog_store:"
                "Command catalog cache removal failed after 3 attempts (RuntimeError)"
            ],
            captured.output,
        )
        self.assertNotIn("private store detail", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
