"""WM (lavatrice). Riscrittura di `_vendor/pyhon/appliances/wm.py`.

`active`/`pause` (machMode==3) derivati. Niente zeroing offline: la disponibilità è
gestita via `available` (entità HA -> unavailable se disconnesso), vedi base_entity.
"""
from __future__ import annotations

from typing import Any

from .base import ApplianceExtra


class Appliance(ApplianceExtra):
    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        data = super().attributes(data)
        params = data.get("parameters", {})
        data["active"] = bool(data.get("activity"))
        data["pause"] = self._is_value(params, "machMode", 3)
        return data
