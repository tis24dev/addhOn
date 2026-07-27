# Air Purifier Task 3 Report

## Status

Complete on `dev`.

- Commit subject: `feat: expose air purifier telemetry`
- Owned production files: `sensor.py`, `translations/en.json`, `translations/it.json`
- Owned tests: `tests/test_air_purifier_entities.py` (new)

## Impact analysis

GitNexus remains unusable here (same graph-identity defect recorded in Tasks 1-2),
so grep and the suite were the ground truth.

- `HonSensorEntityDescription` gains one field with a default, so every existing
  description is unchanged by construction. Pinned by
  `test_no_other_appliance_type_requires_power`.
- `HonSensor.available` is NOT touched. The AP behavior lives in a subclass, so
  no existing entity's availability can shift.
- `sensor.async_setup_entry` gains one class selection on `app_type`; every
  non-AP type still builds a plain `HonSensor`.
- `SENSORS` gains one key. The three sibling sensor test modules assert on
  hardcoded per-type key lists, none of which enumerate AP, so none are
  affected -- confirmed by running them explicitly (118 passed).

## What was built

Thirteen capability-gated read-only sensors for `AP`, matching the design's
measurement list: indoor temperature, indoor humidity, PM2.5, PM10, VOC, raw air
quality, wind speed, main-filter life, pre-filter cleaning, total work time,
normalized error, raw CO, pollen level.

Units follow the convention the AC air-quality block already established rather
than inventing a parallel one:

- temperature `°C` + TEMPERATURE, humidity `%` + HUMIDITY, PM2.5/PM10 `µg/m³`
  with their real device classes, all MEASUREMENT.
- VOC, air quality, wind speed, CO and pollen stay bare integers, class-less AND
  unit-less. They are small level indexes in the official app, not concentrations
  and not a 0-500 AQI; attaching either would assert a measurement the device
  never reports.
- Both filters: `%`, MEASUREMENT, `mdi:air-filter`, and deliberately NO device
  class. A BATTERY class would let Home Assistant read a dirty filter as a low
  battery. `value_fn=filter_remaining` inverts the device's CONSUMED percentage.
- Total work time: minutes + DURATION + TOTAL_INCREASING, which already tolerates
  the observed behavior of flushing accumulated minutes at power-off.
- Errors: `value_fn=normalize_error`, so `0`, `"0"`, `"00"` and `"100"` all read
  as `"0"` while a real code like `"E12"` passes through.

Availability is declared as data, not code: `requires_power` on the description,
consumed by `HonAirPurifierSensor.available`, which returns
`super().available and environment_available(self._attributes)` only for flagged
descriptions. The base rules always run first, so the flag can only narrow
availability, never widen it. The seven live-telemetry readings hide while the
purifier is off; filter, work-time, error, CO and pollen stay.

## Translation keys

Nine of the thirteen sensors reuse translation keys that already exist
(`temp_indoor`, `humidity_indoor`, `pm25`, `pm10`, `voc`, `air_quality`,
`fan_speed`, `errors`, `co`), so the AP entities read the same as their AC
siblings instead of introducing parallel wording.

Four are genuinely new and were added to both `en.json` and `it.json`:
`filter_life`, `filter_cleaning`, `total_work_time`, `pollen_level`.

**Plan ordering note:** Task 11 is nominally where translations land, but
`test_entity_translation_keys.py::test_code_keys_match_translations_exactly`
asserts EXACT set equality between code keys and JSON, per language, so any task
introducing a key must translate it in the same commit. Task 11 becomes a
completeness and wording pass, not the first appearance.

A first attempt round-tripped the JSON through `json.dump` with the sensor block
re-sorted, producing a 444-line diff per file. Reverted: the block is grouped by
appliance type, not alphabetical. The four entries were inserted textually as an
AP group before the account-level diagnostics, giving a 12-line purely additive
diff per file.

## TDD Evidence

RED before implementation: 19 failed, 2 passed, on `KeyError: 'AP'` and
`'HonSensorEntityDescription' object has no attribute 'requires_power'`.

The table and availability tests then went green. Discriminating power verified by
mutating the implementation, one defect at a time:

```text
S1  temp_indoor loses requires_power          -> caught (3)
S2  available ignores the power gate          -> caught (2)
S3  available skips the base rules            -> caught
S4  filter misclassified as a battery         -> caught
S5  filter reports raw consumption            -> caught (2)
S6  work time not total_increasing            -> caught
S7  a description is not gated                -> caught (3)
S8  voc invents a concentration unit          -> caught
S9  setup wires the plain sensor class        -> caught (2)
S10 errors not normalized                     -> caught (2)
```

All ten caught; `sensor.py` restored from a pristine copy after each and the
working tree verified afterwards.

## Clarity fix found by review

The AP wind-speed row initially reused the `AC_FAN_PARAM` constant, since both
resolve to the string `windSpeed`. That constant names the air conditioner's
WRITABLE fan parameter, so seeing it in a read-only AP table implies the AP fan
speed is written through it, which is false: on the observed schemas `windSpeed`
is telemetry only. Replaced with an inline `"windSpeed"`, matching the module's
own stated convention that single-use telemetry keys stay inline.

`AC_ATTR_HUMIDITY_INDOOR` and `AC_ATTR_PM25` were kept, because those really are
the same cloud parameter shared with the AC table and a single source of truth
for the name is worth more than the prefix's implication.

## Deviations from the plan

- `tests/test_entity_translation_keys.py` is in this task's file list but needed
  no change: its assertions are derived from the code and the JSON, not a
  hardcoded key list, so adding the four translations was sufficient.
- Test stubs were written inline in the new test module, matching its three
  sibling sensor test files, rather than centralized in `conftest.py`. Task 5
  explicitly modifies `conftest.py` for the fan/light stubs; consolidating the
  sensor stubs there would change stub resolution for three existing files
  (they use first-wins `getattr` guards), which is a wider change than this task
  needs. Deferred to Task 5 deliberately.

## Verification

```text
python3 -m pytest tests/test_air_purifier_entities.py -q
21 passed

python3 -m pytest tests/test_sensor_per_type.py tests/test_tier2_sensors.py \
  tests/test_entity_translation_keys.py tests/test_entity_availability.py -q
118 passed

python3 -m pytest -q
1302 passed, 1 skipped, 7 xfailed, 302 subtests passed
```

Baseline before this task was 1281. `compileall`, `git diff --check` and
`json.tool` on both translation files all clean.

## Self-Review (no subagent confutator pool this campaign)

- **Soundness**: verified the power gate composes with, rather than replaces, the
  base availability rules (S3 pins it); verified an ABSENT `onOffStatus` hides
  the measurements, since an unreported power state is not a confirmation that
  the readings are live; verified `filter_remaining` returning None yields an
  unknown state rather than a fabricated 0 or 100.
- **Coverage**: gating (full device, partial device, empty device), units and
  classes per group, both filter clamp directions, all four normal error
  spellings plus a real code, availability in both power states and under
  coordinator failure.
- **Adversarial**: these tests assert the mapping is internally consistent, not
  that it matches the hardware. If `mainFilterStatus` were actually a REMAINING
  percentage rather than a consumed one, every test here would still pass and
  every filter reading would be inverted. Only the deferred live validation can
  settle that; it is the single highest-value thing to check on a real device.

## Residual Notes

- `test_all_tier2_descriptions_are_gated` in `test_tier2_sensors.py` iterates a
  hardcoded type list that does not include AP. AP gating is covered by this
  task's own file instead, so that list was left untouched.
- `fan_speed` is shared as a translation key with the AC's writable fan-speed
  sensor. Both render "Fan speed" and neither declares options, so the shared-key
  parity tests are satisfied, but the two entities mean slightly different things
  (an AP telemetry reading versus an AC setting readback).
