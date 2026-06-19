"""Guard for the TOTAL detachment from pyhOn (Phase 4 completed).

History: the hOn session went through the bridge adapter `factory` (the only
file that imported `_vendor.pyhon`). With `_vendor/` DELETED, this guard verifies
the final goal: NO integration file imports `_vendor` anymore, and `_vendor/` does
not exist. `factory` stays the factory for the native client.
"""
from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_COMPONENT = _ROOT / "custom_components" / "addhon"
_ADAPTER = _COMPONENT / "client" / "factory.py"
_HON_CLIENT = _COMPONENT / "hon_client.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vendor_imports(path: Path) -> list[str]:
    out: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            if "_vendor" in mod:
                out.append(f"{path.name}: {mod}")
        elif isinstance(node, ast.Import):
            out.extend(f"{path.name}: {a.name}" for a in node.names if "_vendor" in a.name)
    return out


class TotalDetachGuardTest(unittest.TestCase):
    def test_vendor_dir_deleted(self) -> None:
        self.assertFalse((_COMPONENT / "_vendor").exists(), "_vendor/ still exists")

    def test_no_vendor_imports_anywhere(self) -> None:
        offenders: list[str] = []
        for py in _COMPONENT.rglob("*.py"):
            offenders.extend(_vendor_imports(py))
        self.assertEqual(offenders, [], f"leftover _vendor imports: {offenders}")

    def test_adapter_loads_and_exposes_factories(self) -> None:
        adapter = _load(_ADAPTER, "addhon_factory")
        self.assertTrue(callable(adapter.create_session))
        self.assertTrue(callable(adapter.create_appliance))
        # the old BABYCARE patch has been removed (native fix in the enum)
        self.assertFalse(hasattr(adapter, "ensure_enum_patch"))

    def test_hon_client_uses_native_factory(self) -> None:
        src = _HON_CLIENT.read_text(encoding="utf-8")
        self.assertIn("from .client.factory import create_session", src)
        self.assertIn("create_session(self._email, self._password)", src)
        self.assertNotIn("ensure_enum_patch", src)


if __name__ == "__main__":
    unittest.main()
