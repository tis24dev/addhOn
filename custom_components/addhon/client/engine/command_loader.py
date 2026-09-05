# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command loader.

Loads the three cloud streams in parallel (commands / favourites / command-history)
via the api, builds the `HonCommand`s, applies favourites and
restores the last executed state of each command.

`api`/`appliance` duck-typed.

enum-casing note (to re-validate LIVE): the favourites
(`_update_base_command_with_data`) and recover (`_recover_last_command_states`) paths
write RAW values (saved by the cloud/the history) into the parameters, which may have a
casing different from the `enumValues`. On an enum the setter accepts the value if the
normalized form matches and keeps the raw one in `intern_value`; the
`suppress(ValueError)` guards the rare value that cannot be normalized to an allowed
one (the default is kept). This is not verifiable offline (the fridge has no
favourites, the AC is offline): the decision is DEFERRED to live validation. On
already-clean values (the common case) the behavior is unchanged.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from copy import copy, deepcopy
from typing import Any, Optional

from ...error_codes import APPLIANCE_COMMANDS_UNAVAILABLE, classify
from ..catalog_repository import CachedCommandCatalog, CommandCatalogRepository
from ..transport.command_catalog import (
    CommandCatalogFetch,
    CommandCatalogRequest,
    CommandCatalogResponseError,
    legacy_command_catalog_fetch,
)
from .command_hydration import (
    CATALOG_REQUIRED_TYPES,
    CommandCatalogUnavailable,
    CommandHydration,
)
from .commands import HonCommand
from .exceptions import NoAuthenticationException
from .parameter.fixed import HonParameterFixed
from .parameter.program import HonParameterProgram

_LOGGER = logging.getLogger(__name__)


class _SemanticCatalogError(Exception):
    """Internal marker carrying only bounded parser counts."""

    def __init__(self, raw_entries: int, parsed_commands: int) -> None:
        self.raw_entries = raw_entries
        self.parsed_commands = parsed_commands
        super().__init__("Command catalog is not semantically usable")


class HonCommandLoader:
    """Loads and parses the hOn command data."""

    def __init__(
        self,
        api: Any,
        appliance: Any,
        catalog_repository: CommandCatalogRepository | None = None,
    ) -> None:
        self._api = api
        self._appliance = appliance
        self._repository = (
            catalog_repository
            if catalog_repository is not None
            else CommandCatalogRepository(None, "en")
        )
        self._catalog_request: CommandCatalogRequest | None = None
        self._api_commands: dict[str, Any] = {}
        self._favourites: list[dict[str, Any]] = []
        self._command_history: list[dict[str, Any]] = []
        self._commands: dict[str, HonCommand] = {}
        self._appliance_data: dict[str, Any] = {}
        self._additional_data: dict[str, Any] = {}

    @property
    def api(self) -> Any:
        if self._api is None:
            raise NoAuthenticationException("Missing hOn login")
        return self._api

    @property
    def appliance(self) -> Any:
        return self._appliance

    @property
    def commands(self) -> dict[str, HonCommand]:
        return self._commands

    @property
    def appliance_data(self) -> dict[str, Any]:
        return self._appliance_data

    @property
    def additional_data(self) -> dict[str, Any]:
        return self._additional_data

    async def load_commands(self) -> CommandHydration:
        """Build one complete live or cached candidate without touching the appliance."""
        catalog, raw_favourites, raw_history = await self._load_data()
        request = self._catalog_request
        if request is None:  # pragma: no cover - _fetch_catalog always initializes it
            raise RuntimeError("Command catalog request was not initialized")

        favourites, favourites_outcome = self._enrichment(
            raw_favourites, "favourites"
        )
        history, history_outcome = self._enrichment(raw_history, "history")

        if isinstance(catalog, CommandCatalogFetch):
            try:
                hydration = self._hydrate(
                    catalog.payload,
                    source="live",
                    live_outcome=catalog.probe.outcome,
                    favourites=(favourites, favourites_outcome),
                    history=(history, history_outcome),
                )
            except _SemanticCatalogError as error:
                return self._fallback_after_catalog_failure(
                    request,
                    probe=catalog.probe,
                    failure="semantic",
                    live_outcome=catalog.probe.outcome,
                    live_failure=error,
                    raw_entries=error.raw_entries,
                    parsed_commands=error.parsed_commands,
                    favourites=(favourites, favourites_outcome),
                    history=(history, history_outcome),
                )

            if hydration.parsed_command_count == 0:
                self._record(
                    request,
                    hydration,
                    failure="semantic",
                    code=None,
                    probe=catalog.probe,
                )
                return hydration

            try:
                self._repository.replace(request, catalog.payload)
            except ValueError as error:
                # The cache is an OPTIMIZATION and its failure must never cost a live
                # hydration that has ALREADY succeeded. `replace` rejects an incomplete
                # identity (an empty applianceModelId is a shape this repo has seen --
                # engine/appliance.py documents it) and any non-JSON value such as a
                # non-finite float. Letting that escape would be worse than not caching:
                # ValueError is in session._APPLIANCE_BUILD_ERRORS, so the appliance
                # would be recorded malformed and NON-retryable, never requeued by
                # needs_rehydration, and would lose every command entity permanently --
                # reproducing identically on every reload.
                _LOGGER.debug(
                    "Command catalog cache write was rejected (%s); "
                    "serving the live catalog and leaving the cache untouched",
                    type(error).__name__,
                )
            self._record(request, hydration, failure=None, code=None, probe=catalog.probe)
            return hydration

        if isinstance(catalog, CommandCatalogResponseError):
            return self._fallback_after_catalog_failure(
                request,
                probe=catalog.probe,
                failure="structural",
                live_outcome=catalog.probe.outcome,
                live_failure=catalog,
                raw_entries=catalog.probe.payload_entries,
                parsed_commands=0,
                favourites=(favourites, favourites_outcome),
                history=(history, history_outcome),
            )

        if isinstance(catalog, BaseException):
            code = classify(catalog, phase="load_appliance/commands").label
            cached, hydration = self._cached_hydration(
                request,
                live_outcome=None,
                favourites=(favourites, favourites_outcome),
                history=(history, history_outcome),
            )
            if hydration is not None:
                self._record(
                    request,
                    hydration,
                    failure="transport",
                    code=code,
                    cache=cached,
                )
                return hydration
            self._repository.record(
                request,
                source="none",
                failure="transport",
                live_outcome=None,
                code=code,
                raw_entries=0,
                parsed_commands=0,
                favourites=favourites_outcome,
                history=history_outcome,
                cache=cached,
            )
            raise catalog

        invalid = TypeError("Command catalog fetch returned an invalid result")
        return self._fallback_after_catalog_failure(
            request,
            failure="structural",
            live_outcome="invalid_payload",
            live_failure=invalid,
            raw_entries=0,
            parsed_commands=0,
            favourites=(favourites, favourites_outcome),
            history=(history, history_outcome),
        )

    async def _fetch_catalog(self) -> CommandCatalogFetch:
        request = CommandCatalogRequest.from_appliance(
            self._appliance, self._repository.language
        )
        self._catalog_request = request
        fetcher = getattr(self._api, "fetch_command_catalog", None)
        if callable(fetcher):
            return await fetcher(request)
        payload = await self._api.load_commands(self._appliance)
        return legacy_command_catalog_fetch(payload, request)

    async def _load_favourites(self) -> None:
        self._favourites = await self._api.load_favourites(self._appliance)

    async def _load_command_history(self) -> None:
        self._command_history = await self._api.load_command_history(self._appliance)

    async def _load_data(self) -> tuple[Any, Any, Any]:
        results = await asyncio.gather(
            self._fetch_catalog(),
            self._load_favourites(),
            self._load_command_history(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException) and classify(result).requires_reauth:
                raise result
        favourites = self._favourites if results[1] is None else results[1]
        history = self._command_history if results[2] is None else results[2]
        return results[0], favourites, history

    @staticmethod
    def _enrichment(result: Any, name: str) -> tuple[list[dict[str, Any]], str]:
        if isinstance(result, BaseException):
            _LOGGER.debug(
                "Command catalog %s enrichment failed (%s)",
                name,
                type(result).__name__,
            )
            return [], "raised"
        if not isinstance(result, list):
            return [], "invalid"
        if not result:
            return [], "empty"
        if not all(isinstance(item, dict) for item in result):
            return [], "invalid"
        try:
            return deepcopy(result), "ok"
        except Exception as error:  # noqa: BLE001 - enrichment remains optional
            _LOGGER.debug(
                "Command catalog %s enrichment was invalid (%s)",
                name,
                type(error).__name__,
            )
            return [], "invalid"

    def _hydrate(
        self,
        payload: dict[str, Any],
        *,
        source: str,
        live_outcome: str | None,
        favourites: tuple[list[dict[str, Any]], str],
        history: tuple[list[dict[str, Any]], str],
    ) -> CommandHydration:
        raw_entries = len(payload) if isinstance(payload, dict) else 0
        self._api_commands = {}
        self._favourites = []
        self._command_history = []
        self._commands = {}
        self._appliance_data = {}
        self._additional_data = {}
        try:
            candidate = deepcopy(payload)
            if not isinstance(candidate, dict):
                raise TypeError("Command catalog payload is not a mapping")
            appliance_model = candidate.pop("applianceModel", {})
            model = appliance_model if isinstance(appliance_model, dict) else {}
            self._parse_candidate(candidate, model)
        except Exception as error:
            raise _SemanticCatalogError(raw_entries, len(self._commands)) from error

        parsed_commands = len(self._commands)
        # BOTH halves are required, and the official app is the reason.
        # `storeModelAndCommandsInDatabase` (decomp.txt:1786932-1786991) dereferences
        # `payload.applianceModel.name/.id/.code/.applianceTypeId/.applianceTypeName/
        # .brand/.connectivity/.attributes` with NO guard, while `options` and
        # `settings` right beside it get explicit `if (!x)` fallbacks. The response
        # path feeding it (decomp.txt:3629724-3629735) hands `response.data.payload`
        # straight to that function without checking `resultCode` or the model. So the
        # app treats `applianceModel` as MANDATORY in a usable catalog and would throw
        # on a payload that lacks it: a catalog without it is not a thinner catalog,
        # it is not a catalog. For a fridge it is also actively harmful -- the model
        # attributes are where `zones` lives, and `ref_programs.model_zones` denies on
        # its silence, so a modelless catalog mis-gates every zone-dependent control.
        #
        # The probe's `has_appliance_model` is a payload-shape descriptor next to
        # `has_settings`/`has_start_program`, not evidence that a modelless catalog is
        # a shape the vendor ships; reading it as such was the mistake that briefly
        # relaxed this gate. Falling back is safe now: an unusable catalog degrades the
        # appliance instead of failing the entry (client/session.py, ADDHON-240).
        if self.appliance.appliance_type in CATALOG_REQUIRED_TYPES and (
            not model or parsed_commands == 0
        ):
            raise _SemanticCatalogError(raw_entries, parsed_commands)

        favourite_data, favourite_outcome = favourites
        if favourite_data:
            self._favourites = deepcopy(favourite_data)
            try:
                self._add_favourites()
            except Exception as error:  # noqa: BLE001 - enrichment is optional
                _LOGGER.debug(
                    "Command catalog favourites enrichment was invalid (%s)",
                    type(error).__name__,
                )
                favourite_outcome = "invalid"
                self._parse_candidate(candidate, model)

        history_data, history_outcome = history
        if history_data:
            self._command_history = deepcopy(history_data)
            try:
                self._recover_last_command_states()
            except Exception as error:  # noqa: BLE001 - enrichment is optional
                _LOGGER.debug(
                    "Command catalog history enrichment was invalid (%s)",
                    type(error).__name__,
                )
                history_outcome = "invalid"
                self._parse_candidate(candidate, model)
                if favourite_data and favourite_outcome == "ok":
                    self._favourites = deepcopy(favourite_data)
                    self._add_favourites()

        return CommandHydration(
            commands=self._commands,
            appliance_model=self._appliance_data,
            additional_data=self._additional_data,
            source="cache" if source == "cache" else "live",
            live_outcome=live_outcome,
            raw_entry_count=raw_entries,
            parsed_command_count=parsed_commands,
            favourites_outcome=favourite_outcome,
            history_outcome=history_outcome,
        )

    def _parse_candidate(
        self, commands: dict[str, Any], appliance_model: dict[str, Any]
    ) -> None:
        self._api_commands = deepcopy(commands)
        self._favourites = []
        self._command_history = []
        self._commands = {}
        self._appliance_data = deepcopy(appliance_model)
        self._additional_data = {}
        self._get_commands()

    def _cached_hydration(
        self,
        request: CommandCatalogRequest,
        *,
        live_outcome: str | None,
        favourites: tuple[list[dict[str, Any]], str],
        history: tuple[list[dict[str, Any]], str],
    ) -> tuple[CachedCommandCatalog | None, CommandHydration | None]:
        cached = self._repository.lookup(request)
        if cached is None:
            return None, None
        try:
            hydration = self._hydrate(
                cached.payload,
                source="cache",
                live_outcome=live_outcome,
                favourites=favourites,
                history=history,
            )
        except _SemanticCatalogError:
            return cached, None
        return cached, hydration

    def _fallback_after_catalog_failure(
        self,
        request: CommandCatalogRequest,
        *,
        failure: str,
        live_outcome: str,
        live_failure: BaseException,
        raw_entries: int,
        parsed_commands: int,
        favourites: tuple[list[dict[str, Any]], str],
        history: tuple[list[dict[str, Any]], str],
        probe: Any = None,
    ) -> CommandHydration:
        cached, hydration = self._cached_hydration(
            request,
            live_outcome=live_outcome,
            favourites=favourites,
            history=history,
        )
        code = APPLIANCE_COMMANDS_UNAVAILABLE.label
        if hydration is not None:
            self._record(
                request,
                hydration,
                failure=failure,
                code=code,
                cache=cached,
                probe=probe,
            )
            return hydration
        self._repository.record(
            request,
            source="none",
            failure=failure,
            live_outcome=live_outcome,
            code=code,
            raw_entries=raw_entries,
            parsed_commands=parsed_commands,
            favourites=favourites[1],
            history=history[1],
            cache=cached,
            status=getattr(probe, "status", None),
            sections=probe.sections() if probe is not None else None,
        )
        raise CommandCatalogUnavailable() from live_failure

    def _record(
        self,
        request: CommandCatalogRequest,
        hydration: CommandHydration,
        *,
        failure: str | None,
        code: str | None,
        cache: CachedCommandCatalog | None = None,
        probe: Any = None,
    ) -> None:
        self._repository.record(
            request,
            source=hydration.source,
            failure=failure,
            live_outcome=hydration.live_outcome,
            code=code,
            raw_entries=hydration.raw_entry_count,
            parsed_commands=hydration.parsed_command_count,
            favourites=hydration.favourites_outcome,
            history=hydration.history_outcome,
            cache=cache,
            status=getattr(probe, "status", None),
            sections=probe.sections() if probe is not None else None,
        )

    @staticmethod
    def _is_command(data: dict[str, Any]) -> bool:
        return (
            data.get("description") is not None and data.get("protocolType") is not None
        )

    @staticmethod
    def _clean_name(category: str) -> str:
        if "PROGRAM" in category:
            return category.split(".")[-1].lower()
        return category

    def _get_commands(self) -> None:
        commands = []
        for name, data in self._api_commands.items():
            if command := self._parse_command(data, name):
                commands.append(command)
        self._commands = {c.name: c for c in commands}

    def _parse_command(
        self,
        data: dict[str, Any] | str,
        command_name: str,
        categories: Optional[dict[str, HonCommand]] = None,
        category_name: str = "",
    ) -> Optional[HonCommand]:
        if not isinstance(data, dict):
            self._additional_data[command_name] = data
            return None
        if self._is_command(data):
            return HonCommand(
                command_name,
                data,
                self._appliance,
                category_name=category_name,
                categories=categories,
            )
        if category := self._parse_categories(data, command_name):
            return category
        return None

    def _parse_categories(
        self, data: dict[str, Any], command_name: str
    ) -> Optional[HonCommand]:
        categories: dict[str, HonCommand] = {}
        for category, value in data.items():
            if command := self._parse_command(
                value, command_name, category_name=category, categories=categories
            ):
                categories[self._clean_name(category)] = command
        if categories:
            # setParameters must come first
            if "setParameters" in categories:
                return categories["setParameters"]
            return list(categories.values())[0]
        return None

    def _get_last_command_index(self, name: str) -> Optional[int]:
        return next(
            (
                index
                for (index, d) in enumerate(self._command_history)
                if d.get("command", {}).get("commandName") == name
            ),
            None,
        )

    def _set_last_category(
        self, command: HonCommand, name: str, parameters: dict[str, Any]
    ) -> HonCommand:
        """Point `name` at the category the last accepted command used.

        The swap is applied to the LOADER's own dict rather than through the
        ``command.category`` setter. That setter writes into
        ``appliance.commands[name]``, but during a load the appliance has not adopted
        this loader's dict yet -- ``HonAppliance.load_commands`` assigns
        ``self._commands = command_loader.commands`` only AFTER we return, which would
        overwrite the swapped entry and silently discard the recovery. Writing here is
        what makes ``return self._commands[name]`` (this method's stated intent) true.
        """
        if not command.categories:
            return command
        if program := parameters.pop("program", None):
            category = self._clean_name(str(program))
        elif (category := parameters.pop("category", None)) is not None:
            category = str(category)
        else:
            return command
        # Same guard as the category setter: an unknown category leaves the default in
        # place instead of raising on a stale/renamed program in the history.
        if category in command.categories:
            selected = command.categories[category]
            # This swap bypasses the `category` setter (see the docstring), so the
            # "deliberately selected" mark has to be applied here too -- otherwise a
            # recovered program would be indistinguishable from the schema default.
            selected.mark_selected_explicitly()
            self._commands[name] = selected
        return self._commands[name]

    def _recover_last_command_states(self) -> None:
        for name, command in self.commands.items():
            if (last_index := self._get_last_command_index(name)) is None:
                continue
            last_command = self._command_history[last_index]
            raw_parameters = last_command.get("command", {}).get("parameters", {})
            parameters = dict(raw_parameters) if isinstance(raw_parameters, dict) else {}
            command = self._set_last_category(command, name, parameters)
            for key, data in command.settings.items():
                if parameters.get(key) is None:
                    continue
                with suppress(ValueError):
                    data.value = parameters.get(key)

    def _add_favourites(self) -> None:
        for favourite in self._favourites:
            name, command_name, base = self._get_favourite_info(favourite)
            if not base:
                continue
            base_command: HonCommand = copy(base)
            self._update_base_command_with_data(base_command, favourite)
            self._update_base_command_with_favourite(base_command)
            self._update_program_categories(command_name, name, base_command)

    def _get_favourite_info(
        self, favourite: dict[str, Any]
    ) -> tuple[str, str, HonCommand | None]:
        name = str(favourite.get("favouriteName", ""))
        command = favourite.get("command", {})
        if not isinstance(command, dict):
            return name, "", None
        command_name = str(command.get("commandName", ""))
        if not command_name:
            return name, "", None
        parent = self.commands.get(command_name)
        if parent is None:  # stale favourite: command no longer available
            return name, command_name, None
        program_name = self._clean_name(str(command.get("programName", "")))
        base_command = parent.categories.get(program_name)
        return name, command_name, base_command

    def _update_base_command_with_data(
        self, base_command: HonCommand, command: dict[str, Any]
    ) -> None:
        for data in command.values():
            if not isinstance(data, dict):
                continue
            for key, value in data.items():
                if not (parameter := base_command.parameters.get(key)):
                    continue
                with suppress(ValueError):
                    parameter.value = value

    def _update_base_command_with_favourite(self, base_command: HonCommand) -> None:
        extra_param = HonParameterFixed("favourite", {"fixedValue": "1"}, "custom")
        base_command.parameters.update(favourite=extra_param)

    def _update_program_categories(
        self, command_name: str, name: str, base_command: HonCommand
    ) -> None:
        program = base_command.parameters["program"]
        if isinstance(program, HonParameterProgram):
            program.set_value(name)
        self.commands[command_name].categories[name] = base_command
