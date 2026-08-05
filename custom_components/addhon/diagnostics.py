# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Diagnostics support for Haier hOn (Extended).

This dump is what a user attaches to a GitHub issue when an appliance is "not
mapped" or "mapped badly". Home Assistant renders the built-in "Download
diagnostics" button for the config entry AND for each device (the latter is wired
by async_get_device_diagnostics below); no custom button is needed.

Per appliance the dump carries, beyond the bare key list it used to emit:
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

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    APPLIANCE_AC,
    APPLIANCE_AP,
    APPLIANCE_WASH_GROUP,
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

# Bare attributes consumed by CUSTOM entity classes that have NO description table,
# so a coverage calc based only on the description registries would wrongly report
# them as unmapped. Kept small and documented; a unit test guards against drift.
#   HonMeanWaterConsumption (sensor.py): totalWashCycle + totalWaterUsed
#   HonWashingMachinePauseSwitch (switch.py): machMode
#   HaierClimateEntity (climate.py): tempIndoor (current temp; its other reads are
#       dotted settings.* keys, already excluded from the attribute axis)
_CUSTOM_MAPPED_ATTRS: dict[str, frozenset[str]] = {
    "WM": frozenset({"totalWashCycle", "totalWaterUsed", "machMode"}),
    "WD": frozenset({"totalWashCycle", "totalWaterUsed", "machMode"}),
    "TD": frozenset({"machMode"}),
    "AC": frozenset({"tempIndoor"}),
}

# Settings-command params written by HaierClimateEntity (climate.py has no
# description table). AC only.
_AC_CLIMATE_PARAMS = frozenset(
    {"onOffStatus", "machMode", "tempSel", "windSpeed", "windDirectionVertical"}
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
    """Schema of one command parameter: value + metadata, plus range (min/max/step)
    for a range param OR enum as a fallback only when the param is not a range."""
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
        schema["min"], schema["max"], schema["step"] = rng
    else:
        enum = param_values(param)
        if enum:
            schema["enum"] = enum
    return schema


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


def _mapped_sets(app_type) -> tuple[set[str], set[str]]:
    """(mapped attribute keys, mapped writable command params) for a type.

    Registries are imported lazily so diagnostics.py keeps a tiny top-level import
    surface and cannot be caught in an import cycle; on any import hiccup it degrades
    to the documented custom set rather than crashing the dump.
    """
    mapped_attrs: set[str] = set(_CUSTOM_MAPPED_ATTRS.get(app_type, ()))
    mapped_params: set[str] = set()
    try:
        from .air_purifier import AP_ENTITY_PARAMS
        from .binary_sensor import BINARY_SENSORS, _CONNECTIVITY, _UNIVERSAL_GATED
        from .number import NUMBERS
        from .sensor import SENSORS
        from .switch import _SETTINGS_SWITCHES
    except Exception:  # pragma: no cover - diagnostics must never crash
        _LOGGER.debug(
            "Diagnostics debug: coverage registries unavailable", exc_info=True
        )
        return mapped_attrs, mapped_params

    for desc in SENSORS.get(app_type, ()):
        mapped_attrs.add(desc.attr_key)
        mapped_attrs.update(getattr(desc, "attr_fallbacks", ()) or ())
    for desc in BINARY_SENSORS.get(app_type, ()):
        mapped_attrs.add(desc.attr_key)
    mapped_attrs.add(_CONNECTIVITY.attr_key)
    for desc in _UNIVERSAL_GATED:
        mapped_attrs.add(desc.attr_key)

    for desc in NUMBERS.get(app_type, ()):
        mapped_params.add(desc.param)
    # Settings-command switches (AC toggles + wine-cooler light) map their write param.
    for desc in _SETTINGS_SWITCHES.get(app_type, ()):
        mapped_params.add(desc.param)
    if app_type == APPLIANCE_AC:
        mapped_params |= _AC_CLIMATE_PARAMS
    if app_type in APPLIANCE_WASH_GROUP:
        mapped_params.update(PROGRAM_PARAM_NAMES)
    if app_type == APPLIANCE_AP:
        # The AP fan, light, aroma select, toggles and timing numbers are fixed-key
        # entities or live outside the NUMBERS/_SETTINGS_SWITCHES tables, so the
        # registry walk above cannot see them. Each name is BOTH read as state and
        # written as a command field, hence both axes.
        mapped_attrs |= AP_ENTITY_PARAMS
        mapped_params |= AP_ENTITY_PARAMS
    return mapped_attrs, mapped_params


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


def _coverage(app_type, attributes: Mapping, statistics: Mapping, appliance) -> dict:
    """What the device exposes with no addhon entity (the gold signal).

    Attribute axis: only BARE keys (no dot) are considered; dotted ``settings.*``
    keys mirror command parameters and belong to the command-param axis instead. The
    unmapped set is partitioned so the maintainer reads pure signal first; nothing is
    dropped:
      * ``attributes_unmapped``            - mappable telemetry candidates (the gold);
      * ``attributes_unmapped_statistics`` - keys from the statistics container;
      * ``attributes_unmapped_meta``       - protocol envelope blobs (dict/list-valued)
                                             + scalar debug/protocol noise + program slots.
    The command-param axis is split the same way (``command_params_unmapped`` vs
    ``command_params_unmapped_meta``).
    """
    mapped_attrs, mapped_params = _mapped_sets(app_type)
    settings_params = _settings_param_names(appliance)

    # The device shadow exposes writable params ALSO as bare attribute keys (e.g. a
    # fridge reports `tempSelZ1` both bare and as `settings.tempSelZ1`). Those belong
    # to the command-param axis, not the attribute axis, so subtract every writable
    # settings-param name as well - otherwise controlled setpoints (number/climate/AC
    # switch) would be falsely listed as unmapped read-only attributes.
    bare = {k for k in attributes if isinstance(k, str) and "." not in k}
    # Read-only telemetry is the attribute axis: writable param mirrors live on the
    # command-param axis, so exclude them from BOTH the unmapped list and the total
    # (otherwise `total` overstates this axis' denominator).
    read_only_bare = bare - settings_params
    unmapped = read_only_bare - mapped_attrs
    stats_keys = set(statistics) if isinstance(statistics, Mapping) else set()

    # Partition: statistics carve-out first (unchanged contract), then meta/noise
    # (value-type envelope OR scalar denylist OR program slot), the rest is signal.
    unmapped_statistics = sorted(k for k in unmapped if k in stats_keys)
    rest = unmapped - set(unmapped_statistics)
    unmapped_meta = sorted(
        k
        for k in rest
        if isinstance(attributes.get(k), (Mapping, list)) or _is_meta_attr(k)
    )
    unmapped_signal = sorted(rest - set(unmapped_meta))

    params_unmapped = settings_params - mapped_params
    params_meta = sorted(k for k in params_unmapped if k.lower() in _COVERAGE_META_PARAMS)
    params_signal = sorted(params_unmapped - set(params_meta))

    # `attributes_total` is the telemetry-axis denominator: mapped telemetry + signal.
    # Exclude statistics and meta (like writable mirrors already are) so that
    # `len(attributes_unmapped) / attributes_total` reads as a real coverage gap and
    # not a figure inflated by protocol/debug blobs.
    attributes_total = len(read_only_bare) - len(unmapped_statistics) - len(unmapped_meta)

    return {
        "attributes_total": attributes_total,
        "attributes_unmapped": unmapped_signal,
        "attributes_unmapped_statistics": unmapped_statistics,
        "attributes_unmapped_meta": unmapped_meta,
        # Symmetric with attributes_total: exclude the meta params (plumbing) so the
        # param-axis denominator is mapped controls + signal, not inflated by category/
        # endpoints/rule flags.
        "command_params_total": len(settings_params) - len(params_meta),
        "command_params_unmapped": params_signal,
        "command_params_unmapped_meta": params_meta,
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


def _appliance_block(
    appliance_id: str, data: Mapping, entities: Mapping | None = None
) -> dict:
    """Build the (redacted) diagnostics block for a single appliance.

    `entities` is the registry-derived inventory for THIS appliance, already
    computed once for the whole dump. It defaults to None so the helper stays
    hass-less and pure, and so a caller with no registry still produces a block.
    """
    appliance = data.get("appliance")
    app_type = data.get("type")
    attributes = data.get("attributes")
    attributes = attributes if isinstance(attributes, Mapping) else {}
    statistics = data.get("statistics")
    statistics = statistics if isinstance(statistics, Mapping) else {}

    commands = _command_schema(appliance)
    coverage = _coverage(app_type, attributes, statistics, appliance)
    future = _future_capabilities(app_type, attributes, appliance)

    _LOGGER.debug(
        "Diagnostics debug: appliance id=%s name=%s type=%s attrs=%d commands=%d "
        "unmapped_attrs=%d unmapped_params=%d",
        redact_id(appliance_id),
        data.get("name"),
        app_type,
        len(attributes),
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
        "attributes": dict(attributes),
        "commands": commands,
        "coverage": coverage,
        # Next to coverage on purpose: one says what the code could map for this
        # type, the other what Home Assistant actually holds. Reading them together
        # is the whole point, and a disagreement between them IS the finding.
        "entities": dict(entities) if isinstance(entities, Mapping) else None,
        "future_capabilities": future,
    }
    return _redact(block)


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


def _last_error(hass: HomeAssistant, entry: ConfigEntry) -> dict | None:
    """The last classified setup/update error code, for issue triage.

    Static code+reason (no device identity), pulled from the client. None when no
    failure has been recorded (or the client is absent, e.g. a failed setup)."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    client = entry_data.get("client")
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
    # 2FA summary only when the failure is in the MFA band (160-169) -- challenge_kind is
    # the enum "email"/None and can_resend is a bool; the MfaContext secrets are NEVER here.
    mfa = getattr(client, "last_mfa_summary", None)
    if isinstance(mfa, dict) and 160 <= getattr(code, "code", 0) <= 169:
        out["mfa"] = {
            "challenge_kind": mfa.get("challenge_kind"),
            "can_resend": mfa.get("can_resend"),
        }
    return out


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict:
    """Return diagnostics for a config entry (all appliances)."""
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
                _appliance_block(appliance_id, data, inventory.get(appliance_id))
            )

    return {
        "entry": {
            "title": _redact_title(entry.title),
            "data": {
                "email": _redact_email(entry.data.get("email")),
                "password": _REDACTED,
            },
            "options": dict(entry.options),
        },
        "last_error": _last_error(hass, entry),
        # Entry-wide, next to last_error rather than inside an appliance: a failed
        # setup leaves no appliances at all, and this is exactly the dump where the
        # question "did anything get created" needs an answer. Closed-domain
        # primitives only (platform domains, counts, a status token), so like
        # last_error it is leak-proof by construction and skips _redact.
        "platforms": platforms,
        "appliances": appliances,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device
) -> dict:
    """Return diagnostics for a single device (the appliance behind it).

    ``device.identifiers`` is a set of ``(domain, id)`` tuples; base_entity.device_info
    registers ``{(DOMAIN, appliance_id)}``, so the appliance_id is recovered directly.
    The raw identifier (which may BE the serial) is never echoed into the output.
    """
    coordinator = _coordinator(hass, entry)
    coord_data = getattr(coordinator, "data", None)

    appliance_id = next(
        (
            ident[1]
            for ident in getattr(device, "identifiers", ()) or ()
            if isinstance(ident, tuple) and len(ident) == 2 and ident[0] == DOMAIN
        ),
        None,
    )
    if appliance_id is None or not isinstance(coord_data, Mapping):
        return {}
    data = coord_data.get(appliance_id)
    if not isinstance(data, Mapping):
        return {}
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
        "appliance": _appliance_block(
            appliance_id, data, inventory.get(appliance_id)
        )
    }
