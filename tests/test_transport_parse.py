"""Differential test of the native transport's 2nd piece: parse_appliance_list.

pyhOn's extraction logic lives INLINE in the async+HTTP method
`api.load_appliances`, so it is not importable on its own: the oracle is its
VERBATIM transcription (`_pyhon_extract` below). We compare our parser against
the oracle on many responses; plus the INTENTIONAL DIVERGENCE cases where pyhOn
crashes (a `.get()` chain on a non-dict intermediate) and we fall back to `[]`.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OUR_PARSE = _ROOT / "custom_components" / "addhon" / "client" / "transport" / "parse.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _pyhon_extract(result):
    """Oracle: VERBATIM transcription of pyhon api.load_appliances' parsing (minus
    the logging). NOT importable on its own because it is inline in an async+HTTP method."""
    appliances = []
    if isinstance(result, dict):
        raw = (
            result.get("modules", {})
            .get("applianceList", {})
            .get("payload", {})
            .get("appliances", [])
        )
        if isinstance(raw, list):
            appliances = raw
        elif raw:
            pass  # pyhon logs a warning here; only the return value matters for the comparison
    return appliances


# Well-formed / missing / empty responses: our parser MUST give the same result
# as pyhOn.
_EQUAL = [
    {"modules": {"applianceList": {"payload": {"appliances": [{"a": 1}, {"b": 2}]}}}},
    {"modules": {"applianceList": {"payload": {"appliances": []}}}},
    {"modules": {"applianceList": {"payload": {"appliances": {"x": 1}}}}},  # non-list truthy
    {"modules": {"applianceList": {"payload": {"appliances": 0}}}},          # non-list falsy
    {"modules": {"applianceList": {"payload": {"appliances": None}}}},
    {"modules": {"applianceList": {"payload": {}}}},
    {"modules": {"applianceList": {}}},
    {"modules": {}},
    {},
    None,
    [],
    "x",
    123,
]

# Malformed shapes with a NON-dict intermediate level: pyhOn crashes
# (AttributeError), we fall back to [] (intentional hardening).
_HARDENED = [
    {"modules": "x"},
    {"modules": []},
    {"modules": None},                                       # None intermediate
    {"modules": {"applianceList": "y"}},
    {"modules": {"applianceList": []}},
    {"modules": {"applianceList": None}},
    {"modules": {"applianceList": {"payload": []}}},
    {"modules": {"applianceList": {"payload": "z"}}},
    {"modules": {"applianceList": {"payload": None}}},       # None intermediate (payload)
]


class ParseApplianceListTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parse = _load(_OUR_PARSE, "addhon_transport_parse").parse_appliance_list

    def test_matches_pyhon_on_wellformed(self) -> None:
        for result in _EQUAL:
            with self.subTest(result=result):
                self.assertEqual(self.parse(result), _pyhon_extract(result))

    def test_pinned_real_shape(self) -> None:
        full = {"modules": {"applianceList": {"payload": {"appliances": [{"a": 1}, {"b": 2}]}}}}
        self.assertEqual(self.parse(full), [{"a": 1}, {"b": 2}])
        # returns the REAL list (same object, not a copy): like pyhOn
        self.assertIs(self.parse(full), full["modules"]["applianceList"]["payload"]["appliances"])

    def test_hardened_vs_pyhon_crash_on_intermediate_non_dict(self) -> None:
        for result in _HARDENED:
            with self.subTest(result=result):
                # pyhOn crashes on these (documents the fragility we removed)...
                with self.assertRaises(AttributeError):
                    _pyhon_extract(result)
                # ...we fall back to [] (fail-safe).
                self.assertEqual(self.parse(result), [])


if __name__ == "__main__":
    unittest.main()
