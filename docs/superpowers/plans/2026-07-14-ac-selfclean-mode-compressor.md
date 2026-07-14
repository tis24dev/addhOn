# AC self-clean vs mode compressor-off fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the AC integration from emitting a `settings` command that starts or resumes an operating mode while a self-clean flag is still `1`, which leaves the unit running the fan with the compressor off.

**Architecture:** Add a capability-gated helper in `ac_command.py` that augments a settings params dict with `selfCleaningStatus="0"` / `selfCleaning56Status="0"` for the flags the device exposes. Wrap the two mode-establishing sends in `climate.py` (active-mode `async_set_hvac_mode`, settings-based `async_turn_on`) with it. This mirrors the Haier app's `startProgram` payload, which always carries self-clean = 0 when starting a normal mode.

**Tech Stack:** Python, Home Assistant custom integration, unittest + pytest test harness.

## Global Constraints

- Scope is the settings-based AC write path only. Program-based ACs already route through `startProgram` and are untouched.
- The helper must be capability-gated: it injects a flag ONLY if `settings_param(appliance, name)` is not None. A device without the flag gets no injected key (an unknown param would make `async_send_command` raise "Parameter(s) not found").
- Do NOT clear self-clean on OFF, `set_temperature`, `set_fan_mode`, or `set_swing_mode`. Only starting/resuming an active mode clears it.
- Assertions read `RecordingCommand.sent` (the payload frozen at `send()`), which snapshots ALL params on the settings command, not only the ones passed in.
- Code and comments in English. No em/en dashes in code or comments. Conventional-commit messages. No `Co-Authored-By: Claude` trailer. Commit locally, do not push (push on explicit request only).
- Run tests one at a time, no parallel/xdist, and read the real exit code.

---

### Task 1: `with_self_clean_off` helper

**Files:**
- Modify: `custom_components/addhon/ac_command.py` (add helper + constant near `settings_param`)
- Test: `tests/test_ac_write_path.py` (add helper unit tests)

**Interfaces:**
- Consumes: `settings_param(appliance, name)` (already in `ac_command.py`).
- Produces: `AC_SELF_CLEAN_PARAMS: tuple[str, ...]` and `with_self_clean_off(appliance, params: dict) -> dict` (returns a NEW dict; never mutates the input).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ac_write_path.py` (new test class; `types` and `ac_command`, `Param`, `RecordingCommand` are already imported at module top):

```python
class WithSelfCleanOffTest(unittest.TestCase):
    def _ac_settings(self, params: dict):
        return types.SimpleNamespace(commands={"settings": RecordingCommand(params)})

    def test_injects_zero_for_exposed_flags(self) -> None:
        appliance = self._ac_settings(
            {
                "onOffStatus": Param("0"),
                "machMode": Param("0"),
                "selfCleaningStatus": Param("1"),
                "selfCleaning56Status": Param("1"),
            }
        )
        out = ac_command.with_self_clean_off(
            appliance, {"onOffStatus": "1", "machMode": "1"}
        )
        self.assertEqual(
            {
                "onOffStatus": "1",
                "machMode": "1",
                "selfCleaningStatus": "0",
                "selfCleaning56Status": "0",
            },
            out,
        )

    def test_skips_flags_the_device_does_not_expose(self) -> None:
        appliance = self._ac_settings(
            {"onOffStatus": Param("0"), "machMode": Param("0")}
        )
        out = ac_command.with_self_clean_off(
            appliance, {"onOffStatus": "1", "machMode": "1"}
        )
        self.assertEqual({"onOffStatus": "1", "machMode": "1"}, out)

    def test_does_not_override_caller_supplied_value(self) -> None:
        appliance = self._ac_settings({"selfCleaningStatus": Param("0")})
        out = ac_command.with_self_clean_off(appliance, {"selfCleaningStatus": "1"})
        self.assertEqual("1", out["selfCleaningStatus"])

    def test_does_not_mutate_input(self) -> None:
        appliance = self._ac_settings({"selfCleaningStatus": Param("1")})
        params = {"onOffStatus": "1"}
        ac_command.with_self_clean_off(appliance, params)
        self.assertEqual({"onOffStatus": "1"}, params)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_ac_write_path.py -k WithSelfCleanOff -p no:randomly`
Expected: FAIL with `AttributeError: module 'custom_components.addhon.ac_command' has no attribute 'with_self_clean_off'`.

- [ ] **Step 3: Implement the helper**

In `custom_components/addhon/ac_command.py`, after the `settings_param` function, add:

```python
# Self-clean flags. On the settings write path these must be 0 whenever HA starts or
# resumes an operating mode: the Haier app's startProgram payload always carries
# selfCleaningStatus=0 for a normal mode, and on single-split (1to1) installs the
# fixed-0 programRule is inert, so a mode command could otherwise ship machMode together
# with a cached selfCleaningStatus=1 and the unit runs the fan with the compressor off.
AC_SELF_CLEAN_PARAMS = ("selfCleaningStatus", "selfCleaning56Status")


def with_self_clean_off(appliance, params: dict) -> dict:
    """Return a copy of `params` with the self-clean flags forced to '0', for the flags
    the device exposes on its settings command. Capability-gated (an absent flag is not
    injected). setdefault so a caller that set the flag itself is never overridden."""
    out = dict(params)
    for name in AC_SELF_CLEAN_PARAMS:
        if settings_param(appliance, name) is not None:
            out.setdefault(name, "0")
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_ac_write_path.py -k WithSelfCleanOff -p no:randomly`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add custom_components/addhon/ac_command.py tests/test_ac_write_path.py
git commit -m "fix(ac): add with_self_clean_off settings helper"
```

---

### Task 2: Clear self-clean when starting or resuming a mode

**Files:**
- Modify: `custom_components/addhon/climate.py` (import + two call sites)
- Test: `tests/test_ac_write_path.py` (behavioral + regression tests)

**Interfaces:**
- Consumes: `with_self_clean_off(appliance, params)` from Task 1; existing `_send_command_in_executor`, `_climate`, `RecordingCommand`, `Param`, `FakeClient`, `FakeHass`, `FakeCoordinator`, `_ac`, `switch`.
- Produces: no new public symbol.

- [ ] **Step 1: Write the failing tests**

Add to the AC write-path test class in `tests/test_ac_write_path.py` (same class as `test_set_hvac_mode_maps_each_mode`):

```python
    async def test_set_hvac_mode_active_clears_self_clean(self) -> None:
        entity, settings, _ = _climate(
            {
                "onOffStatus": Param("0"),
                "machMode": Param("0"),
                "selfCleaningStatus": Param("1"),
                "selfCleaning56Status": Param("1"),
            }
        )
        await entity.async_set_hvac_mode(HVACMode.COOL)
        self.assertEqual("1", settings.sent["onOffStatus"])
        self.assertEqual("1", settings.sent["machMode"])
        self.assertEqual("0", settings.sent["selfCleaningStatus"])
        self.assertEqual("0", settings.sent["selfCleaning56Status"])

    async def test_turn_on_clears_self_clean(self) -> None:
        entity, settings, _ = _climate(
            {
                "onOffStatus": Param("0"),
                "machMode": Param("4"),
                "selfCleaningStatus": Param("1"),
            }
        )
        await entity.async_turn_on()
        self.assertEqual("1", settings.sent["onOffStatus"])
        self.assertEqual("0", settings.sent["selfCleaningStatus"])

    async def test_set_temperature_does_not_clear_self_clean(self) -> None:
        entity, settings, _ = _climate(
            {"tempSel": Param("16"), "selfCleaningStatus": Param("1")}
        )
        await entity.async_set_temperature(temperature=22)
        self.assertEqual("1", settings.sent["selfCleaningStatus"])

    async def test_self_clean_switch_turn_on_still_sets_one(self) -> None:
        settings = RecordingCommand({"selfCleaningStatus": Param("0")})
        coordinator = FakeCoordinator(_ac({"settings": settings}))
        desc = switch.HonAcSwitchDescription(
            key="self_clean", param="selfCleaningStatus"
        )
        sw = switch.HonAcSwitch(coordinator, "ac-1", desc, FakeClient())
        sw.hass = FakeHass()
        await sw.async_turn_on()
        self.assertEqual("1", settings.sent["selfCleaningStatus"])
```

- [ ] **Step 2: Run the tests to verify the new mode/turn_on ones fail**

Run: `python -m pytest tests/test_ac_write_path.py -k "active_clears_self_clean or turn_on_clears_self_clean" -p no:randomly`
Expected: FAIL. `sent["selfCleaningStatus"]` is `"1"` (the mode command carries the stale flag) instead of `"0"`.

Note: `test_set_temperature_does_not_clear_self_clean` and `test_self_clean_switch_turn_on_still_sets_one` should already PASS before the change (they assert unchanged behavior). Run them too to confirm the change does not regress them in Step 5.

- [ ] **Step 3: Wire the helper into `climate.py`**

3a. Add `with_self_clean_off` to the `ac_command` import block (currently `async_send_settings, fixed_vertical_value, param_allowed_values, settings_param`):

```python
from .ac_command import (
    async_send_settings,
    fixed_vertical_value,
    param_allowed_values,
    settings_param,
    with_self_clean_off,
)
```

3b. Active-mode branch of `async_set_hvac_mode` (the `else` that sends `{"onOffStatus": "1", "machMode": str(mode_key)}`):

```python
                await self._send_command_in_executor(
                    client,
                    appliance,
                    with_self_clean_off(
                        appliance, {"onOffStatus": "1", "machMode": str(mode_key)}
                    ),
                )
```

3c. Settings-based branch of `async_turn_on` (the `else` that sends `{"onOffStatus": "1"}`):

```python
                await self._send_command_in_executor(
                    client, appliance, with_self_clean_off(appliance, {"onOffStatus": "1"})
                )
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python -m pytest tests/test_ac_write_path.py -k "active_clears_self_clean or turn_on_clears_self_clean or set_temperature_does_not_clear or self_clean_switch_turn_on_still" -p no:randomly`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the full AC write-path file to verify no regression**

Run: `python -m pytest tests/test_ac_write_path.py -p no:randomly`
Expected: PASS. In particular `test_set_hvac_mode_maps_each_mode` and `test_turn_on_*` still pass unchanged, because their fixtures expose no self-clean param so the helper injects nothing.

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest tests/ -p no:randomly`
Expected: PASS (no new failures).

- [ ] **Step 7: Commit**

```bash
git add custom_components/addhon/climate.py tests/test_ac_write_path.py
git commit -m "fix(ac): clear self-clean flags when starting or resuming a mode"
```

---

## Post-implementation (not a code task)

Live-validate on Roberto's HA with debug ON: trigger self-clean, then set cool from HA, confirm `compressorStatus`/`compressorFrequency` show the compressor engaging (no fan-only). Push only on explicit request.

## Self-Review

- Spec coverage: helper (spec section "New helper") = Task 1. Two call sites + exclusions (spec "Call sites") = Task 2 steps 3b/3c + the temperature regression test. Testing section = Task 1 + Task 2 tests. Rollout = Post-implementation note. No gaps.
- Placeholder scan: none.
- Type consistency: `with_self_clean_off(appliance, params)` signature identical in Task 1 (definition) and Task 2 (call sites + import). `AC_SELF_CLEAN_PARAMS` used only where defined.
