"""Shape-only reductions of the hOn wine-cellar inventory API (issue #149).

The hOn app keeps the bottle inventory of a wine cellar -- physical or the
Virtual Wine Cellar, which is a bottle list with no appliance behind it -- on the
`cellar/v1` microservice (hOn 2.30.7, `WineCellarMicroservice`):

    GET {API_URL}/cellar/v1/{key}/wines            the bottles of one cellar
    GET {API_URL}/cellar/v1/wines/bottles-count    a count with no cellar key

`key` is the appliance key the app uses everywhere, which for an entry with no
MAC is its serial number (the app substitutes `serialNumber` for an empty
`macAddress` when it reads the appliance list).

Nothing here is consumed by an entity yet. These functions exist so that a
diagnostics download can show what the response LOOKS like -- keys, types,
counts and the three numeric fields an inventory sensor would need -- without
carrying the user's wines (names, wineries, vintages) into a public issue. The
raw body never leaves this module: only the reductions below do, and
diagnostics validates them again on the way out.
"""

from __future__ import annotations

import math
from typing import Any

# A JSON key is published only when it is a field name the hOn app itself reads
# from this service (hOn 2.30.7): `data.payload` of every call, the selectors over
# `cellars[key]` (`wines`, `quantity`, `price`, `zone`, `favourite`, `bottlesCount`,
# `hasActivities`) and the wine list and detail screens (`id`, `grape`, `country`,
# `type`, `title`, `createdAt`, `bottles`). A cellar API is free to key a mapping by
# bottle id, by a wine's name or by anything else the user typed, and a key that
# reads like a field name ("Merlot") is no safer than one that does not: every other
# key is counted, with the type of its value, and never written.
KNOWN_KEYS = frozenset({
    "payload", "data", "wines", "cellars", "bottlesCount", "hasActivities", "total",
    "quantity", "price", "zone", "favourite", "id", "grape", "country", "type",
    "title", "createdAt", "bottles",
})
MAX_DEPTH = 5
MAX_KEYS = 40
# Magnitude bound for the numbers that are published (bottle counts, prices,
# zones). Anything larger is not a quantity this API should return.
MAX_NUMBER = 1_000_000_000

# Where a wine list may sit in the body. The app reads `data.payload` for every
# other call of this service, so the first candidate is the likely one; the
# others are what a reader would try next. The token, not the path, is published.
_WINE_LIST_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("payload.wines", ("payload", "wines")),
    ("wines", ("wines",)),
    ("payload", ("payload",)),
    ("root", ()),
)
# The per-bottle fields the app's selectors read to count and value a cellar
# (`quantity` summed, `price * quantity`, filtered by `zone`).
WINE_NUMBER_FIELDS = ("quantity", "price", "zone")


def scalar_type(value: Any) -> str:
    """A closed type token for a JSON scalar."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return "other"


def value_kind(value: Any) -> str:
    """A closed type token for any JSON value, containers included."""
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "list"
    return scalar_type(value)


def number(value: Any) -> int | float | None:
    """A finite int or float inside the magnitude bound, else None. bool refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if -MAX_NUMBER <= value <= MAX_NUMBER else None


def json_shape(value: Any, *, numbers: bool, depth: int = 0) -> Any:
    """The structure of a JSON value with every string value removed.

    Objects keep the keys that look like field names (at most MAX_KEYS) and count
    the rest; lists keep their length and the shape of their first item; scalars
    become a type token. With `numbers` an int or float also keeps its value --
    used only for the bottles-count response, which is a count and nothing else.
    """
    if isinstance(value, dict):
        if depth >= MAX_DEPTH:
            return {"type": "object", "truncated": True}
        keys: dict[str, Any] = {}
        other = 0
        other_types: dict[str, int] = {}
        for key, item in value.items():
            if (
                type(key) is str
                and key in KNOWN_KEYS
                and len(keys) < MAX_KEYS
            ):
                keys[key] = json_shape(item, numbers=numbers, depth=depth + 1)
            else:
                other += 1
                kind = value_kind(item)
                other_types[kind] = other_types.get(kind, 0) + 1
        out: dict[str, Any] = {"type": "object", "keys": keys}
        if other:
            out["other_keys"] = other
            out["other_types"] = other_types
        return out
    if isinstance(value, list):
        if depth >= MAX_DEPTH:
            return {"type": "list", "len": len(value), "truncated": True}
        out = {"type": "list", "len": len(value)}
        if value:
            out["item"] = json_shape(value[0], numbers=numbers, depth=depth + 1)
        return out
    kind = scalar_type(value)
    if numbers and kind in ("int", "float"):
        bounded = number(value)
        if bounded is not None:
            return {"type": kind, "value": bounded}
    return kind


def _at(body: Any, path: tuple[str, ...]) -> Any:
    node = body
    for step in path:
        if not isinstance(node, dict):
            return None
        node = node.get(step)
    return node


def wine_list_summary(body: Any) -> dict[str, Any]:
    """Where the wine list is, how long it is, and the numbers of its first bottle.

    The first bottle's three numeric fields keep their value when they are numbers
    and become a type token otherwise, so a reader can tell "the API sends the price
    as a string" from "the API sends no price". No other field of any bottle is read.
    """
    for token, path in _WINE_LIST_PATHS:
        wines = _at(body, path)
        if isinstance(wines, list):
            first = wines[0] if wines and isinstance(wines[0], dict) else None
            summary: dict[str, Any] = {"wines_path": token, "wines_count": len(wines)}
            if first is not None:
                summary["first_wine"] = {
                    field: (
                        number(first[field])
                        if number(first.get(field)) is not None
                        else scalar_type(first[field])
                    )
                    for field in WINE_NUMBER_FIELDS
                    if field in first
                }
            return summary
    return {"wines_path": None}
