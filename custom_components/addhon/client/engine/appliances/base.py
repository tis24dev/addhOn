"""Native base ApplianceExtra.

Per-type hooks on the appliance state:
- `attributes(data)`: post-processes the shadow (adds derived fields).
- `settings(result)`: tweaks the settings dict (default: no-op).

`parent` is the appliance (duck-typed): it needs `.settings`, `.connection`.
The VALUES in `data["parameters"][...]` are `HonAttribute`s (native): we read them
duck-typed via `.value`/`str()`. The `isinstance` checks instead are against the
native PARAMETER classes.

Comparison helper: compare by VALUE (flags "1"/"0" as int 1/0), so the flags
evaluate correctly. The fields it derives (ref `modeZ1`/`modeZ2`, the per-type
`pause` attribute) are computed but currently not surfaced as entities (the Pause
switch reads `machMode` directly).
"""
from __future__ import annotations

from typing import Any

from ..parameter.program import HonParameterProgram


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
    def _is_value(cls, params: dict[str, Any], key: str, expected: Any) -> bool:
        """True if the `key` attribute's `.value` == expected. Comparison by VALUE
        (flags "1"/"0" become int 1/0), so the flags evaluate correctly."""
        return cls._value(params, key) == expected

    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        # programName: slug from the current program code (the app uses an
        # i18n key resolved via dictionaryId = wrong altitude for HA).
        # Robustness: `_raw(...) or "0"` handles an empty/absent prCode -> "No
        # Program" instead of `int("")` -> ValueError.
        program_name = "No Program"
        params = data.get("parameters", {})
        if program := int(self._raw(params, "prCode") or "0"):
            start_cmd = self.parent.settings.get("startProgram.program")
            if isinstance(start_cmd, HonParameterProgram) and (ids := start_cmd.ids):
                program_name = ids.get(program, program_name)
        data["programName"] = program_name
        # available: connectivity as a first-class attribute (app model). Offline
        # is handled by entity availability (base_entity), no longer by zeroing
        # the parameters. (See apk/analysis/per-type-derivations.md #5.)
        data["available"] = bool(self.parent.connection)
        return data

    def settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return settings
