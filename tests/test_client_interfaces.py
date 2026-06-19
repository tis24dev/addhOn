"""Tests for the migration seam (client/interfaces.py).

interfaces.py has NO dependencies (only typing): we load it in total isolation
with importlib (no package __init__, no Home Assistant stubs, no pyhOn), so this
test does not touch the shared state of the pytest process and stays a pure
contract check.

It verifies that the Protocols are runtime_checkable and that an object of the
shape we actually use (value/values, +min/max/step for ranges, command with
send, appliance with commands) is recognized, while a non-conforming one is not.
"""
from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "addhon"
    / "client"
    / "interfaces.py"
)


def _load_interfaces():
    spec = importlib.util.spec_from_file_location("hon_native_interfaces", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClientInterfacesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.I = _load_interfaces()

    def test_parameter_conformance(self) -> None:
        class P:
            value = 1
            values = ["a", "b"]

        self.assertIsInstance(P(), self.I.Parameter)
        self.assertNotIsInstance(object(), self.I.Parameter)

    def test_range_parameter_needs_bounds(self) -> None:
        class OnlyValue:
            value = 5
            values: list = []

        class WithBounds(OnlyValue):
            min = 2.0
            max = 8.0
            step = 1.0

        self.assertIsInstance(WithBounds(), self.I.RangeParameter)
        # without min/max/step it is NOT a RangeParameter (but stays a Parameter)
        self.assertNotIsInstance(OnlyValue(), self.I.RangeParameter)
        self.assertIsInstance(OnlyValue(), self.I.Parameter)

    def test_command_and_appliance_shape(self) -> None:
        class Cmd:
            def __init__(self) -> None:
                self.parameters: dict = {}
                self.categories: dict = {}
                self.category = ""

            def send(self):  # noqa: D401 - shape only
                return None

        self.assertIsInstance(Cmd(), self.I.Command)

        class CmdNoCategories:  # parameters+send but without categories/category
            def __init__(self) -> None:
                self.parameters: dict = {}

            def send(self):
                return None

        self.assertNotIsInstance(CmdNoCategories(), self.I.Command)

        class App:
            commands: dict = {}
            attributes: dict = {}
            statistics: dict = {}
            appliance_type = "REF"
            model_id = 10136
            nick_name = "Frigo"

            def update(self):
                return None

        self.assertIsInstance(App(), self.I.Appliance)

    def test_session_conformance(self) -> None:
        # The shape that the real pyhon.Hon exposes (appliances + context manager).
        # test_session_protocol_live.py verifies that the real Hon has these
        # members; note: runtime_checkable is presence-only, it does not guarantee
        # that __aenter__/__aexit__ are coroutines (we know that from the code).
        class Session:
            appliances: list = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

        self.assertIsInstance(Session(), self.I.HonSession)

        class NoCtx:
            appliances: list = []  # missing __aenter__/__aexit__

        self.assertNotIsInstance(NoCtx(), self.I.HonSession)

    def test_module_is_dependency_free(self) -> None:
        # Check the real IMPORTS (via ast), not the text: the docstring legitimately
        # mentions "homeassistant"/"_vendor" while explaining what it does NOT import.
        tree = ast.parse(_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        bad = sorted(
            m for m in imported if any(t in m for t in ("homeassistant", "pyhon", "_vendor"))
        )
        self.assertEqual(bad, [], f"the seam must not import {bad}")


if __name__ == "__main__":
    unittest.main()
