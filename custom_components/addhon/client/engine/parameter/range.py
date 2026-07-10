# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Range parameter (min/max/step) for hOn commands.

min/max/step/default via `str_to_float` (reuses client.helpers). `step` falls back to 1
if 0. The setter validates range+step via `_on_grid` (snap-to-nearest-index with a
magnitude-scaled epsilon, decimal-agnostic) and raises ValueError if out of bounds or
off-grid (the entities rely on the ValueError for the rollback). `values` enumerates
min..max in steps of step (index-based, bounded).
"""
from __future__ import annotations

from decimal import Decimal
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

        Instead snap to the nearest grid index and accept when the value is within
        ``_grid_eps`` of that grid point. The tolerance is program_options' 1e-9
        float-drift epsilon scaled by the operand magnitude AND capped to step/4, so it
        dominates the ~1e-15 * magnitude rounding error of ``min + n * step`` yet stays
        strictly below the half-step off-grid distance even on absurd schemas. It is
        decimal-agnostic (any number of decimals). An exact fractions.Fraction oracle over
        the realistic AND the extreme parameter space gives 0 false negatives and 0 false
        positives, and the integer-step ranges of the golden fridge dump validate exactly
        as before.
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
        return abs(self.min + index * step - value) <= self._grid_eps(step)

    def _grid_eps(self, step: float) -> float:
        """Snap/enumeration tolerance for the min/step grid, capped to a fraction of step.

        The base epsilon is program_options' 1e-9 float-drift value scaled by the operand
        magnitude, so it always dominates the ~1e-15 * magnitude rounding error of
        ``min + n * step``. On its own that magnitude term can EXCEED half a step on a
        pathological schema (min 0, max 1_000_000, step 0.001 -> 1e-3 > step/2 = 5e-4),
        which would accept an off-grid value and violate the setter's off-grid -> ValueError
        contract (the rollback depends on it). Capping at ``step / 4`` keeps the epsilon
        strictly below the half-step distance in EVERY schema while staying orders of
        magnitude above the real rounding error, so it adds no false negative on any
        realistic range. The SAME epsilon backs _on_grid, option_count() and values(), so
        acceptance and enumeration can never disagree. Callers pass the resolved POSITIVE
        step (this is only reached after the step <= 0 guard). Proven with a
        fractions.Fraction oracle: 0 false negatives / 0 false positives on the realistic
        space and on the 1e6 / 0.001 extreme.
        """
        return min(1e-9 * max(1.0, abs(self.min), abs(self.max), abs(step)), step / 4)

    @staticmethod
    def _decimals(number: float) -> int:
        """Fractional-digit count of a parsed numeric value (0.1 -> 1, 0.125 -> 3, 1 -> 0).

        Uses ``Decimal(str(number))``: str() is the float's shortest round-tripping repr
        (Python guarantees str(0.1) == "0.1", and a decimal-comma "5,5" is already the
        float 5.5 via str_to_float), and normalize() drops an integer's trailing zeros
        (1.0 / 100 -> 0). Only ever used to format values() output, never the stored value.
        """
        exponent = Decimal(str(number)).normalize().as_tuple().exponent
        return -exponent if isinstance(exponent, int) and exponent < 0 else 0

    def _grid_ndigits(self) -> int:
        """Rounding precision for values() output = max(decimals(step), decimals(min)).

        A grid point ``min + i*step`` carries at most max(decimals(min), decimals(step))
        decimals, so rounding to this precision is exact: it removes float drift without
        ever collapsing a distinct point (e.g. min=16.5 step=1 keeps 16.5, 17.5, ...).
        Cosmetic only -- it formats values() and never touches the setter/intern_value.
        """
        return max(self._decimals(self.step), self._decimals(self.min))

    def _grid_count(self, step: float) -> int:
        """Reachable grid-point count for step > 0, bounded by _MAX_RANGE_VALUES.

        Computed ARITHMETICALLY so option_count() and values() never materialize the value
        strings just to measure them. int() floors ``(max + eps - min) / step``; the two
        short reconcile loops pin the result to the exact per-index predicate values() uses
        (``min + i*step <= max + eps``), so a one-ULP disagreement between the float
        division and the iteration cannot make option_count() drift from len(values()) and
        no phantom point past max is emitted. Returns 0 for an inverted range (max < min).
        Callers pass the resolved POSITIVE step.
        """
        eps = self._grid_eps(step)
        limit = self.max + eps
        count = int((limit - self.min) / step) + 1
        if count < 0:
            count = 0
        while count > 0 and self.min + (count - 1) * step > limit:
            count -= 1
        while self.min + count * step <= limit:
            count += 1
        return min(count, _MAX_RANGE_VALUES)

    def option_count(self) -> int:
        """Reachable ``values`` count WITHOUT materializing the strings (F2).

        Arithmetic equivalent of ``len(self.values)`` (same capped epsilon and
        _MAX_RANGE_VALUES cap), so the two are equal by construction -- values() builds
        ``range(self._grid_count(step))``. Lets HonCommand._more_options compare cardinality
        while merging available_settings without allocating up to _MAX_RANGE_VALUES strings.
        """
        step = self.step
        if step <= 0:
            return 1  # values() returns the single [str(self.min)]
        return self._grid_count(step)

    @property
    def values(self) -> list[str]:
        # Index-based enumeration (min + i*step), NOT a ``+= step`` accumulator: the old
        # accumulator compounded float error on decimal steps ("20.700000000000003") and
        # could DROP the final grid point (24.9.. + 0.1 overshoots max) or, on a malformed
        # range, loop forever. The point count comes from _grid_count (shared with
        # option_count -- same capped epsilon + _MAX_RANGE_VALUES bound), so
        # len(values) == option_count() by construction. Each value is ROUNDED to the
        # step/min decimal precision before str() (F3), which cleans decimal drift
        # ("24.200000000000003" -> "24.2") WITHOUT reformatting integers: an integer step
        # rounds to 0 decimals and str(round(int, 0)) stays the bare integer, so
        # integer-step ranges (all real fridge params) render byte-for-byte as before
        # (golden-verified). Rounding is cosmetic; the stored value / intern_value are set
        # by the setter and are never touched here.
        step = self.step
        if step <= 0:
            return [str(self.min)]
        ndigits = self._grid_ndigits()
        return [
            str(round(self.min + index * step, ndigits))
            for index in range(self._grid_count(step))
        ]
