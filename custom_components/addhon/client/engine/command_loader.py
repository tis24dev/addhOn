"""HonCommandLoader nativo. Porting di `_vendor/pyhon/command_loader.py`.

Carica in parallelo i tre flussi dal cloud (commands / favourites / command-history)
via l'api nativa, ne costruisce i `HonCommand` nativi, applica i preferiti e
ripristina l'ultimo stato eseguito di ogni comando.

`api`/`appliance` duck-typed. Comportamento ancorato a pyhОn dal differential test
sui dati reali del frigo (commands.json + command_history.json + favourites).

DIVERGENZA enum-casing (da rivalidare LIVE): i path favourites
(`_update_base_command_with_data`) e recover (`_recover_last_command_states`) scrivono
nei parametri valori GREZZI (salvati dal cloud/dalla history), che possono avere un
casing diverso dai `enumValues`. Su un enum il nostro setter accetta il valore se la
forma normalizzata combacia (fix BABYCARE) e tiene il grezzo in `intern_value`;
pyhОn+patch invece RIFIUTA un valore ri-castato (e l'errore viene inghiottito dal
`suppress(ValueError)`), mantenendo il default. Quindi su un favourite/history con un
enum ri-castato il valore inviato al cloud può differire. Non è validabile offline
(il frigo non ha favourites, l'AC è offline) e il valore "preservato" di pyhОn è esso
stesso un artefatto (es. `[dashboard]` con parentesi): si RIMANDA la decisione alla
validazione live. Sui valori già puliti (caso comune) è identico.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from copy import copy
from typing import Any, Optional

from .commands import HonCommand
from .exceptions import NoAuthenticationException
from .parameter.fixed import HonParameterFixed
from .parameter.program import HonParameterProgram


class HonCommandLoader:
    """Carica e parsa i dati comando di hОn."""

    def __init__(self, api: Any, appliance: Any) -> None:
        self._api = api
        self._appliance = appliance
        self._api_commands: dict[str, Any] = {}
        self._favourites: list[dict[str, Any]] = []
        self._command_history: list[dict[str, Any]] = []
        self._commands: dict[str, HonCommand] = {}
        self._appliance_data: dict[str, Any] = {}
        self._additional_data: dict[str, Any] = {}

    @property
    def api(self) -> Any:
        if self._api is None:
            raise NoAuthenticationException("Missing hОn login")
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

    async def load_commands(self) -> None:
        await self._load_data()
        self._appliance_data = self._api_commands.pop("applianceModel", {})
        self._get_commands()
        self._add_favourites()
        self._recover_last_command_states()

    async def _load_commands(self) -> None:
        self._api_commands = await self._api.load_commands(self._appliance)

    async def _load_favourites(self) -> None:
        self._favourites = await self._api.load_favourites(self._appliance)

    async def _load_command_history(self) -> None:
        self._command_history = await self._api.load_command_history(self._appliance)

    async def _load_data(self) -> None:
        await asyncio.gather(
            self._load_commands(),
            self._load_favourites(),
            self._load_command_history(),
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
            # setParameters deve stare al primo posto
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
        if command.categories:
            if program := parameters.pop("program", None):
                command.category = self._clean_name(program)
            elif category := parameters.pop("category", None):
                command.category = category
            else:
                return command
            return self.commands[name]
        return command

    def _recover_last_command_states(self) -> None:
        for name, command in self.commands.items():
            if (last_index := self._get_last_command_index(name)) is None:
                continue
            last_command = self._command_history[last_index]
            parameters = last_command.get("command", {}).get("parameters", {})
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
        if parent is None:  # favourite stale: comando non piu' disponibile
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
