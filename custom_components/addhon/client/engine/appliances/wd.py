"""WD (washer-dryer) per-type appliance logic.

Like TD but without the dryLevel tweak. `pause` derived by value (see td.py/base.py).
"""
from __future__ import annotations

from typing import Any

from .base import ApplianceExtra


class Appliance(ApplianceExtra):
    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        data = super().attributes(data)
        params = data.get("parameters", {})
        # no offline zeroing: availability via `available` (see td.py/base_entity).
        data["active"] = bool(data.get("activity"))
        data["pause"] = self._is_value(params, "machMode", 3)
        return data
