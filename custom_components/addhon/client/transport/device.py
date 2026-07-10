# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Client device descriptor for the addhOn transport.

Builds the "who am I" identity (app version, OS, model, mobileId) sent to the hOn
cloud on every request. The official app fills these from the running device
(model, OS level, a per-device unique id); addhOn runs headless, so it sends a fixed
identity that presents as addhOn while reporting the current app version, so the
cloud sees an up-to-date client.
"""
from __future__ import annotations

from dataclasses import dataclass

# Identity values live in values.py with their provenance (spec: HHT-sec3). Imported
# here (not inlined) so there is one documented source; MOBILE_ID stays a module
# attribute for `from .device import MOBILE_ID`.
from .values import APP_VERSION, DEVICE_MODEL, MOBILE_ID, OS, OS_VERSION


@dataclass(frozen=True)
class HonDevice:
    """Immutable client descriptor. An empty `mobile_id` falls back to the default."""

    mobile_id: str = MOBILE_ID

    def __post_init__(self) -> None:
        if not self.mobile_id:
            object.__setattr__(self, "mobile_id", MOBILE_ID)

    def payload(self, mobile: bool = False) -> dict[str, str | int]:
        """The identity dictionary sent to the cloud.

        With `mobile=True` the `os` key becomes `mobileOs`, used for the cloud's
        "mobile" calls.
        """
        data: dict[str, str | int] = {
            "appVersion": APP_VERSION,
            "mobileId": self.mobile_id,
            "os": OS,
            "osVersion": OS_VERSION,
            "deviceModel": DEVICE_MODEL,
        }
        if mobile:
            data["mobileOs"] = data.pop("os")
        return data
