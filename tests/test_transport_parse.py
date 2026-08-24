# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contract test of the transport's appliance-list parser: parse_appliance_list.

Oracle = the unified-api appliance-list contract in
docs/protocol/HAIER-HON-TRANSPORT.md sec9 -- NOT a transcription of pyhOn's inline
extraction. The contract: return the list at
`modules.applianceList.payload.appliances`; ANY unexpected shape (missing key,
non-dict intermediate level, non-list final value) -> `[]` (sec9 fail-safe), and a
truthy non-list final value additionally logs a warning. parse.py is stdlib-only, so
it is loaded in isolation.

The second half of the file covers `probe_appliance_list`, which has no pyhOn
counterpart at all: it reports WHERE that fail-safe walk stopped, so a diagnostics
dump can separate the three responses sec9 deliberately collapses into `[]`.
"""
from __future__ import annotations

import importlib.util
import json
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


# (response, "does this payload carry a list at the path") -- the truth is STATED
# here, not computed from the probe, so the equivalence below compares the probe
# against an author's claim instead of against itself.
_EMPTY_LIST_TABLE = [
    ({"modules": {"applianceList": {"payload": {"appliances": []}}}}, True),
    ({"modules": {"applianceList": {"payload": {"appliances": _REAL}}}}, False),
    ({"modules": {"applianceList": {"payload": {"appliances": {}}}}}, False),
    ({"modules": {"applianceList": {"payload": {"appliances": ""}}}}, False),
    ({"modules": {"applianceList": {"payload": {"appliances": 0}}}}, False),
    ({"modules": {"applianceList": {"payload": {"appliances": None}}}}, False),
    ({"modules": {"applianceList": {"payload": {}}}}, False),
    ({"modules": {"applianceList": {}}}, False),
    ({"modules": {}}, False),
    ({}, False),
    (None, False),
    ({"modules": []}, False),
    ({"modules": {"applianceList": {"payload": []}}}, False),
]


class _HostileDict(dict):
    """A dict SUBCLASS that raises from `__contains__`.

    `isinstance(x, dict)` is True for it, so it walks straight past the type guard and
    into the membership test -- the one input on which the probe's try/except is not
    theoretical. Not reachable from `json.loads`, but reachable from a caller handing
    the transport a duck-typed response, which is what the test doubles in this suite
    do all day.
    """

    def __contains__(self, key):
        raise RuntimeError("hostile mapping")


def _deep(depth: int) -> dict:
    node: dict = {}
    for _ in range(depth):
        node = {"modules": node}
    return node


class ProbeApplianceListTest(unittest.TestCase):
    """`probe_appliance_list` names the LEVEL at which the walk stopped.

    Why it exists: a dump reading `"appliances": []` is compatible with an account the
    cloud reports as empty, with a schema drift our walk cannot follow, and with a body
    that never arrived. The parser above cannot tell them apart -- it returns `[]` for
    all three by contract (sec9 fail-safe) -- and the probe is what does, WITHOUT
    copying a single string chosen by the cloud into a document the user pastes into a
    public issue.
    """

    def setUp(self) -> None:
        module = _load(_OUR_PARSE, "addhon_transport_parse")
        self.probe = module.probe_appliance_list
        self.parse = module.parse_appliance_list
        self.path = module.APPLIANCE_LIST_PATH

    def test_probe_reports_ok_and_the_raw_count(self) -> None:
        full = {"modules": {"applianceList": {"payload": {"appliances": _REAL}}}}
        self.assertEqual(
            {
                "outcome": "ok",
                "stopped_at": None,
                "node_type": "list",
                "siblings": None,
                "count": 2,
            },
            self.probe(full),
        )

    def test_probe_names_the_level_a_missing_key_broke(self) -> None:
        # One case per level: the whole point of the field is that a maintainer can
        # tell "the cloud stopped sending `modules`" from "the payload lost its
        # `appliances`", which are different bugs with different owners.
        # `siblings` is the size of the dict the key was missing FROM, counted after
        # the removal, so each level carries a different number of neighbours and a
        # confusion between two levels cannot pass this test.
        for missing, siblings in (
            ("modules", 1),
            ("applianceList", 2),
            ("payload", 1),
            ("appliances", 3),
        ):
            with self.subTest(missing=missing):
                payload = {"appliances": _REAL, "p1": 1, "p2": 2, "p3": 3}
                appliance_list = {"payload": payload, "a1": 1}
                modules = {"applianceList": appliance_list, "m1": 1, "m2": 2}
                response = {"modules": modules, "r1": 1}
                container = {
                    "modules": response,
                    "applianceList": modules,
                    "payload": appliance_list,
                    "appliances": payload,
                }[missing]
                del container[missing]
                probe = self.probe(response)
                self.assertEqual("missing_key", probe["outcome"])
                self.assertEqual(missing, probe["stopped_at"])
                self.assertEqual("dict", probe["node_type"])
                self.assertEqual(siblings, probe["siblings"])
                # NOT 0: nothing here reached a list, and `count: 0` is reserved for
                # the one state that did and found it empty.
                self.assertIsNone(probe["count"])

    def test_probe_names_the_level_a_non_dict_broke(self) -> None:
        self.assertEqual(
            {
                "outcome": "not_a_dict",
                "stopped_at": "applianceList",
                "node_type": "int",
                "siblings": None,
                "count": None,
            },
            self.probe({"modules": 3}),
        )
        # The response itself is not a dict: the walk stops before its first segment.
        probe = self.probe(None)
        self.assertEqual("not_a_dict", probe["outcome"])
        self.assertEqual("modules", probe["stopped_at"])
        self.assertEqual("NoneType", probe["node_type"])

    def test_probe_reports_a_non_list_leaf(self) -> None:
        probe = self.probe(
            {"modules": {"applianceList": {"payload": {"appliances": "x"}}}}
        )
        self.assertEqual("not_a_list", probe["outcome"])
        self.assertEqual("str", probe["node_type"])
        self.assertIsNone(probe["count"])

    def test_a_count_of_zero_means_an_empty_list_and_nothing_else(self) -> None:
        # The pin on `None` vs `0` for every non-ok branch. With `0` there, the single
        # most consequential reading of the whole dump -- "the cloud sent an empty
        # list, so the bug is not ours" -- would be indistinguishable from a walk that
        # never got near a list.
        for response, carries_empty_list in _EMPTY_LIST_TABLE:
            with self.subTest(response=response):
                self.assertEqual(
                    carries_empty_list, self.probe(response)["count"] == 0
                )

    def test_probe_and_parse_agree_on_the_list(self) -> None:
        # The probe is a SEPARATE walk, not a refactor of the parser (that
        # independence is what makes it shippable on a setup path). This is the pin
        # that keeps the two walks describing the same response.
        table = [response for response, _ in _CASES]
        table += _FAILSAFE
        table += [response for response, _ in _EMPTY_LIST_TABLE]
        self.assertGreaterEqual(len(table), 8)
        for response in table:
            with self.subTest(response=response):
                probe = self.probe(response)
                parsed = self.parse(response)
                if probe["outcome"] == "ok":
                    self.assertEqual(probe["count"], len(parsed))
                else:
                    self.assertEqual([], parsed)

    def test_probe_never_raises(self) -> None:
        # It runs inside NativeHon.setup(): an exception here does not spoil a
        # diagnostic field, it takes the config entry down.
        outcomes = {"ok", "not_a_dict", "missing_key", "not_a_list", "raised", "other"}
        for response in (
            None,
            [],
            "",
            0,
            {"modules": []},
            {str(i): i for i in range(10_000)},
            _deep(200),
            _HostileDict({"modules": {}}),
        ):
            with self.subTest(response=type(response).__name__):
                probe = self.probe(response)
                self.assertIn(probe["outcome"], outcomes)
        # The reserved token has exactly one producer, and this is it.
        self.assertEqual("other", self.probe(_HostileDict({"modules": {}}))["outcome"])

    def test_probe_emits_no_cloud_string(self) -> None:
        # The leak test on the WRITER side. `stopped_at` looks like the natural place
        # to put "the key we were looking for when we stopped", and the key we were
        # looking for is ours -- but the SIBLING key names beside it are the cloud's,
        # and a probe that reported them would ship a MAC-shaped key straight into a
        # public issue.
        hostile = {
            "3C:71:BF:AA:BB:CC": "user@example.com",
            "modules": {
                "SN-PLAINTEXT": {"user@example.com": "3C:71:BF:AA:BB:CC"},
            },
        }
        # A CLASS NAME is the third string the probe could copy, and the only one it
        # reads from an object rather than from the response. `type(x).__name__` is
        # chosen by whoever wrote the class, so `_node_type` MAPS it onto a literal
        # set instead of returning it -- and every other node in this file is a plain
        # dict/list/str, so without this leaf the mapping is never exercised and
        # `return type(node).__name__` passes the whole suite.
        leaked = type("Kitchen_Washer_3C71BFAABBCC", (), {})()
        for probe in (
            self.probe(hostile),
            self.probe(
                {
                    "modules": {
                        "applianceList": {
                            "payload": {
                                "appliances": [
                                    {"macAddress": "3C:71:BF:AA:BB:CC"},
                                    {"serialNumber": "SN-PLAINTEXT"},
                                ]
                            }
                        }
                    }
                }
            ),
            self.probe(
                {"modules": {"applianceList": {"payload": {"appliances": leaked}}}}
            ),
        ):
            blob = json.dumps(probe)
            for leak in (
                "3C:71:BF:AA:BB:CC",
                "user@example.com",
                "SN-PLAINTEXT",
                "Kitchen_Washer_3C71BFAABBCC",
            ):
                self.assertNotIn(leak, blob, leak)
        # ...and the finding survives the mapping: the probe still says it reached the
        # list and found something that is not one.
        foreign = self.probe(
            {"modules": {"applianceList": {"payload": {"appliances": leaked}}}}
        )
        self.assertEqual("not_a_list", foreign["outcome"])
        self.assertEqual("other", foreign["node_type"])

    def test_parse_appliance_list_return_type_is_unchanged(self) -> None:
        # The probe was added beside the parser, not inside it. This is the whole
        # claim, stated on the parser's own contract.
        for response, _ in _CASES:
            with self.subTest(response=response):
                self.assertIsInstance(self.parse(response), list)
        for response in _FAILSAFE:
            with self.subTest(response=response):
                self.assertIsInstance(self.parse(response), list)


if __name__ == "__main__":
    unittest.main()
