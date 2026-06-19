"""WH (water heater) per-type appliance logic.

`active` = onOffStatus==1, compared by value. The derived field is not currently
consumed by an entity.
"""
from __future__ import annotations

from typing import Any

from .base import ApplianceExtra


class Appliance(ApplianceExtra):
    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        data = super().attributes(data)
        data["active"] = self._is_value(data.get("parameters", {}), "onOffStatus", 1)
        return data
