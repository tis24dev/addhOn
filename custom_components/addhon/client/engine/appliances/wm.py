"""WM (lavatrice). Riscrittura di `_vendor/pyhon/appliances/wm.py`.

Variante: lo zeroing di machMode è guidato da `lastConnEvent.category == "DISCONNECTED"`
(non da `self.parent.connection` come TD/WD). `pause` FIX per valore (vedi base.py).
"""
from __future__ import annotations

from typing import Any

from .base import ApplianceExtra


class Appliance(ApplianceExtra):
    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        data = super().attributes(data)
        params = data.get("parameters", {})
        if data.get("lastConnEvent", {}).get("category", "") == "DISCONNECTED":
            self._set(params, "machMode", "0")
        data["active"] = bool(data.get("activity"))
        data["pause"] = self._is_value(params, "machMode", 3)
        return data
