# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Transactional command-catalog hydration and cache-fallback contracts."""

from __future__ import annotations

import asyncio
import copy
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import install_stubs  # noqa: E402

install_stubs()

from custom_components.addhon.client.catalog_repository import (  # noqa: E402
    CommandCatalogRepository,
)
from custom_components.addhon.client.engine.appliance import HonAppliance  # noqa: E402
from custom_components.addhon.client.engine.command_hydration import (  # noqa: E402
    CommandCatalogUnavailable,
)
from custom_components.addhon.client.engine.command_loader import (  # noqa: E402
    HonCommandLoader,
)
from custom_components.addhon.client.transport.command_catalog import (  # noqa: E402
    CommandCatalogFetch,
    CommandCatalogRequest,
    CommandCatalogResponseError,
    extract_command_catalog,
)
from custom_components.addhon.error_codes import (  # noqa: E402
    APPLIANCE_COMMANDS_UNAVAILABLE,
    AUTH_LOGIN,
    HonCodedError,
)


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


class ApplianceDouble:
    """Small engine-compatible appliance carrying catalog request metadata."""

    def __init__(self, appliance_type: str = "FRE") -> None:
        self.appliance_type = appliance_type
        self.appliance_model_id = "4321"
        self.mac_address = "AA:BB"
        self.code = "CODE123"
        self.info = {
            "eepromId": "EE",
            "fwVersion": "1.2",
            "series": "S",
            "seriesVersion": "V7",
        }
        self.zone = 0
        self.commands: dict[str, Any] = {}


def _request(appliance: ApplianceDouble, language: str = "it") -> CommandCatalogRequest:
    return CommandCatalogRequest.from_appliance(appliance, language)


def _command(*, fixed_value: str = "1") -> dict[str, Any]:
    return {
        "description": "catalog command",
        "protocolType": "MQTT",
        "parameters": {
            "onOff": {
                "typology": "fixed",
                "category": "command",
                "mandatory": 1,
                "fixedValue": fixed_value,
            }
        },
    }


def _valid_catalog(*, fixed_value: str = "1") -> dict[str, Any]:
    return {
        "applianceModel": {"options": {}},
        "settings": {"setParameters": _command(fixed_value=fixed_value)},
    }


def _program_catalog() -> dict[str, Any]:
    return {
        **_valid_catalog(),
        "startProgram": {
            "PROGRAMS.FRE.NORMAL": {
                "description": "normal",
                "protocolType": "MQTT",
                "parameters": {
                    "program": {
                        "typology": "fixed",
                        "category": "command",
                        "mandatory": 1,
                        "fixedValue": "PROGRAMS.FRE.NORMAL",
                    },
                    "temperature": {
                        "typology": "range",
                        "category": "command",
                        "mandatory": 0,
                        "defaultValue": "-18",
                        "minimumValue": "-24",
                        "maximumValue": "-12",
                        "incrementValue": "1",
                    },
                },
            }
        },
    }


def _fetch(payload: dict[str, Any], request: CommandCatalogRequest) -> CommandCatalogFetch:
    envelope = {"payload": {**payload, "resultCode": "0"}}
    return extract_command_catalog(envelope, status=200, request=request)


def _structural_error(request: CommandCatalogRequest) -> CommandCatalogResponseError:
    try:
        extract_command_catalog(
            {"payload": {"resultCode": "7"}}, status=200, request=request
        )
    except CommandCatalogResponseError as error:
        return error
    raise AssertionError("fixture did not produce a structural error")


class TypedApi:
    """Typed API double with independently configurable enrichment streams."""

    def __init__(
        self,
        fetch: CommandCatalogFetch | BaseException,
        *,
        favourites: Any = None,
        history: Any = None,
    ) -> None:
        self._fetch = fetch
        self._favourites = [] if favourites is None else favourites
        self._history = [] if history is None else history
        self.requests: list[CommandCatalogRequest] = []

    async def fetch_command_catalog(
        self, request: CommandCatalogRequest
    ) -> CommandCatalogFetch:
        self.requests.append(request)
        if isinstance(self._fetch, BaseException):
            raise self._fetch
        return self._fetch

    async def load_favourites(self, appliance: Any) -> Any:
        if isinstance(self._favourites, BaseException):
            raise self._favourites
        return self._favourites

    async def load_command_history(self, appliance: Any) -> Any:
        if isinstance(self._history, BaseException):
            raise self._history
        return self._history


class LookupSpyRepository(CommandCatalogRepository):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lookup_calls = 0

    def lookup(self, request: CommandCatalogRequest):
        self.lookup_calls += 1
        return super().lookup(request)


class CommandCatalogHydrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.appliance = ApplianceDouble()
        self.request = _request(self.appliance)
        self.repo = CommandCatalogRepository(None, "it", clock=lambda: 1_700_000_000)

    def load(
        self, api: TypedApi, repo: CommandCatalogRepository | None = None
    ):
        return _run(HonCommandLoader(api, self.appliance, repo or self.repo).load_commands())

    def test_valid_live_catalog_is_hydrated_and_persisted(self) -> None:
        payload = _valid_catalog()
        hydration = self.load(TypedApi(_fetch(payload, self.request)))

        self.assertEqual(hydration.source, "live")
        self.assertEqual(hydration.live_outcome, "ok")
        self.assertEqual(hydration.raw_entry_count, 2)
        self.assertEqual(hydration.parsed_command_count, 1)
        self.assertIn("settings", hydration.commands)
        self.assertEqual(self.repo.snapshot().generation, 1)
        cached = self.repo.lookup(self.request)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.payload, payload)  # type: ignore[union-attr]
        with self.assertRaises(FrozenInstanceError):
            hydration.source = "cache"  # type: ignore[misc]

    def test_valid_live_catalog_replaces_an_older_cache(self) -> None:
        self.repo.replace(self.request, _valid_catalog(fixed_value="0"))
        live = _valid_catalog(fixed_value="1")

        hydration = self.load(TypedApi(_fetch(live, self.request)))

        self.assertEqual(hydration.source, "live")
        self.assertEqual(self.repo.snapshot().generation, 2)
        self.assertEqual(self.repo.lookup(self.request).payload, live)  # type: ignore[union-attr]

    def test_structural_failure_uses_compatible_cache(self) -> None:
        cached = _valid_catalog()
        self.repo.replace(self.request, cached)

        hydration = self.load(TypedApi(_structural_error(self.request)))

        self.assertEqual(hydration.source, "cache")
        self.assertEqual(hydration.live_outcome, "nonzero_result")
        self.assertIn("settings", hydration.commands)
        observation = self.repo.census()[0]
        self.assertEqual(observation["failure"], "structural")
        self.assertEqual(observation["source"], "cache")

    def test_semantically_empty_required_catalog_uses_cache_without_poisoning_it(self) -> None:
        cached = _valid_catalog()
        self.repo.replace(self.request, cached)
        generation = self.repo.snapshot().generation
        empty = {"applianceModel": {"options": {}}}

        hydration = self.load(TypedApi(_fetch(empty, self.request)))

        self.assertEqual(hydration.source, "cache")
        self.assertEqual(self.repo.snapshot().generation, generation)
        stored = self.repo.lookup(self.request)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.payload, cached)  # type: ignore[union-attr]
        self.assertEqual(self.repo.census()[0]["failure"], "semantic")

    def test_semantically_empty_required_catalog_without_cache_is_typed(self) -> None:
        empty = {"applianceModel": {"options": {}}}

        with self.assertRaises(CommandCatalogUnavailable) as context:
            self.load(TypedApi(_fetch(empty, self.request)))

        self.assertIs(context.exception.error_code, APPLIANCE_COMMANDS_UNAVAILABLE)
        self.assertEqual(context.exception.phase, "load_appliance/commands")
        self.assertEqual(self.repo.snapshot().generation, 0)
        observation = self.repo.census()[0]
        self.assertEqual(observation["source"], "none")
        self.assertEqual(observation["failure"], "semantic")

    def test_required_family_catalog_without_appliance_model_is_rejected(self) -> None:
        # A fridge catalog that parses commands but carries NO applianceModel is not a
        # thinner catalog -- the official app cannot consume it at all
        # (`storeModelAndCommandsInDatabase`, decomp.txt:1786932-1786991, dereferences
        # `payload.applianceModel.*` unguarded while giving `options`/`settings` explicit
        # fallbacks). For a fridge it is worse than useless: `zones` lives in the model
        # attributes and `ref_programs.model_zones` denies on their silence, so a
        # modelless catalog silently mis-gates every zone-dependent control.
        payload = {"settings": {"setParameters": _command()}}

        with self.assertRaises(CommandCatalogUnavailable):
            self.load(TypedApi(_fetch(payload, self.request)))

    def test_required_family_catalog_with_zero_commands_is_rejected(self) -> None:
        # The other half of the same gate: parsable model, no parsable command.
        with self.assertRaises(CommandCatalogUnavailable):
            self.load(TypedApi(_fetch({"applianceModel": {"options": {}}}, self.request)))

    def test_a_rejected_required_catalog_still_prefers_a_compatible_cache(self) -> None:
        # The gate sends the appliance to the cache, it does not strand it. With a
        # validated snapshot the fridge keeps its controls across a bad cloud answer.
        self.repo.replace(self.request, _valid_catalog())
        payload = {"settings": {"setParameters": _command(fixed_value="0")}}

        hydration = self.load(TypedApi(_fetch(payload, self.request)))

        self.assertEqual(hydration.source, "cache")
        self.assertEqual(self.repo.census()[0]["failure"], "semantic")

    def test_a_rejected_cache_write_does_not_lose_a_successful_live_catalog(self) -> None:
        # The cache is an OPTIMIZATION. `replace` validates the record and rejects an
        # incomplete identity or a non-JSON value, and that rejection lands AFTER the
        # hydration already succeeded. Letting it escape would be worse than not
        # caching at all: ValueError is in session._APPLIANCE_BUILD_ERRORS, so the
        # appliance would be filed malformed and NON-retryable, never requeued, and
        # would lose every command entity permanently on every reload.
        class RejectingRepository(CommandCatalogRepository):
            def replace(self, request: Any, payload: Any) -> None:
                raise ValueError("Command catalog request identity is incomplete")

        repo = RejectingRepository(None, "it", clock=lambda: 1_700_000_000)
        hydration = self.load(TypedApi(_fetch(_valid_catalog(), self.request)), repo)

        self.assertEqual(hydration.source, "live")
        self.assertEqual(hydration.parsed_command_count, 1)
        self.assertIn("settings", hydration.commands)
        # Nothing was cached, and the observation still reads as a clean live load.
        self.assertEqual(repo.snapshot().generation, 0)
        self.assertEqual(repo.census()[0]["source"], "live")

    def test_uncensused_family_accepts_empty_live_without_persisting_it(self) -> None:
        appliance = ApplianceDouble("OV")
        request = _request(appliance)
        repo = CommandCatalogRepository(None, "it")
        api = TypedApi(_fetch({"applianceModel": {"options": {}}}, request))

        hydration = _run(HonCommandLoader(api, appliance, repo).load_commands())

        self.assertEqual(hydration.source, "live")
        self.assertEqual(hydration.commands, {})
        self.assertEqual(hydration.parsed_command_count, 0)
        self.assertEqual(repo.snapshot().generation, 0)
        observation = repo.census()[0]
        self.assertEqual(observation["source"], "live")
        self.assertEqual(observation["failure"], "semantic")

    def test_transport_failure_uses_cache_and_records_stable_code(self) -> None:
        self.repo.replace(self.request, _valid_catalog())

        hydration = self.load(TypedApi(asyncio.TimeoutError()))

        self.assertEqual(hydration.source, "cache")
        observation = self.repo.census()[0]
        self.assertEqual(observation["failure"], "transport")
        self.assertEqual(observation["code"], "ADDHON-400")

    def test_transport_failure_without_cache_propagates_original_error(self) -> None:
        timeout = asyncio.TimeoutError()

        with self.assertRaises(asyncio.TimeoutError) as context:
            self.load(TypedApi(timeout))

        self.assertIs(context.exception, timeout)
        observation = self.repo.census()[0]
        self.assertEqual(observation["source"], "none")
        self.assertEqual(observation["failure"], "transport")

    def test_auth_failure_never_reads_cache(self) -> None:
        repo = LookupSpyRepository(None, "it")
        repo.replace(self.request, _valid_catalog())
        auth = HonCodedError(AUTH_LOGIN)

        with self.assertRaises(HonCodedError) as context:
            self.load(TypedApi(auth), repo)

        self.assertIs(context.exception, auth)
        self.assertEqual(repo.lookup_calls, 0)
        self.assertEqual(repo.census(), [])

    def test_payload_favourites_and_history_are_not_mutated(self) -> None:
        payload = _program_catalog()
        favourites = [
            {
                "favouriteName": "Cold",
                "command": {
                    "commandName": "startProgram",
                    "programName": "PROGRAMS.FRE.NORMAL",
                },
                "parameters": {"temperature": "-20"},
            }
        ]
        history = [
            {
                "command": {
                    "commandName": "startProgram",
                    "parameters": {
                        "program": "PROGRAMS.FRE.NORMAL",
                        "category": "normal",
                        "temperature": "-19",
                    },
                }
            }
        ]
        originals = copy.deepcopy((payload, favourites, history))
        api = TypedApi(
            _fetch(payload, self.request),
            favourites=favourites,
            history=history,
        )

        hydration = self.load(api)

        self.assertIn("settings", hydration.commands)
        self.assertEqual(hydration.favourites_outcome, "ok")
        self.assertEqual(hydration.history_outcome, "ok")
        self.assertEqual(payload, originals[0])
        self.assertEqual(favourites, originals[1])
        self.assertEqual(history, originals[2])
        parameters = history[0]["command"]["parameters"]
        self.assertIn("program", parameters)
        self.assertIn("category", parameters)

    def test_optional_failure_and_empty_stream_do_not_discard_base_catalog(self) -> None:
        api = TypedApi(
            _fetch(_valid_catalog(), self.request),
            favourites=RuntimeError("favourite down"),
            history=[],
        )

        hydration = self.load(api)

        self.assertIn("settings", hydration.commands)
        self.assertEqual(hydration.favourites_outcome, "raised")
        self.assertEqual(hydration.history_outcome, "empty")

    def test_invalid_optional_values_are_ignored_independently(self) -> None:
        api = TypedApi(
            _fetch(_valid_catalog(), self.request),
            favourites={"not": "a list"},
            history="not a list",
        )

        hydration = self.load(api)

        self.assertEqual(hydration.favourites_outcome, "invalid")
        self.assertEqual(hydration.history_outcome, "invalid")
        self.assertIn("settings", hydration.commands)


class ApplianceAtomicAdoptionTest(unittest.TestCase):
    def test_failed_hydration_keeps_all_previous_engine_mappings(self) -> None:
        appliance_double = ApplianceDouble()
        request = _request(appliance_double)
        api = TypedApi(_structural_error(request))
        appliance = HonAppliance(
            api,
            {
                "applianceTypeName": "FRE",
                "applianceModelId": "4321",
                "macAddress": "AA:BB",
                "code": "CODE123",
            },
            catalog_repository=CommandCatalogRepository(None, "it"),
        )
        old_commands = {"old": object()}
        old_additional = {"old": object()}
        old_model = {"old": object()}
        appliance._commands = old_commands  # noqa: SLF001 - atomicity contract
        appliance._additional_data = old_additional  # noqa: SLF001
        appliance._appliance_model = old_model  # noqa: SLF001

        with self.assertRaises(CommandCatalogUnavailable):
            _run(appliance.load_commands())

        self.assertIs(appliance.commands, old_commands)
        self.assertIs(appliance.additional_data, old_additional)
        self.assertIs(appliance._appliance_model, old_model)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
