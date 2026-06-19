"""WM (washing machine) per-type appliance logic.

`active`/`pause` (machMode==3) derived. No offline zeroing: availability is
handled via `available` (HA entity -> unavailable if disconnected), see base_entity.
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
