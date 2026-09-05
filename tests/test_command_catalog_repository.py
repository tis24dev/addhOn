# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contract tests for the pure command-catalog repository."""

from __future__ import annotations

import copy
import json
import sys
import types
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _mod(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_stubs() -> None:
    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exceptions = _mod("homeassistant.exceptions")
    base = getattr(
        exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {})
    )
    exceptions.HomeAssistantError = base
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base,), {})
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base,), {})
    )
    coordinator = _mod("homeassistant.helpers.update_coordinator")
    coordinator.DataUpdateCoordinator = getattr(
        coordinator,
        "DataUpdateCoordinator",
        type("DataUpdateCoordinator", (), {}),
    )
    coordinator.UpdateFailed = getattr(
        coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {})
    )
    homeassistant = _mod("homeassistant")
    homeassistant.config_entries = config_entries
    homeassistant.core = core
    homeassistant.exceptions = exceptions
    homeassistant.helpers = _mod("homeassistant.helpers")
    homeassistant.helpers.update_coordinator = coordinator


_install_stubs()

from custom_components.addhon import diagnostics
from custom_components.addhon.client.catalog_repository import (
    CACHE_SCHEMA_VERSION,
    CATALOG_ENRICHMENTS,
    CATALOG_FAILURES,
    CATALOG_REQUEST_FLAGS,
    CATALOG_SOURCES,
    CommandCatalogRepository,
)
from custom_components.addhon.client.transport.command_catalog import (
    CATALOG_OUTCOMES,
    CommandCatalogRequest,
)


NOW = 1_700_000_000


def _request(**overrides: Any) -> CommandCatalogRequest:
    request = CommandCatalogRequest(
        appliance_type="FRE",
        appliance_model_id="4321",
        mac_address="AA:BB",
        code="CODE123",
        firmware_id="EE",
        firmware_version="1.2",
        series="S",
        series_version="V7",
        language="it",
    )
    return replace(request, **overrides)


def _catalog() -> dict[str, Any]:
    return {
        "applianceModel": {"attributes": []},
        "settings": {
            "setParameters": {
                "temp": {"description": "d", "protocolType": "p"}
            }
        },
    }


class CommandCatalogRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = CommandCatalogRepository(
            None, language="it-IT", clock=lambda: NOW
        )

    def test_first_replacement_has_exact_document_shape_and_digest(self) -> None:
        self.assertEqual(self.repo.language, "it")

        self.repo.replace(_request(), _catalog())
        snapshot = self.repo.snapshot()

        self.assertEqual(snapshot.generation, 1)
        self.assertEqual(snapshot.document["version"], CACHE_SCHEMA_VERSION)
        self.assertEqual(set(snapshot.document), {"version", "records"})
        record = snapshot.document["records"]["AA:BB"]
        self.assertEqual(
            record,
            {
                "version": CACHE_SCHEMA_VERSION,
                "appliance_type": "FRE",
                "appliance_model_id": "4321",
                "firmware_id": "EE",
                "firmware_version": "1.2",
                "series": "S",
                "series_version": "V7",
                "language": "it",
                "stored_at": NOW,
                "payload": _catalog(),
                "digest": (
                    "8c2b8b39447f3df3852da73cb603dc0c"
                    "5e5e367fe78e8c7e2f88f7481ec7af62"
                ),
            },
        )
        with self.assertRaises(AttributeError):
            self.repo.language = "en"  # type: ignore[misc]

    def test_all_outward_values_are_defensive_copies(self) -> None:
        payload = _catalog()
        self.repo.replace(_request(), payload)
        payload["settings"].clear()

        first = self.repo.lookup(_request())
        self.assertIsNotNone(first)
        assert first is not None
        first.payload["settings"].clear()

        snapshot = self.repo.snapshot()
        snapshot.document["records"]["AA:BB"]["payload"]["settings"].clear()

        second = self.repo.lookup(_request())
        self.assertIsNotNone(second)
        assert second is not None
        self.assertIn("setParameters", second.payload["settings"])

    def test_generation_changes_only_for_changed_valid_content(self) -> None:
        payload = _catalog()
        self.assertTrue(self.repo.replace(_request(), payload))
        self.assertFalse(self.repo.replace(_request(), copy.deepcopy(payload)))
        self.assertEqual(self.repo.snapshot().generation, 1)

        payload["settings"]["setParameters"]["temp"]["description"] = "changed"
        self.assertTrue(self.repo.replace(_request(), payload))
        self.assertEqual(self.repo.snapshot().generation, 2)

    def test_lookup_requires_exact_identity_and_current_discriminators(self) -> None:
        self.repo.replace(_request(), _catalog())
        mismatches = (
            {"appliance_type": "REF"},
            {"appliance_model_id": "9999"},
            {"language": "en"},
            {"firmware_id": "OTHER"},
            {"firmware_version": "9.9"},
            {"series": "OTHER"},
            {"series_version": "OTHER"},
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                self.assertIsNone(self.repo.lookup(_request(**mismatch)))

    def test_missing_current_optional_discriminator_is_a_degraded_match(self) -> None:
        self.repo.replace(_request(), _catalog())

        for name in (
            "firmware_id",
            "firmware_version",
            "series",
            "series_version",
        ):
            with self.subTest(name=name):
                cached = self.repo.lookup(_request(**{name: None}))
                self.assertIsNotNone(cached)
                assert cached is not None
                self.assertTrue(cached.degraded_match)

        exact = self.repo.lookup(_request())
        self.assertIsNotNone(exact)
        assert exact is not None
        self.assertFalse(exact.degraded_match)

    def test_lookup_reports_nonnegative_age(self) -> None:
        self.repo.replace(_request(), _catalog())
        self.repo._clock = lambda: NOW + 17  # type: ignore[attr-defined]

        cached = self.repo.lookup(_request())

        self.assertIsNotNone(cached)
        assert cached is not None
        self.assertEqual(cached.age_seconds, 17)

    def test_bad_persisted_documents_and_records_are_ignored(self) -> None:
        self.repo.replace(_request(), _catalog())
        valid = self.repo.snapshot().document
        mutations = {
            "schema": lambda document: document.__setitem__("version", 2),
            "records": lambda document: document.__setitem__("records", []),
            "payload": lambda document: document["records"]["AA:BB"].__setitem__(
                "payload", []
            ),
            "timestamp": lambda document: document["records"]["AA:BB"].__setitem__(
                "stored_at", "yesterday"
            ),
            "digest": lambda document: document["records"]["AA:BB"].__setitem__(
                "digest", "0" * 64
            ),
        }

        for name, mutate in mutations.items():
            with self.subTest(name=name):
                document = copy.deepcopy(valid)
                mutate(document)
                loaded = CommandCatalogRepository(
                    document, language="it", clock=lambda: NOW
                )
                self.assertIsNone(loaded.lookup(_request()))
                self.assertEqual(loaded.snapshot().document["records"], {})
                self.assertEqual(loaded.snapshot().generation, 0)

    def test_valid_persisted_document_is_loaded_defensively_at_generation_zero(self) -> None:
        self.repo.replace(_request(), _catalog())
        document = self.repo.snapshot().document

        loaded = CommandCatalogRepository(document, language="it", clock=lambda: NOW)
        document["records"]["AA:BB"]["payload"].clear()

        self.assertIsNotNone(loaded.lookup(_request()))
        self.assertEqual(loaded.snapshot().generation, 0)

    def test_noncanonical_payload_is_rejected_without_replacing_good_data(self) -> None:
        self.repo.replace(_request(), _catalog())

        with self.assertRaises(ValueError):
            self.repo.replace(_request(), {"not_json": object()})

        self.assertEqual(self.repo.snapshot().generation, 1)
        self.assertIsNotNone(self.repo.lookup(_request()))

    def test_census_is_identity_free_bounded_and_overwrites_per_mac(self) -> None:
        self.repo.replace(_request(), _catalog())
        cached = self.repo.lookup(_request())
        assert cached is not None
        self.repo.record(
            _request(),
            source="cache",
            failure="semantic",
            live_outcome="empty_payload",
            code="ADDHON-240",
            raw_entries=2,
            parsed_commands=1,
            favourites="raised",
            history="empty",
            cache=cached,
        )

        expected = {
            "source": "cache",
            "failure": "semantic",
            "live_outcome": "empty_payload",
            "code": "ADDHON-240",
            "raw_entries": 2,
            "parsed_commands": 1,
            "cache_age_s": 0,
            "digest": cached.digest[:12],
            "favourites": "raised",
            "history": "empty",
            "request": {
                "firmware": True,
                "firmware_version": True,
                "series": True,
                "series_version": True,
                "language": True,
            },
        }
        self.assertEqual(self.repo.census(), [expected])
        serialized = json.dumps(self.repo.census())
        for private in ("AA:BB", "4321", "CODE123", "temp", "setParameters"):
            self.assertNotIn(private, serialized)

        self.repo.record(
            _request(),
            source="live",
            failure=None,
            live_outcome="ok",
            code=None,
            raw_entries=-1,
            parsed_commands=1_000_001,
            favourites="ok",
            history="invalid",
        )
        census = self.repo.census()
        self.assertEqual(len(census), 1)
        self.assertEqual(census[0]["source"], "live")
        self.assertIsNone(census[0]["raw_entries"])
        self.assertIsNone(census[0]["parsed_commands"])

        census[0]["request"]["firmware"] = False
        self.assertTrue(self.repo.census()[0]["request"]["firmware"])

    def test_record_rejects_tokens_outside_closed_vocabularies(self) -> None:
        cases = (
            {"source": "remote"},
            {"failure": "auth"},
            {"live_outcome": "server said MAC=AA:BB"},
            {"favourites": "timeout"},
            {"history": "timeout"},
        )
        base = {
            "source": "none",
            "failure": None,
            "live_outcome": None,
            "code": None,
            "raw_entries": 0,
            "parsed_commands": 0,
            "favourites": "empty",
            "history": "empty",
        }
        for override in cases:
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.repo.record(_request(), **(base | override))

        self.assertEqual(
            CATALOG_SOURCES, frozenset({"live", "cache", "none"})
        )
        self.assertEqual(
            CATALOG_FAILURES, frozenset({"transport", "structural", "semantic"})
        )
        self.assertEqual(
            CATALOG_ENRICHMENTS, frozenset({"ok", "empty", "raised", "invalid"})
        )
        self.assertIn("ok", CATALOG_OUTCOMES)


class CommandCatalogDiagnosticsVocabularyTest(unittest.TestCase):
    def test_diagnostics_allowlists_cannot_drift_from_the_producer(self) -> None:
        self.assertEqual(CATALOG_SOURCES, diagnostics._CATALOG_SOURCES)
        self.assertEqual(CATALOG_FAILURES, diagnostics._CATALOG_FAILURES)
        self.assertEqual(CATALOG_OUTCOMES, diagnostics._CATALOG_OUTCOMES)
        self.assertEqual(CATALOG_ENRICHMENTS, diagnostics._CATALOG_ENRICHMENTS)
        self.assertEqual(CATALOG_REQUEST_FLAGS, diagnostics._CATALOG_REQUEST_FLAGS)


if __name__ == "__main__":
    unittest.main()
