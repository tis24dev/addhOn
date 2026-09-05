# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pure, validated storage for last-known-good command catalogs."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .transport.command_catalog import (
    CATALOG_OUTCOMES,
    CommandCatalogRequest,
    normalize_catalog_language,
)

CACHE_SCHEMA_VERSION = 2
DEGRADED_CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

CATALOG_SOURCES = frozenset({"live", "cache", "none"})
CATALOG_FAILURES = frozenset({"transport", "structural", "semantic"})
CATALOG_ENRICHMENTS = frozenset({"ok", "empty", "raised", "invalid"})
CATALOG_REQUEST_FLAGS = (
    "firmware",
    "firmware_version",
    "series",
    "series_version",
    "language",
)

_MAX_COUNT = 1_000_000
_MAX_AGE_SECONDS = 315_360_000
_ADDHON_CODE_RE = re.compile(r"ADDHON-[0-9]{3}")
_RECORD_FIELDS = (
    "version",
    "appliance_type",
    "appliance_model_id",
    "code",
    "firmware_id",
    "firmware_version",
    "series",
    "series_version",
    "language",
    "stored_at",
    "payload",
    "digest",
)
_OPTIONAL_GUARDS = (
    ("firmware_id", "firmware_id"),
    ("firmware_version", "firmware_version"),
    ("series", "series"),
    ("series_version", "series_version"),
)


@dataclass(frozen=True, slots=True)
class CachedCommandCatalog:
    """A compatible catalog copied out of the private repository."""

    payload: dict[str, Any]
    age_seconds: int
    digest: str
    degraded_match: bool


@dataclass(frozen=True, slots=True)
class CommandCatalogSnapshot:
    """A defensive persistent document paired with its local generation."""

    generation: int
    document: dict[str, Any]


def _json_value(value: Any) -> Any:
    """Build a plain JSON-compatible deep copy or reject the value."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Command catalog contains a non-finite number")
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("Command catalog keys must be strings")
            normalized[key] = _json_value(item)
        return normalized
    raise ValueError("Command catalog is not JSON-compatible")


def _payload_and_digest(payload: Any) -> tuple[dict[str, Any], str]:
    if not isinstance(payload, Mapping):
        raise ValueError("Command catalog payload must be a mapping")
    normalized = _json_value(payload)
    assert isinstance(normalized, dict)
    try:
        canonical = json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as err:
        raise ValueError("Command catalog cannot be serialized canonically") from err
    return normalized, hashlib.sha256(canonical).hexdigest()


def _request_text(value: Any) -> str:
    if value is None or value is False:
        return ""
    if type(value) in (str, int, float):
        return str(value)
    return ""


def _valid_stored_text(value: Any) -> bool:
    return type(value) is str


def _bounded_count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= _MAX_COUNT else None


def _bounded_age(value: Any) -> int | None:
    return (
        value
        if type(value) is int and 0 <= value <= _MAX_AGE_SECONDS
        else None
    )


def _safe_code(value: Any) -> str | None:
    if type(value) is str and _ADDHON_CODE_RE.fullmatch(value):
        return value
    return None


def _require_token(
    name: str, value: Any, allowed: frozenset[str], *, optional: bool = False
) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or value not in allowed:
        raise ValueError(f"Unsupported command catalog {name}")
    return value


class CommandCatalogRepository:
    """Own validated raw catalogs and identity-free runtime observations."""

    def __init__(
        self,
        document: Any = None,
        language: Any = "en",
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._language = normalize_catalog_language(language)
        self._clock = clock
        self._generation = 0
        self._records: dict[str, dict[str, Any]] = {}
        self._observations: dict[str, dict[str, Any]] = {}
        self._load_document(document)

    @property
    def language(self) -> str:
        """Normalized base language shared with the request builder."""
        return self._language

    def lookup(self, request: CommandCatalogRequest) -> CachedCommandCatalog | None:
        """Return a compatible defensive catalog copy for one physical appliance."""
        record = self._records.get(_request_text(request.mac_address))
        if record is None:
            return None
        if record["appliance_type"] != _request_text(request.appliance_type):
            return None
        if record["appliance_model_id"] != _request_text(
            request.appliance_model_id
        ):
            return None
        if record["code"] != _request_text(request.code):
            return None
        if record["language"] != normalize_catalog_language(request.language):
            return None

        degraded = False
        for request_name, record_name in _OPTIONAL_GUARDS:
            current = _request_text(getattr(request, request_name))
            if current:
                if current != record[record_name]:
                    return None
            else:
                degraded = True

        age_seconds = self._age(record["stored_at"])
        if degraded and age_seconds > DEGRADED_CACHE_MAX_AGE_SECONDS:
            return None

        return CachedCommandCatalog(
            payload=copy.deepcopy(record["payload"]),
            age_seconds=age_seconds,
            digest=record["digest"],
            degraded_match=degraded,
        )

    def replace(self, request: CommandCatalogRequest, payload: Any) -> bool:
        """Atomically replace one valid record and advance generation if changed."""
        mac = _request_text(request.mac_address)
        appliance_type = _request_text(request.appliance_type)
        appliance_model_id = _request_text(request.appliance_model_id)
        if not mac or not appliance_type or not appliance_model_id:
            raise ValueError("Command catalog request identity is incomplete")

        normalized, digest = _payload_and_digest(payload)
        try:
            stored_at = int(self._clock())
        except (TypeError, ValueError, OverflowError) as err:
            raise ValueError("Command catalog timestamp is invalid") from err
        if stored_at < 0:
            raise ValueError("Command catalog timestamp is invalid")

        replacement = {
            "version": CACHE_SCHEMA_VERSION,
            "appliance_type": appliance_type,
            "appliance_model_id": appliance_model_id,
            "code": _request_text(request.code),
            "firmware_id": _request_text(request.firmware_id),
            "firmware_version": _request_text(request.firmware_version),
            "series": _request_text(request.series),
            "series_version": _request_text(request.series_version),
            "language": normalize_catalog_language(request.language),
            "stored_at": stored_at,
            "payload": normalized,
            "digest": digest,
        }
        existing = self._records.get(mac)
        if existing is not None and self._same_content(existing, replacement):
            return False
        self._records[mac] = replacement
        self._generation += 1
        return True

    def record(
        self,
        request: CommandCatalogRequest,
        *,
        source: str,
        failure: str | None,
        live_outcome: str | None,
        code: str | None,
        raw_entries: int,
        parsed_commands: int,
        favourites: str,
        history: str,
        cache: CachedCommandCatalog | None = None,
    ) -> None:
        """Record one bounded, identity-free runtime observation per local MAC."""
        safe_source = _require_token("source", source, CATALOG_SOURCES)
        safe_failure = _require_token(
            "failure", failure, CATALOG_FAILURES, optional=True
        )
        safe_outcome = _require_token(
            "outcome", live_outcome, CATALOG_OUTCOMES, optional=True
        )
        safe_favourites = _require_token(
            "favourites outcome", favourites, CATALOG_ENRICHMENTS
        )
        safe_history = _require_token(
            "history outcome", history, CATALOG_ENRICHMENTS
        )
        presence = request.presence()
        request_flags = {
            name: bool(presence.get(name)) for name in CATALOG_REQUEST_FLAGS
        }
        cache_age = _bounded_age(cache.age_seconds) if cache is not None else None
        digest = (
            cache.digest[:12]
            if cache is not None
            and type(cache.digest) is str
            and re.fullmatch(r"[0-9a-f]{64}", cache.digest)
            else None
        )
        self._observations[_request_text(request.mac_address)] = {
            "source": safe_source,
            "failure": safe_failure,
            "live_outcome": safe_outcome,
            "code": _safe_code(code),
            "raw_entries": _bounded_count(raw_entries),
            "parsed_commands": _bounded_count(parsed_commands),
            "cache_age_s": cache_age,
            "digest": digest,
            "favourites": safe_favourites,
            "history": safe_history,
            "request": request_flags,
        }

    def snapshot(self) -> CommandCatalogSnapshot:
        """Return a defensive persistent snapshot; observations are never included."""
        return CommandCatalogSnapshot(
            generation=self._generation,
            document={
                "version": CACHE_SCHEMA_VERSION,
                "records": copy.deepcopy(self._records),
            },
        )

    def census(self) -> list[dict[str, Any]]:
        """Return defensive observations without their private physical-MAC keys."""
        return copy.deepcopy(list(self._observations.values()))

    def _age(self, stored_at: int) -> int:
        try:
            age = int(self._clock()) - stored_at
        except (TypeError, ValueError, OverflowError):
            return 0
        return max(age, 0)

    @staticmethod
    def _same_content(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
        return all(
            first.get(field) == second.get(field)
            for field in _RECORD_FIELDS
            if field != "stored_at"
        )

    def _load_document(self, document: Any) -> None:
        if not isinstance(document, Mapping):
            return
        if type(document.get("version")) is not int or document.get(
            "version"
        ) != CACHE_SCHEMA_VERSION:
            return
        records = document.get("records")
        if not isinstance(records, Mapping):
            return
        for mac, candidate in records.items():
            record = self._validated_record(mac, candidate)
            if record is not None:
                self._records[mac] = record

    @staticmethod
    def _validated_record(mac: Any, candidate: Any) -> dict[str, Any] | None:
        if type(mac) is not str or not mac or not isinstance(candidate, Mapping):
            return None
        if set(candidate) != set(_RECORD_FIELDS):
            return None
        if type(candidate.get("version")) is not int or candidate.get(
            "version"
        ) != CACHE_SCHEMA_VERSION:
            return None
        for field in (
            "appliance_type",
            "appliance_model_id",
            "code",
            "firmware_id",
            "firmware_version",
            "series",
            "series_version",
            "language",
            "digest",
        ):
            if not _valid_stored_text(candidate.get(field)):
                return None
        if not candidate["appliance_type"] or not candidate["appliance_model_id"]:
            return None
        if normalize_catalog_language(candidate["language"]) != candidate["language"]:
            return None
        stored_at = candidate.get("stored_at")
        if type(stored_at) is not int or stored_at < 0:
            return None
        try:
            payload, digest = _payload_and_digest(candidate.get("payload"))
        except ValueError:
            return None
        if candidate.get("digest") != digest:
            return None
        return {
            "version": CACHE_SCHEMA_VERSION,
            "appliance_type": candidate["appliance_type"],
            "appliance_model_id": candidate["appliance_model_id"],
            "code": candidate["code"],
            "firmware_id": candidate["firmware_id"],
            "firmware_version": candidate["firmware_version"],
            "series": candidate["series"],
            "series_version": candidate["series_version"],
            "language": candidate["language"],
            "stored_at": stored_at,
            "payload": payload,
            "digest": digest,
        }
