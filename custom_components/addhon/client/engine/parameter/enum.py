"""Enum parameter for hOn commands.

`self.values` is normalized by `clean_value` (lowercase, strip `[]`, `|`->`_`).
The setter normalizes the incoming value with the SAME `clean_value` before
comparing, so a value carrying the cloud's casing (e.g. "BABYCARE") is accepted
against the normalized list (["babycare"]) instead of raising ValueError (this is
the BABYCARE bug it prevents). It accepts both "BABYCARE" and "babycare"; it stores
the raw value (so `intern_value` stays raw = what gets sent to the cloud).

Edge-value handling (cased / `|` / `[]`) follows the app's behavior:
  1. TRIGGER: `check_trigger` runs on EVERY accepted value, so cloud-cased values
     also cascade the rules consistently.
  2. ACCEPTANCE: a value is accepted if its normalized form is among the
     allowed values (a single, consistent rule). The integration always sets clean
     forms from `param.values`.
  3. `|`-STRING: with `enumValues` as a STRING "A|B|C" the value is not accepted as a
     whole; the correct `|` split is handled upstream (the app splits it).
"""
from __future__ import annotations

from typing import Any

from .base import HonParameter


def clean_value(value: str | float) -> str:
    return str(value).strip("[]").replace("|", "_").lower()


class HonParameterEnum(HonParameter):
    def __init__(self, key: str, attributes: dict[str, Any], group: str) -> None:
        super().__init__(key, attributes, group)
        self._default: str | float = ""
        self._value: str | float = ""
        self._values: list[str] = []
        self._set_attributes()
        if self._default and clean_value(self._default) not in self.values:
            self._values.append(str(self._default))

    def _set_attributes(self) -> None:
        super()._set_attributes()
        self._default = self._attributes.get("defaultValue", "")
        self._value = self._default or "0"
        # `enumValues` is normally a list; some payloads give it as the string
        # "A|B|C". Normalize to a list so .append/.values do not break (before, a
        # string here caused an AttributeError in __init__ or a character-by-character
        # iteration). The "|" split is consistent with _apply_enum (rules.py).
        raw_values = self._attributes.get("enumValues", [])
        if isinstance(raw_values, str):
            self._values = raw_values.split("|")
        elif isinstance(raw_values, list):
            self._values = [str(v) for v in raw_values]
        else:
            self._values = []

    def __repr__(self) -> str:
        return f"{self.__class__} (<{self.key}> {self.values})"

    @property
    def values(self) -> list[str]:
        return [clean_value(value) for value in self._values]

    @values.setter
    def values(self, values: list[str]) -> None:
        self._values = values

    @property
    def intern_value(self) -> str:
        return str(self._value) if self._value is not None else str(self.values[0])

    @property
    def value(self) -> str | float:
        return clean_value(self._value) if self._value is not None else self.values[0]

    @value.setter
    def value(self, value: str | float) -> None:
        # Compare on the NORMALIZED value (matching the already-clean list in
        # self.values), so a raw cloud-cased incoming value is accepted correctly.
        if clean_value(value) in self.values:
            self._value = value
            self.check_trigger(value)
        else:
            raise ValueError(f"Allowed values: {self._values} But was: {value}")
