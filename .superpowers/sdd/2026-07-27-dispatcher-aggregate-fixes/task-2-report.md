# Dispatcher Aggregate Fix Task 2 Report

## Status

Complete on `dev`.

- Commit subject: `fix: preserve concurrent mqtt updates through dispatch rollback`
- Owned production files: `param_rollback.py`, `command_dispatch.py`
- Owned tests: `test_command_dispatch.py`, `test_engine_cluster.py`

## Root Cause

`CommandDispatcher.dispatch()`'s `rollback()` did two things that were never
transaction-owned:

1. Blind-restored the ENTIRE shadow (`appliance.attributes["parameters"]`) from
   a pre-send snapshot, even though the exact-send path never mutates the
   shadow before the commit point (`HonCommand._send_parameters` uses
   `sync_shadow=False` for `send_exact`).
2. Blind-restored every command's parameters across the whole category tree
   from a pre-`_prepare()` snapshot, with no way to tell the transaction's own
   write apart from a write made by something else in the meantime.

The awscrt MQTT callback (`mqtt.py::_on_message`) runs on its own OS thread,
outside the dispatcher's per-appliance `asyncio.Lock`, and mutates the SAME
shadow `HonAttribute` objects and (via `sync_params_to_command`) the SAME
`HonCommand.parameters` objects the dispatcher just prepared or is about to
send. If a push landed while `send_exact()` was suspended awaiting the cloud
and the send then failed, rollback discarded that authoritative update and
replaced it with the stale pre-send state.

## Fix

- `param_rollback.py` gains `restore_owned_params(params, baseline, own_write)`
  (additive; `restore_params`/`snapshot_params` and their existing callers in
  `button.py`/`hon_commands.py`/`program_options.py`/`switch.py` are
  untouched). It restores a parameter to `baseline` only if its CURRENT
  `__dict__` still equals `own_write` -- proof nothing touched it since the
  transaction's own write. A mismatch means something else (MQTT) wrote to it
  during the await, and that write is left in place.
- `command_dispatch.py::dispatch()`:
  - Shadow: `shadow_before` is kept (still feeds the post-commit
    `_emit_shadow_safely` diagnostic) but is no longer restored on rollback --
    dispatch never owns it pre-commit, so restoring it could only ever clobber
    a concurrent write.
  - Parameters: `_prepare()` is wrapped in an inner `try/finally` that snapshots
    every command's parameters again immediately after `_prepare()` returns or
    raises (`own_write_snapshots`), i.e. right before the await. `rollback()`
    now calls `restore_owned_params` per command using that snapshot as the
    "did anything else touch this since we wrote it" baseline.

## TDD Evidence

RED (verified by temporarily stashing the two production files):

```text
python3 -m pytest -p no:cacheprovider \
  tests/test_engine_cluster.py::ClusterBehaviorTest::test_dispatch_rollback_preserves_concurrent_mqtt_update -q

AssertionError: 'off' != 'on'
1 failed
```

GREEN after restoring the fix: same command, `1 passed`.

## Tests Added / Changed

- `test_engine_cluster.py::test_dispatch_rollback_preserves_concurrent_mqtt_update`
  (new): real `HonAppliance`/`HonCommand`/`HonAttribute`, dispatch suspended
  mid-`send_exact`, a concurrent task calls the REAL `sync_params_to_command`
  (the same method `mqtt.py` calls) against a parameter the transaction never
  touched; asserts it survives the subsequent failed send's rollback while the
  transaction's own untouched-since write is still correctly undone.
- `test_command_dispatch.py::test_dispatch_rollback_preserves_update_landing_while_send_is_suspended`
  (new): same scenario via real event-loop suspension (`asyncio.Event`
  blocking inside a stubbed `send_exact`, a separate task races the update),
  against a parameter the transaction itself rule-cascaded before the race.
- `_mutate_selected_category_and_shadow` renamed to
  `_race_concurrent_update_during_send` (unit-fixture helper); the 4 existing
  rollback tests that used it
  (`test_dispatch_transaction_rolls_back_cloud_false`,
  `_cloud_api_error`, `_transport_exception`,
  `test_dispatch_cancellation_rolls_back_and_propagates`) now assert the race
  survives (via a new shared assertion helper,
  `_assert_rollback_undid_own_write_but_kept_the_race`) instead of asserting a
  full-state restore -- their old assertion was exactly the over-restore bug
  this task fixes.
- `test_dispatch_transaction_rolls_back_assignment_failure` and
  `_prepare_failure` are UNCHANGED and still assert full
  `_snapshot_transaction(appliance) == before`: with no concurrent actor,
  compare-and-restore degrades to the original blind-restore behavior, so
  these two tests are the regression guard against the fix silently weakening
  rollback in the common (non-racing) case.

## Verification

```text
python3 -m pytest -p no:cacheprovider tests/test_command_dispatch.py tests/test_engine_cluster.py -q
84 passed

python3 -m pytest -p no:cacheprovider -q
1230 passed, 1 skipped, 7 xfailed, 302 subtests passed
```

Static checks: `compileall` and `git diff --check` passed on all owned files.

GitNexus exact-Cypher caller check (`CALLS -> dispatch` / `CALLS -> restore_params`)
reconfirmed Task 1's finding: no production caller of `CommandDispatcher.dispatch`
outside tests, so this task's blast radius is fully bounded to
`command_dispatch.py` + `param_rollback.py` (additive) + their tests; exact-caller
grep on `restore_params`/`snapshot_params` cross-checked the same four legacy
call sites are untouched.

## Self-Review (no subagent confutator pool this round)

Per this session's active constraint against spawning the Agent tool without
an explicit user request, the usual 3-confutator adversarial pool was not run.
Instead a single-pass self-review across the same three lenses the pool
usually covers:

- **Soundness**: verified the `__dict__` equality comparison is safe for every
  parameter type in play (plain values, `_triggers` compares by function
  identity, unaffected by this change); confirmed `own_write_snapshots`' 1:1
  correspondence with `parameter_snapshots`; confirmed the swap restore
  (`commands[patch.command_name] = command_before`) is unaffected and still
  safe (MQTT never touches that dict entry, and the per-appliance lock already
  excludes a second `dispatch()` racing it).
- **Coverage**: confirmed both compare-and-restore branches are exercised --
  racing a transaction-owned, rule-cascaded parameter (`coupled`, the 4
  repurposed unit tests + the new suspension test) and racing a parameter the
  transaction never touched at all (`light`, the new real-engine test) -- and
  that the no-race case is unchanged (the two untouched full-snapshot tests
  stayed green, proving compare-and-restore is not a silent weakening when
  nothing concurrent happens).
- **Adversarial**: looked for a race landing INSIDE `_prepare()` itself, before
  `own_write_snapshots` is captured (see Residual Notes) and confirmed the
  `finally` around `_prepare()` correctly captures partial mutations from a
  raising `patch.prepare()` callback (still covered by the unchanged
  `test_dispatch_transaction_rolls_back_prepare_failure`).

This is a narrower net than the user's standard 3-confutator hold-cycle; flagged
here rather than silently substituted.

## Residual Notes

- A concurrent write landing in the narrow synchronous window of `_prepare()`
  itself (before `own_write_snapshots` is captured, no `await` in between) is
  indistinguishable from the transaction's own write and could later be
  clobbered if rollback fires and nothing else touches that key again first.
  This window is orders of magnitude smaller than the `await send_exact(...)`
  window Finding 2 is about, and is intentionally left as a residual risk
  (parallel to Task 1's "cancellation after remote acceptance" note).
- Compare-and-restore cannot semantically distinguish "an authoritative MQTT
  update" from any other concurrent write; per the review's own framing, ANY
  write landing after the transaction's own write is treated as not ours to
  undo.
