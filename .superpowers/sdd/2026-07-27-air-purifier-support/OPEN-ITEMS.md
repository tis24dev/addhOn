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
| L8 | The experimental CO alarm's raw `2` and the AQ label's raw `0` are each a SINGLE observation. | If the value is merely unobserved, the entity is always unavailable (the chosen failure mode). If the value is WRONG (raw 2 actually benign), the CO entity raises a false alarm. Highest-value check on the experimental pair. | Task 9 |
| L9 | Custom-aroma timing granularity assumed to be SECONDS (design: observed range 1..3600, 60 = one minute). | If they are deciseconds or minutes the numbers offer a wrong unit and scale; every test passes. | Task 9 |

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
| B7 | PRE-EXISTING, reported not fixed: four `decomp.txt:<line>` citations in tracked code (`const.py:242,245` from release v5.7.0; `test_sensor_per_type.py:417` and `test_tier2_sensors.py:464` from v5.0.5). They reference a gitignored dump by offset. Outside this plan's scope; verified by `git log -L` that the campaign added none. | Task 13 |
| B6 | RESOLVED as far as VISIBILITY goes: B1-B4 now appear in a real diagnostics dump (`no2ValueIndoor` under `attributes_unmapped`, `humidificationStatus` and friends under `command_params_unmapped`), so a beta report surfaces them without this ledger. Deciding whether to MAP any of them is still open. | Task 10 |
| B5 | `test_all_tier2_descriptions_are_gated` (in `test_tier2_sensors.py`) iterates a hardcoded type list that does not include AP. AP gating is covered by `test_air_purifier_entities.py` instead, so the list was left alone. | Task 3 |

## ACTION: queued for a specific later task

| # | Item | Owner |
|---|---|---|
| A1 | CLOSED (Task 12): the trap fixture now carries `SYNTHETIC-MODEL-NEVER-READ`. The AST guard that forbids reading a model name is unaffected; the evidence sweep is clean. | Task 12 |

## DECIDE: needs a human call

| # | Item | Raised |
|---|---|---|
| D1 | RESOLVED (Task 11): keep `sleep` / `auto` / `max`. An integration CANNOT translate fan preset modes (HA core owns those state-attribute labels), so the only lever would be renaming a preset to one core already translates, breaking Task 2's contract and every automation using `max`. Consequence: core translates `auto` and `sleep`, `max` renders literally. Cosmetic, reversible, overrulable by the user. | Tasks 5, 11 |
| D2 | `evidence: live_observation` on all 17 contract cases. The plan permits `live_observation` or `cloud_schema`, and the state mappings do come from the design's live evidence, but the payload-MINIMALITY rules are design decisions for which `derived_contract` would be the honest label. | Task 1 |

## DEVIATION: plan diverged, on purpose, each pinned by a test

| # | Item | Raised |
|---|---|---|
| V1 | `aroma_custom` fixture uses `aromaTimeOn=30` / `aromaTimeOff=90` instead of the plan's literal `60`/`60`. The AP shadow starts at 60/60, so the literals would make both timing entries no-op deltas and the case would pass with the timing write dropped or the two fields swapped. `test_shadow_deltas_are_real_changes` now makes 60/60 unwritable. | Task 1 |
| V2 | RESOLVED. `fan` and `light` were NOT added to `PLATFORMS` in Task 2 as Step 4 dictates; each landed in the commit that creates its module (`fan` Task 5, `light` Task 6), so every commit stays runnable on a real instance. Enforced by `test_every_declared_platform_has_a_module`, which exists because the stubbed suite stays green with a platform listed and no module present. | Tasks 2, 5, 6 |
| V3 | `ap_patch` gained an EIGHTH action, `set_aroma_time`, not in Task 2's Step 5 list. Task 9 needs `aromaStatus=4` plus only the changed timing key, and its file list excludes `air_purifier.py`; without this it would have had to assemble a CommandPatch inside `number.py`, bypassing the intent builder. Shape fully specified by Task 9's Step 6, so nothing was guessed. | Task 2 |
| V4 | `tests/test_command_dispatch.py` modified although listed in no task before Task 10. Its dormancy guard allow-lists dispatcher callers; this plan is what wakes the dispatcher. Now contains `air_purifier.py` (Task 2), `fan.py` (Task 5), `light.py` (Task 6) and `switch.py` (Task 7); each further AP platform adds itself. | Tasks 2, 5, 6, 7 |
| V9 | The experimental value mappings (`AP_AIR_QUALITY_LABELS`, `AP_CO_ALARM_RAW`, `air_quality_label`, `co_alarm`) went into `air_purifier.py`, which Task 9's file list excludes. That module's docstring claims ownership of every AP decision, and Tasks 3/4 already set the split: attribute NAMES inline in the platform table, value SEMANTICS imported from `air_purifier.py`. Duplicating a raw-value map into a platform file is exactly the drift the module exists to prevent. | Task 9 |
| V10 | The options step title changed from "Debug options" to "Options" (both languages). Not a new key, so no parity test covers it; leaving it would have mislabelled a screen that now carries a non-debug toggle. | Task 9 |
| V8 | **MIXED-FILE allow-listing weakens the guard.** A platform file holding both an AP entity that dispatches and legacy entities that intentionally send whole commands cannot be protected by the file-level allow-list, and `_EXPECTED_LEGACY_CALL_EDGES` does not help because `async_send_settings` is not one of its tracked callees. Replaced by `test_mixed_platform_legacy_classes_keep_the_legacy_sender`, a per-CLASS check: `switch.py` (2 classes, Task 7) and `select.py` (2 classes, Task 8) are covered. **`number.py` (Task 9) becomes the third mixed file and MUST be added to that test.** | Tasks 7, 8 |
| V12 | Task 10 found TWO more instances of V11's class: `test_diagnostics.py` clobbered the same three platform bases, and **every module declared its own subset of `SensorDeviceClass` / `BinarySensorDeviceClass`**, so which MEMBERS existed depended on collection order (a stub without `SAFETY` made Task 9's "never a safety device" assertion fail with AttributeError) and the VALUES diverged (`co2` vs the real `carbon_dioxide`). The enums moved into conftest with real HA values; the four `CoordinatorEntity` FORCE-ASSIGNS were converted too (they predate conftest and are now strict SUBSETS of its class). New `tests/test_stub_hygiene.py` is an AST guard so the class of bug fails immediately in any order. | Task 10 |
| V11 | `tests/test_entity_translation_keys.py` assigned `SwitchEntity` / `SelectEntity` / `ButtonEntity` with NO getattr guard, clobbering conftest's complete stubs. Entity classes bind whatever base exists when their module is first imported, so in a collection order where that module is imported first the AP aroma select lost the `options` property real HA provides: a pre-existing failure in any four-file subset, invisible in the full suite. Now guarded. Found because the Task 9 CONTROL mutation was also reported as caught. | Task 9 |
| V5 | `tests/test_entity_translation_keys.py` collectors hardcode BOTH the platform list and each platform's key sources, so a new platform, table or fixed-key entity goes unchecked (the `extra` half of the parity assertion only runs for known sources). `fan` Task 5, `light` Task 6, `_AIR_PURIFIER_SWITCHES` Task 7, `aroma` name + its `state` block Task 8. The state-block collector iterates DESCRIPTION TABLES, so the table-less aroma select had to register its option set explicitly or its five state labels would never have been parity-checked. Task 11 owns the file; every further AP key source must add itself. | Tasks 5, 6, 7, 8 |
| V13 | Task 11 changed SIX already-committed labels: four Italian ones normalized to the AP block's dominant Title Case, plus `co` and `air_quality` for internal consistency, and the asymmetric filter pair ("Filter life" / "Pre-filter cleaning" -> "Pre-filter life"). Nothing is released, so no entity name migration is involved. Both rules are now pinned by tests. | Task 11 |
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
| P25 | The suite runs without a real Home Assistant, so nothing proves the integration LOADS in HA: `hassfest` and a real start-up are unexercised, and the JSON/manifest checks are a weak proxy. Belongs to the beta release, next to the live items. | Task 13 |
| P23 | The end-to-end matrix fakes only `api.send_command`, which always accepts. Nothing here proves behavior against a cloud that ACCEPTS a command and then reports a different state; that is L1-L9 territory. | Task 12 |
| P24 | `HonCommand._send_parameters` raises `ApiError` on a falsy api result, so the dispatcher's `result is not True` branch is unreachable through a REAL HonCommand. It stays covered by `test_command_dispatch.py`'s fake command. | Task 12 |
| P21 | `test_no_air_purifier_label_leaks_implementation_detail` uses a fixed forbidden-token list, so a NEW parameter name pasted into a label would not be caught. The list covers the parameters this campaign touches. | Task 11 |
| P22 | The Italian capitalization checker has a small minor-word set (articles, prepositions). A new label using an unlisted preposition is classified "mixed" and fails; the message names the label. | Task 11 |
| P18 | `AP_ENTITY_PARAMS` (the diagnostics coverage registration) is hand-maintained. Its drift guard derives the switch and number TABLES and asserts containment, but the fan, light and select parameters are fixed-key and cannot be derived. A new AP control whose parameter is forgotten there resurfaces as a phantom coverage gap in the dump: a wrong report, not a functional bug. | Task 10 |
| P19 | `future_capabilities.state_values_unhandled` covers only the six enum-ish AP parameters in the handled registry. A range parameter reporting an out-of-range value is not captured (its raw value is in `attributes` regardless). | Task 10 |
| P20 | Task 10's Step 5 required NO production change: the dispatcher already emitted the action, the requested/mandatory/rule-added split, the latency and the unexpected shadow fields, all identity-redacted. The two new tests are verification of an existing guarantee, not new behavior. | Task 10 |
| P14 | `ap_patch` validates a requested timing against the CAPABILITY snapshot taken at setup, while the number reports bounds read LIVE. A rules change that widens the range after setup is offered by the UI and refused by the intent builder as a localized `command_error`. Fails closed; not fixed because duplicating range resolution into `ap_patch` is the larger risk. | Task 9 |
| P15 | The experimental CO alarm can never report "off": with only the alarming value known, an all-clear would assert safety on no evidence. It is therefore unavailable on a healthy device, which is honest but of little use until L8 is settled. | Task 9 |
| P16 | `unavailable_when_unmapped` reaches `native_value` / `is_on` from `available`, so the value function runs twice per state write. Both are pure dictionary lookups. | Task 9 |
| P17 | The AP timing numbers are power-gated as well as Custom-gated, extending P12's rule from the aroma select to every write that carries `aromaStatus`. The plan only required the Custom gate. | Task 9 |
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
  Task 6's L10, Task 9's E23): a no-op edit that must NOT fail, proving the harness
  does not manufacture failures. **In Task 9 the control FIRED**, which is what
  exposed V11: 22 real mutations had been reported as caught when the whole subset
  was failing for an unrelated reason. A control mutation is not ceremony.
- Task 1's RED was not observed at the time (fixture and test written together,
  straight to green). Verified retroactively. Not repeated since.
