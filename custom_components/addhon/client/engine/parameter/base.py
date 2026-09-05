# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Base HonParameter.

The trigger system (`add_trigger`/`check_trigger`/`triggers`) is the surface through
which the rules drive the parameters; the commands+rules cluster interoperates with it.
`value` defaults to "0" if None (the entities and `intern_value` rely on it).
"""
from __future__ import annotations

from typing import Any, Callable


class HonParameter:
    def __init__(self, key: str, attributes: dict[str, Any], group: str) -> None:
        self._key = key
        self._attributes = attributes
        self._category: str = ""
        self._typology: str = ""
        self._mandatory: int = 0
        self._value: str | float = ""
        self._group = group
        # value-trigger -> list of (callback, rule). Typed Any so the engine is not
        # tied to the HonRule class (our HonRule).
        self._triggers: dict[str, list[tuple[Callable[[Any], None], Any]]] = {}
        self._set_attributes()

    def _set_attributes(self) -> None:
        self._category = self._attributes.get("category", "")
        self._typology = self._attributes.get("typology", "")
        self._mandatory = self._attributes.get("mandatory", 0)

    @property
    def key(self) -> str:
        return self._key

    @property
    def declares_value(self) -> bool:
        """True when the schema itself said what this parameter should carry.

        Some nodes are pure descriptors: they list `enumValues` so a client can render
        a control, and state no `defaultValue`/`fixedValue` because there is nothing to
        send. The subclasses still materialize a value (see HonParameterEnum, which
        falls back to "0" so reads never return None), so the fallback is
        indistinguishable from a real one once you only look at `intern_value`. This
        keeps the distinction available to the payload builder.

        A declared "0" counts, hence the comparison against None/"" rather than a
        truthiness test -- a numeric 0 default is a value the schema asked for.
        """
        return any(
            self._attributes.get(name) not in (None, "")
            for name in ("defaultValue", "fixedValue")
        )

    @property
    def value(self) -> str | float:
        return self._value if self._value is not None else "0"

    @value.setter
    def value(self, value: str | float) -> None:
        self._value = value
        self.check_trigger(value)

    @property
    def intern_value(self) -> str:
        return str(self.value)

    @property
    def values(self) -> list[str]:
        return [str(self.value)]

    def option_count(self) -> int:
        """Cheap count of selectable ``values``.

        Default = len of the (already small) materialized list, correct for
        enum/fixed/program. HonParameterRange overrides it to count the min..max grid
        ARITHMETICALLY, so HonCommand._more_options can compare cardinality on a merge
        without materializing a range of up to _MAX_RANGE_VALUES strings.
        """
        return len(self.values)

    @property
    def category(self) -> str:
        return self._category

    @property
    def typology(self) -> str:
        return self._typology

    @property
    def mandatory(self) -> int:
        return self._mandatory

    @property
    def group(self) -> str:
        return self._group

    def add_trigger(self, value: str, func: Callable[[Any], None], data: Any) -> None:
        """Register `func` to run when this parameter is SET to `value`. Never fires here.

        pyhOn also fired the rule on registration whenever the parameter's default
        already equalled the trigger value, so a rule could apply at COMMAND-BUILD time,
        before anything had changed. The app has no such step: its only generic
        `programRules` pass is `updateProgramRulesParameters` (decomp.txt:1028648), it
        runs while building the program list, it reads the trigger value off the
        APPLIANCE RECORD (`activeAppliance[getMappedParamName(trigger)]`,
        decomp.txt:1028738-1028740), and it only assigns a value -- it never narrows a
        parameter's value set.

        That build-time fire broke every wash-and-dry program of every washer-dryer
        (issue #99). `dryOption` is an ancillary descriptor that defaults to "0" and
        carries the rule `dryLevel <- dryOption==0 -> "0"`; the app never reads it (it is
        not a field of the appliance record), while we flatten ancillaryParameters into
        the same `parameters` dict, so the rule fired on construction and collapsed
        `dryLevel` from its real enum to ["0"] before the user ever saw an entity -- the
        cycle then started as wash-only. In the app's own bundled catalogue all 72
        categories carrying that rule are W+D or W+D+S, i.e. exactly the programs that
        CAN dry: it is the starting position of the dry switch, not a ban.

        Rules that depend on the device's current state still apply: the shadow sync and
        every entity write go through the value setters, and `check_trigger` runs on each
        accepted write. Static device config keeps its own build-time pass in
        `HonRuleSet._apply_config_rules` ($installationType), which is the record-keyed
        mechanism the app actually has.
        """
        self._triggers.setdefault(value, []).append((func, data))

    def check_trigger(self, value: str | float) -> None:
        triggers = {str(k).lower(): v for k, v in self._triggers.items()}
        normalized = str(value).lower()
        if normalized in triggers:
            for func, args in triggers[normalized]:
                func(args)

    @property
    def triggers(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for value, rules in self._triggers.items():
            for _, rule in rules:
                if rule.extras:
                    param = result.setdefault(value, {})
                    for extra_key, extra_value in rule.extras.items():
                        param = param.setdefault(extra_key, {}).setdefault(
                            extra_value, {}
                        )
                else:
                    param = result.setdefault(value, {})
                if fixed_value := rule.param_data.get("fixedValue"):
                    param[rule.param_key] = fixed_value
                else:
                    param[rule.param_key] = rule.param_data.get("defaultValue", "")
        return result

    def reset_triggers(self) -> None:
        """Replace the trigger table with a fresh empty one.

        Used by `HonCommand.__copy__`: a shallow-copied parameter shares this dict with the
        original, whose rule callbacks close over the original command. Rebinding to a NEW
        dict (not `.clear()`, which would empty the shared/original table) lets the copy's
        rules be re-attached against the copy without touching the base."""
        self._triggers = {}

    def reset(self) -> None:
        self._set_attributes()
