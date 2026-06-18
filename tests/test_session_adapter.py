"""Tests del primo strangle reale: la sessione hОn passa per l'adattatore-ponte.

`client/pyhon_adapter.create_session` è l'UNICO punto che importa _vendor.pyhon per
la sessione; `hon_client.py` non importa più `Hon` direttamente da _vendor.

- Il modulo adapter si carica in isolamento (l'import di pyhОn è lazy, dentro la
  funzione) e `create_session` è callable.
- Guardia di regressione sul sorgente di hon_client: usa il seam e NON contiene
  più `from ._vendor.pyhon import Hon`.
"""
from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ADAPTER = _ROOT / "custom_components" / "addhon" / "client" / "pyhon_adapter.py"
_HON_CLIENT = _ROOT / "custom_components" / "addhon" / "hon_client.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SessionAdapterTest(unittest.TestCase):
    def test_adapter_loads_and_exposes_create_session(self) -> None:
        # L'import di _vendor.pyhon è lazy (dentro create_session), quindi il
        # modulo si carica senza pyhОn/aiohttp installati.
        adapter = _load(_ADAPTER, "addhon_pyhon_adapter")
        self.assertTrue(callable(adapter.create_session))

    def test_adapter_vendor_imports_are_lazy(self) -> None:
        # Gli import di _vendor stanno SOLO dentro le funzioni (lazy: create_session
        # ->session, e i factory create_appliance/create_mqtt/ensure_enum_patch),
        # mai a livello di modulo: così il modulo resta importabile a secco e
        # l'adapter è l'unico ponte verso _vendor.
        tree = ast.parse(_ADAPTER.read_text(encoding="utf-8"))
        module_level_vendor: list[str] = []
        for node in tree.body:  # solo statement top-level
            if isinstance(node, ast.ImportFrom):
                mod = ("." * (node.level or 0)) + (node.module or "")
                if "_vendor" in mod:
                    module_level_vendor.append(mod)
            elif isinstance(node, ast.Import):
                module_level_vendor.extend(a.name for a in node.names if "_vendor" in a.name)
        self.assertEqual(
            module_level_vendor, [], f"import _vendor a livello modulo: {module_level_vendor}"
        )
        # ...ma _vendor È usato (lazy, dentro i factory/patch)
        self.assertIn("_vendor.pyhon", _ADAPTER.read_text(encoding="utf-8"))

    def test_hon_client_no_longer_imports_session_from_vendor(self) -> None:
        src = _HON_CLIENT.read_text(encoding="utf-8")
        self.assertIn("from .client.pyhon_adapter import create_session", src)
        self.assertIn("create_session(self._email, self._password)", src)
        # la sessione NON arriva più da un import diretto di _vendor
        self.assertNotIn("from ._vendor.pyhon import Hon", src)

    def test_enum_patch_reached_via_adapter(self) -> None:
        src = _HON_CLIENT.read_text(encoding="utf-8")
        self.assertIn(
            "from .client.pyhon_adapter import create_session, ensure_enum_patch", src
        )
        self.assertIn("ensure_enum_patch()", src)

    def test_hon_client_has_no_vendor_imports(self) -> None:
        # Milestone: TUTTO il coupling a _vendor di hon_client è dietro il ponte.
        # Controlla gli IMPORT reali (ast), non stringhe/commenti: la riga di log
        # che cita "_vendor.pyhon.connection.api" è un literal, non un import.
        tree = ast.parse(_HON_CLIENT.read_text(encoding="utf-8"))
        vendor_imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = ("." * (node.level or 0)) + (node.module or "")
                if "_vendor" in mod:
                    vendor_imports.append(mod)
            elif isinstance(node, ast.Import):
                vendor_imports.extend(a.name for a in node.names if "_vendor" in a.name)
        self.assertEqual(vendor_imports, [], f"hon_client importa ancora _vendor: {vendor_imports}")


if __name__ == "__main__":
    unittest.main()
