"""WD (lavasciuga). Riscrittura di `_vendor/pyhon/appliances/wd.py`.

Come TD ma senza il ritocco dryLevel. `pause` FIX per valore (vedi td.py/base.py).
"""
from __future__ import annotations

from typing import Any

from .base import ApplianceExtra


class Appliance(ApplianceExtra):
    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        data = super().attributes(data)
        params = data.get("parameters", {})
        # niente zeroing offline: disponibilità via `available` (vedi td.py/base_entity).
        data["active"] = bool(data.get("activity"))
        data["pause"] = self._is_value(params, "machMode", 3)
        return data
