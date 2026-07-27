# Air Purifier Task 8 Report

## Status

Complete on `dev`.

- Commit subject: `feat: control air purifier aroma mode`
- Owned production files: `select.py`, `translations/en.json`, `translations/it.json`
- Owned tests: `tests/test_air_purifier_entities.py`
- Unplanned but required: `tests/conftest.py`, `tests/test_command_dispatch.py`, `tests/test_entity_translation_keys.py`

## Impact analysis

Grep (GitNexus remains unusable):

- `select.async_setup_entry` is a flat dispatch on `app_type` with `continue` per
  branch. The AP branch was inserted before the AC one; the cooling, AC and
  wash-group branches are untouched.
- `HonRefProgramSelect`, `HonProgramSelect`, `HonAcDirectionSelect` and the
  program-option selects are not modified. Pinned per class by the mixed-file
  guard (below).

## What was built

`HonAirPurifierAromaSelect`. Options are the canonical names
(`off`, `soft`, `mid`, `h_biotics`, `custom`) intersected with the device's live
`aromaStatus` enum, with Custom withheld unless BOTH timing parameters are
writable: its contract cannot be completed without them, so offering it would
guarantee a failed write.

- `current_option` returns None for any live value that is not one of the OFFERED
  options. An undeclared raw, or Custom on a device whose timings are missing,
  reads as unknown rather than being mapped to something the user could not have
  chosen.
- Unavailable while the purifier is stopped, per the design: the observed
  application sends aroma patches only during an active session, and selecting a
  mode must never implicitly start the appliance. This is the first AP control
  that IS power-gated; lock and tone deliberately are not.
- A normal mode sends `aromaStatus` alone. Custom sends the status plus both
  timings.

`_custom_time` resolves each timing field with the live shadow reading first and
the command's own value second, validating BOTH against the live range parameter.
A stale or out-of-range shadow value is therefore never echoed back; the schema
default stands in instead, so Custom stays completable. If neither candidate
validates, the selection fails with a localized error rather than inventing a
value.

## Mixed-file guard extended

`select.py` is the second mixed file (after `switch.py` in Task 7): the AP select
dispatches while `HonAcDirectionSelect` and `HonRefProgramSelect` intentionally
send whole commands. Added to the dispatcher allow-list AND to
`test_mixed_platform_legacy_classes_keep_the_legacy_sender`, which now pins four
classes across two files. `number.py` in Task 9 will be the third.

## Translation parity: a second uncovered hole

`entity.select.aroma` needed a `name` plus a `state` block for all five options.
Two collectors in `test_entity_translation_keys.py` had to learn about it:

- `_collect_code_keys()["select"]` is a hardcoded set, so the new key had to be
  added or the `extra` half of the parity assertion would never see it.
- `_collect_select_state_keys()` iterates DESCRIPTION TABLES to find label-mapped
  selects. The aroma select is one fixed-key entity with no table, so its `state`
  block would have been entirely unchecked in both languages. Registered its
  option set explicitly.

That is the same class of gap as ledger V5, one level deeper: not just per
platform, but per key SOURCE within a platform.

## TDD Evidence

RED before implementation: 17 failed, 102 passed.

Mutations, all nine caught:

```text
A1 aroma select available while purifier off  -> caught
A2 unoffered live state mapped anyway         -> caught
A3 unoffered option accepted                  -> caught
A4 custom sent without its timings            -> caught (3)
A5 live timing reading ignored                -> caught
A6 out-of-range timing echoed back            -> caught
A7 read state no longer gated                 -> caught
A8 write schema no longer gated               -> caught
A9 no refresh after a selection               -> caught
```

A5 and A6 are the pair worth naming: they pin the two halves of the timing
resolution independently, so neither "always use the schema default" nor "always
trust the shadow" can pass.

## A bogus assertion of my own

`test_an_unoffered_raw_state_reads_as_unknown` shipped with a leftover
`self.assertIn("custom", [None])` from drafting, which failed for the wrong
reason. Removed; the two real assertions in that test (custom not offered, current
option unknown) were already correct and are what the mutation A2 exercises.

## Verification

```text
python3 -m pytest tests/test_air_purifier_entities.py -q
119 passed

python3 -m pytest tests/test_air_purifier_entities.py tests/test_command_dispatch.py \
  tests/test_program_select.py tests/test_ref_program_select.py \
  tests/test_ac_fan_direction_select.py tests/test_entity_translation_keys.py -q
263 passed

python3 -m pytest -q
1401 passed, 1 skipped, 7 xfailed, 302 subtests passed
```

Baseline before this task was 1382. `json.tool` clean on both translation files.

## Self-Review (no subagent confutator pool this campaign)

- **Soundness**: verified the offered-set membership check guards BOTH directions
  (reading a state and accepting a selection), so the two can never disagree;
  verified `_custom_time` validates the command's own value too rather than
  trusting it, since a schema default outside its own declared range would
  otherwise be sent; verified the aroma patch never carries `onOffStatus` or
  `machMode`, which is what makes "no selection starts the purifier" structural
  rather than incidental.
- **Coverage**: six setup gates, all five state mappings, two unknown-state cases,
  availability in both power states, all four normal writes, Custom with live
  times and with fallback times, the no-start assertion, and two error paths.
- **Adversarial**: the aroma ordinals (1=soft, 2=mid, 3=h_biotics) come from the
  design, not from a live dump. If they are permuted, every test here passes and
  the user gets the wrong scent intensity. Added to the ledger.

## Residual Notes

- Custom always sends BOTH timing fields, per the design's Custom contract. Task 9's
  single-timing variant (`set_aroma_time`, added in Task 2 as V3) is the other
  shape and is deliberately not used here.
- `_custom_time` reads the command parameter through `command_param` +
  `param_range` rather than caching bounds on the capabilities dataclass, so it
  always reflects the CURRENT command object rather than the one present at
  construction.
