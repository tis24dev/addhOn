# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest

from tests._golden import install_stubs

install_stubs()

from custom_components.addhon import air_purifier
from custom_components.addhon.client.engine.appliance import HonAppliance
from custom_components.addhon.client.engine.attributes import HonAttribute
from custom_components.addhon.client.engine.commands import HonCommand
from custom_components.addhon.client.engine.exceptions import ApiError
from custom_components.addhon.command_dispatch import CommandDispatcher, CommandPatch
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


# --- Task 2: mappings, capabilities, intents ---------------------------------

_INTENT_FOR_CASE = {
    "turn_on_auto": ("turn_on", {"value": "2"}),
    "turn_off": ("turn_off", {}),
    "preset_sleep": ("set_preset", {"value": "1"}),
    "preset_auto": ("set_preset", {"value": "2"}),
    "preset_max": ("set_preset", {"value": "4"}),
    "light_off": ("set_light", {"value": "0"}),
    "light_50": ("set_light", {"value": "1"}),
    "light_100": ("set_light", {"value": "2"}),
    "lock_on": ("set_lock", {"value": "1"}),
    "lock_off": ("set_lock", {"value": "0"}),
    "tone_on": ("set_tone", {"value": "1"}),
    "tone_off": ("set_tone", {"value": "0"}),
    "aroma_off": ("set_aroma", {"value": "0"}),
    "aroma_soft": ("set_aroma", {"value": "1"}),
    "aroma_mid": ("set_aroma", {"value": "2"}),
    "aroma_h_biotics": ("set_aroma", {"value": "3"}),
    "aroma_custom": ("set_aroma", {"value": "4", "time_on": "30", "time_off": "90"}),
}


class _FakeAppliance:
    """Minimal appliance surface HonCommand needs. Deliberately carries a model
    and a nickname so a capability read that peeks at either is provable."""

    def __init__(self) -> None:
        self.zone = 0
        self.options: dict[str, str] = {}
        self.commands: dict[str, Any] = {}
        self.appliance_type = "AP"
        # Synthetic on purpose: the trap only needs the ATTRIBUTES to exist so a
        # capability read that peeks at either is provable. A real model code in
        # tracked test code would also be evidence leaking out of the gitignored
        # analysis material.
        self.model_name = "SYNTHETIC-MODEL-NEVER-READ"
        self.nick_name = "Purifier"


def _appliance_from_schema(schema: dict[str, Any]) -> _FakeAppliance:
    """Real HonCommand objects from a contract case's schema, so capability
    discovery is exercised against the repository engine's parameter classes
    rather than permissive hand-written fakes."""
    appliance = _FakeAppliance()
    for command_name, body in schema.items():
        appliance.commands[command_name] = HonCommand(
            command_name, deepcopy(body), appliance
        )
    return appliance


def _ap_case(case_id: str = "preset_auto") -> dict[str, Any]:
    return next(
        case
        for case in load_contract_cases("air_purifier.json")
        if case["id"] == case_id
    )


def _full_capabilities():
    case = _ap_case()
    appliance = _appliance_from_schema(case["schema"])
    return air_purifier.discover_capabilities(appliance, case["initial_shadow"])


def test_ap_mappings_are_exact() -> None:
    assert air_purifier.AP_MODE_TO_PRESET == {"1": "sleep", "2": "auto", "4": "max"}
    assert air_purifier.AP_PRESET_TO_MODE == {"sleep": "1", "auto": "2", "max": "4"}
    assert air_purifier.AP_LIGHT_TO_OPTION == {"0": "off", "1": "low", "2": "high"}
    assert air_purifier.AP_OPTION_TO_LIGHT == {"off": "0", "low": "1", "high": "2"}
    assert air_purifier.AP_LIGHT_OPTION_ORDER == ("off", "low", "high")
    assert air_purifier.AP_AROMA_TO_OPTION == {
        "0": "off", "1": "soft", "2": "mid", "3": "h_biotics", "4": "custom",
    }
    assert air_purifier.AP_OPTION_TO_AROMA == {
        "off": "0", "soft": "1", "mid": "2", "h_biotics": "3", "custom": "4",
    }


def test_ap_mappings_round_trip() -> None:
    for raw, preset in air_purifier.AP_MODE_TO_PRESET.items():
        assert air_purifier.AP_PRESET_TO_MODE[preset] == raw
    for raw, option in air_purifier.AP_LIGHT_TO_OPTION.items():
        assert air_purifier.AP_OPTION_TO_LIGHT[option] == raw
    assert set(air_purifier.AP_LIGHT_OPTION_ORDER) == set(
        air_purifier.AP_LIGHT_TO_OPTION.values()
    )
    for raw, option in air_purifier.AP_AROMA_TO_OPTION.items():
        assert air_purifier.AP_OPTION_TO_AROMA[option] == raw


def test_filter_remaining_inverts_and_clamps() -> None:
    assert air_purifier.filter_remaining("34") == 66
    assert air_purifier.filter_remaining("150") == 0
    assert air_purifier.filter_remaining("0") == 100
    assert air_purifier.filter_remaining(0) == 100
    assert air_purifier.filter_remaining("-20") == 100
    assert air_purifier.filter_remaining(None) is None
    assert air_purifier.filter_remaining("") is None
    assert air_purifier.filter_remaining("not-a-number") is None


def test_normalize_error_folds_every_normal_representation() -> None:
    for normal in (0, "0", "00", "000", 100, "100", None, "", "  "):
        assert air_purifier.normalize_error(normal) == "0", normal
    assert air_purifier.normalize_error("E12") == "E12"
    assert air_purifier.normalize_error(" E12 ") == "E12"
    assert air_purifier.normalize_error("1000") == "1000"
    assert air_purifier.normalize_error(7) == "7"


def test_has_problem_tracks_normalized_error() -> None:
    assert air_purifier.has_problem("E12") is True
    assert air_purifier.has_problem("1000") is True
    for normal in (0, "0", "00", 100, "100", None, ""):
        assert air_purifier.has_problem(normal) is False, normal


def test_environment_available_requires_confirmed_power() -> None:
    assert air_purifier.environment_available({"onOffStatus": "0"}) is False
    assert air_purifier.environment_available({"onOffStatus": "1"}) is True
    assert air_purifier.environment_available({"onOffStatus": 1}) is True
    assert air_purifier.environment_available({}) is False
    assert air_purifier.environment_available({"onOffStatus": None}) is False


def test_capabilities_from_the_full_schema_enable_every_feature() -> None:
    caps = _full_capabilities()

    assert caps.supports_fan is True
    assert caps.supports_light is True
    assert caps.supports_lock is True
    assert caps.supports_tone is True
    assert caps.supports_aroma is True
    assert caps.supports_custom_aroma is True
    assert caps.settings_command == "settings"
    assert caps.start_modes == frozenset({"1", "2", "4"})
    assert caps.preset_modes == frozenset({"1", "2", "4"})
    assert caps.aroma_options == ("off", "soft", "mid", "h_biotics", "custom")


def test_capability_discovery_discards_the_off_and_allergen_modes() -> None:
    """machMode 0 is a read-only off sentinel and 3 is undeclared on the observed
    devices; neither may ever become a writable preset."""
    case = _ap_case()
    schema = deepcopy(case["schema"])
    for command in ("startProgram", "settings"):
        schema[command]["parameters"]["machMode"]["enumValues"] = [
            "0", "1", "2", "3", "4",
        ]
    caps = air_purifier.discover_capabilities(
        _appliance_from_schema(schema), case["initial_shadow"]
    )

    assert caps.start_modes == frozenset({"1", "2", "4"})
    assert caps.preset_modes == frozenset({"1", "2", "4"})


@pytest.mark.parametrize(
    ("drop", "disabled", "still_enabled"),
    [
        (("settings", "lightStatus"), "supports_light",
         ("supports_fan", "supports_lock", "supports_tone", "supports_aroma")),
        (("settings", "lockStatus"), "supports_lock",
         ("supports_fan", "supports_light", "supports_tone", "supports_aroma")),
        (("settings", "touchToneStatus"), "supports_tone",
         ("supports_fan", "supports_light", "supports_lock", "supports_aroma")),
        (("settings", "aromaStatus"), "supports_aroma",
         ("supports_fan", "supports_light", "supports_lock", "supports_tone")),
        (("settings", "aromaTimeOn"), "supports_custom_aroma",
         ("supports_fan", "supports_light", "supports_aroma")),
        (("settings", "aromaTimeOff"), "supports_custom_aroma",
         ("supports_fan", "supports_light", "supports_aroma")),
        (("startProgram", "machMode"), "supports_fan",
         ("supports_light", "supports_lock", "supports_tone", "supports_aroma")),
    ],
)
def test_a_missing_capability_disables_only_its_own_feature(
    drop: tuple[str, str], disabled: str, still_enabled: tuple[str, ...]
) -> None:
    case = _ap_case()
    schema = deepcopy(case["schema"])
    command_name, param_name = drop
    del schema[command_name]["parameters"][param_name]
    caps = air_purifier.discover_capabilities(
        _appliance_from_schema(schema), case["initial_shadow"]
    )

    assert getattr(caps, disabled) is False
    for feature in still_enabled:
        assert getattr(caps, feature) is True, feature


def test_dropping_a_whole_command_disables_only_its_writes() -> None:
    case = _ap_case()
    schema = deepcopy(case["schema"])
    del schema["stopProgram"]
    caps = air_purifier.discover_capabilities(
        _appliance_from_schema(schema), case["initial_shadow"]
    )

    assert caps.supports_fan is False
    assert caps.supports_light is True
    assert caps.supports_aroma is True


def test_custom_aroma_needs_both_timing_ranges() -> None:
    """Status 4 alone cannot complete the Custom contract, so the option is
    withheld rather than sent without its timing fields."""
    case = _ap_case()
    schema = deepcopy(case["schema"])
    del schema["settings"]["parameters"]["aromaTimeOff"]
    caps = air_purifier.discover_capabilities(
        _appliance_from_schema(schema), case["initial_shadow"]
    )

    assert caps.supports_aroma is True
    assert caps.supports_custom_aroma is False
    assert caps.aroma_options == ("off", "soft", "mid", "h_biotics")


def test_every_contract_action_yields_its_exact_patch() -> None:
    for case in load_contract_cases("air_purifier.json"):
        appliance = _appliance_from_schema(case["schema"])
        caps = air_purifier.discover_capabilities(appliance, case["initial_shadow"])
        action, kwargs = _INTENT_FOR_CASE[case["id"]]
        patch = air_purifier.ap_patch(action, caps, **kwargs)

        assert patch.command_name == case["command_name"], case["id"]
        assert dict(patch.values) == case["values"], case["id"]
        assert patch.action == f"ap_{action}", case["id"]


def test_ap_patch_rejects_unsupported_actions_and_values() -> None:
    caps = _full_capabilities()

    with pytest.raises(ValueError):
        air_purifier.ap_patch("explode", caps)
    for forbidden in ("0", "3", "9"):
        with pytest.raises(ValueError):
            air_purifier.ap_patch("turn_on", caps, value=forbidden)
        with pytest.raises(ValueError):
            air_purifier.ap_patch("set_preset", caps, value=forbidden)
    with pytest.raises(ValueError):
        air_purifier.ap_patch("set_light", caps, value="3")
    with pytest.raises(ValueError):
        air_purifier.ap_patch("set_lock", caps, value="2")
    with pytest.raises(ValueError):
        air_purifier.ap_patch("set_aroma", caps, value="5")
    with pytest.raises(ValueError):  # custom without its timing values
        air_purifier.ap_patch("set_aroma", caps, value="4")
    with pytest.raises(ValueError):  # timing outside the live range
        air_purifier.ap_patch(
            "set_aroma", caps, value="4", time_on="0", time_off="60"
        )
    with pytest.raises(ValueError):
        air_purifier.ap_patch(
            "set_aroma", caps, value="4", time_on="60", time_off="99999"
        )


def test_ap_patch_refuses_an_unsupported_feature() -> None:
    case = _ap_case()
    schema = deepcopy(case["schema"])
    del schema["settings"]["parameters"]["lightStatus"]
    caps = air_purifier.discover_capabilities(
        _appliance_from_schema(schema), case["initial_shadow"]
    )

    with pytest.raises(ValueError):
        air_purifier.ap_patch("set_light", caps, value="0")


def test_air_purifier_never_reads_a_model_or_nickname() -> None:
    """Capability gating must come from the live schema only: no model-name
    exception is permitted anywhere in the AP module.

    Checked over identifiers and string literals rather than raw text, so the
    prose in a docstring cannot trip it and, more importantly, cannot mask a
    real `appliance.model_name` read either.
    """
    source = (
        Path(__file__).parents[1]
        / "custom_components" / "addhon" / "air_purifier.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }

    tokens: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.append(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.append(node.attr)
        elif isinstance(node, ast.arg):
            tokens.append(node.arg)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            tokens.append(node.value)

    normalized = {"".join(c for c in token.lower() if c.isalnum()) for token in tokens}
    for forbidden in ("hhp", "model", "nick", "serial", "macaddress"):
        offenders = sorted(token for token in normalized if forbidden in token)
        assert offenders == [], f"{forbidden}: {offenders}"


def test_every_declared_platform_has_a_module() -> None:
    """PLATFORMS drives async_forward_entry_setups, which imports each name as a
    module. The suite stubs Home Assistant and never performs that import, so a
    platform listed before its module exists stays green here and fails only on
    a real instance."""
    from custom_components.addhon.const import PLATFORMS

    component_root = Path(__file__).parents[1] / "custom_components" / "addhon"
    missing = [
        name for name in PLATFORMS if not (component_root / f"{name}.py").exists()
    ]
    assert missing == []


def test_normalize_error_handles_an_already_numeric_zero() -> None:
    """The client may unwrap an attribute into a real float. Parsing with int()
    would raise on "0.0" and promote a healthy purifier to a permanent problem."""
    assert air_purifier.normalize_error(0.0) == "0"
    assert air_purifier.normalize_error(100.0) == "0"
    assert air_purifier.has_problem(0.0) is False
    assert air_purifier.has_problem(100.0) is False
    assert air_purifier.normalize_error(12.5) == "12.5"
    assert air_purifier.has_problem(12.5) is True


def test_capabilities_read_attributes_wrapped_as_hon_attribute() -> None:
    """Attributes arrive either already unwrapped or as HonAttribute; capability
    discovery and the availability gate must agree on both shapes."""
    from custom_components.addhon.client.engine.attributes import HonAttribute

    case = _ap_case()
    wrapped = {
        key: HonAttribute(value) for key, value in case["initial_shadow"].items()
    }
    caps = air_purifier.discover_capabilities(
        _appliance_from_schema(case["schema"]), wrapped
    )

    assert caps.has_power_state is True
    assert caps.has_mode_state is True
    assert caps.supports_fan is True
    assert air_purifier.environment_available(wrapped) is True
    assert air_purifier.environment_available(
        {"onOffStatus": HonAttribute("0")}
    ) is False


def test_environment_available_accepts_a_boolean_power_state() -> None:
    assert air_purifier.environment_available({"onOffStatus": True}) is True
    assert air_purifier.environment_available({"onOffStatus": False}) is False


def test_presets_are_writable_from_either_purifier_state() -> None:
    """A preset is written through startProgram while off and through settings
    while on. A mode only one of them declares would be offered and then
    rejected depending on when the user picked it, so it is not offered."""
    case = _ap_case()
    schema = deepcopy(case["schema"])
    schema["settings"]["parameters"]["machMode"]["enumValues"] = ["1", "2"]
    caps = air_purifier.discover_capabilities(
        _appliance_from_schema(schema), case["initial_shadow"]
    )

    assert caps.start_modes == frozenset({"1", "2", "4"})
    assert caps.preset_modes == frozenset({"1", "2"})
    assert caps.writable_modes == frozenset({"1", "2"})
    assert caps.preset_options == ("sleep", "auto")
    with pytest.raises(ValueError):
        air_purifier.ap_patch("set_preset", caps, value="4")


def test_capability_discovery_discards_an_injected_default_mode() -> None:
    """HonParameterEnum appends defaultValue to its values even when enumValues
    omits it, so a schema defaulting to the allergen mode would smuggle 3 into
    the writable set if the intersection did not filter it out."""
    case = _ap_case()
    schema = deepcopy(case["schema"])
    for command in ("startProgram", "settings"):
        schema[command]["parameters"]["machMode"]["defaultValue"] = "3"
    appliance = _appliance_from_schema(schema)

    assert "3" in appliance.commands["settings"].parameters["machMode"].values
    caps = air_purifier.discover_capabilities(appliance, case["initial_shadow"])
    assert caps.writable_modes == frozenset({"1", "2", "4"})
    with pytest.raises(ValueError):
        air_purifier.ap_patch("set_preset", caps, value="3")


def test_fan_needs_a_mode_writable_from_both_commands() -> None:
    case = _ap_case()
    schema = deepcopy(case["schema"])
    for command, values in (("startProgram", ["1"]), ("settings", ["2"])):
        schema[command]["parameters"]["machMode"]["enumValues"] = values
        schema[command]["parameters"]["machMode"]["defaultValue"] = values[0]
    caps = air_purifier.discover_capabilities(
        _appliance_from_schema(schema), case["initial_shadow"]
    )

    assert caps.writable_modes == frozenset()
    assert caps.supports_fan is False
    assert caps.supports_light is True


@pytest.mark.parametrize(
    ("param", "values", "feature"),
    [
        ("lightStatus", ["0", "1"], "supports_light"),
        ("lightStatus", ["0", "1", "2", "3"], "supports_light"),
        ("lightStatus", ["1", "2", "3"], "supports_light"),
        ("lockStatus", ["0", "1", "2"], "supports_lock"),
        ("touchToneStatus", ["1"], "supports_tone"),
    ],
)
def test_a_differently_shaped_enum_disables_its_feature(
    param: str, values: list[str], feature: str
) -> None:
    """A schema declaring a different value set is a different device behavior.
    Reusing the observed mapping against it would send a value the device never
    declared, so the feature is dropped instead of guessed at."""
    case = _ap_case()
    schema = deepcopy(case["schema"])
    schema["settings"]["parameters"][param]["enumValues"] = values
    schema["settings"]["parameters"][param]["defaultValue"] = values[0]
    caps = air_purifier.discover_capabilities(
        _appliance_from_schema(schema), case["initial_shadow"]
    )

    assert getattr(caps, feature) is False
    assert caps.supports_aroma is True


def test_set_aroma_time_carries_only_the_changed_timing_key() -> None:
    """Task 9's contract: the changed-time patch is `aromaStatus=4` plus ONLY
    the timing field the user moved. Sending the untouched sibling too would
    overwrite it with whatever stale value the entity happened to be holding."""
    caps = _full_capabilities()

    on_only = air_purifier.ap_patch("set_aroma_time", caps, time_on="30")
    assert on_only.command_name == "settings"
    assert on_only.action == "ap_set_aroma_time"
    assert dict(on_only.values) == {"aromaStatus": "4", "aromaTimeOn": "30"}
    assert "aromaTimeOff" not in on_only.values

    off_only = air_purifier.ap_patch("set_aroma_time", caps, time_off="90")
    assert dict(off_only.values) == {"aromaStatus": "4", "aromaTimeOff": "90"}
    assert "aromaTimeOn" not in off_only.values

    both = air_purifier.ap_patch("set_aroma_time", caps, time_on="30", time_off="90")
    assert dict(both.values) == {
        "aromaStatus": "4", "aromaTimeOn": "30", "aromaTimeOff": "90",
    }


def test_set_aroma_time_rejects_what_the_schema_refuses() -> None:
    caps = _full_capabilities()

    with pytest.raises(ValueError):  # no timing at all is not a change
        air_purifier.ap_patch("set_aroma_time", caps)
    for bad in ("0", "3601", "not-a-number"):
        with pytest.raises(ValueError):
            air_purifier.ap_patch("set_aroma_time", caps, time_on=bad)
        with pytest.raises(ValueError):
            air_purifier.ap_patch("set_aroma_time", caps, time_off=bad)


def test_set_aroma_time_needs_the_custom_capability() -> None:
    case = _ap_case()
    schema = deepcopy(case["schema"])
    del schema["settings"]["parameters"]["aromaTimeOff"]
    caps = air_purifier.discover_capabilities(
        _appliance_from_schema(schema), case["initial_shadow"]
    )

    assert caps.supports_custom_aroma is False
    with pytest.raises(ValueError):
        air_purifier.ap_patch("set_aroma_time", caps, time_on="30")


def test_set_aroma_time_only_writes_declared_settings_fields() -> None:
    """Ties the new action back to the global constraint: every key it emits must
    exist in the live settings schema."""
    case = _ap_case()
    declared = set(case["schema"]["settings"]["parameters"])
    caps = _full_capabilities()

    patch = air_purifier.ap_patch(
        "set_aroma_time", caps, time_on="30", time_off="90"
    )
    assert set(patch.values) <= declared


# --- Task 12: the matrix through the REAL dispatcher --------------------------
#
# Everything below drives `CommandDispatcher.dispatch` against real `HonCommand`
# objects, the engine's real parameter setters and rule application, the real
# appliance shadow-sync method, and a fake TRANSPORT. Only the network boundary is
# replaced, so a payload that the engine would refuse never reaches a test
# assertion, and the transactional guarantees (rollback, serialization) are
# exercised rather than described.


class _RecordingApi:
    """The network boundary, and nothing else.

    `HonCommand._send_parameters` raises ApiError when the api returns a falsy
    result, so a cloud rejection reaches the dispatcher as an exception, not as a
    False return: `result=False` here reproduces a real rejection.
    """

    def __init__(self, result: bool = True, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result = result
        self.error = error

    async def send_command(
        self,
        appliance: Any,
        command_name: str,
        params: Any,
        ancillary: Any,
        category: str,
    ) -> bool:
        self.calls.append((command_name, {k: str(v) for k, v in params.items()}))
        if self.error is not None:
            raise self.error
        return self.result


class _DispatchAppliance(_FakeAppliance):
    """Appliance surface the dispatcher touches: commands, a shadow, and the
    engine's OWN `sync_payload_to_params` (bound from `HonAppliance`, not
    reimplemented, so the reconciliation under test is the real one)."""

    sync_payload_to_params = HonAppliance.sync_payload_to_params

    def __init__(self, shadow: dict[str, str], api: _RecordingApi) -> None:
        super().__init__()
        self.api = api
        self.attributes = {
            "parameters": {key: HonAttribute(value) for key, value in shadow.items()}
        }


def _dispatch_appliance(
    case: dict[str, Any],
    *,
    api: _RecordingApi | None = None,
    schema: dict[str, Any] | None = None,
) -> tuple[_DispatchAppliance, _RecordingApi]:
    transport = api if api is not None else _RecordingApi()
    appliance = _DispatchAppliance(case["initial_shadow"], transport)
    for command_name, body in deepcopy(schema or case["schema"]).items():
        appliance.commands[command_name] = HonCommand(command_name, body, appliance)
    return appliance, transport


def _plain_shadow(appliance: _DispatchAppliance) -> dict[str, str]:
    return {
        key: str(attribute.value)
        for key, attribute in appliance.attributes["parameters"].items()
    }


def _capabilities_for(appliance: _DispatchAppliance, case: dict[str, Any]):
    return air_purifier.discover_capabilities(appliance, case["initial_shadow"])


def _patch_for(case: dict[str, Any], appliance: _DispatchAppliance):
    action, kwargs = _INTENT_FOR_CASE[case["id"]]
    return air_purifier.ap_patch(action, _capabilities_for(appliance, case), **kwargs)


def _command_values(appliance: _DispatchAppliance, command_name: str) -> dict[str, str]:
    command = appliance.commands[command_name]
    return {
        name: str(parameter.value) for name, parameter in command.parameters.items()
    }


@pytest.mark.parametrize(
    "case", load_contract_cases("air_purifier.json"), ids=lambda case: case["id"]
)
def test_ap_contract_through_real_dispatcher(case: dict[str, Any]) -> None:
    appliance, api = _dispatch_appliance(case)
    before = _plain_shadow(appliance)

    result = asyncio.run(CommandDispatcher().dispatch(appliance, _patch_for(case, appliance)))

    assert result is True, case["id"]
    assert api.calls == [(case["command_name"], case["expected_payload"])], case["id"]
    after = _plain_shadow(appliance)
    delta = {key: value for key, value in after.items() if before[key] != value}
    assert delta == case["expected_shadow_delta"], case["id"]
    for key in case["must_not_change"]:
        assert after[key] == before[key], f"{case['id']}: {key}"


@pytest.mark.parametrize(
    "case", load_contract_cases("air_purifier.json"), ids=lambda case: case["id"]
)
def test_every_case_sends_exactly_one_command(case: dict[str, Any]) -> None:
    """One user action is one cloud call. A second call would mean the payload was
    split, or a sibling command was restated."""
    appliance, api = _dispatch_appliance(case)
    asyncio.run(CommandDispatcher().dispatch(appliance, _patch_for(case, appliance)))
    assert len(api.calls) == 1, case["id"]


def test_the_dispatcher_releases_its_lock_after_every_case() -> None:
    dispatcher = CommandDispatcher()
    for case in load_contract_cases("air_purifier.json"):
        appliance, _api = _dispatch_appliance(case)
        asyncio.run(dispatcher.dispatch(appliance, _patch_for(case, appliance)))
    assert dispatcher.lock_count == 0


# --- Negative matrix: the engine must REFUSE, and leave nothing behind --------


def _expect_refusal(
    case: dict[str, Any],
    patch: Any,
    expected: type[BaseException],
    *,
    api: _RecordingApi | None = None,
    schema: dict[str, Any] | None = None,
) -> None:
    """A refused write sends nothing AND leaves both the shadow and the command
    parameters exactly as they were: that is the transactional guarantee, and it is
    what a half-applied purifier setting would violate."""
    appliance, transport = _dispatch_appliance(case, api=api, schema=schema)
    shadow_before = _plain_shadow(appliance)
    params_before = {
        name: _command_values(appliance, name) for name in appliance.commands
    }

    with pytest.raises(expected):
        asyncio.run(CommandDispatcher().dispatch(appliance, patch))

    assert transport.calls == []
    assert _plain_shadow(appliance) == shadow_before
    assert {
        name: _command_values(appliance, name) for name in appliance.commands
    } == params_before


def test_an_off_schema_field_is_refused_before_any_send() -> None:
    case = _ap_case("light_off")
    patch = CommandPatch(
        "settings", {"bogusUndeclaredField": "1"}, action="ap_negative"
    )
    _expect_refusal(case, patch, ValueError)


def test_the_off_state_sentinel_mode_is_refused() -> None:
    """machMode=0 is the value the device REPORTS while stopped. `ap_patch` never
    builds it, so this goes straight at the engine: the enum setter must refuse it
    even when a caller hand-builds the patch."""
    case = _ap_case("preset_auto")
    patch = CommandPatch("settings", {"machMode": "0"}, action="ap_negative")
    _expect_refusal(case, patch, ValueError)


def test_the_allergen_mode_is_refused() -> None:
    case = _ap_case("preset_auto")
    patch = CommandPatch("settings", {"machMode": "3"}, action="ap_negative")
    _expect_refusal(case, patch, ValueError)


def test_an_undeclared_enum_value_is_refused() -> None:
    case = _ap_case("light_off")
    patch = CommandPatch("settings", {"lightStatus": "3"}, action="ap_negative")
    _expect_refusal(case, patch, ValueError)


def test_a_timing_outside_the_declared_range_is_refused() -> None:
    case = _ap_case("aroma_custom")
    patch = CommandPatch(
        "settings",
        {"aromaStatus": "4", "aromaTimeOn": "99999"},
        action="ap_negative",
    )
    _expect_refusal(case, patch, ValueError)


def test_a_missing_command_is_refused() -> None:
    case = _ap_case("turn_on_auto")
    schema = {
        name: body for name, body in case["schema"].items() if name != "startProgram"
    }
    patch = CommandPatch("startProgram", {"machMode": "2"}, action="ap_negative")
    _expect_refusal(case, patch, KeyError, schema=schema)


def test_custom_aroma_without_its_timings_never_reaches_the_transport() -> None:
    """The refusal happens in the intent builder, one layer before the dispatcher,
    which is why the assertion is that NOTHING was constructed to send."""
    case = _ap_case("aroma_custom")
    schema = deepcopy(case["schema"])
    del schema["settings"]["parameters"]["aromaTimeOff"]
    appliance, api = _dispatch_appliance(case, schema=schema)
    capabilities = air_purifier.discover_capabilities(
        appliance, case["initial_shadow"]
    )
    with pytest.raises(ValueError):
        air_purifier.ap_patch("set_aroma", capabilities, value="4", time_on="30")
    assert api.calls == []


def test_a_cloud_rejection_rolls_everything_back() -> None:
    case = _ap_case("light_100")
    appliance, api = _dispatch_appliance(case, api=_RecordingApi(result=False))
    shadow_before = _plain_shadow(appliance)
    params_before = _command_values(appliance, "settings")

    with pytest.raises(ApiError):
        asyncio.run(CommandDispatcher().dispatch(appliance, _patch_for(case, appliance)))

    # The call DID leave: rejection happens at the far end, unlike the refusals above.
    assert len(api.calls) == 1
    assert _plain_shadow(appliance) == shadow_before
    assert _command_values(appliance, "settings") == params_before


def test_a_transport_timeout_rolls_everything_back() -> None:
    case = _ap_case("preset_sleep")
    appliance, api = _dispatch_appliance(
        case, api=_RecordingApi(error=asyncio.TimeoutError())
    )
    shadow_before = _plain_shadow(appliance)
    params_before = _command_values(appliance, "settings")

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(CommandDispatcher().dispatch(appliance, _patch_for(case, appliance)))

    assert len(api.calls) == 1
    assert _plain_shadow(appliance) == shadow_before
    assert _command_values(appliance, "settings") == params_before


def test_concurrent_ap_actions_serialize_and_both_land() -> None:
    """Two purifier controls written at once (a light change and a lock change) must
    not interleave: each cloud payload carries its OWN field only, never the other
    control's. The settings command declares no mandatory parameter (ledger P4), so
    a sparse settings patch is exactly one key wide."""
    case = _ap_case("light_off")
    appliance, api = _dispatch_appliance(case)
    capabilities = _capabilities_for(appliance, case)
    dispatcher = CommandDispatcher()

    async def run() -> list[bool]:
        return list(
            await asyncio.gather(
                dispatcher.dispatch(
                    appliance, air_purifier.ap_patch("set_light", capabilities, value="1")
                ),
                dispatcher.dispatch(
                    appliance, air_purifier.ap_patch("set_lock", capabilities, value="1")
                ),
            )
        )

    assert asyncio.run(run()) == [True, True]
    assert len(api.calls) == 2
    payloads = {tuple(sorted(payload)) for _name, payload in api.calls}
    assert payloads == {("lightStatus",), ("lockStatus",)}
    shadow = _plain_shadow(appliance)
    assert shadow["lightStatus"] == "1"
    assert shadow["lockStatus"] == "1"
    assert dispatcher.lock_count == 0
