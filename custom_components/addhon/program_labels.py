# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Readable program names from the hOn translation catalog (issue #71).

THE PROBLEM. `command_loader._clean_name` reduces a startProgram category
`PROGRAMS.WM_WD.HQD_AUTOCLEAN` to the slug `hqd_autoclean`, and that slug is what both
the program select and the program-name sensor end up displaying. The slug IS the i18n
key the app resolves against a downloaded catalog -- so the label was never missing,
just never fetched.

WHY THE LOOKUP LIVES HERE (HA layer) AND NOT IN THE ENGINE. The code sent to the
appliance and the text shown to the user must not be the same string: `client/engine/`
keeps emitting stable machine slugs (which the command path depends on), and the
translation is applied only where a human reads it. This is also what the issue asks
for -- "the original program code must remain unchanged" -- and it keeps the engine
free of any HA/locale concern.

DESIGN NOTES:

* The catalog is fetched ONCE per config entry (see `async_load`) and parked on the
  coordinator, so entities do no I/O.
* Everything is best-effort. No network, a shape change, an unknown language: the
  lookup returns None and the caller keeps the raw code. A cosmetic feature must never
  degrade the ability to run a program.
* A favourite/downloaded program appears in the schema under the name the user gave it
  in the app ("Ariel Fresh Clean"), which is already readable and has no catalog key.
  Uppercasing it yields no match, the lookup returns None, and the name survives
  untouched -- exactly the required behaviour, with no special case.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .const import (
    APPLIANCE_AC,
    APPLIANCE_DW,
    APPLIANCE_FR,
    APPLIANCE_FRE,
    APPLIANCE_IH,
    APPLIANCE_KT,
    APPLIANCE_OV,
    APPLIANCE_REF,
    APPLIANCE_RVC,
    APPLIANCE_TD,
    APPLIANCE_WC,
    APPLIANCE_WD,
    APPLIANCE_WH,
    APPLIANCE_WM,
)

_LOGGER = logging.getLogger(__name__)

# appliance type -> catalog namespaces to try, in order.
#
# The mapping is NOT the identity: the washer and the washer-dryer share one namespace,
# `WM_WD` -- verified on a real WM (a plain washing machine reports
# `programName: "PROGRAMS.WM_WD.HQD_SMART"`, diagnostics/live-2026-06-22/device-WM.json).
# The fridge family gets two candidates because the catalog splits fridge and freezer
# programs across `REF` and `FRE`.
#
# A type that is absent here simply gets no translation; that is a missing row, not a
# bug, and `label` degrades to returning the raw code.
PROGRAM_NAMESPACES: dict[str, tuple[str, ...]] = {
    APPLIANCE_WM: ("WM_WD",),
    APPLIANCE_WD: ("WM_WD",),
    APPLIANCE_TD: ("TD",),
    APPLIANCE_DW: ("DW",),
    APPLIANCE_OV: ("OV",),
    APPLIANCE_REF: ("REF", "FRE"),
    APPLIANCE_FR: ("REF", "FRE"),
    APPLIANCE_FRE: ("FRE", "REF"),
    APPLIANCE_WC: ("WC",),
    APPLIANCE_WH: ("WH",),
    APPLIANCE_AC: ("AC",),
    APPLIANCE_IH: ("IH",),
    APPLIANCE_KT: ("KT",),
    APPLIANCE_RVC: ("RVC",),
}

# Attribute name under which the catalog is parked on the coordinator. Same convention
# as the other coordinator-level stores (see base_entity._coordinator_store).
COORDINATOR_ATTR = "hon_program_labels"

# HA storage for the catalog. ONE store for the whole integration, not one per config
# entry: the catalog is keyed by language and is identical for every account, so two
# entries would otherwise download and persist the same ~270 KB twice.
STORAGE_KEY = "addhon_program_labels"
STORAGE_VERSION = 1


class ProgramLabels:
    """Immutable `{namespace: {KEY: label}}` catalog with a slug -> label lookup."""

    def __init__(self, catalog: dict[str, dict[str, str]] | None = None) -> None:
        self._catalog = catalog or {}

    def __bool__(self) -> bool:
        return bool(self._catalog)

    @property
    def namespaces(self) -> tuple[str, ...]:
        return tuple(sorted(self._catalog))

    def label(self, appliance_type: str | None, code: str | None) -> str | None:
        """Readable name for `code` on `appliance_type`, or None if not translatable.

        None (not the code) is returned on every miss so the caller stays in charge of
        the fallback -- the select and the sensor both want to keep the raw code, but
        they must be able to tell "translated" from "not translated" for logging.
        """
        if not code or not self._catalog:
            return None
        namespaces = PROGRAM_NAMESPACES.get(str(appliance_type or "").upper())
        if not namespaces:
            return None
        # The engine lowercases the key when it strips the `PROGRAMS.<TYPE>.` prefix;
        # the catalog stores it uppercase. This is the exact inverse of
        # command_loader._clean_name.
        key = str(code).upper()
        for namespace in namespaces:
            label = self._catalog.get(namespace, {}).get(key)
            if label:
                return label
        return None

    def apply(self, appliance_type: str | None, programs: dict[str, str]) -> dict[str, str]:
        """Translate a `code -> label` map, leaving untranslatable entries as they are.

        Insertion order is preserved because the select builds its option list from it
        and a reshuffle would move the entries around in the UI on every restart.
        """
        return {
            code: (self.label(appliance_type, code) or current)
            for code, current in programs.items()
        }


# Shared empty catalog: entities can call `.label()` unconditionally when the fetch
# failed, without a None check at every call site.
EMPTY = ProgramLabels()

# Wall-clock budget for the whole fetch. This runs INSIDE async_setup_entry -- it has
# to, because the select freezes its option list in __init__ and a catalog arriving
# later would only show up after a reload -- so a slow CDN must not be able to stall the
# integration's startup. HA's shared session allows 300 s, far too long to sit in a
# setup for a cosmetic feature; on timeout we simply proceed with the raw codes.
FETCH_TIMEOUT = 30

# Budget when a usable cache is already in hand. The entities have everything they need,
# so the request is only a freshness check and the wait is pure setup latency: no entity
# is created until it returns. Kept short so a stalled CDN costs seconds, not the full
# first-run budget.
REFRESH_TIMEOUT = 8

# Slice of the budget the small app-config request may consume. It is ~1 KB against the
# catalog's ~7.6 MB, so without its own bound a slow gateway could eat the whole budget
# and leave the download a second to finish -- turning a recoverable slowdown into "no
# labels". Splitting the wait is what a single outer timeout cannot express.
CONFIG_TIMEOUT = 5


def for_coordinator(coordinator: Any) -> ProgramLabels:
    """The catalog parked on `coordinator`, or the empty one."""
    labels = getattr(coordinator, COORDINATOR_ATTR, None)
    return labels if isinstance(labels, ProgramLabels) else EMPTY


def _cached_catalog(cached: Any, code: str) -> dict | None:
    """The cached catalog for `code`, or None when absent/foreign/malformed."""
    if not isinstance(cached, dict) or cached.get("language") != code:
        return None
    catalog = cached.get("catalog")
    return catalog if isinstance(catalog, dict) and catalog else None


async def async_load(hass: Any, language: str | None = None) -> ProgramLabels:
    """Catalog for `language` (default: HA's configured language), cached across restarts.

    PERSISTENCE IS A CORRECTNESS REQUIREMENT, not an optimisation. The program select
    freezes its option list from these labels, and in HA the options ARE the state: if a
    restart could not reach the CDN the options would silently revert to raw codes, every
    automation calling `select.select_option` with a label would fail with
    ServiceValidationError, and the recorder history would split across two naming
    schemes. Caching keeps the option strings stable across a failed fetch.

    Freshness costs one small request, not the whole catalog: app-config (~1 KB) carries
    the catalog `version`, so an unchanged revision is confirmed without re-downloading
    ~7.6 MB. That also removes the download from the setup path in the common case.

    Never raises: any failure falls back to the cache, and to the empty catalog only when
    there is no cache -- a setup always completes and the entities show raw codes.
    """
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.storage import Store

    from .client.transport.translations import (
        async_app_config,
        async_fetch_catalog,
        catalog_url,
        catalog_version,
    )

    code = (language or getattr(hass.config, "language", None) or "en").split("-")[0]
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    try:
        cached = await store.async_load()
    except Exception as err:  # noqa: BLE001 - a corrupt store must not fail a setup
        _LOGGER.debug("ProgramLabels debug: cache unreadable (%s); ignoring", err)
        cached = None

    # A cache in hand turns this into a freshness check, so it gets the short budget.
    budget = REFRESH_TIMEOUT if _cached_catalog(cached, code) is not None else FETCH_TIMEOUT
    try:
        session = async_get_clientsession(hass)
        async with asyncio.timeout(budget):
            # Inner bound so the freshness check cannot starve the download that follows.
            async with asyncio.timeout(min(CONFIG_TIMEOUT, budget)):
                config = await async_app_config(session, code)
            version = catalog_version(config)
            known = _cached_catalog(cached, code)
            if known is not None and version is not None and cached.get("version") == version:
                _LOGGER.debug(
                    "ProgramLabels debug: cached catalog '%s' is current (version %s); "
                    "skipping the download",
                    code,
                    version,
                )
                return ProgramLabels(known)
            url = catalog_url(config)
            if not url:
                raise ValueError("app-config carries no payload.language.jsonPath")
            catalog = await async_fetch_catalog(session, url)
    except Exception as err:  # noqa: BLE001 - cosmetic feature, never fails a setup
        known = _cached_catalog(cached, code)
        if known is not None:
            # The load-bearing branch: the option list stays exactly what it was on the
            # previous run instead of reverting to raw codes under the user's automations.
            _LOGGER.debug(
                "ProgramLabels debug: refresh of catalog '%s' failed (%s: %s); "
                "keeping the cached one",
                code,
                type(err).__name__,
                err,
            )
            return ProgramLabels(known)
        # WARNING, not debug. This runs AFTER the coordinator's first refresh has already
        # talked to the hOn cloud successfully, so a failure here is anomalous rather than
        # the ordinary "user is offline" case -- and it fires once per config-entry setup,
        # never per poll. It is also the signal that was missing when a silent short-read
        # made this feature dead-on-arrival for every user while the suite stayed green:
        # a degradation nobody can observe is one nobody will ever report.
        _LOGGER.warning(
            "Program-name catalog '%s' unavailable (%s: %s); programs will show their "
            "raw hOn code (e.g. 'hqd_autoclean'). The integration is otherwise unaffected.",
            code,
            type(err).__name__,
            err,
        )
        return EMPTY

    # Persisted OUTSIDE the fetch guard. Inside it, an unwritable `.storage` would send a
    # SUCCESSFUL download down the failure path: the fresh catalog would be dropped for
    # the stale cache (or EMPTY), and the user would be told it is "unavailable" when it
    # had in fact just arrived. A catalog we hold is usable whether or not it persists.
    try:
        await store.async_save({"language": code, "version": version, "catalog": catalog})
    except Exception as err:  # noqa: BLE001 - a failed save must not drop a good catalog
        _LOGGER.debug(
            "ProgramLabels debug: could not persist catalog '%s' (%s: %s); "
            "using it for this run only",
            code,
            type(err).__name__,
            err,
        )
    labels = ProgramLabels(catalog)
    _LOGGER.debug(
        "ProgramLabels debug: catalog '%s' loaded, namespaces=%s",
        code,
        labels.namespaces,
    )
    return labels
