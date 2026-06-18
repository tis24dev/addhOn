"""ApplianceExtra base nativo. Riscrittura di `_vendor/pyhon/appliances/base.py`.

Hook per-tipo sullo stato dell'appliance:
- `attributes(data)`: post-processa lo shadow (aggiunge campi derivati).
- `settings(result)`: ritocca il dict settings (default: no-op).

`parent` è l'appliance (duck-typed): servono `.settings`, `.connection`.
I VALORI in `data["parameters"][...]` sono `HonAttribute` (ancora di pyhОn finché lo
slice 5 non flippa gli attributi): li leggiamo duck-typed via `.value`/`str()`.
Gli `isinstance` invece sono contro le classi PARAMETRO native (i parametri sono già
nativi dopo il flip del cluster) — è il motivo per cui per-tipo (slice 4) e cluster
(slice 3) flippano insieme.

Helper di confronto: pyhОn confrontava `HonAttribute == "1"` che è SEMPRE False
(nessun `__eq__`) -> ref/td/wm pause erano no-op rotti. Qui confrontiamo per VALORE
(intento dell'app), correggendo il bug. I campi che ne dipendono (modeZ1/Z2/pause) non
sono però consumati dall'integrazione: la differenza è documentata, non rischiosa.
"""
from __future__ import annotations

from typing import Any

from ..parameter.program import HonParameterProgram


class ApplianceExtra:
    def __init__(self, appliance: Any) -> None:
        self.parent = appliance

    # --- helper di lettura attributi (duck-typed su HonAttribute) ---
    @staticmethod
    def _raw(params: dict[str, Any], key: str) -> str:
        """Valore grezzo (stringa) via __str__. SOLO per campi mai impostati a numero
        (es. prCode): dopo un set numerico __str__ solleverebbe. Per i flag usare _value."""
        if key not in params:
            return ""
        return str(params[key])

    @staticmethod
    def _value(params: dict[str, Any], key: str, default: Any = None) -> Any:
        """Valore tipizzato dell'attributo (`.value`, numerico se convertibile),
        default se assente."""
        attr = params.get(key)
        return attr.value if attr is not None and hasattr(attr, "value") else default

    @classmethod
    def _is_value(cls, params: dict[str, Any], key: str, expected: Any) -> bool:
        """True se `.value` dell'attributo `key` == expected. Confronto per VALORE
        (i flag "1"/"0" diventano int 1/0): sostituisce il `HonAttribute == "..."` di
        pyhОn, che è SEMPRE False (manca __eq__) -> i suoi modeZ/pause erano no-op."""
        return cls._value(params, key) == expected

    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        # programName: slug dal codice programma corrente (come pyhОn; l'app usa una
        # chiave i18n risolta via dictionaryId = altitudine sbagliata per HA).
        # Robustezza vs pyhОn: `_raw(...) or "0"` gestisce prCode vuoto/assente -> "No
        # Program" invece del `int("")` -> ValueError di pyhОn (divergenza voluta, safe).
        program_name = "No Program"
        params = data.get("parameters", {})
        if program := int(self._raw(params, "prCode") or "0"):
            start_cmd = self.parent.settings.get("startProgram.program")
            if isinstance(start_cmd, HonParameterProgram) and (ids := start_cmd.ids):
                program_name = ids.get(program, program_name)
        data["programName"] = program_name
        # available: connettività come attributo first-class (modello app). Additivo;
        # lo zeroing offline resta nelle per-tipo come layer di compat finché le entità
        # non passano a `available`. (Vedi apk/analysis/per-type-derivations.md #5.)
        data["available"] = bool(self.parent.connection)
        return data

    def settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return settings
