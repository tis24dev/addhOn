"""Range parameter (min/max/step) for hOn commands.

min/max/step/default via `str_to_float` (reuses client.helpers). `step` falls back to 1
if 0. The setter validates range+step (modulo *100 to avoid float imprecision) and
raises ValueError if out of bounds (the entities rely on it for the rollback). `values`
enumerates min..max in steps of step.
"""
from __future__ import annotations

from typing import Any

from ...helpers import str_to_float
from .base import HonParameter


class HonParameterRange(HonParameter):
    def __init__(self, key: str, attributes: dict[str, Any], group: str) -> None:
        super().__init__(key, attributes, group)
        self._min: float = 0
        self._max: float = 0
        self._step: float = 0
        self._default: float = 0
        self._value: float = 0
        self._set_attributes()

    def _set_attributes(self) -> None:
        super()._set_attributes()
        self._min = str_to_float(self._attributes.get("minimumValue", 0))
        self._max = str_to_float(self._attributes.get("maximumValue", 0))
        self._step = str_to_float(self._attributes.get("incrementValue", 0))
        self._default = str_to_float(self._attributes.get("defaultValue", self.min))
        self._value = self._default

    def __repr__(self) -> str:
        return f"{self.__class__} (<{self.key}> [{self.min} - {self.max}])"

    @property
    def min(self) -> float:
        return self._min

    @min.setter
    def min(self, mini: float) -> None:
        self._min = mini

    @property
    def max(self) -> float:
        return self._max

    @max.setter
    def max(self, maxi: float) -> None:
        self._max = maxi

    @property
    def step(self) -> float:
        if not self._step:
            return 1
        return self._step

    @step.setter
    def step(self, step: float) -> None:
        self._step = step

    @property
    def value(self) -> str | float:
        return self._value if self._value is not None else self.min

    @value.setter
    def value(self, value: str | float) -> None:
        # A fractional float passed directly (instead of the documented string) would be
        # truncated by str_to_float's int()-first quirk (22.5 -> 22) and then silently
        # accepted, so route only non-integer floats through str() to keep the decimals.
        # Integer-valued inputs (str "4", int 4, float 4.0) stay int -> clean
        # intern_value "4" (never "4.0"). str_to_float is golden-pinned, so the fix lives
        # here in the write path, not in the helper.
        if isinstance(value, float) and not value.is_integer():
            value = str_to_float(str(value))
        else:
            value = str_to_float(value)
        if self.min <= value <= self.max and not ((value - self.min) * 100) % (
            self.step * 100
        ):
            self._value = value
            self.check_trigger(value)
        else:
            allowed = f"min {self.min} max {self.max} step {self.step}"
            raise ValueError(f"Allowed: {allowed} But was: {value}")

    @property
    def values(self) -> list[str]:
        result = []
        i = self.min
        while i <= self.max:
            result.append(str(i))
            i += self.step
        return result
