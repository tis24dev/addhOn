# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Numeric utilities of the native hOn client.

`str_to_float` converts hOn values (usually strings) to numbers and is used by the
parser engine (the range/enum parameters and the attributes layer). Its behavior is
pinned by the golden test, so values parse exactly as the cloud expects.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def parse_cloud_timestamp(value: Any) -> Optional[datetime]:
    """Parse a cloud-stamped timestamp into a timezone-aware UTC `datetime`.

    Accepts the two shapes the hOn cloud uses for the SAME wall-clock instant, so a
    `lastConnEvent` time and an `appliancestatus` payload time are directly comparable:
      - ISO8601 string, e.g. `lastConnEvent.instantTime` "2026-06-25T13:35:13Z" or the
        realtime payload `timestamp` "2026-06-25T16:04:21.1Z" (note: the cloud emits a
        VARIABLE number of fractional digits, including a single one);
      - epoch milliseconds (int or numeric string), e.g. `lastConnEvent.timestampEvent`
        1782394513133.

    Returns `None` on anything it cannot establish an ordering from (None, empty,
    garbage, wrong type) so the caller can degrade gracefully instead of raising -- this
    runs in connectivity reconciliation and on the awscrt callback thread, neither of
    which may raise. The result is ALWAYS tz-aware UTC, so naive/aware mismatches never
    crash a comparison: a bare ISO string with no offset is assumed UTC (the cloud stamps
    UTC, hence the trailing "Z").
    """
    if value is None or isinstance(value, bool):
        # bool is an int subclass; a True/False epoch is meaningless -> reject it
        # rather than treating it as 0/1 ms past the epoch.
        return None
    # Epoch milliseconds: int/float, or a purely-numeric string.
    if isinstance(value, (int, float)):
        # Guard the float() too: an arbitrary-precision int (e.g. a 300+ digit value from
        # json.loads) overflows float() with OverflowError BEFORE the fromtimestamp guard
        # below. This function must be TOTAL (never raises) -- it runs on the awscrt
        # callback thread and in the per-appliance setup loop, where OverflowError
        # (an ArithmeticError, not a ValueError) would escape the boundary catch.
        try:
            epoch_ms: Optional[float] = float(value)
        except (ValueError, OverflowError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        epoch_ms = None
        if text.lstrip("+-").isdigit():
            try:
                epoch_ms = float(text)
            except (ValueError, OverflowError):
                epoch_ms = None
        if epoch_ms is None:
            # ISO8601. fromisoformat (Py>=3.7) handles the variable fractional digits;
            # normalize a trailing "Z" to "+00:00" for the 3.10 interpreter (3.11+ takes
            # "Z" natively, but the substitution is harmless there).
            iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
            try:
                parsed = datetime.fromisoformat(iso)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    else:
        return None
    try:
        return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


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
