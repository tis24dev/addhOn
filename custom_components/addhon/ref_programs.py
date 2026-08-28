# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a fridge's `startProgram` catalogue offers, sorted by KIND (issue #93).

A REF `startProgram` enum is not one list of interchangeable programs. It is three
different kinds of thing that the hOn app renders on three different surfaces, and
until this module they were all collapsed into one mutually-exclusive select:

* FLAG modes -- `auto_set`, `super_cool`, `super_freeze`, `holiday`. Each pins ONE
  boolean the shadow reports back, and `stopProgram` declares that same boolean at 0.
  They are independent: `startProgram(zero_fresh)` writes `tempSelZ3` and does not
  clear `quickModeZ1`, so My Zone at 0 degrees and Super Cool coexist by construction
  -- which is exactly what the reporter of #93 said and we denied.
* MY ZONE drawer modes -- the vtRoom1 card. Each pins `tempSelZ3` to one number, and
  the register has no "off": `stopProgram` zeroes the four flags and never touches it,
  so `tempSelZ3` is always one of the drawer's own values.
* DOWNLOAD presets (`programFamily = download`, named `iot_*`). They write a `tempSel`
  triple, set no flag, and leave NO program-identity field in the shadow. The app
  treats them as QuickSet cards -- a "what you can SEND" menu whose active member is
  recovered only from device-local AsyncStorage (`@quickSet`), never from the cloud.

Evidence: apk/analysis/issue93-ref-controls-shape.md (the whole document),
apk/analysis/issue93-ref-unmapped-values.md sections 4.4 and 4.7,
apk/analysis/deep/ref-active-program-detection.md (T1-T9),
apk/analysis/deep/refrigeration.md sections 2 and 6.

EVERYTHING HERE IS READ OFF THE LIVE SCHEMA. The only slugs written down are
`REF_FLAG_TO_PARAM` (a flag/parameter pairing that is a fact about the four commands,
not about any model) and `REF_DOWNLOAD_PRESETS` (see its own note: it exists to keep
the entity unique_id vocabulary closed, NOT to gate on a model list).
"""
from __future__ import annotations

import logging
from typing import Any

from .const import PROGRAM_PARAM_NAMES
from .hon_commands import SYNTHETIC_CATEGORY, get_command

_LOGGER = logging.getLogger(__name__)

STARTPROGRAM = "startProgram"
STOPPROGRAM = "stopProgram"

# The four boost/special modes: program code -> the boolean it drives. The app's own
# identity map (`MyHabitsAwareModes`, decomp.txt:1729069-1729080) and the only
# running-mode signal a fridge shadow carries -- there is no `modeZ*` field, in the app
# or anywhere else (#93 closed that question). Written down because it is a property of
# the FOUR COMMANDS, identical on every catalogue seen, and not of any one model; which
# code a given fridge actually offers is still read from its live enum below.
REF_FLAG_TO_PARAM: dict[str, str] = {
    "auto_set": "intelligenceMode",
    "super_cool": "quickModeZ1",
    "super_freeze": "quickModeZ2",
    "holiday": "holidayMode",
}
# The read direction. Derived, never restated, so the two halves cannot drift.
REF_PARAM_TO_FLAG: dict[str, str] = {
    param: code for code, param in REF_FLAG_TO_PARAM.items()
}

# The vtRoom1 drawer: the register its programs pin, and the zone that owns it.
# `getTempSel` maps MY_ZONE_1 / VT_ROOM_1 -> tempSelZ3 (decomp.txt:4587895).
REF_MY_ZONE_PARAM = "tempSelZ3"
REF_MY_ZONE_ZONE = "vtRoom1"

# The per-category ancillaries this module classifies on. Both are ordinary parameters
# at runtime: `HonCommand._load_parameters` walks EVERY top-level group of a command
# schema, so `ancillaryParameters.zone` lands in `category.parameters["zone"]` exactly
# like a command parameter does (client/engine/commands.py:159-200). Confirmed on
# production data, not only on the APK fixture: rmxs's own dump prints
# `commands.startProgram.zone` and `.programFamily` for his active category.
REF_ZONE_PARAM = "zone"
REF_FAMILY_PARAM = "programFamily"
# The family the app filters its QuickSet card list on. `dashboard` is a mode card;
# `download` is a preset that writes setpoints and leaves no trace of itself.
REF_DOWNLOAD_FAMILY = "download"

# The download presets this repository can NAME. This is a closed vocabulary and not a
# capability gate: the gate is the live enum plus `programFamily`, both read below. It
# exists because a preset becomes a BUTTON, and a button needs a stable `unique_id`
# suffix and a translation key. `diagnostics._entity_section` states the invariant that
# forces it: the privacy of the whole `entities.sources` section rests on "the closed
# vocabulary of unique_id suffixes, every one of which is a constant written in this
# repository", and warns that "a class that built its unique_id out of a device-supplied
# name would break it". A favourite is filed under the name the USER typed
# (command_loader._add_favourites), so a code taken straight from the catalogue could be
# a nickname. Hence: only these six ever become entities.
#
# The six are the union of the two catalogues we hold -- rmxs's HFW7720EWMP offers five
# of them, HDPW5620CNPK all six -- and they are the same six `select.ref_program`
# already labels, so the wording carries over unchanged. A seventh preset on a future
# model gets no button and says so once, at DEBUG, from `download_codes` below: a
# missing entity is visible the moment somebody looks for it, an invented one is not.
# The parameter the engine injects into a user's saved favourite, and the only reliable
# way to tell one from a catalogue program. `command_loader._add_favourites` copies the
# base category, adds `HonParameterFixed("favourite", ...)` to the copy, and files it
# under `favouriteName` -- the string the USER typed -- so a favourite is a category
# whose KEY is free text and whose parameters carry this name.
_FAVOURITE_PARAM = "favourite"

REF_DOWNLOAD_PRESETS: tuple[str, ...] = (
    "iot_daily_use",
    "iot_extra_cold",
    "iot_extra_cold_water",
    "iot_extra_ice",
    "iot_high_efficiency",
    "iot_special_food_core",
)


def model_zones(appliance) -> frozenset[str]:
    """Zones the MODEL CATALOGUE declares, or an empty set when it says nothing.

    `applianceModel.attributes.zones` is the criterion the hOn app uses to decide which
    fridge compartments exist -- `useRefrigeratorCommands` splits it on "|" and returns
    immediately (decomp.txt:2843098-2843131) -- and it is per-model metadata, not
    telemetry, so it answers even for a drawer whose shadow value has not moved in two
    years (#93).

    Empty means "the catalogue did not answer", and every caller then DENIES. That is
    stricter than the app, which falls back to the union of every startProgram's zone
    enum. Deliberate: we do not implement that fallback, and inventing an entity out of
    silence is the worse failure -- an entity that should not exist cannot be told from
    one that should, while a missing one is visible the moment somebody looks for it.

    Moved here verbatim from `sensor._model_zones` (5.20.0) so the sensor gate and the
    select gate cannot disagree about which drawers a fridge has.
    """
    attributes = getattr(appliance, "model_attributes", None)
    raw = attributes.get("zones") if isinstance(attributes, dict) else None
    return frozenset(part for part in str(raw or "").split("|") if part)


def offered_codes(appliance) -> list[str]:
    """The device's LIVE `startProgram.program` enum.

    Order is whatever the parameter returns and is never relied on: on the real engine
    `HonParameterProgram.values` answers `sorted(...)`, so it is alphabetical rather
    than the schema's. Every caller either builds a set from this or imposes its own
    order (`flag_codes` uses the flag map's, `my_zone_codes` sorts by the pinned value).

    Resolved ONLY on `startProgram` -- not the broader `PROGRAM_SOURCE_COMMANDS` walk the
    washer select uses -- for the reason `HonRefProgramSelect._start_program_param` was
    written: on a fridge the option SOURCE must be the very command every caller here
    sends to, so the two can never diverge.

    The three attribute names are the same ones `HonProgramSelect._program_values` tries,
    and the duplication is deliberate rather than a shared helper: that function also
    carries the washer's label handling (a dict of code -> human name) which no fridge
    program parameter has, and folding the two would put washer concerns on the fridge
    path. Here only the KEYS are ever wanted.
    """
    command = get_command(appliance, STARTPROGRAM)
    params = getattr(command, "parameters", None) if command is not None else None
    if not isinstance(params, dict):
        return []
    for param_name in PROGRAM_PARAM_NAMES:
        param = params.get(param_name)
        if param is None:
            continue
        for attr in ("values", "value_list", "options"):
            raw = getattr(param, attr, None)
            if isinstance(raw, dict):
                return [str(code) for code in raw]
            if isinstance(raw, (list, tuple)):
                return [str(value) for value in raw]
        return []
    return []


def program_categories(appliance) -> dict[str, Any]:
    """`code -> category command` for the real programs, or {}.

    `SYNTHETIC_CATEGORY` is skipped for the same reason `program_code_for_fixed_value`
    skips it: `HonCommand.categories` answers `{"_": self}` for a command with no
    categories at all, and that placeholder is not a program name. Admitting it would
    let a category-less startProgram's own parameters be read as if a program had pinned
    them, and put a bare "_" in front of the user (the 5.20.0 review found exactly that).

    A user's saved FAVOURITE is skipped too, and that is the load-bearing half. A
    favourite is a copy of a catalogue category filed under `favouriteName` -- free text
    the user typed -- which inherits the base command's `programFamily` and its pinned
    parameters, so it passes every classifier below and would become a `select` option, a
    `unique_id` and a log line carrying that text. `diagnostics._entity_section` rests
    the privacy of its whole `sources` map on the opposite ("every unique_id suffix is a
    constant written in this repository"), and this module's loggers on the same rule
    every other logger here follows.

    Excluded on the ENGINE'S OWN MARKER and not on the shape of the name: `_add_favourites`
    adds a `favourite` parameter to the copy, so the test is exact. A name-shape heuristic
    is not: a favourite called `iot_something` is lowercase, underscored and
    indistinguishable from a schema slug, and would have walked straight through it.

    Skipping them costs nothing a user wanted: a favourite is a saved SEND, not one of the
    appliance's own modes, and the code we would have to transmit for it is the catalogue
    slug it was copied from.
    """
    command = get_command(appliance, STARTPROGRAM)
    categories = getattr(command, "categories", None) if command is not None else None
    if not isinstance(categories, dict):
        return {}
    return {
        str(code): category
        for code, category in categories.items()
        if str(code) != SYNTHETIC_CATEGORY and not _is_favourite(category)
    }


def _is_favourite(category) -> bool:
    """True for a user's saved favourite rather than a catalogue program."""
    params = getattr(category, "parameters", None)
    return isinstance(params, dict) and _FAVOURITE_PARAM in params


def _category_values(category, param_name: str) -> frozenset[str]:
    """Allowed values of a category parameter, CASE-FOLDED, or an empty set.

    Folded because the engine already folds one side and not the other:
    `HonParameterEnum.values` pushes every member through `clean_value`
    (strip "[]", "|"->"_", lower -- client/engine/parameter/enum.py:29), so the schema's
    `vtRoom1` reaches a reader as `vtroom1`, while `model_zones` above reads the model
    attribute raw and keeps the capital R. Comparing the two spellings without folding
    is a silent, total gate failure, so the fold lives here where both sides pass.
    """
    params = getattr(category, "parameters", None)
    param = params.get(param_name) if isinstance(params, dict) else None
    if param is None:
        return frozenset()
    values = getattr(param, "values", None)
    if isinstance(values, (list, tuple)):
        return frozenset(str(value).strip().lower() for value in values)
    return frozenset()


def _pinned_value(category, param_name: str) -> str | None:
    """The value a category PINS `param_name` to, or None if it does not pin it.

    Restricted to `typology == "fixed"` for the reason `program_code_for_fixed_value`
    is: a range or an enum member merely ALLOWS a value, while `fixed` means "this
    program is the reason the value is what it is". `HonParameterFixed` is duck-typed
    through `.typology`, like every other reader in this cluster.
    """
    params = getattr(category, "parameters", None)
    param = params.get(param_name) if isinstance(params, dict) else None
    if param is None or str(getattr(param, "typology", "")) != "fixed":
        return None
    return str(getattr(param, "value", "")).strip()


def flag_codes(appliance) -> list[str]:
    """Offered FLAG modes whose OFF half the device really declares.

    Both halves, not either: a code in the live enum gives the ON (`startProgram`), and
    the matching parameter under `stopProgram` gives the OFF. The app never sends the
    four-flag reset -- `apk/dump/ref_10136/attributes.json` catches its own
    `stopProgram` carrying `{"intelligenceMode": "0"}` and nothing else -- so a flag we
    cannot clear on its own is a flag we should not offer as a switch, and a switch that
    can only be turned on is not a switch.

    Order is `REF_FLAG_TO_PARAM`'s -- fridge modes first, then the freezer's, then
    Holiday -- and not the enum's, which the engine returns alphabetically.
    """
    offered = set(offered_codes(appliance))
    stop = get_command(appliance, STOPPROGRAM)
    stop_params = getattr(stop, "parameters", None) if stop is not None else None
    stop_params = stop_params if isinstance(stop_params, dict) else {}
    return [
        code
        for code, param in REF_FLAG_TO_PARAM.items()
        if code in offered and param in stop_params
    ]


def my_zone_codes(appliance) -> list[str]:
    """Offered vtRoom1 DRAWER modes, coldest first, or [].

    Three conditions, and every one of them is load-bearing -- this was measured against
    both catalogues we hold, not reasoned about:

    1. the category PINS `tempSelZ3`. That is what `getModeNameFromCommands` looks for
       (decomp.txt:2834689-2834780) and it is what makes the register readable back.
       Excludes the four flag modes. It also excludes HOLIDAY, whose rules force
       `tempSelZ3 -> "17"`: that is a `programRules` entry, and the rules engine only
       mutates a parameter the command already declares (rules.py `_add_trigger.apply`),
       so no `tempSelZ3` parameter is ever created on the HOLIDAY category.
    2. the category's `zone` is EXACTLY the drawer. HOLIDAY declares `fridge|vtRoom1`
       and three of HDPW5620CNPK's download presets declare `fridge|freezer|vtRoom1`:
       a program that also moves the fridge and the freezer is not a drawer mode.
    3. the category is not a `download` preset. This one exists for a single
       counterexample and would look like belt-and-braces without it:
       `PROGRAMS.REF.IOT_SPECIAL_FOOD_CORE` pins `tempSelZ3 = 5` AND declares
       `zone = [vtRoom1]` and nothing else (apk/dump/ref_10136/commands.json). It passes
       1 and 2 and is still a QuickSet preset, not a drawer mode.

    On HDPW5620CNPK the result is EMPTY -- which is the point: that fridge writes its
    drawer through a real `setParameters.tempSelZ3` range and already has
    `number.target_temp_zone3`. The two controls exclude each other from the data alone,
    with no rule anywhere saying "if the number exists, skip the select".

    Sorted by the PINNED VALUE and not alphabetically. The register is a temperature
    scale at firmware level -- Holiday pins it to 17 -- so ordering by the number gives
    the drawer's own coldest-to-warmest order (zero_fresh 0, quick_cool 2,
    fruit_and_veg 5), which is the order the app's mode card shows. A non-numeric pin
    sorts last and keeps its code as the tie-break, so the order is total and stable.

    THE MODEL-ZONE TEST LIVES HERE, not in the caller, and that placement is the whole
    point of this function. It used to sit in `select.async_setup_entry`, so
    `has_replacement_controls` -- which calls this -- answered a DIFFERENT question from
    the one that decides whether the drawer select is built. The catalogue this design
    was derived from is the counterexample: `decomp.txt:3495902` declares `vtZone = 1`
    and no `zones` attribute at all, so the predicate said "a replacement exists",
    `select.ref_program` stepped aside, and nothing took its place -- the appliance ended
    up with no way to reach `zero_fresh`, `quick_cool` or `fruit_and_veg` at all. One
    question, answered once, is the only shape in which that cannot happen again.
    """
    if REF_MY_ZONE_ZONE not in model_zones(appliance):
        return []
    offered = set(offered_codes(appliance))
    drawer = REF_MY_ZONE_ZONE.lower()
    found: list[tuple[float, str]] = []
    for code, category in program_categories(appliance).items():
        if code not in offered:
            continue
        pinned = _pinned_value(category, REF_MY_ZONE_PARAM)
        if pinned is None:
            continue
        if _category_values(category, REF_ZONE_PARAM) != {drawer}:
            continue
        if REF_DOWNLOAD_FAMILY in _category_values(category, REF_FAMILY_PARAM):
            continue
        try:
            order = float(pinned)
        except (TypeError, ValueError):
            order = float("inf")
        found.append((order, code))
    return [code for _order, code in sorted(found)]


def my_zone_readable_codes(appliance) -> list[str]:
    """Drawer programs that can EXPLAIN a `tempSelZ3` reading, in catalogue order.

    The read-side twin of `my_zone_codes`, and deliberately looser, because reading and
    writing are not the same question. To SEND a program the code has to be in the live
    `startProgram` enum and the category has to be the drawer's own -- otherwise the send
    is refused or moves another compartment. To EXPLAIN a number already sitting in the
    shadow, neither is required: a category the enum no longer offers still pinned that
    value when it ran, and a catalogue that declares no `zone` ancillary at all has not
    said the program is NOT the drawer's, it has said nothing.

    The justification is narrower than it first looks, and two tempting versions of it
    are FALSE, so they are written down here rather than left to be rediscovered:
    `HonParameterProgram.values` does not drop a drawer program (it filters only
    `iot_recipe` and `iot_guided`), and "a diagnostics dump prints only the active
    category" is a fact about the REPORT, not about the runtime object graph -- every
    category is its own `HonCommand` carrying its own ancillaries. What is left is the
    real reason, and it is enough: a catalogue that declares no `zone` ancillary, or a
    model whose `zones` attribute does not name the drawer, has said NOTHING about a
    program that visibly pins the drawer's register, and silence is not a denial when
    the only question is "which program explains this number". Requiring the write-side
    gate here would have regressed `sensor.my_zone_mode` (5.20.0) on exactly those
    shapes, reporting `chiller` where the catalogue says `quick_cool`.

    On every catalogue this repository holds the two sets are IDENTICAL. The split is
    defence against a shape we have not seen, not a correction to one we have.

    ONE condition survives from the write side, and it is the one that was actually
    wrong: `programFamily = download` is excluded. That is the correction this pair of
    functions exists for -- on damigioanna's HDPW5620CNPK five download presets pin
    `tempSelZ3` (2, 2, 5, 5, 5), so an unfiltered lookup reports a whole-appliance preset
    as the drawer's mode, and `IOT_SPECIAL_FOOD_CORE` is zoned on the drawer alone so no
    zone test would have caught it either. A preset writes the register on its way past;
    it is not the drawer being in a mode.
    """
    return [
        code
        for code, category in program_categories(appliance).items()
        if _pinned_value(category, REF_MY_ZONE_PARAM) is not None
        and REF_DOWNLOAD_FAMILY not in _category_values(category, REF_FAMILY_PARAM)
    ]


def my_zone_code_for_value(appliance, value) -> str | None:
    """Which DRAWER program explains `value`, or None.

    `hon_commands.program_code_for_fixed_value` restricted to `my_zone_codes` -- the
    same one lookup, narrowed -- and the narrowing is a correction, not a nicety. Left
    unrestricted the walk answers with the FIRST category that pins the number, and on
    HDPW5620CNPK five download presets pin `tempSelZ3` (2, 2, 5, 5, 5), so an
    unrestricted lookup calls the drawer `iot_extra_cold` at 2 and `iot_daily_use` at 5.
    The drawer's register is explained by the DRAWER's programs; a whole-appliance
    preset that happens to write the same number is not evidence about the drawer.

    Narrowed to `my_zone_readable_codes` and NOT to `my_zone_codes`: this is a read, and
    the write-side gate would drop a program the live enum no longer offers or whose
    category declares no `zone` -- neither of which makes it a worse explanation of a
    number the appliance is already reporting. The select still checks the answer against
    its own frozen options before showing it, so the looser set cannot leak a state the
    control could not reach.

    None for a value no drawer program explains -- notably 17, which HOLIDAY's rules
    force. While Holiday runs the drawer is in none of its own modes, and the app agrees:
    it shows NO_MODE_SELECTED there (decomp.txt:2841604).
    """
    from .hon_commands import program_code_for_fixed_value

    return program_code_for_fixed_value(
        appliance, REF_MY_ZONE_PARAM, value, codes=my_zone_readable_codes(appliance)
    )


def download_codes(appliance) -> list[str]:
    """Offered DOWNLOAD presets this repository can name, in `REF_DOWNLOAD_PRESETS` order.

    Gated on the LIVE enum and on the schema's own `programFamily = download`, then
    intersected with `REF_DOWNLOAD_PRESETS` -- see that constant for why the
    intersection is about unique_id vocabulary and not about capability.

    `programFamily` is REQUIRED here and merely consulted in `my_zone_codes`, and the
    asymmetry is deliberate. A preset button is a fire-and-forget send with no state to
    correct it, so it is built only where the schema says in so many words what it is;
    the drawer select has two other conditions carrying it and can afford to treat a
    silent family as "not a download".

    A `download` program outside the six says so once, at DEBUG, naming itself. That is
    the visible kind of gap: a reporter who wonders where their preset went finds the
    line, and the fix is one tuple entry plus two labels.
    """
    offered = set(offered_codes(appliance))
    categories = program_categories(appliance)
    known: list[str] = []
    for code in REF_DOWNLOAD_PRESETS:
        category = categories.get(code)
        if code not in offered or category is None:
            continue
        if REF_DOWNLOAD_FAMILY not in _category_values(category, REF_FAMILY_PARAM):
            continue
        known.append(code)
    unnamed = sorted(
        code
        for code, category in categories.items()
        if code in offered
        and code not in REF_DOWNLOAD_PRESETS
        and REF_DOWNLOAD_FAMILY in _category_values(category, REF_FAMILY_PARAM)
    )
    if unnamed:
        # Safe to name in full: `program_categories` has already dropped the user's
        # favourites, so every key that reaches here is a catalogue slug the cloud chose.
        # Naming them is the whole point of the line -- the fix for a genuinely new
        # preset is one tuple entry plus two labels, and a reporter has to be able to
        # tell us WHICH one is missing.
        _LOGGER.debug(
            "RefPrograms debug: %d download preset(s) with no entity because this "
            "repository does not name them: %s (see REF_DOWNLOAD_PRESETS)",
            len(unnamed),
            unnamed,
        )
    return known


def active_mode_code(read_attr) -> str | None:
    """The program code of the first mode flag the shadow reports as running, or None.

    `read_attr` is a one-argument reader over the device's shadow -- the entity's own
    `_get_attr` -- so this stays a pure classifier over `REF_FLAG_TO_PARAM` and never
    reaches into an entity or a coordinator.

    Used to refuse a zone-setpoint write while a mode owns the setpoint, which is what
    the app does: its REF `setParameters` handler (decomp.txt:2873608, exported beside
    `disableActivatedDefaultModes` at 2873755) reads all four shadow flags and, if ANY is
    on, sends the four-zero reset INSTEAD of the setpoint the user moved. Coarser than
    the `programRules` would strictly require -- with Super Cool on, the rules pin
    `tempSelZ1` to 1 and leave `tempSelZ2` at `@tempSelZ2`, i.e. untouched -- and coarse
    on purpose, because it is the app's own rule and because our engine cannot evaluate
    those rules anyway: `HonRuleSet._attach_triggers` only binds a trigger that is a
    parameter of the SAME command, and the reporter's `settings` declares none of the
    four flags. So the range never widens, the shadow can report 17 on a 1..9 grid, and
    a write from here would be overwritten by the appliance without saying so.

    Order is `REF_FLAG_TO_PARAM`'s, so the answer is stable when two modes are on.
    """
    for code, param in REF_FLAG_TO_PARAM.items():
        if str(read_attr(param)) == "1":
            return code
    return None


def has_replacement_controls(appliance) -> bool:
    """True when this appliance gets the controls that make `select.ref_program` wrong.

    THE SUPPRESSION PREDICATE, in one place so the select that steps aside and the
    entities that replace it can never disagree about whether the replacement actually
    exists. Evaluated from the LIVE schema of THIS appliance and from nothing else: no
    model list, no series check, no "REF usually has these".

    Why those two and not the download buttons. The flag switches and the drawer select
    are what makes the single select WRONG, not merely redundant: `ref_program` is
    mutually exclusive and its `off` sends a four-flag `stopProgram`, so with the
    switches present two controls write the same registers with opposite meanings of
    "off", and with the drawer select present two controls own `tempSelZ3`. The preset
    buttons add no such conflict -- they send and forget -- so on the one shape where
    they are the ONLY thing we can build (an enum of `iot_*` and nothing else) the
    select survives and keeps being the appliance's only stop control and its only
    state. Coexistence there is by design, the same way the AC's vertical direction
    select coexists with the climate swing mode.

    Both halves are COMPLETE predicates: `flag_codes` and `my_zone_codes` each answer
    exactly the question their platform asks, model-zone test included, so no caller may
    add a further condition of its own. A caller that did would recreate the defect this
    function exists to prevent -- an appliance whose select steps aside for a replacement
    that the caller then refuses to build.
    """
    return bool(flag_codes(appliance)) or bool(my_zone_codes(appliance))
