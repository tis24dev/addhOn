# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Haier account password must never render as a plain text field.

A bare ``vol.Required("password"): str`` renders as a normal text input, so the
password stays visible on screen while it is typed. Both the user step and the
reauth step must declare it through a ``TextSelector`` in ``PASSWORD`` mode.

Checked at source level (AST), like the wiring guards in test_mqtt_log_level:
that keeps the guard independent of whether the harness can build a real
Home Assistant selector, and a plain substring match would still pass if the
declaration drifted into an unrelated schema.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FLOW = ROOT / "custom_components" / "addhon" / "config_flow.py"

SELECTOR_NAME = "_PASSWORD_SELECTOR"


class PasswordSelectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tree = ast.parse(CONFIG_FLOW.read_text(encoding="utf-8"))

    def _password_schema_values(self) -> list[ast.expr]:
        """Every value bound to a ``vol.Required("password")`` schema key."""
        values: list[ast.expr] = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Call)
                    and key.args
                    and isinstance(key.args[0], ast.Constant)
                    and key.args[0].value == "password"
                ):
                    values.append(value)
        return values

    def test_both_steps_declare_a_password_field(self) -> None:
        # The user step and the reauth step: losing one would silently drop the
        # guard for that flow.
        self.assertEqual(len(self._password_schema_values()), 2)

    def test_password_never_declared_as_bare_str(self) -> None:
        for value in self._password_schema_values():
            self.assertFalse(
                isinstance(value, ast.Name) and value.id == "str",
                "password declared as a bare str: it would render in clear",
            )

    def test_password_uses_the_shared_selector(self) -> None:
        for value in self._password_schema_values():
            self.assertTrue(
                isinstance(value, ast.Name) and value.id == SELECTOR_NAME,
                f"password must be declared with {SELECTOR_NAME}",
            )

    def test_selector_is_built_in_password_mode(self) -> None:
        assigned = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == SELECTOR_NAME
                for target in node.targets
            )
        ]
        self.assertEqual(len(assigned), 1, f"{SELECTOR_NAME} must be defined once")

        attributes = {
            f"{node.value.id}.{node.attr}"
            for node in ast.walk(assigned[0])
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }
        self.assertIn("TextSelectorType.PASSWORD", attributes)


if __name__ == "__main__":
    unittest.main()
