# Air Purifier Task 12 Report

## Status

Complete on `dev`.

- Commit: `366a35f` `test: verify purifier writes end to end`
- Owned files: `tests/test_air_purifier_contracts.py`
- `tests/test_air_purifier_entities.py` needed no change: its harness already
  drives every AP entity through `async_dispatch_patch`, and the negative cases the
  plan lists are contract-level, not entity-level.
- No production edits.

## What was built

Everything new drives the **real** `CommandDispatcher` against **real** `HonCommand`
objects, the engine's real parameter setters and rule application, and the engine's
OWN `sync_payload_to_params` (bound from `HonAppliance`, not reimplemented). Only the
network boundary is faked. A payload the engine would refuse can therefore never
reach an assertion, which is exactly what the previous per-task tests could not
guarantee: they asserted the INTENT, this asserts what leaves the machine.

Per contract case (17, parametrized by id):

- the transport received exactly `expected_payload` under `command_name`;
- the shadow delta is exactly `expected_shadow_delta`;
- every `must_not_change` key is untouched;
- exactly ONE cloud call happened, so a split payload or a restated sibling command
  fails;
- and across the whole matrix, the dispatcher's per-appliance lock table returns to
  empty.

## Negative matrix

`_expect_refusal` asserts the same three things for every refusal: nothing was sent,
the shadow is unchanged, AND every command's parameters are unchanged. The third is
the transactional guarantee, and a half-applied purifier setting is what violating it
looks like.

```text
off-schema field                 -> ValueError, nothing sent
machMode=0 (off-state sentinel)  -> ValueError from the ENUM SETTER
machMode=3 (allergen)            -> ValueError from the ENUM SETTER
undeclared lightStatus=3         -> ValueError
aromaTimeOn out of range         -> ValueError from the RANGE SETTER
missing command                  -> KeyError
Custom without both timings      -> refused by ap_patch, nothing constructed
cloud rejection (api False)      -> ApiError, full rollback after the call left
transport timeout                -> TimeoutError, full rollback after the call left
two concurrent AP writes         -> serialized, both land, neither payload carries
                                    the other's field, lock table drains
```

The mode-0 and mode-3 cases deliberately hand-build a `CommandPatch` instead of going
through `ap_patch`, which already refuses them. That puts the assertion on the ENGINE:
even a caller that bypasses the intent builder cannot get an undeclared mode onto the
wire.

The cloud-rejection and timeout cases assert `len(api.calls) == 1` on purpose: unlike
the refusals, the call DID leave the machine, and the point is that the local state
was still restored.

## Two facts the end-to-end run made concrete

- `HonCommand._send_parameters` raises `ApiError` when the api returns a falsy
  result, so a cloud rejection reaches the dispatcher as an EXCEPTION. Its
  `result is not True` branch is unreachable through a real `HonCommand`; the
  rollback that matters is the exception one.
- My first concurrency assertion expected `onOffStatus` in each settings payload.
  Production was right and the test was wrong: the AP settings command declares no
  mandatory parameter (ledger P4), so a sparse settings patch is exactly one key
  wide. Corrected, with the reason in the docstring.

## Ledger A1 closed

`_FakeAppliance.model_name` no longer carries a real model code. The trap only needs
the ATTRIBUTE to exist so a capability read that peeks at it is provable, and a real
code in tracked test code is evidence leaking out of the gitignored analysis
material. Now `SYNTHETIC-MODEL-NEVER-READ`; the AST guard that forbids reading a
model name is unaffected. Task 13's evidence sweep is clean.

## TDD Evidence

The end-to-end tests were written against unmodified production code and passed once
the two authoring mistakes above were corrected, so "RED" here is mutation testing of
the dispatcher, 9 real plus 1 control:

```text
M1  unknown-key check removed          -> caught (e2e: off-schema refusal)
M2  no rollback on a cloud rejection   -> caught
M3  no rollback on an exception        -> caught (8)
M4  shadow never reconciled            -> caught (23)
M5  mandatory keys dropped             -> caught (e2e: turn_on / turn_off ONLY)
M6  per-appliance lock removed         -> caught (3)
M7  missing command no longer raises   -> caught (e2e only)
M8  light encoding no longer inverse   -> caught (9)
M9  turn_off sends the start command   -> caught (4)
M10 CONTROL set-literal reorder        -> SURVIVED (correct)
```

M5 and M7 are the ones that justify this task. M5 survives every AP unit test,
because the settings command has no mandatory parameter: only the two cases that use
`startProgram` / `stopProgram` (whose `onOffStatus` is fixed and mandatory) notice
that the dispatcher stopped adding schema-mandatory keys. M7 is caught by the new
missing-command test alone.

## Verification

```text
python3 -m pytest tests/test_air_purifier_contracts.py -q
90 passed

python3 -m pytest tests/test_air_purifier_contracts.py \
  tests/test_air_purifier_entities.py tests/test_command_dispatch.py -q
316 passed

python3 -m pytest -q
1548 passed, 1 skipped, 7 xfailed, 302 subtests passed
```

Baseline before this task was 1503.

## Self-Review (no subagent confutator pool this campaign)

- **Soundness**: verified the fake replaces ONLY `api.send_command`, so parameter
  setters, rule application, `canonical_exact_payload` and shadow reconciliation are
  all the real code; verified `_expect_refusal` snapshots every command's parameters,
  not just the targeted one, so a rule that mutated a sibling command would be
  caught; verified the concurrency test asserts the payload CONTENT, not only that
  two calls happened, since two serialized calls with cross-contaminated payloads
  would otherwise pass.
- **Coverage**: 17 cases x 3 assertions end to end, plus one-call-per-action and lock
  drainage, plus 10 negative scenarios.
- **Adversarial**: the fake api returns True unconditionally, so nothing here proves
  behavior against a cloud that ACCEPTS a command and then reports a different state.
  That is a live-device question (ledger L1-L9), not something a fake can answer.

## Residual Notes

- `test_ap_contract_through_real_dispatcher` compares the payload with values
  stringified by the fake api. The engine's `intern_value` may be a float for a range
  parameter, and the fixture spells every value as a string; stringifying at the
  boundary keeps the comparison honest without hiding a wrong VALUE.
- The dispatcher's `result is not True` branch stays covered by
  `test_command_dispatch.py`, which uses a fake command able to return False. The AP
  matrix cannot reach it through a real `HonCommand`.
