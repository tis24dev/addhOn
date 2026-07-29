# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).parent / "fixtures" / "contracts"
_REQUIRED = {
    "id", "appliance_type", "schema", "initial_shadow", "action",
    "expected_payload", "expected_shadow_delta", "must_not_change",
    "evidence", "support",
}
_EVIDENCE = {
    "live_observation", "cloud_schema", "official_app_behavior",
    "derived_contract",
}
_SUPPORT = {"confirmed", "experimental"}


def load_contract_cases(name: str) -> list[dict[str, object]]:
    cases = json.loads((_ROOT / name).read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise AssertionError(f"{name}: expected a non-empty list")
    ids: set[str] = set()
    for case in cases:
        missing = _REQUIRED - set(case)
        if missing:
            raise AssertionError(f"{name}: missing {sorted(missing)}")
        # Normalized ONCE, then both compared and stored. Testing the raw value
        # against a set of strings let a duplicate through whenever the spellings
        # differed: "1" recorded first, then a bare 1, matched nothing and collapsed
        # onto the same entry, so two cases shared an id and the second silently
        # shadowed the first in any id-keyed lookup.
        case_id = str(case["id"])
        if case_id in ids:
            raise AssertionError(f"{name}: duplicate id {case['id']}")
        ids.add(case_id)
        if case["evidence"] not in _EVIDENCE:
            raise AssertionError(f"{name}: bad evidence {case['evidence']}")
        if case["support"] not in _SUPPORT:
            raise AssertionError(f"{name}: bad support {case['support']}")
    return cases
