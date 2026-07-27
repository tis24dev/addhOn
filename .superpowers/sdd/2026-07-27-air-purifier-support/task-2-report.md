# Air Purifier Task 2 Report

## Status

Complete on `dev`.

- Commit subject: `feat: model air purifier capabilities`
- Owned production files: `air_purifier.py` (new), `const.py`
- Owned tests: `tests/test_air_purifier_contracts.py`
- Unplanned but required: `tests/test_command_dispatch.py` (see Deviations)

## Impact analysis

GitNexus is not usable on this repository. `impact(PLATFORMS, upstream)` returned
`impactedCount: 0` while grep finds eight real consumers in `__init__.py`, and
`detect_changes(scope="all")` reported a single unrelated test function while
missing the new module entirely. This is the graph-identity defect already
recorded during the dispatcher campaign. Grep and the test suite were used as
ground truth, as they were there.

Findings that changed the plan:

- **HIGH: adding `fan`/`light` to `PLATFORMS` now would break a real instance.**
  `PLATFORMS` feeds `async_forward_entry_setups`, which imports each name as a
  module. `fan.py` and `light.py` are created by Tasks 5 and 6. The suite stubs
  Home Assistant and never performs that import, so the full suite stays green
  with both names listed and no modules present -- verified empirically. The
  green was not evidence of safety.
- **HIGH: `air_purifier.py` cannot reference `CommandPatch` under the existing
  dormancy guard.** `test_dispatcher_has_no_production_entity_caller` allow-lists
  only `command_dispatch.py` and `hon_client.py`.
- `hon_commands.py`'s helpers (`get_command`, `param_values`, `param_range`,
  `SETTINGS_COMMANDS`) are pure readers; a new consumer is additive with no
  effect on the existing callers in `climate.py`, `diagnostics.py`, and
  `program_options.py`.

## What was built

`air_purifier.py` owns every AP-specific decision so the platform modules stay
thin. It reads the live command schema and the live state attributes and nothing
else: no model name, no nickname, no serial.

- Mappings and their inverses: mode/preset, the INVERSE light encoding
  (raw 2/1/0 to brightness 0/128/255), aroma status/option.
- `AP_WRITABLE_MODES = {"1","2","4"}`: every discovered mode set is intersected
  with it, so the off-state sentinel `0` and the undeclared allergen mode `3`
  can never become writable no matter what a schema enumerates.
- `AirPurifierCapabilities`: a frozen dataclass of the raw discovered sets plus
  derived `supports_*` properties, one per entity feature.
- `discover_capabilities(appliance, attributes)`.
- `ap_patch(action, capabilities, *, value, time_on, time_off)`: builds the
  eight contract intents, rejecting an unknown action, an unsupported feature,
  or an undeclared value with `ValueError` so a bad intent never reaches the
  device.

Transforms: `filter_remaining` (inverts the device's CONSUMED percentage and
clamps), `normalize_error`, `has_problem`, `environment_available`.

## Defects found by self-review, before commit

The implementation passed its tests and was still wrong in three places. Each
was found by review, then reproduced, then fixed and pinned by a test.

- **A numeric zero error read as a fault.** `normalize_error` parsed with
  `int(text)`, which raises on `"0.0"`. The client unwraps attributes into real
  floats, so `has_problem(0.0)` returned True: a healthy purifier would have
  shown a permanent problem. Now parsed as a float and checked for integrality.
- **A boolean power state read as off.** `_raw(True)` produced `"True"`, which
  never equals the schema's `"1"`, so `environment_available` would hide every
  environmental sensor on a client build that unwraps the status to a bool.
  `base_entity._get_attr` documents False as a valid attribute value, so this
  shape is real.
- **A preset could be offered and then rejected.** `preset_options` was derived
  from `start_modes` while `ap_patch("set_preset")` validates against
  `preset_modes`. A preset is written through `startProgram` while the purifier
  is off and through the settings command while it runs, so a mode declared by
  only one of them is honoured or refused depending on when the user picks it.
  Added `writable_modes` (the intersection); `preset_options` and `supports_fan`
  both derive from it.

## Engine behavior discovered

`HonParameterEnum.__init__` appends `defaultValue` to `values` even when
`enumValues` omits it. A schema defaulting `machMode` to `3` therefore offers
`3` as a value. The `AP_WRITABLE_MODES` intersection already filtered it, and
`test_capability_discovery_discards_an_injected_default_mode` now pins that,
asserting the raw parameter really does expose `3` before checking that
capability discovery drops it.

## TDD Evidence

RED before any implementation: 22 failed, 8 passed (the 8 being Task 1's tests
plus the new `PLATFORMS` guard, which passes because `fan`/`light` are not
listed yet).

The capability and transform tests then went green. Their discriminating power
was verified by mutating the implementation, one defect at a time:

```text
P1  drop off/allergen filter (startProgram)   -> caught
P2  drop off/allergen filter (settings)       -> caught
P3  supports_light accepts any value set      -> SURVIVED
P4  writable_modes ignores the settings set   -> caught
P5  error normalization disabled              -> caught
P6  filter clamp removed                      -> caught
P7  custom aroma skips its timing check       -> caught
P8  value validation disabled                 -> caught
P9  timing bounds disabled                    -> caught
P10 bool coercion removed                     -> caught
```

P3 was a real hole: nothing tested a light declaring a DIFFERENT value set, only
one missing the parameter entirely. The plan requires the exact `{"0","1","2"}`
schema, since reusing the observed three-level mapping against a two-level
device would send an undeclared value. Added
`test_a_differently_shaped_enum_disables_its_feature` over five wrong-shaped
light/lock/tone sets; re-running P3 (extended to lock and tone) now produces
five failures.

## Deviations from the plan

- **`fan`/`light` are NOT added to `PLATFORMS` in this task**, contrary to
  Step 4. They are added by Tasks 5 and 6, in the same commit that creates each
  module, so every commit stays runnable on a real instance. Enforced rather
  than merely noted: `test_every_declared_platform_has_a_module` fails if a
  platform is listed before its module exists, closing the blind spot that let
  the stubbed suite stay green.
- **`tests/test_command_dispatch.py` was modified** although it is not in this
  task's file list. The dormancy guard had to be widened to allow
  `air_purifier.py`, since this plan is what deliberately wakes the dispatcher.
  Its docstring now states the narrowed meaning: no LEGACY path may reach the
  dispatcher, and adding a module to the allow-list is a declaration that its
  writes are transactional.

## Plan gap closed: the eighth intent

Task 9 requires a patch carrying `aromaStatus=4` plus ONLY the changed timing
key, but its file list does not include `air_purifier.py`, and the seven shapes
listed in this task's Step 5 contain no single-timing variant. Leaving the gap
would have forced Task 9 to assemble a CommandPatch inside `number.py`,
bypassing the module that owns intent construction.

Re-reading Task 9's Step 6 shows the shape is fully specified there, so it was
not a guess to make. Added as an eighth action, `set_aroma_time`:

- emits `aromaStatus=4` plus only the timing fields actually supplied, so the
  untouched sibling is never pushed over a concurrent change to it;
- requires at least one timing value, since no timing is not a change;
- gated on `supports_custom_aroma`;
- deliberately does NOT check whether Custom is the currently active mode. That
  is live state, which this module does not read. The caller gates on it and
  refuses the action, so the patch can never switch the mode as a side effect --
  exactly what Task 9's "raises a localized error rather than switching mode"
  requires.

Mutation-verified: emitting the sibling unconditionally (either direction),
accepting a no-op call, dropping the capability gate, and omitting the required
status are each caught by a test.

## Verification

```text
python3 -m pytest tests/test_air_purifier_contracts.py -q
45 passed

python3 -m pytest -q
1281 passed, 1 skipped, 7 xfailed, 302 subtests passed
```

Baseline before this task was 1244. `compileall` and `git diff --check` clean.
`rg` for the observed model names, serials, and MAC addresses finds nothing in
the module, and `test_air_purifier_never_reads_a_model_or_nickname` enforces it
over identifiers and string literals via `ast` rather than raw text, so
docstring prose can neither trip it nor mask a real `appliance.model_name` read.

## Self-Review (no subagent confutator pool this campaign)

- **Soundness**: verified the settings command is resolved ONCE for the whole
  capability set, so a settings patch can never be split across `settings` and
  `setParameters`; verified `_attr_value` treats an empty string as unreported
  while preserving a legitimate 0 or False; verified `ap_patch` validates
  `turn_on` against `start_modes` and `set_preset` against `preset_modes`
  (each command declares its own enum) while only the intersection is ever
  offered.
- **Coverage**: 10 implementation mutations, 9 caught, the survivor closed and
  re-verified. Capability isolation is covered per parameter and per whole
  command.
- **Adversarial**: the module asserts schema-consistency, not device truth. An
  inverted light mapping or a wrong aroma ordinal would pass everything here;
  only the deferred live validation can catch that.

## Residual Notes

- `_checked_time` validates the range bounds but not the step grid, so a value
  like `30.5` on a step-1 range is accepted here and rejected later by
  `HonParameterRange`'s setter. The rejection is safe (the dispatcher rolls back
  and no payload is sent) but late; snapping was deliberately not used, since it
  would silently change the value the user asked for.
- `environment_available` returns False when `onOffStatus` is absent. That is
  the conservative reading of the acceptance criterion "off-state sentinels are
  not shown as measurements": an unreported power state is not a confirmation.
