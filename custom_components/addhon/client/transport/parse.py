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

# Path in the POST /unified-api/v1/view/appliance-list response.
_APPLIANCE_LIST_PATH = ("modules", "applianceList", "payload", "appliances")


def parse_appliance_list(result: Any) -> list:
    """Extract the appliance list (including offline ones) from the unified-api response.

    Returns the list at `modules.applianceList.payload.appliances`. Any unexpected
    shape (missing key, non-dict intermediate level, non-list final value)
    -> `[]`. A non-list but *truthy* final value = schema drift: log + `[]`.
    """
    node: Any = result
    for key in _APPLIANCE_LIST_PATH:
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
