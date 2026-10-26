# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Native base ApplianceExtra.

Per-type hooks on the appliance state:
- `attributes(data)`: post-processes the shadow (adds derived fields).
- `settings(result)`: tweaks the settings dict (default: no-op).

`parent` is the appliance (duck-typed): it needs `.settings`, `.connection`.
The VALUES in `data["parameters"][...]` are `HonAttribute`s (native): we read them
duck-typed via `.value`/`str()`. The `isinstance` checks instead are against the
native PARAMETER classes.

Comparison helper: compare by VALUE (flags "1"/"0" as int 1/0), so the flags
evaluate correctly. The per-type `pause` attribute it derives is computed but not
surfaced as an entity (the Pause switch reads `machMode` directly).

A derived field is not free: it is written into the SAME dict the shadow parameters
land in, so diagnostics cannot tell it from cloud telemetry by shape and reports it as
an unmapped DEVICE capability. That is what happened to the fridge's `modeZ1`/`modeZ2`
in issue #93, and why they are gone (appliances/ref.py). `attributes_last_update`, which
carries exactly the shadow parameter set, is the discriminator when reading a dump.
"""
from __future__ import annotations

from typing import Any

from ..parameter.program import CycleActivity, HonParameterProgram


class ApplianceExtra:
    def __init__(self, appliance: Any) -> None:
        self.parent = appliance

    # --- attribute-reading helpers (duck-typed on HonAttribute) ---
    @staticmethod
    def _raw(params: dict[str, Any], key: str) -> str:
        """Raw value (string) via __str__. ONLY for fields never set to a number
        (e.g. prCode): after a numeric set __str__ would raise. For flags use _value."""
        if key not in params:
            return ""
        return str(params[key])

    @staticmethod
    def _value(params: dict[str, Any], key: str, default: Any = None) -> Any:
        """Typed attribute value (`.value`, numeric if convertible),
        default if absent."""
        attr = params.get(key)
        return attr.value if attr is not None and hasattr(attr, "value") else default

    @classmethod
    def _int_or_none(cls, params: dict[str, Any], key: str) -> int | None:
        """Integer reading of `key`, or None when absent/blank/non-numeric.

        Distinct from `_raw` (which flattens a missing key to ""): callers here need to
        tell "the appliance did not report this field" from "it reported 0". Used for the
        optional `prPosition` tie-break, whose whole contract is None = not reported.
        """
        # `_raw` is inside the try on purpose: it goes through `HonAttribute.__str__`,
        # which returns `self._value` unconverted, so a numerically-set attribute makes
        # `str()` raise TypeError (the hazard `_raw`'s own docstring warns about). An
        # optional read must never be able to break the whole attribute derivation.
        try:
            return int(cls._raw(params, key))
        except (TypeError, ValueError):
            return None

    @classmethod
    def _is_value(cls, params: dict[str, Any], key: str, expected: Any) -> bool:
        """True if the `key` attribute's `.value` == expected. Comparison by VALUE
        (flags "1"/"0" become int 1/0), so the flags evaluate correctly."""
        return cls._value(params, key) == expected

    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        # programName: the program's stable SLUG, which is also the hOn i18n key
        # (`PROGRAMS.WM_WD.HQD_AUTOCLEAN` -> `hqd_autoclean`). The engine deliberately
        # stops here and does not resolve the label: that needs the appliance type and a
        # downloaded catalog, both HA-layer concerns (see program_labels.py).
        #
        # Resolution goes through `name_for_code`, NOT the raw `ids` map: prCode is
        # ambiguous (up to 35 categories share one code), so `ids` can only ever name a
        # code's base program -- it would report "Cottons" while a downloaded "Perfect
        # White" runs. `name_for_code` answers, in order:
        #
        # 1. with the cycle the cloud's activity describes, when it is on the reported
        #    code (issue #130): the programme it names, or the favourite of it that its
        #    title or the selection names; a panel cycle that names nothing usable gives
        #    the code's own programme. The appliance keeps the cycle for the types whose
        #    starts are followed (WM, WD, TD, DW); for the others it is None;
        # 2. with the active category -- recovered from `/history` or selected -- when
        #    its own prCode agrees with the reported one;
        # 3. with `ids`, the code's own programme.
        #
        # Robustness: `_raw(...) or "0"` handles an empty/absent prCode -> "No Program"
        # instead of `int("")` -> ValueError.
        program_name = "No Program"
        params = data.get("parameters", {})
        if program := int(self._raw(params, "prCode") or "0"):
            start_cmd = self.parent.settings.get("startProgram.program")
            if isinstance(start_cmd, HonParameterProgram):
                program_name = (
                    start_cmd.name_for_code(
                        program,
                        self._int_or_none(params, "prPosition"),
                        # Step 1 of the order above: decides before the active category.
                        cycle=self._cycle_activity(),
                    )
                    or program_name
                )
        data["programName"] = program_name
        # available: connectivity as a first-class attribute (app model). Offline
        # is handled by entity availability (base_entity), no longer by zeroing
        # the parameters. (See apk/analysis/per-type-derivations.md #5.)
        data["available"] = bool(self.parent.connection)
        return data

    def _cycle_activity(self) -> CycleActivity | None:
        """The parent's `cycle_activity` (see `HonAppliance`), None on any other parent."""
        cycle = getattr(self.parent, "cycle_activity", None)
        return cycle if isinstance(cycle, CycleActivity) else None

    def settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return settings
