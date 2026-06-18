"""REF (frigorifero). Riscrittura di `_vendor/pyhon/appliances/ref.py`.

modeZ1/modeZ2 dai flag holiday/intelligence/quickMode. FIX vs pyhОn: il confronto è
per VALORE (pyhОn faceva `HonAttribute == "1"` = sempre False -> modeZ1/Z2 erano sempre
`no_mode`). Campi NON consumati dall'integrazione (i modi veri li calcola la mappatura
Tier-0 dallo shadow) -> il fix è inerte ma corretto.

DIVERGENZA app documentata (vedi apk/analysis/per-type-derivations.md #3): l'app inverte
la priorità Z1 (super_cool PRIMA di holiday) e ha un alias `energySavingStatus`~auto_set.
Teniamo l'ordine di pyhОn (i modi sono mutuamente esclusivi via startProgram/stopProgram,
l'inversione è cosmetica) finché non validiamo live sull'AC/frigo.
"""
from __future__ import annotations

from typing import Any

from .base import ApplianceExtra


class Appliance(ApplianceExtra):
    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        data = super().attributes(data)
        params = data.get("parameters", {})
        if self._is_value(params, "holidayMode", 1):
            data["modeZ1"] = "holiday"
        elif self._is_value(params, "intelligenceMode", 1):
            data["modeZ1"] = "auto_set"
        elif self._is_value(params, "quickModeZ1", 1):
            data["modeZ1"] = "super_cool"
        else:
            data["modeZ1"] = "no_mode"

        if self._is_value(params, "quickModeZ2", 1):
            data["modeZ2"] = "super_freeze"
        elif self._is_value(params, "intelligenceMode", 1):
            data["modeZ2"] = "auto_set"
        else:
            data["modeZ2"] = "no_mode"
        return data
