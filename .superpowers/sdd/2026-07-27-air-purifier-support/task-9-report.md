# Air Purifier Task 9 Report

## Status

Complete on `dev`.

- Commit: `ab0d5b9` `feat: gate experimental purifier controls`
- Owned production files: `const.py`, `config_flow.py`, `__init__.py`, `sensor.py`,
  `binary_sensor.py`, `number.py`
- Owned tests: `tests/test_options_flow.py`, `tests/test_air_purifier_entities.py`
- Unplanned but required: `air_purifier.py`, `tests/conftest.py`,
  `tests/test_command_dispatch.py`, `tests/test_entity_translation_keys.py`,
  `tests/test_number_setpoints.py`, `tests/test_sensor_per_type.py`,
  `tests/test_tier2_sensors.py`

## Impact analysis

Grep (GitNexus remains unusable, see the ledger's process notes):

- `OptionsFlowHandler.async_step_init` is the only writer of `entry.options`; the
  config (not options) flow is untouched.
- `_async_options_updated` is registered once via `entry.add_update_listener`. Its
  only callers are HA itself and the two existing listener tests.
- `sensor.async_setup_entry` / `binary_sensor.async_setup_entry` /
  `number.async_setup_entry` each gained ONE read of `entry.options`. Three test
  doubles for a ConfigEntry had no `options` attribute at all and were completed.
- `number.NUMBERS` gains no entry, so no cooling/oven/wine number changes. Pinned
  by `test_no_other_appliance_gains_a_number`.

## What was built

**The option.** `CONF_ENABLE_EXPERIMENTAL`, third toggle of the same screen, default
false. The submitted payload is now built as `{**options, ...}`: the screen REPLACES
`entry.options` wholesale, so a literal three-key payload silently dropped any key
it does not render.

**Targeted reload.** `_debug_opts` became `_entry_opts` and returns a triple. The
listener now acts per half instead of returning early on one comparison:

- the loggers are re-applied only when one of the first two values moved, so an
  experimental-only change cannot clobber a DEBUG level raised at runtime;
- the entry is reloaded only when the third moved, because that is the only option
  that changes which entities exist;
- no baseline in `hass.data` means nothing is KNOWN to have changed: the levels are
  applied as before, but a reload is never issued on a guess (that would risk a
  reload loop at setup).

**`air_quality_label`** (experimental sensor). ENUM over the confirmed labels only,
which today is `{"0": "good"}`. Every other ordinal leaves it unavailable rather
than publishing a guess next to the raw `air_quality` number, which is already
correct. Power-gated like the raw reading it interprets.

**`co_alarm`** (experimental binary). On for the single observed alarming value,
UNAVAILABLE for everything else: the values meaning "no carbon monoxide" have not
been observed, so `co_alarm()` deliberately never returns False. Diagnostic
category, and no device class at all, since `BinarySensorDeviceClass.SAFETY` would
present it as a certified detector.

**`aroma_time_on` / `aroma_time_off`** (experimental numbers). SECONDS, per the
design (observed range 1..3600, where 60 means one minute); bounds and step read
from the live parameter on every access, never from a literal. Available only while
Custom is the live aroma mode AND the purifier is running, and the write refuses
outside Custom rather than trusting the UI to have hidden the entity: the patch has
to repeat `aromaStatus=4` for the device to accept a timing change, so permitting it
in another mode would switch the aroma mode as a side effect. The sibling timing is
never included. Unique IDs are `<id>_aroma_time_on` / `_off`, with no support level
encoded, so promoting the entity later keeps its identity.

Two new description flags, both defaulting off so no existing entity changes:
`experimental` (exists only with the option) and `unavailable_when_unmapped` (hides
instead of reporting a value it could not interpret).

## Mixed-file guard extended

`number.py` is the third mixed file. Added to the dispatcher allow-list AND to
`test_mixed_platform_legacy_classes_keep_the_legacy_sender`, which now pins six
classes across three files. `HonProgramOptionNumber` is pinned on `self._buffer(`:
it never sends, so the buffering IS its write path and losing it would be the same
regression as losing a sender.

## A latent order-dependence bug, found by the control mutation

The first mutation run reported all 23 mutations as caught, INCLUDING the deliberate
no-op control. That is the harness accusing itself, so the run was discarded.

Cause: `test_entity_translation_keys.py` assigned `SwitchEntity`, `SelectEntity` and
`ButtonEntity` UNCONDITIONALLY, clobbering conftest's complete stubs. Entity classes
bind whatever base is installed the first time their module is imported, so in any
collection order where that module is imported first, `HonAirPurifierAromaSelect`
ended up with a bare base and no `options` property: 1 pre-existing failure in the
four-file subset, invisible in the full suite. The three assignments are now
getattr-guarded like every other stub in that file.

Without a control mutation this would have been read as 23 confirmations.

## TDD Evidence

RED before implementation: 15 failed / 17 passed (options), then 41 failed /
125 passed (entities).

Mutations, 22 real plus 1 control:

```text
E1  options payload drops pre-existing keys   -> caught
E2  experimental never read into the snapshot -> caught (4)
E3  reload triggered by a debug change        -> caught (5)
E4  loggers re-applied on any change          -> caught
E5  sensor experimental gate removed          -> caught (3)
E6  air_quality_label maps every value        -> caught
E7  sensor unmapped reading stays available   -> caught
E8  label no longer power gated               -> caught (2)
E9  co_alarm reports an all-clear             -> caught
E10 co_alarm becomes a safety device          -> caught (2)
E11 co_alarm loses its diagnostic category    -> caught
E12 binary unmapped reading stays available   -> caught
E13 binary experimental gate removed          -> caught (2)
E14 numbers ignore the experimental option    -> caught
E15 numbers accept any aroma support          -> caught (2)
E16 number read half no longer gated          -> caught
E17 numbers available outside custom          -> caught
E18 numbers available with the purifier off   -> caught
E19 write no longer refuses outside custom    -> caught (2)
E20 on/off intent fields swapped              -> caught (3)
E21 bounds frozen at the setup snapshot       -> SURVIVED, then closed
E22 no refresh after a timing write           -> caught
E23 CONTROL no-op comment edit                -> SURVIVED (correct)
```

**E21 was a genuine test gap.** The live re-read in `_live_range` was unobservable
because the fallback snapshot is derived from the same schema at discovery, so a
narrower schema narrowed both. Closed by
`test_the_bounds_follow_the_parameter_after_setup`, which moves the parameter's
bounds AFTER construction: the real scenario is an engine rule changing min/max/step
while the entry stays loaded, and a stale range would let the UI submit a value the
device now rejects. Not dead code (unlike Task 6's L6/L8) - just untested.

E19 also trips `test_no_orphan_exception_keys`: removing the only raise site for
`aroma_custom_not_active` leaves an untranslatable orphan. Two independent guards.

## Verification

```text
python3 -m pytest tests/test_options_flow.py tests/test_air_purifier_entities.py -q
201 passed

python3 -m pytest tests/test_number_setpoints.py tests/test_wash_option_params.py -q
40 passed

python3 -m pytest -q
1462 passed, 1 skipped, 7 xfailed, 302 subtests passed

python3 -m compileall -q custom_components/addhon tests   # clean
```

Baseline before this task was 1401. `json.tool` clean on both translation files;
translation diffs are 26 lines each, purely additive apart from the options step
title (see below).

## Self-Review (no subagent confutator pool this campaign)

- **Soundness**: verified the listener cannot reload without a recorded baseline,
  cannot reload twice for one change (the snapshot is updated first), and cannot
  reset a runtime debug level for an experimental-only change; verified `co_alarm`
  has no code path returning False, so "never claims an all-clear" is structural
  rather than a matter of which raw values the tests happen to use; verified the
  timing patch carries neither `onOffStatus` nor `machMode`, so a timing change can
  never start the appliance.
- **Coverage**: option default/persistence/unknown-key survival, four reload
  scenarios plus two no-reload ones, both experimental entities in created/absent
  x confirmed/unconfirmed x powered/stopped combinations, six number setup gates,
  live bounds before and after a rules change, both write directions, four refusal
  paths, and the architecture split.
- **Adversarial**: the CO alarm's raw `2` and the AQ label's raw `0` are both
  single observations. If either is wrong the entity is simply always unavailable,
  which is the failure mode chosen on purpose, but a WRONG mapping (raw 2 meaning
  something benign) would raise a false alarm. Added to the ledger. Separately, the
  options step title said "Debug options" and is now "Options": leaving it would
  have mislabelled the whole screen, and no test asserts the title.

## Residual Notes

- `ap_patch` validates the requested timing against the CAPABILITY snapshot, while
  the entity reports bounds from the live parameter. A rules change that widens the
  range after setup is therefore offered by the UI and rejected by the intent
  builder, surfacing as a localized `command_error`. Fails closed; recorded as a
  LIMIT rather than duplicating range resolution into `ap_patch`.
- `unavailable_when_unmapped` reaches `native_value` / `is_on` from `available`, so
  the value function runs twice per state write. Both are pure dictionary lookups.
- `tests/test_air_purifier_contracts.py:210` hardcodes `HHP50CA011` on a trap
  fixture (Task 1). Deliberate, but Task 13's evidence sweep forbids the string in
  tracked test code; queued for Task 12, which owns that file.
