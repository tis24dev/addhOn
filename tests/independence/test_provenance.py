# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

"""Gate B -- provenance-manifest completeness & anti-copy (catches inherited values).

Every module-level literal constant in values.py must be declared in provenance.json
(so a value can never be added without recording where it came from), and every
constant flagged ``must_differ_from_pyhon`` must NOT still equal pyhOn's value (so an
inherited placeholder cannot survive silently).

The single known-inherited value -- the User-Agent -- is checked in its own STRICT
``xfail``: the debt stays VISIBLE (the row is flagged, the check exists) and the suite
stays green while the UA is still the inherited sentinel. Because the xfail is strict,
the day an owner captures a real, differing UA the check xpasses and CI goes RED until
the marker is removed -- so the debt cannot be closed and then left mislabelled as
still-owed (see VALUES-PROVENANCE.md and the OWNER-ACTION note in values.py).
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest

_HERE = pathlib.Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
_VALUES = _REPO / "custom_components" / "addhon" / "client" / "transport" / "values.py"

_MANIFEST = json.loads((_HERE / "provenance.json").read_text())
_ROWS = {row["id"]: row for row in _MANIFEST["constants"]}


def _values_constants() -> dict[str, object]:
    """Module-level ``UPPER_CASE = <literal>`` assignments in values.py."""
    out: dict[str, object] = {}
    for node in ast.parse(_VALUES.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    out[target.id] = node.value.value
    return out


_CONSTANTS = _values_constants()


def test_every_constant_is_declared_in_manifest() -> None:
    missing = sorted(name for name in _CONSTANTS if name not in _ROWS)
    assert not missing, (
        f"values.py constants absent from provenance.json: {missing}. Every literal "
        f"needs a provenance row (see tests/independence/provenance.json)."
    )


def test_manifest_values_match_source() -> None:
    """The manifest must not drift from the actual literal in values.py."""
    drifted = [
        f"{name}: manifest={_ROWS[name]['value']!r} source={value!r}"
        for name, value in _CONSTANTS.items()
        if name in _ROWS and _ROWS[name]["value"] != value
    ]
    assert not drifted, "provenance.json out of sync with values.py: " + "; ".join(drifted)


def test_manifest_has_no_orphan_rows() -> None:
    orphans = sorted(name for name in _ROWS if name not in _CONSTANTS)
    assert not orphans, (
        f"provenance.json rows with no matching values.py constant: {orphans}."
    )


def test_client_chosen_constants_are_not_copied_from_pyhon() -> None:
    """must_differ rows (excluding the UA, which is xfail'd below) must differ."""
    copied = []
    for name, value in _CONSTANTS.items():
        row = _ROWS.get(name)
        if not row or name == "USER_AGENT":
            continue
        if row.get("must_differ_from_pyhon") and "pyhon_value" in row:
            if value == row["pyhon_value"]:
                copied.append(f"{name}={value!r} still equals pyhOn")
    assert not copied, (
        "client-chosen constants still inherited verbatim from pyhOn: "
        + "; ".join(copied)
    )


_ALLOWED_CLASSES = {"OBSERVED", "CLIENT-CHOSEN", "UNRESOLVED"}


def test_every_row_class_is_valid_and_consistent() -> None:
    """The ``class`` label is not decorative: it decides whether a value is ALLOWED to
    equal pyhOn. OBSERVED = Haier-dictated interop (may legitimately be identical);
    CLIENT-CHOSEN / UNRESOLVED = addhOn's own or a to-be-captured value, which MUST
    assert difference so a copied literal cannot hide in a mislabelled row.

    Enforce both the vocabulary and the class<->must_differ tie. Previously ``class``
    was read by no test, so a genuinely inherited value could be parked as OBSERVED /
    must_differ=false to dodge the anti-copy check entirely; now the label and the
    must_differ flag must agree (must_differ iff the class is not OBSERVED)."""
    problems = []
    for name, row in _ROWS.items():
        cls = row.get("class")
        if cls not in _ALLOWED_CLASSES:
            problems.append(f"{name}: class {cls!r} not in {sorted(_ALLOWED_CLASSES)}")
            continue
        expected = cls != "OBSERVED"  # only interop (OBSERVED) values may match pyhOn
        if bool(row.get("must_differ_from_pyhon")) != expected:
            problems.append(
                f"{name}: class {cls} requires must_differ_from_pyhon={expected}, "
                f"got {row.get('must_differ_from_pyhon')!r}"
            )
    assert not problems, (
        "provenance class / must_differ inconsistencies:\n" + "\n".join(problems)
    )


def test_must_differ_rows_declare_a_pyhon_value() -> None:
    """A ``must_differ`` row with no ``pyhon_value`` would be SILENTLY SKIPPED by the
    anti-copy check (it can only compare against a value it actually has), so an
    inherited literal could hide simply by omitting the field. Require every must_differ
    row to carry the pyhOn value it must differ from -- so the check can never no-op."""
    missing = sorted(
        name
        for name, row in _ROWS.items()
        if row.get("must_differ_from_pyhon") and "pyhon_value" not in row
    )
    assert not missing, (
        "must_differ rows missing 'pyhon_value' (the anti-copy check would skip them): "
        f"{missing}. Add the pyhOn value each must differ from."
    )


@pytest.mark.xfail(
    reason=(
        "OWNER-ACTION: USER_AGENT is still pyhOn's synthetic sentinel "
        "'Chrome/999.999.999.999'; capture the real UA from the hOn APK / mitmproxy "
        "and replace it (see VALUES-PROVENANCE.md). Debt is tracked; the suite stays "
        "green while the sentinel stands, and a real differing UA reds this strict xfail."
    ),
    # strict=True: the day a real, differing UA is captured this test xpasses and CI
    # goes red until the marker is removed -- the debt cannot be quietly closed and
    # left mislabelled as still-owed (the ratchet).
    strict=True,
)
def test_user_agent_is_independently_sourced() -> None:
    row = _ROWS["USER_AGENT"]
    assert row.get("must_differ_from_pyhon") is True
    assert _CONSTANTS["USER_AGENT"] != row["pyhon_value"], (
        "USER_AGENT is still equal to pyhOn's inherited placeholder"
    )
