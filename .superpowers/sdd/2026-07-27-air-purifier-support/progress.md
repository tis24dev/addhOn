# SDD ledger — plan: /opt/addhOn/.development/plans/2026-07-27-air-purifier-support-implementation-plan.md

BASE: 0546291
BRANCH: dev (in-place by explicit user request)
EXECUTION: solo (no subagent-driven-development this campaign, per explicit user decision; self-review substitutes for confutator pool, same as dispatcher-aggregate-fixes)
LIVE DEVICE: not available this session; Task 14/15 (HHP50/HHP55 live validation) deferred to beta release + user-collected diagnostics, per explicit user decision
Design spec: /opt/addhOn/.development/specs/2026-07-27-air-purifier-support-design.md
Aggregate review dependency (Transactional Command Dispatcher): CLOSED, see /opt/addhOn/.superpowers/sdd/2026-07-27-dispatcher-aggregate-fixes/progress.md

Task 1: complete — AP contract matrix, self-validating against its own schema
Task 2: complete — AP mappings, capabilities, intents (fan/light deferred out of PLATFORMS to Tasks 5/6)
Task 3: complete — 13 AP read-only sensors, requires_power availability flag
Task 4: complete — eco_active + problem (has_problem-derived); backlog: no2ValueIndoor unmapped, errors unconfirmed on AP
Task 5: complete — fan platform (dispatcher now LIVE from an entity); fan added to PLATFORMS
Task 6: complete — inverse 3-level panel light; light added to PLATFORMS (V2 closed)
Task 7: complete — sparse child_lock + touch_tone switches; per-class legacy guard for mixed files
Task 8: complete — aroma select (power-gated), custom timings validated against the live range
Task 9: complete — experimental option (targeted reload), AQ label + CO alarm, custom timing numbers in seconds; number.py is the third mixed file; control mutation exposed an unguarded stub clobber (V11)
Task 10: complete — AP coverage registration + bounded future_capabilities; Step 5 was already satisfied by the dispatcher; control mutation found the device-class enum stub divergence (V12), new test_stub_hygiene.py
Task 11: pending — EN/IT translations
Task 12: pending — contract matrix through real dispatcher
Task 13: pending — full regression and static validation
Task 14/15: BLOCKED — no live HHP50/HHP55 device this session; deferred to beta
