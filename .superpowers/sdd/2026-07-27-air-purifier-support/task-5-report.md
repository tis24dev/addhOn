# Air Purifier Task 5 Report

## Status

Complete on `dev`.

- Commit subject: `feat: control air purifier power and modes`
- Owned production files: `fan.py` (new), `const.py`, `translations/en.json`, `translations/it.json`
- Owned tests: `tests/conftest.py`, `tests/test_air_purifier_entities.py`
- Unplanned but required: `tests/test_command_dispatch.py`, `tests/test_entity_translation_keys.py`

This is the first task where the transactional dispatcher actually drives a
device from an entity. Everything before it was dormant or read-only.

## What was built

`HonAirPurifierFan`, one per purifier that can actually be driven. Setup gates on
`capabilities.supports_fan`, so a device missing `stopProgram`, missing a mode
writable from both commands, or not reporting power/mode state simply gets no fan
instead of a broken one.

Write matrix, exactly as the plan specifies:

```text
turn_on                 -> startProgram {"machMode": last_or_auto}
turn_off                -> stopProgram  {}
set_preset while on     -> settings     {"machMode": selected}
set_preset while off    -> startProgram {"machMode": selected}
```

Every write goes through `ap_patch` + `async_dispatch_patch`; nothing reaches
`async_send_command`, pinned by a source-level test and by the dispatcher's own
allow-list guard.

`AP_LAST_MODE_STORE` holds the last ACTIVE mode per appliance on the coordinator
(volatile, so a reload falls back to the deterministic Auto default). It is
recorded in `_handle_coordinator_update`, not in the `preset_mode` property:
Home Assistant reads properties far more often than state changes, and a property
with a side effect is a trap. `_remember_mode` refuses anything outside
`writable_modes`, so neither the off sentinel `0` nor the undeclared allergen
mode `3` can ever become the value a later turn-on replays.

The intent is built INSIDE the try in `_dispatch`, so a value the live schema
rejects surfaces as the same localized `command_error` as a transport failure
rather than a bare `ValueError` reaching Home Assistant.

`fan` was added to `PLATFORMS` in this commit, the one that creates `fan.py`,
per Task 2's deliberate deferral. `test_every_declared_platform_has_a_module`
already enforced the ordering.

## Test gap found by mutation, and why it mattered

Ten mutations, nine caught immediately. One survived:

```text
F1  setup ignores the capability gate        -> caught (3)
F2  store accepts the off sentinel / mode 3  -> caught
F3  stopped purifier still reports a preset  -> SURVIVED
F4  preset while off uses settings            -> caught
F5  preset while on uses startProgram         -> caught (2)
F6  last mode never replayed                  -> caught (3)
F7  no refresh after a command                -> caught (2)
F8  transport failure not localized           -> caught
F8b no-op control mutation                    -> correctly passed
F9  no deterministic auto default             -> caught (2)
```

F3 removed the `if not self.is_on: return None` guard from `preset_mode` and
nothing failed. The reason is a flaw in how the tests stopped the purifier: every
"off" case set `onOffStatus="0"` AND `machMode="0"` together, so the mapping
returned None regardless of the guard.

The case that was missing is the realistic one. The design notes the device
retains stale values at power-off, so a stopped purifier can still report
`machMode="2"` in its shadow. Without the guard the fan would then show the
"auto" preset while stopped, claiming the purifier is running that mode --
exactly what the design forbids ("The fan reports off with no active preset").
Added `test_a_stopped_purifier_reports_no_preset_even_with_a_live_mode`; F3 now
fails.

F8b was a deliberate no-op mutation and passed, confirming the harness does not
manufacture failures.

## Shared test stubs moved to conftest

Per the plan, `FanEntity`, `FanEntityFeature` and `AddEntitiesCallback` live in
`conftest.py` rather than in the AP test module. Two gaps in the existing shared
stubs surfaced while wiring this up and were filled there:

- `CoordinatorEntity` had no `_handle_coordinator_update`, which real HA provides
  and the fan overrides.
- Nothing exposed `unique_id`, `preset_mode`, `preset_modes`, `supported_features`
  as properties over their `_attr_*` backing fields. Without them a test reads the
  private attribute and passes even when the entity never exposes the value to
  Home Assistant, which is the opposite of what these tests are for.

`FanEntityFeature` uses the real Home Assistant numeric values so a stub-vs-real
mismatch cannot hide a wrong feature declaration.

## Deviations from the plan

- **`tests/test_command_dispatch.py`**: `fan.py` added to the dispatcher
  allow-list, same reason as `air_purifier.py` in Task 2. The guard's docstring
  now says each AP platform module dispatches, not just the intent builder.
- **`tests/test_entity_translation_keys.py`**: `_collect_code_keys` hardcodes the
  platform list and had no `fan` entry, so an `entity.fan` block would have been
  entirely unchecked for stale or misspelled keys (the `extra` half of the parity
  assertion only runs for platforms the collector knows). Added the fan platform.
  Task 11 nominally owns this file; leaving a whole platform unverified for six
  tasks was the worse option.

## Known limitation: preset display casing

Home Assistant does not translate `fan` preset modes; they render literally. The
presets are therefore shown as `sleep` / `auto` / `max`, because
`AP_MODE_TO_PRESET` is pinned to those lowercase keys by the plan's own Task 2
assertion. Capitalizing them would break that contract and the mapping is used as
a machine key elsewhere. Flagged for Task 11 to decide whether the display
strings should diverge from the mapping keys.

## Verification

```text
python3 -m pytest tests/test_air_purifier_entities.py -q
61 passed

python3 -m pytest tests/test_command_dispatch.py tests/test_entity_translation_keys.py \
  tests/test_sensor_per_type.py tests/test_tier2_sensors.py tests/test_switch_params.py -q
167 passed

python3 -m pytest -q
1342 passed, 1 skipped, 7 xfailed, 302 subtests passed
```

Baseline before this task was 1314. `compileall`, `git diff --check` and
`json.tool` on both translation files all clean.

## Self-Review (no subagent confutator pool this campaign)

- **Soundness**: verified the store is written only from `writable_modes`, so a
  replayed turn-on can never send a value the schema rejects; verified the
  refresh is skipped when the dispatch raises (F7 and the transport-failure test
  pin both directions); verified `_dispatch` re-raises `HomeAssistantError`
  untouched so the missing-client case keeps its own translation key instead of
  being folded into `command_error`.
- **Coverage**: capability gating in four directions, preset list from the live
  intersection, all four write paths, last-mode retention including both values
  that must NOT be retained, the reload default, sparse payloads, and three error
  paths.
- **Adversarial**: the write matrix is asserted against a fake client that records
  patches; nothing here proves the purifier accepts `startProgram` with only
  `machMode`, nor that a running purifier accepts a `settings` mode change. Both
  are contract claims from the design awaiting live validation. The sparseness
  assertion (`no write ever carries an unrelated field`) is the one that would
  most visibly break if the device turns out to require more fields.

## Residual Notes

- `async_turn_on(percentage=...)` is accepted and ignored: the purifier exposes
  discrete modes, not a percentage, and `SET_SPEED` is deliberately not declared.
  A caller passing a percentage gets the last-mode behavior rather than an error.
- The fan's `unique_id` suffix is `purifier`. It is the device's primary entity;
  if a future task ever renames the key, already-registered entities orphan.
