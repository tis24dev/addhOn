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
from typing import Any, NamedTuple

from .enum import HonParameterEnum

_LOGGER = logging.getLogger(__name__)


class CycleActivity(NamedTuple):
    """The cycle the cloud's `activity` describes, as a context read found it.

    Issue #130: the activity names the running programme exactly, wherever the cycle
    was started -- `programName` `HQD_COTTONS` with `channel` `appliance` for a panel
    start, `IOT_WASH_DARK` with `mobileApp` for an app start -- while the shadow's
    prCode is shared by several programmes (115: Cottons, Perfect White, Denim). The
    app names its running card from it (`mapLastActivity`, apk2 decomp.txt @1551831).

    `programme` is `programName` as sent, `functional_id` the dishwasher's
    `functionalId` (its activity may name the programme by that alone), `channel`
    where the cycle was started, `label` the start's `prStrDisp`, `code` the shadow's
    prCode in the same read and `started` its `activityStarted`. Kept by the appliance
    for the types whose starts are followed (`HonAppliance.cycle_activity`), read by
    `HonParameterProgram.name_for_code`.
    """

    programme: Any
    functional_id: Any
    channel: Any
    label: Any
    code: int | None
    started: Any


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

    def reset(self) -> None:
        """No-op: this parameter has no schema to go back to.

        It is a VIEW over the command's categories and is built on an EMPTY attributes dict
        (`super().__init__(key, {}, group)`), so the inherited `reset()` -- which re-runs
        `_set_attributes()` -- would seed `_value` with the enum's fabricated "0" and blank
        `_typology`, and `name_for_code` trusts both. The selected program is not a value
        the schema prescribes: it is the choice the rebuild happens BECAUSE of
        (`HonCommand.rebuild_from_schema`).
        """

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

        When several base programs share a code, the first one whose `programFamily`
        says `dashboard` wins, as on the app's washer dashboard (its card rebuilt from
        the shadow takes the first `dashboard` category on the code, apk2 decomp.txt
        @2591908-2591914). Without one, the last base program wins, as before. A
        tumble-dryer's prCode 75 is claimed by `hqd_duvet` (`dashboard_auto`) and
        `hqd_feather` (`hidden`): the dial programme, not the hidden one.
        """
        values: dict[int, str] = {}
        dashboard: dict[int, str] = {}
        for name, parameter in self._programs.items():
            if "iot_" in name:
                continue
            if not parameter.parameters.get("prCode"):
                continue
            if (fav := parameter.parameters.get("favourite")) and fav.value == "1":
                continue
            code = int(parameter.parameters["prCode"].value)
            values[code] = name
            if code not in dashboard and "dashboard" in self._family_words(parameter):
                dashboard[code] = name
        return dict(sorted((values | dashboard).items()))

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

    def name_for_code(
        self,
        code: int,
        position: int | None = None,
        cycle: CycleActivity | None = None,
    ) -> str | None:
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

        `position` is the appliance-reported `prPosition`, the tie-breaker the hOn app's
        washer dashboard uses in its last fallback (`createCardFromShadows`, 2.27.9
        decomp.txt:2628490, 2.30.7 apk2 @2586342, which ANDs prCode and prPosition; the
        similarly named `findCurrentProgramNameFromPrCodeAndPrPosition` @2705620 belongs to
        the oven, not the washer). It is accepted here but deliberately applied
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

        `cycle` is the cycle the cloud's activity describes (see `CycleActivity`), None
        for the types whose starts are not followed. On the live code it comes first
        (issue #130): it names the programme wherever the cycle was started, and the
        active category only says what Home Assistant or the app started last -- on
        2026-10-16 Denim from the app, while Cottons, on the same prCode 115, ran from
        the panel. Its programme is taken when the catalog places it on the code,
        without the prPosition check, which the dishwasher's shadow fails (decision 8,
        as D5). An activity from the panel that names nothing usable leaves the code's
        own programme: the active category was certainly not started there (decision
        4). Any other activity that names nothing usable falls through to the rules
        below. Pure: the appliance keeps `cycle`, so a push and a poll can both call it.
        """
        if position is not None:
            self._log_position_discovery(code, position)
        active = self._programs.get(str(self._value))
        if cycle is not None and cycle.code == code:
            if (named := self._cycle_programme(cycle, code)) is not None:
                # A favourite is a copy of its base programme under the user's name: the
                # activity names the base, so a selected favourite of it keeps its name.
                if (
                    active is not None
                    and self._is_favourite(active)
                    and self._category_key(getattr(active, "category", "")) == named
                ):
                    return str(self._value)
                return named
            if cycle.channel == "appliance":
                return self.ids.get(code)
        # The active category is trusted ONLY when it was deliberately selected -- the
        # last-started program recovered from the command history, or one set through the
        # program parameter. On a fresh load with no history `_value` is merely the
        # schema's FIRST category, so its prCode agreeing is a coincidence, not evidence:
        # prCode 115 is shared by 3 categories on a real HW80 and prCode 4 by 35, so
        # whichever the schema happens to list first would hijack the name and could
        # report a downloaded program that was never started. Falling through to `ids`
        # then yields the base program, which is what shipped before.
        # It must also agree on prPosition when the appliance reports it (see below),
        # unless `/history` named it: the dishwasher's shadow reports a prPosition its
        # catalog does not declare (2 dumps out of 2), and the guard then hid every
        # programme recovered on a shared code (issue #112, decision D5).
        if (
            active is not None
            and getattr(active, "selected_explicitly", False)
            and self._category_code(active) == code
            and (
                position is None
                or getattr(active, "recovered_from_history", False)
                or self._category_position(active) == position
            )
        ):
            return str(self._value)
        if position is not None:
            exact = self._categories_matching(code, position)
            if len(exact) == 1:
                return exact[0]
        return self.ids.get(code)

    @staticmethod
    def _category_key(name: Any) -> str:
        """A programme's category key from the name a cloud record spells.

        The categories are keyed as `command_loader._clean_name` cleans them: the last
        dotted part, lowercased (`PROGRAMS.WM_WD.HQD_COTTONS` -> `hqd_cottons`); an
        activity spells the bare name (`HQD_COTTONS`), cut the same way.
        """
        return str(name).strip().rsplit(".", 1)[-1].lower()

    @staticmethod
    def _fixed(category: Any, key: str) -> str:
        """A category parameter's value as a stripped string, "" when it has none."""
        parameters = getattr(category, "parameters", None)
        parameter = parameters.get(key) if isinstance(parameters, dict) else None
        return str(getattr(parameter, "value", "") or "").strip()

    def _cycle_programme(self, cycle: CycleActivity, code: int) -> str | None:
        """The category `cycle` names, when the catalog places it on `code`.

        By `programName`, or, for a dishwasher activity without one, by `functionalId`:
        the first programme declaring it, as the app's `mapLastActivity` takes it (apk2
        decomp.txt @1551897-1551988). A favourite is keyed by text the user typed,
        which no activity spells. A programme filed under another code disagrees with
        the shadow read with it, so it names nothing.
        """
        key: str | None = None
        if isinstance(cycle.programme, str) and cycle.programme.strip():
            key = self._category_key(cycle.programme)
        elif cycle.functional_id not in (None, ""):
            wanted = str(cycle.functional_id).strip()
            key = next(
                (
                    name
                    for name, category in self._programs.items()
                    if not self._is_favourite(category)
                    and self._fixed(category, "functionalId") == wanted
                ),
                None,
            )
        category = self._programs.get(key) if key is not None else None
        if (
            category is None
            or self._is_favourite(category)
            or self._category_code(category) != code
        ):
            return None
        return key

    @staticmethod
    def _family_words(category: Any) -> frozenset[str]:
        """The words of a category's `programFamily`, or an empty set.

        Mirrors `select._family_words` (the HA layer is not imported from the engine):
        each value, stripped of `[]`, split on `|` and `_`.
        """
        parameters = getattr(category, "parameters", None)
        family = parameters.get("programFamily") if isinstance(parameters, dict) else None
        values = getattr(family, "values", None)
        if not isinstance(values, (list, tuple)):
            return frozenset()
        return frozenset(
            word
            for value in values
            for word in str(value).strip().strip("[]").lower().replace("|", "_").split("_")
            if word
        )

    def name_for_unit_code(self, code: int, code_key: str) -> str | None:
        """Programme name for a unit's own code (`WMprCode` / `TDprCode`), or None.

        The Laundry Centre (#124) puts a washer and a dryer under one MAC, and each
        unit reports its own code. The candidates are the categories, in catalogue
        order, whose `code_key` parameter reads `code`; categories of the other unit
        never match, since they carry the other unit's key. Answer, in order:

        1. the first candidate that is neither `iot_*` nor a favourite (the `ids` rule);
        2. else the first candidate whose `programFamily` words contain `dashboard`
           (the app's washer dashboard, P6);
        3. else the first candidate.

        It never reads `self._value`: the active category is not evidence of what runs
        (P5, pending). And `ids`' "no prCode is claimed by iot_* alone" does not hold on
        the LC catalogue (WM code 9 belongs to `iot_*` categories only), hence rule 2/3.
        """
        candidates = [
            (name, category)
            for name, category in self._programs.items()
            if self._category_int(category, code_key) == code
        ]
        for name, category in candidates:
            if "iot_" not in name and not self._is_favourite(category):
                return name
        for name, category in candidates:
            if "dashboard" in self._family_words(category):
                return name
        return candidates[0][0] if candidates else None

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
