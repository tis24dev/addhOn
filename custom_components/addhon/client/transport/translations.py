# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Anonymous fetch of the hOn program-label catalog (issue #71).

WHY THIS EXISTS. A startProgram category is named `PROGRAMS.<TYPE>.<PROGRAM>` -- that
is an i18n KEY, not a label. The command schema carries no readable name anywhere, so a
client that only reads `commands/v1/retrieve` can never show more than the raw slug
(`hqd_autoclean`, `iot_wash_bed_linen`). The hOn app resolves those keys against a
translation catalog it downloads separately; this module reproduces exactly that.

THE CHAIN (derived from the hOn 2.27.9 APK, then confirmed on the wire 2026-08-02 --
`apk/probe_translations_live.py` carries the decompiler offsets and the full response
matrix):

  1. GET `<API_URL>/config/app-config?languageCode=..&beta=..&appVersion=..&os=android`
     with `x-api-key: <CONFIG_API_KEY>`. NO session token: the app calls this with
     `useAuth=false`, and the endpoint indeed answers unauthenticated.
  2. Read `payload.language.jsonPath` -- a CDN URL of the form
     `https://assets.he-cdn.services/languages/language-<lang>-<version>.json`.
  3. GET that URL. The CDN sits behind Cloudflare and answers `error code: 1010` to any
     request without a User-Agent, so one must be sent (the api-key is NOT wanted here).

TWO NON-OBVIOUS CONSTRAINTS, both load-bearing:

  * PATH. `/config/app-config` has NO version segment. The app builds this endpoint with
    a 2-argument join (base + microservice) while its generic `endpoint()` helper adds
    `v1` -- and `/config/v1/app-config` is rejected with 403. Getting this wrong looks
    like an auth failure, which is why it is spelled out here.
  * API KEY. The bare AWS api-key gives 403; only the `-configuration`-suffixed form is
    accepted (see values.CONFIG_API_KEY).

MEMORY. The catalog is ~7.6 MB of JSON but the `PROGRAMS` subtree we need is ~270 KB. We
therefore never build the whole document as a Python dict: `_slice_programs` locates the
key and hands only that one object to the JSON decoder (see its docstring).

Errors are the caller's to decide about: every failure raises, and the HA layer treats
the catalog as best-effort (no catalog -> entities keep showing the raw code).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .values import (
    API_URL,
    APP_VERSION,
    CONFIG_API_KEY,
    CONFIG_MICROSERVICE,
    CONTENT_TYPE,
    OS,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

# Top-level branch of the catalog holding every program label, keyed by appliance-type
# namespace (`WM_WD`, `TD`, `DW`, ...). The rest of the 7.6 MB document is UI copy we
# have no use for.
PROGRAMS_ROOT = "PROGRAMS"

# Read cap for the catalog download. The largest observed catalog is ~7.6 MB (en), so
# 16 MB is over 2x headroom while keeping the WORST case survivable: decoding holds the
# bytes and the resulting str at once, and measuring the real catalog puts the peak at
# roughly 8x the body (53 MB for 7.6 MB), not the 2x one might assume. At a 32 MB cap
# that worst case approaches 250 MB, which a Raspberry Pi running HA does not have.
_MAX_CATALOG_BYTES = 16 * 1024 * 1024

# Chunk size for the accumulating read (see async_fetch_catalog).
_CHUNK_BYTES = 64 * 1024

_APP_CONFIG_URL = f"{API_URL}/{CONFIG_MICROSERVICE}/app-config"


_WHITESPACE = " \t\r\n"


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index] in _WHITESPACE:
        index += 1
    return index


def _slice_programs(text: str) -> dict[str, Any]:
    """Decode ONLY the top-level `PROGRAMS` object out of a full catalog document.

    `json.loads` on the whole 7.6 MB document materialises every UI string in the app,
    to then throw ~97% of it away. Instead we walk the top-level object member by
    member with `JSONDecoder.raw_decode` and keep just the one we need: each other
    member is decoded and immediately dropped, so the peak stays at the largest single
    member. Measured on the real en catalog: 45 ms / 1.3 MB peak, against 207 ms /
    17.1 MB for `json.loads` on the whole document.

    Walking the members is not merely an optimisation, it is what makes the extraction
    CORRECT. Searching for the `"PROGRAMS":` literal does not work: the real catalog
    contains 22 occurrences of it, several of which are nested objects belonging to
    other screens (`{"SCENARIOS": ...}` at offset 16792 is the first one). Only a
    member of the ROOT object is the catalog we want, and depth is exactly what a
    substring search cannot see.

    `raw_decode` (rather than hand-rolled brace matching) applies the real JSON grammar,
    so braces and quotes inside string values cannot desynchronise the walk.
    """
    decoder = json.JSONDecoder()
    index = _skip_whitespace(text, 0)
    if index >= len(text) or text[index] != "{":
        raise ValueError("catalog is not a JSON object")
    index += 1
    while True:
        index = _skip_whitespace(text, index)
        if index >= len(text) or text[index] == "}":
            break
        if text[index] == ",":
            index += 1
            continue
        key, index = decoder.raw_decode(text, index)
        index = _skip_whitespace(text, index)
        if index >= len(text) or text[index] != ":":
            raise ValueError("malformed catalog: missing ':' after a root key")
        index = _skip_whitespace(text, index + 1)
        value, index = decoder.raw_decode(text, index)
        if key == PROGRAMS_ROOT:
            if not isinstance(value, dict) or not value:
                raise ValueError(f"catalog '{PROGRAMS_ROOT}' is not a non-empty object")
            return value
    raise ValueError(f"catalog has no root '{PROGRAMS_ROOT}' member")


def _program_namespaces(programs: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Keep only the `<NAMESPACE> -> {KEY: label}` branches.

    `PROGRAMS` mixes real per-appliance-type namespaces (dict values: `WM_WD`, `TD`, ...)
    with loose string leaves (`WM_WD_PROGRAM_IOT_WASH_NAME_WOOL: "Wool"`) that belong to
    other screens. Only the dict branches are addressable as `PROGRAMS.<TYPE>.<KEY>`, so
    the leaves are dropped here and never reach the label lookup.
    """
    out: dict[str, dict[str, str]] = {}
    for namespace, entries in programs.items():
        if not isinstance(entries, dict):
            continue
        labels = {
            str(key): value for key, value in entries.items() if isinstance(value, str)
        }
        if labels:
            out[str(namespace)] = labels
    return out


async def async_app_config(session: Any, language: str) -> dict[str, Any]:
    """The anonymous app-config document for `language`.

    `session` is any aiohttp-style client session; in HA this is the shared
    `async_get_clientsession(hass)`, so no session is created or owned here.
    """
    params = {
        "languageCode": language,
        # The app sends its own beta flag; `true` is what a stock install reports and it
        # is what the live probe was validated against.
        "beta": "true",
        "appVersion": APP_VERSION,
        "os": OS,
    }
    # `CONFIG_API_KEY` is a PUBLIC CLIENT key, not a secret: it identifies the client
    # software rather than the user, every hOn installation ships it, and the endpoint
    # accepts no other. Nothing about this request is authenticated -- see the note on
    # the constant in values.py.
    headers = {"Content-Type": CONTENT_TYPE, "x-api-key": CONFIG_API_KEY}
    async with session.get(_APP_CONFIG_URL, params=params, headers=headers) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


def _language_node(app_config: Any) -> dict[str, Any] | None:
    """`payload.language` out of an app-config document, or None.

    The app null-checks every hop of this path and so do we: an app-config that answers
    200 with an unexpected shape must degrade to "no catalog", never raise.
    """
    if not isinstance(app_config, dict):
        return None
    payload = app_config.get("payload")
    if not isinstance(payload, dict):
        return None
    language = payload.get("language")
    return language if isinstance(language, dict) else None


def catalog_url(app_config: dict[str, Any]) -> str | None:
    """URL of the translation catalog, or None."""
    language = _language_node(app_config)
    json_path = language.get("jsonPath") if language else None
    return str(json_path) if json_path else None


def catalog_version(app_config: dict[str, Any]) -> str | None:
    """Catalog revision advertised by app-config, or None.

    The cheap freshness check: app-config is a ~1 KB request that carries the catalog's
    version, so a caller holding a cached catalog can confirm it is current without
    re-downloading ~7.6 MB. Observed values are integers (en=6192, it=4346), normalised
    to str here so a JSON round-trip through a cache cannot change the comparison.
    """
    language = _language_node(app_config)
    version = language.get("version") if language else None
    return None if version is None else str(version)


async def async_fetch_catalog(session: Any, url: str) -> dict[str, dict[str, str]]:
    """Download `url` and return `{namespace: {KEY: label}}`.

    The User-Agent is required (Cloudflare 1010 without it) and the api-key must NOT be
    sent -- the CDN is a plain asset host, not the API gateway.
    """
    async with session.get(url, headers={"User-Agent": USER_AGENT}) as response:
        response.raise_for_status()
        # Accumulate to EOF in chunks. NOT `response.content.read(cap + 1)`: aiohttp's
        # StreamReader.read(n) returns *at most* n bytes and returns as soon as the
        # buffer holds anything, so on this ~7.6 MB body it hands back a single ~98 KB
        # high-water-mark slice. That truncated JSON still passes the `startswith(b"{")`
        # guard and only blows up later in _slice_programs, where async_load swallows it
        # -- i.e. the feature degrades to "no labels" for EVERY user, silently.
        # `iter_chunked` (rather than the simpler `response.read()`) keeps the cap
        # meaningful: it is enforced WHILE reading, so an oversized body is abandoned
        # instead of being fully buffered and only then rejected.
        # A bytearray accumulator, NOT a chunk list plus `b"".join`: the join holds the
        # pieces and the joined copy at the same time, so it adds a whole extra body to
        # the peak for nothing (measured on the real catalog: 60.8 MB against 53.5 MB).
        body = bytearray()
        async for chunk in response.content.iter_chunked(_CHUNK_BYTES):
            if len(body) + len(chunk) > _MAX_CATALOG_BYTES:
                raise ValueError(f"catalog larger than the {_MAX_CATALOG_BYTES} byte cap")
            body.extend(chunk)
    # The app validates the download with `startsWith('{')` before trusting it (a
    # captive-portal HTML page is the failure it guards against); same check here.
    if not body.lstrip().startswith(b"{"):
        raise ValueError("downloaded catalog is not a JSON object")
    return _program_namespaces(_slice_programs(body.decode("utf-8")))


async def async_load_program_labels(
    session: Any, language: str
) -> dict[str, dict[str, str]]:
    """Full chain: app-config -> jsonPath -> catalog -> `{namespace: {KEY: label}}`.

    Raises on any failure; the caller decides whether a missing catalog is fatal (in
    addhOn it is not -- see program_labels.py).
    """
    app_config = await async_app_config(session, language)
    url = catalog_url(app_config)
    if not url:
        raise ValueError("app-config carries no payload.language.jsonPath")
    catalog = await async_fetch_catalog(session, url)
    _LOGGER.debug(
        "Translations debug: catalog '%s' loaded, namespaces=%s",
        language,
        sorted(catalog),
    )
    return catalog
