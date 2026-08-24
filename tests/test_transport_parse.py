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

The third covers the envelope ABOVE that walk -- the two `success` flags and the size
of `authInfo` -- whose oracle is not a spec at all but a live capture of a healthy
response (apk/analysis/addhon210-healthy-envelope-baseline.md, shared as
`tests/_envelopes.py`). sec9 never mentions those levels because nothing read them:
not this integration, and not the official app.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

from _envelopes import healthy, reporter  # noqa: E402

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


    def test_a_mapping_that_fights_back_is_still_zero_appliances(self) -> None:
        """The docstring promised "any unexpected shape -> []" and a subclass broke it.

        `isinstance(x, dict)` is True for a subclass, so a hostile one walks past the
        type guard and into `.get`, where it raises. Before the guard that TypeError
        left `parse_appliance_list` and took the config entry down from inside
        `NativeHon.setup()` -- with a bare exception naming nothing a reporter can act
        on, which is the opposite of what a fail-safe parser is for.

        Only `get` is a door HERE, and that asymmetry is worth stating: this function
        walks with `node.get(key)` while `probe_appliance_list` walks with `key in node`
        followed by `node[key]`, so a subclass raising from `__contains__` stops the
        probe and leaves this one untouched. The two are pinned to agree on the OUTCOME
        of a healthy response, never on which hostile input reaches them -- which is why
        the probe's guard is not evidence that this one was unnecessary.
        """
        for level, hostile in (
            ("top", _HostileGetDict(_REAL_ENVELOPE)),
            ("modules", {"modules": _HostileGetDict({"applianceList": {}})}),
            ("applianceList",
             {"modules": {"applianceList": _HostileGetDict({"payload": {}})}}),
            ("payload",
             {"modules": {"applianceList": {"payload": _HostileGetDict({"appliances": []})}}}),
        ):
            with self.subTest(level=level):
                self.assertEqual([], self.parse(hostile))

    def test_a_contains_that_raises_is_not_this_function_s_door(self) -> None:
        # Documents the asymmetry rather than leaving it to be rediscovered: the same
        # object that drives the probe to `outcome: "other"` is answered normally here.
        self.assertEqual(_REAL, self.parse(_HostileDict(_REAL_ENVELOPE)))

    def test_the_healthy_shape_is_untouched_by_the_guard(self) -> None:
        # The guard must not turn a real answer into a fail-safe one: same object,
        # not a copy and not an empty list.
        self.assertIs(
            _REAL_ENVELOPE["modules"]["applianceList"]["payload"]["appliances"],
            self.parse(_REAL_ENVELOPE),
        )


_REAL_ENVELOPE = {"modules": {"applianceList": {"payload": {"appliances": _REAL}}}}


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


class _HostileGetDict(dict):
    """A dict SUBCLASS that raises from `get`.

    The membership test is not the only door. `_envelope_flags` runs BEFORE the walk's
    try/except and reaches the response through `.get`, so this is the input that
    decides whether its own guard is load-bearing: without it an exception here
    propagates out of `probe_appliance_list` entirely and aborts a config entry setup,
    which is the one thing the whole probe is written not to do.
    """

    def get(self, key, default=None):
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
        # Asserted as a WHOLE dict, so a field added to the probe has to be added here
        # too: the census this returns is copied verbatim into the diagnostics document
        # and its key set is the block's key set.
        full = {"modules": {"applianceList": {"payload": {"appliances": _REAL}}}}
        self.assertEqual(
            {
                "outcome": "ok",
                "stopped_at": None,
                "node_type": "list",
                "siblings": None,
                "count": 2,
                # No envelope at all around this fixture: the two `success` flags and
                # the `authInfo` size are absent, and absent reads as null rather than
                # as false. See EnvelopeFlagsTest below.
                "envelope_ok": None,
                "module_ok": None,
                "auth_keys": None,
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
                "envelope_ok": None,
                "module_ok": None,
                "auth_keys": None,
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

    def test_the_envelope_flags_survive_every_branch_of_the_walk(self) -> None:
        """All FIVE outcomes, as a table, not the two that were easy to reach.

        The reason `_envelope_flags` runs BEFORE the try and is spread into every
        return. A failure branch is exactly where a reader needs to know whether the
        cloud declared the call a success: "our walk stopped at `payload`" and "the
        module said false, so there was no payload" are the same picture otherwise.
        Computing them on the happy path only would leave the field null in every dump
        that needed it, and every test asserting the ok branch would still pass.

        Enumerated rather than sampled because that is exactly how the gap got in: the
        earlier version of this test carried this name while asserting two branches out
        of five, so `not_a_dict` and `not_a_list` could both drop all three flags with
        the whole suite green. Each case below breaks the walk BELOW the module level,
        so the flags stay readable and their loss is a defect rather than an absence.
        """
        def _break(mutate) -> dict:
            envelope = healthy()
            mutate(envelope)
            return envelope

        def _payload(envelope) -> dict:
            return envelope["modules"]["applianceList"]

        cases = (
            # outcome        stopped_at     response
            ("ok", None, healthy()),
            ("missing_key", "appliances",
             _break(lambda e: _payload(e)["payload"].pop("appliances"))),
            ("not_a_dict", "appliances",
             _break(lambda e: _payload(e).__setitem__("payload", "not a mapping"))),
            ("not_a_list", None,
             _break(lambda e: _payload(e)["payload"].__setitem__("appliances", {"a": 1}))),
        )
        for outcome, stopped_at, response in cases:
            with self.subTest(outcome=outcome):
                probe = self.probe(response)
                self.assertEqual(outcome, probe["outcome"])
                self.assertEqual(stopped_at, probe["stopped_at"])
                # The three flags describe the ENVELOPE, which every one of these
                # responses still carries intact: none of them may go null.
                self.assertIs(True, probe["envelope_ok"])
                self.assertIs(True, probe["module_ok"])
                self.assertEqual(0, probe["auth_keys"])

        # A false module flag has to survive too, or the one branch where the cloud
        # TOLD us why the payload is missing is the branch that discards the answer.
        broken = healthy()
        del broken["modules"]["applianceList"]["payload"]
        broken["modules"]["applianceList"]["success"] = False
        probe = self.probe(broken)
        self.assertEqual("missing_key", probe["outcome"])
        self.assertEqual("payload", probe["stopped_at"])
        self.assertIs(True, probe["envelope_ok"])
        self.assertIs(False, probe["module_ok"])
        self.assertEqual(0, probe["auth_keys"])

        # ...and the reserved `other` branch, reached through the guard rather than
        # through a return, which is why it can lose the flags in its own way.
        hostile = _HostileDict(healthy())
        self.assertEqual("other", self.probe(hostile)["outcome"])
        self.assertIs(True, self.probe(hostile)["envelope_ok"])
        self.assertIs(True, self.probe(hostile)["module_ok"])
        self.assertEqual(0, self.probe(hostile)["auth_keys"])

    def test_every_branch_emits_the_same_key_set(self) -> None:
        # The census this returns IS the diagnostics block's key set (the reader spreads
        # it into a document whose fields must be comparable between two downloads), so
        # a branch that forgot `**flags` would silently drop three fields from exactly
        # the responses that need them.
        keys = {
            frozenset(self.probe(response))
            for response in (
                healthy(), reporter(), None, {}, {"modules": 3},
                {"modules": {"applianceList": {"payload": {"appliances": "x"}}}},
                _HostileDict({"modules": {}}),
            )
        }
        self.assertEqual(1, len(keys), keys)
        self.assertEqual(
            {"outcome", "stopped_at", "node_type", "siblings", "count",
             "envelope_ok", "module_ok", "auth_keys"},
            next(iter(keys)),
        )

    def test_the_healthy_capture_reads_as_healthy(self) -> None:
        # The calibration. Every field of the block, against a response verified on the
        # wire: without this the flags are asserted only against shapes this file
        # invented, and "true means the cloud said true" is an assumption.
        self.assertEqual(
            {
                "outcome": "ok", "stopped_at": None, "node_type": "list",
                "siblings": None, "count": 1,
                "envelope_ok": True, "module_ok": True, "auth_keys": 0,
            },
            self.probe(healthy()),
        )

    def test_the_reporter_envelope_differs_only_in_the_count(self) -> None:
        # The null hypothesis, stated as a test. Their log records the two key sets and
        # both match the healthy capture, so a schema drift at those levels is ruled
        # out -- and this pins that the fields added on top do NOT invent a difference
        # where the evidence shows none. The one field that moves is the one their dump
        # already reported.
        healthy_probe = self.probe(healthy())
        reporter_probe = self.probe(reporter())
        self.assertEqual(1, healthy_probe.pop("count"))
        self.assertEqual(0, reporter_probe.pop("count"))
        self.assertEqual(healthy_probe, reporter_probe)

    def test_a_success_that_is_not_a_boolean_is_not_coerced(self) -> None:
        # `bool("false")` is True and `bool(0)` is False: coercion would let the field
        # answer the exact question it exists to establish, using a value the cloud
        # never gave. Null means "this build could not tell", which is the truth.
        for junk in ("true", "false", 1, 0, [], {}, None):
            with self.subTest(junk=junk):
                response = healthy()
                response["success"] = junk
                response["modules"]["applianceList"]["success"] = junk
                probe = self.probe(response)
                self.assertIsNone(probe["envelope_ok"])
                self.assertIsNone(probe["module_ok"])
        # ...and a real boolean still comes through, so the assertions above are not
        # passing merely because the fields are always null.
        false = healthy()
        false["success"] = False
        false["modules"]["applianceList"]["success"] = False
        self.assertIs(False, self.probe(false)["envelope_ok"])
        self.assertIs(False, self.probe(false)["module_ok"])

    def test_auth_info_is_counted_and_never_named(self) -> None:
        # `authInfo` is empty on a healthy session and can carry `cognitoTokenNew`, a
        # bearer token the app adopts and we ignore. The COUNT is the whole signal: a
        # channel that is silent when healthy makes any content meaningful, and a
        # number cannot carry an identity or a credential.
        rotated = healthy()
        rotated["modules"]["applianceList"]["authInfo"] = {
            "cognitoTokenNew": "eyJhbGciOiJIUzI1NiJ9.SECRET-TOKEN-VALUE.sig",
            "3C:71:BF:AA:BB:CC": "user@example.com",
        }
        probe = self.probe(rotated)
        self.assertEqual(2, probe["auth_keys"])
        blob = json.dumps(probe)
        for leak in ("cognitoTokenNew", "SECRET-TOKEN-VALUE", "3C:71:BF:AA:BB:CC",
                     "user@example.com"):
            self.assertNotIn(leak, blob, leak)

    def test_a_foreign_auth_info_is_refused_rather_than_measured(self) -> None:
        # A list has a len() too, and `len("abcdef")` is 6: without the isinstance
        # guard the field would report a character count as a key count, which reads
        # as a rotation that never happened.
        for junk in ([], ["a", "b"], "abcdef", 7, None, True):
            with self.subTest(junk=junk):
                response = healthy()
                response["modules"]["applianceList"]["authInfo"] = junk
                self.assertIsNone(self.probe(response)["auth_keys"])
        # Absent entirely is the same answer as unreadable: null, never 0. 0 is
        # reserved for "the object was there and it was empty", which is the baseline
        # the whole field is calibrated against.
        missing = healthy()
        del missing["modules"]["applianceList"]["authInfo"]
        self.assertIsNone(self.probe(missing)["auth_keys"])

    def test_the_flags_are_read_under_the_module_and_nowhere_else(self) -> None:
        # `success` exists at two levels and the two mean different things, so a walk
        # that read the wrong one would report the envelope's answer as the module's.
        # Deliberately opposite values: a confusion between the levels cannot pass.
        response = healthy()
        response["success"] = False
        response["modules"]["applianceList"]["success"] = True
        probe = self.probe(response)
        self.assertIs(False, probe["envelope_ok"])
        self.assertIs(True, probe["module_ok"])
        # A `success` sitting anywhere else is not either of them.
        stray = healthy()
        del stray["success"]
        del stray["modules"]["applianceList"]["success"]
        stray["modules"]["success"] = True
        stray["modules"]["applianceList"]["payload"]["success"] = True
        self.assertIsNone(self.probe(stray)["envelope_ok"])
        self.assertIsNone(self.probe(stray)["module_ok"])

    def test_the_flags_never_raise(self) -> None:
        # Same standard as the walk beside them, and a STRONGER requirement: the flags
        # are computed before the walk's try/except, so an exception escaping them
        # escapes `probe_appliance_list` itself and takes a config entry down with it.
        # `_HostileGetDict` is the input that decides it -- the flags reach the
        # response through `.get`, which the walk's own hostile double never touches.
        for response in (
            _HostileDict({"modules": {}}),
            _HostileGetDict({"modules": {}}),
            {"modules": _HostileGetDict({"applianceList": {}})},
            {"modules": _HostileDict({"applianceList": {}})},
            {"modules": {"applianceList": _HostileGetDict({"success": True})}},
            _deep(200),
            {str(i): i for i in range(10_000)},
        ):
            with self.subTest(response=type(response).__name__):
                probe = self.probe(response)
                for key in ("envelope_ok", "module_ok"):
                    self.assertIn(probe[key], (True, False, None))
                self.assertIn(type(probe["auth_keys"]), (int, type(None)))
        # ...and what it managed to read before the raise is KEPT rather than thrown
        # away with it: `envelope_ok` was already established when the module level
        # exploded, and it is the half that says whether the cloud declared success.
        partial = self.probe(
            {"success": True, "modules": _HostileGetDict({"applianceList": {}})}
        )
        self.assertIs(True, partial["envelope_ok"])
        self.assertIsNone(partial["module_ok"])

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
