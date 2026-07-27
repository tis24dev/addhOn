# Air Purifier Task 13 Report

## Status

Complete on `dev`. Verification gate only: **no production and no test edits**.

## Step 1: repeated full runs

```text
python3 -m pytest tests/ -q      1548 passed, 1 skipped, 7 xfailed, 302 subtests
python3 -m pytest tests/ -q      1548 passed, 1 skipped, 7 xfailed, 302 subtests
python3 -m compileall -q custom_components/addhon tests   clean
```

Identical counts on both runs. Campaign baseline was 1236 at Task 1; the plan added
312 tests.

## Order independence, checked properly

Order dependence was THIS campaign's recurring defect (V11, V12), so the gate goes
beyond "the suite passes":

Every heavily-stubbed module in isolation:

```text
test_air_purifier_entities   175   test_command_dispatch       51
test_air_purifier_contracts   90   test_stub_hygiene            3
test_diagnostics              55   test_number_setpoints       30
test_options_flow             32   test_sensor_per_type        46
test_translations             30   test_tier2_sensors          55
test_entity_translation_keys  10
```

And nine adversarial pairs, each putting a module that installs its own Home Assistant
stubs BEFORE the AP entity tests, which is the exact shape that produced both earlier
defects:

```text
entity_translation_keys + ap_entities   185 passed
diagnostics             + ap_entities   230 passed
program_options         + ap_entities   214 passed
wash_option_params      + ap_entities   185 passed
tier2_sensors           + ap_entities   230 passed
switch_params           + ap_entities   183 passed
ac_write_path           + ap_entities   254 passed
entity_availability     + ap_entities   182 passed
number_setpoints        + ap_entities   205 passed
```

All green. `tests/test_stub_hygiene.py` is what keeps it that way.

## Step 2: repository integrity

```text
python3 -m pytest tests/independence -q   11 passed, 7 xfailed
git diff --check                          clean
git status --short                        M .gitignore  (pre-existing, excluded)
git check-ignore -v <plan>                .gitignore:17:/.development/  -> ignored
git ls-files apk diagnostics .development .gitnexus AGENTS.md CLAUDE.md
                                          (empty: nothing tracked)
python3 -m json.tool manifest.json / hacs.json / both translations   clean
```

`PLATFORMS` is `climate, sensor, binary_sensor, switch, select, button, number, fan,
light`; `test_every_declared_platform_has_a_module` pins that each has a module, which
is the guard added in Task 5 after finding the stubbed suite stays green without one.

## Step 3: the five invariants (GitNexus substitute)

GitNexus remains unusable on this repository (see the ledger's process notes), so each
invariant is checked by the test that pins it plus a grep, with the substitution stated
here as in every previous report.

| Invariant | How it is held |
|---|---|
| Legacy AC/washer/dryer/ref/wine-cooler processes retain their current sender | `test_command_dispatch.py -k "legacy or dispatcher_has_no"`: 5 passed. The per-class guard now pins 6 classes across the 3 mixed files. |
| AP writes reach `CommandDispatcher` exclusively | No `async_send_command` / `async_send_settings` / `run_command_sync` anywhere in `fan.py`, `light.py`, `air_purifier.py`; the 3 mixed-file AP classes are pinned by their own architecture tests (10 passed). |
| Full program/category sends unchanged | `git diff 0546291..HEAD` touches none of `program_options.py`, `button.py`, `climate.py`, `ac_command.py`, `hon_commands.py`. |
| `HonApi.send_command()` unchanged | `git diff 0546291..HEAD` touches neither `client/transport/api.py` nor `client/engine/commands.py`. |
| No model-name conditional | `test_air_purifier_never_reads_a_model_or_nickname` (AST-based) passes; grep finds no `model_name` / `modelName` / `appliance_model` read in ANY of the eight AP-touching platform modules. |

The whole campaign changed **zero** files under `client/`. Every behavior added sits in
the integration layer, and the engine is untouched.

## Step 4: prohibited evidence

```text
rg "HHP50CA011|HHP55CA011|decomp\.txt|diagnostica_richiesta|serialNumber|macAddress" \
   custom_components tests
```

- `HHP50CA011` / `HHP55CA011`: **zero matches.** The last one, the Task 1 trap fixture,
  was closed in Task 12 (ledger A1).
- `diagnostica_richiesta`: zero matches.
- `serialNumber` / `macAddress`: many matches, all legitimate. These are hOn API FIELD
  NAMES, needed by the transport, by the redaction code and by the tests that PROVE
  redaction. Every value in tracked test code is obviously synthetic
  (`AA:BB:CC:DD:EE:FF`, `SN-PLAINTEXT`, `AP-PLAINTEXT-SERIAL`). No real identity.
- `decomp.txt`: **4 matches, all pre-existing and none from this campaign.** Verified
  with `git log -L`: `const.py:242,245` came from `5482f57` (Release v5.7.0),
  `test_sensor_per_type.py:417` and `test_tier2_sensors.py:464` from `21622e8`
  (Release v5.0.5). They are line-number citations of a gitignored dump. Reported, not
  silently changed: they predate the plan and are outside its scope.

This campaign's own provenance comments name the decompiled ENUM
(`AP_PARAMS_ENUM`) without any path or offset, matching the repository's existing
`apk/analysis/...` citation convention used across `client/engine/`.

## Verification of the plan's own expectations

Every "Expected: PASS" in Tasks 9-13 was run and passed. The two commands the plan
spells with `python` were run as `python3` (that interpreter does not exist here), the
same substitution used since Task 1.

## Self-Review (no subagent confutator pool this campaign)

- **Soundness**: verified the two suite runs report identical counts, so nothing is
  order- or state-dependent within a run; verified the evidence sweep's noise is field
  NAMES rather than values by reading every non-obvious match; verified the "unchanged"
  claims with `git diff` against the campaign base rather than by inspection.
- **Coverage**: all four plan steps, plus the isolation and adversarial-order sweep the
  plan does not ask for and which this campaign proved necessary twice.
- **Adversarial**: the suite has no real Home Assistant, so nothing here proves the
  integration LOADS in HA. `hassfest` and a real HA start-up remain unexercised; the
  JSON and manifest checks are a weak proxy. That belongs to the beta release, next to
  the live items.

## Residual: what this gate cannot certify

The campaign's central weakness is unchanged and is recorded as ledger L1-L9: every
test asserts internal coherence against a schema and a shadow derived from the design,
not against hardware. The two highest-value live checks stay `mainFilterStatus`
direction (L1) and the inverse light encoding (L5), followed by the experimental CO
value (L8). Tasks 14 and 15 remain BLOCKED by the user's explicit decision to resolve
them through a beta release plus user-collected diagnostics, which Task 10's
`future_capabilities` section now makes readable in a plain diagnostics download.
