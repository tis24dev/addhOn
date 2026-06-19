"""OV (oven) per-type appliance logic.

`active` = onOffStatus==1 (compared by `.value`). Robustness: `.get`/no-op on absent
keys instead of a KeyError.
"""
from __future__ import annotations

from typing import Any

from .base import ApplianceExtra


class Appliance(ApplianceExtra):
    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        data = super().attributes(data)
        params = data.get("parameters", {})
        # no offline zeroing: availability via `available` (see td.py/base_entity).
        data["active"] = self._is_value(params, "onOffStatus", 1)
        return data
