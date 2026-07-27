# Air Purifier Task 6 Report

## Status

Complete on `dev`.

- Commit subject: `feat: control air purifier panel light`
- Owned production files: `light.py` (new), `const.py`, `air_purifier.py`, `translations/en.json`, `translations/it.json`
- Owned tests: `tests/conftest.py`, `tests/test_air_purifier_entities.py`
- Unplanned but required: `tests/test_command_dispatch.py`, `tests/test_entity_translation_keys.py` (same reasons as Task 5)

## What was built

`HonAirPurifierLight`, a brightness-only light over the panel's three INVERSE
levels (raw `2` off, `1` half, `0` full). Created only when the schema declares
exactly those three values AND the device reports the state, so a two- or
four-level device gets nothing rather than an undeclared value sent to it.

- `_quantize` picks the nearest supported level, ties going to the BRIGHTER one.
  The only integer tie is 64 (equidistant from 0 and 128); rounding up keeps a
  "make it a bit dimmer" request from crossing all the way to off.
- `async_turn_on(brightness=...)` raises a quantized off-level result to the
  dimmest LIT level: a turn-on must leave the panel on. A bare `turn_on()`
  restores the last lit level from `AP_LAST_LIGHT_STORE`, defaulting to full after
  a reload.
- `async_turn_off()` sends the off level.
- `_levels` is fixed at construction from the schema, so a shadow value outside
  the declared set can never add or drop a level.

Every write carries `lightStatus` alone, via `ap_patch` + `async_dispatch_patch`.

## New shared helper

`air_purifier.reports_attribute(attributes, key)` was added and exported. The
setup gate needs the same "reported" semantics the entity reads with: a
present-but-EMPTY attribute means not available, so a plain `key in attributes`
check would create an entity that can only ever read unknown. Tasks 7 and 8 gate
lock, tone and aroma the same way, which is why this is a helper rather than four
more booleans on the capabilities dataclass.

## Two mutation survivors, both revealing DEAD CODE

Ten mutations, eight caught. The two survivors were not test gaps: they were
unreachable production code that looked like protection.

```text
L1  exact three-level schema no longer required  -> caught (2)
L2  read state no longer gated                   -> caught (2)
L3  tie rounds DOWN to the dimmer level          -> caught
L4  turn_on can switch the panel off             -> caught
L5  off level stored as the restore level        -> caught (2)
L6  undeclared level stored as restore level     -> SURVIVED (dead code)
L7  bare turn_on never restores                  -> caught (2)
L8  out-of-range not clamped                     -> SURVIVED (dead code)
L9  no refresh after a command                   -> caught
L10 levels hardcoded (deliberate control)        -> correctly passed
```

- **L6**: `_remember_level` had two guards, `brightness is None` then
  `brightness not in self._levels`. The second can never fire: `supports_light`
  requires exactly `{"0","1","2"}`, so `_levels` always covers the whole
  `AP_LIGHT_TO_BRIGHTNESS` range and any undeclared raw already maps to None.
  Collapsed both plus the off-level check into ONE membership test against a
  `_lit_levels` set derived from the schema. Same behavior, no unreachable
  branch, and still correct if `supports_light` is ever widened.
- **L8**: `_quantize` clamped its input to 0-255 before searching. Also
  unreachable in effect: the nearest level to a value below 0 or above 255 is the
  boundary level anyway. Removed the clamp and kept the test, which asserts the
  BEHAVIOR (out-of-range input yields a declared level) rather than the mechanism.

Both reworked lines were re-mutated afterwards and now fail, so neither is
load-bearing-by-accident.

L10 was a deliberate control (hardcoding `[0, 128, 255]` produces the identical
list) and correctly passed, confirming the harness does not manufacture failures.

## A test-fixture bug found the honest way

Twelve light tests failed at first because `FULL_ATTRIBUTES` had no `lightStatus`
at all: Task 3 only needed sensor attributes and Task 4 added `ecoModeStatus`.
The entity was correctly NOT created; the tests were wrong. Adding the writable
control attributes (`lightStatus`, `lockStatus`, `touchToneStatus`, `aromaStatus`,
`aromaTimeOn`, `aromaTimeOff`) makes the shared fixture a complete purifier and
leaves the Task 3/4 counts untouched, since none of them is a sensor or binary
source key.

## Verification

```text
python3 -m pytest tests/test_air_purifier_entities.py -q
85 passed

python3 -m pytest -q
1366 passed, 1 skipped, 7 xfailed, 302 subtests passed
```

Baseline before this task was 1342. `compileall`, `git diff --check` and
`json.tool` on both translation files all clean. `light` added to `PLATFORMS` in
the commit that creates `light.py`, closing Task 2's deferral V2 (both `fan` and
`light` are now declared, each alongside its module).

## Self-Review (no subagent confutator pool this campaign)

- **Soundness**: verified the tie rule is implemented as one `max()` over
  `(-distance, level)` rather than two comparisons, so "nearest" and
  "ties-to-brighter" cannot disagree; verified `is_on` derives from `brightness`
  so the two can never contradict; verified the store holds Home Assistant
  brightness rather than raw values, so the INVERSE encoding is applied in exactly
  one place.
- **Coverage**: three setup gates, the full inverse mapping in both directions,
  quantization boundaries and the tie, out-of-range input, turn-on floor,
  reload default, restore path, all three values that must NOT be restored,
  sparse payloads, and the localized error path.
- **Adversarial**: the whole entity rests on the design's claim that the encoding
  is inverse. If raw `0` is actually off, every test here still passes and the
  panel behaves exactly backwards. That is ledger item L5 and is the second
  highest-value check on a real device after the filter direction.

## Residual Notes

- `supported_brightness_levels` is a custom property, not a Home Assistant
  concept. It exists so a test can assert the level set never resizes; Home
  Assistant itself ignores it.
- `brightness` returns 0 rather than None while the panel is off. Truthful for
  this device, and Home Assistant does not surface brightness for an off light.
- The `unique_id` suffix is `panel_light`; renaming it later orphans registered
  entities (ledger P7).
