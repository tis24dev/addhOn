# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Structural guard: no test module may CLOBBER a shared Home Assistant stub.

There is no real `homeassistant` package in this environment. `conftest.py` installs
one complete set of stubs before any test module is imported, and every module then
reuses it through the first-wins `getattr(mod, "X", <fallback>)` idiom.

An UNCONDITIONAL `mod.X = <something>` breaks that contract, and the damage is
invisible in a full run: an entity class binds whatever base is installed the first
time its module is imported, so the failure only appears in some collection orders.
Two real instances were found this way:

  * a bare `SelectEntity` clobbering conftest's, leaving the air purifier's aroma
    select without the `options` property real Home Assistant provides;
  * a `BinarySensorDeviceClass` stub missing `SAFETY`, making an unrelated "never a
    safety device" assertion fail with AttributeError.

Both were discovered only because a deliberate no-op CONTROL mutation was reported
as caught. This test makes the next one fail immediately, in any order.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Symbols conftest.py installs for the whole suite. Assigning one of these anywhere
# in tests/ must go through getattr so the shared stub keeps winning.
SHARED_STUBS = frozenset(
    {
        "AddEntitiesCallback",
        "BinarySensorDeviceClass",
        "BinarySensorEntityDescription",
        "ColorMode",
        "CoordinatorEntity",
        "DataUpdateCoordinator",
        "DeviceEntryType",
        "DeviceInfo",
        "FanEntity",
        "FanEntityFeature",
        "HomeAssistantError",
        "LightEntity",
        "NumberDeviceClass",
        "NumberEntity",
        "NumberEntityDescription",
        "NumberMode",
        "SelectEntity",
        "SensorDeviceClass",
        "SensorEntityDescription",
        "SensorStateClass",
        "SwitchEntity",
        "UpdateFailed",
    }
)


def _getattr_name(value: ast.expr) -> str | None:
    """The attribute name a `getattr(obj, "X", fallback)` call resolves, else None."""
    if not isinstance(value, ast.Call):
        return None
    if not (isinstance(value.func, ast.Name) and value.func.id == "getattr"):
        return None
    if len(value.args) < 2 or not isinstance(value.args[1], ast.Constant):
        return None
    name = value.args[1].value
    return name if isinstance(name, str) else None


def _guarded_bindings(tree: ast.Module) -> dict[str, str]:
    """Local names bound from a getattr, e.g. `base = getattr(exc, "X", ...)`.

    The two-step form `base = getattr(...)` / `exc.X = base` is the same first-wins
    idiom written across two statements (the fallback class then subclasses `base`),
    so it must not be reported.
    """
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        resolved = _getattr_name(node.value)
        if resolved is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = resolved
    return bindings


def _unguarded_assignments(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bindings = _guarded_bindings(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr not in SHARED_STUBS:
                continue
            if _getattr_name(node.value) == target.attr:
                continue
            if (
                isinstance(node.value, ast.Name)
                and bindings.get(node.value.id) == target.attr
            ):
                continue
            found.append(f"{path.name}:{node.lineno}: {target.attr}")
    return found


class StubHygieneTest(unittest.TestCase):
    def test_no_test_module_clobbers_a_shared_stub(self) -> None:
        offenders: list[str] = []
        for path in sorted(TESTS_DIR.rglob("*.py")):
            if path.name == "conftest.py":
                continue  # conftest is the authority; it installs them
            offenders.extend(_unguarded_assignments(path))
        self.assertEqual(
            [],
            offenders,
            "unconditional shared-stub assignment (use "
            'getattr(mod, "X", fallback) so conftest keeps winning): '
            f"{offenders}",
        )

    def test_the_guard_can_actually_see_an_offender(self) -> None:
        """The check is only worth anything if it flags the pattern it forbids."""
        import tempfile

        source = 'mod.SelectEntity = type("SelectEntity", (), {})\n'
        with tempfile.NamedTemporaryFile(
            "w", suffix=".py", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(source)
            probe = Path(handle.name)
        try:
            self.assertEqual(1, len(_unguarded_assignments(probe)))
        finally:
            probe.unlink()

    def test_conftest_installs_every_symbol_the_guard_lists(self) -> None:
        """The list is the contract; a stale entry would make the guard vacuous."""
        conftest_source = (TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
        for symbol in sorted(SHARED_STUBS):
            self.assertIn(f'"{symbol}"', conftest_source, symbol)


if __name__ == "__main__":
    unittest.main()
