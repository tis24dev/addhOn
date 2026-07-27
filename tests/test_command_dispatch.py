# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
from tests.contract_fixtures import load_contract_cases


def test_dispatcher_contract_fixture_is_valid() -> None:
    cases = load_contract_cases("dispatcher.json")
    assert {case["id"] for case in cases} == {
        "requested_only",
        "mandatory_included",
        "unrelated_preserved",
    }
    assert all(case["support"] == "confirmed" for case in cases)
