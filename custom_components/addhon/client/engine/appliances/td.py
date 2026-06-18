"""TD (asciugatrice). Riscrittura di `_vendor/pyhon/appliances/td.py`.

`active`/zeroing-offline a parità con pyhОn (funzionavano). `pause` FIX: per valore
(pyhОn `machMode == "3"` = sempre False; campo non consumato, fix inerte ma corretto).
`settings`: nasconde `startProgram.dryLevel` quando è un fixed "non selezionato".
MIGLIORIA app (per-type-derivations.md #4): l'app nasconde per '11' E '0'/vuoto
(pyhОn solo '11'); il nostro fixed `value` non è mai "" (getter -> "0"), quindi {"0","11"}.
"""
from __future__ import annotations

from typing import Any

from ..parameter.fixed import HonParameterFixed
from .base import ApplianceExtra

_DRY_HIDDEN = {"", "0", "11"}


class Appliance(ApplianceExtra):
    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        data = super().attributes(data)
        params = data.get("parameters", {})
        if not self.parent.connection:
            self._set(params, "machMode", "0")
        data["active"] = bool(data.get("activity"))
        data["pause"] = self._is_value(params, "machMode", 3)
        return data

    def settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        dry_level = settings.get("startProgram.dryLevel")
        if isinstance(dry_level, HonParameterFixed) and str(dry_level.value) in _DRY_HIDDEN:
            settings.pop("startProgram.dryLevel", None)
        return settings
