# Dispatcher Aggregate Fix Task 3 Report

## Status

Complete on `dev`.

- Commit subject: `fix: discriminate mqtt correlation matches and bound diagnostic traversal`
- Owned production file: `command_diagnostics.py`
- Owned tests: `test_transport_mqtt.py`, `test_log_identity_redaction.py`

Unlike Tasks 1-2 (the `CommandDispatcher` path, still dormant outside tests),
`command_diagnostics.py`'s public functions are already LIVE production code:
`client/transport/mqtt.py::_on_message` calls `observe_mqtt_update` directly on
every real MQTT push, which in turn calls `emit_command_event`. Both findings
in this task are reachable on a real running integration today, not just
future-dormant-dispatcher hardening.

## Finding 4: FIFO correlation could consume the wrong pending command

`observe_mqtt_update` matched on `any(observed.get(key) == expected for key,
expected in pending.expected.items())` over the FIFO queue and consumed the
FIRST (oldest) entry with ANY overlapping key. A mandatory field (e.g.
`onOff`) is common to every command on an appliance, so an older, barely-
related pending command could consume a push that actually belonged to a
newer, more fully-confirmed one -- and the newer command would then be
misdiagnosed as never having arrived.

Fix: `_match_coverage(pending, observed)` counts how many of a pending
entry's expected key/value pairs the push actually confirms. `observe_mqtt_update`
now scans the whole queue and keeps the entry with the HIGHEST coverage,
using strict `>` so a later (newer) entry only displaces the current best by
covering MORE fields -- ties naturally resolve to the earliest (oldest, FIFO)
entry in iteration order, matching "FIFO only as a tie-break" from the brief.
An entry with zero matching keys is never selected (unchanged from before).

## Finding 5: unbounded traversal before limits applied

`emit_command_event` ran `redact_identity(raw_record)`, then
`redact_identity(_materialize_collections(redacted))`, and only THEN `_bound(...)`
applied the depth-oblivious, cycle-oblivious size caps. Every step before
`_bound` fully copied/recursed the ENTIRE structure -- a self-referencing dict
hit Python's recursion limit (event silently dropped), a 2000-level-deep
structure did the same, and a large mapping/set was fully sorted before any
of it was ever trimmed to the 80-item cap.

Fix: `_bound` is now the single traversal. It tracks recursion depth (capped
at `_MAX_DEPTH = 12`, past which a container is replaced by `"<max-depth>"`)
and a `seen` set of container `id()`s scoped to the CURRENT recursion path
only (a repeat is `"<cycle>"`; a shared-but-acyclic reference visited via two
different branches is NOT a false positive, since `seen` is a fresh
per-branch frozenset, not a global "ever visited" set). Every
mapping/list/tuple/set is sampled via `itertools.islice(..., _COLLECTION_LIMIT)`
BEFORE any sort or recursion, so a huge collection costs O(_COLLECTION_LIMIT)
here instead of a full copy/sort of everything it holds; only the bounded
sample is then sorted for deterministic output. `_materialize_collections` is
gone (its set-to-list job is now part of `_bound`).

`emit_command_event` now calls `_bound` FIRST, then `redact_identity` ONCE on
the result. Since `_bound` already produced a small, finite, cycle-free, pure
dict/list/scalar structure, the still-unbounded `redact_identity` (a widely
shared cross-module helper -- session.py/api.py/mqtt.py/etc all call it --
deliberately NOT touched by this task) is now only ever asked to walk
something already small. `redact_identity` staying a real, patchable call was
a hard constraint: `test_command_event_failure_uses_only_fixed_message`
patches `command_diagnostics.redact_identity` to raise and asserts the whole
event fails safely with no secret leaked; that test is unchanged and still
passes, confirming the reorder didn't weaken that guarantee.

## TDD Evidence

RED (verified by temporarily stashing `command_diagnostics.py`):

```text
FAILED test_transport_mqtt.py::CommandCorrelationReviewTest::test_command_correlation_prefers_fuller_match_over_older_partial_match
FAILED test_log_identity_redaction.py::CommandDiagnosticReviewTest::test_command_event_survives_a_self_referencing_cycle
FAILED test_log_identity_redaction.py::CommandDiagnosticReviewTest::test_command_event_survives_a_deeply_nested_structure
FAILED test_log_identity_redaction.py::CommandDiagnosticReviewTest::test_command_event_bounds_a_very_large_mapping_quickly
```

The cycle/deep-structure failures were a `RecursionError` swallowed by
`emit_command_event`'s broad `except Exception`, surfacing as "command
diagnostic event failed" instead of a valid record -- exactly Finding 5's
"deep/cyclic structures fail the event". The large-mapping timing assertion
needed calibration: old code measured ~0.6s for 200k items (3 repeated runs),
new code ~0.0007s regardless of size (200k or 2M) -- the test threshold
(0.2s) sits with wide margin on both sides of that gap.

GREEN after restoring the fix: all four pass, plus the FIFO tie-break and
list/set bounding tests added alongside them.

## Tests Added

- `test_transport_mqtt.py::test_command_correlation_prefers_fuller_match_over_older_partial_match`:
  an older pending command sharing only the mandatory field must lose to a
  newer one matching all of its fields.
- `test_transport_mqtt.py::test_command_correlation_equal_coverage_ties_break_fifo`:
  equal coverage explicitly pins the FIFO (oldest-wins) tie-break rule.
- `test_log_identity_redaction.py::test_command_event_survives_a_self_referencing_cycle`,
  `_survives_a_deeply_nested_structure`, `_bounds_a_very_large_mapping_quickly`,
  `_bounds_a_large_list_and_set`: cycles, depth, and large collections (mapping,
  list, and set) per the brief; each asserts the event still produces valid,
  secret-free, <=4096-byte JSON instead of silently failing or doing unbounded
  work.

## Verification

```text
python3 -m pytest -p no:cacheprovider tests/test_transport_mqtt.py tests/test_log_identity_redaction.py -q
112 passed, 9 subtests passed

python3 -m pytest -p no:cacheprovider -q
1236 passed, 1 skipped, 7 xfailed, 302 subtests passed
```

Static checks: `compileall` and `git diff --check` passed on all owned files.

Exact-caller grep (not GitNexus's broad impact tool, per Task 1/2's noted
graph-identity defect) confirmed the only production callers of this module's
public surface: `command_dispatch.py` (still dormant) and
`client/transport/mqtt.py::_on_message` (live). `debug_utils.py` (the shared
`redact_identity`/`_IDENTITY_KEYS`) was NOT modified, so its other callers
(session.py, api.py, mqtt.py's own direct redaction calls, etc.) are
unaffected by construction.

## Self-Review (no subagent confutator pool this round)

Same constraint and method as Task 2 -- single-pass self-review across three
lenses, no 3-agent pool:

- **Soundness**: verified `seen` is scoped to container ids on the current
  path only (immutable scalars like small ints are never id-tracked, avoiding
  CPython interning false positives); verified the cycle check runs before
  the depth check so a genuinely deep-but-acyclic structure (the 2000-level
  test) is caught by depth, not miscounted as a cycle; verified a
  shared-but-acyclic reference visited from two branches is processed twice,
  not falsely flagged as a cycle (a global-visited-set design would have been
  a correctness regression here, not just a perf tradeoff); verified
  `redact_identity` after `_bound` cannot re-violate the string/collection
  bounds `_bound` already established (it only substitutes matched
  spans/keys, never lengthens or adds items).
- **Coverage**: both directions of Finding 4 (fuller match wins; equal
  coverage ties FIFO) plus the three untouched FIFO/TTL/cross-appliance/cap
  tests confirm no regression to documented behavior. Finding 5: cycle, pure
  depth, and all three container-type size bounds (mapping/list/set) covered,
  plus the existing identity-redaction and record-size tests confirm no
  regression.
- **Adversarial**: considered a structure both deep AND wide enough to defeat
  both bounds simultaneously (`_COLLECTION_LIMIT ^ _MAX_DEPTH` nodes) --
  physically unconstructable in real memory, not a practical concern.
  Considered whether `_sort_key`'s `str(value)` could itself be expensive for
  a single pathological item; this is pre-existing behavior unchanged by this
  task (not part of either finding), noted as a residual rather than fixed
  here.

## Residual Notes

- `_sort_key` still calls `str(value)` on each item in the bounded sample
  before truncating; a single item whose `__str__` is itself very expensive
  is not bounded by this task's fix. Pre-existing, out of scope for Finding 5.
- The `_MAX_DEPTH = 12` and continued `_COLLECTION_LIMIT = 80` are judgment
  calls, not values dictated by the review; chosen to comfortably exceed any
  realistic command/shadow payload shape while remaining a real, finite cap.
