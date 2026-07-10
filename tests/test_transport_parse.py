# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Contract test of the transport's appliance-list parser: parse_appliance_list.

Oracle = the unified-api appliance-list contract in
docs/protocol/HAIER-HON-TRANSPORT.md sec9 -- NOT a transcription of pyhOn's inline
extraction. The contract: return the list at
`modules.applianceList.payload.appliances`; ANY unexpected shape (missing key,
non-dict intermediate level, non-list final value) -> `[]` (sec9 fail-safe), and a
truthy non-list final value additionally logs a warning. parse.py is stdlib-only, so
it is loaded in isolation.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OUR_PARSE = _ROOT / "custom_components" / "addhon" / "client" / "transport" / "parse.py"

_REAL = [{"a": 1}, {"b": 2}]


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# (response, expected) -- expected is the sec9-stated result. Only a well-formed list
# survives; everything else (missing/empty/non-list/non-dict intermediate) -> [].
_CASES = [
    ({"modules": {"applianceList": {"payload": {"appliances": _REAL}}}}, _REAL),
    ({"modules": {"applianceList": {"payload": {"appliances": []}}}}, []),
    ({"modules": {"applianceList": {"payload": {"appliances": {"x": 1}}}}}, []),  # truthy non-list
    ({"modules": {"applianceList": {"payload": {"appliances": 0}}}}, []),          # falsy non-list
    ({"modules": {"applianceList": {"payload": {"appliances": None}}}}, []),
    ({"modules": {"applianceList": {"payload": {}}}}, []),
    ({"modules": {"applianceList": {}}}, []),
    ({"modules": {}}, []),
    ({}, []),
    (None, []),
    ([], []),
    ("x", []),
    (123, []),
]

# Non-dict intermediate levels: sec9 fail-safe returns [] (a .get() walk over a
# non-dict would otherwise raise; the parser guards each level with isinstance).
_FAILSAFE = [
    {"modules": "x"},
    {"modules": []},
    {"modules": None},
    {"modules": {"applianceList": "y"}},
    {"modules": {"applianceList": []}},
    {"modules": {"applianceList": None}},
    {"modules": {"applianceList": {"payload": []}}},
    {"modules": {"applianceList": {"payload": "z"}}},
    {"modules": {"applianceList": {"payload": None}}},
]


class ParseApplianceListTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parse = _load(_OUR_PARSE, "addhon_transport_parse").parse_appliance_list

    def test_matches_spec_contract(self) -> None:
        for result, expected in _CASES:
            with self.subTest(result=result):
                self.assertEqual(self.parse(result), expected)

    def test_pinned_real_shape(self) -> None:
        full = {"modules": {"applianceList": {"payload": {"appliances": _REAL}}}}
        self.assertEqual(self.parse(full), _REAL)
        # returns the REAL list object (no copy) so the caller sees the live data.
        self.assertIs(self.parse(full), full["modules"]["applianceList"]["payload"]["appliances"])

    def test_failsafe_on_non_dict_intermediate(self) -> None:
        for result in _FAILSAFE:
            with self.subTest(result=result):
                # sec9: schema drift is treated as "0 appliances", never a crash.
                self.assertEqual(self.parse(result), [])


if __name__ == "__main__":
    unittest.main()
