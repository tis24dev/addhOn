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

Identity (id/serial/mac and credential-ish keys) is redacted. The device nickname
(`name`) is kept readable on purpose, to correlate the dump with the physical
appliance.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    APPLIANCE_AC,
    APPLIANCE_WASH_GROUP,
    DOMAIN,
    PROGRAM_PARAM_NAMES,
)
from .debug_utils import redact_id
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
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "value"):
        inner = value.value
        if inner is None or isinstance(inner, (str, int, float, bool)):
            return inner
        return str(inner)
    return str(value)


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
    """Full schema of one command parameter: value + enum + range + metadata."""
    schema: dict = {
        "value": _param_value(param),
        "typology": getattr(param, "typology", None),
        "category": getattr(param, "category", None),
        "mandatory": getattr(param, "mandatory", None),
    }
    enum = param_values(param)
    if enum:
        schema["enum"] = enum
    rng = param_range(param)
    if rng is not None:
        schema["min"], schema["max"], schema["step"] = rng
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
        from .binary_sensor import BINARY_SENSORS, _CONNECTIVITY, _UNIVERSAL_GATED
        from .number import NUMBERS
        from .sensor import SENSORS
        from .switch import _AC_SWITCHES
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
    if app_type == APPLIANCE_AC:
        for desc in _AC_SWITCHES:
            mapped_params.add(desc.param)
        mapped_params |= _AC_CLIMATE_PARAMS
    if app_type in APPLIANCE_WASH_GROUP:
        mapped_params.update(PROGRAM_PARAM_NAMES)
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


def _appliance_block(appliance_id: str, data: Mapping) -> dict:
    """Build the (redacted) diagnostics block for a single appliance."""
    appliance = data.get("appliance")
    app_type = data.get("type")
    attributes = data.get("attributes")
    attributes = attributes if isinstance(attributes, Mapping) else {}
    statistics = data.get("statistics")
    statistics = statistics if isinstance(statistics, Mapping) else {}

    commands = _command_schema(appliance)
    coverage = _coverage(app_type, attributes, statistics, appliance)

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
    }
    return _redact(block)


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

    appliances: list[dict] = []
    coord_data = getattr(coordinator, "data", None)
    if isinstance(coord_data, Mapping):
        for appliance_id, data in coord_data.items():
            if isinstance(data, Mapping):
                appliances.append(_appliance_block(appliance_id, data))

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
    return {"appliance": _appliance_block(appliance_id, data)}
