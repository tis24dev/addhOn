"""Characterization of str_to_float (client/helpers.py).

It used to be a differential test vs pyhOn's str_to_float (_vendor/pyhon/helper.py);
with `_vendor/` deleted, the NATIVE characterization remains: "pinned" values that
fix the behavior (including the int() truncation quirk), proven == pyhOn during the
migration. Loaded in isolation (importlib, no package __init__, no aiohttp).
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OUR_HELPER = _ROOT / "custom_components" / "addhon" / "client" / "helpers.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load the module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StrToFloatCharacterizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ours = _load(_OUR_HELPER, "addhon_client_helpers").str_to_float

    def test_pinned_characterization(self) -> None:
        self.assertEqual(self.ours("5"), 5)
        self.assertEqual(self.ours("0"), 0)
        self.assertEqual(self.ours("-16"), -16)
        self.assertEqual(self.ours("5.5"), 5.5)
        self.assertEqual(self.ours("5,5"), 5.5)       # decimal comma
        self.assertEqual(self.ours("-16.5"), -16.5)
        self.assertEqual(self.ours("0.0"), 0.0)
        self.assertEqual(self.ours(5), 5)
        self.assertEqual(self.ours(5.5), 5)           # QUIRK: float truncated by int()
        self.assertEqual(self.ours("  3 "), 3)        # int() tolerates spaces
        with self.assertRaises(ValueError):
            self.ours("abc")
        with self.assertRaises(ValueError):
            self.ours("1.2.3")
        with self.assertRaises(TypeError):
            self.ours(None)


if __name__ == "__main__":
    unittest.main()
