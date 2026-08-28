# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import NoReturn

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

try:
    # In real Home Assistant these symbols always exist. The import is tolerant
    # only for the test harness, which stubs homeassistant.core with the bare
    # minimum (shared sys.modules: the first stub wins, so it is more robust to
    # degrade here than to extend every stub).
    from homeassistant.core import ServiceCall, callback
except ImportError:  # pragma: no cover - only under the test stub
    ServiceCall = object  # type: ignore[assignment,misc]

    def callback(func):  # type: ignore[no-redef]
        return func
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ACCOUNT_DEVICE_SUFFIX,
    APPLIANCE_FR,
    APPLIANCE_FRE,
    APPLIANCE_HOB,
    APPLIANCE_IH,
    APPLIANCE_REF,
    APPLIANCE_TD,
    ATTR_LEVEL,
    CONF_ENABLE_DEBUG,
    CONF_ENABLE_EXPERIMENTAL,
    CONF_ENABLE_MQTT_DEBUG,
    DOMAIN,
    PLATFORMS,
    SCAN_INTERVAL,
    SERVICE_REFRESH,
    SERVICE_SET_LOG_LEVEL,
    SERVICE_SET_MQTT_LOG_LEVEL,
)
from .logging_utils import (
    MQTT_LOG_LEVELS,
    apply_integration_log_level,
    apply_mqtt_log_level,
    reset_integration_log_level,
    silence_mqtt_noise,
)
from .debug_utils import redact_id, redact_mac
from . import program_labels

_LOGGER = logging.getLogger(__name__)


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register (only once) the service for the MQTT log level.

    On the first registration it also applies the default silencing of the
    realtime MQTT noise. The service is global to the domain, not per-entry, so it
    is idempotent: if already present it does nothing.

    voluptuous and homeassistant.helpers.service are imported here (not at module
    level) so the import of __init__ does not depend on them: the test harness
    imports the package without always providing their stubs, while this function
    only runs in real HA.
    """
    mqtt_service_exists = hass.services.has_service(DOMAIN, SERVICE_SET_MQTT_LOG_LEVEL)
    log_service_exists = hass.services.has_service(DOMAIN, SERVICE_SET_LOG_LEVEL)
    refresh_service_exists = hass.services.has_service(DOMAIN, SERVICE_REFRESH)
    if mqtt_service_exists and log_service_exists and refresh_service_exists:
        return

    import voluptuous as vol

    from homeassistant.helpers.service import async_register_admin_service

    # First registration (HA start/restart): silence the noise by default.
    # On a reload of a single entry the service stays registered, so a debug level
    # possibly set at runtime is not re-silenced.
    if not mqtt_service_exists:
        silence_mqtt_noise()

    async def _handle_set_mqtt_log_level(call: ServiceCall) -> None:
        level_name = call.data[ATTR_LEVEL]
        apply_mqtt_log_level(MQTT_LOG_LEVELS[level_name])
        _LOGGER.info(
            "realtime MQTT log level set to %s", level_name.upper()
        )

    async def _handle_set_log_level(call: ServiceCall) -> None:
        level_name = call.data[ATTR_LEVEL]
        apply_integration_log_level(MQTT_LOG_LEVELS[level_name])
        _LOGGER.info(
            "Haier hOn diagnostic log level set to %s", level_name.upper()
        )

    async def _handle_refresh(call: ServiceCall) -> None:
        """Force an immediate cloud poll on every loaded config entry.

        Domain-wide equivalent of the per-device "Refresh now" button: it reads
        hass.data live at call time and asks each loaded coordinator for a
        debounced refresh (async_request_refresh, like the button). Per-entry
        failures are isolated (asyncio.gather(..., return_exceptions=True)) and
        logged at warning; the service NEVER re-raises to the caller, so one
        unhealthy account does not break an automation refreshing the others.
        """
        coordinators = [
            entry_data["coordinator"]
            for entry_data in hass.data.get(DOMAIN, {}).values()
            if isinstance(entry_data, dict) and entry_data.get("coordinator") is not None
        ]
        _LOGGER.debug(
            "Refresh service: requesting refresh on %d coordinator(s)",
            len(coordinators),
        )

        async def _refresh_one(coordinator) -> None:
            # Wrap the call INSIDE a coroutine so even a SYNCHRONOUS raise from
            # async_request_refresh (e.g. a future refactor storing a wrapper /
            # wrong-typed object) is captured by gather(return_exceptions=True)
            # instead of escaping the generator and aborting the other refreshes.
            await coordinator.async_request_refresh()

        results = await asyncio.gather(
            *(_refresh_one(coordinator) for coordinator in coordinators),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                _LOGGER.warning("Refresh service: a coordinator refresh failed: %s", result)

    level_schema = vol.Schema(
        {vol.Required(ATTR_LEVEL, default="debug"): vol.In(tuple(MQTT_LOG_LEVELS))}
    )

    # Admin-only. Both handlers end in logging.getLogger().setLevel(), which is
    # global to the Python process: not per config entry, not per user. Registered
    # plainly, any authenticated non-admin could turn integration-wide debug
    # logging on through the REST/WebSocket API. async_register_admin_service
    # resolves call.context.user_id and raises Unauthorized for non-admins; calls
    # with no user attached (automations, internal) keep working.
    if not mqtt_service_exists:
        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_SET_MQTT_LOG_LEVEL,
            _handle_set_mqtt_log_level,
            schema=level_schema,
        )

    if not log_service_exists:
        async_register_admin_service(
            hass,
            DOMAIN,
            SERVICE_SET_LOG_LEVEL,
            _handle_set_log_level,
            schema=level_schema,
        )

    if not refresh_service_exists:
        # No schema: the service takes no fields and no target (domain-wide).
        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH,
            _handle_refresh,
        )


@callback
def _apply_debug_options(entry: ConfigEntry, *, reset_when_off: bool = True) -> None:
    """Align the log levels to the two toggles persisted in entry.options.

    enable_debug=True  -> integration logger to DEBUG; False -> NOTSET
                          (they go back to inheriting the level configured in HA).
    enable_mqtt_debug=True -> realtime MQTT logger to DEBUG; False -> WARNING
                          (silenced).

    The MQTT level is applied AFTER the integration's one, so the explicit level
    of the MQTT child wins over the parent's cascade: enabling the integration's
    DEBUG does NOT turn the realtime noise back on if the MQTT toggle is off. NB
    the loggers are global to the process (see OptionsFlowHandler): with more than
    one entry (rare, multi-account) the levels are shared and changing the options
    of one entry re-applies them based on THAT entry, possibly resetting another
    one's active debug. The typical installation has a single account.

    reset_when_off=True (default, used by the options listener): an OFF toggle
    RESETS the level (NOTSET / WARNING), so disabling it from the UI takes effect
    immediately and also clears any manual override done with the set_log_level
    service. reset_when_off=False (used in async_setup_entry): an OFF toggle does
    NOT touch the loggers, so an integration DEBUG set at runtime via the services
    survives re-setups/retries (e.g. an unstable login) instead of being reset on
    every attempt; the default MQTT silencing on the first registration is still
    guaranteed by _async_register_services (which, however, on a reload of the only
    entry that removes and re-registers the services, also re-silences any MQTT
    level raised at runtime).
    """
    if entry.options.get(CONF_ENABLE_DEBUG, False):
        apply_integration_log_level(logging.DEBUG)
    elif reset_when_off:
        reset_integration_log_level()
    if entry.options.get(CONF_ENABLE_MQTT_DEBUG, False):
        apply_mqtt_log_level(logging.DEBUG)
    elif reset_when_off:
        silence_mqtt_noise()


_ENTRY_OPTS_KEY = "entry_options"


def _entry_opts(entry: ConfigEntry) -> tuple[bool, bool, bool]:
    """The options the update listener has to react to.

    (integration-debug, mqtt-debug, experimental). The first two are applied on the
    fly; the third decides which entities EXIST, so only it can require a reload.
    """
    return (
        entry.options.get(CONF_ENABLE_DEBUG, False),
        entry.options.get(CONF_ENABLE_MQTT_DEBUG, False),
        entry.options.get(CONF_ENABLE_EXPERIMENTAL, False),
    )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to an options change: log levels live, entity set by reload.

    A reload would tear down auth and the MQTT channel just to change a log level;
    the debug levels are therefore re-applied on the fly, as the existing services
    do. enable_experimental is different in kind: it adds or removes entities, which
    only a reload can do, so it is the one option that reloads the entry.

    HA fires update listeners on ANY entry change (data, options, title), not only
    on an options change. A data-only write -- e.g. _persist_refresh_token rotating
    the OAuth refresh token during a routine poll -- must NOT re-apply/reset the
    debug levels: that would silently kill a debug level raised at runtime via the
    set_log_level / set_mqtt_log_level service (reset_when_off=True), exactly when the
    logs are needed, and must certainly not reload. So each half acts only on the
    values it owns, and an experimental-only change leaves the loggers untouched.
    """
    current = _entry_opts(entry)
    hass_data = getattr(hass, "data", None)
    entry_data = (
        hass_data.get(DOMAIN, {}).get(entry.entry_id)
        if isinstance(hass_data, dict)
        else None
    )
    previous: tuple[bool, bool, bool] | None = None
    if entry_data is not None:
        previous = entry_data.get(_ENTRY_OPTS_KEY)
        if previous == current:
            return  # entry changed but none of these options did
        # Recorded before the reload below: a reload detaches this dict from
        # hass.data, so a write afterwards would land nowhere.
        entry_data[_ENTRY_OPTS_KEY] = current
    _LOGGER.debug(
        "Options debug: options updated entry=%s enable_debug=%s enable_mqtt_debug=%s "
        "enable_experimental=%s",
        entry.entry_id,
        current[0],
        current[1],
        current[2],
    )
    # No baseline (a first call, or an entry absent from hass.data) is not evidence
    # of a change: apply the levels as before, but never reload on a guess.
    if previous is None or previous[:2] != current[:2]:
        _apply_debug_options(entry)
    if previous is not None and previous[2] != current[2]:
        _LOGGER.info(
            "Options: experimental features %s, reloading the entry",
            "enabled" if current[2] else "disabled",
        )
        await hass.config_entries.async_reload(entry.entry_id)


def _redact_email(email: str | None) -> str | None:
    if not email:
        return None
    if "@" not in email:
        return "***"
    _, domain = email.split("@", 1)
    return f"***@{domain}"


def _redact_title(title: str | None) -> str | None:
    if not title or "@" not in title:
        return title
    prefix, domain_and_suffix = title.rsplit("@", 1)
    open_paren = prefix.rfind("(")
    safe_prefix = prefix[: open_paren + 1] if open_paren >= 0 else ""
    return f"{safe_prefix}***@{domain_and_suffix}"


async def _async_close_client(client) -> None:
    """Close HonClient without masking the original setup/unload error."""
    try:
        await client.async_close()
    except Exception as err:
        _LOGGER.warning("Error closing HonClient: %s", err)


def _store_setup_failure(hass: HomeAssistant, entry: ConfigEntry, client) -> None:
    """Leave behind WHY this setup failed, in place of the bucket it never filled.

    Until this existed the two failure branches of async_setup_entry popped the entry
    bucket whole, so a diagnostics download taken after a failed setup found no client
    to ask and reported `{"status": "client_absent"}` -- true, and useless: it is the
    same answer a dump gives while Home Assistant is still retrying, and it says
    nothing about the classified code, the phase that burned the time, or the
    appliance-list call. That is the state a reporter downloads a dump in most often,
    and the artefacts built to explain it (the per-phase ledger of #76, the fetch
    census) were unreachable in exactly that dump.

    The bucket is REPLACED, never merged: whatever platforms and the coordinator were
    handed must not survive a failed setup, and the record is the only key left. It is
    overwritten by the next attempt (async_setup_entry stores the live bucket as one
    literal) and removed by async_unload_entry / async_remove_entry, so the record
    describes the LAST attempt and only while the entry has no working session.

    Called BEFORE _async_close_client on both branches, because closing the client
    nulls the session `last_appliance_fetch` reads through -- the census would be gone
    by the time this ran after it. `last_setup_fetch` covers the other half: a failure
    raised INSIDE setup_sync has already closed the session there, and that path
    snapshots the census before it does (hon_client.py, the except of setup_sync).

    Every value stored here is one this integration already emits in the dump through
    `_last_error`: an ADDHON label, a phase token, the phase ledger, the census
    primitives. No new class of value reaches hass.data, and diagnostics still
    validates each one on the way out rather than trusting this record.

    The reads are GUARDED, and the guard is not symmetry with `diagnostics._last_fetch`
    -- it costs more here. `getattr(x, name, default)` swallows a MISSING attribute, not
    an exception raised inside a property body, and `_setup_failure_record` reads four
    HonClient properties that delegate to `_hon_instance` (`setup_drops` ends in
    `dict(self._setup_drops)` on a session that setup may still be appending to from the
    hOn loop thread -- `client/session.py:625-632`, and `_RaisingFetchClient` in the
    diagnostics tests exists for exactly that). Unguarded, one such raise leaves this
    helper propagating out of the `except` handler that called it, so
    `_async_close_client` never runs and the dedicated loop thread and the owned
    aiohttp session leak -- and in the CancelledError branch it would replace a
    cancellation with an unrelated error. A dump that cannot say why setup failed is a
    worse dump; a client nobody closed is a worse process.

    On that path the bucket is still cleared and left EMPTY rather than filled with a
    half-read record: `_last_error` then answers `client_absent`, which is exactly true
    (there is no client, and nothing could be read about why).
    """
    record: dict | None = None
    try:
        record = _setup_failure_record(client)
    except Exception:  # noqa: BLE001 - a diagnostics record must never block teardown
        _LOGGER.debug(
            "Setup debug: could not record the setup failure for entry=%s",
            entry.entry_id,
            exc_info=True,
        )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = (
        {"setup_failure": record} if record is not None else {}
    )
    _LOGGER.debug(
        "Setup debug: recorded setup failure for entry=%s code=%s phase=%s fetch=%s",
        entry.entry_id,
        (record or {}).get("code"),
        (record or {}).get("phase"),
        ((record or {}).get("fetch") or {}).get("outcome"),
    )


def _setup_failure_record(client) -> dict:
    """The record itself. Split from the guard above so that guard is the whole
    degradation story: four property reads that may raise, none of them allowed to
    keep a failed setup from closing its client."""
    code = getattr(client, "last_error_code", None)
    census = getattr(client, "last_appliance_fetch", None) or getattr(
        client, "last_setup_fetch", None
    )
    fetch: dict | None = None
    if isinstance(census, Mapping):
        # The four counters live on the client, not in the census: they are what SETUP
        # made of the list, while the census is what the CALL returned. Flattened into
        # one mapping here so the reader in diagnostics has a single source for the
        # block whether it is reading a live client or this record.
        fetch = {
            **dict(census),
            "expanded": getattr(client, "setup_expanded", None),
            "built": getattr(client, "appliance_count", None),
            "skipped": getattr(client, "setup_drops", None),
            "degraded": getattr(client, "degraded_census", None),
        }
    return {
        # The LABEL only, never the code object: diagnostics resolves the reason from
        # the catalog at read time, so the text in the dump comes from error_codes.py
        # in the running version and cannot be a string that rode in here.
        "code": getattr(code, "label", None),
        "phase": getattr(client, "last_error_phase", None),
        "phase_ledger": getattr(client, "last_phase_ledger", None) or None,
        "fetch": fetch,
        "at": datetime.now(timezone.utc),
    }


@callback
def _persist_refresh_token(hass: HomeAssistant, entry: ConfigEntry, hon_client) -> None:
    """Copy a rotated refresh token into entry.data, once, only on a real change.

    Single source of truth for the write rule, used by BOTH the initial setup and the
    coordinator update path, so a token rotated later (a runtime `auth.refresh()` or a
    background `_async_reauth()`) still reaches `entry.data` and survives a restart -- not
    just the first login. The non-empty AND changed guard reads `entry.data` live each
    call, so it never wipes a good token and never writes on an unchanged poll (no entry
    churn). HA-loop only (`@callback`). NEVER logs the token value."""
    new_token = hon_client.refresh_token
    stored = entry.data.get("refresh_token", "")
    if new_token and new_token != stored:
        _LOGGER.debug("Persisting rotated refresh token")
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "refresh_token": new_token}
        )


def _raise_setup_error(err: Exception) -> NoReturn:
    """Classify a SETUP failure and raise the matching HA exception.

    An auth error triggers the reauth flow (ConfigEntryAuthFailed); anything else is
    ConfigEntryNotReady so HA retries setup later. Extracted from async_setup_entry so
    the branch is unit-testable (a swapped branch would otherwise pass the suite). (#11)
    """
    from .error_codes import classify, error_detail
    from .hon_client import _requires_reauth

    code = classify(err)
    # error_detail() drops a leading "ADDHON-NNN: " so the code appears ONCE. These two
    # messages are shown by Home Assistant on the config-entry page, so the user really
    # did read the code twice before (#76).
    detail = error_detail(err)
    if _requires_reauth(err):
        raise ConfigEntryAuthFailed(f"[{code.label}] Invalid hOn credentials: {detail}") from err
    raise ConfigEntryNotReady(f"[{code.label}] Unable to connect to hOn: {detail}") from err


def _raise_update_error(err: Exception) -> NoReturn:
    """Classify a COORDINATOR update failure and raise the matching HA exception.

    An auth error triggers the reauth flow (ConfigEntryAuthFailed); anything else is a
    transient UpdateFailed (the coordinator keeps its last good snapshot and retries).
    Extracted for unit-testing (#11)."""
    from .error_codes import classify, error_detail
    from .hon_client import _requires_reauth

    code = classify(err)
    detail = error_detail(err)
    if _requires_reauth(err):
        raise ConfigEntryAuthFailed(f"[{code.label}] Invalid hOn credentials: {detail}") from err
    raise UpdateFailed(f"[{code.label}] hOn update error: {detail}") from err


# "Washer-only" sensors that were mistakenly created on the tumble dryers (TD)
# too: a tumble dryer does not use water and does not report loadingPercentage
# (the app gates that statistic to WM/WD), so they stayed forever "unknown"
# entities. After the per-type refactor they are no longer created: here we clean
# up the ones already registered, ONLY on TD devices.
_TD_REMOVED_SUFFIXES = (
    "_total_water",
    "_total_energy",
    "_current_energy",
    "_current_water",
    "_loading_percentage",
)

# The unique_id of an entity that belonged to a per-ZONE CLONE of an appliance.
# `HonAppliance._check_name_zone` builds a clone's id as "<base>_z<N>", and every
# entity's id is "<appliance_id>_<key>", so a clone's entity reads
# "<base>_z<N>_<key>".
#
# ANCHORED and greedy on purpose. Unanchored it would also match a key that merely
# CONTAINS the shape, and the hob's own tables are full of near misses:
# `pan_zone1`, `temp_zone1`, `power_zone1`, `hot_zone1` all carry "zone" followed
# by a digit and none of them is a clone. What distinguishes a clone is the
# underscore-z-digits-underscore run in the DEVICE half of the id, before the key
# begins. The greedy `.+` takes the LAST such run, which is the right one: a base
# id built from the import-name fallback ("ih_<modelId>") contains underscores of
# its own, and the zone suffix is always the final segment the engine appends.
_ZONE_ONLY_RE = re.compile(r"^(?P<base>.+)_z\d+_")


def _remove_legacy_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove from the registry the legacy entities no longer provided by the integration.

    - "Power" SWITCH (unique_id '<id>_power'), removed in the 2.3/2.4 refactor.
      Scoped to the switch domain on purpose: the legitimate WH `power` sensor
      (unique_id '<id>_power') and KT `current_power` sensor (unique_id
      '<id>_current_power') both end in '_power' and must NOT be purged.
    - Washer-only sensors on the tumble dryers (TD): '<td_id>_total_water',
      '_total_energy', '_current_energy', '_current_water', '_loading_percentage'.
      Removed ONLY on devices of type TD (cross-checked with the coordinator),
      never on WM/WD/AC.
    - The air purifier panel LIGHT (unique_id '<id>_panel_light'), replaced by a
      select with the same unique_id in a different domain. Scoped to the light
      domain for that reason: removing by unique_id alone would delete the
      replacement along with the entity it replaces.
    - The fridge program SELECT (unique_id '<id>_ref_program'), replaced in #93 by
      four independent mode switches, a My Zone select and one button per download
      preset. The only CONDITIONAL rule here: that select still ships on a fridge
      where none of those can be built, so the id must also be one this session
      really superseded (`_superseded_ref_program_ids`). Scoped to the select
      domain for the same reason the panel light is: nothing else may match.
    - The My Zone mode SENSOR (unique_id '<id>_my_zone_mode'), shipped in 5.20.0 and
      replaced in #93 by a writable select of the same name. Conditional too, and on
      a NARROWER predicate than the select above: the sensor steps aside only where
      the drawer select is really built, so a fridge with the mode switches and no
      drawer programs keeps its sensor. Scoped to the sensor domain, since the select
      that replaces it carries the same unique_id suffix.
    - Everything that belonged to a per-zone CLONE of an induction hob
      ('<base>_z<N>_<key>'), which the session no longer creates. Double-anchored:
      the id must have the clone shape AND its base must be a hob in the current
      snapshot, so a genuinely zoned appliance of another type keeps its entities.

    Without this cleanup there would be orphan 'unavailable' entities with the '?' badge.
    """
    from homeassistant.helpers import entity_registry as er

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get("coordinator")
    coord_data = getattr(coordinator, "data", None)
    td_ids = {
        appliance_id
        for appliance_id, device in (coord_data or {}).items()
        if isinstance(device, dict) and device.get("type") == APPLIANCE_TD
    }
    td_orphans = {
        f"{appliance_id}{suffix}"
        for appliance_id in td_ids
        for suffix in _TD_REMOVED_SUFFIXES
    }
    hob_ids = _hob_ids(coord_data)
    superseded_refs = _superseded_ref_program_ids(coord_data)
    superseded_my_zones = _superseded_my_zone_ids(coord_data)

    registry = er.async_get(hass)
    checked = 0
    removed = 0
    # Counted apart from `removed` because this is the only rule whose removal BREAKS
    # something a user may still be calling; see the repair below.
    removed_ref_programs = 0
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        checked += 1
        unique_id = reg_entry.unique_id or ""
        domain = (reg_entry.entity_id or "").split(".", 1)[0]
        if domain == "switch" and unique_id.endswith("_power"):
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            _LOGGER.info("Removed legacy power switch: id=%s", redact_id(reg_entry.unique_id))
        elif domain == "light" and unique_id.endswith("_panel_light"):
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            _LOGGER.info(
                "Removed legacy purifier panel light: id=%s",
                redact_id(reg_entry.unique_id),
            )
        elif unique_id in td_orphans:
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            _LOGGER.info(
                "Removed invalid consumption entity for tumble dryer: id=%s",
                redact_id(reg_entry.unique_id),
            )
        elif (
            domain == "select"
            and unique_id.endswith("_ref_program")
            and unique_id.removesuffix("_ref_program") in superseded_refs
        ):
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            removed_ref_programs += 1
            _LOGGER.info(
                "Removed the fridge program select replaced by the per-mode controls: "
                "id=%s",
                redact_id(reg_entry.unique_id),
            )
        elif (
            domain == "sensor"
            and unique_id.endswith("_my_zone_mode")
            and unique_id.removesuffix("_my_zone_mode") in superseded_my_zones
        ):
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            _LOGGER.info(
                "Removed the My Zone mode sensor replaced by the writable select: "
                "id=%s",
                redact_id(reg_entry.unique_id),
            )
        elif _is_hob_zone_clone(unique_id, hob_ids):
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            _LOGGER.info(
                "Removed duplicate per-zone entity of an induction hob: id=%s",
                redact_id(reg_entry.unique_id),
            )
    if removed_ref_programs:
        _raise_ref_program_repair(hass, entry)
    _LOGGER.debug(
        "Setup debug: legacy cleanup completed for entry=%s, checked=%d, removed=%d",
        entry.entry_id,
        checked,
        removed,
    )
    _remove_zone_clone_devices(hass, entry, coord_data, hob_ids)


def _raise_ref_program_repair(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Tell the user their fridge program select is gone, and what replaced it.

    Raised ONLY when this run actually removed one, so it fires once and never on a
    fresh install. The purge above is idempotent, so a later start removes nothing and
    says nothing more.

    A repair rather than a log line, because the way this breaks is silent: Home
    Assistant answers `select.select_option` on an entity that no longer exists with a
    WARNING in the log and a SUCCESSFUL service call, so an automation that used to set
    the fridge to Holiday keeps reporting as run and does nothing. Nothing in the UI
    would say so.

    `is_fixable=False`: there is no button we could offer. The old option maps to three
    different kinds of entity depending on which one it was -- a switch, the My Zone
    select, or a preset button -- and only the person who wrote the automation knows
    which they meant. The repair carries no appliance id or name: it is one notice per
    config entry, and the entities it names are the generic keys, never this user's.
    """
    try:
        from homeassistant.helpers import issue_registry as ir

        ir.async_create_issue(
            hass,
            DOMAIN,
            "ref_program_select_replaced",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="ref_program_select_replaced",
        )
    except Exception:  # noqa: BLE001 - a notice must never cost a setup
        _LOGGER.debug("Setup debug: could not raise the ref_program repair", exc_info=True)


def _superseded_my_zone_ids(coord_data) -> set[str]:
    """Appliance ids whose My Zone mode SENSOR was replaced by the writable select (#93).

    A sibling of `_superseded_ref_program_ids`, and deliberately NOT the same predicate.
    `has_replacement_controls` is true as soon as the flag switches can be built, but the
    read-only sensor steps aside only where the DRAWER select really appears -- so a
    fridge with the four switches and no drawer programs keeps its sensor, and reusing
    the broader predicate here would delete a live entity on the next start.

    The condition is `my_zone_codes`, which is exactly what `sensor.async_setup_entry`
    consults to skip creating it. One question, answered once, for the same reason the
    program select's gate was folded into that function.
    """
    from .ref_programs import my_zone_codes

    superseded: set[str] = set()
    for appliance_id, device in (coord_data or {}).items():
        if not isinstance(device, dict):
            continue
        if device.get("type") not in (APPLIANCE_REF, APPLIANCE_FR, APPLIANCE_FRE):
            continue
        try:
            if my_zone_codes(device.get("appliance")):
                superseded.add(appliance_id)
        except Exception:  # noqa: BLE001 - a degraded schema must not cost a setup
            _LOGGER.debug(
                "Setup debug: My Zone supersession check failed for id=%s",
                redact_id(appliance_id),
                exc_info=True,
            )
    return superseded


def _superseded_ref_program_ids(coord_data) -> set[str]:
    """Appliance ids whose fridge program select was REPLACED, not merely dropped (#93).

    The fifth purge rule, and the first conditional one. The four above are
    unconditional -- that entity is not created any more, full stop -- while
    `select.ref_program` still ships on a fridge where no per-mode control can be built
    (one offering `iot_*` download presets alone). So the anchor is not "this is a
    fridge", it is `ref_programs.has_replacement_controls`: the SAME predicate
    `HonRefProgramSelect.supports_appliance` consults to step aside. Anything else would
    be a second opinion about which appliances lost the entity, and the two could differ.

    Double-anchored like the tumble-dryer and hob purges: the id must be a cooling
    appliance IN THE CURRENT SNAPSHOT and its live schema must really offer the
    replacement. An empty or degraded snapshot yields an empty set and removes nothing --
    the purge is idempotent and runs on every setup, so a bad start postpones it rather
    than guessing.
    """
    from .ref_programs import has_replacement_controls

    superseded: set[str] = set()
    for appliance_id, device in (coord_data or {}).items():
        if not isinstance(device, dict):
            continue
        if device.get("type") not in (APPLIANCE_REF, APPLIANCE_FR, APPLIANCE_FRE):
            continue
        try:
            if has_replacement_controls(device.get("appliance")):
                superseded.add(appliance_id)
        except Exception:  # noqa: BLE001 - a degraded schema must not cost a setup
            _LOGGER.debug(
                "Setup debug: supersession check failed for id=%s",
                redact_id(appliance_id),
                exc_info=True,
            )
    return superseded


def _hob_ids(coord_data) -> set[str]:
    """Appliance ids of the induction hobs in the current snapshot.

    The second anchor of the zone-clone purge. Matching the '<base>_z<N>_' shape
    alone would also delete the entities of an appliance whose zones are still
    real -- a twin-cavity oven, say -- and those are not duplicates of anything.
    Cross-checking against the snapshot's TYPE is the pattern the tumble-dryer
    purge above already uses.

    An empty snapshot yields an empty set and therefore removes nothing. That is
    the safe direction: the purge is idempotent and runs on every setup, so a
    degraded start postpones it instead of guessing.
    """
    return {
        appliance_id
        for appliance_id, device in (coord_data or {}).items()
        if isinstance(device, dict) and device.get("type") in (APPLIANCE_IH, APPLIANCE_HOB)
    }


def _is_hob_zone_clone(unique_id: str, hob_ids: set[str]) -> bool:
    """True for an entity that belonged to a zone clone of a hob in `hob_ids`."""
    match = _ZONE_ONLY_RE.match(unique_id or "")
    return bool(match) and match.group("base") in hob_ids


def _remove_zone_clone_devices(hass: HomeAssistant, entry: ConfigEntry, coord_data, hob_ids) -> None:
    """Detach the now-empty '<base>_z<N>' devices of a hob from this config entry.

    Removing an entity does NOT remove its device: the row stays attached to the
    config entry and keeps showing in the UI, now empty -- which is a worse result
    than the duplicate it replaced, since an empty device looks like a broken one.

    `remove_config_entry_id` rather than `async_remove_device`: it detaches only OUR
    entry and lets Home Assistant drop the device once nothing else references it,
    so a device an unrelated integration also claims is left alone.

    The list is MATERIALISED before the loop. `async_entries_for_config_entry`
    returns a live view over the registry, and detaching a device mutates exactly
    what is being iterated.
    """
    from homeassistant.helpers import device_registry as dr

    if not hob_ids:
        return
    live_ids = set(coord_data or {})
    dev_reg = dr.async_get(hass)
    for device in list(dr.async_entries_for_config_entry(dev_reg, entry.entry_id)):
        ours = {ident for domain, ident in device.identifiers if domain == DOMAIN}
        # `<base>_z<N>` with no trailing key: the DEVICE id, not an entity id, so
        # the shared regex needs the separator the entity form always carries.
        if not any(
            _is_hob_zone_clone(f"{ident}_", hob_ids) and ident not in live_ids
            for ident in ours
        ):
            continue
        dev_reg.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
        _LOGGER.info(
            "Removed duplicate per-zone device of an induction hob: id=%s",
            redact_id(sorted(ours)[0]),
        )


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device
) -> bool:
    """Let the user delete a device this entry no longer provides.

    Home Assistant shows the "Delete" button on a device card only when the
    integration implements this hook, and until now it did not: a device left over
    from an older layout could not be removed by hand at all. The automatic purge
    above covers the hob clones, so this is the safety net for anything it misses
    -- a clone whose base has since left the account, for instance, which no
    snapshot can identify as a hob any more.

    Returns True only for a device NOT in the current snapshot: allowing a live
    device to be deleted would let a user remove an appliance the next poll
    recreates, which reads as the delete button not working.

    The ACCOUNT's synthetic "diagnostics" device counts as live even though it is
    not an appliance and so never appears in the snapshot. It is the one device
    every entry of every account owns, it carries the debug toggles and the
    refresh button, and setup recreates it on the next reload -- so offering to
    delete it is offering to break the entry's own controls. Without this it was
    the ONLY device this hook answered True for on a healthy single-appliance
    account, which is the opposite of what the paragraph above promises.
    """
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_data.get("coordinator")
    coord_data = getattr(coordinator, "data", None)
    if not isinstance(coord_data, Mapping):
        # No snapshot means no way to tell a live device from a stale one, and
        # answering True there would offer to delete every device of the entry.
        return False
    # Built from `entry` and not from the snapshot: the account device is ours by
    # construction, whatever the poll returned. `base_entity.account_device_info`
    # builds the same identifier from the same constant.
    live_ids = set(coord_data) | {f"{entry.entry_id}{ACCOUNT_DEVICE_SUFFIX}"}
    ours = {ident for domain, ident in device.identifiers if domain == DOMAIN}
    return bool(ours) and not (ours & live_ids)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Haier hOn integration from a Config Entry."""
    from .hon_client import HonClient

    # Silence by default the noise of the realtime MQTT attempts and register
    # the debug service. Done BEFORE the client setup so the logger is already at
    # WARNING when the MQTT client starts to (re)connect.
    _async_register_services(hass)

    # Apply the persisted debug toggles RIGHT AWAY, but AFTER _async_register_services
    # (which on the first registration silences the MQTT noise by default) so the
    # persisted MQTT toggle, if active, wins over that silencing. Applying them here
    # and not at the end of setup makes the DEBUG level cover the setup path too
    # (login, discovery, first refresh): that is exactly what one wants to trace when
    # enabling debug for discovery problems. reset_when_off=False: an OFF toggle must
    # not reset a DEBUG set at runtime via the services, which must survive the
    # retries of a failing setup (the default MQTT silencing stays guaranteed by
    # _async_register_services).
    _apply_debug_options(entry, reset_when_off=False)

    # Current entries store "email"; tolerate and migrate older/corrupt entries that
    # still carry the old "username" key so setup can recover without a reinstall.
    email = entry.data.get("email") or entry.data.get("username")
    password = entry.data.get("password")
    # Persisted refresh token (added for 2FA): runtime refreshes instead of doing a
    # full login, so an account with email-OTP is not re-challenged on every restart.
    # "" on legacy entries / non-2FA accounts -> a normal login as before.
    refresh_token = entry.data.get("refresh_token", "")

    _LOGGER.debug(
        "Setup debug: starting setup entry=%s title=%s email=%s platforms=%s scan_interval=%ss",
        entry.entry_id,
        _redact_title(getattr(entry, "title", None)),
        _redact_email(email),
        PLATFORMS,
        SCAN_INTERVAL,
    )

    if not email:
        _LOGGER.error(
            "Missing credentials in the config entry ('email' key absent). "
            "Remove and reconfigure the integration."
        )
        return False
    if "email" not in entry.data and entry.data.get("username"):
        # Drop the legacy "username" key in the same update so the migrated entry
        # data carries only "email" (no stale key left for diagnostics/iteration).
        migrated = {k: v for k, v in entry.data.items() if k != "username"}
        migrated["email"] = email
        hass.config_entries.async_update_entry(entry, data=migrated)

    hon_client = HonClient(email=email, password=password, refresh_token=refresh_token)

    # Initial client setup in executor (does not block HA's event loop)
    try:
        _LOGGER.debug("Setup debug: running HonClient.setup_sync in executor")
        await hass.async_add_executor_job(hon_client.setup_sync)
        _LOGGER.debug("Setup debug: HonClient.setup_sync completed")
    except asyncio.CancelledError:
        await _async_close_client(hon_client)
        raise
    except Exception as err:
        # A background setup cannot prompt for a 2FA code: an MFA challenge (carried
        # MFA_REQUIRED -> requires_reauth) is routed by _raise_setup_error to
        # ConfigEntryAuthFailed -> the reauth flow, which CAN prompt for the OTP.
        _LOGGER.error("Unable to connect to hOn: %s", err)
        await _async_close_client(hon_client)
        _raise_setup_error(err)

    # Persist a rotated refresh token so the next restart keeps skipping the full login
    # (and the 2FA prompt). Single helper, change-guarded (see _persist_refresh_token).
    _persist_refresh_token(hass, entry, hon_client)

    async def async_update_data() -> dict:
        """Fetch the updated data from all the hOn devices."""
        try:
            _LOGGER.debug("Coordinator debug: starting hOn data update")
            data = await hon_client.async_get_appliances_data()
            # A runtime token refresh / background re-auth may have rotated the refresh
            # token during this fetch; persist it (only on a real change) so it survives a
            # restart -- not just the initial setup.
            _persist_refresh_token(hass, entry, hon_client)
            summary = [
                {
                    "id": redact_mac(appliance_id),
                    "name": redact_id(appliance_data.get("name")),
                    "type": appliance_data.get("type"),
                    "mac": redact_mac(appliance_data.get("mac")),
                    "attributes": len(appliance_data.get("attributes", {}))
                    if isinstance(appliance_data.get("attributes"), dict)
                    else 0,
                    "settings": len(appliance_data.get("settings", {}))
                    if isinstance(appliance_data.get("settings"), dict)
                    else 0,
                }
                for appliance_id, appliance_data in data.items()
            ]
            _LOGGER.debug(
                "Coordinator debug: hOn data update completed, devices=%d summary=%s",
                len(data),
                summary,
            )
            return data
        except Exception as err:
            from .error_codes import classify

            hon_client.last_error_code = classify(err)
            # Attribute the phase to THIS update failure (a carried HonCodedError.phase, or
            # the live auth phase if a re-auth was in flight) so diagnostics never shows a
            # phase/mfa-summary left over from the last login event.
            hon_client.last_error_phase = (
                getattr(err, "phase", None)
                or getattr(hon_client._hon_instance, "auth_phase", "")
                or None
            )
            hon_client.last_mfa_summary = None
            _LOGGER.debug(
                "Coordinator debug: hOn data update failed [%s]: %s",
                hon_client.last_error_code.label,
                err,
                exc_info=True,
            )
            _raise_update_error(err)

    stored = False
    try:
        coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Haier hOn data",
            update_method=async_update_data,
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )

        # First fetch
        _LOGGER.debug("Setup debug: first coordinator refresh at startup")
        await coordinator.async_config_entry_first_refresh()
        _LOGGER.debug(
            "Setup debug: first refresh completed, last_update_success=%s devices=%d",
            getattr(coordinator, "last_update_success", None),
            len(coordinator.data) if isinstance(coordinator.data, dict) else 0,
        )
        coordinator.hon_client = hon_client

        # Program-label catalog (#71). The appliance schema names a program with its
        # i18n KEY (`PROGRAMS.WM_WD.HQD_AUTOCLEAN` -> slug `hqd_autoclean`), so readable
        # names only exist in the catalog the hOn app downloads. Fetched ONCE here and
        # parked on the coordinator, so no entity ever does I/O for a label. Best-effort
        # by construction: async_load absorbs every failure and returns an empty catalog,
        # in which case the entities keep showing the raw code.
        setattr(
            coordinator,
            program_labels.COORDINATOR_ATTR,
            await program_labels.async_load(hass),
        )

        # Realtime: wire MQTT pushes to the coordinator (#4). Without this the push
        # channel was inert and entities only refreshed on the 60s poll. The push
        # arrives on the awscrt thread, so the snapshot is built THERE (a coherent
        # intra-thread read of the just-mutated appliances) and the publish is hopped
        # onto the HA event loop via call_soon_threadsafe; async_set_updated_data
        # publishes WITHOUT triggering a new poll (the 60s poll stays as a
        # reconciliation safety-net). Detached on unload.
        @callback
        def _publish_realtime(snapshot: dict) -> None:
            if snapshot:
                coordinator.async_set_updated_data(snapshot)

        def _on_realtime_push(_arg) -> None:
            # Runs on the awscrt thread; must never let an exception reach it.
            try:
                snapshot = hon_client.build_realtime_snapshot()
                hass.loop.call_soon_threadsafe(_publish_realtime, snapshot)
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.debug("Setup debug: realtime push handling failed: %s", err)

        try:
            hon_client.subscribe_updates(_on_realtime_push)
            entry.async_on_unload(lambda: hon_client.subscribe_updates(None))
            _LOGGER.debug("Setup debug: realtime MQTT push wired to coordinator")
        except Exception as err:  # pragma: no cover - realtime is best-effort
            _LOGGER.warning("Setup debug: could not wire realtime MQTT push: %s", err)

        # Integration version, for the diagnostics device's sw_version ("Firmware:"
        # row on the device card). Lazy import so the test stubs that import this
        # package do not need to stub homeassistant.loader; tolerant if unavailable.
        integration_version: str | None = None
        try:
            from homeassistant.loader import async_get_integration

            integration = await async_get_integration(hass, DOMAIN)
            integration_version = str(integration.version)
        except Exception as err:  # pragma: no cover - non-critical, cosmetic only
            _LOGGER.debug("Setup debug: could not resolve integration version: %s", err)

        # FIX: store both the coordinator and the client in the structure expected by all platforms
        hass.data.setdefault(DOMAIN, {})
        hass.data[DOMAIN][entry.entry_id] = {
            "coordinator": coordinator,
            "client": hon_client,
            "integration_version": integration_version,
            # Baseline for _async_options_updated: the options already in effect at
            # the start of setup, so a later data-only entry write (token rotation) is
            # a no-op and only a real options change re-applies the levels or reloads.
            _ENTRY_OPTS_KEY: _entry_opts(entry),
        }
        stored = True
        _LOGGER.debug("Setup debug: coordinator and client stored in hass.data for entry=%s", entry.entry_id)

        # Legacy entity cleanup (e.g. the removed "Power" switch): it must never
        # block the setup, so we absorb any registry errors.
        try:
            _remove_legacy_entities(hass, entry)
        except Exception as err:
            _LOGGER.debug("Legacy entity cleanup failed: %s", err)

        _LOGGER.debug("Setup debug: forwarding platforms %s", PLATFORMS)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        _LOGGER.debug("Setup debug: platform forwarding completed")
    except asyncio.CancelledError:
        if stored:
            unload_platforms = getattr(hass.config_entries, "async_unload_platforms", None)
            if callable(unload_platforms):
                try:
                    await unload_platforms(entry, PLATFORMS)
                except Exception as err:
                    _LOGGER.warning("Error unloading platforms after cancelled setup: %s", err)
        _store_setup_failure(hass, entry, hon_client)
        await _async_close_client(hon_client)
        raise
    except Exception:
        if stored:
            unload_platforms = getattr(hass.config_entries, "async_unload_platforms", None)
            if callable(unload_platforms):
                try:
                    await unload_platforms(entry, PLATFORMS)
                except Exception as err:
                    _LOGGER.warning("Error unloading platforms after failed setup: %s", err)
        _store_setup_failure(hass, entry, hon_client)
        await _async_close_client(hon_client)
        raise

    # Setup succeeded: register a listener that re-applies the debug toggles on the
    # fly when they change (async_on_unload removes the listener when the entry is
    # unloaded, without a reload). The levels have already been applied at the start
    # of setup; here it only remains to hook up the on-the-fly update.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry when the integration is disabled."""
    _LOGGER.debug("Unload debug: unloading entry=%s platforms=%s", entry.entry_id, PLATFORMS)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    _LOGGER.debug("Unload debug: async_unload_platforms result=%s", unload_ok)
    if unload_ok:
        entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
        client = entry_data.get("client")
        if client is not None:
            _LOGGER.debug("Unload debug: closing HonClient for entry=%s", entry.entry_id)
            await _async_close_client(client)
        else:
            _LOGGER.debug("Unload debug: no HonClient to close for entry=%s", entry.entry_id)
        # Last entry removed: remove the global debug services.
        if not hass.data.get(DOMAIN):
            for service in (SERVICE_SET_MQTT_LOG_LEVEL, SERVICE_SET_LOG_LEVEL, SERVICE_REFRESH):
                if hass.services.has_service(DOMAIN, service):
                    hass.services.async_remove(DOMAIN, service)
                    _LOGGER.debug("Unload debug: removed service %s", service)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the setup-failure record when the entry itself is deleted.

    The only hook that can. An entry whose setup never succeeded is never unloaded --
    Home Assistant calls async_unload_entry for LOADED entries -- so without this the
    record written by _store_setup_failure would outlive the entry it describes for the
    rest of the process, and `hass.data[DOMAIN]` would stay non-empty, which is what
    async_unload_entry reads to decide whether the last entry just went away and the
    global debug services can go with it.

    No client to close here: a failed setup left none, and a loaded entry has already
    been through async_unload_entry by the time Home Assistant removes it.
    """
    removed = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    _LOGGER.debug(
        "Remove debug: entry=%s dropped_record=%s",
        entry.entry_id,
        removed is not None,
    )
