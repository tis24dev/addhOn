# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Command.

A command = a dict of parameter groups (parameters / ancillaryParameters / ...)
plus category/program metadata. It builds the parameters (range/enum/
fixed/program), collects the rules from the `category=="rule"` parameters, and knows how
to send itself to the cloud via the injected api (appliance.api).

`appliance` is duck-typed (the ROOT HonAppliance):
it needs `.api`, `.zone`, `.commands`, `.sync_command_to_params`.

Error-path: on `NoAuthenticationException` the error propagates -> the caller
(button/switch/hon_commands) turns it into an honest HomeAssistantError instead of a
false "sent".
"""
from __future__ import annotations

from copy import copy
from typing import Any, Optional, Union

from .exceptions import ApiError, NoAuthenticationException
from .parameter.base import HonParameter
from .parameter.enum import HonParameterEnum
from .parameter.fixed import HonParameterFixed
from .parameter.program import HonParameterProgram
from .parameter.range import HonParameterRange
from .rules import HonRuleSet

import logging

_LOGGER = logging.getLogger(__name__)


class HonCommand:
    def __init__(
        self,
        name: str,
        attributes: dict[str, Any],
        appliance: Any,
        categories: Optional[dict[str, "HonCommand"]] = None,
        category_name: str = "",
    ) -> None:
        self._name = name
        self._api: Any = None
        self._appliance = appliance
        self._categories = categories
        self._category_name = category_name
        self._parameters: dict[str, HonParameter] = {}
        self._data: dict[str, Any] = {}
        self._rules: list[HonRuleSet] = []
        attributes.pop("description", "")
        attributes.pop("protocolType", "")
        self._load_parameters(attributes)

    def __repr__(self) -> str:
        return f"{self._name} command"

    def __copy__(self) -> "HonCommand":
        # `_add_favourites` (command_loader) does `copy(base)` and then MUTATES the
        # copy's parameters (sets values, injects a `favourite` fixed, sets the program
        # value). A default shallow copy shares the SAME `_parameters` dict AND the same
        # parameter objects with the base program command (also reachable via
        # `parent.categories`), so those mutations corrupt the base program: its values
        # get overwritten, it gains `favourite="1"`, and it then disappears from
        # `HonParameterProgram.ids` (which filters favourites out). Give each copy its own
        # parameter dict with copied parameter objects so the base stays pristine.
        new = self.__class__.__new__(self.__class__)
        new.__dict__.update(self.__dict__)
        new._parameters = {name: copy(param) for name, param in self._parameters.items()}
        # A shallow-copied parameter still SHARES its `_triggers` table with the base, and
        # every rule callback in it closes over THIS command -- so setting a value on the
        # copy would fire rules that mutate the base's parameters (the exact corruption the
        # `_parameters` isolation above prevents, via the trigger back-door). Give each
        # copied param a fresh trigger table and rebind the rule sets to the copy, so its
        # rules act only on itself.
        for parameter in new._parameters.values():
            parameter.reset_triggers()
            # A copied HonParameterProgram keeps its base back-references intact:
            # `_command` points at the BASE command and its value-setter does
            # `self._command.category = value` (which swaps `appliance.commands`).
            # Rebind them to the copy so a write on the copy's program parameter can
            # never reach the base command. The current favourite loader never hits
            # this (the raw "PROGRAMS.X" value is not in the cleaned `.values`, so the
            # setter raises a suppressed ValueError), but rebinding removes the latent
            # back-door for good.
            if isinstance(parameter, HonParameterProgram):
                parameter._command = new
                parameter._programs = new.categories
        new._rules = [ruleset.rebound(new) for ruleset in self._rules]
        return new

    @property
    def name(self) -> str:
        return self._name

    @property
    def api(self) -> Any:
        if self._api is None and self._appliance is not None:
            self._api = self._appliance.api  # may raise if not authenticated
        if self._api is None:
            raise NoAuthenticationException("Missing hOn login")
        return self._api

    @property
    def appliance(self) -> Any:
        return self._appliance

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    @property
    def parameters(self) -> dict[str, HonParameter]:
        return self._parameters

    @property
    def settings(self) -> dict[str, HonParameter]:
        return self._parameters

    @property
    def parameter_groups(self) -> dict[str, dict[str, Union[str, float]]]:
        result: dict[str, dict[str, Union[str, float]]] = {}
        for name, parameter in self._parameters.items():
            result.setdefault(parameter.group, {})[name] = parameter.intern_value
        return result

    @property
    def mandatory_parameter_groups(self) -> dict[str, dict[str, Union[str, float]]]:
        result: dict[str, dict[str, Union[str, float]]] = {}
        for name, parameter in self._parameters.items():
            if parameter.mandatory:
                result.setdefault(parameter.group, {})[name] = parameter.intern_value
        return result

    @property
    def parameter_value(self) -> dict[str, Union[str, float]]:
        return {n: p.value for n, p in self._parameters.items()}

    def _load_parameters(self, attributes: dict[str, Any]) -> None:
        for key, items in attributes.items():
            if not isinstance(items, dict):
                _LOGGER.info("Loading Attributes - Skipping %s", str(items))
                continue
            for name, data in items.items():
                self._create_parameters(data, name, key)
        for rule in self._rules:
            rule.patch()

    def _create_parameters(
        self, data: dict[str, Any], name: str, parameter: str
    ) -> None:
        if name == "zoneMap" and self._appliance.zone:
            data["default"] = self._appliance.zone
        if data.get("category") == "rule":
            if "fixedValue" in data:
                self._rules.append(HonRuleSet(self, data["fixedValue"]))
            elif "enumValues" in data:
                self._rules.append(HonRuleSet(self, data["enumValues"]))
            else:
                _LOGGER.warning("Rule not supported: %s", data)
        match data.get("typology"):
            case "range":
                self._parameters[name] = HonParameterRange(name, data, parameter)
            case "enum":
                self._parameters[name] = HonParameterEnum(name, data, parameter)
            case "fixed":
                self._parameters[name] = HonParameterFixed(name, data, parameter)
            case _:
                self._data[name] = data
                return
        if self._category_name:
            name = "program" if "PROGRAM" in self._category_name else "category"
            self._parameters[name] = HonParameterProgram(name, self, "custom")

    async def send(self, only_mandatory: bool = False) -> bool:
        grouped_params = (
            self.mandatory_parameter_groups if only_mandatory else self.parameter_groups
        )
        params = grouped_params.get("parameters", {})
        return await self.send_parameters(params)

    async def send_specific(self, param_names: list[str]) -> bool:
        params: dict[str, str | float] = {}
        for key, parameter in self._parameters.items():
            if key in param_names or parameter.mandatory:
                params[key] = parameter.value
        return await self.send_parameters(params)

    async def send_parameters(self, params: dict[str, str | float]) -> bool:
        ancillary_params = self.parameter_groups.get("ancillaryParameters", {})
        ancillary_params.pop("programRules", None)
        if "prStr" in params:
            params["prStr"] = self._category_name.upper()
        self.appliance.sync_command_to_params(self.name)
        result = await self.api.send_command(
            self._appliance,
            self._name,
            params,
            ancillary_params,
            self._category_name,
        )
        if not result:
            _LOGGER.error("Command rejected by cloud: %s", self._name)
            raise ApiError("Can't send command")
        return result

    @property
    def categories(self) -> dict[str, "HonCommand"]:
        if self._categories is None:
            return {"_": self}
        return self._categories

    @property
    def category(self) -> str:
        return self._category_name

    @category.setter
    def category(self, category: str) -> None:
        if category in self.categories:
            self._appliance.commands[self._name] = self.categories[category]

    @property
    def setting_keys(self) -> list[str]:
        return list(
            {param for cmd in self.categories.values() for param in cmd.parameters}
        )

    @staticmethod
    def _more_options(first: HonParameter, second: HonParameter) -> HonParameter:
        if isinstance(first, HonParameterFixed) and not isinstance(
            second, HonParameterFixed
        ):
            return second
        if second.option_count() > first.option_count():
            return second
        return first

    @property
    def available_settings(self) -> dict[str, HonParameter]:
        result: dict[str, HonParameter] = {}
        for command in self.categories.values():
            for name, parameter in command.parameters.items():
                if name in result:
                    result[name] = self._more_options(result[name], parameter)
                else:
                    result[name] = parameter
        return result

    def reset(self) -> None:
        for parameter in self._parameters.values():
            parameter.reset()
