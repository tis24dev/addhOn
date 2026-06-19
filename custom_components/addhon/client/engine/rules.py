"""Rules engine.

A "rule" ties a trigger-parameter to an action on another parameter: when the
trigger takes a certain value, the target is constrained (fixed value, or
restricted to an enum/range). It hooks in via `parameter.add_trigger` (the
trigger system of the base parameter): when the trigger changes value,
`check_trigger` runs the callbacks registered here.

rules MODEL (see apk/analysis/rules-model.md):
  `ancillaryParameters.programRules` IS the parameter with `category=="rule"`, same
  node, with nesting `{targetParam: {triggerParam: {triggerValue: action}}}`
  (+ nested extra-conditions e.g.
  `tempSel: {ecoMode: {"1": {machMode: {"1": {fixedValue:"26"}}}}}`).
  The extra-conditions are matched in `_extra_rules_matches` (see below) by comparing
  `str(param.value)`: on the real AC ecoMode=1 constrains
  tempSel/windSpeed/windDirection as the app does.
  NOTE: rules with the `$installationType` trigger (static multi-split config, not a
  parameter) do NOT fire (`$` not stripped, options empty at construction);
  low impact (remoteVisible/selfClean), not implemented blindly.

`isinstance` here is against the parameter classes: parameters, commands and
rules are a cohesive cluster.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .parameter.base import HonParameter
from .parameter.enum import HonParameterEnum
from .parameter.range import HonParameterRange


@dataclass
class HonRule:
    trigger_key: str
    trigger_value: str
    param_key: str
    param_data: dict[str, Any]
    extras: Optional[dict[str, str]] = None


# Trigger key `$<x>` = device CONFIG variable (the app's sigil), not a
# runtime parameter. Mapping -> field of the appliance record (from the decompiled app,
# `getMappedParamName`): the app knows ONLY `$installationType` -> `unitConfiguration`.
_DOLLAR_FIELDS = {"$installationType": "unitConfiguration"}


class HonRuleSet:
    def __init__(self, command: Any, rule: dict[str, Any]) -> None:
        self._command = command
        self._rules: dict[str, list[HonRule]] = {}
        # "config" rules (trigger `$...`): resolved statically, not via triggers.
        self._config_rules: list[tuple[str, str, dict[str, Any]]] = []
        self._parse_rule(rule)

    @property
    def rules(self) -> dict[str, list[HonRule]]:
        return self._rules

    def _parse_rule(self, rule: dict[str, Any]) -> None:
        for param_key, params in rule.items():
            param_key = self._command.appliance.options.get(param_key, param_key)
            for trigger_key, trigger_data in params.items():
                self._parse_conditions(param_key, trigger_key, trigger_data)

    def _parse_conditions(
        self,
        param_key: str,
        trigger_key: str,
        trigger_data: dict[str, Any],
        extra: Optional[dict[str, str]] = None,
    ) -> None:
        if extra is None and trigger_key.startswith("$"):
            # CONFIG-rule (app model): the `$installationType` trigger is not a
            # parameter but a device field (unitConfiguration). Resolved statically
            # in `patch()` against the appliance record, not as a runtime trigger.
            self._config_rules.append((param_key, trigger_key, trigger_data))
            return
        trigger_key = trigger_key.replace("@", "")
        trigger_key = self._command.appliance.options.get(trigger_key, trigger_key)
        for multi_trigger_value, param_data in trigger_data.items():
            for trigger_value in multi_trigger_value.split("|"):
                if isinstance(param_data, dict) and "typology" in param_data:
                    self._create_rule(
                        param_key, trigger_key, trigger_value, param_data, extra
                    )
                elif isinstance(param_data, dict):
                    # Per-branch copy: `extra` must not be mutated/shared across the
                    # loop iterations, otherwise a rule already created in an earlier
                    # branch would see a later branch's condition (e.g. ecoMode 1 -> 2).
                    branch_extra = dict(extra or {})
                    branch_extra[trigger_key] = trigger_value
                    for extra_key, extra_data in param_data.items():
                        self._parse_conditions(
                            param_key, extra_key, extra_data, branch_extra
                        )
                else:
                    param_data = {"typology": "fixed", "fixedValue": param_data}
                    self._create_rule(
                        param_key, trigger_key, trigger_value, param_data, extra
                    )

    def _create_rule(
        self,
        param_key: str,
        trigger_key: str,
        trigger_value: str,
        param_data: dict[str, Any],
        extras: Optional[dict[str, str]] = None,
    ) -> None:
        if param_data.get("fixedValue") == f"@{param_key}":
            return
        self._rules.setdefault(trigger_key, []).append(
            HonRule(
                trigger_key,
                trigger_value,
                param_key,
                param_data,
                extras.copy() if extras is not None else None,
            )
        )

    def _duplicate_for_extra_conditions(self) -> None:
        new: dict[str, list[HonRule]] = {}
        for rules in self._rules.values():
            for rule in rules:
                if rule.extras is None:
                    continue
                for key, value in rule.extras.items():
                    extras = rule.extras.copy()
                    extras.pop(key)
                    extras[rule.trigger_key] = rule.trigger_value
                    new.setdefault(key, []).append(
                        HonRule(key, value, rule.param_key, rule.param_data, extras)
                    )
        for key, rules in new.items():
            for rule in rules:
                self._rules.setdefault(key, []).append(rule)

    def _extra_rules_matches(self, rule: HonRule) -> bool:
        if rule.extras:
            for key, value in rule.extras.items():
                param = self._command.parameters.get(key)
                if not param:
                    return False
                # Compare the parameter VALUE, not the object: `str(param.value)`
                # against `str(value)`, so the extra-conditions (nested rules,
                # e.g. AC `ecoMode==1 AND machMode==1 -> tempSel=26`) fire only when
                # the actual value matches. On the real AC ecoMode=1 correctly
                # constrains tempSel/windSpeed/windDirection as the app does.
                if str(param.value) != str(value):
                    return False
        return True

    def _apply_fixed(self, param: HonParameter, value: str | float) -> None:
        if isinstance(param, HonParameterEnum) and set(param.values) != {str(value)}:
            param.values = [str(value)]
            param.value = str(value)
        elif isinstance(param, HonParameterRange):
            numeric = float(value)
            if numeric < param.min:
                param.min = numeric
            elif numeric > param.max:
                param.max = numeric
            # Pass a STRING to the setter: str_to_float tries int() first and a float
            # like 22.5 would be truncated to 22 (see helpers.str_to_float). The string
            # preserves decimals (same reason number.py sends setpoints as str).
            param.value = str(value)
            return
        param.value = str(value)

    def _apply_enum(self, param: HonParameter, rule: HonRule) -> None:
        if not isinstance(param, HonParameterEnum):
            return
        if enum_values := rule.param_data.get("enumValues"):
            param.values = enum_values.split("|")
        if default_value := rule.param_data.get("defaultValue"):
            # NB enum-casing: if `defaultValue` has a casing different
            # from its `enumValues`, the setter accepts it (it compares on the
            # normalized value), and the trigger caller swallows any error.
            # Degenerate case, not verifiable offline -> deferred to live-AC.
            param.value = default_value

    def _add_trigger(self, parameter: HonParameter, data: HonRule) -> None:
        def apply(rule: HonRule) -> None:
            if not self._extra_rules_matches(rule):
                return
            if not (param := self._command.parameters.get(rule.param_key)):
                return
            if fixed_value := rule.param_data.get("fixedValue", ""):
                self._apply_fixed(param, fixed_value)
            elif rule.param_data.get("typology") == "enum":
                self._apply_enum(param, rule)

        parameter.add_trigger(data.trigger_value, apply, data)

    def _apply_config_rules(self) -> None:
        """Apply the rules with a `$...` trigger (static device config) as the app does:
        resolve the appliance record field (e.g. `$installationType`->`unitConfiguration`),
        index the branch by the device value and write its `fixedValue`/enum into the target.
        Static (the value is a persistent device property, it does not change while operating).
        If the device lacks that field or there is no branch for its value -> it does not fire
        (like the app: `if(!r5) return`). Validated live: AC `unitConfiguration='1to1'` -> no
        branch (the rules have only 1to2/1toN) -> does not fire, correct."""
        if not self._config_rules:
            return
        info = getattr(self._command.appliance, "info", {}) or {}
        for param_key, dollar_key, branch_map in self._config_rules:
            field = _DOLLAR_FIELDS.get(dollar_key, dollar_key)
            device_value = info.get(field)
            if device_value is None:
                continue
            action = branch_map.get(str(device_value))
            if not isinstance(action, dict):
                continue
            if not (param := self._command.parameters.get(param_key)):
                continue
            if fixed_value := action.get("fixedValue", ""):
                self._apply_fixed(param, fixed_value)
            elif action.get("typology") == "enum":
                self._apply_enum(
                    param, HonRule(dollar_key, str(device_value), param_key, action)
                )

    def patch(self) -> None:
        self._duplicate_for_extra_conditions()
        for name, parameter in self._command.parameters.items():
            if name not in self._rules:
                continue
            for data in self._rules.get(name, []):
                self._add_trigger(parameter, data)
        self._apply_config_rules()
