# Air Purifier campaign: open items ledger

Living record of everything raised at the end of each task. Updated at every task
close. Nothing here is a blocker for the next task unless marked BLOCKED.

Legend: **BLOCKED** cannot proceed here / **LIVE** needs a real device /
**BACKLOG** should be done, no owner yet / **DEVIATION** plan diverged, on purpose /
**LIMIT** known and accepted / **DECIDE** needs a human call.

---

## LIVE: cannot be settled without a real HHP50/HHP55

The campaign's central weakness. Every test asserts internal coherence; none
proves the mapping matches hardware. Per the user's decision, resolution is a beta
release plus user-collected diagnostics, not Tasks 14/15 as written.

| # | Item | Consequence if wrong | Raised |
|---|---|---|---|
| L1 | `mainFilterStatus` / `preFilterStatus` assumed to be CONSUMED percentages | Every filter reading inverted (a fresh filter reads 0%). Highest-value single check on a real device. | Task 3 |
| L2 | `ecoModeStatus` taken from the decompiled app, not a live dump | If the firmware names it differently the eco entity silently never appears. Check beta diagnostics for an unmapped eco-like attribute. | Task 4 |
| L3 | `errors` is absent from `AP_PARAMS_ENUM` | Inconclusive (`machMode`/`onOffStatus` are also absent and are documented as shared cross-type params). If a real AP does not report it, the error sensor AND the problem binary both never appear. | Task 4 |
| L4 | `startProgram` accepted with only `machMode`; a running purifier accepts a `settings` mode change | Contract claims from the design. The "no write ever carries an unrelated field" assertion is what would break most visibly. | Task 5 |
| L5 | Light encoding assumed inverse (raw 2/1/0 = off/50/100) | Every light test still passes if raw `0` is actually OFF; the panel would simply behave backwards. Second highest-value live check after L1. | Task 6 |
| L6 | `touchToneStatus` polarity assumed (1 = tone audible). The decompiled enum lists the parameter but not its sense. | The switch reads and writes backwards; every test still passes. | Task 7 |
| L7 | Aroma ordinals assumed (1=soft, 2=mid, 3=h_biotics, 4=custom). From the design, not a live dump. | A permutation gives the wrong scent intensity for every selection; every test still passes. | Task 8 |

## BACKLOG: evidence says more than the design covers

Found by reading `apk/analysis/deep/climate-air.md:362` (`AP_PARAMS_ENUM`, from
`decomp.txt:1850910-1850958`). None of these were silently added: they fall
outside the frozen entity lists of Tasks 3-9.

| # | Item | Raised |
|---|---|---|
| B1 | `no2ValueIndoor` is declared by the app and listed as an AP read sensor, but the design's measurement section and Task 3's sensor list both omit it. Sensors are capability-gated, so adding it is harmless on a device that does not report it. Belongs in Task 10's capture at minimum. | Task 4 |
| B2 | `humidificationStatus` + `humiditySelHigh/Med/Low` declared. Deliberately out of scope: the design's non-goals exclude humidifier support where the capability is absent. Should surface in Task 10's passive capture, never as a guessed control. | Task 4 |
| B3 | `aromaPreferredSetting` declared, unmapped and undocumented. | Task 4 |
| B4 | `transMode` / `highTransRate` / `stdTransRate` declared, meaning unknown. | Task 4 |
| B5 | `test_all_tier2_descriptions_are_gated` (in `test_tier2_sensors.py`) iterates a hardcoded type list that does not include AP. AP gating is covered by `test_air_purifier_entities.py` instead, so the list was left alone. | Task 3 |

## DECIDE: needs a human call

| # | Item | Raised |
|---|---|---|
| D1 | Fan preset display casing. Home Assistant does not translate `fan` preset modes, so they render literally as `sleep` / `auto` / `max`. `AP_MODE_TO_PRESET` is pinned to those lowercase keys by Task 2's own assertion, so capitalizing would break the contract. Task 11 should decide whether display strings diverge from mapping keys. | Task 5 |
| D2 | `evidence: live_observation` on all 17 contract cases. The plan permits `live_observation` or `cloud_schema`, and the state mappings do come from the design's live evidence, but the payload-MINIMALITY rules are design decisions for which `derived_contract` would be the honest label. | Task 1 |

## DEVIATION: plan diverged, on purpose, each pinned by a test

| # | Item | Raised |
|---|---|---|
| V1 | `aroma_custom` fixture uses `aromaTimeOn=30` / `aromaTimeOff=90` instead of the plan's literal `60`/`60`. The AP shadow starts at 60/60, so the literals would make both timing entries no-op deltas and the case would pass with the timing write dropped or the two fields swapped. `test_shadow_deltas_are_real_changes` now makes 60/60 unwritable. | Task 1 |
| V2 | RESOLVED. `fan` and `light` were NOT added to `PLATFORMS` in Task 2 as Step 4 dictates; each landed in the commit that creates its module (`fan` Task 5, `light` Task 6), so every commit stays runnable on a real instance. Enforced by `test_every_declared_platform_has_a_module`, which exists because the stubbed suite stays green with a platform listed and no module present. | Tasks 2, 5, 6 |
| V3 | `ap_patch` gained an EIGHTH action, `set_aroma_time`, not in Task 2's Step 5 list. Task 9 needs `aromaStatus=4` plus only the changed timing key, and its file list excludes `air_purifier.py`; without this it would have had to assemble a CommandPatch inside `number.py`, bypassing the intent builder. Shape fully specified by Task 9's Step 6, so nothing was guessed. | Task 2 |
| V4 | `tests/test_command_dispatch.py` modified although listed in no task before Task 10. Its dormancy guard allow-lists dispatcher callers; this plan is what wakes the dispatcher. Now contains `air_purifier.py` (Task 2), `fan.py` (Task 5), `light.py` (Task 6) and `switch.py` (Task 7); each further AP platform adds itself. | Tasks 2, 5, 6, 7 |
| V8 | **MIXED-FILE allow-listing weakens the guard.** A platform file holding both an AP entity that dispatches and legacy entities that intentionally send whole commands cannot be protected by the file-level allow-list, and `_EXPECTED_LEGACY_CALL_EDGES` does not help because `async_send_settings` is not one of its tracked callees. Replaced by `test_mixed_platform_legacy_classes_keep_the_legacy_sender`, a per-CLASS check: `switch.py` (2 classes, Task 7) and `select.py` (2 classes, Task 8) are covered. **`number.py` (Task 9) becomes the third mixed file and MUST be added to that test.** | Tasks 7, 8 |
| V5 | `tests/test_entity_translation_keys.py` collectors hardcode BOTH the platform list and each platform's key sources, so a new platform, table or fixed-key entity goes unchecked (the `extra` half of the parity assertion only runs for known sources). `fan` Task 5, `light` Task 6, `_AIR_PURIFIER_SWITCHES` Task 7, `aroma` name + its `state` block Task 8. The state-block collector iterates DESCRIPTION TABLES, so the table-less aroma select had to register its option set explicitly or its five state labels would never have been parity-checked. Task 11 owns the file; every further AP key source must add itself. | Tasks 5, 6, 7, 8 |
| V6 | Translations are added by the task that introduces a key, not by Task 11. `test_code_keys_match_translations_exactly` asserts EXACT set equality per language, so a new key without JSON fails immediately. Task 11 becomes a completeness and wording pass, not the first appearance. | Task 3 |
| V7 | Task 3 wrote inline test stubs rather than centralizing in `conftest.py`, deferring to Task 5 which the plan says modifies conftest. Resolved in Task 5; `switch` added in Task 7. Each addition makes conftest win the first-wins race for modules that stubbed the platform themselves, so the full suite is re-run after every one. | Tasks 3, 5, 7 |

## LIMIT: known and accepted

| # | Item | Raised |
|---|---|---|
| P1 | `_checked_time` validates the range bounds but not the step grid, so `30.5` on a step-1 range is accepted here and rejected later by `HonParameterRange`'s setter. Safe (the dispatcher rolls back, nothing is sent) but the error message is less precise. Not fixed: duplicating that module's magnitude-scaled, step/4-capped epsilon is a bigger risk than the imprecise message, and Task 9's number entity takes `native_step` from the schema anyway. | Task 2 |
| P2 | `environment_available` returns False when `onOffStatus` is absent. Conservative reading of the acceptance criterion "off-state sentinels are not shown as measurements": an unreported power state is not a confirmation. | Task 2 |
| P3 | `_sort_key` (diagnostics) still calls `str(value)` on each sampled item; a single item with a pathologically expensive `__str__` is unbounded. Pre-existing, inherited from the dispatcher campaign. | dispatcher campaign |
| P4 | The AP settings schema declares no mandatory parameter, so the contract matrix never exercises "mandatory key added to a settings patch". If a live schema declares one, the matrix needs a case. | Task 1 |
| P5 | `fan_speed` is shared as a translation key with the AC's writable fan-speed sensor. Both render "Fan speed" and neither declares options, so the parity tests pass, but the two mean different things (AP telemetry vs AC setting readback). | Task 3 |
| P6 | `async_turn_on(percentage=...)` on the fan is accepted and ignored: the purifier exposes discrete modes and `SET_SPEED` is deliberately not declared. A caller passing a percentage gets last-mode behavior rather than an error. | Task 5 |
| P7 | Entity `unique_id` suffixes are permanent. The fan uses `purifier`, the light `panel_light`; renaming any AP key in a later task orphans already-registered entities. | Tasks 5, 6 |
| P8 | `supported_brightness_levels` on the light is a custom property, not a Home Assistant concept. It exists so a test can assert the level set never resizes; HA ignores it. | Task 6 |
| P9 | The light reports `brightness` 0 rather than None while off. Truthful for this device, and HA does not surface brightness for an off light. | Task 6 |
| P10 | The AP branch in `switch.async_setup_entry` reads `coordinator.data.items()` directly, matching that module's existing style, while the newer `fan.py`/`light.py` use `coordinator_data_map`. Left consistent with the file it lives in. | Task 7 |
| P11 | `child_lock` shares a translation key across the AC and the AP. Same parameter, same meaning, so intentional consistency rather than the accidental overlap of `fan_speed` (P5). | Task 7 |
| P12 | The aroma select is the FIRST AP control that is power-gated (unavailable while stopped), per the design's rule that aroma patches only go out during an active session. Lock and tone deliberately are not. If a live device turns out to accept aroma changes while stopped, this gate is the thing to relax. | Task 8 |
| P13 | Custom aroma always sends BOTH timing fields. The single-timing variant (`set_aroma_time`, V3) is Task 9's shape and is deliberately unused here. | Task 8 |

## Process notes

- **GitNexus is unusable on this repository.** `impact(PLATFORMS, upstream)`
  reports 0 consumers where grep finds 8; `detect_changes(scope="all")` reports
  unrelated symbols and misses new modules. Known graph-identity defect, recorded
  during the dispatcher campaign. Grep and the suite are the ground truth, and the
  plan's mandatory-GitNexus steps are satisfied by an equivalent grep sweep with
  the substitution stated in each report.
- **No subagent confutator pool this campaign**, per explicit user decision.
  Self-review across three lenses (soundness, coverage, adversarial) plus
  mutation testing of the implementation substitutes for it, disclosed in every
  task report.
- **Mutation testing is the real RED for validator-style tests.** Tests that
  validate already-correct data cannot be proven by stashing a production file, so
  each defect class is injected one at a time and the catching test recorded. Two
  survivors so far, both closed: Task 2's P3 (`supports_light` accepting any
  value set) and Task 5's F3 (a stopped purifier still reporting a preset).
- **Mutation survivors are not always test gaps.** Task 6 produced two survivors
  that were both UNREACHABLE production code masquerading as protection: a
  second membership guard that `supports_light` made impossible to reach, and a
  0-255 clamp that the nearest-level search already made redundant. Both were
  removed rather than covered by a new test, and the reworked lines were
  re-mutated to confirm they are now load-bearing. A mutation that survives is a
  question, not automatically a missing test.
- **Deliberate control mutations are run alongside the real ones** (Task 5's F8b,
  Task 6's L10): a no-op edit that must NOT fail, proving the harness does not
  manufacture failures.
- Task 1's RED was not observed at the time (fixture and test written together,
  straight to green). Verified retroactively. Not repeated since.
