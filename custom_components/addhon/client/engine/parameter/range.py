"""Range parameter (min/max/step) for hOn commands.

min/max/step/default via `str_to_float` (reuses client.helpers). `step` falls back to 1
if 0. The setter validates range+step via `_on_grid` (snap-to-nearest-index with a
magnitude-scaled epsilon, decimal-agnostic) and raises ValueError if out of bounds or
off-grid (the entities rely on the ValueError for the rollback). `values` enumerates
min..max in steps of step (index-based, bounded).
"""
from __future__ import annotations

from typing import Any

from ...helpers import str_to_float
from .base import HonParameter

# Safety bound when materializing a range's reachable ``values``: a malformed schema
# (huge max / tiny step) must never loop unbounded. Generous on purpose so no plausible
# real setpoint range is ever truncated -- truncating would silently drop valid grid
# points, the same class of data loss the setter fix below removes. program_options uses
# a tighter 1000 for UI select options, which is a different purpose.
_MAX_RANGE_VALUES = 100000


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
        if self._on_grid(value):
            self._value = value
            self.check_trigger(value)
        else:
            allowed = f"min {self.min} max {self.max} step {self.step}"
            raise ValueError(f"Allowed: {allowed} But was: {value}")

    def _on_grid(self, value: float) -> bool:
        """True if ``value`` is in [min, max] AND lands on the min/step grid.

        Replaces the inherited ``((value - min) * 100) % (step * 100)`` trick, which was
        NOT a tolerance but a naive integerization: ``(value - min) * 100`` stays a float
        and keeps its IEEE-754 representation error, so an on-grid setpoint with a
        non-zero min and a decimal step (e.g. 20.1 on 20..25 step 0.1, or 16.3 on 16..30
        step 0.1) was wrongly rejected. That reject surfaces as a ValueError on the write
        path (climate.py setpoint / number.py), which the entities read as a failed set
        and SILENTLY roll the user's value back.

        Instead snap to the nearest grid index and accept when the value is within a
        tolerance of that grid point. The tolerance is program_options' 1e-9 float-drift
        epsilon scaled by the operand magnitude, so it always dominates the
        ~1e-15 * magnitude rounding error of ``min + n * step`` yet stays orders of
        magnitude below any real off-grid distance (a genuine off-grid value is at least a
        fraction of a step away). It is decimal-agnostic (any number of decimals). An
        exact fractions.Fraction oracle over the realistic parameter space gives 0 false
        negatives and 0 false positives, and the integer-step ranges of the golden fridge
        dump validate exactly as before.
        """
        if not self.min <= value <= self.max:
            return False
        step = self.step
        if step <= 0:
            # Malformed incrementValue (non-positive): there is no grid to test and we must
            # not divide by it. Accept anything already in [min, max] rather than reject a
            # legitimate value on corrupt metadata (a reject would re-trigger the silent
            # rollback this fix removes). The step property maps a falsy 0 -> 1, so this
            # only fires for a genuinely negative step.
            return True
        index = round((value - self.min) / step)
        tolerance = 1e-9 * max(1.0, abs(self.min), abs(self.max), abs(step))
        return abs(self.min + index * step - value) <= tolerance

    @property
    def values(self) -> list[str]:
        # Index-based enumeration (min + i*step), NOT a ``+= step`` accumulator: the old
        # accumulator compounded float error on decimal steps ("20.700000000000003") and
        # could DROP the final grid point (24.9.. + 0.1 overshoots max) or, on a malformed
        # range, loop forever (commands.py _more_options calls this to compare lengths).
        # Bounded by _MAX_RANGE_VALUES and an epsilon on the max bound (1e-9 * magnitude,
        # NOT step/2, so an overshooting step cannot add a phantom point past max). Keeps
        # the previous ``str()`` formatting, so integer-step ranges (all real fridge
        # params) render byte-for-byte as before (golden-verified).
        step = self.step
        if step <= 0:
            return [str(self.min)]
        result: list[str] = []
        index = 0
        tolerance = 1e-9 * max(1.0, abs(self.min), abs(self.max), abs(step))
        while index < _MAX_RANGE_VALUES:
            current = self.min + index * step
            if current > self.max + tolerance:
                break
            result.append(str(current))
            index += 1
        return result
