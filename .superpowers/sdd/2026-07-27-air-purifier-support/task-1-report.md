# Air Purifier Task 1 Report

## Status

Complete on `dev`.

- Commit subject: `test: define air purifier contracts`
- Owned fixtures: `tests/fixtures/contracts/air_purifier.json`, `tests/fixtures/ap/schema.json`
- Owned tests: `tests/test_air_purifier_contracts.py`

Task 1 produces no production code. It defines the anonymized AP contract
matrix that Tasks 2-8 build against and Task 12 replays through the real
dispatcher, so the matrix itself is the artifact that has to be trustworthy.

## The matrix

17 cases, one per action required by the plan: `turn_on_auto`, `turn_off`,
`preset_sleep`, `preset_auto`, `preset_max`, `light_off`, `light_50`,
`light_100`, `lock_on`, `lock_off`, `tone_on`, `tone_off`, `aroma_off`,
`aroma_soft`, `aroma_mid`, `aroma_h_biotics`, `aroma_custom`.

Every case carries the full command-attribute schema (the shape
`dispatcher.json` already uses, so Task 12 can build real `HonCommand` objects
from it), an `initial_shadow`, the intent (`command_name` + `values`), the
`expected_payload`, the `expected_shadow_delta`, and the protected
`must_not_change` keys.

Payload shapes follow the design's command-intent section: `startProgram` with
the selected `machMode` plus the schema's mandatory `onOffStatus=1`,
`stopProgram` with nothing but its mandatory `onOffStatus=0`, and every
settings action carrying only its own field. `aroma_custom` carries
`aromaStatus=4` plus both timing fields.

## Verification finding: the first test pair did not defend the matrix

The two tests the plan dictates check that all 17 action names exist and that
no payload sends `machMode` `0` or `3`. A re-read of the committed task showed
that is all they check. An injected off-schema field
(`expected_payload["bogusField"] = "9"`) passed both.

That gap matters because the plan's global constraint is "never send a field or
value absent from the active command schema", and the matrix is the source of
truth for eleven downstream tasks. The first real check would otherwise have
been Task 12.

A second finding: `tests/fixtures/ap/schema.json` was created because the plan
requires it, but nothing read it. It duplicated the schema already inlined in
every case, with no mechanism keeping the two in agreement.

Six tests were added to close both:

- `test_every_case_carries_the_ap_specific_keys`: `command_name` and `values`
  live outside `contract_fixtures._REQUIRED` (dispatcher.json spells the same
  information as one `action` mapping), so the shared loader cannot catch a
  misspelling there. Validated locally instead of widening the shared
  `_REQUIRED`, which would break `dispatcher.json`.
- `test_every_case_declares_the_shared_ap_schema`: normalizes each case's
  full schema down to the shorthand notation of `fixtures/ap/schema.json` and
  compares. The shorthand fixture stops being dead weight and becomes the pin
  that keeps the two representations from drifting.
- `test_payloads_only_use_declared_fields_and_values`: every payload key must
  be declared by the case's own command schema, enum values must be in
  `enumValues`, fixed values must match `fixedValue`, range values must fall
  within `minimumValue`/`maximumValue`.
- `test_payload_is_the_intent_plus_the_schema_mandatory_keys`: pins that the
  payload is exactly the intent merged with the schema's mandatory keys. The
  disjointness assertion is deliberately separate from the equality one: merged
  into a single check, an intent that duplicates a mandatory key at its schema
  value would be invisible.
- `test_shadow_deltas_are_real_changes`: no delta entry may equal the initial
  shadow value, or the case would still pass with the write silently dropped.
- `test_protected_keys_partition_the_shadow`: written and protected keys are
  disjoint and together cover the whole shadow, so no field escapes the matrix.

## TDD Evidence

The original pair was written and run green without an observed RED. Verified
retroactively by hiding the fixture: both fail (`FileNotFoundError`), which is
the failure the plan predicted for a missing fixture.

The six added tests validate data that is already correct, so their discriminating
power was proven by mutation rather than by a stashed production file. Each
defect class was injected into the fixture and the catching test recorded:

```text
M1  undeclared field in payload             -> payloads_only_use_declared_fields_and_values (+2)
M2  enum value not declared                 -> payloads_only_use_declared_fields_and_values
M3  range value out of bounds               -> payloads_only_use_declared_fields_and_values
M4  fixed value mismatch                    -> payloads_only_use_declared_fields_and_values (+1)
M5  mandatory key dropped from payload      -> payload_is_the_intent_plus_the_schema_mandatory_keys (+1)
M6  intent duplicates mandatory key         -> payload_is_the_intent_plus_the_schema_mandatory_keys
M7  no-op shadow delta                      -> shadow_deltas_are_real_changes
M8  protected key also written              -> protected_keys_partition_the_shadow
M9  shadow key neither written nor protected -> protected_keys_partition_the_shadow
M10 case schema drifts from ap/schema.json  -> every_case_declares_the_shared_ap_schema
M11 command_name removed / values wrong type / action mismatched
                                            -> every_case_carries_the_ap_specific_keys
```

All 11 mutations produced at least one failure; the fixture was restored from a
pristine copy after each and the working tree verified clean afterwards.

## Deviation from the plan

The plan's Step 4 dictates the custom aroma payload as
`{"aromaStatus":"4","aromaTimeOn":"60","aromaTimeOff":"60"}`. The matrix uses
`30` and `90` instead.

Reason: the AP shadow starts at `aromaTimeOn=60`/`aromaTimeOff=60`, so the
plan's literal values would make both timing entries no-op deltas. The case
would then pass with the timing write dropped entirely, and with the two fields
swapped. Distinct in-range values make the delta real and the fields
distinguishable. `test_shadow_deltas_are_real_changes` now enforces this
property for every case, so reverting to the literal `60/60` would fail the
suite.

## Verification

```text
python3 -m pytest tests/test_air_purifier_contracts.py -q
8 passed

python3 -m pytest -q
1244 passed, 1 skipped, 7 xfailed, 302 subtests passed
```

Baseline before this task was 1236 passed. GitNexus `detect_changes(scope="all")`
reported no changed symbols, as expected for a test/fixture-only task.

## Self-Review (no subagent confutator pool this campaign)

Per the user's explicit decision for this campaign, execution is solo and this
self-review substitutes for the usual three-confutator pool.

- **Soundness**: verified the schema normalizer handles all three typologies
  present and raises on an unknown one rather than silently skipping it;
  verified range comparison is numeric on both sides so `"1"` and `1` cannot
  disagree by JSON spelling; verified the mandatory-key helper reads
  `fixedValue` for fixed parameters and `defaultValue` otherwise.
- **Coverage**: 11 injected defects, all caught. Checked that the merge-order
  hole in the payload equality assertion (a same-value mandatory duplication)
  is closed by the separate disjointness assertion, and confirmed it with M6.
- **Adversarial**: the matrix asserts self-consistency, not device truth. A
  wrong-but-coherent contract (e.g. an inverted light mapping) passes every
  test here. Only Task 12's dispatcher replay and the deferred live validation
  can catch that; the tests deliberately claim no more than internal coherence.

## Residual Notes

- All 17 cases are labelled `evidence: live_observation`. The plan permits
  `live_observation` or `cloud_schema`, and the state mappings do come from the
  design's live evidence, but the payload-minimality rules are design decisions
  for which `derived_contract` would be the more honest label. Left as the plan
  specified; worth revisiting if the evidence field is ever audited.
- The settings command in the AP schema declares no mandatory parameter, so the
  matrix never exercises "mandatory key added to a settings patch". If a live
  schema later declares one, the matrix needs a case for it.
- Task 14/15 live validation is blocked with no HHP50/HHP55 access; per the
  user's decision the plan is to ship a beta and collect user diagnostics.
