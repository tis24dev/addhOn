"""Tests del primo strangle reale: la sessione hОn passa per l'adattatore-ponte.

`client/pyhon_adapter.create_session` è l'UNICO punto che importa _vendor.pyhon per
la sessione; `hon_client.py` non importa più `Hon` direttamente da _vendor.

- Il modulo adapter si carica in isolamento (l'import di pyhОn è lazy, dentro la
  funzione) e `create_session` è callable.
- Guardia di regressione sul sorgente di hon_client: usa il seam e NON contiene
  più `from ._vendor.pyhon import Hon`.
"""
from __future__ import annotations

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

    def test_adapter_is_the_single_vendor_bridge(self) -> None:
        # L'import di _vendor avviene SOLO dentro la funzione (lazy), non a
        # livello di modulo: così client/ resta importabile a secco.
        src = _ADAPTER.read_text(encoding="utf-8")
        self.assertIn("from .._vendor.pyhon import Hon", src)

    def test_hon_client_no_longer_imports_session_from_vendor(self) -> None:
        src = _HON_CLIENT.read_text(encoding="utf-8")
        self.assertIn("from .client.pyhon_adapter import create_session", src)
        self.assertIn("create_session(self._email, self._password)", src)
        # la sessione NON arriva più da un import diretto di _vendor
        self.assertNotIn("from ._vendor.pyhon import Hon", src)


if __name__ == "__main__":
    unittest.main()
