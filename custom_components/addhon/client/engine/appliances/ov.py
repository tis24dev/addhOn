"""OV (forno). Riscrittura di `_vendor/pyhon/appliances/ov.py`.

Zeroing offline di temp/onOffStatus/remoteCtrValid/remainingTimeMM; `active` =
onOffStatus==1. A PARITA' con pyhОn (usava già `.value == 1`, corretto). Robustezza:
`.get`/no-op su chiavi assenti invece del KeyError di pyhОn.
"""
from __future__ import annotations

from typing import Any

from .base import ApplianceExtra


class Appliance(ApplianceExtra):
    def attributes(self, data: dict[str, Any]) -> dict[str, Any]:
        data = super().attributes(data)
        params = data.get("parameters", {})
        # niente zeroing offline: disponibilità via `available` (vedi td.py/base_entity).
        data["active"] = self._is_value(params, "onOffStatus", 1)
        return data
