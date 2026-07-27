# Air Purifier Task 11 Report

## Status

Complete on `dev`.

- Commit: `f6892b6` `feat: translate air purifier entities`
- Owned files: `translations/en.json`, `translations/it.json`,
  `tests/test_translations.py`
- `tests/test_entity_translation_keys.py` needed NO change: every key source added
  in Tasks 3-10 already registered itself there (deviation V5's rule held).

## What this task actually was

Per deviation V6, translations were added by the task that introduced each key, so
`test_code_keys_match_translations_exactly` (exact set equality per language) was
already green before this task started. Task 11 is therefore a **completeness and
wording pass**, and its value is in the assertions that did NOT exist:

**The options screen had no code-vs-JSON parity check at all.** `options.step.init.data`
is a config-flow schema, not an entity translation_key, so the entity collectors
never look at it: `enable_experimental` could have shipped rendering as a raw key
with no test complaining. `OptionsScreenTranslationTest` now pins the label AND
description set against the rendered option list, asserts the experimental
description states the evidence is incomplete, and asserts the screen title is no
longer debug-only.

**The AP key LIST is now pinned per platform.** The generic tests compare code to
JSON and en to it; both pass if an entity and its label disappear together.
`AirPurifierTranslationTest.EXPECTED` is the list itself, so a dropped label fails
even when code and JSON agree with each other. It also pins the presence of the `fan`
and `light` platform blocks: a whole missing platform would leave the generic
collector nothing to compare and pass vacuously.

**Semantic assertions the parity tests cannot express:**

- the CO entity's name must deny being a certified detector, in both languages. A
  binary sensor has no description field in Home Assistant, so the label is the ONLY
  place a user sees it;
- every experimental entity must say so in its name, and no standard one may;
- no AP label may leak a parameter name, a schema, the decompiled app or debug
  mechanics;
- the aroma timing exception must be non-empty and placeholder-free.

## Wording fixes

- **Italian capitalization.** The file is dominantly Title Case (91 labels against
  42), and so is the AP block from Tasks 3-8, but the Task 9 additions used sentence
  case. Four labels normalized, plus `co` and `air_quality` for internal AP
  consistency. Pinned by
  `test_the_italian_air_purifier_labels_share_one_capitalization_style`, deliberately
  scoped to the AP keys: the rest of the file is not this task's business, but the
  purifier's entities sit next to each other on one dashboard, and a mix reads as a
  bug.
- **The two filter labels were asymmetric.** Both report a REMAINING percentage, but
  they read "Filter life" and "Pre-filter cleaning": one gauge and one alarm. Now
  "Filter life" / "Pre-filter life" and "Durata Filtro" / "Durata Prefiltro", pinned
  by `test_the_two_filter_labels_are_symmetric`, which requires shared wording rather
  than a specific string.

## Ledger D1 resolved: fan preset casing stays lowercase

Home Assistant does not let an integration translate `fan` preset modes: they are
state attributes whose labels HA core owns. The only lever would be renaming a preset
to one core already translates, which would change `AP_MODE_TO_PRESET` and break the
contract Task 2 pins, plus every automation referring to `max`. Decision: keep
`sleep` / `auto` / `max` as they are. Consequence: HA core translates `auto` and
`sleep`, while `max` renders literally. Cosmetic and reversible; flagged in the ledger
as accepted rather than open. The user can overrule by choosing a core-translated
preset name.

## TDD Evidence

The plan's Step 2 ("verify red") could not be observed the usual way: the JSON was
already complete, so nothing failed. Verified by mutation instead, per the campaign
convention, 14 real plus 1 control:

```text
T1  experimental option label dropped        -> caught (2)
T2  experimental description dropped         -> caught (3)
T3  description hides the incomplete-evidence caveat -> caught
T4  options title back to debug-only         -> caught
T5  AQ label state translation dropped       -> caught (3)
T6  CO disclaimer removed from the name      -> caught (2)
T7  experimental marker dropped from a number-> caught
T8  a standard entity claims to be experimental -> caught
T9  aroma state block loses an option        -> caught (3)
T10 italian light label lowercased           -> caught
T11 filter labels desymmetrized              -> caught
T12 italian number label dropped             -> caught (6)
T13 aroma exception message emptied          -> caught
T14 implementation detail leaks into a label -> caught (2)
T15 CONTROL trailing-space service reword    -> SURVIVED (correct)
```

T1's first form deleted a line and left a trailing comma, so all 37 tests failed on
invalid JSON: a useless verdict. Re-run with the comma included, it fails on exactly
the two semantic assertions. A mutation that breaks the parser proves nothing.

## Verification

```text
python3 -m pytest tests/test_translations.py tests/test_entity_translation_keys.py \
  tests/test_code_is_english.py -q
43 passed

python3 -m pytest -q
1503 passed, 1 skipped, 7 xfailed, 302 subtests passed

python3 -m json.tool en.json / it.json   # both clean
```

Baseline before this task was 1487.

## Self-Review (no subagent confutator pool this campaign)

- **Soundness**: verified each new assertion fails for the RIGHT reason (the mutation
  table names the failing test); verified the capitalization check ignores a trailing
  parenthetical, so "(sperimentale)" cannot be read as a lowercase content word;
  verified the implementation-detail scan runs over the whole per-key JSON blob, so a
  leak in a `state` label is caught too, not just in `name`.
- **Coverage**: 26 AP keys across 7 platforms, both `state` blocks, the exception
  message, the three option labels and descriptions, and the screen title.
- **Adversarial**: `test_no_air_purifier_label_leaks_implementation_detail` uses a
  fixed token list. A NEW parameter name would not be on it. The list covers the
  parameters this campaign actually touches, which is what a copy-paste from the code
  would most likely leak.

## Residual Notes

- The forbidden-token list includes `debug` and `mqtt`, which are legitimate words in
  the OPTIONS block. The scan is scoped to the AP entity keys, so it cannot collide
  with them.
- Italian title case leaves articles and prepositions lowercase; the style checker
  has a small minor-word set for that. A new label using an unlisted preposition
  would be classified "mixed" and fail. That is a nudge to extend the set, and the
  failure message shows the offending label.
