# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

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

import math
from collections.abc import Mapping
from copy import copy
from typing import Any, Optional, Union

from .energy_label import actual_duration, ancillary_values
from .energy_label import energy_label as label_for_duration
from .exceptions import ApiError, NoAuthenticationException
from .parameter.base import HonParameter
from .parameter.enum import HonParameterEnum
from .parameter.fixed import HonParameterFixed
from .parameter.program import HonParameterProgram
from .parameter.range import HonParameterRange
from .rules import HonRuleSet

import logging

_LOGGER = logging.getLogger(__name__)


def _is_zero(value: object) -> bool:
    """The app's `(value || '0') === '0'`: absent, empty or "0" all mean off."""
    return value is None or str(value) in ("", "0")


def _js_number(value: object) -> float:
    """The app's `value - 0`: a blank string is 0, anything unreadable NaN.

    None stands for JavaScript's `undefined` (a missing node), which is NaN too.
    """
    if value is None:
        return math.nan
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return 0.0
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return math.nan


def _whole_minutes(value: object) -> int:
    """`value` as a whole number of minutes; 0 when absent or unreadable."""
    try:
        return int(float(str(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


class _CanonicalExactPayload(dict[str, str | float]):
    __slots__ = ("command",)

    def __init__(
        self,
        command: HonCommand,
        params: Mapping[str, str | float],
    ) -> None:
        super().__init__(params)
        self.command = command


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
        # Set only when this category is deliberately chosen (see the `category` setter
        # and `mark_selected_explicitly`). Declared here rather than created on first
        # write so the state is explicit and `__copy__` can reset it.
        self._selected_explicitly = False
        # The name the user gave a favourite in the app; empty on a schema category.
        # Set by `HonCommandLoader._add_favourites` on its copy (see `favourite_name`).
        self._favourite_name = ""
        # The category's `remainingTimes` group exactly as the cloud sent it (issue #99,
        # see `remaining_times`). Kept aside, not instead: `_load_parameters` still files
        # its sub-nodes as before.
        self._remaining_times: Any = attributes.get("remainingTimes")
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
        # A copy has NOT been selected, whatever the original was. `_add_favourites`
        # copies a base program category, so without this a favourite could inherit the
        # flag and `HonParameterProgram.name_for_code` would trust it as the running
        # program. Unreachable today only because `_add_favourites` runs before
        # `_recover_last_command_states` and nothing is marked yet -- an ordering, not an
        # invariant. Isolating it here matches what this method already does for
        # `_parameters`, the triggers and the rule sets.
        new._selected_explicitly = False
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
    def remaining_times(self) -> Any:
        """The category's raw `remainingTimes` group, None when the catalog has none.

        The table the hOn app sums a washer programme's duration from off the HQD
        platform (issue #99, see `energy_label.actual_duration`). `_load_parameters`
        also walks this group, but files each sub-node flat in `data` under its own
        name (`dirtyLevel`, `spinSpeed`, `prewash`...), where the group is lost and a
        same-named node of another group would overwrite it; this keeps the group
        whole. A favourite is a `copy` of its category and shares it.
        """
        return self._remaining_times

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

    async def send(
        self, only_mandatory: bool = False, *, program_label: str | None = None
    ) -> bool:
        """Send this command's parameters.

        `program_label`: the programme's name in the user's language, which a washer
        start carries as `prStr` (see `_program_label_to_send`). The catalog it comes
        from lives in the Home Assistant layer, hence the argument.
        """
        grouped_params = (
            self.mandatory_parameter_groups if only_mandatory else self.parameter_groups
        )
        params = grouped_params.get("parameters", {})
        if "dryTime" in params and self._resolves_dry_time():
            params["dryTime"] = self._dry_time_to_send(params)
        return await self.send_parameters(params, program_label=program_label)

    def _resolves_dry_time(self) -> bool:
        """Whether this send carries the washer / washer-dryer `dryTime` rule.

        Only a `startProgram` of a WM or WD: that is where the app's program screens
        apply it (decomp.txt:4491766-4491914, skipped for DW), and a tumble dryer's
        drying time is a different setting. Everything else sends `dryTime` untouched.
        """
        appliance_type = getattr(self._appliance, "appliance_type", "")
        return self._name == "startProgram" and appliance_type in ("WM", "WD")

    def _dry_time_to_send(self, params: Mapping[str, str | float]) -> str | float:
        """`dryTime` as the app's classic program screen sends it. Issue #99.

        On a washer-dryer `dryTime` 1..4 is 30/60/90/120 minutes of TIMED drying
        (decomp.txt:1757888, `dryTime-1` = `drying-30`), and a dry level and a drying
        time are two modes of the same drawer. The classic screen
        (`ProgramCycleDetailsView`, decomp.txt:4491800-4491856) settles it once:

            dryLevel != '0' and programType != 'D'  ->  dryTime = '0'  (the level wins)
            otherwise                               ->  dryTime = its value, or '0'

        A missing dryLevel counts as '0' (`dryLevel || '0'` there), and programType is
        matched on its raw value as the app does: an enum reads back lower-cased
        (`clean_value`), so `value` would never equal 'D'. "Its value" is what
        the program or a write gave it; a range the schema left without a default has
        none, only the `min` this engine invents for reads, so it goes as '0'.

        This is the screen whose payload the reporter's own app history shows
        (`dryLevel=1`, `dryTime=0`, `energyLabel=1`), and "0" is the only value proven
        on his appliance (BHA6SD696M6DB980). What we sent before, and why it failed:
        the range's `min`, 1, turned the Eco 40-60 wash-and-dry into wash + 30 minutes
        by the clock (5.22.1); null got an HTTP 500 from the cloud (5.26.0-beta4) --
        and no path of the app ever sends null. Analysis:
        apk/analysis/wm-wd-send-pipeline.md and
        apk/analysis/issue98-99-program-options-and-wd-dry.md sections 11-12.

        IF DRYING PROGRAMS MISBEHAVE AFTER THIS, the app's other screen is the fallback:
        `NewProgramCycleDetails` drops an unset `dryTime` from the body altogether
        (`_normalize`, decomp.txt:2676915-2676974) instead of sending '0'.
        """
        program_type = self._parameters.get("programType")
        if not _is_zero(params.get("dryLevel")) and (
            program_type is None or program_type.intern_value != "D"
        ):
            return "0"
        dry_time = self._parameters.get("dryTime")
        if isinstance(dry_time, HonParameterRange) and dry_time.is_unset:
            return "0"
        return params["dryTime"]

    def _is_washer_start(self) -> bool:
        """A `startProgram` of a washer (WM) or washer-dryer (WD). Issue #112.

        Where the app's washer builder (`getSendCommandPayload`, apk2
        decomp.txt:1362211-1362920) adds what the generic body lacks. Every other
        command and type goes out exactly as before.
        """
        appliance_type = getattr(self._appliance, "appliance_type", "")
        return self._name == "startProgram" and appliance_type in ("WM", "WD")

    def _program_label_to_send(self, program_label: str | None) -> str:
        """`attributes.prStr` of a washer start, as the app names the programme.

        The app sends `translate(key)` with the key itself as the fallback (issue
        #112, apk/analysis/issue112-wm-hqd-program-options.md section 7.4): the
        catalog label in the user's language, else the raw category key such as
        `PROGRAMS.WM_WD.HQD_COTTONS`. A favourite goes by the name the user gave it,
        whatever label its base programme has. "" when there is nothing to name.
        """
        return self._favourite_name or program_label or self._category_name

    def _is_hqd(self) -> bool:
        """True on the HQD platform (`platform` in the model attributes, any case).

        Matched as `program_options.keep_fresh_hidden` matches it.
        """
        model = getattr(self._appliance, "model_attributes", None)
        platform = model.get("platform") if isinstance(model, Mapping) else None
        return str(platform or "").upper() == "HQD"

    def _energy_label(self, params: Mapping[str, str | float]) -> str:
        """`energyLabel` as the app computes it for a washer start. Issue #112.

        The builder (apk2 decomp.txt:1362480-1362560, 1362660-1362760) and
        `calculateEnergyLabel` (@1364133-1364170):

            temp not an enum, or no tempContribution.fixedValue  ->  '0'
            floor((500 - 1.6*minutes + 5*tc*(maxT - temp)) / 100), clamped to 1..5
            NaN                                                  ->  '0'

        `maxT` is the LAST entry of the schema's `enumValues` in the order the cloud
        sent them, not the highest; `minutes` the program's `remainingTime`; `temp`
        the value this body carries. `tc` is tested the JavaScript way: only a
        missing or blank value fails, the string '0' passes and zeroes the term.

        Evidence: the app sends the result in both `attributes` and
        `ancillaryParameters` (apk/analysis/issue112-wm-hqd-program-options.md
        section 7.4); addhOn used to send '0' and the schema's default.
        """
        temp = self._parameters.get("temp")
        contribution = self._parameters.get("tempContribution")
        tc = contribution.schema_node.get("fixedValue") if contribution else None
        if temp is None or temp.typology != "enum" or not tc:
            return "0"
        # `enumValues[length - 1]`: a list's last entry; on the rare `A|B|C` string
        # shape, its last character, exactly as the app indexes it.
        enum_values = temp.schema_node.get("enumValues")
        max_temp = (
            _js_number(enum_values[-1])
            if isinstance(enum_values, (list, str)) and enum_values
            else math.nan
        )
        remaining = self._parameters.get("remainingTime")
        minutes = _js_number(remaining.schema_value) if remaining else math.nan
        sent = _js_number(params.get("temp") or 0)
        # Same operations in the same order as the app, so the float rounding agrees.
        score = (500 - minutes * 1.6 + 5 * _js_number(tc) * (max_temp - sent)) / 100
        if math.isnan(score):
            return "0"
        label = math.floor(score) if math.isfinite(score) else score
        if label > 5:
            return "5"
        if label <= 0:
            return "1"
        return str(int(label))

    def _table_energy_label(self, params: Mapping[str, str | float]) -> str | None:
        """`energyLabel` of a washer start off HQD, or None to send what we always did.

        Issue #99. Outside HQD the app has no `remainingTime` to read: it sums the
        category's `remainingTimes` table for the values this body carries (apk2
        decomp.txt:1367613-1368630) and writes the label in both places, as above.
        The duration sees what the builder passes it: `params` as they go on the
        wire, the ancillary schema mapped by `mapCommandParameters` (@1362222-1362234),
        `parameters.temp` and `ancillaryParameters.tempContribution.fixedValue` of
        the category (@1362207-1362221, @1362680-1362692).

        None when the app would send '0': no table, no duration out of it, a
        temperature that is not an enum, no contribution, or an unreadable value.
        Those starts deliberately keep the body they had (the schema's ancillary
        value and the attributes' "0") instead of switching to the app's '0'.
        Reference and cases: apk2/analysis/issue99-wd/4-actual-duration-energy-label.md.
        """
        ancillary_nodes = {
            name: parameter.schema_node
            for name, parameter in self._parameters.items()
            if parameter.group == "ancillaryParameters"
        }
        minutes = actual_duration(
            getattr(self._appliance, "appliance_type", ""),
            params,
            ancillary_values(ancillary_nodes),
            self._remaining_times,
        )
        if minutes is None:
            return None
        temp = self._parameters.get("temp")
        contribution = self._parameters.get("tempContribution")
        label = label_for_duration(
            minutes,
            temp.schema_node if temp is not None and temp.group == "parameters" else None,
            contribution.schema_node.get("fixedValue")
            if contribution is not None and contribution.group == "ancillaryParameters"
            else None,
            params.get("temp"),
        )
        return str(label) if label else None

    async def send_specific(self, param_names: list[str]) -> bool:
        params: dict[str, str | float] = {}
        for key, parameter in self._parameters.items():
            if key in param_names or parameter.mandatory:
                params[key] = parameter.value
        return await self.send_parameters(params)

    async def _send_parameters(
        self,
        params: dict[str, str | float],
        *,
        sync_shadow: bool,
        program_name: str | None = None,
        wire_command: str | None = None,
        energy_label: bool = True,
        ancillary_params: Mapping[str, str | float] | None = None,
        program_label: str | None = None,
    ) -> bool:
        """Transmit `params`; `program_name` overrides the category on the wire.

        `program_label` is the translated programme name for a washer start's
        `prStr` (see `_program_label_to_send`); ignored by every other send.

        `ancillary_params` None (the default) sends this command's own
        `ancillary_parameters()`, as always; a mapping, even an empty one, is sent
        instead (the heat-pump water heater's profile takes them from
        `settings.setParameters` for every command).

        `program_name` is the top-level `programName` of a `startProgram` body, and
        the default (None) keeps the historical behaviour: the command's own raw
        cloud category key, which is what a washer or a fridge program start has to
        carry. An explicit "" SUPPRESSES the key -- the caller is telling us this
        `startProgram` is not a program start at all.

        The cooker hood is why the override exists. Its `startProgram` is filed
        under a placeholder category the cloud invented for a command that starts
        nothing (the app never names it, and no such key exists in the app's
        translation catalogue), while the app's own hood body -- proven three times
        over in the decompiled sources -- carries no `programName` field whatsoever.
        Without a way to say "not a program" the hood could not use the only command
        that can set its `onOffStatus`. Every other caller passes nothing and is
        unaffected.
        """
        if not (
            isinstance(params, _CanonicalExactPayload)
            and params.command is self
        ):
            params = self.canonical_exact_payload(params)
        ancillary = (
            self.ancillary_parameters()
            if ancillary_params is None
            else dict(ancillary_params)
        )
        wire_energy_label: bool | str = energy_label
        if energy_label and self._is_washer_start() and self._is_hqd():
            # Issue #112: the app's value, the same in both places. Elsewhere the
            # attributes keep "0" and the ancillaries the schema's own value.
            wire_energy_label = self._energy_label(params)
            ancillary["energyLabel"] = wire_energy_label
        elif energy_label and self._is_washer_start():
            # Issue #99: off HQD the app works the label out of the remainingTimes
            # table, again the same in both places. Without a label the body stays
            # as it was: "0" and the schema's value.
            label = self._table_energy_label(params)
            if label is not None:
                wire_energy_label = label
                ancillary["energyLabel"] = label
        if self._is_washer_start() and _whole_minutes(params.get("delayTime")) > 0:
            # Issue #112: the app confirms every delayed washer start with
            # ecoDelayStart '0' (seen in each delayed command of the reporters'
            # histories); '1' belongs to its Eco Delay configuration, which we lack.
            ancillary["ecoDelayStart"] = "0"
        # Only a washer start names its programme; every other body keeps its shape.
        extra: dict[str, str] = {}
        if self._is_washer_start() and (
            label := self._program_label_to_send(program_label)
        ):
            extra["program_label"] = label
        if sync_shadow:
            self.appliance.sync_command_to_params(self.name)
        result = await self.api.send_command(
            self._appliance,
            self._name,
            params,
            ancillary,
            self._category_name if program_name is None else program_name,
            wire_command=wire_command,
            energy_label=wire_energy_label,
            **extra,
        )
        if not result:
            _LOGGER.error("Command rejected by cloud: %s", self._name)
            raise ApiError("Can't send command")
        return result

    def ancillary_parameters(self) -> dict[str, str | float]:
        """This command's `ancillaryParameters` as they go on the wire.

        Built from the parameters rather than from `parameter_groups`, which has
        already collapsed everything to `intern_value` and so cannot tell a value the
        schema asked for from one a subclass invented to keep reads non-None. Only
        the former belongs on the wire: a descriptor-only node such as the AC's
        windDirectionVerticalPositionSequence would otherwise travel as "0", a value
        outside its own enumValues, into the slot the app reads the louvre position
        sequence from. See `HonParameter.declares_value`.

        programRules is dropped deliberately, NOT for parity: the app does send it
        back on an AC startProgram. It is the constraint set the cloud handed us, we
        have already applied it locally, and echoing it risks re-pinning parameters
        the user has since moved.
        """
        return {
            name: parameter.intern_value
            for name, parameter in self._parameters.items()
            if parameter.group == "ancillaryParameters"
            and name != "programRules"
            and parameter.declares_value
        }

    async def send_parameters(
        self, params: dict[str, str | float], *, program_label: str | None = None
    ) -> bool:
        return await self._send_parameters(
            params, sync_shadow=True, program_label=program_label
        )

    def canonical_exact_payload(
        self,
        params: Mapping[str, str | float],
    ) -> dict[str, str | float]:
        payload = _CanonicalExactPayload(self, params)
        if "prStr" in payload:
            payload["prStr"] = self._category_name.upper()
        return payload

    async def send_exact(
        self,
        params: dict[str, str | float],
        *,
        program_name: str | None = None,
        wire_command: str | None = None,
        energy_label: bool = True,
        ancillary_params: Mapping[str, str | float] | None = None,
    ) -> bool:
        return await self._send_parameters(
            params,
            sync_shadow=False,
            program_name=program_name,
            wire_command=wire_command,
            energy_label=energy_label,
            ancillary_params=ancillary_params,
        )

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
            selected = self.categories[category]
            # Record that THIS category was deliberately chosen (a program set by the
            # user, or the last-started one recovered from the command history), as
            # opposed to being the schema's first entry that a fresh load leaves active
            # by default. `HonParameterProgram.name_for_code` needs to tell the two
            # apart: a default category sharing the reported prCode is a coincidence,
            # not evidence of what is running. See `selected_explicitly`.
            selected.mark_selected_explicitly()
            self._appliance.commands[self._name] = selected

    def mark_selected_explicitly(self) -> None:
        """Flag this category command as deliberately selected (see the category setter).

        Lives on the CATEGORY command rather than on the program parameter because a
        selection swaps the whole command object: a flag on the pre-swap parameter would
        not survive, while this one travels with the category that was picked.
        """
        self._selected_explicitly = True

    @property
    def selected_explicitly(self) -> bool:
        """True if this category was chosen, rather than left active by default."""
        return self._selected_explicitly

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

    @property
    def rule_targets(self) -> set[str]:
        """Names of the parameters this command's rules can write (see `HonRuleSet`)."""
        targets: set[str] = set()
        for ruleset in self._rules:
            targets |= ruleset.rule_targets
        return targets

    @property
    def favourite_name(self) -> str:
        """The user's name for this favourite, "" on a schema category.

        The favourite is a copy of its base category, so its `category` is still the
        base's raw key; this is the one place its own name survives. The app sends it
        as `prStr` when the favourite starts (issue #112).
        """
        return self._favourite_name

    @favourite_name.setter
    def favourite_name(self, name: str) -> None:
        self._favourite_name = name

    @property
    def is_favourite(self) -> bool:
        """True if this category is a saved favourite.

        Reads the `favourite="1"` marker `HonCommandLoader._add_favourites` injects into
        the copy -- the same one `HonParameterProgram._is_favourite` uses to tell a schema
        slug apart from a user-typed name."""
        favourite = self._parameters.get("favourite")
        return favourite is not None and str(getattr(favourite, "value", "")) == "1"

    def rebuild_from_schema(self) -> bool:
        """Put every parameter back to what its schema declares. True if it ran.

        This is `openProgramEpic` (@3618118-3618266), which the hOn app runs on every
        program opening: it rebuilds the whole parameter map from `dictionaryParameters`
        and drops it on unmount, so its display and its payload cannot drift apart. Our
        categories instead live for the whole config entry and are written by the Start
        path (`apply_pending_options`, kept on success), by `_recover_last_command_states`
        and by the favourites, with nothing ever putting them back -- which is why a
        program the user has run before starts at their old choice rather than at what it
        prescribes. Analysis: apk/analysis/issue98-99-program-options-and-wd-dry.md section 9.

        A FAVOURITE is refused: it IS the user's saved configuration, so rebuilding it
        would erase the thing they asked for. The app agrees -- opening a favourite feeds
        the saved values into `setValue`'s highest-precedence slot instead of reading the
        schema (@3617979).

        The program parameter is left alone by its own no-op `reset()`, and the static
        `$...` config rules go back on afterwards (see `HonRuleSet.reapply_static_rules`,
        which deliberately is not `patch()`). `_category_name`, `_selected_explicitly` and
        `_data` are not touched: they are identity and transport, not schema.
        """
        if self.is_favourite:
            return False
        for parameter in self._parameters.values():
            parameter.reset()
        for rule in self._rules:
            rule.reapply_static_rules()
        return True
