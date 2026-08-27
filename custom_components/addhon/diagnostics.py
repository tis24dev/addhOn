# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Diagnostics support for Haier hOn (Extended).

This dump is what a user attaches to a GitHub issue when an appliance is "not
mapped" or "mapped badly". Home Assistant renders the built-in "Download
diagnostics" button for the config entry AND for each device (the latter is wired
by async_get_device_diagnostics below); no custom button is needed.

Per appliance the dump carries, beyond the bare key list it used to emit:
  * `model_attributes` - the cloud CATALOGUE metadata for the model
                    (`applianceModel.attributes`): `zones`, `seriesVersion`,
                    `doorNumber`, `vtRoom1`/`vtRoom2`, ... It answers what the
                    appliance IS, which the shadow cannot: the hOn app decides
                    which fridge zones exist from `zones`.split("|"), not from
                    which `tempZ*` keys the shadow carries. Without it, a
                    zone-indexing report (issue #75) needs a round trip to the
                    reporter before it can even be diagnosed.
  * `attributes`  - the attribute VALUES (telemetry/state), recursively redacted;
  * `commands`    - the writable schema per command param: value + enum + min/max/
                    step + typology, so a maintainer sees the real ranges/options;
  * `coverage`    - the signal: which bare attribute keys and which writable command
                    params the device exposes with NO addhon entity. That is what
                    tells the maintainer what to add.
  * `future_capabilities` - for the types that declare which raw values they handle
                    (air purifier today), the values the device declares or is
                    currently reporting that this code does NOT handle. Passive: it
                    surfaces a firmware ahead of the integration without any entity
                    being guessed into existence.
  * `entities`    - what Home Assistant ACTUALLY has for this appliance, read from
                    the entity registry and cross-checked against the live state
                    machine. `coverage` above says what the code could map for this
                    TYPE; it is computed from static tables and stays identical
                    whether an entity was created or the whole platform crashed.
                    That gap is why "the control is missing from Home Assistant"
                    used to be unanswerable from a dump and needed the log as well.

The entry-level `platforms` block is the same reading one level up, and it exists
for the case the per-appliance one cannot cover: when setup fails there are no
appliances at all, and the registry is owned by Home Assistant core, so it is
still readable. Its per-domain `account` counts are the discriminator between a
platform that DIED and a device that was legitimately gated out: the two account
debug entities are created unconditionally, so a domain missing them never ran.

Identity (id/serial/mac and credential-ish keys) is redacted. The device nickname
(`name`) is kept readable on purpose, to correlate the dump with the physical
appliance. The entity inventory deliberately emits neither `entity_id` (its
object_id is the nickname slug) nor a raw `unique_id` (its prefix is the
appliance id): only the entity domain and the code-authored unique_id suffix.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    ACCOUNT_DEVICE_SUFFIX,
    APPLIANCE_AC,
    APPLIANCE_AP,
    APPLIANCE_FR,
    APPLIANCE_FRE,
    APPLIANCE_HO,
    APPLIANCE_HOB,
    APPLIANCE_IH,
    APPLIANCE_REF,
    APPLIANCE_WASH_GROUP,
    APPLIANCE_WD,
    APPLIANCE_WM,
    CONF_ENABLE_DEBUG,
    CONF_ENABLE_EXPERIMENTAL,
    CONF_ENABLE_MQTT_DEBUG,
    DOMAIN,
    PROGRAM_PARAM_NAMES,
)
from .debug_utils import _MAC_RE, redact_id
from .hon_commands import SETTINGS_COMMANDS, param_range, param_values

_LOGGER = logging.getLogger(__name__)

_REDACTED = "***"

# Keys whose VALUE is identity/credential material and must never leave the user's
# machine in cleartext. Matched case-insensitively by EXACT key name (not substring,
# which would risk nuking legitimately-named telemetry). `code` is the serial
# fallback in client/engine/appliance.py, hence redacted despite the innocuous name.
# `id`/`name`/`model`/`type` are deliberately absent: they are needed for mapping,
# and the nickname is kept readable by product decision.
_TO_REDACT = frozenset(
    {
        "serial",
        "serialnumber",
        "serial_number",
        "mac",
        "macaddress",
        "mac_address",
        "code",
        "nickname",
        "nick_name",
        "email",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "secret",
        # commandHistory carries identity in VALUES: transactionId is "<MAC>_<ts>"
        # (leaks the full MAC despite mac/macAddress being redacted), mobileId is the
        # phone-install id of whoever issued the last command (often a third party).
        "transactionid",
        "transaction_id",
        "mobileid",
        "mobile_id",
    }
)


# Settings-command params written by HaierClimateEntity (climate.py has no
# description table). AC only.
_AC_CLIMATE_PARAMS = frozenset(
    {"onOffStatus", "machMode", "tempSel", "windSpeed", "windDirectionVertical"}
)

# The entities this integration builds with NO description table behind them.
# `_mapped_sets` walks the per-type registries to learn which parameter each
# entity speaks to; these classes are not in any registry, so without a row here
# they would be the only entities in the inventory with nothing to say, and they
# include the two most-asked-about controls on a washer (start and stop).
#
# THE RULE, and it is the same one the registry walk follows: a row names a
# parameter only when a STATIC table in this repository names it. Where the code
# discovers the name from the DEVICE's own schema at runtime, the row stays empty
# and the section emits `null` -- which reads as "this entity exists and this dump
# cannot say what it speaks to". That is an honest gap; a guess would be a finding
# the reader can act on and be wrong about, which is worse than no finding.
#
# The two program selects are exactly that case. `HonProgramSelect` and
# `HonRefProgramSelect` walk the appliance's OWN commands for whichever of
# `PROGRAM_PARAM_NAMES` it happens to carry (select.py), so the answer is a
# property of the device, not of this repository, and the same select is
# `program` on one washer and `prCode` on the next. `button.start_program` is the
# same shape seen from the other end: it sends a whole command and fixes no
# parameter at all, so there is nothing to name. Its sibling `stop_program` DOES
# fix one -- `command_parameters={"onOffStatus": "0"}` in button.py, applied to
# the command's parameters before the send -- and that write appears in no other
# section of the dump, which is precisely why it is worth a row.
#
# `climate.climate` is the row where the rule is least comfortable, and it is
# followed anyway. All five writes are named because `_AC_CLIMATE_PARAMS` names
# them; but an air conditioner whose `settings` command has no `onOffStatus`
# drives power and mode through startProgram/stopProgram instead (see
# `climate._is_program_based`), so on that model two of these five are written
# somewhere other than where a reader would look first. Nothing is hidden to
# paper over it: `commands` a few lines below says which command really carries
# each parameter, and `coverage.command_params_expected_absent` says when the
# device carries it nowhere. Going silent instead would cost every reader of
# every other AC the remaining three names to spare one reader a second look.
#
# The write half is spelled out here as its own literal rather than reusing
# `_AC_CLIMATE_PARAMS`, so that the drift test comparing the two compares two
# independent statements instead of asserting a value against itself.
_CUSTOM_ENTITY_SOURCES: tuple[dict, ...] = (
    {
        "tag": "climate.climate",
        "types": (APPLIANCE_AC,),
        "read": (
            "settings.onOffStatus",
            "settings.machMode",
            "settings.tempSel",
            "tempIndoor",
            "settings.windSpeed",
            "settings.windDirectionVertical",
        ),
        "write": (
            "onOffStatus",
            "machMode",
            "tempSel",
            "windSpeed",
            "windDirectionVertical",
        ),
    },
    # Derived from TWO attributes, which is why it can never be a description row
    # (those read a single attr_key) and why it is the entity most likely to be
    # reported as "stuck on unknown": it needs both, and a washer publishing only
    # one of them produces exactly that.
    {
        "tag": "sensor.mean_water_consumption",
        "types": (APPLIANCE_WM, APPLIANCE_WD),
        "read": ("totalWaterUsed", "totalWashCycle"),
    },
    # Reads machMode (3 = paused) but writes the `pause` parameter of the
    # pauseProgram/resumeProgram commands -- the one row in this table whose two
    # halves name different parameters, and a reader who assumed they matched
    # would go looking for a writable machMode that does not exist.
    {
        "tag": "switch.pause",
        "types": APPLIANCE_WASH_GROUP,
        "read": ("machMode",),
        "write": ("pause",),
    },
    {"tag": "button.start_program", "types": APPLIANCE_WASH_GROUP},
    # The one row whose truth is per-APPLIANCE, not per-type. `button.py` hands
    # the stop button `command_parameters={"onOffStatus": "0"}` and applies each
    # only `if name in params`, so on a device whose `stopProgram` does not carry
    # the name the button fixes nothing. The live washer's stopProgram carries
    # `onOffStatus`; the live dryer's carries `returnStandby` and nothing else,
    # so shipping this row for the whole wash group states a write that cannot
    # happen on a real TD. `write_command` names the command the fixed parameters
    # have to be declared under, and `_entity_section` drops the ones the device
    # does not declare.
    {
        "tag": "button.stop_program",
        "types": APPLIANCE_WASH_GROUP,
        "write": ("onOffStatus",),
        "write_command": "stopProgram",
    },
    {"tag": "select.program", "types": APPLIANCE_WASH_GROUP},
    # The fridge program select. It reads seven attributes and its row said nothing at
    # all until issue #93, whose dump therefore printed `"select.ref_program": null`
    # beside a coverage block accusing `programName` of being unmapped -- two sections
    # of one document disagreeing about an entity that does read it.
    #
    # The four FLAGS come first because they are the only running-mode signal a fridge
    # shadow really carries (`_REF_MODE_FLAG_TO_PROGRAM`); the three identity fields are
    # the fallback behind them. `programName` is named here even though on every REF seen
    # so far it is addhOn's own "No Program" constant: a source row states what an entity
    # READS, not what the value turns out to be worth, and the select's offered-code
    # double-gate is what makes reading a sentinel harmless.
    #
    # No `write`: the select sends whole startProgram/stopProgram commands and owns no
    # named parameter -- the same reason `button.start_program` above stays null.
    {
        "tag": "select.ref_program",
        "types": (APPLIANCE_REF, APPLIANCE_FR, APPLIANCE_FRE),
        "read": (
            "intelligenceMode",
            "holidayMode",
            "quickModeZ1",
            "quickModeZ2",
            "programName",
            "prStr",
            "prCode",
        ),
    },
    # The air purifier's three fixed-key entities. Their parameters are already in
    # `AP_ENTITY_PARAMS` (which is why coverage sees them), but that frozenset says
    # only THAT the integration uses them, never WHICH entity uses which.
    #
    # The fan READS `onOffStatus` (fan.py) and does not write it: power is a whole
    # COMMAND action -- `ap_patch` returns startProgram with a machMode for
    # turn_on and stopProgram with no values at all for turn_off -- and the
    # dispatcher fills `onOffStatus` from the schema at its existing value. Naming
    # it as a write would send a reader looking for a writable onOffStatus on the
    # AP settings command that this integration never touches, which is the same
    # misdirection `button.start_program` stays null to avoid.
    {
        "tag": "fan.purifier",
        "types": (APPLIANCE_AP,),
        "read": ("onOffStatus", "machMode"),
        "write": ("machMode",),
    },
    {
        "tag": "select.aroma",
        "types": (APPLIANCE_AP,),
        "read": ("aromaStatus",),
        "write": ("aromaStatus",),
    },
    {
        "tag": "select.panel_light",
        "types": (APPLIANCE_AP,),
        "read": ("lightStatus",),
        "write": ("lightStatus",),
    },
    # The hood fan, a fixed-key entity with no description table for the registry
    # walk to find. Unlike the purifier fan it DOES write its read parameter:
    # `windSpeed` is the whole control, both the level it reports and the level it
    # sends -- including the zero that stops it, which is a speed like any other
    # and not a separate command.
    #
    # `onOffStatus` is NOT named here even though every hood write carries it: the
    # dispatcher fills it from the schema's own pinned value because the command
    # marks it mandatory, exactly as `fan.purifier` above does not claim the field
    # its stop command pins. The entity that genuinely chooses that value is the
    # power switch, and it declares it on its own row.
    #
    # The single-name write half is only TRUE because the speed goes out as a
    # sparse patch (`fan.HonHoodFan._send`). While it went through the full-command
    # sender the wire also carried `clockHH`/`clockMM`/`clockSS`,
    # `filterCleaningAlarmStatus` and every other member of the `settings` group,
    # and this row was telling the reporter something the dump itself disproved.
    {
        "tag": "fan.hood",
        "types": (APPLIANCE_HO,),
        "read": ("windSpeed",),
        "write": ("windSpeed",),
    },
    # The hood power switch, the other fixed-key HO entity. It reads the bare
    # `onOffStatus` attribute and drives it in both directions -- up through
    # `startProgram`, down through `stopProgram` -- so unlike the fan row above it
    # is the entity that OWNS this parameter.
    {
        # `switch.power`, not `switch.hood_power`: the join key is
        # `f"{domain}.{unique_id suffix}"`, and this entity's suffix is `power`
        # (same shape as `switch.pause` above). A tag that does not match the
        # suffix never joins the registry row, and the entity reports a null
        # source instead of its parameter.
        "tag": "switch.power",
        "types": (APPLIANCE_HO,),
        "read": ("onOffStatus",),
        "write": ("onOffStatus",),
    },
)

# Coverage noise: keys that are technically "unmapped" but are never mappable
# telemetry/controls, so they bury the real signal. Two mechanisms, complementary:
#   1. VALUE-TYPE (no list to maintain): a bare attribute whose VALUE is a dict/list is
#      a protocol envelope blob (commandHistory, lastConnEvent, activity, parameters,
#      mostUsedPrograms, ...). Validated on 4 live appliances: dict/list-valued bare
#      keys partition exactly into envelope + statistics, with zero genuine-signal loss.
#      This auto-catches future envelope blobs without a name list.
#   2. NAME DENYLIST (the scalar residue value-type can't see): protocol/debug scalars
#      and the program1..N definition slots. Matched lowercased, like _TO_REDACT.

# Attributes this integration WRITES ITSELF, in `client/engine/appliances/*.attributes`
# and in `Appliance.load_attributes`, before anything reads the dict. They are not device
# capabilities, and listing them as unmapped telemetry accuses the appliance of a gap it
# does not have -- which is exactly what issue #93 reported: a fridge whose diagnostics
# named `programName`, `modeZ1` and `modeZ2` as unmapped values "returned by the API",
# all three written by addhOn a moment earlier. `modeZ1`/`modeZ2` are gone (they were
# dead); `programName` stays because the wash group, oven, dishwasher and wine cooler
# genuinely read it.
#
# They are carved out of the SIGNAL only, after the mapped subtraction, so a type that
# maps one of them is untouched: `available` is read by the connectivity binary and
# `programName` by `sensor.program_name` on six types, so neither reaches this bucket
# there. The fridge, which maps neither, is where the carve-out actually fires.
#
# NAMED rather than silently dropped: a reader who saw `programName` in the attributes
# block and nowhere in the coverage lists would reasonably conclude the section is
# lossy. `attributes_unmapped_derived` says where it went.
#
# `active` and `pause` join it because they are the same thing seen on another type:
# `pause` is derived from `machMode` by the wash-group layers and read by nobody (the
# Pause switch reads `machMode` itself), so on every washer it was being reported as an
# unmapped device capability too.
#
# The independent check on any dump is `attributes_last_update`: it carries exactly the
# shadow parameter set, and none of these names is ever in it.
#
# Kept honest by `test_engine_derived_attrs_matches_the_engine` (test_diagnostics.py),
# which parses `client/engine/` for the assignments rather than trusting this list.
_ENGINE_DERIVED_ATTRS = frozenset({"programName", "available", "active", "pause"})

_COVERAGE_META_ATTRS = frozenset(
    {
        "resultcode",
        "debugenabled",
        "hightransrate",
        "statussyncrate",
        "stdtransrate",
        "transmode",
        # Scalar stats/protocol/test blobs the value-type rule can't see (they are
        # strings, not dict/list): programStats is a packed counter blob (sibling of
        # the already-carved programsCounter); the cloud-program ids and the test/force
        # flags are pure plumbing. Conservative: warning-ish flags (softWarn/detWarn)
        # and the program code (prCode) are deliberately KEPT as signal.
        "programstats",
        "cloudprogid",
        "cloudprogsrc",
        "forcedelete",
        "testcmdreceivestatus",
    }
)
_COVERAGE_META_ATTR_PATTERNS = (re.compile(r"(?i)^program\d+$"),)  # program1..N slots

# Coverage noise on the command-param axis: settings-command plumbing that is never a
# user-controllable function (the command selector, cloud endpoints, rule/visibility
# flags). Matched lowercased. Genuine controls (humiditySel, windDirectionHorizontal,
# specialMode, ...) are deliberately NOT here.
_COVERAGE_META_PARAMS = frozenset(
    {
        "category",
        "httpendpoint",
        "mqttendpoint",
        "resw",
        "operationname",
        "programrules",
        "remoteactionable",
        "remotevisible",
        "winddirectionverticalpositionsequence",
    }
)


# Bounds for the future-capability section. It is passive evidence, not a report:
# a firmware declaring hundreds of values must add a hint to the dump, never bloat
# it. Truncation is announced with a `truncated` flag rather than silently applied.
# Bound for the entity inventory, per appliance and per domain. An appliance has a
# few dozen entities today; the cap is a runaway guard, not a filter, and like the
# future-capability bounds it announces itself rather than truncating silently.
_ENTITY_MAX_PER_DOMAIN = 80

# Bound on materialising a RANGE's grid into the dump (see `_param_schema`). Only a
# grid this small is enumerated, so the never-enumerate-a-setpoint rule stands. 8
# covers every few-position control observed so far -- a 0/1 lock or tone, a 0..2
# panel light, a 0..4 aroma -- and eight short numeric strings are noise next to the
# blocks around them.
_RANGE_MAX_MATERIALISED = 8

_FUTURE_MAX_ENTRIES = 40
_FUTURE_MAX_VALUES = 20
# A separate CHARACTER bound. An unhandled state value is one scalar, so it needs a
# length cap rather than a count; reusing _FUTURE_MAX_VALUES here read as "20 values"
# while meaning "80 characters". Generous, since a value long enough to be truncated
# is itself the interesting evidence.
_FUTURE_MAX_VALUE_CHARS = 80


def _is_meta_attr(name: str) -> bool:
    """True if a bare attribute name is protocol/debug noise (scalar residue)."""
    return name.lower() in _COVERAGE_META_ATTRS or any(
        pattern.match(name) for pattern in _COVERAGE_META_ATTR_PATTERNS
    )


def _redact_title(title: str | None) -> str | None:
    if not title:
        return None
    if "@" not in title:
        return title
    prefix, domain_and_suffix = title.rsplit("@", 1)
    open_paren = prefix.rfind("(")
    safe_prefix = prefix[: open_paren + 1] if open_paren >= 0 else ""
    return f"{safe_prefix}***@{domain_and_suffix}"


def _redact_email(email: str | None) -> str | None:
    if not email:
        return None
    if "@" in email:
        _, domain = email.split("@", 1)
        return f"***@{domain}"
    return _REDACTED


def _jsonable(value):
    """Coerce a leaf value to a JSON-native primitive.

    The merged attributes dict carries wrapper objects (HonAttribute from the device
    shadow, HonParameter from ``appliance.settings``), not primitives; HA's JSON
    encoder raises ``TypeError`` on them. Unwrap a ``.value`` if present (one level),
    else stringify, so the dump never carries an unserializable object.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _MAC_RE.sub(_REDACTED, value)
    if hasattr(value, "value"):
        inner = value.value
        if inner is None or isinstance(inner, (bool, int, float)):
            return inner
        return _MAC_RE.sub(_REDACTED, str(inner))
    return _MAC_RE.sub(_REDACTED, str(value))


def _redact(value):
    """Recursively replace the value of any identity/credential key with ``***``.

    Redaction is keyed on the dict KEY name (exact, case-insensitive); leaf telemetry
    values, enum lists and numeric ranges pass through (coerced to JSON primitives).
    """
    if isinstance(value, Mapping):
        return {
            key: (
                _REDACTED
                if isinstance(key, str) and key.lower() in _TO_REDACT
                else _redact(val)
            )
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return _jsonable(value)


def _param_value(param):
    """Scalar current value of a parameter, coerced to a JSON-safe primitive."""
    return _jsonable(getattr(param, "value", None))


def _param_schema(param) -> dict:
    """Schema of one command parameter: value + metadata, plus range (min/max/step,
    and the materialised grid when it is small enough) for a range param OR enum as
    a fallback only when the param is not a range."""
    schema: dict = {
        "value": _param_value(param),
        "typology": getattr(param, "typology", None),
        "category": getattr(param, "category", None),
        "mandatory": getattr(param, "mandatory", None),
    }
    # Check range FIRST and emit ONLY min/max/step for a range param. param_values()
    # calls `.values`, which on a HonParameterRange ENUMERATES the whole grid (up to
    # _MAX_RANGE_VALUES = 100000 strings) synchronously on the event loop -- a huge,
    # redundant dump for what min/max/step already describe. program_options makes the
    # same rule explicit ("never call .values on a HonParameterRange"). enum is only
    # meaningful for enum/fixed params, where param_range() returns None.
    rng = param_range(param)
    if rng is not None:
        low, high, step = rng
        schema["min"], schema["max"], schema["step"] = rng
        # ...and, for a SMALL grid only, the values it actually materialises.
        #
        # min/max/step cannot answer the question a missing 0/1 control raises.
        # param_range() casts through float(), so a schema spelling its bounds
        # "0"/"1" and one spelling them "0.0"/"1.0" print IDENTICALLY here, while
        # `.values` yields ['0', '1'] for the first and ['0.0', '1.0'] for the
        # second -- and the capability gates compare exactly those STRINGS
        # (air_purifier.supports_lock is `lock_values == {"0", "1"}`). A single
        # decimal-spelled minimumValue or incrementValue therefore removes a
        # control, and until now the deciding input appeared nowhere in the dump.
        #
        # The bound is what keeps the rule above intact: a real setpoint range is
        # still never enumerated. The point count is computed ARITHMETICALLY, and
        # param_range() has already guaranteed step > 0 and max >= min, so a
        # 0..1400 step 100 grid is refused without `.values` ever being read.
        # Emitted under its own key, never `enum`: this is the grid a range
        # materialises, not an enumeration the device declares.
        if (high - low) / step + 1 <= _RANGE_MAX_MATERIALISED:
            schema["values"] = param_values(param)
    else:
        enum = param_values(param)
        if enum:
            schema["enum"] = enum
    return schema


def _model_attributes(appliance) -> dict:
    """Cloud catalogue metadata for the MODEL, flattened to parName -> parValue.

    Read straight off the appliance (`applianceModel.attributes`, already
    normalised by the engine), not off the coordinator entry: it is per-model
    and immutable for the session, so it never belongs in the polled snapshot.
    Returns {} for any appliance implementation that does not expose it.
    """
    raw = getattr(appliance, "model_attributes", None)
    if not isinstance(raw, Mapping):
        return {}
    return {str(name): value for name, value in raw.items()}


def _command_schema(appliance) -> dict:
    """Per-command, per-parameter schema for every command the appliance exposes."""
    commands = getattr(appliance, "commands", None)
    if not isinstance(commands, Mapping):
        return {}
    out: dict = {}
    for cmd_name, cmd in commands.items():
        params = getattr(cmd, "parameters", None)
        if isinstance(params, Mapping):
            out[str(cmd_name)] = {
                str(p_name): _param_schema(p) for p_name, p in params.items()
            }
        else:
            out[str(cmd_name)] = {}
    return out


def _read_chain(key) -> list[str]:
    """The attribute keys `base_entity._get_attr` really tries, in order.

    A description names ONE key, and when that key carries the `settings.` prefix
    the name is only the first thing the entity looks at: `_get_attr` tries the
    key whole, then the statistics container, and then -- only for a key that
    itself carries the prefix -- the BARE spelling underneath it. Emitting just
    the dotted name would hide exactly the fallback chain this section exists to
    reveal. A reader asking why the climate entity shows no temperature would
    search the `attributes` map for `settings.tempSel`, find nothing, and never
    learn that the entity would equally have accepted the bare `tempSel` printed
    a few lines above it -- which is the same chain-hiding that made
    `actualWeight` -> `weight` invisible until someone read sensor.py.

    The statistics container is deliberately NOT listed as a third link.
    `hon_client._get_attributes` merges it into `attributes` before this dump ever
    sees either, so naming it would point the reader at a place that does not
    appear in the document.
    """
    text = str(key)
    prefix = "settings."
    if text.startswith(prefix):
        return [text, text[len(prefix):]]
    return [text]


def _source_row(read=(), write=()) -> dict[str, list[str]] | None:
    """One `entities.sources` value: what an entity reads, what it writes.

    An EMPTY half is omitted rather than emitted as `[]`, so a read-only sensor is
    visibly read-only instead of carrying a `"write": []` the reader has to
    interpret; and a row with NEITHER half is `null`, which is how this section
    says "the entity exists and this dump cannot name its parameter" without
    inventing one.

    Order is preserved and duplicates dropped (`dict.fromkeys`, never `sorted`):
    for a read chain the SEQUENCE is the information, because it is the order
    `_get_attr` tries the keys in, and sorting it would turn a fallback chain into
    an unordered set of equally-plausible names.
    """
    # A bare string is ONE name, not a sequence of characters. A row written
    # `write="onOffStatus"` instead of `write=("onOffStatus",)` -- the missing
    # trailing comma -- would otherwise be emitted as nine invented one-character
    # parameter names under a real entity tag.
    if isinstance(read, (str, bytes)):
        read = (read,)
    if isinstance(write, (str, bytes)):
        write = (write,)
    row: dict[str, list[str]] = {}
    reads = [str(name) for name in read if name]
    if reads:
        row["read"] = list(dict.fromkeys(reads))
    writes = [str(name) for name in write if name]
    if writes:
        row["write"] = list(dict.fromkeys(writes))
    return row or None


def _note_missing_registry(module: str, failed: list[str]) -> None:
    """Record one platform module that would not import, and log why.

    Separate from the `except` bodies that call it so that each of them stays two
    statements long: the seven of them are already the widest thing in
    `_mapped_sets`, and a walk of tables reads badly when two thirds of it is
    error handling. `exc_info` is passed HERE and not once at the end, because
    the traceback a maintainer needs is the one from the import that failed and
    not from whichever ran last.
    """
    failed.append(module)
    _LOGGER.debug(
        "Diagnostics debug: registry module %s unavailable", module, exc_info=True
    )


def _mapped_sets(
    app_type,
) -> tuple[
    set[str], set[str], dict[str, dict[str, list[str]] | None] | None, list[str]
]:
    """(mapped attribute keys, mapped params, entity sources, missing modules).

    Registries are imported lazily so diagnostics.py keeps a tiny top-level import
    surface and cannot be caught in an import cycle, and each import is guarded on
    its OWN, so an import hiccup costs that module's tables and never the dump.

    The THIRD value is the static per-type index behind `entities.sources`: which
    raw parameter each entity of this type reads and writes, keyed by the same
    ``<domain>.<unique_id suffix>`` tag that ``by_domain``, ``disabled``,
    ``hidden`` and ``not_created`` already speak, so the five views join on one
    key. It is built HERE, in the same traversal that produces the two coverage
    sets, rather than in a helper of its own, and that is a correctness
    requirement rather than a tidiness one: a second lazy-import block with
    DIFFERENT membership means a single broken platform module makes one dump
    contradict itself. With `select` imported only by the sources half, a
    `select.py` that fails to import would produce a dump whose `coverage`
    confidently reports `tempSelZ3` as mapped while `entities.sources` is a lone
    null claiming nothing could be looked at. One walk, one degradation decision,
    and the two sections can never disagree about whether the tables were read.

    One walk, however, must NOT mean one failure. Each import therefore gets its
    OWN `try` and its own empty stand-in, so a module that will not import costs
    its own tables and nothing else. Rebuilt on the four archived live
    appliances, a single unimportable module used to roughly double or triple
    `attributes_unmapped` on every one of them, and to invent three
    `command_params_unmapped` entries (`tempSelZ1`, `tempSelZ2`, `tempSelZ3`)
    about setpoints that exist and work. The exact pairs are deliberately not
    quoted: they move with every table row added, and reconstructing an appliance
    from an archived dump gives a different denominator than the live object did,
    so three mutually inconsistent sets of them were produced while this was
    being written. The blast radius is the claim; the figures are not. Fabricating
    entries on the list this module calls the gold signal is the one thing that
    list may not do, and it is a strictly worse failure than the silence the
    single `try` was chosen over: silence is unhelpful, an accusation is wrong.
    Per import, `select` costs the REF nothing measurable at all and the AC one
    name (`windDirectionHorizontal`), which is what it actually owns there.

    The benefit is real for FIVE of the seven, not all of them, and the two
    exceptions are worth naming because the obvious worked example is one of
    them. `binary_sensor`, `number`, `select`, `sensor` and `switch` are leaves:
    nothing else here imports them, so each really does degrade alone.
    `air_purifier` is imported by all five and `program_options` by three, and
    Home Assistant loads the platform modules at platform setup -- so a genuinely
    broken `air_purifier.py` means its five importers never loaded either, they
    are absent from `sys.modules`, this walk re-imports them and they fail again.
    Breaking it costs six of the seven tables no matter how the guards are
    arranged. Take `sensor` on a fridge, or `select` on an AC, as the case this
    isolation actually rescues.

    `registries_unavailable` NAMES the modules that failed instead of saying
    `True`, because with per-import degradation the flag no longer means "every
    number in this dict was measured against nothing". It means "the tables these
    modules own are missing from the numerators", and only the list tells the
    reader whether that touches the axis they came to read: `sensor` missing
    invalidates `attributes_unmapped`, `air_purifier` missing invalidates
    nothing at all on a fridge.

    A None third value is reserved for the TOTAL collapse -- nothing at all could
    be named -- so that a dump which could not look at any table costs the reader
    ONE null instead of a null per entity, which would read as "every entity was
    looked up and none was found". A partial failure returns the rows it did
    read, and the entities whose table was lost take their own explicit null in
    `entities.sources`: that is the narrower and truer statement, and it names
    them one by one instead of blanking the whole map.
    """
    # Custom entity classes have no description table, so their bare attributes
    # used to be listed in a `_CUSTOM_MAPPED_ATTRS` constant and seeded here.
    # Measured on every type the constant covered: the registry walk below
    # already supplies all eight names, so the seed contributed nothing and is
    # gone. The guarantee it existed for -- a custom class's attribute never
    # reported unmapped -- is pinned behaviourally in the tests, which is the
    # loud failure a narrowed description table should produce rather than the
    # silent correction a standing seed would apply.
    mapped_attrs: set[str] = set()
    mapped_params: set[str] = set()
    sources: dict[str, dict[str, list[str]] | None] = {}
    unavailable: list[str] = []
    # Nine imports, nine guards. Written out rather than driven by a table of
    # module/name strings on purpose: `from .sensor import SENSORS` is a spelling
    # a linter, a grep and `rename` all understand, and this walk is already one
    # rename away from silently collapsing a numerator (there is no test that can
    # see a table nobody added here).
    try:
        from .air_purifier import AP_ENTITY_PARAMS
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        AP_ENTITY_PARAMS = frozenset()
        _note_missing_registry("air_purifier", unavailable)
    try:
        from .hob import HOB_ENTITY_PARAMS
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        HOB_ENTITY_PARAMS = frozenset()
        _note_missing_registry("hob", unavailable)
    try:
        from .hood import HOOD_ENTITY_PARAMS
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        HOOD_ENTITY_PARAMS = frozenset()
        _note_missing_registry("hood", unavailable)
    try:
        from .binary_sensor import BINARY_SENSORS, _CONNECTIVITY, _UNIVERSAL_GATED
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        BINARY_SENSORS, _CONNECTIVITY, _UNIVERSAL_GATED = {}, None, ()
        _note_missing_registry("binary_sensor", unavailable)
    try:
        from .number import NUMBERS, _AP_TIMING_NUMBERS, _PROGRAM_OPTION_NUMBERS
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        NUMBERS, _AP_TIMING_NUMBERS, _PROGRAM_OPTION_NUMBERS = {}, (), ()
        _note_missing_registry("number", unavailable)
    try:
        from .program_options import STARTPROGRAM_COMMAND
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        STARTPROGRAM_COMMAND = ""
        _note_missing_registry("program_options", unavailable)
    try:
        from .select import (
            _AC_DIRECTION_SELECTS,
            _PROGRAM_OPTION_SELECTS,
            HonRefProgramSelect,
        )
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        _AC_DIRECTION_SELECTS, _PROGRAM_OPTION_SELECTS = (), ()
        HonRefProgramSelect = None
        _note_missing_registry("select", unavailable)
    try:
        from .sensor import HOB_ZONE_TIME_ATTRS, SENSORS
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        HOB_ZONE_TIME_ATTRS, SENSORS = frozenset(), {}
        _note_missing_registry("sensor", unavailable)
    try:
        from .switch import (
            _AIR_PURIFIER_SWITCHES,
            _PROGRAM_OPTION_SWITCHES,
            _SETTINGS_SWITCHES,
        )
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        _AIR_PURIFIER_SWITCHES, _PROGRAM_OPTION_SWITCHES, _SETTINGS_SWITCHES = (
            (),
            (),
            {},
        )
        _note_missing_registry("switch", unavailable)

    # ONE pass per table: the coverage sets get the DECLARED names (unchanged --
    # `mapped_attrs` is the attribute axis' numerator and moving it would silently
    # restate every published coverage figure), while the source rows get the
    # declared name EXPANDED through `_read_chain`, which is the reader-facing
    # question and a different one.
    for desc in SENSORS.get(app_type, ()):
        declared = [desc.attr_key, *(getattr(desc, "attr_fallbacks", ()) or ())]
        mapped_attrs.update(declared)
        sources[f"sensor.{desc.key}"] = _source_row(
            read=[key for name in declared for key in _read_chain(name)]
        )
    for desc in BINARY_SENSORS.get(app_type, ()):
        mapped_attrs.add(desc.attr_key)
        sources[f"binary_sensor.{desc.key}"] = _source_row(
            read=_read_chain(desc.attr_key)
        )
    # The only row in this walk with no type gate and no table to be empty, so
    # it is also the only one that needs a None check rather than an empty
    # stand-in: `binary_sensor` failing leaves nothing to read `.attr_key` off.
    if _CONNECTIVITY is not None:
        mapped_attrs.add(_CONNECTIVITY.attr_key)
        sources[f"binary_sensor.{_CONNECTIVITY.key}"] = _source_row(
            read=_read_chain(_CONNECTIVITY.attr_key)
        )
    for desc in _UNIVERSAL_GATED:
        mapped_attrs.add(desc.attr_key)
        sources[f"binary_sensor.{desc.key}"] = _source_row(
            read=_read_chain(desc.attr_key)
        )

    for desc in NUMBERS.get(app_type, ()):
        mapped_params.add(desc.param)
        sources[f"number.{desc.key}"] = _source_row(
            read=_read_chain(desc.param), write=[desc.param]
        )
    # Settings-command switches (AC toggles + wine-cooler light) map their write param.
    for desc in _SETTINGS_SWITCHES.get(app_type, ()):
        mapped_params.add(desc.param)
        sources[f"switch.{desc.key}"] = _source_row(
            read=_read_chain(desc.param), write=[desc.param]
        )
    # A program option is BUFFERED, not sent: it is written to the pending store
    # and applied to `startProgram` when the start button fires. That is why its
    # read chain has a second link nothing else in this walk has -- the option is
    # read bare first and then under `startProgram.<param>`, because unlike
    # `settings` the startProgram command is not mirrored into the device shadow
    # (`program_options.HonProgramOptionEntity._current_raw`). These params were
    # once left out of both coverage numerators, on the argument that they live
    # on startProgram, which `_settings_param_names` never reads. That argument
    # is false and the loop below says where; the sentence is corrected here
    # rather than deleted, because it is the belief a reader arrives with and
    # the code makes no sense while they still hold it.
    #
    # `program_options` contributes exactly one thing to this walk: the command
    # name those options are read under. Without it the second link cannot be
    # spelled: with the stand-in `STARTPROGRAM_COMMAND = ""` the loop would emit
    # `read: ['tumblingStatus', '.tumblingStatus']` -- a fabricated key name no
    # device ever publishes, under a real entity tag -- which is worse than no
    # row at all. Both
    # halves of that were measured on the live TD: emitting the chain truncated
    # makes `_coverage`'s `reachable` test find no published spelling for
    # `tumblingStatus` and drop it into `attributes_expected_absent` -- telling
    # the reader the dryer does not have a control it does have, since the TD
    # publishes it only as `startProgram.tumblingStatus` -- WHILE `sources` two
    # sections below still names the switch that reads and writes it. That is
    # the contradiction shape this whole section exists to remove. Skipping the
    # loop drops both halves back to what they said before this walk read the
    # option tables: three names return to the unmapped lists -- so `coverage` is
    # not silent, it still accuses `tumblingStatus`, exactly as it did at
    # 5d12cb2 -- but `sources` no longer contradicts it, and
    # `registries_unavailable` names the module that caused it. Nor does the
    # skip cover every module that can produce this shape: with only `number`
    # broken, the live WM gains `delayTime` in `attributes_expected_absent`
    # while `sensor.delay_time` two sections below still reads it. That one is
    # not fixed here; the list in `registries_unavailable` is what tells the
    # reader to distrust that axis. Little is lost
    # in practice either way: number.py, select.py and switch.py each import
    # `program_options` themselves, so a real break in it empties all three
    # tables anyway and this branch changes nothing.
    for prefix, table in (
        (
            ("number.opt_", _PROGRAM_OPTION_NUMBERS),
            ("select.opt_", _PROGRAM_OPTION_SELECTS),
            ("switch.opt_", _PROGRAM_OPTION_SWITCHES),
        )
        if STARTPROGRAM_COMMAND
        else ()
    ):
        for desc in table:
            if app_type not in (getattr(desc, "types", None) or ()):
                continue
            # BOTH numerators, not just `sources`. The stated reason for leaving
            # them out -- "they live on startProgram, which `_settings_param_names`
            # never reads" -- is false on the live TD, whose `settings` command
            # carries `tumblingStatus`, and does not apply at all on the attribute
            # axis, where the option IS a bare shadow key (live TD:
            # `antiCreaseTime`, `sterilizationStatus`). Leaving them out ships one
            # block whose `coverage` calls a name unmapped while `entities.sources`
            # twenty lines below names the switch that reads and writes it -- the
            # exact failure the `_AC_DIRECTION_SELECTS` correction exists to avoid.
            mapped_attrs.add(desc.param)
            mapped_params.add(desc.param)
            sources[f"{prefix}{desc.key}"] = _source_row(
                read=[desc.param, f"{STARTPROGRAM_COMMAND}.{desc.param}"],
                write=[desc.param],
            )
    if app_type == APPLIANCE_AC:
        mapped_params |= _AC_CLIMATE_PARAMS
        # The manual louver selects. `desc.attr` is the DOTTED read path and
        # `desc.param` the settings parameter written, and they differ, which is
        # the whole reason this section is worth its bytes.
        #
        # `mapped_params.add` is here and not only in `sources`, and it is a
        # deliberate correction rather than an oversight repeated. Before this
        # section existed, `windDirectionHorizontal` sat in
        # `command_params_unmapped` -- "the device has it and no addhon entity
        # maps it" -- and nothing in the dump disproved it. `sources` now names
        # `select.fan_direction_horizontal` as its writer ten lines below, so
        # leaving the coverage line alone would ship one block making two
        # statements the reader can see contradict each other, on the list this
        # module calls the gold signal. Nothing published moves:
        # `command_params_total` is `len(settings_params) - len(params_meta)`,
        # which never reads `mapped_params`, and this name is not a meta param.
        for desc in _AC_DIRECTION_SELECTS:
            mapped_params.add(desc.param)
            sources[f"select.{desc.key}"] = _source_row(
                read=_read_chain(desc.attr), write=[desc.param]
            )
    if app_type in APPLIANCE_WASH_GROUP:
        mapped_params.update(PROGRAM_PARAM_NAMES)
    if app_type in (APPLIANCE_REF, APPLIANCE_FR, APPLIANCE_FRE) and HonRefProgramSelect:
        # The fridge program select is a fixed-key entity, so the registry walk cannot
        # see what it reads. `_CUSTOM_ENTITY_SOURCES` names those attributes, but that
        # table feeds `entities.sources` ONLY -- never these two sets -- so without this
        # line a fridge publishing `prCode` or `prStr` would have them reported as
        # unmapped device capabilities on the very dump whose `sources` block names the
        # entity that reads them. That contradiction is the one issue #93 was about, and
        # naming the attributes in the source row without mapping them here would have
        # rebuilt it for the two members nobody had noticed.
        #
        # DERIVED names are subtracted rather than listed: `programName` is written by
        # `ApplianceExtra.attributes`, not by the appliance, so it belongs to
        # `attributes_unmapped_derived` and must NOT be counted as mapped telemetry --
        # that would put addhOn's own output back into the device's denominator. The set
        # is computed from the select's own tuple so a field added there is picked up
        # here, and a field that becomes engine-derived drops out, with no second list.
        mapped_attrs |= (
            set(HonRefProgramSelect._REF_ACTIVE_PROGRAM_ATTRS) - _ENGINE_DERIVED_ATTRS
        )
    if app_type in (APPLIANCE_IH, APPLIANCE_HOB):
        # The per-zone remaining-time sensors are DERIVED from two attributes
        # each, so they are custom classes with no description row and the walk
        # above cannot see either half. Twelve live readings would otherwise be
        # reported unmapped on every hob dump while `entities.sources` two
        # sections below named the sensors that read them.
        mapped_attrs |= HOB_ZONE_TIME_ATTRS
        # The intake-limit select is a fixed-key entity, so the same applies to
        # the one parameter a hob lets anyone write.
        mapped_attrs |= HOB_ENTITY_PARAMS
        mapped_params |= HOB_ENTITY_PARAMS
        sources["select.power_limit"] = _source_row(
            read=_read_chain("powerManagement"), write=["powerManagement"]
        )
        for zone in sorted(
            {name.rsplit("Z", 1)[1] for name in HOB_ZONE_TIME_ATTRS}, key=int
        ):
            sources[f"sensor.remaining_time_zone{zone}"] = _source_row(
                read=[
                    key
                    for unit in ("HH", "MM")
                    for key in _read_chain(f"remainingTime{unit}Z{zone}")
                ]
            )
    if app_type == APPLIANCE_HO:
        # Same shape as the AP block below, same reason. The hood's five parameters
        # are each read as state AND written as a command field, but only two of the
        # five reach the walk above: `windSpeed` belongs to the fan and
        # `onOffStatus` to the power switch, both fixed-key entities with no table,
        # and `delayTime`/`delayTimeStatus` are written from the
        # NUMBERS/_SETTINGS_SWITCHES tables, which feed `mapped_params` alone and
        # never the attribute axis. Without this line the dump kept naming
        # `windSpeed` an unmapped control on a hood that ships a fan for it.
        mapped_attrs |= HOOD_ENTITY_PARAMS
        mapped_params |= HOOD_ENTITY_PARAMS
    if app_type == APPLIANCE_AP:
        # The AP fan, light, aroma select, toggles and timing numbers are fixed-key
        # entities or live outside the NUMBERS/_SETTINGS_SWITCHES tables, so the
        # registry walk above cannot see them. Each name is BOTH read as state and
        # written as a command field, hence both axes.
        mapped_attrs |= AP_ENTITY_PARAMS
        mapped_params |= AP_ENTITY_PARAMS
        for prefix, table in (
            ("number.", _AP_TIMING_NUMBERS),
            ("switch.", _AIR_PURIFIER_SWITCHES),
        ):
            for desc in table:
                sources[f"{prefix}{desc.key}"] = _source_row(
                    read=_read_chain(desc.param), write=[desc.param]
                )

    # How many rows the PLATFORM tables produced, counted before the literal
    # ones below join them. Nothing below this line can fail to import --
    # `_CUSTOM_ENTITY_SOURCES` is written out three hundred lines above, in this
    # file -- so the SIZE of the finished map cannot say whether this walk was
    # able to look at anything. This count can, and a healthy walk always makes
    # it non-zero: `_CONNECTIVITY` above is added with no type gate.
    platform_rows = len(sources)

    # Last, the entities no registry knows about. Guarded field by field even
    # though the table three hundred lines above is a literal in this same file:
    # this loop is OUTSIDE the try above, and `_appliance_block` is called with no
    # try/except around it from either entry point, so one malformed row added
    # later -- a missing `types`, a tag with no dot -- would not cost a section, it
    # would cost the reporter the entire dump.
    for entry in _CUSTOM_ENTITY_SOURCES:
        tag = str(entry.get("tag") or "")
        domain, _dot, suffix = tag.partition(".")
        if not domain or not suffix:
            continue
        if app_type not in (entry.get("types") or ()):
            continue
        sources[tag] = _source_row(
            read=[
                key
                for name in (entry.get("read") or ())
                for key in _read_chain(name)
            ],
            write=entry.get("write") or (),
        )
    # No platform row at all is the TOTAL collapse, and the only thing the None
    # third value is for: the reader gets ONE null instead of a null per entity,
    # which would read as "every entity was looked up and none was found".
    # Anything the walk DID read is returned as it stands, and the entities
    # behind a lost table then take one explicit null each in `entities.sources`
    # -- the narrower and truer statement, naming them one at a time instead of
    # blanking a map that is mostly correct.
    return (
        mapped_attrs,
        mapped_params,
        sources if platform_rows else None,
        unavailable,
    )


# Custom rows whose `write` half is a set of FIXED parameters the entity only
# gets to apply when the device declares them under a named command. Built from
# the table above rather than repeated, so a row cannot grow the key and be
# forgotten here.
_FIXED_WRITE_COMMANDS: dict[str, str] = {
    str(entry.get("tag") or ""): str(entry["write_command"])
    for entry in _CUSTOM_ENTITY_SOURCES
    if entry.get("write_command")
}


def _declared_command_params(appliance) -> dict[str, set[str]] | None:
    """Parameter names the appliance declares, per command, or None if unread.

    Distinct from `_settings_param_names` (writables, settings commands only)
    and from `_command_param_names` (every command flattened into one set): the
    question here is which command a name is declared UNDER, because a fixed
    parameter applied by an entity is only applied when that command carries it.

    None and `{}` are DIFFERENT answers and must never be folded together, for
    the same reason `_registry_entries` keeps them apart. `{}` is an appliance
    whose command schema was read and declares nothing -- the live dryer -- and
    `_declared_write_only` narrows on it, which is correct. None is a schema
    this dump could not read at all, and narrowing there would print "not
    looked" as "the device does not declare it". Returning `{}` for both made
    that substitution on every unreadable appliance: it is a Mapping, so the
    guard downstream let it through and `button.stop_program` shipped as null,
    indistinguishable from the dryer that really applies nothing.
    """
    commands = getattr(appliance, "commands", None)
    if not isinstance(commands, Mapping):
        return None
    out: dict[str, set[str]] = {}
    for cmd_name, cmd in commands.items():
        params = getattr(cmd, "parameters", None)
        if isinstance(params, Mapping):
            out[str(cmd_name)] = {str(name) for name in params}
    return out


def _settings_param_names(appliance) -> set[str]:
    """Names of every parameter under a settings/setParameters command (writables)."""
    commands = getattr(appliance, "commands", None)
    if not isinstance(commands, Mapping):
        return set()
    names: set[str] = set()
    for cmd_name in SETTINGS_COMMANDS:
        cmd = commands.get(cmd_name)
        params = getattr(cmd, "parameters", None) if cmd is not None else None
        if isinstance(params, Mapping):
            names.update(str(k) for k in params)
    return names


def _command_param_names(appliance) -> set[str]:
    """Names of every parameter under ANY command, writable through it or not.

    `_settings_param_names` above answers 'what can be written through the
    settings command', which is the universe the `command_params_unmapped` axis
    is measured against and must stay measured against. This helper answers a
    different question -- 'does this device's schema know this parameter AT
    ALL' -- and exists for `command_params_expected_absent`, where the
    settings-only universe manufactures false positives: `program` and `prCode`
    are mapped for every wash appliance and live under `startProgram`, and
    `onOffStatus` is mapped for the air purifier and lives under
    `startProgram`/`stopProgram`. Measured on the live 2026-06-22 dump,
    subtracting only the settings params reported all three as missing from
    devices whose `commands` section printed them a few lines further down.

    Only KEYS are read. Iterating a parameter mapping's VALUES would reach
    `HonParameterRange.values`, which materialises the entire grid (the trap
    documented in `_param_schema`), so this walk stays proportional to the
    number of parameters the device declares and never to their ranges.

    Every read is guarded, and the guards are a BACKSTOP rather than the dump's
    protection - be exact about that, because the tempting claim is the wrong
    one. `_command_schema` already walks EVERY command and reads the same
    `.parameters` surface, and `_appliance_block` calls it one line above
    `_coverage`, so an appliance whose `startProgram.parameters` raises has
    already taken the dump down before this helper is reached; measured,
    building the block for such an appliance propagates RuntimeError out of
    `_command_schema`. That exposure predates this change and is not fixed
    here. What the guards below DO buy is that this helper cannot ADD a way to
    fail. The loop setup is inside the guard as well as the loop body, and that
    part is not theoretical: `_command_schema` iterates the commands mapping
    with `.items()` and `_settings_param_names` with `.get(name)`, so a foreign
    `commands` object whose `.values()` view alone raises survives both of them
    and would die here, in new code, on the one axis this module treats as
    inviolable.
    """
    commands = getattr(appliance, "commands", None)
    if not isinstance(commands, Mapping):
        return set()
    names: set[str] = set()
    try:
        entries = list(commands.values())
    except Exception:  # pragma: no cover - diagnostics must never crash
        _LOGGER.debug(
            "Diagnostics debug: command list unreadable", exc_info=True
        )
        return names
    for cmd in entries:
        try:
            params = getattr(cmd, "parameters", None)
            if isinstance(params, Mapping):
                names.update(str(k) for k in params)
        except Exception:  # pragma: no cover - diagnostics must never crash
            _LOGGER.debug(
                "Diagnostics debug: command parameters unreadable",
                exc_info=True,
            )
    return names


def _coverage(
    app_type, attributes: Mapping, statistics: Mapping, appliance, mapped=None
) -> dict:
    """What the device exposes with no addhon entity (the gold signal).

    Attribute axis: only BARE keys (no dot) are considered; dotted ``settings.*``
    keys mirror command parameters and belong to the command-param axis instead. The
    unmapped set is partitioned so the maintainer reads pure signal first; nothing is
    dropped:
      * ``attributes_unmapped``            - mappable telemetry candidates (the gold);
      * ``attributes_unmapped_statistics`` - keys from the statistics container;
      * ``attributes_unmapped_meta``       - protocol envelope blobs (dict/list-valued)
                                             + scalar debug/protocol noise + program slots;
      * ``attributes_unmapped_derived``    - fields THIS INTEGRATION wrote into the
                                             shadow dict itself (_ENGINE_DERIVED_ATTRS).
                                             Not device capabilities, so they are neither
                                             signal nor part of the denominator; named
                                             rather than dropped so the section stays
                                             legible against the attributes block.
    The command-param axis is split the same way (``command_params_unmapped`` vs
    ``command_params_unmapped_meta``).

    Both axes are then reported once more in MIRROR image.
    ``attributes_expected_absent`` and ``command_params_expected_absent`` are
    the keys this integration maps for the TYPE and this DEVICE does not have
    at all. The unmapped lists answer 'what should we add'; the mirror answers
    'why is the control I expected not in Home Assistant', which until now was
    the one coverage question a dump could not answer. Issue #75 is the worked
    example: its reporter established for himself, across two dumps four days
    apart and a decompiler pass, that his fridge declares a vtRoom2 zone and
    publishes no ``tempZ4``. On the live REF dump this line prints thirteen
    names on the first download, and ``tempZ4`` is one of them.
    """
    mapped_attrs, mapped_params, sources_index, registries_unavailable = (
        mapped if mapped is not None else _mapped_sets(app_type)
    )
    settings_params = _settings_param_names(appliance)
    # A SECOND, wider universe, used only by the expected-absent mirror below:
    # every parameter of every command, not just the settings ones. The two are
    # deliberately different sets; see the comment on the mirror keys.
    command_params = _command_param_names(appliance)

    # The device shadow exposes writable params ALSO as bare attribute keys (e.g. a
    # fridge reports `tempSelZ1` both bare and as `settings.tempSelZ1`). Those belong
    # to the command-param axis, not the attribute axis, so subtract every writable
    # settings-param name as well - otherwise controlled setpoints (number/climate/AC
    # switch) would be falsely listed as unmapped read-only attributes.
    published = {k for k in attributes if isinstance(k, str)}
    bare = {k for k in published if "." not in k}
    # Read-only telemetry is the attribute axis: writable param mirrors live on the
    # command-param axis, so exclude them from BOTH the unmapped list and the total
    # (otherwise `total` overstates this axis' denominator).
    # A mapped name is ABSENT only when no spelling its OWN read chain reaches is
    # published. A buffered program option is read bare and then under
    # `startProgram.<name>`, and the live TD publishes `tumblingStatus` only
    # there; a single-link chain (`sensor.delay_time` -> `delayTime`) keeps the
    # strict bare test, because that entity really cannot see a dotted spelling.
    reachable = set(bare)
    for _row in (sources_index or {}).values():
        _chain = (_row or {}).get("read") or ()
        if len(_chain) > 1 and any(link in published for link in _chain[1:]):
            reachable.add(_chain[0])
    read_only_bare = bare - settings_params
    unmapped = read_only_bare - mapped_attrs
    stats_keys = set(statistics) if isinstance(statistics, Mapping) else set()

    # Partition: statistics carve-out first (unchanged contract), then meta/noise
    # (value-type envelope OR scalar denylist OR program slot), then the fields this
    # integration derived itself, and the rest is signal.
    unmapped_statistics = sorted(k for k in unmapped if k in stats_keys)
    rest = unmapped - set(unmapped_statistics)
    unmapped_meta = sorted(
        k
        for k in rest
        if isinstance(attributes.get(k), (Mapping, list)) or _is_meta_attr(k)
    )
    rest -= set(unmapped_meta)
    unmapped_derived = sorted(k for k in rest if k in _ENGINE_DERIVED_ATTRS)
    unmapped_signal = sorted(rest - set(unmapped_derived))

    params_unmapped = settings_params - mapped_params
    params_meta = sorted(k for k in params_unmapped if k.lower() in _COVERAGE_META_PARAMS)
    params_signal = sorted(params_unmapped - set(params_meta))

    # `attributes_total` is the telemetry-axis denominator: mapped telemetry + signal.
    # Exclude statistics and meta (like writable mirrors already are) so that
    # `len(attributes_unmapped) / attributes_total` reads as a real coverage gap and
    # not a figure inflated by protocol/debug blobs.
    # ...and exclude the derived names too: they are not telemetry at all, so counting
    # them would put addhOn's own output in the denominator of the device's coverage.
    attributes_total = (
        len(read_only_bare)
        - len(unmapped_statistics)
        - len(unmapped_meta)
        - len(unmapped_derived)
    )

    return {
        "attributes_total": attributes_total,
        "attributes_unmapped": unmapped_signal,
        "attributes_unmapped_statistics": unmapped_statistics,
        "attributes_unmapped_meta": unmapped_meta,
        "attributes_unmapped_derived": unmapped_derived,
        # Symmetric with attributes_total: exclude the meta params (plumbing) so the
        # param-axis denominator is mapped controls + signal, not inflated by category/
        # endpoints/rule flags.
        "command_params_total": len(settings_params) - len(params_meta),
        "command_params_unmapped": params_signal,
        "command_params_unmapped_meta": params_meta,
        # The mirror of the two unmapped axes, and the only part of this block
        # that reads the tables against the device rather than the device
        # against the tables. Neither list needs a cap or a truncation flag:
        # both are differences taken FROM the static per-type tables, so their
        # length is bounded by what this repository ships and not by anything
        # the cloud sends. Measured worst case per type today: 37 short names
        # (WD and AC), 27 (REF), 5 (HO).
        #
        # The attribute side subtracts the BARE keys and nothing else, because
        # that is how the entity actually reads: `base_entity._get_attr` looks
        # its key up whole, then in statistics, and strips a `settings.` prefix
        # only when the key ITSELF carries one. So a mapped bare name that the
        # device publishes only as `startProgram.delayTime` genuinely is
        # unreadable and belongs in this list -- the live WM is exactly that
        # case, and treating the dotted spelling as coverage would hide it.
        #
        # The command side subtracts EVERY command's parameters, which is where
        # it stops mirroring `command_params_total` above. Restricted to the
        # settings params it reported `program` and `prCode` on every wash
        # appliance and `onOffStatus` on the air purifier -- all of them
        # present, under `startProgram`/`stopProgram`, in the same dump. An
        # axis sold as pure signal cannot open with three names the reader can
        # disprove by scrolling down. Say plainly what that costs, because the
        # two command axes are now measured against DIFFERENT universes:
        # `command_params_unmapped` asks whether the entity can WRITE the
        # parameter through the settings command, this asks only whether the
        # schema KNOWS the name at all, so a mapped param the entity writes
        # through settings while the device declares it only under
        # `startProgram` is reported by neither.
        "attributes_expected_absent": sorted(mapped_attrs - reachable),
        "command_params_expected_absent": sorted(mapped_params - command_params),
        # Emitted ONLY when a platform module would not import, and it NAMES
        # the ones that did not, because every number in this dict is then
        # measured against tables that are missing pieces: the unmapped lists
        # swell by whatever those modules mapped and both expected_absent lists
        # shrink by the same names. Naming them is what keeps the marker worth
        # reading now that each import degrades on its own -- `sensor` missing
        # invalidates `attributes_unmapped`, `air_purifier` missing invalidates
        # nothing at all on a fridge, and a bare `true` cannot tell the two
        # apart. `entities.sources` carries the matching per-entity nulls -- but
        # that section is absent entirely whenever there is no registry
        # inventory for this appliance (the device dump, or a registry that
        # could not be read), and a reader cannot tell that absence apart from
        # "there was nothing to decorate". A coverage section that under-reports
        # in silence is the one thing this dump may not do.
        **(
            {"registries_unavailable": list(registries_unavailable)}
            if registries_unavailable
            else {}
        ),
    }


def _handled_values(app_type) -> dict[str, frozenset[str]]:
    """Per-parameter raw values the integration HANDLES, for the types that say.

    Lazily imported like `_mapped_sets`, and empty for every type with no registry,
    so the future-capability section simply does not appear there instead of being
    filled with guesses.
    """
    if app_type != APPLIANCE_AP:
        return {}
    try:
        from .air_purifier import AP_HANDLED_VALUES
    except Exception:  # pragma: no cover - diagnostics must never crash
        _LOGGER.debug("Diagnostics debug: AP value registry unavailable", exc_info=True)
        return {}
    return dict(AP_HANDLED_VALUES)


def _scalar_text(value) -> str | None:
    """Canonical text of a shadow scalar, or None when it is not one.

    An integral float drops its decimal tail and a bool renders as the device's own
    1/0 spelling, so a value the client handed back as 3.0 or True still compares
    equal to the schema's "3" / "1". Containers return None: a delta only makes
    sense against a scalar.
    """
    plain = _jsonable(value)
    if plain is None or isinstance(plain, (Mapping, list)):
        return None
    if isinstance(plain, bool):
        return "1" if plain else "0"
    if isinstance(plain, float) and plain.is_integer():
        return str(int(plain))
    text = str(plain)
    return text or None


def _bounded_text(value, limit: int) -> str | None:
    """A cloud-controlled string, MASKED first and only then cut to `limit`.

    The order is the whole safety property, not a style preference. Both masks
    in this module run LATE: `_redact` walks the finished block at the end of
    `_appliance_block`, and the only VALUE-shaped mask anywhere in the dump is
    `_MAC_RE` inside `_jsonable`, which recognises a MAC address only when all
    six octet groups are present. A helper that slices its input to a cap
    BEFORE that mask therefore hands the mask a three-and-a-half octet fragment
    that no longer matches the pattern, and the remains of the address travel
    to a public GitHub issue in cleartext. Measured, with a MAC straddling the
    boundary of an 80 character cap:

        cut first, then mask  ->  'xxxxx3c:71:bf:b'   (3.5 octets, readable)
        mask first, then cut  ->  'xxxxx***'

    `_future_capabilities` already bounds its state values in this order
    (`_scalar_text`, which masks, and only then the slice at the call site).
    This helper exists so that the next section needing a bounded cloud string
    does not have to rediscover why, and so that reviewing the order is a
    matter of checking the call site uses it at all.

    Containers are refused OUTRIGHT, and the guard has to run TWICE - once on
    the object and once on what it wraps. `_scalar_text` cannot be relied upon
    for this: it tests the value AFTER `_jsonable` has run, and `_jsonable`
    turns a dict into its repr, so its isinstance guard never fires for a real
    Mapping. `_jsonable` also unwraps a `.value` one level, and the merged
    attributes mapping this module reads is made almost entirely of such
    wrappers (HonAttribute from the shadow, HonParameter from
    `appliance.settings`), so a wrapper holding a cloud dict is the ordinary
    shape here rather than an exotic one. Measured, with only the outer guard a
    wrapped lastConnEvent came through as "{'macAddress': '***', 'category':
    'CONNECTED', 'mobileId': 'm-123'}": the MAC masked, `mobileId` in
    cleartext, and the whole envelope collapsed into one string under whatever
    key the caller chose, which is exactly where key-based redaction can no
    longer reach it (see the `mobileId`/`transactionId` note on `_TO_REDACT`).
    A caller holding a cloud CONTAINER must still type-guard the field it wants
    before calling; this is the backstop for when it forgets, not a licence to
    pass the container in.

    What this helper does NOT do is make an arbitrary string safe. The only
    value-shaped mask it applies is `_MAC_RE`; an email, a serial, a nickname
    or a mobileId inside a cloud string passes through untouched, and `_redact`
    cannot help because it matches on KEY names. Bounding is a size property,
    not a privacy one. A caller is still responsible for the argument that its
    field is either a closed vocabulary or emitted under a name `_TO_REDACT`
    already knows.

    Returns None for anything `_scalar_text` refuses too: a character bound
    only means something for a scalar.
    """
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return None
    try:
        inner = value.value if hasattr(value, "value") else value
    except Exception:  # pragma: no cover - a raising wrapper must not kill it
        return None
    if isinstance(inner, (Mapping, list, tuple, set, frozenset)):
        return None
    text = _scalar_text(value)
    return None if text is None else text[:limit]


def _as_utc(value, assume_naive_utc: bool) -> datetime | None:
    """A datetime as an AWARE UTC datetime, or None when it cannot be one.

    Every age this dump prints is a subtraction, and Python raises TypeError
    when the two operands disagree about awareness. `_appliance_block` is
    called with no try/except around it from both entry points, so one naive
    datetime reaching one subtraction does not produce a wrong age, it produces
    no dump at all. This is the single place that settles awareness, so that no
    caller has to reason about it twice.

    What is guarded is `utcoffset()`, NOT `tzinfo`. A tzinfo object whose
    `utcoffset()` returns None does not raise: CPython falls back to the HOST's
    local zone, silently shifting the instant and then labelling it '+00:00' --
    a wrong time wearing an authoritative offset, which is worse than no time.
    Such a value is treated as naive here. A `utcoffset()` that raises (a
    foreign tzinfo, a broken subclass) yields None rather than propagating, and
    so does a subclass that raises from `astimezone` or `replace`: everything
    that touches the value at all is inside the one guard, because the callers
    downstream are fed cloud-supplied objects.

    `assume_naive_utc` deliberately has NO default. A naive instant is a guess
    whichever way it is read, and making every call site spell out its policy
    keeps that guess on the record instead of hiding it in this signature.
    """
    if not isinstance(value, datetime):
        return None
    try:
        if value.utcoffset() is not None:
            return value.astimezone(timezone.utc)
        if not assume_naive_utc:
            return None
        return value.replace(tzinfo=timezone.utc)
    except Exception:  # pragma: no cover - a foreign tzinfo must not kill it
        return None


# What an emitted instant must look like, and how long it may be. These sit
# here rather than in the bounds block near the top of the module because the
# rule in this file is that a constant lives immediately above its single
# consumer (see `_ZONE_SUFFIX_RE`): a reader meeting `_STAMP_MAX_CHARS` in
# `_stamp_text` should not have to scroll 300 lines to learn what it bounds.
# The separator is 'T' and nothing else: `_stamp_text` calls `isoformat()` with
# no `sep` argument, so a genuine datetime can never produce a space, and every
# shape this pattern accepts is a shape a datetime SUBCLASS could smuggle
# through the output validation below. The narrower it is, the less it admits.
_ISO_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:[+-][0-9]{2}:[0-9]{2})?"
)
_STAMP_MAX_CHARS = 40


def _stamp_text(value) -> str | None:
    """The one datetime -> ISO text conversion in the whole dump.

    An AWARE value is converted to UTC first, and that conversion is load
    bearing for CORRECTNESS, not merely for comparability: `_MAC_RE` runs over
    every string in the block and a sub-minute offset makes an ISO instant look
    exactly like a MAC address. Measured, '2026-04-09T12:34:56-05:30:15'
    survives `_jsonable` as '2026-04-09T***'. Normalising to UTC always yields
    '+00:00', which cannot match, which is the only reason this field reaches
    the reader intact. A future simplification that 'preserves the original
    offset' would silently mangle the section.

    A NAIVE value is emitted as it is, never shifted onto the host's zone: the
    cloud does hand back stamps with no offset, and a reader can reason about
    an instant with no zone, but not about one that has been quietly moved by
    however many hours the reporter's machine happens to be from UTC.

    Anything that is not a datetime yields None, on purpose and by contract. A
    cloud STRING instant such as `lastConnEvent.instantTime` must NOT be routed
    through here: this helper's promise is that its output was built by
    `datetime.isoformat`, and echoing a cloud-chosen string would break that
    promise while looking identical in the dump.

    Finally the OUTPUT is validated, not just the input type. `isinstance`
    admits SUBCLASSES, and a datetime subclass is free to override `isoformat`
    and return any string at all; without the length and shape check, an
    attacker-or-accident-supplied value would be copied verbatim into a
    document the user is about to attach to a public issue.
    """
    if not isinstance(value, datetime):
        return None
    try:
        if value.utcoffset() is not None:
            value = value.astimezone(timezone.utc)
        text = value.isoformat()
    except Exception:  # pragma: no cover - foreign subclass or tzinfo
        return None
    if not isinstance(text, str) or len(text) > _STAMP_MAX_CHARS:
        return None
    return text if _ISO_RE.fullmatch(text) else None


def _utcnow() -> datetime:
    """Now, as an aware UTC datetime.

    One line on purpose. `homeassistant.util.dt.now(tz)` is defined as
    `datetime.now(tz or DEFAULT_TIME_ZONE)`, so asking it for UTC makes the
    whole Home Assistant branch an identity function: a lazy import, a fallback
    path and a comment, all to produce what the line below produces.

    What this function is actually FOR is being a seam. Every test that asserts
    an age patches `diagnostics._utcnow`, and a dump that read the clock inline
    could only be tested against the wall clock, which is how age assertions
    turn into tests that fail once a year on a slow machine.
    """
    return datetime.now(timezone.utc)


def _enum_deltas(appliance, handled: Mapping[str, frozenset[str]]) -> tuple[dict, bool]:
    """Declared enum values the integration does not handle, per command param.

    Range parameters are skipped: a numeric range has no enumerable unhandled value,
    and `.values` on one would enumerate the whole grid (see `_param_schema`).
    """
    commands = getattr(appliance, "commands", None)
    if not isinstance(commands, Mapping):
        return {}, False
    deltas: dict[str, list[str]] = {}
    truncated = False
    for cmd_name in sorted(commands, key=str):
        params = getattr(commands[cmd_name], "parameters", None)
        if not isinstance(params, Mapping):
            continue
        for p_name in sorted(params, key=str):
            known = handled.get(str(p_name))
            param = params[p_name]
            if known is None or param_range(param) is not None:
                continue
            extra = sorted(set(param_values(param)) - known)
            if not extra:
                continue
            if len(deltas) >= _FUTURE_MAX_ENTRIES:
                return deltas, True
            if len(extra) > _FUTURE_MAX_VALUES:
                extra = extra[:_FUTURE_MAX_VALUES]
                truncated = True
            deltas[f"{cmd_name}.{p_name}"] = extra
    return deltas, truncated


def _future_capabilities(app_type, attributes: Mapping, appliance) -> dict:
    """Passive capture of what the device can do and this code cannot.

    Two signals the coverage block cannot express, because they are about
    parameters the integration DOES map:
      * `enum_deltas`            - values the schema declares and no entity handles;
      * `state_values_unhandled` - a value the device is reporting RIGHT NOW that no
                                   mapping covers (an active mode 3, say).
    Parameter names and raw non-identity values only. A writable parameter with no
    entity at all is already `coverage.command_params_unmapped`; it is not repeated
    here. Nothing in this section creates an entity or a control: it exists so a beta
    report shows the gap instead of the code guessing at it.
    """
    handled = _handled_values(app_type)
    if not handled:
        return {}
    deltas, truncated = _enum_deltas(appliance, handled)
    unhandled_state: dict[str, str] = {}
    for name in sorted(handled):
        text = _scalar_text(attributes.get(name))
        if text is not None and text not in handled[name]:
            unhandled_state[name] = text[:_FUTURE_MAX_VALUE_CHARS]
    section: dict = {
        "enum_deltas": deltas,
        "state_values_unhandled": unhandled_state,
    }
    if truncated:
        section["truncated"] = True
    return section


# Bound on the ROWS of the instant map below, one row per shadow parameter. The
# largest real device this repository holds a dump from is a washing machine
# with 112 of them (counted in diagnostics/live-2026-06-22/: REF 16, TD 41,
# AC 74, WM 112), so at 200 this is a runaway guard against a firmware that
# starts publishing thousands of parameters, not a filter -- exactly the framing
# _ENTITY_MAX_PER_DOMAIN carries above, and on every appliance seen so far it
# does not fire. It exists at all because `attributes` beside it is emitted
# UNCAPPED: if a shadow ever did explode, this map would be the second copy of
# the explosion in one document and the reader would pay for it twice.
_STAMP_MAX_ROWS = 200

# "This value exposes no `last_update` surface at all", which has to stay
# distinguishable from "it exposes one and the answer is None". The first is not
# a shadow parameter and gets NO row; the second is one and gets a `null` row,
# and those are two different findings. It is the same distinction
# `_registry_entries` draws between None and [], and it is why the plain
# `getattr(value, "last_update", None)` is not good enough here.
_NO_LAST_UPDATE = object()

# The cloud's "this parameter has never been stamped" sentinel. Every one of the
# 66 `lastUpdate` values in the only real REF capture this repository holds
# (tests/fixtures/ref_10136, apk/dump/ref_10136) is exactly this instant, while
# the AC capture beside it carries real 2023 dates. It is not an instant, and it
# must never become the newest one: printed as an age it reads as a 56-year-old
# shadow on a fridge that is connected and reporting.
_NEVER_STAMPED = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _attribute_timestamps(
    attributes: Mapping,
) -> tuple[dict[str, str | None], bool, datetime | None]:
    """The cloud instant behind each shadow value: the map, the cap flag, the newest.

    The device shadow arrives as `{parNewVal, lastUpdate}` per parameter and the
    engine keeps both on a `HonAttribute` (`client/engine/attributes.py`), but
    `_jsonable` unwraps only `.value`, so every instant the cloud sent has until
    now been discarded at the last step before the dump. The difference that
    makes is the difference between "the appliance reports tempZ3 = -43" and
    "the appliance last moved tempZ3 four days ago and has been repeating it
    since", and answering the second used to require a SECOND dump days later --
    which is precisely the round trip issue #75 paid for. The hOn app's own
    mocked shadow shows how much signal sits in the field: of its 74 parameters
    38 carry one identical instant from 2020 and the remainder spread over 21
    distinct ones (`apk/decomp.txt:1836416-1836566`).

    Three values are returned, deliberately out of ONE pass:

      [0] `{parameter name: ISO text or None}`, one row for each value that
          exposes a `last_update` surface, in `attributes`' own key order --
          the map this one is emitted beside and the only map it is ever read
          with; the return statement says why. That set is also the only honest
          answer to "which of these keys are shadow parameters", a bit the dump
          carries today only by duplicating the whole `attributes["parameters"]`
          sub-map.
      [1] True when the ROW CAP dropped rows, and for nothing else. In this
          module a flag named `truncated` means dropped ROWS -- see
          `_future_capabilities`, `_entity_inventory` and the platform summary,
          which all use it that way -- and a character cap would need a
          different name. There is no character cap here.
      [2] the newest instant, as an AWARE UTC datetime, computed over ALL
          parameters INCLUDING any the row cap dropped, and never emitted from
          here. It is handed back rather than left for the caller to recompute
          so that anything reporting freshness from it and the map above it are
          physically incapable of disagreeing: a second pass over an already
          truncated map would quietly report the newest SURVIVING instant as
          the newest instant, which is the worst kind of wrong -- plausible.

    `None` in [0] is OVERLOADED and a reader has to be told all three ways it is
    reached. It means "no parseable instant has ever arrived for this
    parameter"; it means "reading the instant raised"; and it means "a datetime
    did arrive and `_stamp_text` refused its output", which happens when a
    datetime SUBCLASS returns something from `isoformat` that is too long or is
    not an ISO instant. It does NOT mean "the cloud stopped stamping this
    parameter": `attributes.py:73` is a walrus guard (`if last_update :=
    data.get("lastUpdate"):`), so an update carrying no `lastUpdate` leaves the
    previous instant standing, and only a present-but-unparseable value resets
    it (`attributes.py:75-77`). The consequence is worth stating plainly,
    because it will otherwise be read as a bug: a stamp here can legitimately be
    OLDER than the value printed beside it in `attributes`, on the plain REST
    path, with nothing wrong anywhere.

    Formatting is delegated to `_stamp_text` and NOT reimplemented here. That
    helper owns the three properties this section would otherwise have to
    rediscover: an aware instant is normalised to UTC because `_MAC_RE` eats a
    sub-minute offset ('2026-04-09T12:34:56-05:30:15' survives `_jsonable` as
    '2026-04-09T***'); awareness is decided on `utcoffset()` and not on
    `tzinfo`, because a tzinfo whose `utcoffset()` returns None makes CPython
    silently re-read the instant in the HOST's zone and then label it '+00:00';
    and the OUTPUT is validated, because `isinstance(x, datetime)` admits
    subclasses and a subclass may return anything at all from `isoformat`.

    The `parameters` sub-map is walked as a SECOND source, and the honest
    statement about that walk is that it is INERT.
    `hon_client._get_attributes` merges `raw` -- which carries that sub-map
    under its own key -- at :187, and only then flattens the parameters over
    the top at :191, so on a healthy device the `HonAttribute` IS already the
    bare value and the nested copy is pure duplication (all four live dumps
    agree on every key, zero mismatches in 243 parameters). Measured on the
    real REF capture driven through the real engine, and on AC and WM rebuilt
    from it: the nested source contributes ZERO rows the flattened top level
    did not already carry.

    An earlier version of this paragraph named `hon_client.py:192-196` -- the
    `except` around `dict(params)` -- as the reachable failure this walk
    rescues. That was WRONG, and it is corrected here rather than quietly
    deleted, because the guards below read as evidence for it and the next
    reader would otherwise re-derive the same belief from them. The producer
    chain was traced end to end. Only three sites in this integration ever
    write a `parameters` key, all in `client/engine/appliance.py`: :277 and
    :385 mutate a VALUE inside the container, and :279 CREATES the container as
    a literal `{}`. The one wholesale replacement is `self._attributes |=
    attributes` at :280, whose right-hand side is the `/commands/v1/context`
    payload straight out of `response.json()` (`client/transport/api.py:170`),
    so it is a plain `dict`, a `list`, a `str`, a number, a bool or None --
    never a Mapping SUBCLASS. The per-type layer at :330 returns the object it
    was given and never touches the key, and the MQTT handler
    (`client/transport/mqtt.py:434`) only calls `.update()` on values already
    inside it. Both coordinator producers reach this module through the one
    `hon_client._build_appliance_entry` (:975) -- the REST poll at :1104 and
    `build_realtime_snapshot` at :1004 alike -- so the realtime push introduces
    no fourth shape. And `dict(x)` cannot raise on a plain `dict`, nor on a
    dict SUBCLASS: CPython takes the `PyDict_Merge` fast path and never calls
    an overridden `keys()`. So :192-196 can only fire for a value that is NOT a
    Mapping, and `test_diagnostics.py` pins that invariant on the real engine
    so this paragraph cannot go stale in silence.

    That is what ties the correction to the paragraph below rather than leaving
    it as trivia: the only shapes `dict(params)` can actually choke on are
    exactly the shapes this map refuses to read, so on every input a producer
    can deliver there is nothing for the fallback to fall back to. The walk is
    kept anyway -- it costs one `isinstance` and one `seen` lookup per
    parameter, and it is the only thing that would keep the instants in the
    document if the engine ever stopped building that container itself (a
    `MappingProxyType`, a lazy view, a cache object). Keep it as insurance; do
    not read it as a report that a half-broken Mapping arrives here today.

    Only a Mapping is followed there. A non-Mapping iterable under that key (a
    generator, a list) is deliberately NOT walked: this runs on the event loop,
    `len()` is the only bound available before iterating, and a section that
    would hang the loop to rescue a degraded corner is worse than the corner.

    Two shapes were weighed and rejected, on the record.

    Re-using `attributes["parameters"]` by replacing its values with the
    instants is measurably cheaper than a sibling map (one key, one nesting
    level, and the duplication is paid for already). It is refused because it
    silently redefines a key four live dumps already carry: a maintainer
    comparing a June dump against an August one would read instants where the
    older file has values, with nothing in either file to announce the change.
    It would also mean special-casing one key inside the `dict(attributes)`
    copy, and that copy's whole contract is to be a faithful echo of what the
    device said. The honest treatment of that duplication is to DELETE the
    nested map, which is a separate decision from this one and is taken (or
    not) at the point where `attributes` is normalised.

    Emitting relative AGES (`{"tempZ3": 345}`) instead of absolute instants is
    the privacy-cheaper shape: an age carries no wall clock and no zone. It is
    refused because an age is only meaningful against the moment the dump was
    taken, which `generated_at` now supplies at the top of the document, so the
    absolute form gives that up for nothing; because two dumps taken days apart
    can be aligned on absolute instants and cannot be aligned on ages; and
    because an age is computed from the REPORTER's clock, which adds a
    dependency on that clock being right, where the instants are cloud-authored
    and arrive already agreed.
    """
    rows: dict[str, str | None] = {}
    newest: datetime | None = None
    truncated = False
    seen: set[str] = set()

    sources: list[Mapping] = [attributes]
    try:
        nested = attributes.get("parameters")
    except Exception:  # pragma: no cover - a Mapping that refuses one key
        # Guarded even though `.get` looks total: `Mapping.get` only swallows
        # KeyError, and this is the FIRST read of `attributes` in the block, so
        # an unguarded call here would take the dump down before `_coverage`
        # ever gets its own chance to fail on the same object.
        nested = None
    if isinstance(nested, Mapping):
        sources.append(nested)

    for source in sources:
        try:
            names = sorted(source, key=str)
        except Exception:  # pragma: no cover - a Mapping that cannot list keys
            # Honest scope, corrected: NEITHER source can reach this today.
            # For `attributes` it was always a formality -- `_coverage`
            # iterates the same object a few lines below with no guard at all,
            # so a mapping that cannot list its keys ends the dump regardless.
            # For the NESTED map this was called the reachable guard, and the
            # producer trace in the docstring above shows it is not: that key
            # holds a plain `dict`, which lists its keys, or a non-Mapping,
            # which is never appended to `sources`. Kept as the cheap half of
            # "degrade, never raise", not as evidence of a live failure.
            _LOGGER.debug(
                "Diagnostics debug: attribute names unreadable", exc_info=True
            )
            continue
        for name in names:
            key = str(name)
            if key in seen:
                continue
            try:
                value = source[name]
            except Exception:
                # A key that enumerates and then refuses to resolve is the
                # object that WOULD have made `dict(params)` raise upstream; no
                # producer builds one today (traced in the docstring). Skip the
                # one key rather than the whole source, so a single bad entry
                # costs one row and not the entire section.
                continue
            try:
                raw = getattr(value, "last_update", _NO_LAST_UPDATE)
            except Exception:
                # A `last_update` PROPERTY that raises. This must land as a
                # `null` row, never as a missing one: dropping it here would
                # merge "this parameter has never carried a parseable instant"
                # into "this key is not a shadow parameter at all", and telling
                # those two apart is most of what the section is for.
                raw = None
            if raw is _NO_LAST_UPDATE:
                continue
            seen.add(key)
            # Two conversions of one value, on purpose, because the two answers
            # follow different policies. `_stamp_text` keeps a naive instant
            # naive, so the dump never claims a zone the cloud did not send;
            # `_as_utc(..., True)` reads that same naive instant AS UTC, because
            # an age has to be computed under some assumption and stating it is
            # better than dropping the row. The newest is tracked for EVERY
            # parameter, including the ones the cap below refuses to print.
            try:
                instant = _as_utc(raw, True)
                if instant is not None:
                    # Rebuilt from the integer fields, INSIDE the guard, and
                    # both halves of that matter. `isinstance(x, datetime)`
                    # admits SUBCLASSES: the door `_stamp_text` closes on
                    # `isoformat` is wide open on the `__gt__` below and on the
                    # `__sub__` that whoever consumes this value will perform,
                    # and a subclass raising in either does not produce a wrong
                    # age, it produces NO DUMP AT ALL. Rebuilding also means the
                    # instant handed on is a plain `datetime`, so no later
                    # consumer inherits the risk.
                    instant = datetime(
                        instant.year,
                        instant.month,
                        instant.day,
                        instant.hour,
                        instant.minute,
                        instant.second,
                        instant.microsecond,
                        tzinfo=timezone.utc,
                    )
                    if instant > _NEVER_STAMPED and (
                        newest is None or instant > newest
                    ):
                        newest = instant
            except Exception:  # pragma: no cover - a hostile datetime subclass
                _LOGGER.debug(
                    "Diagnostics debug: instant not comparable", exc_info=True
                )
            if len(rows) >= _STAMP_MAX_ROWS:
                truncated = True
                continue
            rows[key] = _stamp_text(raw)
    # Ordered ONCE, at the end, and not per source: the two sources are each
    # walked in sorted order but a bare key and a nested one can both be
    # productive, and a map whose second half restarts the alphabet reads as a
    # rendering bug.
    #
    # The order taken is `attributes`' OWN enumeration and not the alphabet,
    # because this map is never read alone. The one question it exists to answer
    # -- "when did tempZ3 last move" -- is asked with `attributes` already open
    # at tempZ3, and the sibling keeps the cloud's merge order, so an
    # alphabetical map made the reader find one name at two unrelated ranks in
    # two adjacent sections. Measured on the four archived live dumps
    # (diagnostics/live-2026-06-22), same name, both maps: mean displacement 4.5
    # rows on the REF, 15.7 on the TD, 24.6 on the AC, 42.4 on the WM, worst
    # case 100, and 66 places where reading the rows top to bottom sent the eye
    # BACKWARDS up the sibling. Following the sibling makes every one of those
    # zero, and it is free here: the shadow parameters are a CONTIGUOUS run
    # inside `attributes` on all four appliances (`hon_client._get_attributes`
    # flattens them in one update), so the map is now that same run, reprinted
    # with the instants.
    #
    # `sorted` survives as the FALLBACK, for rows `attributes` does not
    # enumerate at all: the nested-only keys of the degraded merge path, where
    # `dict(params)` raised and nothing was flattened. There are none on any
    # archived appliance -- zero across all four dumps and entry.json -- so this
    # is the corner and not the case. They keep the order they have today and
    # land together after everything ranked, rather than being interleaved at
    # rank zero among names they have no relationship to.
    #
    # WHICH rows survive does not move: the cap is applied during the walk, and
    # the walk is untouched. Only the printing order changes, and it changes for
    # the better under the cap too -- `_appliance_block` keeps the
    # `attributes["parameters"]` sub-map whenever this map is truncated, and the
    # surviving rows are now a SUBSEQUENCE of it, so the dropped names show up
    # as holes in an aligned scan instead of having to be diffed across two
    # different orders.
    try:
        order = {str(name): position for position, name in enumerate(attributes)}
    except Exception:
        # Not the same formality as the guard on the walk above. That one had
        # already decided this mapping could not be listed and CONTINUED, so the
        # rows in hand may have come entirely from the nested sub-map and be
        # perfectly good; an unguarded second enumeration here would throw them
        # away along with the whole dump, from the helper that makes the FIRST
        # read of `attributes` in the block. Unranked, every row falls into one
        # bucket and the tiebreaker below emits exactly the alphabetical map
        # this line returned before.
        _LOGGER.debug(
            "Diagnostics debug: attribute order unreadable", exc_info=True
        )
        order = {}
    return (
        dict(
            sorted(
                rows.items(),
                key=lambda row: (order.get(row[0], len(order)), row[0]),
            )
        ),
        truncated,
        newest,
    )


def _attribute_values(attributes: Mapping) -> Mapping:
    """The attribute mapping the block uses, minus the sub-map that repeats it.

    `attributes["parameters"]` is the device shadow's own container, and
    `hon_client._get_attributes` flattens it over the top level one line after
    merging it (`hon_client.py:187-191`), so every one of its keys is normally
    ALSO a bare key pointing at the very same object. Measured on
    diagnostics/live-2026-06-22/, across all four appliances and all 243
    parameters (REF 16, AC 74, TD 41, WM 112): zero mismatches. The nested map
    is a verbatim second copy, and it costs 7624 bytes and 251 lines of the
    entry dump at the indent=2 Home Assistant serves.

    The one thing it carried that the bare keys did not is WHICH keys are shadow
    parameters -- and `attributes_last_update` beside it now carries exactly
    that set, with the instant as well. Dropping it is therefore not a loss of
    information, which is the only reason it is dropped at all.

    The result replaces `attributes` for the WHOLE block, not just for the
    emitted echo, and that is not a convenience. `_coverage` classifies
    `parameters` as an envelope blob and lists it in
    ``attributes_unmapped_meta`` -- it does so on all four live appliances --
    so deduplicating only the copy the reader sees would ship a document whose
    coverage section names a key the section beside it no longer prints. Two
    parts of one dump contradicting each other is the failure this dump exists
    to expose, not to commit.

    It is dropped ONLY when it is provably redundant, and never merely because
    it is present. The check is identity (`is`), not equality, for two reasons:
    equality on a cloud object calls an `__eq__` this module does not control
    and cannot afford to have raise, and identity is what the merge above
    actually establishes. The lookup uses the module sentinel rather than
    `.get(name)`, because `.get` answers None both for "absent" and for
    "present and None", and a nested value of None would otherwise read as
    redundant against a bare key that is not there at all. An EMPTY sub-map is
    kept for the same conservatism: `all([])` is True, and "the shadow
    container was empty" is a different statement from "there is no shadow
    container". When even one nested key is missing from the top level the whole
    sub-map is kept, on the plain principle that a map which cannot be SHOWN to
    be a copy is not deleted: deleting it would delete data rather than
    duplication.

    The reason this branch used to give for itself is retracted. It cited "the
    signature of the degraded path at `hon_client.py:192-196` -- a `parameters`
    Mapping whose `dict()` coercion raised", and `_attribute_timestamps`'
    docstring above now traces every producer of this key and shows it holds
    either a plain `dict`, which `dict()` cannot refuse, or a value that is not
    a Mapping at all, which the `isinstance` below returns on before this check
    is reached. Measured over the real REF capture through the real engine,
    over AC and WM rebuilt from it, and over every JSON shape a cloud payload
    can put under the key: `redundant` is True for every NON-EMPTY sub-map that
    gets this far, so that half of the branch has no reachable input. The EMPTY
    half does have one -- a payload carrying its own top-level `parameters`
    replaces the shadow container, and `{}` then fails `bool(nested)` -- and
    the principle above is reason enough for both halves to stay.
    """
    try:
        nested = attributes.get("parameters")
    except Exception:  # pragma: no cover - a Mapping that refuses one key
        return attributes
    if not isinstance(nested, Mapping):
        return attributes
    try:
        redundant = bool(nested) and all(
            attributes.get(name, _NO_LAST_UPDATE) is value
            for name, value in nested.items()
        )
    except Exception:  # pragma: no cover - a Mapping that cannot be walked
        # The same object that made `dict(params)` raise upstream. If it cannot
        # be compared it cannot be shown to be redundant, so it stays.
        _LOGGER.debug(
            "Diagnostics debug: nested parameters not comparable", exc_info=True
        )
        return attributes
    if not redundant:
        return attributes
    try:
        return {
            name: value for name, value in attributes.items() if name != "parameters"
        }
    except Exception:  # pragma: no cover - a Mapping that cannot be copied
        # The last unguarded read of `attributes` in this helper. Losing the
        # dedupe costs bytes; raising here costs the reporter the whole file.
        _LOGGER.debug(
            "Diagnostics debug: attributes not copyable", exc_info=True
        )
        return attributes


def _appliance_block(
    appliance_id: str,
    data: Mapping,
    entities: Mapping | None = None,
    now: datetime | None = None,
) -> dict:
    """Build the (redacted) diagnostics block for a single appliance.

    `entities` is the registry-derived inventory for THIS appliance, already
    computed once for the whole dump. It defaults to None so the helper stays
    hass-less and pure, and so a caller with no registry still produces a block.

    `now` is the instant the WHOLE dump was taken, threaded in from the entry
    point instead of read here, so that the `generated_at` at the top of the
    document and every age inside every appliance block describe one moment.
    Reading the clock per block would let two ages in one document disagree by
    however long the dump took to build -- a small inconsistency, and exactly
    the kind that costs a maintainer an hour before it is recognised as noise.
    It is TRAILING and defaults to None so that the existing callers passing
    only the first arguments keep working and still get a dated block, and it
    is normalised on the first line of the body: see the comment there for why
    nothing downstream may be handed a naive datetime.
    """
    appliance = data.get("appliance")
    app_type = data.get("type")
    attributes = data.get("attributes")
    attributes = attributes if isinstance(attributes, Mapping) else {}
    # Read the instants HERE, off the same mapping the block echoes below, and
    # once. The third value is the newest of them: it is computed in the same
    # pass so that anything reporting freshness from it can never disagree with
    # the map the reader is looking at, even after the row cap has fired.
    stamps, stamps_truncated, newest_stamp = _attribute_timestamps(attributes)
    # De-duplicated ONCE, above every consumer, and only after the instants
    # have been read. The ordering is kept for the reason it was written -- the
    # sub-map is the instants' fallback source -- even though that fallback is
    # inert on every input a producer can deliver, which
    # `_attribute_timestamps`' docstring traces in full. Two helpers whose
    # combined result depends on call order is a trap either way, and one that
    # holds only while the fallback is dead is the worse of the two.
    # Skipped entirely when the row cap fired: the justification for
    # deleting the sub-map is that `attributes_last_update` now carries the one
    # bit it held (which keys are shadow parameters), and a truncated map does
    # not carry it for the rows it dropped.
    if not stamps_truncated:
        attributes = _attribute_values(attributes)
    statistics = data.get("statistics")
    statistics = statistics if isinstance(statistics, Mapping) else {}
    # Normalised HERE and nowhere else. A caller may pass nothing, a naive
    # datetime, or something that is not a datetime at all; below this line
    # `now` is always an aware UTC instant, so every age is a subtraction that
    # cannot raise TypeError and take the entire dump down with it -- this
    # function is called with no try/except around it from both entry points.
    # `assume_naive_utc=True` is the honest reading AT THIS BOUNDARY: the only
    # naive value that can arrive here comes from a caller that meant 'now', so
    # trusting it is wrong by the host's offset at worst, where refusing it
    # would be wrong by the whole age.
    now = _as_utc(now, True) or _utcnow()

    commands = _command_schema(appliance)
    model_attributes = _model_attributes(appliance)
    # ONE walk of the per-type tables, ONE lazy-import decision, shared by both
    # consumers -- which is what the `_mapped_sets` docstring claims and what
    # makes `coverage.registries_unavailable` and `entities.sources` structurally
    # incapable of disagreeing.
    mapped = _mapped_sets(app_type)
    coverage = _coverage(app_type, attributes, statistics, appliance, mapped)
    # Built from `newest_stamp`, the third value of the SAME `_attribute_timestamps`
    # pass that produced the printed `attributes_last_update` map below, never from a
    # second walk of `attributes`. Two passes over one mapping is two chances to
    # disagree, and they WOULD disagree on the dump where it matters most: once the
    # row cap has dropped the newest parameter from the printed map, a second pass
    # over the printed rows would report a shadow that is older than the appliance's
    # real newest reading, in the very document a reader opened to find out how stale
    # the readings are.
    freshness = _freshness(appliance, attributes, newest_stamp, now)
    future = _future_capabilities(app_type, attributes, appliance)
    entity_section = _entity_section(
        entities, app_type, mapped, _declared_command_params(appliance)
    )

    _LOGGER.debug(
        "Diagnostics debug: appliance id=%s name=%s type=%s attrs=%d model_attrs=%d "
        "commands=%d unmapped_attrs=%d unmapped_params=%d",
        redact_id(appliance_id),
        data.get("name"),
        app_type,
        len(attributes),
        len(model_attributes),
        len(commands),
        len(coverage["attributes_unmapped"]),
        len(coverage["command_params_unmapped"]),
    )

    block = {
        "id": _REDACTED,
        "name": data.get("name"),  # kept readable on purpose (correlation aid)
        "type": app_type,
        "model": data.get("model"),
        "serial": _REDACTED,
        "mac": _REDACTED,
        # Directly under the identity keys, and above everything it qualifies: a
        # reader who does not know whether the appliance was online cannot safely
        # interpret a single value below this line, so the answer must arrive before
        # the values do rather than being hunted for among sixty attribute keys.
        "freshness": freshness,
        # The pinned order from here on, which sections are APPENDED into rather than
        # sorted: what the model IS, then a reserved slot for the per-zone map, then
        # what it is DOING and when the cloud last moved it, then what can be
        # COMMANDED, then the analysis sections -- `coverage`, `entities`,
        # `future_capabilities` -- which are read together and are the reason the
        # dump exists at all. The reserved slot is real and load bearing: `zone_map`
        # goes BETWEEN `model_attributes` and `attributes`, and naming it here is
        # what stops the next section from being dropped wherever its patch happened
        # to apply cleanly.
        "model_attributes": model_attributes,
        "attributes": dict(attributes),
        # BESIDE the values, never inside them. `attributes` above has one
        # contract -- a flat name -> value echo of what the device said -- and a
        # section that turned its leaves into {value, last_update} objects would
        # break every reader, every jq filter and the value-type partition
        # `_coverage` runs over exactly those leaves.
        #
        # The truncation flag is a SIBLING key rather than a `truncated` entry
        # inside the map, and that is a deliberate deviation from the three
        # precedents in this module (`_future_capabilities`, `_entity_inventory`
        # and the platform summary all put it inside). The reason is specific to
        # this map and does not generalise: every key in it is a cloud-CHOSEN
        # parameter name, so a reserved key named `truncated` would be
        # indistinguishable from a device that publishes a parameter called
        # `truncated` -- the section would be unable to say whether it had
        # dropped rows or merely found one oddly named. It is emitted only when
        # true and immediately after the map, so it still reads as part of it.
        "attributes_last_update": stamps,
        **({"attributes_last_update_truncated": True} if stamps_truncated else {}),
        "commands": commands,
        "coverage": coverage,
        # Next to coverage on purpose: one says what the code could map for this
        # type, the other what Home Assistant actually holds. Reading them together
        # is the whole point, and a disagreement between them IS the finding.
        "entities": entity_section,
        "future_capabilities": future,
    }
    return _redact(block)


# The bound on the ONE cloud-controlled string this section prints. The vocabulary
# behind it is closed: the engine decides connectivity by comparing `category`
# against the single literal "DISCONNECTED" (client/engine/appliance.py,
# load_attributes), and every live dump captured so far carries either that word or
# "CONNECTED". Forty characters is therefore already several times more than a real
# value needs, and no truncation flag is warranted here -- a `category` long enough
# to be cut is not a category, it is a payload, and the cap exists to stop it from
# becoming one rather than to summarise it. Placed immediately above its single
# consumer for the same reason `_STAMP_MAX_CHARS` is, and not in the bounds block
# near the top of the module.
_CONN_CATEGORY_MAX_CHARS = 40


def _freshness(
    appliance,
    attributes: Mapping,
    newest_stamp: datetime | None,
    now: datetime,
) -> dict:
    """How OLD everything below this key is, stated before the values arrive.

    The misreading this section exists to prevent is the expensive one: a reporter
    downloads a dump, the maintainer reads `machMode`, `tempZ3` and a wash program
    out of it as the appliance's CURRENT state, and neither of them notices for two
    round trips that the cloud stopped updating that shadow days ago and every value
    in the file is a fossil. `generated_at` at the top of the document says when the
    dump was taken; this says how far behind the dump the DEVICE was.

    Be exact about what it is not. `available` is ALREADY a top-level scalar under
    `attributes` today -- the live 2026-06-22 capture carries it as `false` on the
    fridge, the tumble dryer and the washer, next to a `lastConnEvent.category` of
    "DISCONNECTED" -- so nothing here is a new FACT about the appliance. What the
    section buys is PROMINENCE (three lines above the values instead of two keys
    lost among sixty in cloud-chosen order) and an AGE, which is the part no
    existing key carries: `lastConnEvent` prints an instant, and turning an instant
    into "six minutes" or "eleven days" needs a second instant that this document
    did not contain at all until `generated_at` landed.

    THREE FIELDS WERE DESIGNED FOR THIS BLOCK AND ARE DELIBERATELY ABSENT.
    `decided_by`, `realtime` and `realtime_ttl_s` must not come back without an
    engine change, for three independent reasons, each of which makes them print a
    claim this same dump disproves a line later:
      * the engine keys connectivity on `"category" in lce` and then on
        `lce["category"] != "DISCONNECTED"`, so a category that is PRESENT and null
        makes it declare the appliance CONNECTED, while any mirror written from the
        same values reads a null as "unknown". The block would contradict the
        connectivity it printed one line above, on precisely the malformed payload
        it was added to explain.
      * `mark_realtime_disconnected` sets connection False and CLEARS both realtime
        marks, while leaving the cached `lastConnEvent` at its last REST value. The
        block would then print `connected: false` beside a "CONNECTED" category and
        two empty realtime marks: every printed input saying online, the engine
        saying offline, and the evidence that reconciles them already erased from
        the object. "Derived only from the values printed above it, so a reader can
        check it by hand" is false exactly there, and no amount of patching makes it
        true from inside this module.
      * whether an MQTT delta advances a parameter's `lastUpdate` is genuinely
        unresolved, so "the shadow has not moved, therefore nothing under
        `attributes` is live" is an inference this code is not entitled to draw.
    A field that is right most of the time and confidently wrong on the one failure
    it was built for is worse than no field at all.

    `poll` is absent for a plainer reason: there is no honest source for it here.
    Every input reachable from this function was checked. The coordinator entry
    (`hon_client._build_appliance_entry`) carries appliance, type, name, model,
    serial, mac, attributes, statistics and settings, and no fetch instant. The
    coordinator itself is not passed to `_appliance_block` and must not be, since
    nothing here may grow a fifth parameter. And the only poll-ish datetime the
    engine holds, `HonAppliance._last_update`, is disqualified three times over: it
    is private; it is `datetime.now()`, hence NAIVE and in the HOST's zone, so read
    as UTC it prints a NEGATIVE age for every reporter east of Greenwich and read as
    local it needs a third naive policy that `_as_utc` deliberately refuses to have;
    and it only advances on the HTTP path, so on a realtime MQTT snapshot it would
    report the freshest data in the whole dump as the stalest. An `age_s` nobody can
    trust is the mistake above in a smaller font. If a fetch instant is wanted here,
    the honest fix is for the coordinator to record one and for
    `_build_appliance_entry` to carry it; until then the key stays out rather than
    being sourced from the nearest datetime that happens to be in scope.

    `newest_stamp` is handed in, not recomputed: see the comment at the call site.
    `now` arrives already normalised to an aware UTC instant by `_appliance_block`,
    so every subtraction below is aware-against-aware and cannot raise the
    `TypeError` that would take the entire dump down -- this function is reached
    with no try/except around it from either entry point.

    A section that could not be established is ABSENT, never present-and-null.
    `last_conn_event` missing means the appliance carries no such envelope at all;
    `last_conn_event` present with a null `category` means it carries one and the
    field inside it was not USABLE text -- not a string at all, or the empty
    string, which `_bounded_text` refuses along with every other empty scalar. Note
    what that second case costs, because it is the same ambiguity that sank
    `decided_by`: the engine compares `lce.get("category", "") != "DISCONNECTED"`
    and therefore reads an EMPTY category as CONNECTED, so on that one payload this
    row prints a null where the engine saw a decision. It is reported as null anyway
    rather than as "", because a bare empty string in a dump reads as a rendering
    bug and the finding a reader needs is already carried by `connected` on the line
    above. Folding the two shapes together altogether would report "this device has
    never reported a connection event" and "this payload is malformed" as the same
    finding, which is the None-versus-[] collapse `_registry_entries` documents a
    few functions below.
    """
    # A raising property is not hypothetical here: `connection` IS a property on
    # HonAppliance, and a foreign or half-constructed appliance object is exactly
    # what this dump is most often built from when something has gone wrong.
    try:
        connected = getattr(appliance, "connection", None)
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        _LOGGER.debug(
            "Diagnostics debug: appliance connection unreadable", exc_info=True
        )
        connected = None
    # One try around BOTH attribute reads, for the same reason `_conn_event_row`
    # guards its own: `attributes` arrives as whatever the coordinator entry
    # carried, and a Mapping whose `.get` raises would abort the ENTIRE dump from
    # inside a section that is only ever a convenience. There is nothing upstream
    # to have failed first on every appliance: `_coverage` reaches `.get` only
    # while it still has unmapped bare keys left to classify, and
    # `_future_capabilities` only for the air purifier.
    try:
        available = attributes.get("available")
        event = attributes.get("lastConnEvent")
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        _LOGGER.debug("Diagnostics debug: attributes unreadable", exc_info=True)
        available = event = None
    # Both are booleans or they are nothing. The engine writes real `bool`s into
    # both surfaces, so a string "true" or a 1 arriving here means something
    # upstream is no longer what this section thinks it is, and printing it anyway
    # would launder that into a fact the reader cannot question.
    section: dict = {
        "connected": connected if isinstance(connected, bool) else None,
        "available": available if isinstance(available, bool) else None,
    }

    if isinstance(event, Mapping):
        section["last_conn_event"] = _conn_event_row(event, now)

    # Normalised here even though the only supported producer of `newest_stamp`
    # already returns an aware UTC datetime. `_conn_event_row` decides awareness
    # for its own instant rather than trusting another module to keep a promise it
    # could quietly stop keeping, and this half must do the same: a naive value
    # reaching `_stamp_text` alone would print an `at` with no offset next to a
    # null `age_s`, which is exactly the "an age with no `at`, an `at` with no age"
    # shape this section refuses everywhere else. `_stamp_text` still refuses
    # anything that is not a datetime, so junk simply produces no shadow.
    moment = _as_utc(newest_stamp, True)
    text = _stamp_text(moment)
    if text is not None:
        section["shadow"] = {"at": text, "age_s": _age_seconds(moment, now)}
    return section


def _conn_event_row(event: Mapping, now: datetime) -> dict:
    """What the CLOUD last said about this appliance's link, and how long ago.

    `category` is type-guarded as `str` before anything else touches it, and that
    guard carries the whole privacy weight of this section. The live capture shows
    the envelope as `{macAddress, category, instantTime, timestampEvent}`: applying
    `str(...)` or `_scalar_text(...)` to the ENVELOPE instead of to the field
    flattens the entire dict into one string filed under the key `category`, and
    `_redact` matches by exact key NAME, so the `macAddress` sitting inside that
    string is permanently out of its reach. The value that survives the guard then
    goes through `_bounded_text`, which masks before it cuts, so an address
    straddling the cap cannot leave a readable fragment behind either. Be honest
    about what the cap is and is not: it is a SIZE bound. `_MAC_RE` is the only
    value-shaped mask in the dump, so up to forty characters of arbitrary cloud
    text would still reach the reader here; what makes the field safe is the closed
    CONNECTED/DISCONNECTED vocabulary behind it, and a firmware that started
    putting free text in this slot would need this decision revisited, not just
    this number.

    The instant is read from `timestampEvent` FIRST and only then from
    `instantTime`, which is not a coin toss: it is the same order, on the same two
    keys, that `HonAppliance.load_attributes` uses when it decides whether a REST
    disconnect is newer than the last realtime traffic. Printing the instant the
    ENGINE ordered on is the point -- a dump that quietly preferred the other key
    would, on a payload where the two disagree, explain a decision the engine did
    not make. `timestampEvent` is epoch milliseconds, so its rendering can carry a
    sub-second tail; that is the real precision of the field and it is not rounded
    away.

    Neither raw value is ever echoed. `instantTime` is a cloud-chosen STRING, and
    the only way it reaches the dump is by being parsed into a datetime and then
    re-rendered by `_stamp_text` from that datetime's own fields, so a `category`-
    shaped payload smuggled into an instant slot yields no `at` key at all rather
    than a verbatim copy.

    `at` and `age_s` appear together or not at all. An `at` with no age would make
    the reader do the arithmetic this section exists to have already done, and an
    age with no `at` would be a number with nothing to check it against.
    """
    # Read in TWO guards rather than one, so that a `.get` which raises on the
    # instant keys does not throw away a category this function has already read.
    # `event` is a cloud-supplied Mapping and a foreign implementation of `.get`
    # is free to raise, which from here would abort the entire dump.
    try:
        category = event.get("category")
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        _LOGGER.debug("Diagnostics debug: lastConnEvent unreadable", exc_info=True)
        category = None
    row: dict = {
        "category": (
            _bounded_text(category, _CONN_CATEGORY_MAX_CHARS)
            if isinstance(category, str)
            else None
        )
    }
    try:
        candidates = (event.get("timestampEvent"), event.get("instantTime"))
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        _LOGGER.debug(
            "Diagnostics debug: lastConnEvent instant unreadable", exc_info=True
        )
        return row
    for raw in candidates:
        # `assume_naive_utc=True` mirrors the parser's own documented assumption
        # ("the cloud stamps UTC, hence the trailing Z"). It is belt and braces
        # today, since `parse_cloud_timestamp` already returns aware UTC; it is here
        # so this section keeps deciding awareness for itself instead of trusting
        # another module to keep a promise it could quietly stop keeping.
        moment = _as_utc(_parse_cloud_moment(raw), True)
        text = _stamp_text(moment)
        if text is not None:
            row["at"] = text
            row["age_s"] = _age_seconds(moment, now)
            break
    return row


def _parse_cloud_moment(value) -> datetime | None:
    """A cloud-stamped instant (epoch milliseconds OR ISO text) as a datetime.

    Delegates to the client's `parse_cloud_timestamp` rather than reimplementing
    it, and the reason is correctness rather than economy: that function is what
    the engine itself orders `lastConnEvent` with, it is already total (it returns
    None for None, for a bool, for garbage, for an arbitrary-precision int that
    overflows `float()`, and for an out-of-range epoch), and it handles the two
    cloud spellings this dump meets -- a trailing "Z" that Python 3.10's
    `fromisoformat` rejects, and a VARIABLE number of fractional digits. A local
    reimplementation would have to rediscover all of that, and would then be free
    to drift away from the engine it is supposed to be reporting on.

    The import is function-local, mirroring `_mapped_sets` and `_registry_entries`:
    diagnostics.py keeps a tiny top-level import surface so it cannot be caught in
    an import cycle, and this parser is only ever needed while a dump is being
    built. It costs one `sys.modules` lookup per call after the first, and it is
    called at most twice per appliance.
    """
    try:
        from .client.helpers import parse_cloud_timestamp

        return parse_cloud_timestamp(value)
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        _LOGGER.debug("Diagnostics debug: cloud timestamp unreadable", exc_info=True)
        return None


def _age_seconds(moment, now: datetime) -> int | None:
    """Whole seconds from `moment` to `now`. NEGATIVE when `moment` is ahead.

    The negative is deliberate and must not be clamped to zero. Both instants a
    reader sees here are stamped by the CLOUD while `now` is stamped by the
    reporter's host, so a negative age is not nonsense: it is the dump saying the
    two clocks disagree, which is itself a finding, and one that explains
    connectivity decisions the engine makes by ordering those very timestamps
    against each other. Clamping would erase the evidence and leave behind a
    plausible-looking `0` that reads as "just now".

    Truncated toward zero rather than rounded, so the number never claims a second
    that has not fully elapsed. `moment` is deliberately UNANNOTATED: both callers
    hand it an aware UTC datetime, but the try/except exists precisely for the
    thing that is not one -- a foreign datetime subclass with an opinion about
    subtraction, which would otherwise take down the whole dump from inside a
    field that is only ever a convenience.
    """
    try:
        return int((now - moment).total_seconds())
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        _LOGGER.debug("Diagnostics debug: age not computable", exc_info=True)
        return None


def _declared_write_only(row, tag: str, declared) -> dict | None:
    """Drop fixed writes the DEVICE does not declare, from the rows that have them.

    Only `_FIXED_WRITE_COMMANDS` rows are touched, and only when the caller could
    read the appliance's command schema -- `declared=None` means "not looked", and
    a row is never narrowed on the strength of a lookup that did not happen.

    A row left with no half at all becomes an explicit `null`, which is the same
    statement `select.program` already makes: the entity exists and this dump
    cannot name a parameter for it. That is the honest reading for a dryer, whose
    stop button really does apply nothing -- and it is more useful than the
    alternative of printing a write the device cannot perform, because a reader
    chasing `onOffStatus` there finds it published as a bare attribute and
    concludes the button works.
    """
    if not isinstance(row, Mapping) or not isinstance(declared, Mapping):
        return row
    command = _FIXED_WRITE_COMMANDS.get(tag)
    if command is None:
        return row
    keep = [
        name for name in (row.get("write") or ()) if name in declared.get(command, ())
    ]
    narrowed = {half: value for half, value in row.items() if half != "write"}
    if keep:
        narrowed["write"] = keep
    return narrowed or None


def _entity_section(
    entities: Mapping | None, app_type, mapped=None, declared=None
) -> dict | None:
    """The registry inventory for one appliance, plus what each entity speaks to.

    `by_domain` and its siblings answer WHICH entities Home Assistant holds.
    `sources` answers the question that used to need the source tree: which raw
    parameter each of them reads, and which one it writes. On issue #75 those are
    two adjacent rows -- `sensor.temp_zone3` reads `tempZ3` while
    `number.target_temp_zone4` writes `tempSelZ4` -- and reading them together is
    what removes the round trip that otherwise costs the reporter another dump.

    Appended LAST so that every key a reader already knows keeps its position; a
    section that reshuffles between releases is a section people stop skimming.

    THE PRIVACY ARGUMENT, stated precisely because the obvious version of it is
    wrong. Registry rows ARE an input here: every key of `sources` is a tag built
    in `_entity_inventory` from `_entity_row(row.unique_id, appliance_id)`, so
    cloud-derived text reaches this map's KEYS. What makes that safe is not that
    the registry is untouched, it is `_entity_row`'s prefix slice (the appliance
    id is removed by construction, having already been proven a prefix) plus the
    closed vocabulary of unique_id suffixes, every one of which is a constant
    written in this repository. The VALUES are safe for a stronger reason: they
    are copied out of the static per-type tables and never out of the device, so
    no cloud string can reach them at all. Both halves are pinned by tests, and
    the first is the one to re-check whenever a new entity class lands -- a class
    that built its unique_id out of a device-supplied name would break it.

    A non-Mapping inventory yields None, which is today's behaviour for "there was
    nothing to decorate", unchanged. A section with no `by_domain` is returned as
    a plain copy: that shape is the degraded `{"status": "unavailable"}` marker,
    whose whole meaning is that the registry could not be read, and hanging a
    `sources` key off it would claim a lookup that never happened. The test that
    guards it asserts EXACT dict equality, on purpose.
    """
    if not isinstance(entities, Mapping):
        return None
    section = dict(entities)
    by_domain = section.get("by_domain")
    if not isinstance(by_domain, Mapping):
        return section
    _mapped_attrs, _mapped_params, index, _unavailable = (
        mapped if mapped is not None else _mapped_sets(app_type)
    )
    if index is None:
        # ONE null for the whole section, not one per entity. A map of nulls would
        # read as "every entity was looked up and none of them is known", which is
        # a finding; "this dump could not look" is the absence of one, and the
        # sibling `_registry_entries` docstring spells out why the two must never
        # be folded together. Reached only when NO table could be read at all:
        # a single broken platform module leaves the rest of the index intact,
        # and its own entities fall through to the per-entity null below, which
        # is that same distinction drawn one entity at a time.
        section["sources"] = None
        return section
    sources: dict[str, dict[str, list[str]] | None] = {}
    for domain, keys in by_domain.items():
        if not isinstance(keys, (list, tuple)):
            continue
        for key in keys:
            # `index.get` and not `index[...]`: a registry row left over from an
            # older release names a suffix no current table knows, and that row
            # must still appear here as an explicit null. Dropping it would make
            # `sources` disagree with `by_domain` about how many entities exist,
            # which is the one thing a join key must never do.
            tag = f"{domain}.{key}"
            sources[tag] = _declared_write_only(index.get(tag), tag, declared)
    section["sources"] = sources
    return section


def _registry_entries(hass: HomeAssistant, entry: ConfigEntry) -> list | None:
    """Every registry row of this config entry, or None when unreadable.

    The import is function-local, mirroring `_remove_legacy_entities`: the module
    must stay importable where `homeassistant.helpers.entity_registry` is not
    installed, and the registry is only ever needed while a dump is being built.

    None and [] mean DIFFERENT things and must never be folded together: [] is
    "Home Assistant remembers nothing for this entry", which is a finding, while
    None is "this dump could not look", which is the absence of one. Reporting the
    second as the first would invent the exact failure the section exists to
    detect.
    """
    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        if registry is None:
            return None
        entries = er.async_entries_for_config_entry(registry, entry.entry_id)
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        _LOGGER.debug("Diagnostics debug: entity registry unreadable", exc_info=True)
        return None
    return list(entries)


def _states_getter(hass: HomeAssistant):
    """`hass.states.get`, or None where there is no state machine to ask."""
    try:
        return getattr(getattr(hass, "states", None), "get", None)
    except Exception:  # noqa: BLE001 - same rule as above
        return None


# A zoned appliance id is `<base>_z<N>` (client/engine/appliance.py `_check_name_zone`),
# so the base id is a proper prefix of its own zones'. Longest-first matching handles
# that while every zone is present, but a zone the current poll dropped would have its
# rows fall back onto the base appliance. No entity of the base appliance has a
# unique_id suffix in this shape, so it is a safe tell.
_ZONE_SUFFIX_RE = re.compile(r"^z\d+_")


def _entity_row(unique_id: str, appliance_id: str) -> str:
    """The unique_id with its appliance prefix removed.

    Every entity in this integration is named `f"{appliance_id}_{suffix}"` where the
    suffix is a constant written in this repository, so what is emitted is drawn
    from a closed, already-public vocabulary. Slicing (rather than masking) is what
    keeps the appliance id out: the caller has already proven the prefix matches.
    """
    return unique_id[len(appliance_id) + 1:]


def _entity_inventory(
    entries: list | None,
    appliance_ids: list,
    entry_id: str,
    state_get,
) -> tuple[dict, dict]:
    """Split the registry rows into a per-appliance view and an entry-wide one.

    Returns ``(per_appliance, platforms)``. Pure: it takes the rows, never `hass`.

    Three facts drive the shape:

    - A row is attributed by unique_id PREFIX, longest id first. Prefixes nest for
      real (a multi-zone appliance expands into `<id>`, `<id>_z1`), and the
      mandatory separator plus longest-first order resolves that. Suffix matching,
      which the legacy cleanup documents as ambiguous, is never used.
    - A DISABLED row has no state by construction: Home Assistant does not add
      disabled entities to the state machine. Consulting the state machine for one
      would report every user-disabled entity as "the platform failed", which is
      the single most common reason a control is missing and the least alarming.
    - A row that is enabled and yet has no live state (or a `restored` one) is the
      interesting case: Home Assistant remembers the entity from an earlier run but
      nothing re-created it now. That is what a dead platform looks like, and it is
      invisible to the registry alone, which survives reloads.
    """
    unavailable = {"status": "unavailable"}
    ids = sorted(
        (i for i in appliance_ids if isinstance(i, str) and i), key=len, reverse=True
    )
    if entries is None:
        return ({appliance_id: dict(unavailable) for appliance_id in ids}, dict(unavailable))

    status = "ok" if callable(state_get) else "registry_only"
    # Seeded for EVERY appliance, before a single row is read: an appliance with no
    # rows must render as "nothing was created", never as "nothing was looked at".
    per_appliance: dict = {
        appliance_id: {"status": status, "by_domain": {}} for appliance_id in ids
    }
    account: dict = {}
    account_not_created: dict = {}
    unattributed = 0
    truncated = False

    for row in entries:
        unique_id = getattr(row, "unique_id", None) or ""
        entity_id = getattr(row, "entity_id", None) or ""
        domain = entity_id.split(".", 1)[0]
        if not unique_id or not domain:
            unattributed += 1
            continue
        if entry_id and unique_id.startswith(f"{entry_id}_diag_"):
            # Account-level debug entities: they belong to no appliance, and they
            # are created unconditionally. A domain missing from here is a domain
            # whose platform did not finish, which is what separates a dead
            # platform from a device that was simply gated out.
            #
            # Counted the same way an appliance row is: a registry row left over
            # from an earlier run proves the platform ran ONCE, not that it ran
            # now, and taking it at face value would clear the very platform that
            # just died. Disabled rows are skipped for the same reason as below.
            account[domain] = account.get(domain, 0) + 1
            if (
                callable(state_get)
                and not _enum_text(getattr(row, "disabled_by", None))
                and _is_restored(state_get, entity_id)
            ):
                account_not_created[domain] = account_not_created.get(domain, 0) + 1
            continue
        appliance_id = next(
            (i for i in ids if unique_id.startswith(f"{i}_")), None
        )
        if appliance_id is not None and _ZONE_SUFFIX_RE.match(
            _entity_row(unique_id, appliance_id)
        ):
            # The row belongs to a ZONE of this appliance, and that zone is not in
            # the data this dump was built from: the poll dropped it, or the cloud
            # stopped declaring it. Attributing it to the base appliance would put
            # an entity in a block that does not own it, and would drag its
            # not_created finding along with it.
            appliance_id = None
        if appliance_id is None:
            unattributed += 1
            continue

        section = per_appliance[appliance_id]
        bucket = section["by_domain"].setdefault(domain, [])
        if len(bucket) >= _ENTITY_MAX_PER_DOMAIN:
            # Marked on the section that actually dropped a row, not entry-wide:
            # a complete inventory carrying someone else's truncation flag reads
            # as incomplete, and the reader stops trusting the one thing this
            # section exists to state.
            section["truncated"] = True
            truncated = True
            continue
        key = _entity_row(unique_id, appliance_id)
        bucket.append(key)
        # Qualified with the domain OUTSIDE by_domain, which is the only structure
        # carrying it implicitly. Home Assistant scopes unique_id uniqueness to the
        # domain and this integration relies on that: a wine cooler holds both a
        # `light` switch and a `light` binary sensor, and the purifier's panel light
        # became a select keeping the light's unique_id. Bare suffixes would let one
        # overwrite the other in these maps, silently dropping a finding, and would
        # leave the reader unable to tell which of the two a finding is about.
        # It LOOKS like an entity_id and is not one: the right-hand side is the
        # code-authored unique_id suffix, never the nickname-derived object_id.
        tagged = f"{domain}.{key}"
        disabled_by = _enum_text(getattr(row, "disabled_by", None))
        hidden_by = _enum_text(getattr(row, "hidden_by", None))
        if disabled_by:
            section.setdefault("disabled", {})[tagged] = disabled_by
            continue
        if hidden_by:
            section.setdefault("hidden", {})[tagged] = hidden_by
        if not callable(state_get):
            continue
        if _is_restored(state_get, entity_id):
            section.setdefault("not_created", []).append(tagged)

    totals: dict = {}
    for section in per_appliance.values():
        for domain, keys in section["by_domain"].items():
            totals[domain] = totals.get(domain, 0) + len(keys)
        for domain in section["by_domain"]:
            section["by_domain"][domain] = sorted(section["by_domain"][domain])
        for field in ("disabled", "hidden", "not_created"):
            if field in section and isinstance(section[field], list):
                section[field] = sorted(section[field])

    platforms = {
        "status": status,
        "appliance_totals": totals,
        "account": account,
        "account_not_created": account_not_created,
        "unattributed": unattributed,
    }
    if truncated:
        platforms["truncated"] = True
    return per_appliance, platforms


def _enum_text(value) -> str | None:
    """A registry flag as plain text: these are enums whose `.value` is the token."""
    if value is None:
        return None
    inner = getattr(value, "value", value)
    return str(inner)


def _is_restored(state_get, entity_id: str) -> bool:
    """True when the registry remembers the entity but nothing created it now.

    Home Assistant writes a placeholder state carrying ``restored: True`` for a
    registered entity no platform added, so the two cases (no state at all, and a
    restored one) are the same finding.
    """
    try:
        state = state_get(entity_id)
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        return False
    if state is None:
        return True
    attributes = getattr(state, "attributes", None)
    if isinstance(attributes, Mapping):
        return bool(attributes.get("restored"))
    return False


def _coordinator(hass: HomeAssistant, entry: ConfigEntry):
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    return entry_data.get("coordinator")


# A setup phase as the client records it: flat ("authenticate", "mfa_challenge") or
# hierarchical ("load_appliances/auth"). Shape-validated rather than enumerated, because
# the vocabulary is assembled at raise time from the segment a phase() context was opened
# with (client/phase.py) and a frozenset here would have to be kept in step with every
# new context in the transport. Lower-case words and slashes only, and short: whatever a
# future caller puts in `last_error_phase`, what this file publishes is that shape.
_PHASE_RE = re.compile(r"[a-z_]+(?:/[a-z_]+)*")
_PHASE_MAX_LEN = 64


def _phase_token(value) -> str | None:
    """A phase name, or None. The `type(...) is not str` is the guard `_closed_token`
    documents: a str SUBCLASS passes `isinstance` and re-implements `__str__`, so the
    string that reaches the file need not be the one the regex matched."""
    if type(value) is not str or len(value) > _PHASE_MAX_LEN:
        return None
    return value if _PHASE_RE.fullmatch(value) else None


def _reason_for_label(label: str | None) -> str | None:
    """The catalog reason for an ADDHON label, resolved HERE and never echoed.

    `_last_error`'s live branch reads `code.reason_en` off the code object the client
    holds, which is an `error_codes` singleton -- the text is this module's, not the
    cloud's. The failure-record branch has no such object: it has a string that has been
    through hass.data. Resolving the reason from the catalog instead of storing it keeps
    the two branches on the same footing, and means a record written by an older version
    of this integration cannot put a stale (or foreign) sentence in the document.
    """
    if label is None:
        return None
    from . import error_codes

    for code in error_codes.all_codes():
        if code.label == label:
            return code.reason_en
    return None


def _setup_failed(failure: Mapping) -> dict:
    """`last_error` for an entry whose setup failed, from the record __init__ left.

    Same leak-proof argument as the live branch below, one notch stricter because the
    values arrive through a mapping rather than off a code object: the label is
    shape-checked (`_label_token`), the reason is looked up in the catalog rather than
    read from the record, the phase is shape-checked, and the instant goes through the
    same `_as_utc` + `_stamp_text` pair every other stamp in this file uses.

    `phase_ledger` is passed through exactly as the live branch passes it (:2620): a
    list of {phase, seconds, outcome} rows built by our own phase tracker, in this
    process, out of the same objects. It is guarded to a list so a foreign value cannot
    make the key a shape no reader expects, but its rows are not rebuilt -- doing that
    here and not there would claim a difference in trust that does not exist.
    """
    label = _label_token(failure.get("code"))
    out: dict = {
        "status": "setup_failed",
        "code": label,
        "reason": _reason_for_label(label),
        "phase": _phase_token(failure.get("phase")),
        "at": _stamp_text(_as_utc(failure.get("at"), assume_naive_utc=False)),
    }
    ledger = failure.get("phase_ledger")
    if isinstance(ledger, list) and ledger:
        out["phase_ledger"] = ledger
    return out


def _last_error(hass: HomeAssistant, entry: ConfigEntry) -> dict | None:
    """The last classified setup/update error code, for issue triage.

    Static code+reason (no device identity), pulled from the client. None when the
    client was asked and had recorded no failure; ``{"status": "client_absent"}``
    when there was no client to ask at all (e.g. a failed setup)."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    client = entry_data.get("client")
    if client is None:
        # A dump from a failed setup used to be indistinguishable from a healthy
        # account that simply owns no appliances: both read `"last_error": null,
        # "appliances": []`, and a real 5.9.3 report was triaged as the second when
        # it was the first. `null` is now the answer of a client, not the absence of
        # one.
        #
        # A bucket without a client IS a state this integration produces, and only
        # one thing produces it: `_store_setup_failure` replacing the bucket on a
        # failed setup (__init__.py). When that record is there it is a strictly
        # better answer than the marker below -- it carries the classified code, the
        # phase, the ledger -- so it is preferred. `client_absent` keeps its meaning
        # for everything else: an entry absent from hass.data, and a bucket a future
        # caller builds without either key.
        failure = entry_data.get("setup_failure")
        if isinstance(failure, Mapping):
            return _setup_failed(failure)
        # A closed-domain token like the fields below, so it needs no _redact either.
        return {"status": "client_absent"}
    code = getattr(client, "last_error_code", None)
    if code is None:
        return None
    # All fields are closed-domain primitives (catalog strings / bools / a finite phase
    # token / the 2FA challenge_kind enum) -- no device identity, no token/OTP/csrf, so
    # this hand-built block is leak-proof by construction (not run through _redact).
    out: dict = {
        "code": code.label,
        "reason": code.reason_en,
        "requires_reauth": getattr(code, "requires_reauth", None),
        "ui": getattr(code, "ui", None),
        "phase": getattr(client, "last_error_phase", None),
        "had_refresh_token": bool(getattr(client, "_refresh_token", "")),
    }
    # Per-phase duration+outcome of the failed attempt (issue #76): without it a report
    # cannot say WHICH phase burned the time, so no hypothesis about a timeout is
    # falsifiable. Same leak-proof shape as the block above: phase names from a closed
    # vocabulary, rounded seconds, and an outcome in {ok, error, timeout}.
    ledger = getattr(client, "last_phase_ledger", None)
    if ledger:
        out["phase_ledger"] = ledger
    # 2FA summary only when the failure is in the MFA band (160-169) -- challenge_kind is
    # the enum "email"/None and can_resend is a bool; the MfaContext secrets are NEVER here.
    mfa = getattr(client, "last_mfa_summary", None)
    if isinstance(mfa, dict) and 160 <= getattr(code, "code", 0) <= 169:
        out["mfa"] = {
            "challenge_kind": mfa.get("challenge_kind"),
            "can_resend": mfa.get("can_resend"),
        }
    return out


# The option keys the Configure screen writes (config_flow.OptionsFlowHandler),
# taken from the CONF_ constants so renaming one cannot leave a stale literal
# here that quietly demotes a live toggle to `***`. CONF_AUTH_DIAGNOSTICS is
# deliberately absent although it sits with them in const.py: the config flow
# strips it before anything is validated or persisted, so it is not an option
# key, and a build that ever left it in `entry.options` should be told so by the
# mask rather than trusted.
_KNOWN_OPTIONS = frozenset(
    {CONF_ENABLE_DEBUG, CONF_ENABLE_EXPERIMENTAL, CONF_ENABLE_MQTT_DEBUG}
)


def _entry_options(options: Mapping) -> dict:
    """Entry options with every key outside `_KNOWN_OPTIONS` stripped of its value.

    Mask first, admit by name: today all three toggles are booleans and nothing
    here can leak, but this block is copied out of storage unread, so the FOURTH
    option ships whatever its author put in it -- a token, an endpoint, a user
    string -- into every dump pasted into an issue. A whitelist is what makes
    that a deliberate act instead of the default.

    An unrecognised key keeps its NAME and loses only its VALUE: "this install
    carries an option this build knows nothing about" is itself a finding, and
    dropping the key would hide the very thing worth reporting. Sorted like the
    other mappings in the dump, so two downloads of the same install differ only
    where the install does.

    Admitting a key admits a SHAPE with it. What makes these three safe to print
    is not their names but the fact that the Configure screen coerces each to a
    bool before storing it (config_flow.py: `bool(user_input.get(...))`), so a
    string under `enable_debug` was not written by the form this whitelist
    trusts, and nothing vouches for what it holds. It is masked like any
    stranger, and the name still reports that the install carries it.
    """
    return {
        key: (
            options[key]
            if key in _KNOWN_OPTIONS and isinstance(options[key], bool)
            else _REDACTED
        )
        for key in sorted(options, key=str)
    }


def _last_poll(hass: HomeAssistant, entry: ConfigEntry) -> dict | None:
    """The census the client recorded for its last completed poll cycle.

    Passed through as the client built it (`hon_client._poll_census`), which is
    where the leak-proof shape is enforced. None when no cycle has ever completed
    -- no client, a setup that never got that far, or a first poll that aborted --
    and `last_error` is what speaks for those.
    """
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    census = getattr(entry_data.get("client"), "last_poll_census", None)
    return dict(census) if isinstance(census, Mapping) else None


# The closed domains `last_fetch` may print. Duplicated from the transport rather
# than imported: the Home Assistant layer does not import transport internals
# anywhere else in this file, and a drift test pins each set against its source
# (the same guarantee `_KNOWN_OPTIONS` and `_TO_REDACT` already carry, and for the
# same reason -- the duplication is what makes the guard a test rather than an
# import that would drag the transport package into every dump).
_FETCH_STATES = frozenset({"recorded", "never_ran", "client_absent", "unreadable"})
_FETCH_STOPS = frozenset({"modules", "applianceList", "payload", "appliances"})
_FETCH_OUTCOMES = frozenset(
    {"ok", "not_a_dict", "missing_key", "not_a_list", "raised", "other"}
)
_FETCH_NODE_TYPES = frozenset(
    {"dict", "list", "str", "int", "float", "bool", "NoneType", "other"}
)
# The verdicts of the identity self-check `api.account_match` makes: does the account
# our id_token names still own the appliances the cloud answered with. Duplicated and
# drift-tested against `api.ACCOUNT_TOKENS` exactly like the sets above -- and here the
# duplication matters more than anywhere else in the block, because a verdict this
# build has not heard of would print as "other", which for an identity question is the
# one answer a reader cannot act on.
_FETCH_ACCOUNTS = frozenset(
    {"match", "mismatch", "mixed", "no_appliances", "no_claim", "unknown"}
)
# The reasons a setup may drop an appliance the cloud returned. The counts arrive from
# the session, which declares the same tokens as `SETUP_DROP_REASONS` and is pinned
# against this tuple by a drift guard; a reason this build does not know is a reason
# this build cannot vouch for, so it is dropped instead of printed.
_FETCH_SKIP_REASONS = ("not_a_dict", "bad_zone", "construction_error", "mac_empty")
# Only to refuse a foreign object: a count is an int and an int cannot carry identity,
# so this is hygiene, not privacy.
_FETCH_MAX_INT = 1_000_000
# Ten years either way. `_age_seconds` returns a NEGATIVE number when the clocks
# disagree (documented at its definition) and that finding must survive the bound.
_FETCH_MAX_AGE_S = 315_360_000
# A catalog label cannot be enumerated here without duplicating error_codes.py, so it
# is bounded by SHAPE instead: seven fixed characters and three digits, validated on
# the way OUT. `last_poll.dropped` already emits `code.label` unvalidated; this is the
# same value under a tighter contract, not a new class of value.
_ADDHON_LABEL_RE = re.compile(r"ADDHON-[0-9]{3}")
_FETCH_MAX_LABEL_ROWS = 20


def _closed_token(value, allowed: frozenset) -> str | None:
    """A token of `allowed`, or "other". NEVER an object chosen by the writer.

    `type(value) is str`, NOT isinstance -- the same refusal `_bounded_int` makes just
    below, for a stronger reason. isinstance admits SUBCLASSES; a str subclass is free
    to override `__eq__`/`__hash__` so that it compares equal to "ok" while its actual
    characters are "3C:71:BF:AA:BB:CC", and Home Assistant's JSON encoder writes the
    characters. With `type(value) is str` the comparison is `str.__eq__` on a plain
    str, so the object returned is a plain str whose characters ARE the literal's --
    which is what makes "the output is a literal of this module" a fact rather than a
    hope.

    An unrecognised value renders as "other" rather than None so the dump still reports
    that the writer produced something this reader does not know: the same "keep the
    finding, lose the value" rule `_entry_options` applies to an option key.
    """
    if value is None:
        return None
    return value if type(value) is str and value in allowed else "other"


def _bounded_int(value, low: int, high: int) -> int | None:
    """An int inside [low, high], else None. `bool` is refused on purpose:
    isinstance(True, int) is True and would render this field as `true`."""
    return value if type(value) is int and low <= value <= high else None


def _closed_bool(value) -> bool | None:
    """`True`, `False` or None -- never the object the writer handed over.

    The mirror image of `_bounded_int` beside it: that one refuses a bool because
    isinstance(True, int) is True, this one refuses an INT because `1` is not the same
    statement as `true`. The fields it guards report what the CLOUD said about its own
    call, so `1`, `"true"` and `"success"` must all come out as null -- "this build
    could not tell" -- rather than be coerced into the very answer the field exists to
    establish. `bool(value)` here would make every non-empty string in the document
    read as a cloud that declared success.

    `type(value) is bool` also makes the output provably one of two singletons of this
    process: `bool` cannot be subclassed in CPython, so unlike `_closed_token` there is
    no `__eq__` trick to close and no "other" bucket to keep -- the value is a literal
    or it is nothing.
    """
    return value if type(value) is bool else None


def _label_token(value) -> str | None:
    """An ADDHON catalog label, or None. Shape-validated on the way out, so the
    emitted string is provably "ADDHON-" plus three digits and nothing else."""
    if type(value) is not str or not _ADDHON_LABEL_RE.fullmatch(value):
        return None
    return value


def _skip_census(raw) -> dict:
    """Reason -> count, over the ALLOWLIST and never over the mapping received.

    Iterating the received mapping would put a key CHOSEN BY THE WRITER into the
    document, which is precisely the door `_redact` cannot close: it masks by key NAME
    (:375-392), so a key it has never heard of is a key it publishes.

    Four keys at most, by construction. No `truncated` twin is needed and none is
    admitted: the problem `attributes_last_update` solves that way (:1940-1952) does
    not exist when no key of the map is chosen by the cloud.
    """
    raw = raw if isinstance(raw, Mapping) else {}
    out: dict = {}
    for reason in _FETCH_SKIP_REASONS:
        counted = _bounded_int(raw.get(reason), 0, _FETCH_MAX_INT)
        if counted:
            out[reason] = counted
    return out


def _degraded_census(raw) -> dict:
    """ADDHON label -> count, both halves validated on the way out.

    Unlike `_skip_census` the keys cannot come from an allowlist without duplicating
    error_codes.py here, so they are bounded by SHAPE: `_label_token` emits only
    "ADDHON-" plus three digits, and a row count caps the map. A key that is not a
    label is dropped whole -- keys and values move together or not at all.

    This is the ONLY place in the block that iterates a mapping it received instead of
    an allowlist. It is admitted because the key is validated for shape BEFORE it is
    written, not because the writer is trusted: the session that fills this map keys it
    by `f"{mac}#{zone}"` in its own bookkeeping, so a build that ever handed those keys
    over directly would publish a MAC per degraded appliance.
    """
    raw = raw if isinstance(raw, Mapping) else {}
    out: dict = {}
    for key, value in raw.items():
        if len(out) >= _FETCH_MAX_LABEL_ROWS:
            break
        label = _label_token(key)
        counted = _bounded_int(value, 0, _FETCH_MAX_INT)
        if label is not None and counted:
            out[label] = counted
    return out


# The keys of a block that has nothing to report. Written once so a `never_ran` and a
# `recorded` block can never disagree about which keys exist: the key set of a
# top-level block has to be stable for a field-by-field diff between two downloads of
# the same issue to mean anything.
#
# A FACTORY, not a module constant, and the two mutable values are why. `{**CONST}` is
# a SHALLOW copy, so a constant would hand every `never_ran`/`client_absent`/
# `unreadable` block ever produced by this process the same two dict objects -- aliased
# to the constant itself. Nothing mutates the returned document today (Home Assistant
# json-serialises it and `_redact` never reaches this block), so that was latent rather
# than live; but the claim this block makes is that every value in it is a literal of
# this module BY CONSTRUCTION, and an alias makes that claim depend on no future caller
# ever writing into the dict it was handed. A census that grew a `truncated` twin, a
# post-pass that annotated the block, or a test helper would then have written into
# EVERY later dump of EVERY config entry in the process. Building them per call costs
# two empty dicts per download and removes the premise. Pinned by
# test_the_key_set_is_the_same_in_every_state.
def _fetch_empty() -> dict:
    return {
        "at": None, "age_s": None, "status": None, "code": None, "outcome": None,
        "stopped_at": None, "node_type": None, "siblings": None, "count": None,
        "envelope_ok": None, "module_ok": None, "auth_keys": None, "account": None,
        "expanded": None, "built": None, "skipped": {}, "degraded": {},
    }


def _last_fetch(hass: HomeAssistant, entry: ConfigEntry, *, now: datetime) -> dict:
    """What the ONE appliance-list HTTP call returned, and what setup made of it.

    NEVER None. `last_poll` beside this is a per-cycle recount of a list built once at
    setup (the poll reads `HonClient.async_get_appliances`, which returns the cached
    inventory `NativeHon.setup()` filled); this block is the call itself, and it is the
    only place in the dump where "the cloud sent nothing" and "we could not read what
    the cloud sent" stop looking alike. A `null` block would fold those into the
    absence of a session -- the ambiguity `_last_error` was rewritten to remove
    (:2582-2588).

    IN that list since the failed setup got a record of its own: `outcome: "raised"`,
    "the call never reached a body". The census still dies with the session the raise
    tore down -- `setup_sync` snapshots it into `last_setup_fetch` one line before
    `_close_sync`, and `_store_setup_failure` copies that into the entry bucket in
    place of the coordinator and client the setup never handed over. A dump taken
    after a failed setup therefore reads the block, not `client_absent`. See
    `HonApi.load_appliances` for the writer.

    Leak-proof by construction on the strongest terms in the file: every KEY is a
    literal written above, and every VALUE is either an int this function range-checks,
    a boolean `_closed_bool` refuses to coerce, an instant `_stamp_text` validates
    against `_ISO_RE`, a token `_closed_token` looks up in a frozenset written above,
    or a label `_label_token` shape-checks. Not one string from the cloud, from the
    session or from the client is copied into the document -- so `_redact` is not
    merely skipped here, it would have nothing to do.

    That rule is what shapes `account`, the block's identity self-check: the two
    account ids it rests on are compared in the transport (`api.account_match`) and
    only the VERDICT crosses into this file. Same for `auth_keys`, which is a count of
    the keys in an object whose values include a bearer token, and never their names.

    Deliberate divergence from `_last_poll` next door, which passes the census through
    as `dict(census)` and rests entirely on its writer. This one does not trust its
    writer, because the census arrives through a `getattr` on `_hon_instance`, which in
    a test or in the presence of a session double can be any object at all.
    """
    try:
        return _build_last_fetch(hass, entry, now=now)
    except Exception:  # noqa: BLE001 - a dump must degrade, never raise
        _LOGGER.debug("Diagnostics debug: last_fetch not computable", exc_info=True)
        return {"state": "unreadable", **_fetch_empty()}


def _build_last_fetch(hass: HomeAssistant, entry: ConfigEntry, *, now: datetime) -> dict:
    """The block itself. Split from `_last_fetch` so the guard above is the whole
    degradation story: eight `.get` calls on a mapping this function declares untrusted
    and five `getattr` reads of properties that may raise, all of them inside a
    convenience field, and none of them allowed to take the download down."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    client = entry_data.get("client")
    if client is None:
        # No client, but possibly a record of why there is none. `_store_setup_failure`
        # flattens the census and the four counters into one mapping precisely so this
        # branch reads the same block builder the live branch does -- and it is what
        # finally makes `outcome: "raised"` reachable in a dump. The census of a setup
        # that died before the response had a body used to be torn down with the
        # session that raised, which is why the docstring above listed that state as
        # written-but-unobservable.
        failure = entry_data.get("setup_failure")
        recorded = failure.get("fetch") if isinstance(failure, Mapping) else None
        if isinstance(recorded, Mapping):
            return _fetch_block(recorded, recorded, now=now)
        return {"state": "client_absent", **_fetch_empty()}
    raw = getattr(client, "last_appliance_fetch", None)
    if not isinstance(raw, Mapping):
        # A live client whose session was closed, or one between _close_sync and the
        # end of setup_sync on a runtime re-auth. `null` would be true too, but silent.
        return {"state": "never_ran", **_fetch_empty()}
    return _fetch_block(
        raw,
        # The counters are what SETUP made of the list; `raw` is what the CALL
        # returned. Two sources on the live path, one flattened mapping on the record
        # path, and one block builder for both.
        {
            "expanded": getattr(client, "setup_expanded", None),
            "built": getattr(client, "appliance_count", None),
            "skipped": getattr(client, "setup_drops", None),
            "degraded": getattr(client, "degraded_census", None),
        },
        now=now,
    )


def _fetch_block(raw: Mapping, counters: Mapping, *, now: datetime) -> dict:
    """The `recorded` block, from a census mapping and a counter mapping.

    Every value is validated HERE rather than at either call site, so the block a live
    client produces and the block a setup-failure record produces are the same document
    under the same guarantees -- and the record, which has been through hass.data and is
    therefore the less trusted of the two, cannot be the one that got the shorter check.
    """
    moment = _as_utc(raw.get("at"), assume_naive_utc=False)
    return {
        "state": "recorded",
        # Both halves hang off ONE `moment`, so an instant `_as_utc` refuses -- a naive
        # datetime (:1259-1260), a string that merely looks like one, any foreign
        # object -- blanks BOTH. That is the coupling that matters: it is what stops a
        # value this reader could not validate from being half-published.
        #
        # It is NOT a claim that the two always move together, and the two ways they
        # part are both readings worth keeping rather than defects to suppress:
        #   * `at` alone, `age_s` null -- the age fell outside +/-10 years. The usual
        #     cause is the reporter's own clock (a host that stamped the fetch before
        #     NTP corrected it reads `1970-01-01T00:00:11+00:00`, `age_s: null`).
        #     Blanking `at` too would delete that finding, which is the one thing in
        #     the block that explains why every other age in the dump looks absurd.
        #   * `age_s` alone, `at` null -- `_stamp_text` refused to render an instant
        #     `_as_utc` accepted, which takes a `datetime` SUBCLASS whose `isoformat`
        #     returns something that is not an ISO stamp. The refusal is the point (an
        #     unvalidated string is exactly what must not reach the file) and the age
        #     survives it because it is computed, not echoed.
        # Neither half is unreadable on its own: `generated_at` is in the same document,
        # so a reader recovers whichever is missing by subtraction. Both cases are
        # pinned in test_at_and_age_move_together.
        "at": _stamp_text(moment),
        "age_s": (
            _bounded_int(_age_seconds(moment, now), -_FETCH_MAX_AGE_S, _FETCH_MAX_AGE_S)
            if moment is not None
            else None
        ),
        "status": _bounded_int(raw.get("status"), 100, 599),
        "code": _label_token(raw.get("code")),
        "outcome": _closed_token(raw.get("outcome"), _FETCH_OUTCOMES),
        "stopped_at": _closed_token(raw.get("stopped_at"), _FETCH_STOPS),
        "node_type": _closed_token(raw.get("node_type"), _FETCH_NODE_TYPES),
        "siblings": _bounded_int(raw.get("siblings"), 0, _FETCH_MAX_INT),
        "count": _bounded_int(raw.get("count"), 0, _FETCH_MAX_INT),
        # The envelope ABOVE the walk, and the question `count: 0` cannot answer on its
        # own. A `success: false` at either level is a state in which the official app
        # shows zero appliances too -- neither it nor this integration has ever read
        # them -- and in which every other field of this block looks exactly like a
        # legitimately empty account. `null` at either means the cloud sent something
        # that was not a boolean, which is itself worth seeing.
        "envelope_ok": _closed_bool(raw.get("envelope_ok")),
        "module_ok": _closed_bool(raw.get("module_ok")),
        # How many keys `modules.applianceList.authInfo` carried, never which. That
        # object is how the cloud hands back a replacement cognito token, and on a
        # healthy session it is verifiably empty -- so 0 is the baseline and any other
        # number is a signal. A count is an int and an int carries no identity; a key
        # NAME here would be a value chosen by the cloud, and one of the values behind
        # those names is a bearer token.
        "auth_keys": _bounded_int(raw.get("auth_keys"), 0, _FETCH_MAX_INT),
        # Whose appliances the cloud answered with, as a verdict and never as an id.
        # See `api.account_match`: the comparison happens in the transport and only the
        # token crosses into this document.
        #
        # `mismatch` and `mixed` are NOT faults to triage on their own: family sharing
        # puts appliances a member does not own into that member's own list, so a group
        # member reads `mismatch` on a perfectly healthy install. The verdict states an
        # account BOUNDARY; only the reported symptom says whether crossing it is the
        # bug. `no_appliances` is the one that carries a complaint by itself.
        "account": _closed_token(raw.get("account"), _FETCH_ACCOUNTS),
        "expanded": _bounded_int(counters.get("expanded"), 0, _FETCH_MAX_INT),
        "built": _bounded_int(counters.get("built"), 0, _FETCH_MAX_INT),
        "skipped": _skip_census(counters.get("skipped")),
        "degraded": _degraded_census(counters.get("degraded")),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """Return diagnostics for a config entry (all appliances)."""
    # Read ONCE, before anything else is built. `generated_at` and every age in
    # every appliance block below must all describe the same instant, or the
    # document contains numbers that disagree for a reason no reader can see.
    now = _utcnow()
    coordinator = _coordinator(hass, entry)
    _LOGGER.debug(
        "Diagnostics debug: diagnostics requested entry=%s title=%s coordinator_present=%s",
        entry.entry_id,
        _redact_title(getattr(entry, "title", None)),
        coordinator is not None,
    )

    coord_data = getattr(coordinator, "data", None)
    coord_data = coord_data if isinstance(coord_data, Mapping) else {}
    inventory, platforms = _entity_inventory(
        _registry_entries(hass, entry),
        list(coord_data),
        getattr(entry, "entry_id", "") or "",
        _states_getter(hass),
    )

    appliances: list[dict] = []
    for appliance_id, data in coord_data.items():
        if isinstance(data, Mapping):
            appliances.append(
                _appliance_block(
                    appliance_id, data, inventory.get(appliance_id), now=now
                )
            )

    return {
        # The FIRST key of the document on purpose: 'when was this taken' is the
        # first thing a maintainer needs and the last thing a reporter thinks to
        # say, and an issue thread routinely carries two downloads days apart
        # with nothing in the files to tell them apart. Aware UTC, so it always
        # ends in '+00:00' and never has to be reconciled with the zone the
        # reporter's machine happened to be in. Like `last_error` and
        # `platforms` below, this key sits OUTSIDE `_redact` -- only
        # `_appliance_block` redacts -- so it is leak-proof by construction
        # instead: `_stamp_text` refuses every non-datetime and then validates
        # its own output against `_ISO_RE` and a 40 character bound, so the
        # only thing that can appear here is an instant.
        "generated_at": _stamp_text(now),
        "entry": {
            "title": _redact_title(entry.title),
            "data": {
                "email": _redact_email(entry.data.get("email")),
                "password": _REDACTED,
            },
            "options": _entry_options(entry.options),
        },
        "last_error": _last_error(hass, entry),
        # Entry-wide, next to last_error rather than inside an appliance: a failed
        # setup leaves no appliances at all, and this is exactly the dump where the
        # question "did anything get created" needs an answer. Closed-domain
        # primitives only (platform domains, counts, a status token), so like
        # last_error it is leak-proof by construction and skips _redact.
        "platforms": platforms,
        # What separates "this account owns nothing" from "the cloud returned three
        # and the poll dropped all three": both render as `"appliances": []` with a
        # null last_error, and until now only the `Partial update` WARNING -- which
        # no dump carries -- told them apart. Counts plus ADDHON catalog labels, so
        # like the two keys above it is leak-proof by construction and skips _redact.
        "last_poll": _last_poll(hass, entry),
        # The ONE HTTP call the whole inventory rests on, which happens once per
        # session at setup and is therefore invisible to the poll above: `last_poll`
        # recounts a list built long before it. This is what separates "the cloud sent
        # an empty list" from "our walk could not reach the list" -- two states that
        # both render as `"appliances": []` with a null `last_error`, and that until
        # now no dump could tell apart. The third of that family, "the call never
        # reached a body", is written by the transport but is NOT readable here: it
        # dies with the session the raise tore down (see `_last_fetch`). Closed-domain
        # tokens, range-checked ints and a validated instant, so like `last_poll` it is
        # leak-proof by construction and skips _redact.
        "last_fetch": _last_fetch(hass, entry, now=now),
        "appliances": appliances,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device
) -> dict:
    """Return diagnostics for a single device (the appliance behind it).

    ``device.identifiers`` is a set of ``(domain, id)`` tuples; base_entity.device_info
    registers ``{(DOMAIN, appliance_id)}``, so the appliance_id is recovered directly.
    The raw identifier (which may BE the serial) is never echoed into the output.

    One device per entry is NOT an appliance: the synthetic per-account service
    device that carries the debug controls. Asked for its diagnostics, this
    returns the account's own dump -- see the route below.
    """
    appliance_id = next(
        (
            ident[1]
            for ident in getattr(device, "identifiers", ()) or ()
            if isinstance(ident, tuple) and len(ident) == 2 and ident[0] == DOMAIN
        ),
        None,
    )
    # The account's service device asks an ACCOUNT question, so it gets the
    # account's answer. Its identifier is `{entry_id}{ACCOUNT_DEVICE_SUFFIX}`
    # (base_entity.device_info), which is never a key of `coordinator.data` --
    # that mapping is indexed by appliance id. Without this route the lookup
    # below could only miss, so every user pressing "Download diagnostics" on
    # that device got the degraded dated stub; and in the one install that has
    # ZERO appliances the service device is the only device that exists, so the
    # only such button reachable is the one that could never answer. That is how
    # the ADDHON-210 report arrived as `{"generated_at": ...}` instead of the
    # self-contained dump 5.17.0 shipped for exactly that case.
    #
    # Delegating returns the entry dump byte for byte rather than a bespoke
    # subset. That is deliberate on two counts: it introduces no new class of
    # value and therefore no new privacy surface (`entry` still goes through
    # _redact_title/_redact_email/_entry_options, `appliances` through
    # _appliance_block + _redact, and the leak-proof-by-construction keys stay
    # the same ones), and it leaves ONE top-level key set to maintain instead of
    # a second shape that would drift out of step with the first -- the drift
    # `_fetch_empty` exists to prevent.
    entry_id = getattr(entry, "entry_id", "") or ""
    # Exact equality, never `endswith`: a suffix test would swallow an appliance
    # whose own id happens to end in `_diagnostics` and answer an appliance
    # question with the account dump. Guarded on a non-empty entry_id because
    # without one the expected identifier collapses to the bare suffix, which is
    # a plausible id in its own right; with no entry to compare against the old
    # behaviour stands.
    if entry_id and appliance_id == f"{entry_id}{ACCOUNT_DEVICE_SUFFIX}":
        return await async_get_config_entry_diagnostics(hass, entry)

    # Read BEFORE the two early returns below, not after. A device dump that
    # resolves no appliance is precisely the one that gets pasted into an issue
    # as 'the download gave me nothing', and an undated empty object cannot
    # even be placed in time relative to the entry dump beside it. Read AFTER
    # the account route above so that every path reads the clock exactly once:
    # the delegated dump takes its own single instant.
    now = _utcnow()
    coordinator = _coordinator(hass, entry)
    coord_data = getattr(coordinator, "data", None)

    # Both degraded paths still return a DATED document rather than a bare {}.
    # They are the two dumps most likely to be attached to an issue, because
    # they are what a user gets when the thing they are reporting is that
    # nothing works, and 'the file was empty' is a materially different report
    # from 'the file said it was taken at 07:09 and had nothing in it'. Like
    # the entry dump's own `generated_at`, these bypass `_redact` and are
    # leak-proof by construction rather than by masking. What reaches them is
    # now only what they were meant for -- a device whose identifier this
    # integration does not own, or an appliance device the coordinator has
    # nothing for -- because the account device is answered above.
    if appliance_id is None or not isinstance(coord_data, Mapping):
        return {"generated_at": _stamp_text(now)}
    data = coord_data.get(appliance_id)
    if not isinstance(data, Mapping):
        return {"generated_at": _stamp_text(now)}
    # The inventory is built over EVERY appliance id, not just this one: attribution
    # is by unique_id prefix and those prefixes nest, so hiding the siblings would
    # let a longer id's rows fall into this block.
    inventory, _platforms = _entity_inventory(
        _registry_entries(hass, entry),
        list(coord_data),
        getattr(entry, "entry_id", "") or "",
        _states_getter(hass),
    )
    return {
        "generated_at": _stamp_text(now),
        "appliance": _appliance_block(
            appliance_id, data, inventory.get(appliance_id), now=now
        ),
    }
