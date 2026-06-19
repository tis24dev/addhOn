"""Tests del seam di migrazione (client/interfaces.py).

interfaces.py è SENZA dipendenze (solo typing): lo carichiamo in totale
isolamento con importlib (niente package __init__, niente stub Home Assistant,
niente pyhОn), così questo test non tocca lo stato condiviso del processo pytest
e resta una verifica pura del contratto.

Verifica che i Protocol siano runtime_checkable e che un oggetto della forma che
usiamo davvero (value/values, +min/max/step per i range, command con send,
appliance con commands) sia riconosciuto, mentre uno non conforme no.
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
        # senza min/max/step NON è un RangeParameter (ma resta un Parameter)
        self.assertNotIsInstance(OnlyValue(), self.I.RangeParameter)
        self.assertIsInstance(OnlyValue(), self.I.Parameter)

    def test_command_and_appliance_shape(self) -> None:
        class Cmd:
            parameters: dict = {}
            categories: dict = {}
            category = ""

            def send(self):  # noqa: D401 - shape only
                return None

        self.assertIsInstance(Cmd(), self.I.Command)

        class CmdNoCategories:  # parameters+send ma senza categories/category
            parameters: dict = {}

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
        # La forma che il vero pyhon.Hon espone (appliances + context manager).
        # test_session_protocol_live.py verifica che il Hon reale abbia questi
        # membri; nota: runtime_checkable è presence-only, non garantisce che
        # __aenter__/__aexit__ siano coroutine (quello lo sappiamo dal codice).
        class Session:
            appliances: list = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

        self.assertIsInstance(Session(), self.I.HonSession)

        class NoCtx:
            appliances: list = []  # manca __aenter__/__aexit__

        self.assertNotIsInstance(NoCtx(), self.I.HonSession)

    def test_module_is_dependency_free(self) -> None:
        # Controlla gli IMPORT reali (via ast), non il testo: la docstring cita
        # legittimamente "homeassistant"/"_vendor" spiegando cosa NON importa.
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
        self.assertEqual(bad, [], f"il seam non deve importare {bad}")


if __name__ == "__main__":
    unittest.main()
