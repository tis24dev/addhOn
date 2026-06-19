"""HonAttribute nativo.

Porting di `_vendor/pyhon/attributes.HonAttribute`. Un "attribute" è un valore di
stato che arriva dallo shadow del device (`shadow.parameters.<name> =
{parNewVal, lastUpdate}`) o da un push MQTT. Comportamento ancorato a pyhОn dal
differential test (tests/test_engine_attributes.py) sui dati reali del frigo
(apk/dump/ref_10136/attributes.json).

UNICA divergenza voluta vs pyhОn = FIX deprecazione: il lock usa
`datetime.now(timezone.utc)` (aware) invece di `datetime.utcnow()` (naive,
deprecato da Python 3.12). Il timestamp del lock è scritto e confrontato SOLO qui
dentro, quindi naive->aware è coerente e non mischia mai aware/naive; il
comportamento osservabile di `lock` (True entro 10s da uno shield, poi False) è
identico. `last_update` resta parsato com'è dalla stringa ISO (può essere naive o
aware) e non viene mai confrontato col lock, quindi nessun rischio di mixing.

Riusa il `str_to_float` nostro (client/helpers), identico a quello di pyhОn.
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
        """Valore dell'attributo (numerico se convertibile, altrimenti stringa)."""
        try:
            return str_to_float(self._value)
        except ValueError:
            return self._value

    @value.setter
    def value(self, value: str) -> None:
        self._value = value

    @property
    def last_update(self) -> Optional[datetime]:
        """Timestamp dell'ultimo aggiornamento dall'api (None se assente/invalido)."""
        return self._last_update

    @property
    def lock(self) -> bool:
        """True finché il valore è "schermato" (entro _LOCK_TIMEOUT secondi da uno
        shield): in quella finestra gli update non-shield vengono ignorati, così un
        comando appena inviato non viene sovrascritto da uno stato vecchio in arrivo."""
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
            except (ValueError, TypeError):  # lastUpdate non-stringa dal cloud
                self._last_update = None
        return True

    def __str__(self) -> str:
        return self._value
