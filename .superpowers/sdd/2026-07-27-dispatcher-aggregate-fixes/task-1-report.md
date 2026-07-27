# Dispatcher Aggregate Fix Task 1 Report

## Status

Complete on `dev`.

- Commit subject: `fix: canonicalize exact payload commit point`
- Owned production files: `commands.py`, `command_dispatch.py`
- Owned tests: `test_command_dispatch.py`, `test_engine_cluster.py`
- The pre-existing `.gitignore` modification was not touched and is excluded
  from the task commit.

## Pre-edit Impact Analysis

GitNexus upstream impact reported `send_exact` and
`CommandDispatcher.dispatch` as CRITICAL (`59` impacted, `45` direct, `7`
processes). This is the known graph identity defect, not the actionable blast
radius: exact Cypher counted `2142` Method nodes, of which `2115` have a null
or empty ID.

Exact caller queries found:

- `HonCommand.send_exact`: one indexed CALLS edge, from
  `test_send_exact_does_not_broadly_sync_shadow`; source inspection also found
  the expected production caller `CommandDispatcher.dispatch`.
- `CommandDispatcher.dispatch`: duplicated edges from dispatcher tests only;
  the production dispatcher remains dormant outside its adapter/client seam.
- `HonCommand._send_parameters`: LOW risk, with the two real direct callers
  `send_parameters` and `send_exact`.

The CRITICAL gate was released after the exact graph evidence was reported.

## TDD Evidence

RED, after correcting the hand-derived range fixture from numeric `6` to the
actual transmitted string `"6"`:

```text
python3 -m pytest -p no:cacheprovider \
  tests/test_engine_cluster.py::ClusterBehaviorTest::test_dispatch_reuses_canonical_start_program_payload \
  tests/test_command_dispatch.py::test_dispatch_post_commit_sync_error_keeps_committed_state -q

2 failed
```

The real-engine test showed wire `prStr=PROGRAMS.REF.SUPER_COOL` while sync
still received `prStr=x`. The post-commit test propagated `RuntimeError("sync
boom")` from a partial sync.

GREEN with the same command:

```text
2 passed in 0.12s
```

## Implementation

- `HonCommand.canonical_exact_payload()` creates the single owned payload copy
  and canonicalizes `prStr` before dispatch diagnostics or transport.
- An internal owner marker lets `send_exact()` reuse that exact mapping without
  making another copy; ordinary direct callers still get copy-on-send behavior.
- The canonical mapping instance is reused for wire send, targeted shadow sync,
  expected MQTT correlation, and payload diagnostics.
- Legacy `send()`, `send_specific()`, and direct `send_exact()` behavior is
  preserved.
- `send_exact() is True` is the remote commit point. Preparation,
  canonicalization, rejection, send exceptions, and pre-commit cancellation
  retain the existing rollback and propagation behavior.
- Post-commit sync `Exception` is best-effort: partial local updates are kept,
  expected MQTT correlation is still recorded, dispatch returns `True`, and the
  result diagnostic is `result=true`, `outcome=committed_sync_error`, with the
  reconciliation error type. `BaseException` cancellation after the commit
  point is not rolled back or mislabeled as a pre-commit command error.

## Verification

Focused dispatcher and engine suites:

```text
82 passed in 0.72s
```

Full suite outside the sandbox:

```text
1228 passed, 1 skipped, 7 xfailed in 6.05s
```

Static checks:

- `PYTHONPYCACHEPREFIX=/tmp/addhon-task1-pyc python3 -m compileall -q ...` passed.
- `git diff --check` on all owned code and tests passed.

Pre-commit `detect_changes(scope="all")` reported CRITICAL (`15` changed
symbols, `186` affected, `5` files), again with unrelated methods/processes
caused by the empty Method IDs. Its fifth file is the pre-existing `.gitignore`
change, which is not staged. Exact impact evidence and the complete suite are
the actionable verification.

After selective staging, `detect_changes(scope="staged")` reported the same
false CRITICAL (`15` changed symbols, `186` affected) across the six intended
code, test, report, and ledger files. The staged set contains no `.gitignore`.

## Residual Notes

- Verification used Python `3.11.2`, not Python 3.13.
- Cancellation internal to a transport after remote acceptance but before
  `send_exact()` can return `True` remains unknowable to the dispatcher; the
  explicit commit point is the literal return value required by the brief.
