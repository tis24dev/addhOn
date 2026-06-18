"""DW (lavastoviglie). Riscrittura di `_vendor/pyhon/appliances/dw.py`.

`active = bool(activity)`, zeroing machMode offline. Nessun `pause` (come pyhОn).
"""
from __future__ import annotations

from typing import Any

from .base import ApplianceExtra


class Appliance(ApplianceExtra):
    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        data = super().attributes(data)
        params = data.get("parameters", {})
        if not self.parent.connection:
            self._set(params, "machMode", "0")
        data["active"] = bool(data.get("activity"))
        return data
