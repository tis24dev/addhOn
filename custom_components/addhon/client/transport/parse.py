# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Parser of the hOn cloud responses (addhOn transport).

Appliance-list extraction from the endpoint
`POST /unified-api/v1/view/appliance-list`, which also returns offline devices.

Response shape: `result.modules.applianceList.payload.appliances` (a list).

The walk is defensive: any unexpected shape (a non-dict intermediate level such
as `{"modules": "x"}` or `{"modules": {"applianceList": []}}`) falls back to `[]`
(fail-safe), so the caller treats schema-drift as "0 appliances" instead of a
crash.
"""
from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Path in the POST /unified-api/v1/view/appliance-list response. PUBLIC (no leading
# underscore) because two readers outside this function depend on it: `probe_appliance_list`
# below walks the very same tuple, and `tests/test_diagnostics.py` pins
# `diagnostics._FETCH_STOPS` against it so the token set the dump may print cannot drift
# away from the path the parser actually walks.
APPLIANCE_LIST_PATH = ("modules", "applianceList", "payload", "appliances")


def parse_appliance_list(result: Any) -> list:
    """Extract the appliance list (including offline ones) from the unified-api response.

    Returns the list at `modules.applianceList.payload.appliances`. Any unexpected
    shape (missing key, non-dict intermediate level, non-list final value)
    -> `[]`. A non-list but *truthy* final value = schema drift: log + `[]`.
    """
    node: Any = result
    for key in APPLIANCE_LIST_PATH:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    if isinstance(node, list):
        return node
    if node:
        _LOGGER.warning(
            "appliance-list response: 'appliances' of unexpected type %s, ignored",
            type(node).__name__,
        )
    return []


# Every type `json.loads` can produce. `type(x).__name__` on a foreign object is a
# CLASS NAME, i.e. an arbitrary string chosen by whoever wrote that class, so the name
# is MAPPED onto this set rather than copied: the value `probe_appliance_list` emits is
# always a member of a literal written here, which is what lets the diagnostics dump
# print it without a redaction pass (see the `_closed_token` argument in
# diagnostics.py). A class named after a MAC address is exactly the accident this
# closes.
_NODE_TYPES = frozenset({"dict", "list", "str", "int", "float", "bool", "NoneType"})


def _node_type(node: Any) -> str:
    name = type(node).__name__
    return name if name in _NODE_TYPES else "other"


def probe_appliance_list(result: Any) -> dict:
    """Where the appliance-list walk stopped, as closed-domain primitives.

    Deliberately INDEPENDENT of `parse_appliance_list` rather than a refactor of it: a
    probe that cannot influence what the parser returns is a structural property, not a
    review promise. `parse_appliance_list` sits on the setup path of every config entry,
    and the whole point of this function is to be shippable without touching it. The two
    are pinned to agree by test (`tests/test_transport_parse.py`,
    `test_probe_and_parse_agree_on_the_list`).

    NO value and NO key name from the response is read into the result. Every string it
    emits is an element of `APPLIANCE_LIST_PATH` or of `_NODE_TYPES`, both literals of
    this file. That is what makes the output safe to publish in a diagnostics dump the
    user pastes into a public issue, where a key name such as `"3C:71:BF:AA:BB:CC"`
    would otherwise ride out on `stopped_at`.

    `count` is None -- not 0 -- on every branch that never reached a list, so a `count`
    of 0 in the dump means exactly one thing: the cloud sent an empty list. With 0 on
    the failure branches the reader would be back to the ambiguity this function exists
    to remove.

    The try/except is not decoration. The input is `await resp.json(content_type=None)`
    on a duck-typed response: in production aiohttp decodes with `json.loads` and none
    of isinstance/`in`/len/`type().__name__` can raise, but a dict SUBCLASS overriding
    `__contains__`/`__len__`/`__getitem__` satisfies `isinstance` and can. This runs
    inside `NativeHon.setup()` (session.py:450), so "it cannot raise on a json.loads
    output" is not the same claim as "it can never abort a setup" -- the guard makes the
    second one true too, and gives the reserved "other" token its only producer.
    """
    try:
        node: Any = result
        for key in APPLIANCE_LIST_PATH:
            if not isinstance(node, dict):
                return {"outcome": "not_a_dict", "stopped_at": key,
                        "node_type": _node_type(node), "siblings": None, "count": None}
            if key not in node:
                return {"outcome": "missing_key", "stopped_at": key,
                        "node_type": "dict", "siblings": len(node), "count": None}
            node = node[key]
        if isinstance(node, list):
            return {"outcome": "ok", "stopped_at": None,
                    "node_type": "list", "siblings": None, "count": len(node)}
        return {"outcome": "not_a_list", "stopped_at": None,
                "node_type": _node_type(node), "siblings": None, "count": None}
    except Exception:  # noqa: BLE001 - a probe must never abort a setup
        return {"outcome": "other", "stopped_at": None,
                "node_type": None, "siblings": None, "count": None}
