"""addhOn native parser engine (commands/parameters/rules/program/appliance).

OUR code (commands/parameter/rules/command_loader/appliance), validated against
the real dumps + the decompiled app (see diagnostics/FASE4-engine-plan.md and
apk/analysis/).

Design constraint: `rules.py` uses `isinstance` against the parameter classes; for this
reason parameters, commands, rules, program and the per-type layer are a cohesive cluster
that lives and evolves together. Behavior anchored to the real dumps by the golden tests
(tests/golden/).
"""
