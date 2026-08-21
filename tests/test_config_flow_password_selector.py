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

    @staticmethod
    def _is_required_password_key(key: ast.expr | None) -> bool:
        """True for a ``vol.Required("password")`` schema key, and only that.

        The marker call is matched too, not just its argument: accepting any call
        with "password" as first argument would also accept a vol.Optional, or an
        unrelated helper, and quietly weaken the guard.
        """
        return (
            isinstance(key, ast.Call)
            and isinstance(key.func, ast.Attribute)
            and key.func.attr == "Required"
            and bool(key.args)
            and isinstance(key.args[0], ast.Constant)
            and key.args[0].value == "password"
        )

    def _password_schema_values(self) -> list[ast.expr]:
        """Every value bound to a ``vol.Required("password")`` schema key."""
        values: list[ast.expr] = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if self._is_required_password_key(key):
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

        # Structure, not presence: TextSelectorType.PASSWORD appearing anywhere in
        # the assignment would also be satisfied by a selector built the wrong way
        # round, which is exactly the regression this guard exists to catch.
        outer = assigned[0].value
        self.assertTrue(
            isinstance(outer, ast.Call)
            and isinstance(outer.func, ast.Name)
            and outer.func.id == "TextSelector",
            f"{SELECTOR_NAME} must be a TextSelector(...) call",
        )

        self.assertTrue(outer.args, "TextSelector must be given a config")
        config = outer.args[0]
        self.assertTrue(
            isinstance(config, ast.Call)
            and isinstance(config.func, ast.Name)
            and config.func.id == "TextSelectorConfig",
            "TextSelector must wrap a TextSelectorConfig(...) call",
        )

        modes = [kw.value for kw in config.keywords if kw.arg == "type"]
        self.assertEqual(len(modes), 1, "TextSelectorConfig needs one type= keyword")
        self.assertTrue(
            isinstance(modes[0], ast.Attribute)
            and modes[0].attr == "PASSWORD"
            and isinstance(modes[0].value, ast.Name)
            and modes[0].value.id == "TextSelectorType",
            "the config must ask for TextSelectorType.PASSWORD",
        )


if __name__ == "__main__":
    unittest.main()
