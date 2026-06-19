"""Client device descriptor for the addhOn transport.

Builds the "who am I" identity (app version, OS, model, mobileId) sent to the hOn
cloud on every request. The official app fills these from the running device
(model, OS level, a per-device unique id); addhOn runs headless, so it sends a fixed
identity that presents as addhOn while reporting the current app version, so the
cloud sees an up-to-date client.
"""
from __future__ import annotations

from dataclasses import dataclass

# Fixed client identity sent to the cloud (single point to update). APP_VERSION
# tracks the current hOn app version.
APP_VERSION = "2.27.9"
OS_VERSION = 34
OS = "android"
DEVICE_MODEL = "addhon"
MOBILE_ID = "addhon"


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
