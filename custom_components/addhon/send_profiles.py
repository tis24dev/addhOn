# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How the transactional dispatcher shapes a command, per appliance type.

The official app has no single way of building a command body: it has one builder
per appliance family, and they disagree on what goes in `parameters`
(apk/analysis/issue113-hw-hpwh-control-model.md, section 7, and
apk/analysis/app-command-model.md). A profile is this integration's copy of one of
them. `LEGACY` is what the dispatcher has always done and stays the answer for every
type until that type is migrated deliberately, with a real device behind it.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class SendProfile:
    """One family's command shape.

    - `backfill_mandatory`: add every parameter the schema marks mandatory that the
      patch did not name.
    - `include_rule_changes`: add the siblings a rule cascade moved.
    - `wire_commands`: the `commandName` the body carries, keyed by this
      integration's command name, where the two differ.
    - `energy_label`: put `energyLabel: "0"` in the body's `attributes`.
    - `ancillary_source`: the `(command, category)` whose `ancillaryParameters`
      every send carries, whatever command is sent; nothing if the appliance lacks
      it. None: each command carries its own.
    """

    name: str
    backfill_mandatory: bool = True
    include_rule_changes: bool = True
    wire_commands: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    energy_label: bool = True
    ancillary_source: tuple[str, str] | None = None


LEGACY = SendProfile(name="legacy")

# The heat-pump water heater, as the app's `getSendCommandPayload` /
# `mapSendCommandPayload` build it (apk2 decomp.txt:2328259-2328356,
# 1551297-1551370): `parameters` is exactly what the caller passed, the command is
# named `setParameters`, and `attributes` carries channel and origin only. Every
# send, the `startProgram` of a mode change included, takes its
# `ancillaryParameters` from `settings.setParameters` (decomp.txt:4506560-4506580
# mode change, 2327080-2327090 boost auto-off), and none when that is missing.
HPWH = SendProfile(
    name="hpwh",
    backfill_mandatory=False,
    include_rule_changes=False,
    wire_commands=MappingProxyType({"settings": "setParameters"}),
    energy_label=False,
    ancillary_source=("settings", "setParameters"),
)

_BY_TYPE: Mapping[str, SendProfile] = MappingProxyType({"HW": HPWH})


def profile_for(appliance_type: str | None) -> SendProfile:
    """The profile of one appliance type; `LEGACY` for anything not migrated."""
    return _BY_TYPE.get(appliance_type or "", LEGACY)
