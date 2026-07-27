# Air Purifier Task 4 Report

## Status

Complete on `dev`.

- Commit subject: `feat: expose air purifier status signals`
- Owned production files: `binary_sensor.py`, `translations/en.json`, `translations/it.json`
- Owned tests: `tests/test_air_purifier_entities.py`

## The eco attribute is evidence-backed, not guessed

The design names the entity "eco active" but never names the attribute behind it,
and the plan does not either. Rather than inventing a plausible key, the
decompiled-app analysis already in the repository was consulted:
`apk/analysis/deep/climate-air.md:362` carries the full `AP_PARAMS_ENUM`
(`decomp.txt:1850910-1850958`), which lists `ecoModeStatus` and classifies it
under "Toggles" alongside `touchToneStatus`, `lightStatus` and `lockStatus`.

That same enum independently confirms every attribute Task 3 used
(`airQuality`, `pm2p5ValueIndoor`, `pm10ValueIndoor`, `vocValueIndoor`,
`coLevel`, `humidityIndoor`, `temp`, `mainFilterStatus`, `preFilterStatus`,
`totalWorkTime`, `pollenLevel`, `windSpeed`), so Task 3's table is corroborated
by a second source.

## What was built

Two capability-gated binary sensors for `AP`:

- `eco_active` from `ecoModeStatus`, a plain toggle: on for raw `1` only.
- `problem` from `errors`, PROBLEM device class, state derived through
  `has_problem()`. Not compared to a literal: the device spells "no error" as
  numeric zero, `"0"`, `"00"` and `"100"` interchangeably, so any single
  `on_value` would report a healthy purifier as faulty.

Neither is gated on power. Both readings are reported and meaningful with the
purifier stopped, which matches the design's availability rules; the power gate
belongs to the environmental sensors only.

`HonBinarySensorEntityDescription` gains an optional `value_fn`, consumed by
`HonBinarySensor.is_on` only when set. The existing `raw is None -> None` guard
runs FIRST and was deliberately kept ahead of `value_fn`, so a missing reading
stays unknown instead of being folded into a False. `has_problem(None)` would
return False, which reads as "no problem" and would present absent data as a
healthy device.

Every existing description leaves `value_fn` at None and is unchanged by
construction, pinned by `test_the_value_fn_field_defaults_off_everywhere_else`.

## Translation keys

`eco_active` and `problem` are new in both `en.json` and `it.json`, inserted
textually before the account-level `update_ok` entry (6 additive lines per file).

`energy_saving` already exists and renders "Energy saving", but the design and the
plan's Interfaces section both specify `eco_active`, so it was not reused; the two
are different attributes on different appliance types.

## TDD Evidence

RED before implementation: 11 failed, 22 passed.

Discriminating power verified by mutating the implementation:

```text
B1 problem compares one literal instead of has_problem -> caught (2)
B2 is_on ignores value_fn                              -> caught
B3 missing reading no longer reports unknown           -> caught
B4 eco reads a wrong attribute name                    -> caught (6)
B5 problem loses its device class                      -> caught
B6 AP not registered in BINARY_SENSORS                 -> caught (10)
```

All six caught; `binary_sensor.py` restored from a pristine copy after each.

## Findings for the backlog

Two gaps between the app evidence and the design's scope. Neither was silently
fixed: both are outside this task's and Task 3's frozen entity lists.

- **`no2ValueIndoor` is unmapped.** `AP_PARAMS_ENUM` declares it and the same
  analysis lists NO2 among the AP read sensors, but the design's measurement
  section and the plan's Task 3 sensor list both omit it. Task 3 shipped thirteen
  sensors with no NO2. Since sensors are capability-gated, adding it would be
  harmless on a device that does not report it and would cover one that does.
  Belongs in Task 10's future-capability capture at minimum, or a Task 3
  amendment.
- **`errors` is not in `AP_PARAMS_ENUM`.** The design states the AP exposes a raw
  normalized error code, and both the Task 3 `errors` sensor and this task's
  `problem` binary read it. The absence is INCONCLUSIVE rather than
  contradictory: `machMode` and `onOffStatus` are also missing from that enum,
  and the same document documents them as shared cross-type params, so the enum
  is the AP-specific namespace and not the full attribute set. Still, both
  entities are capability-gated, so if a real purifier does not report `errors`
  they simply never appear -- which means the error normalization is one of the
  things the beta's user-collected diagnostics have to confirm.

Also declared by the enum and deliberately out of scope: `humidificationStatus`
and `humiditySelHigh/Med/Low` (the design's non-goals exclude humidifier support
where the capability is absent), `aromaPreferredSetting`, and the undocumented
`transMode` / `highTransRate` / `stdTransRate`. Task 10's passive capture is
where those should surface rather than becoming guessed-at controls.

## Verification

```text
python3 -m pytest tests/test_air_purifier_entities.py -q
33 passed

python3 -m pytest -q
1314 passed, 1 skipped, 7 xfailed, 302 subtests passed
```

Baseline before this task was 1302. `json.tool` clean on both translation files.

## Self-Review (no subagent confutator pool this campaign)

- **Soundness**: verified the None-guard precedes `value_fn` so missing data
  cannot masquerade as a healthy device (B3 pins it); verified `has_problem` is
  referenced as the shared function rather than reimplemented, so the error
  vocabulary stays defined in exactly one place; verified the AP entry does not
  disturb the universal `connectivity` and `remoteCtrValid` gating, which still
  apply to AP.
- **Coverage**: table shape, both attribute names, device class, gating per
  attribute in isolation, a bare device, eco on/off/other, all five normal error
  spellings, three real error codes, empty-string readings, availability while
  off.
- **Adversarial**: `ecoModeStatus` comes from the decompiled app, not from a live
  purifier. If the shipped firmware named it differently the entity would simply
  never be created -- silent, benign, and invisible to every test here. The beta
  diagnostics should be checked for an eco-like attribute that went unmapped.
