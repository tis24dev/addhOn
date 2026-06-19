"""Numeric utilities of the native hOn client.

`str_to_float` converts hOn values (usually strings) to numbers and is used by the
parser engine (the range/enum parameters and the attributes layer). Its behavior is
pinned by the golden test, so values parse exactly as the cloud expects.
"""
from __future__ import annotations


def str_to_float(value: str | float) -> float:
    """Convert an hOn value (usually a string) into a number.

    Behavior (pinned by the golden test):
    - tries `int(value)` first: "5"->5, "-16"->-16, 5->5;
    - on ValueError falls back to `float`, normalizing the decimal
      comma: "5.5"->5.5, "5,5"->5.5.

    Known QUIRK, DELIBERATELY preserved: `int()` is attempted on floats too,
    and `int(5.5)` TRUNCATES to 5 without error (it only catches ValueError, not the others).
    So a STRING must be passed to preserve the decimals ("5.5"), never a float
    (5.5 -> 5). That is the reason number.py sends the setpoints as a string.
    Also non-numeric inputs (e.g. "abc", None) propagate the original exception
    (ValueError / TypeError): they are not masked.
    """
    try:
        return int(value)
    except ValueError:
        return float(str(value).replace(",", "."))
