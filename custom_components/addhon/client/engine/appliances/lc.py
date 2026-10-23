# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Native Laundry Centre (type LC) layer.

An LC is a washer (`WM*`) and a dryer (`TD*`) under one MAC, every shadow key prefixed by
its unit. Each unit reports its own programme code (`WMprCode` / `TDprCode`), looked up on
that unit's own categories of `startProgram` -- never on the active category (P5, pending).

No `programName`: it would name nothing on a two-unit appliance (#124).
"""
from __future__ import annotations

from typing import Any

from ..parameter.program import HonParameterProgram
from .base import ApplianceExtra


class Appliance(ApplianceExtra):
    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        params = data.get("parameters", {})
        start_cmd = self.parent.settings.get("startProgram.program")
        # A unit's name is written only when the shadow carries that unit's code: with
        # no key at all there is no unit, and a name would conjure its device. A stale
        # value from an earlier derivation must not survive either.
        if "WMprCode" in params:
            data["WMprogramName"] = self._unit_program(params, start_cmd, "WMprCode")
        else:
            data.pop("WMprogramName", None)
        if "TDprCode" in params:
            data["TDprogramName"] = self._unit_program(params, start_cmd, "TDprCode")
        else:
            data.pop("TDprogramName", None)
        data["available"] = bool(self.parent.connection)
        return data

    def _unit_program(self, params: dict[str, Any], start_cmd: Any, code_key: str) -> str:
        """Slug of the programme a unit runs, "No Program" for code 0, unreadable or unmatched."""
        code = self._int_or_none(params, code_key)
        if code and isinstance(start_cmd, HonParameterProgram):
            return start_cmd.name_for_unit_code(code, code_key) or "No Program"
        return "No Program"
