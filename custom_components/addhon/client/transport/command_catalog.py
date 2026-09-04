# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed request and privacy-safe structural probe for command catalogs."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from . import device as _device

CATALOG_OUTCOMES = frozenset(
    {"ok", "empty_payload", "invalid_payload", "missing_result", "nonzero_result"}
)
CATALOG_RESULT_CATEGORIES = frozenset({"zero", "missing", "nonzero", "other"})


def normalize_catalog_language(value: Any) -> str:
    """Return the lower-case base language, falling back to English."""
    text = value.strip().lower() if isinstance(value, str) else ""
    return text.split("-", 1)[0] or "en"


@dataclass(frozen=True, slots=True)
class CommandCatalogRequest:
    """Immutable inputs for the official command-catalog request."""

    appliance_type: str
    appliance_model_id: str
    mac_address: str
    code: str
    firmware_id: Any = None
    firmware_version: Any = None
    series: Any = None
    series_version: Any = None
    language: str = "en"

    @classmethod
    def from_appliance(
        cls, appliance: Any, language: Any
    ) -> "CommandCatalogRequest":
        """Build request inputs without retaining the appliance or its info mapping."""
        info = appliance.info if isinstance(appliance.info, dict) else {}
        return cls(
            appliance_type=str(appliance.appliance_type),
            appliance_model_id=str(appliance.appliance_model_id),
            mac_address=str(appliance.mac_address),
            code=str(appliance.code),
            firmware_id=info.get("eepromId"),
            firmware_version=info.get("fwVersion"),
            series=info.get("series"),
            series_version=info.get("seriesVersion"),
            language=normalize_catalog_language(language),
        )

    def params(self) -> dict[str, Any]:
        """Create a fresh HTTP parameter mapping for each request."""
        params: dict[str, Any] = {
            "applianceType": self.appliance_type,
            "applianceModelId": self.appliance_model_id,
            "macAddress": self.mac_address,
            "code": self.code,
            "os": _device.OS,
            "appVersion": _device.APP_VERSION,
            "lang": normalize_catalog_language(self.language),
        }
        optional = (
            ("firmwareId", self.firmware_id),
            ("fwVersion", self.firmware_version),
            ("series", self.series),
            ("seriesVersion", self.series_version),
        )
        for name, value in optional:
            if value:
                params[name] = value
        return params

    def presence(self) -> dict[str, bool]:
        """Return only privacy-safe discriminator-presence flags."""
        return {
            "firmware": bool(self.firmware_id),
            "firmware_version": bool(self.firmware_version),
            "series": bool(self.series),
            "series_version": bool(self.series_version),
            "language": bool(self.language),
        }


@dataclass(frozen=True, slots=True)
class CommandCatalogProbe:
    """Closed-vocabulary structural facts safe for logs and diagnostics."""

    outcome: str
    status: int | None
    result_category: str
    has_appliance_model: bool
    has_settings: bool
    has_set_parameters: bool
    has_start_program: bool
    has_stop_program: bool
    payload_entries: int
    request_firmware: bool
    request_firmware_version: bool
    request_series: bool
    request_series_version: bool
    request_language: bool
    code_length: int

    def as_dict(self) -> dict[str, str | int | bool | None]:
        """Return a fresh mapping containing only the declared safe fields."""
        return {
            "outcome": self.outcome,
            "status": self.status,
            "result_category": self.result_category,
            "has_appliance_model": self.has_appliance_model,
            "has_settings": self.has_settings,
            "has_set_parameters": self.has_set_parameters,
            "has_start_program": self.has_start_program,
            "has_stop_program": self.has_stop_program,
            "payload_entries": self.payload_entries,
            "request_firmware": self.request_firmware,
            "request_firmware_version": self.request_firmware_version,
            "request_series": self.request_series,
            "request_series_version": self.request_series_version,
            "request_language": self.request_language,
            "code_length": self.code_length,
        }


@dataclass(frozen=True, slots=True)
class CommandCatalogFetch:
    """A normalized payload paired with its structural observation."""

    payload: dict[str, Any]
    probe: CommandCatalogProbe


class CommandCatalogResponseError(Exception):
    """A structurally unusable response carrying only a safe probe."""

    def __init__(self, probe: CommandCatalogProbe) -> None:
        self.probe = probe
        super().__init__(f"Command catalog response rejected: {probe.outcome}")


def _result_category(payload: dict[Any, Any]) -> str:
    if "resultCode" not in payload:
        return "missing"
    result = payload.get("resultCode")
    if result == "0":
        return "zero"
    if isinstance(result, (str, int, float)) and not isinstance(result, bool):
        return "nonzero"
    return "other"


def _probe(
    payload: dict[Any, Any] | None,
    *,
    outcome: str,
    result_category: str,
    status: int | None,
    request: CommandCatalogRequest,
) -> CommandCatalogProbe:
    normalized = (
        {key: value for key, value in payload.items() if key != "resultCode"}
        if payload is not None
        else {}
    )
    settings = normalized.get("settings")
    presence = request.presence()
    return CommandCatalogProbe(
        outcome=outcome,
        status=status if type(status) is int else None,
        result_category=result_category,
        has_appliance_model="applianceModel" in normalized,
        has_settings="settings" in normalized,
        has_set_parameters=isinstance(settings, dict) and "setParameters" in settings,
        has_start_program="startProgram" in normalized,
        has_stop_program="stopProgram" in normalized,
        payload_entries=len(normalized),
        request_firmware=presence["firmware"],
        request_firmware_version=presence["firmware_version"],
        request_series=presence["series"],
        request_series_version=presence["series_version"],
        request_language=presence["language"],
        code_length=len(request.code) if isinstance(request.code, str) else 0,
    )


def extract_command_catalog(
    data: Any, *, status: int | None, request: CommandCatalogRequest
) -> CommandCatalogFetch:
    """Validate and copy a command-catalog envelope without mutating its body."""
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        probe = _probe(
            None,
            outcome="invalid_payload",
            result_category="other",
            status=status,
            request=request,
        )
        raise CommandCatalogResponseError(probe)

    result_category = _result_category(payload)
    if not payload:
        outcome = "empty_payload"
    elif result_category == "missing":
        outcome = "missing_result"
    elif result_category != "zero":
        outcome = "nonzero_result"
    elif len(payload) == 1:
        outcome = "empty_payload"
    else:
        outcome = "ok"

    probe = _probe(
        payload,
        outcome=outcome,
        result_category=result_category,
        status=status,
        request=request,
    )
    if outcome != "ok":
        raise CommandCatalogResponseError(probe)

    normalized = {key: value for key, value in payload.items() if key != "resultCode"}
    return CommandCatalogFetch(copy.deepcopy(normalized), probe)


def legacy_command_catalog_fetch(
    payload: Any, request: CommandCatalogRequest
) -> CommandCatalogFetch:
    """Adapt a legacy mapping result without letting empty values bypass validation."""
    if not isinstance(payload, dict):
        probe = _probe(
            None,
            outcome="invalid_payload",
            result_category="other",
            status=None,
            request=request,
        )
        raise CommandCatalogResponseError(probe)
    outcome = "ok" if payload else "empty_payload"
    probe = _probe(
        payload,
        outcome=outcome,
        result_category="missing",
        status=None,
        request=request,
    )
    if not payload:
        raise CommandCatalogResponseError(probe)
    return CommandCatalogFetch(copy.deepcopy(payload), probe)
