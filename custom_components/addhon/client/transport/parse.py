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

    The guard makes that promise TRUE rather than merely stated. `isinstance` accepts
    a dict SUBCLASS, and a subclass is free to raise from `get` -- in production
    `json.loads` cannot build one, but the input is `await resp.json(content_type=None)`
    on a duck-typed response, so "a json.loads output cannot raise here" is not the
    same claim as "this can never abort a setup". It runs inside `NativeHon.setup()`
    (session.py), where an exception does not spoil a returned list, it takes the
    config entry down with a bare TypeError that names nothing a reporter can act on.

    NOTHING IS LOST BY SWALLOWING IT. `probe_appliance_list` walks the same response
    under its own guard and reports `outcome: "other"` for exactly this input, and that
    token reaches the diagnostics dump, so the difference between "the cloud sent an
    empty list" and "the body fought back" survives in the one place a reader looks.
    Returning `[]` also keeps the single documented contract instead of adding a second
    failure mode that only this call site would know how to handle.
    """
    try:
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
    except Exception:  # noqa: BLE001 - a parse must never abort a setup
        # Deliberately not logged here: the caller already emits ADDHON-210 with the
        # response structure when the list comes back empty, and the census carries
        # `outcome: "other"`. A second warning would say less and repeat more.
        return []
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


# The envelope ABOVE the payload, and the two keys read there. The path is SLICED from
# `APPLIANCE_LIST_PATH` instead of being written again, because `success` and
# `authInfo` are only ever looked up in the module envelope -- if a level of the
# response is ever renamed, both walks have to move together or the flags would start
# describing a different node than the one `stopped_at` names.
_MODULE_PATH = APPLIANCE_LIST_PATH[:2]
_SUCCESS_KEY = "success"
# `authInfo` is the channel through which the cloud can hand back a REPLACEMENT cognito
# token (`cognitoTokenNew`): the hOn app adopts it in its generic request layer
# (decomp.txt:525784-525805) and this integration has never read it at all. On a
# freshly authenticated session it is verifiably `{}` (live capture,
# apk/analysis/addhon210-healthy-envelope-baseline.md), which is what makes a COUNT
# worth having: a channel that is silent when things are healthy turns any content
# into a signal. Only the size is taken. Never a key NAME and never a value -- one of
# those values is a bearer token, and a count carries no identity at all.
_AUTH_INFO_KEY = "authInfo"


def _flag(node: Any) -> bool | None:
    """`node` when it is a real bool, else None.

    `type(node) is bool`, not isinstance and not `bool(node)`: the point of these two
    fields is to report what the cloud SAID, so `1`, `"true"` and `"false"` must all
    read as "this build could not tell", not as True. Coercing would invent the one
    answer the field exists to establish. `bool` cannot be subclassed in CPython, so
    the value returned here is `True`, `False` or `None` and nothing else can wear
    those clothes.
    """
    return node if type(node) is bool else None


def _envelope_flags(result: Any) -> dict:
    """The two `success` flags and the SIZE of `authInfo`, above the payload walk.

    Why they are here at all. On the ADDHON-210 report the cloud answered 200 with a
    well-formed envelope and an empty list, and the dump could say only that. But a
    live capture of a HEALTHY response (2026-08-24, one appliance) shows the module
    envelope carries its OWN `success` beside the payload -- and neither this
    integration NOR the official app reads it. A `modules.applianceList.success` of
    false is therefore a state in which the app shows zero appliances too, and in
    which every field the dump already prints looks exactly like a legitimately empty
    account. Two booleans separate those, and nothing else in the document can.

    Read BEFORE the payload walk and merged into every branch of
    `probe_appliance_list`, including the ones that stop early. The failure branches
    are precisely where a reader most needs to know whether the cloud declared the
    call a success: "our walk stopped at `payload`" and "the module reported failure,
    so there was no payload to walk" are the same picture until these are in it.

    Same leak-proof contract as the rest of the probe: `_flag` returns one of two
    singletons of this file or None, and `auth_keys` is `len()` of a mapping -- an
    int, which cannot carry identity. Not one string from the response is read.

    Never raises, and keeps whatever it managed to read when something does: the guard
    is `probe_appliance_list`'s (see its docstring) applied one level down, so a
    hostile mapping cannot cost the caller the flags it had already established.
    """
    flags: dict = {"envelope_ok": None, "module_ok": None, "auth_keys": None}
    try:
        if not isinstance(result, dict):
            return flags
        flags["envelope_ok"] = _flag(result.get(_SUCCESS_KEY))
        node: Any = result
        for key in _MODULE_PATH:
            if not isinstance(node, dict):
                return flags
            node = node.get(key)
        if not isinstance(node, dict):
            return flags
        flags["module_ok"] = _flag(node.get(_SUCCESS_KEY))
        auth_info = node.get(_AUTH_INFO_KEY)
        if isinstance(auth_info, dict):
            flags["auth_keys"] = len(auth_info)
    except Exception:  # noqa: BLE001 - a probe must never abort a setup
        return flags
    return flags


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

    `envelope_ok`/`module_ok`/`auth_keys` describe the envelope ABOVE the walk and are
    computed by `_envelope_flags` FIRST, then merged into every branch below --
    including the early returns and the `except`. A flag captured only on the happy
    path would be missing from exactly the responses that need explaining: see that
    function for what the three answer.
    """
    flags = _envelope_flags(result)
    try:
        node: Any = result
        for key in APPLIANCE_LIST_PATH:
            if not isinstance(node, dict):
                return {"outcome": "not_a_dict", "stopped_at": key,
                        "node_type": _node_type(node), "siblings": None,
                        "count": None, **flags}
            if key not in node:
                return {"outcome": "missing_key", "stopped_at": key,
                        "node_type": "dict", "siblings": len(node),
                        "count": None, **flags}
            node = node[key]
        if isinstance(node, list):
            return {"outcome": "ok", "stopped_at": None,
                    "node_type": "list", "siblings": None,
                    "count": len(node), **flags}
        return {"outcome": "not_a_list", "stopped_at": None,
                "node_type": _node_type(node), "siblings": None,
                "count": None, **flags}
    except Exception:  # noqa: BLE001 - a probe must never abort a setup
        return {"outcome": "other", "stopped_at": None,
                "node_type": None, "siblings": None, "count": None, **flags}
