# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.contract_fixtures import load_contract_cases

_AP_SCHEMA_FIXTURE = (
    Path(__file__).parent / "fixtures" / "ap" / "schema.json"
)


def _ap_schema() -> dict[str, Any]:
    return json.loads(_AP_SCHEMA_FIXTURE.read_text(encoding="utf-8"))


def _simplify(schema: dict[str, Any]) -> dict[str, Any]:
    """Reduce the full command-attribute schema carried by each case to the
    shorthand notation of `fixtures/ap/schema.json`, so the two representations
    of the same device can be compared instead of silently drifting apart."""
    simplified: dict[str, Any] = {}
    for command_name, body in schema.items():
        params: dict[str, Any] = {}
        for name, attributes in body["parameters"].items():
            match attributes["typology"]:
                case "enum":
                    params[name] = [str(v) for v in attributes["enumValues"]]
                case "fixed":
                    params[name] = {"fixed": str(attributes["fixedValue"])}
                case "range":
                    params[name] = {
                        "min": float(attributes["minimumValue"]),
                        "max": float(attributes["maximumValue"]),
                        "step": float(attributes["incrementValue"]),
                    }
                case unknown:  # pragma: no cover - guards fixture authoring
                    raise AssertionError(f"{name}: unsupported typology {unknown}")
        simplified[command_name] = params
    return simplified


def _normalize_shorthand(commands: dict[str, Any]) -> dict[str, Any]:
    """Same normalization applied to the shorthand fixture, so numeric ranges
    compare by value rather than by JSON spelling."""
    normalized: dict[str, Any] = {}
    for command_name, params in commands.items():
        entries: dict[str, Any] = {}
        for name, declared in params.items():
            if isinstance(declared, list):
                entries[name] = [str(v) for v in declared]
            elif "fixed" in declared:
                entries[name] = {"fixed": str(declared["fixed"])}
            else:
                entries[name] = {
                    "min": float(declared["min"]),
                    "max": float(declared["max"]),
                    "step": float(declared["step"]),
                }
        normalized[command_name] = entries
    return normalized


def _mandatory_payload(parameters: dict[str, Any]) -> dict[str, str]:
    """Keys the dispatcher adds from the schema, which an intent must never
    duplicate: every mandatory parameter at its schema-dictated value."""
    result: dict[str, str] = {}
    for name, attributes in parameters.items():
        if not attributes.get("mandatory"):
            continue
        if attributes["typology"] == "fixed":
            result[name] = str(attributes["fixedValue"])
        else:
            result[name] = str(attributes["defaultValue"])
    return result


def test_ap_contract_matrix_is_complete() -> None:
    cases = load_contract_cases("air_purifier.json")
    actions = {case["action"] for case in cases}
    assert {
        "turn_on_auto", "turn_off", "preset_sleep", "preset_auto",
        "preset_max", "light_off", "light_50", "light_100",
        "lock_on", "lock_off", "tone_on", "tone_off",
        "aroma_off", "aroma_soft", "aroma_mid", "aroma_h_biotics",
        "aroma_custom",
    } <= actions


def test_contract_never_sends_off_or_allergen_mode() -> None:
    for case in load_contract_cases("air_purifier.json"):
        payload = case["expected_payload"]
        assert payload.get("machMode") not in {"0", "3"}


def test_every_case_carries_the_ap_specific_keys() -> None:
    """`command_name` and `values` sit outside contract_fixtures' shared
    `_REQUIRED` set, because dispatcher.json spells the same information as a
    single `action` mapping. Validate them here so the AP matrix still refuses a
    case that misspells or omits one."""
    for case in load_contract_cases("air_purifier.json"):
        assert isinstance(case.get("command_name"), str), case["id"]
        assert isinstance(case.get("values"), dict), case["id"]
        assert case["action"] == case["id"], case["id"]


def test_every_case_declares_the_shared_ap_schema() -> None:
    """The shorthand device fixture and the per-case command schemas describe
    the same appliance; without this they are two copies nothing keeps in sync."""
    expected = _normalize_shorthand(_ap_schema()["commands"])
    for case in load_contract_cases("air_purifier.json"):
        assert _simplify(case["schema"]) == expected, case["id"]


def test_payloads_only_use_declared_fields_and_values() -> None:
    """Global constraint: never send a field or value absent from the active
    command schema. The matrix is the source of truth for every AP entity, so
    an off-schema payload has to fail here and not eleven tasks later."""
    for case in load_contract_cases("air_purifier.json"):
        command_name = case["command_name"]
        assert command_name in case["schema"], case["id"]
        parameters = case["schema"][command_name]["parameters"]
        for key, value in case["expected_payload"].items():
            assert key in parameters, f"{case['id']}: undeclared {key}"
            declared = parameters[key]
            match declared["typology"]:
                case "enum":
                    assert value in declared["enumValues"], f"{case['id']}: {key}={value}"
                case "fixed":
                    assert value == declared["fixedValue"], f"{case['id']}: {key}={value}"
                case "range":
                    low = float(declared["minimumValue"])
                    high = float(declared["maximumValue"])
                    assert low <= float(value) <= high, f"{case['id']}: {key}={value}"


def test_payload_is_the_intent_plus_the_schema_mandatory_keys() -> None:
    """An intent carries only what the user asked for; mandatory keys come from
    the schema. The disjointness check is separate on purpose: merging the two
    would hide an intent that duplicates a mandatory key at its schema value."""
    for case in load_contract_cases("air_purifier.json"):
        parameters = case["schema"][case["command_name"]]["parameters"]
        mandatory = _mandatory_payload(parameters)
        assert not set(case["values"]) & set(mandatory), case["id"]
        assert case["expected_payload"] == {**case["values"], **mandatory}, case["id"]


def test_shadow_deltas_are_real_changes() -> None:
    """A delta equal to the initial value proves nothing: the case would still
    pass with the write silently dropped."""
    for case in load_contract_cases("air_purifier.json"):
        for key, value in case["expected_shadow_delta"].items():
            assert case["initial_shadow"].get(key) != value, f"{case['id']}: {key}"


def test_protected_keys_partition_the_shadow() -> None:
    """Every shadow key is either written or protected, never both and never
    neither, so no field can escape the matrix unobserved."""
    for case in load_contract_cases("air_purifier.json"):
        protected = set(case["must_not_change"])
        written = set(case["expected_payload"])
        assert not protected & written, case["id"]
        assert protected | written == set(case["initial_shadow"]), case["id"]
