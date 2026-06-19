"""Native HonAttribute.

hOn attribute: a state value (`HonAttribute`) read from the device shadow
(`shadow.parameters.<name> = {parNewVal, lastUpdate}`) or from an MQTT push.
Behavior is pinned by the golden test (tests/test_engine_attributes.py) against
the real fridge data (tests/fixtures/ref_10136/attributes.json).

The lock uses `datetime.now(timezone.utc)` (timezone-aware) rather than
`datetime.utcnow()` (naive, deprecated since Python 3.12). The lock timestamp is
written and compared ONLY in here, so it is consistent and never mixes aware/naive;
the observable behavior of `lock` (True within 10s of a shield, then False) is
unaffected. `last_update` is still parsed as-is from the ISO string (can be naive or
aware) and is never compared with the lock, so there is no mixing risk.

Reuses our own `str_to_float` (client/helpers).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Final, Optional

from ..helpers import str_to_float


class HonAttribute:
    _LOCK_TIMEOUT: Final = 10

    def __init__(self, data: dict[str, str] | str) -> None:
        self._value: str = ""
        self._last_update: Optional[datetime] = None
        self._lock_timestamp: Optional[datetime] = None
        self.update(data)

    @property
    def value(self) -> float | str:
        """Attribute value (numeric if convertible, otherwise a string)."""
        try:
            return str_to_float(self._value)
        except ValueError:
            return self._value

    @value.setter
    def value(self, value: str) -> None:
        self._value = value

    @property
    def last_update(self) -> Optional[datetime]:
        """Timestamp of the last update from the api (None if absent/invalid)."""
        return self._last_update

    @property
    def lock(self) -> bool:
        """True while the value is "shielded" (within _LOCK_TIMEOUT seconds of a
        shield): in that window non-shield updates are ignored, so a just-sent
        command is not overwritten by an old incoming state."""
        if not self._lock_timestamp:
            return False
        lock_until = self._lock_timestamp + timedelta(seconds=self._LOCK_TIMEOUT)
        return lock_until >= datetime.now(timezone.utc)

    def update(self, data: dict[str, str] | str, shield: bool = False) -> bool:
        if self.lock and not shield:
            return False
        if shield:
            self._lock_timestamp = datetime.now(timezone.utc)
        if isinstance(data, str):
            self.value = data
            return True
        self.value = data.get("parNewVal", "")
        if last_update := data.get("lastUpdate"):
            try:
                self._last_update = datetime.fromisoformat(last_update)
            except (ValueError, TypeError):  # non-string lastUpdate from the cloud
                self._last_update = None
        return True

    def __str__(self) -> str:
        return self._value
