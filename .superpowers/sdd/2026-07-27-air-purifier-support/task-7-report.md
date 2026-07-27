# Air Purifier Task 7 Report

## Status

Complete on `dev`.

- Commit subject: `feat: control air purifier settings`
- Owned production files: `switch.py`, `translations/en.json`, `translations/it.json`
- Owned tests: `tests/test_air_purifier_entities.py`
- Unplanned but required: `tests/conftest.py`, `tests/test_command_dispatch.py`, `tests/test_entity_translation_keys.py`

## Impact analysis

Grep (GitNexus remains unusable, see the ledger's process notes):

- `switch.async_setup_entry` is an `if / elif` chain on `app_type`. AP was added as
  its own branch; the wash-group, `_SETTINGS_SWITCHES` and account-level branches
  are untouched, so no existing switch changes.
- `HonSettingsSwitch._set_param` is NOT touched. The AP toggles are a separate
  class, so no legacy switch's write path shifts.
- `_SETTINGS_SWITCHES` gains no entry. Pinned by
  `test_the_legacy_settings_switches_are_untouched`.

## What was built

`HonAirPurifierSwitch` plus `HonAirPurifierSwitchDescription`, for `lockStatus`
(child lock) and `touchToneStatus`. Both halves are gated: the write schema via the
capability (`supports_lock` / `supports_tone`, each requiring an exact `0/1` enum)
and the read state via `reports_attribute`. Neither is power-gated: a lock and a
beep setting are meaningful and changeable with the purifier stopped.

Deliberately NOT a `HonSettingsSwitch` subclass. That class applies the value to
the whole `settings` command and sends it through the legacy sender, which would
restate every sibling setting; the AP toggle dispatches a patch carrying its own
field alone. The description carries the capability property name and the
`ap_patch` action, so the table stays declarative.

`child_lock` reuses the air conditioner's translation key: same parameter name,
same meaning. `touch_tone` is new and was deliberately NOT folded into the AC's
`mute`, whose polarity is the opposite (mute on = sound off, touch tone on = sound
on).

## The allow-list weakening, and the guard that replaces it

`switch.py` had to join the dispatcher allow-list in
`test_dispatcher_has_no_production_entity_caller`, but it is the first MIXED file:
it holds an AP entity that dispatches AND legacy entities that intentionally send
whole commands. Allow-listing a file silences the dispatcher check for everything
in it.

`_EXPECTED_LEGACY_CALL_EDGES` does not close the gap either. Its tracked callees
are `async_send_command`, `run_command_sync` and `send`; `HonSettingsSwitch`
writes through `async_send_settings`, which is not among them. So converting
`HonSettingsSwitch` to the dispatcher would have passed both tests once switch.py
was allow-listed.

Added `test_mixed_platform_legacy_classes_keep_the_legacy_sender`: a per-CLASS
check asserting `HonSettingsSwitch` still contains `async_send_settings`,
`HonWashingMachinePauseSwitch` still contains `run_command_sync`, and neither
source references any dispatcher symbol. `select.py` (Task 8) and `number.py`
(Task 9) become mixed files too and must be added to it.

## TDD Evidence

RED before implementation: 15 failed, 85 passed.

Mutations, all seven caught:

```text
W1 write capability no longer gated      -> caught (3)
W2 read state no longer gated            -> caught
W3 undeclared raw value reads as on      -> caught
W4 turn_off writes the on value          -> caught
W5 no refresh after a command            -> caught
W6 tone switch writes the lock field     -> caught
W7 AP folded into the legacy table       -> caught
```

W6 is the one worth naming: swapping the tone description's `action` to
`set_lock` makes the tone switch write `lockStatus`. Nothing in the entity class
itself would notice, since the action is data on the description; the payload
assertion is what catches it.

## Shared stub added

`homeassistant.components.switch.SwitchEntity` moved into `conftest.py` alongside
the fan and light stubs. A bare class is enough: every addhon switch defines its
own properties. Verified the whole suite after, since conftest now wins the
first-wins race for three test modules that previously installed their own
(1380 passed at that point, no regression).

## Verification

```text
python3 -m pytest tests/test_air_purifier_entities.py -q
100 passed

python3 -m pytest tests/test_air_purifier_entities.py tests/test_command_dispatch.py \
  tests/test_switch_params.py -q
157 passed

python3 -m pytest -q
1382 passed, 1 skipped, 7 xfailed, 302 subtests passed
```

Baseline before this task was 1366. `json.tool` clean on both translation files.

## Self-Review (no subagent confutator pool this campaign)

- **Soundness**: verified `is_on` returns None for an unreported attribute rather
  than False, so missing data is not presented as "unlocked"; verified the
  capability gate demands an exact `0/1` enum, so a three-value lock creates no
  entity instead of having `"1"` asserted against it; verified the entity class
  never appears in `HonSettingsSwitch.__mro__` in either direction.
- **Coverage**: six setup gates, state mapping in both directions plus an
  undeclared raw value, availability while stopped, both write directions per
  toggle, sibling non-interference, and the localized error path.
- **Adversarial**: `touchToneStatus` polarity is assumed (1 = tone audible). The
  decompiled enum lists the parameter but not its sense. If it is inverted, the
  switch reads and writes backwards and every test here still passes. Added to the
  ledger as a live item.

## Residual Notes

- The AP branch reads `coordinator.data.items()` directly, matching this module's
  existing style, rather than `coordinator_data_map` as the newer fan/light
  platforms do. Left consistent with the file it lives in.
- `child_lock` shares a translation key across the AC and the AP. Same parameter,
  same meaning, so this is intentional consistency rather than the accidental
  overlap noted for `fan_speed` (ledger P5).
