# Air Purifier Task 10 Report

## Status

Complete on `dev`.

- Commit: `71a8bbf` `feat: diagnose purifier capabilities`
- Owned production files: `diagnostics.py`
- Owned tests: `tests/test_diagnostics.py`, `tests/test_command_dispatch.py`
- Unplanned but required: `air_purifier.py`, `tests/test_air_purifier_entities.py`,
  `tests/conftest.py`, **new** `tests/test_stub_hygiene.py`, plus getattr guards in
  `tests/test_switch_params.py`, `tests/test_wash_option_params.py`,
  `tests/test_program_options.py`, `tests/test_ac_write_path.py`,
  `tests/test_entity_availability.py`, `tests/test_tier2_sensors.py`

## Impact analysis

Grep (GitNexus remains unusable):

- `_mapped_sets` has one caller, `_coverage`, which has one caller,
  `_appliance_block`. The AP branch is additive and after the AC / wash-group ones.
- `_appliance_block` gains one key. Every existing assertion on the block is by key,
  so no other test sees a change.
- `command_diagnostics.py` was NOT modified: see below.

## What was built

**AP coverage registration.** `air_purifier.AP_ENTITY_PARAMS` names every parameter
an AP entity reads as state or writes as a command field. The AP fan, light, aroma
select, toggles and timing numbers are fixed-key entities or live outside the
`NUMBERS` / `_SETTINGS_SWITCHES` tables, so the existing registry walk could not see
them and the dump reported eight already-controlled parameters as unmapped, sending a
maintainer chasing controls that exist. Both axes are registered, because each name
is both read and written.

**`future_capabilities`**, a bounded section carrying the two signals `coverage`
cannot express, because they concern parameters the integration DOES map:

- `enum_deltas` - values a command schema declares that no entity handles, keyed
  `<command>.<param>`;
- `state_values_unhandled` - a value the device is reporting RIGHT NOW that no
  mapping covers, e.g. an active `machMode=3`.

Driven by `air_purifier.AP_HANDLED_VALUES`, a per-parameter registry of the raw
values this code handles, derived from the existing mapping constants rather than
duplicated. A type with no registry gets `{}`, not guesses. Truncation is announced
with a `truncated` flag instead of silently applied.

A writable parameter with NO entity at all is deliberately not repeated here: it is
already `coverage.command_params_unmapped`.

## Step 5 needed no production change, and that is the finding

The plan asked for AP command diagnostics carrying the action string, the
requested/mandatory/rule-added split, latency and unexpected shadow fields, without
identity. The transactional dispatcher already emits exactly that for every patch,
and `ap_patch` already names each action `ap_*`. Both new tests passed on the first
run against unmodified production code.

They are kept rather than deleted: nothing previously proved the AP path produced
correlated events, and `test_ap_diagnostic_records_carry_no_appliance_identity` goes
through the REAL emitter (not a captured stub) to prove the encoded records carry no
appliance id, model, serial or MAC.

## The control mutation fired again, and found a second and third defect

Task 9 exposed one unguarded stub clobber. This task's first mutation run reported
all 13 mutations as caught, control included, so it was discarded again. Two more
causes, both pre-existing:

1. `test_diagnostics.py` clobbered `SwitchEntity` / `SelectEntity` / `ButtonEntity`
   unconditionally, same defect as V11.
2. **`BinarySensorDeviceClass` had no `SAFETY` member** in that module's stub. Every
   test module declared its own subset of the device/state class enums, so which
   MEMBERS existed depended on collection order: the Task 9 assertion that the CO
   entity is never a safety device failed with `AttributeError` in this subset. The
   VALUES diverged too (`co2` against the real `carbon_dioxide`), which would have
   let a wrong device class pass unnoticed.

Fixed by moving `SensorDeviceClass`, `SensorStateClass` and
`BinarySensorDeviceClass` into conftest with REAL Home Assistant values, guarding the
four remaining unconditional assignments across three files, and converting the four
`CoordinatorEntity` FORCE-ASSIGNS. Those force-assigns predate conftest; their own
comments explain they existed because another module might install a base without
`available`. conftest solved that centrally, and each of the four is now a strict
SUBSET of conftest's class (no `unique_id`, no `_handle_coordinator_update`), so they
had turned from a workaround into the hazard they were written to prevent.

**New `tests/test_stub_hygiene.py`** makes the class of bug fail immediately in any
order: an AST guard asserting no test module assigns a shared stub symbol outside the
first-wins `getattr` idiom (accepting the two-step
`base = getattr(...)` / `mod.X = base` form), plus a test proving the guard actually
flags the pattern it forbids, plus a test that every listed symbol is really
installed by conftest so the list cannot go stale.

## TDD Evidence

RED before implementation: 8 failed / 4 passed (diagnostics), and the two dispatcher
tests green from the start (see above).

Mutations, 12 real plus 1 control:

```text
D1  AP params not registered as mapped   -> caught
D2  only the attribute axis registered   -> caught
D3  AP_ENTITY_PARAMS loses a control     -> caught (2)
D4  handled values registry ignored      -> caught (6)
D5  enum deltas report the whole enum    -> caught (2)
D6  range params enumerated too          -> SURVIVED, then closed
D7  unhandled live state never reported  -> caught
D8  value bound removed                  -> caught
D9  truncation not announced             -> caught
D10 integral float no longer normalized  -> SURVIVED, then closed
D11 future section emitted for any type  -> caught
D12 AP fan offers an undeclared mode     -> caught (4)
D13 CONTROL no-op comment edit           -> SURVIVED (correct)
```

**D10** was a plain test gap: nothing fed a numeric state value, so the
integral-float normalization was untested. Without it a client handing back `2.0`
instead of `"2"` turns a fully handled value into a phantom future capability. Closed
by `test_future_capability_normalizes_a_numeric_state`.

**D6** was a survivor for a more interesting reason: the range guard was
unreachable because no range parameter is in the handled registry, AND the test
fixture's `FakeParam` did not expose `.values` for a range, so even a patched
registry produced nothing. Unlike Task 6's L6/L8 the code was NOT removed: the hazard
it prevents is documented in this very file (`.values` on a `HonParameterRange`
enumerates up to 100000 strings synchronously on the event loop). Instead the FAKE
was made faithful - a range parameter now enumerates its grid, as the engine's
property does - and a test patches the registry to include a range parameter and
asserts no delta is produced. A fake that is more forgiving than production is a test
gap of its own.

## Verification

```text
python3 -m pytest tests/test_diagnostics.py -k "air_purifier or future_capability" -q
15 passed

python3 -m pytest tests/test_command_dispatch.py -k ap_diagnostic -q
2 passed

python3 -m pytest tests/test_diagnostics.py tests/test_log_identity_redaction.py \
  tests/test_command_dispatch.py -q
121 passed

python3 -m pytest -q
1487 passed, 1 skipped, 7 xfailed, 302 subtests passed

python3 -m compileall -q custom_components/addhon tests   # clean
```

Baseline before this task was 1462.

## Self-Review (no subagent confutator pool this campaign)

- **Soundness**: verified `future_capabilities` reads only parameter names and raw
  scalars, so identity cannot reach it even indirectly (a test greps the encoded
  section for the fixture's MAC, serial, model and id); verified an unknown parameter
  or an extra enum value creates no entity, no option and no write, on all five AP
  platforms; verified the section is empty for a type with no registry rather than
  half-populated.
- **Coverage**: coverage registration on both axes and its drift guard, five
  future-capability behaviors plus bounding and truncation announcement, numeric
  normalization, range refusal, and six passive-capture entity assertions.
- **Adversarial**: `AP_ENTITY_PARAMS` is a hand-maintained list. The drift guard
  derives the switch and number tables and asserts containment, but the fan, light
  and select params are fixed-key and can only be checked by reading the code. A new
  AP control whose parameter is forgotten there resurfaces as a phantom coverage gap,
  which is a cosmetic wrong-report rather than a functional bug. Added to the ledger.

## Residual Notes

- `state_values_unhandled` covers only the six enum-ish AP parameters in the handled
  registry. A range parameter reporting an out-of-range value is not captured; the
  raw value is in `attributes` regardless.
- The AP section makes `future_capabilities` a per-type opt-in. No other type
  declares a handled-value registry today, so the key is `{}` for all of them.
- Ledger B1-B4 are now VISIBLE in a real dump rather than only recorded here:
  `no2ValueIndoor` shows in `attributes_unmapped`, `humidificationStatus` and
  friends in `command_params_unmapped`.
