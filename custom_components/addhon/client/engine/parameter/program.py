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

import logging
from typing import Any

from .enum import HonParameterEnum

_LOGGER = logging.getLogger(__name__)


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
        # One-shot latch for the prPosition discovery log (see `name_for_code`). The
        # attributes are re-derived on every poll, so without it a device that DOES
        # report the field would log on each refresh.
        self._position_logged = False

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
    def _category_int(category: Any, key: str) -> int | None:
        """Integer value of a category's `key` parameter, or None if absent/unparsable."""
        parameters = getattr(category, "parameters", None)
        if not isinstance(parameters, dict):
            return None
        parameter = parameters.get(key)
        if parameter is None:
            return None
        try:
            return int(parameter.value)
        except (AttributeError, TypeError, ValueError):
            return None

    @classmethod
    def _category_code(cls, category: Any) -> int | None:
        """`prCode` of a program category as an int, or None if absent/unparsable."""
        return cls._category_int(category, "prCode")

    @classmethod
    def _category_position(cls, category: Any) -> int | None:
        """`prPosition` of a program category as an int, or None if absent/unparsable.

        `prPosition` is the physical dial slot. In the schema it is declared per category
        as `{'category': 'command', 'typology': 'fixed', 'fixedValue': '<N>'}`.
        """
        return cls._category_int(category, "prPosition")

    def name_for_code(self, code: int, position: int | None = None) -> str | None:
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

        `position` is the appliance-reported `prPosition`, the tie-breaker the hOn app
        itself uses (`findCurrentProgramNameFromPrCodeAndPrPosition`, decomp.txt:2705620,
        which ANDs prCode and prPosition). It is accepted here but deliberately applied
        under a STRICT-NARROWING rule -- used only when prCode+prPosition select exactly
        ONE category -- so it can only ever turn an ambiguous answer into a precise one,
        never change an already-unambiguous one. That rule also makes it self-limiting in
        the case where it is useless: a downloaded program is loaded onto a base
        program's dial slot and inherits BOTH fields, so those groups keep >1 candidate
        and fall through untouched.

        NOTE: no appliance observed so far reports `prPosition` in its shadow (checked on
        the real WM/TD/REF dumps in diagnostics/live-2026-06-22/), so this path is
        currently dead code kept deliberately -- see `_log_position_discovery`.
        Full analysis: apk/analysis/program-identity-prcode.md.
        """
        if position is not None:
            self._log_position_discovery(code, position)
        active = self._programs.get(str(self._value))
        # The active category is trusted ONLY when it was deliberately selected -- the
        # last-started program recovered from the command history, or one set through the
        # program parameter. On a fresh load with no history `_value` is merely the
        # schema's FIRST category, so its prCode agreeing is a coincidence, not evidence:
        # prCode 115 is shared by 3 categories on a real HW80 and prCode 4 by 35, so
        # whichever the schema happens to list first would hijack the name and could
        # report a downloaded program that was never started. Falling through to `ids`
        # then yields the base program, which is what shipped before.
        # It must also agree on prPosition when the appliance reports it (see below).
        if (
            active is not None
            and getattr(active, "selected_explicitly", False)
            and self._category_code(active) == code
            and (position is None or self._category_position(active) == position)
        ):
            return str(self._value)
        if position is not None:
            exact = self._categories_matching(code, position)
            if len(exact) == 1:
                return exact[0]
        return self.ids.get(code)

    @staticmethod
    def _is_favourite(category: Any) -> bool:
        """True if this category is a favourite, i.e. keyed by a USER-TYPED name.

        `HonCommandLoader._add_favourites` injects a fixed `favourite="1"` parameter and
        files the copy under `favouriteName`. That marker (the same one `ids` filters on)
        is what tells a schema slug apart from text the user typed -- which matters
        because the discovery log prints category names.
        """
        parameters = getattr(category, "parameters", None)
        if not isinstance(parameters, dict):
            return False
        favourite = parameters.get("favourite")
        return favourite is not None and str(getattr(favourite, "value", "")) == "1"

    def _categories_matching(
        self, code: int, position: int | None, schema_only: bool = False
    ) -> list[str]:
        """Category names whose prCode (and prPosition, when given) match.

        `schema_only` drops favourites, whose names are user-typed (see `_is_favourite`).
        """
        return [
            name
            for name, category in self._programs.items()
            if self._category_code(category) == code
            and (position is None or self._category_position(category) == position)
            and not (schema_only and self._is_favourite(category))
        ]

    def _log_position_discovery(self, code: int, position: int) -> None:
        """Report, ONCE, that an appliance actually reports `prPosition`.

        This is the whole reason the `position` path exists. The tie-breaker cannot be
        validated against any dump we have, and it never will be while nothing tells us a
        device started sending the field -- the gap would stay permanently unverifiable.
        So the first time we see it, we log what it is worth ON THIS MODEL: how many
        categories share the prCode, and how many survive adding prPosition. That single
        line is the evidence a future implementation needs.

        Logged at INFO on purpose, not debug: it fires only on a device that reports the
        field (none known today), so it is zero-noise and would otherwise be invisible
        without the user enabling debug. Grep marker: "prPosition".

        The sample names are SCHEMA-ONLY. Category names normally come from the appliance
        schema, but favourites are filed under the name the user typed for them, and this
        line asks to be attached to a diagnostics report -- so a nickname must not ride
        along. The counts stay whole (they are the actionable part); only the printed
        samples are filtered.
        """
        if self._position_logged:
            return
        self._position_logged = True
        by_code = self._categories_matching(code, None)
        by_both = self._categories_matching(code, position)
        sample_code = self._categories_matching(code, None, schema_only=True)
        sample_both = self._categories_matching(code, position, schema_only=True)
        _LOGGER.info(
            "Program debug: this appliance REPORTS prPosition=%s (prCode=%s) -- the first "
            "one known to. Candidates by prCode alone: %d %s; adding prPosition: %d %s. "
            "Please attach this line to a diagnostics report: it is the evidence needed "
            "to enable the prCode+prPosition tie-break (apk/analysis/"
            "program-identity-prcode.md).",
            position,
            code,
            len(by_code),
            sorted(sample_code)[:8],
            len(by_both),
            sorted(sample_both)[:8],
        )

    def set_value(self, value: str) -> None:
        self._value = value
