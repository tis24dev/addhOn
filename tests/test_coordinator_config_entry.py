"""Regression tests for binding the DataUpdateCoordinator to its config entry.

Covers the fix that passes config_entry=entry to DataUpdateCoordinator. That
keyword exists only since HA 2024.11 (and omitting it hard-breaks in a later
release), so the minimum HA version is declared in hacs.json (the only valid
place; manifest.json has no min-version key and would reject one via hassfest).

A behavioral test is infeasible with the repo's stub harness (async_setup_entry
runs the executor login, first refresh and platform forwarding), so these are
source/manifest-level guards that catch accidental regressions.
"""
from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "addhon"
INIT = COMPONENT / "__init__.py"
MANIFEST = COMPONENT / "manifest.json"
HACS = ROOT / "hacs.json"

# Minimum HA version that accepts DataUpdateCoordinator(config_entry=...).
_MIN_FOR_CONFIG_ENTRY = (2024, 11, 0)


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


class CoordinatorConfigEntryTest(unittest.TestCase):
    def test_coordinator_constructed_with_config_entry(self) -> None:
        source = INIT.read_text(encoding="utf-8")
        self.assertIn(
            "config_entry=entry",
            source,
            "DataUpdateCoordinator must receive config_entry=entry "
            "(HA 2024.11+; omitting it breaks on newer HA)",
        )

    def test_coordinator_summary_redacts_mac(self) -> None:
        # #24: the per-device debug summary must not log the raw MAC (a behavioral
        # test is infeasible: async_update_data is a closure inside async_setup_entry).
        source = INIT.read_text(encoding="utf-8")
        self.assertIn(
            '"mac": redact_mac(',
            source,
            "the coordinator debug summary must redact the MAC",
        )
        self.assertNotIn(
            '"mac": appliance_data.get("mac")',
            source,
            "raw MAC must not be put in the coordinator debug summary",
        )
        # The summary 'id' is the appliance_id = unique_id = MAC (or serial): it must
        # be redacted too (GAP found by the refuter pool), not just 'mac'.
        self.assertIn(
            '"id": redact_mac(appliance_id)',
            source,
            "the coordinator debug summary 'id' (= MAC/serial) must be redacted",
        )
        self.assertNotIn('"id": appliance_id,', source)
        self.assertIn("from .debug_utils import redact_mac", source)

    def test_coordinator_summary_redacts_mac_ast(self) -> None:
        # Robust (decoy/whitespace-proof) version of the guard above: AST-parse the
        # summary dict literal and require its 'id' and 'mac' values to be a
        # redact_mac(...) call. A substring guard is fooled by a comment + a space
        # before the comma; this is not.
        def _is_redact_mac(call: ast.AST) -> bool:
            if not isinstance(call, ast.Call):
                return False
            func = call.func
            return (isinstance(func, ast.Name) and func.id == "redact_mac") or (
                isinstance(func, ast.Attribute) and func.attr == "redact_mac"
            )

        tree = ast.parse(INIT.read_text(encoding="utf-8"))
        summaries = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
                if {"id", "name", "type", "mac"} <= set(keys):
                    summaries.append(node)
        self.assertTrue(summaries, "coordinator summary dict literal not found")
        for node in summaries:
            kv = {
                k.value: v
                for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant)
            }
            for field in ("id", "mac"):
                self.assertTrue(
                    _is_redact_mac(kv[field]),
                    f"summary '{field}' must be a redact_mac(...) call",
                )

    def test_manifest_has_no_invalid_homeassistant_key(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        # "homeassistant" is NOT a valid manifest.json key (hassfest rejects it,
        # the loader never reads it). Min HA version belongs in hacs.json.
        self.assertNotIn("homeassistant", manifest)
        self.assertNotIn("min_version", manifest)

    def test_hacs_declares_min_ha_for_config_entry(self) -> None:
        self.assertTrue(HACS.is_file(), "hacs.json must declare the minimum HA version")
        hacs = json.loads(HACS.read_text(encoding="utf-8"))
        min_version = hacs.get("homeassistant")
        self.assertIsNotNone(
            min_version, "hacs.json must declare a minimum 'homeassistant' version"
        )
        self.assertGreaterEqual(
            _version_tuple(min_version),
            _MIN_FOR_CONFIG_ENTRY,
            f"hacs.json homeassistant {min_version} is below the 2024.11.0 needed "
            "to pass config_entry to DataUpdateCoordinator",
        )


if __name__ == "__main__":
    unittest.main()
