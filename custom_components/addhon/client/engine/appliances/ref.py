# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""REF (refrigerator) per-type appliance logic.

The class carries no derivation of its own and still has to exist. `get_extra`
(registry.py) returns None for a type with no entry, and `load_attributes` then skips
the extra entirely -- so deleting this module would also stop `ApplianceExtra.attributes`
from running on a fridge, and `programName` (which `select.ref_program` reads as its
second fallback) would disappear from the shadow dict. The registry entry is the only
thing keeping the BASE derivation reachable for this type.

WHAT USED TO BE HERE, and why it is gone (issue #93). This layer synthesised `modeZ1`
and `modeZ2` from the holiday/intelligence/quickMode flags, an inheritance from pyhOn.
Three independent reasons to drop it rather than keep maintaining it:

* Haier has no such field. `modeZ1` and `modeZ2` have ZERO occurrences in the decompiled
  app and in its disassembly, and no member of `REF_PARAMS_ENUM` is a `modeZ*`. Nor does
  the vocabulary match: the lowercase slugs this code emitted (`auto_set`, `no_mode`, ...)
  appear nowhere in the bundle either -- the app's own enum is `RefrigeratorOptionType`,
  SCREAMING_SNAKE, with `NO_MODE_SELECTED` where this wrote `no_mode`.
* Nothing read them. `select.py` excludes them by name and explains why, and no entity
  description, gate or template ever named them.
* They were reported as a DEVICE capability gap. Being written at the top level of the
  attributes dict, they reached `diagnostics.coverage.attributes_unmapped` and told the
  reporter of issue #93 that their fridge exposed two values addhOn had failed to map --
  values addhOn itself had written a moment earlier.

The app does derive a per-zone mode client-side, from the same shadow booleans, but the
shape is not portable: its precedence is per zone (holiday never writes the freezer), it
is inverted relative to this code, its value set is larger (ECO_MODE, SMART_MODE,
QUICK_COOL, SHOCK_FREEZE) and on an upright the same `quickModeZ1` flag means something
else entirely. A flat two-zone table was wrong for a whole family of models.

Full evidence: apk/analysis/issue93-ref-unmapped-values.md section 2.
"""
from __future__ import annotations

from .base import ApplianceExtra


class Appliance(ApplianceExtra):
    """Keeps the BASE derivation reachable for REF; adds nothing of its own."""
