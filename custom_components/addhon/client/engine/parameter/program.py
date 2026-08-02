# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

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
        """`prCode -> program name`, restricted to the CANONICAL programs.

        The two skips are disambiguation, not filtering for its own sake: `prCode` does
        NOT identify a program. On a real washer catalog 154 categories share only 14
        distinct codes -- prCode 4 is claimed by 35 of them, prCode 1 by 34 -- because a
        downloaded (`iot_*`) program is a variant layered on a base program's code, and a
        favourite is a copy of a base category under the user's own name. Without the
        skips the last iteration would win and the map would return an arbitrary one of
        the 34; with them the base program wins deterministically. No prCode is claimed
        by `iot_*` categories alone, so nothing becomes unmappable.

        The flip side is a ceiling this map cannot break: it can only ever name the BASE
        program of a code. See `name_for_code` for the precise resolution.
        """
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

    @staticmethod
    def _category_code(category: Any) -> int | None:
        """`prCode` of a program category as an int, or None if absent/unparsable."""
        parameters = getattr(category, "parameters", None)
        if not isinstance(parameters, dict):
            return None
        code = parameters.get("prCode")
        if code is None:
            return None
        try:
            return int(code.value)
        except (AttributeError, TypeError, ValueError):
            return None


    def name_for_code(self, code: int) -> str | None:
        """Program name for the live `prCode`, preferring the ACTIVE category.

        `ids` alone can only name the base program of a code (see its docstring), so
        while a downloaded "Perfect White" runs it would answer "Cottons" -- both share
        prCode 115 on a real HW80. But we also hold the precise identity: `self._value`
        is the active startProgram category, recovered from the command history at load
        and updated by the category swap when a program is started. When that category's
        OWN prCode matches the code the appliance reports, the two agree and the active
        name is the exact answer.

        When they disagree the active category is stale -- typically a cycle started from
        the machine's own dial, which the cloud does not attribute to a category -- and we
        fall back to the unambiguous base program. That fallback is today's behaviour, so
        a mismatch is never a regression.

        """
        active = self._programs.get(str(self._value))
        # The active category is trusted ONLY when it was deliberately selected -- the
        # last-started program recovered from the command history, or one set through the
        # program parameter. On a fresh load with no history `_value` is merely the
        # schema's FIRST category, so its prCode agreeing is a coincidence, not evidence:
        # prCode 115 is shared by 3 categories on a real HW80 and prCode 4 by 35, so
        # whichever the schema happens to list first would hijack the name and could
        # report a downloaded program that was never started. Falling through to `ids`
        # then yields the base program, which is what shipped before.
        if (
            active is not None
            and getattr(active, "selected_explicitly", False)
            and self._category_code(active) == code
        ):
            return str(self._value)
        return self.ids.get(code)


    def set_value(self, value: str) -> None:
        self._value = value
