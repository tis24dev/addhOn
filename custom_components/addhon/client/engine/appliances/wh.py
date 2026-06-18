"""WH (scaldabagno). Riscrittura di `_vendor/pyhon/appliances/wh.py`.

`active` = onOffStatus==1. FIX vs pyhОn: pyhОn faceva `isinstance(attr, HonParameter)`
(falso: è un HonAttribute) -> ramo `attr == 1` = sempre False -> active rotto. Qui per
valore (corretto). Campo non consumato -> fix inerte ma corretto.
"""
from __future__ import annotations

from typing import Any

from .base import ApplianceExtra


class Appliance(ApplianceExtra):
    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        data = super().attributes(data)
        data["active"] = self._is_value(data.get("parameters", {}), "onOffStatus", 1)
        return data
