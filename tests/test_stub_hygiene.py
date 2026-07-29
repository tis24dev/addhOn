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


_MAIN = "__main__"
# Synthetic guard sources, assembled so this module's own text carries exactly one
# real `__main__` guard: a literal one inside a string would still read as a second
# occurrence to anyone grepping, and to a future check that is textual.
HEAD_GUARD = f"if __name__ == {_MAIN!r}:\n"
INVERTED_GUARD = f"if __name__ != {_MAIN!r}:\n"
REVERSED_GUARD = f"if {_MAIN!r} == __name__:\n"


def _main_guards(tree: ast.Module) -> list[tuple[int, int]]:
    """(line, module-level statements that follow) for each `__main__` guard.

    Both operand orders are accepted and the operator must be `==`: an inverted
    `if __name__ != "__main__":` is a different construct, and reading it as a guard
    would let a real one hide behind it. Only that exact comparison is recognized,
    which is the single spelling this corpus uses; a novel one would be invisible
    here, and that is the trade for a check with no false positives.
    """
    found = []
    for index, node in enumerate(tree.body):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        if not isinstance(test.ops[0], ast.Eq) or len(test.comparators) != 1:
            continue
        operands = (test.left, test.comparators[0])
        names = {n.id for n in operands if isinstance(n, ast.Name)}
        literals = {
            n.value
            for n in operands
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        if names == {"__name__"} and literals == {"__main__"}:
            # Every module-level statement, not only definitions: the rule is that the
            # guard is the LAST thing in the file, and a trailing assignment or call is
            # just as stranded as a trailing class.
            found.append((node.lineno, len(tree.body[index + 1 :])))
    return found


class MainGuardPlacementTest(unittest.TestCase):
    """A `__main__` guard must be the LAST thing in a test module.

    `test_air_purifier_entities.py` carried one at line 490 of 2561 with 54 module
    level definitions below it, accumulated by appending a class per task. Under
    pytest it is inert, which is why a full green run never showed it, and that module
    cannot run standalone at all (it needs conftest's stubs installed first), so
    nothing was silently skipped either. What it was is a dead entry point that reads
    as the end of the file and strands everything after it: the next reader appending
    a class has no signal that they are below the line.

    Placement is all this checks. Whether a guard WORKS is a separate, pre-existing
    and much wider question, since a standalone run of the corpus shows many modules
    failing or crashing on their stub bootstrap; curing one of them here would leave
    its siblings untouched. Absence of a guard is fine too, and several modules have
    none.
    """

    #: Floors, not exact counts. They only have to prove the sweep really read the
    #: corpus: an empty scan would otherwise be indistinguishable from a clean one.
    MIN_MODULES = 50
    MIN_GUARDS = 50

    def test_no_statement_follows_a_main_guard(self) -> None:
        modules = sorted(TESTS_DIR.rglob("*.py"))
        self.assertGreaterEqual(len(modules), self.MIN_MODULES)
        guarded, offenders = 0, []
        for path in modules:
            for line, following in _main_guards(
                ast.parse(path.read_text(encoding="utf-8"))
            ):
                guarded += 1
                if following:
                    offenders.append(
                        f"{path.name}:{line} has {following} statements after it"
                    )
        self.assertEqual([], offenders)
        self.assertGreaterEqual(guarded, self.MIN_GUARDS)

    def test_at_most_one_main_guard_per_module(self) -> None:
        for path in sorted(TESTS_DIR.rglob("*.py")):
            guards = _main_guards(ast.parse(path.read_text(encoding="utf-8")))
            self.assertLessEqual(len(guards), 1, f"{path.name}: {guards}")

    def test_the_placement_guard_sees_every_stranded_shape(self) -> None:
        """Anti-vacuity. A class is the shape that actually occurred; a function and a
        bare statement are counted too, so narrowing the check back to definitions
        alone fails here rather than silently shrinking the guard."""
        head = HEAD_GUARD + "    pass\n\n\n"
        for trailing in (
            "class Late:\n    pass\n",
            "def late():\n    pass\n",
            "async def late():\n    pass\n",
            "LATE = 1\n",
        ):
            self.assertEqual(
                [(1, 1)], _main_guards(ast.parse(head + trailing)), trailing
            )
        clean = "class Early:\n    pass\n\n\n" + HEAD_GUARD + "    pass\n"
        self.assertEqual([(5, 0)], _main_guards(ast.parse(clean)))

    def test_an_inverted_name_check_is_not_a_main_guard(self) -> None:
        """`!=` is a different construct; reading it as a guard would let a real one
        hide behind it and go unchecked."""
        source = INVERTED_GUARD + "    pass\n\n\nclass Late:\n    pass\n"
        self.assertEqual([], _main_guards(ast.parse(source)))

    def test_either_operand_order_is_recognized(self) -> None:
        source = REVERSED_GUARD + "    pass\n\n\nclass Late:\n    pass\n"
        self.assertEqual([(1, 1)], _main_guards(ast.parse(source)))


class StubInstallerDocstringTest(unittest.TestCase):
    """conftest's platform installer must say what it installs.

    Its name said `fan` while it stubbed seven platforms, having grown one per
    campaign task, and its docstring listed five. A reader deciding whether their
    module needs its own stub was reading a list that had been wrong for weeks.
    """

    @staticmethod
    def _installer_source() -> str:
        import ast

        source = (TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_install_entity_platform_stubs"
        )
        return ast.get_source_segment(source, function) or ""

    def test_the_docstring_lists_every_platform_it_stubs(self) -> None:
        import re

        body = self._installer_source()
        stubbed = set(re.findall(r"homeassistant\.components\.([a-z_]+)", body))
        self.assertGreaterEqual(len(stubbed), 5, stubbed)
        docstring = body[: body.index('"""', body.index('"""') + 3)]
        for platform in sorted(stubbed):
            self.assertIn(f"`{platform}`", docstring, platform)


if __name__ == "__main__":
    unittest.main()
