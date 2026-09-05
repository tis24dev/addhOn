# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

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

    def test_refresh_token_read_and_persisted(self) -> None:
        # 2FA: async_setup_entry must (a) read the persisted refresh_token from the entry
        # and pass it to HonClient, and (b) write back a ROTATED token via the single
        # _persist_refresh_token helper, guarded so it only writes on a real change (no
        # entry churn). The helper is called BOTH at setup and on the coordinator path so a
        # token rotated later survives a restart. A behavioral test of the closure is
        # infeasible with the stub harness, so guard the source/AST.
        source = INIT.read_text(encoding="utf-8")
        self.assertIn('entry.data.get("refresh_token"', source,
                      "must read the persisted refresh_token from the entry")
        self.assertIn("refresh_token=refresh_token", source,
                      "must pass the refresh_token to HonClient")
        self.assertIn("hon_client.refresh_token", source,
                      "must read back the (possibly rotated) refresh_token")
        # the helper's write is conditional on a genuine change (truthy AND different)
        self.assertIn("new_token and new_token != stored", source,
                      "the persist must be gated so it never wipes a token or churns")
        self.assertIn("async_update_entry", source)
        self.assertIn("def _persist_refresh_token", source,
                      "the persist logic must live in one helper (deduped)")
        # the helper must be CALLED at >=2 sites (setup + coordinator path), and at least
        # one call inside async_update_data -- AST so a comment/whitespace can't fool it.
        tree = ast.parse(source)
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "_persist_refresh_token"
        ]
        self.assertGreaterEqual(len(calls), 2, "persist helper must run at setup AND on update")
        in_update = False
        for fn in ast.walk(tree):
            if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "async_update_data":
                in_update = any(
                    isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_persist_refresh_token"
                    for n in ast.walk(fn)
                )
        self.assertTrue(in_update, "coordinator update must persist a rotated token")

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
        # Robust to import ordering / extra co-imports (e.g. redact_id added for the
        # INFO privacy-log fixes): only require redact_mac to be imported here.
        self.assertRegex(source, r"from \.debug_utils import [^\n]*\bredact_mac\b")

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
            name_value = kv["name"]
            self.assertIsInstance(name_value, ast.Call)
            name_func = name_value.func
            self.assertTrue(
                (isinstance(name_func, ast.Name) and name_func.id == "redact_id")
                or (isinstance(name_func, ast.Attribute) and name_func.attr == "redact_id"),
                "summary 'name' must be a redact_id(...) call",
            )
            self.assertEqual(len(name_value.args), 1)
            raw_name = name_value.args[0]
            self.assertIsInstance(raw_name, ast.Call)
            self.assertIsInstance(raw_name.func, ast.Attribute)
            self.assertEqual(raw_name.func.attr, "get")
            self.assertIsInstance(raw_name.func.value, ast.Name)
            self.assertEqual(raw_name.func.value.id, "appliance_data")
            self.assertEqual(len(raw_name.args), 1)
            self.assertIsInstance(raw_name.args[0], ast.Constant)
            self.assertEqual(raw_name.args[0].value, "name")

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


class CommandCatalogLifecycleSourceTest(unittest.TestCase):
    @staticmethod
    def _functions() -> dict[str, ast.AsyncFunctionDef]:
        tree = ast.parse(INIT.read_text(encoding="utf-8"))
        return {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
        }

    @staticmethod
    def _attribute_calls(
        function: ast.AsyncFunctionDef,
        attribute: str,
        receiver: str | None = None,
    ) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attribute
            and (
                receiver is None
                or (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == receiver
                )
            )
        ]

    def test_cache_load_precedes_client_construction_with_language_and_document(
        self,
    ) -> None:
        setup = self._functions()["async_setup_entry"]
        loads = self._attribute_calls(setup, "async_load", "catalog_store")
        constructors = [
            node
            for node in ast.walk(setup)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "HonClient"
        ]
        self.assertEqual(1, len(loads), "setup must load exactly one catalog store")
        self.assertEqual(1, len(constructors), "setup must construct exactly one HonClient")
        constructor = constructors[0]
        self.assertLess(loads[0].lineno, constructor.lineno)
        keywords = {keyword.arg: keyword.value for keyword in constructor.keywords}
        self.assertIsInstance(keywords.get("command_catalog_cache"), ast.Name)
        self.assertEqual("command_catalog_cache", keywords["command_catalog_cache"].id)
        language = keywords.get("language")
        self.assertIsInstance(language, ast.Call)
        self.assertIsInstance(language.func, ast.Name)
        self.assertEqual("getattr", language.func.id)
        self.assertEqual(3, len(language.args))
        self.assertIsInstance(language.args[0], ast.Attribute)
        self.assertEqual("config", language.args[0].attr)
        self.assertIsInstance(language.args[0].value, ast.Name)
        self.assertEqual("hass", language.args[0].value.id)
        self.assertIsInstance(language.args[1], ast.Constant)
        self.assertEqual("language", language.args[1].value)
        self.assertIsInstance(language.args[2], ast.Constant)
        self.assertIsNone(language.args[2].value)

    def test_store_sync_follows_setup_and_runs_inside_successful_poll(self) -> None:
        functions = self._functions()
        setup = functions["async_setup_entry"]
        update = functions["async_update_data"]
        setup_sync_references = [
            node
            for node in ast.walk(setup)
            if isinstance(node, ast.Attribute)
            and node.attr == "setup_sync"
            and isinstance(node.value, ast.Name)
            and node.value.id == "hon_client"
        ]
        all_syncs = self._attribute_calls(setup, "async_sync", "catalog_store")
        update_syncs = self._attribute_calls(update, "async_sync", "catalog_store")
        self.assertEqual(1, len(setup_sync_references))
        self.assertEqual(2, len(all_syncs), "sync once after setup and once per poll")
        self.assertEqual(1, len(update_syncs), "successful poll must sync its cache")
        setup_only_sync = next(
            sync for sync in all_syncs if sync.lineno != update_syncs[0].lineno
        )
        self.assertGreater(setup_only_sync.lineno, setup_sync_references[0].lineno)
        polls = [
            node
            for node in ast.walk(update)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_get_appliances_data"
        ]
        self.assertEqual(1, len(polls))
        self.assertGreater(update_syncs[0].lineno, polls[0].lineno)

    def test_unload_retains_cache_and_entry_removal_awaits_deletion(self) -> None:
        functions = self._functions()
        unload = functions["async_unload_entry"]
        remove = functions["async_remove_entry"]
        self.assertEqual(
            [], self._attribute_calls(unload, "async_remove", "catalog_store")
        )
        removals = self._attribute_calls(remove, "async_remove", "catalog_store")
        self.assertEqual(1, len(removals))
        awaited_calls = [
            node.value
            for node in ast.walk(remove)
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
        ]
        self.assertIn(removals[0], awaited_calls)

    def test_entry_bucket_keeps_the_exact_store_adapter(self) -> None:
        setup = self._functions()["async_setup_entry"]
        buckets = [
            node
            for node in ast.walk(setup)
            if isinstance(node, ast.Dict)
            and any(
                isinstance(key, ast.Constant) and key.value == "command_catalog_store"
                for key in node.keys
            )
        ]
        self.assertEqual(1, len(buckets))
        bucket = buckets[0]
        values = {
            key.value: value
            for key, value in zip(bucket.keys, bucket.values)
            if isinstance(key, ast.Constant)
        }
        self.assertIsInstance(values["command_catalog_store"], ast.Name)
        self.assertEqual("catalog_store", values["command_catalog_store"].id)


if __name__ == "__main__":
    unittest.main()
