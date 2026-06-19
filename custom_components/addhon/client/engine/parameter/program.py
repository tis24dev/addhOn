"""Program parameter.

A "program" parameter is not an enum of data: it is a VIEW over the command's
categories (the programs). Reading `value` = the command's current category;
writing `value` = changing the command's category (and thus the active command
on the appliance). `values` = the program names (categories) filtering out the iot recipes.
Subclass of the enum because the rules do `isinstance(param, enum)` and a
program counts as an enum.

`command` is duck-typed (our HonCommand): it needs `.category` (str) and
`.categories` (dict name->command).
"""
from __future__ import annotations

from typing import Any

from .enum import HonParameterEnum


class HonParameterProgram(HonParameterEnum):
    _FILTER = ["iot_recipe", "iot_guided"]

    def __init__(self, key: str, command: Any, group: str) -> None:
        super().__init__(key, {}, group)
        self._command = command
        if "PROGRAM" in command.category:
            self._value = command.category.split(".")[-1].lower()
        else:
            self._value = command.category
        self._programs: dict[str, Any] = command.categories
        self._typology = "enum"

    @property
    def value(self) -> str | float:
        return self._value

    @value.setter
    def value(self, value: str | float) -> None:
        if value in self.values:
            self._command.category = str(value)
        else:
            raise ValueError(f"Allowed values: {self.values} But was: {value}")

    @property
    def values(self) -> list[str]:
        values = [v for v in self._programs if all(f not in v for f in self._FILTER)]
        return sorted(values)

    @values.setter
    def values(self, values: list[str]) -> None:
        raise ValueError("Cant set values {values}")

    @property
    def ids(self) -> dict[int, str]:
        values: dict[int, str] = {}
        for name, parameter in self._programs.items():
            if "iot_" in name:
                continue
            if not parameter.parameters.get("prCode"):
                continue
            if (fav := parameter.parameters.get("favourite")) and fav.value == "1":
                continue
            values[int(parameter.parameters["prCode"].value)] = name
        return dict(sorted(values.items()))

    def set_value(self, value: str) -> None:
        self._value = value
