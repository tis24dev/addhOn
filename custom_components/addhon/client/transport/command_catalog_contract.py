# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Request and error contract for command-catalog transport operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import device as _device

if TYPE_CHECKING:
    from .command_catalog import CommandCatalogProbe


_OPTIONAL_PARAMETERS = (
    ("firmwareId", "firmware_id"),
    ("fwVersion", "firmware_version"),
    ("series", "series"),
    ("seriesVersion", "series_version"),
)

_APPLIANCE_IDENTIFIERS = (
    ("appliance_type", "appliance_type"),
    ("appliance_model_id", "appliance_model_id"),
    ("mac_address", "mac_address"),
    ("code", "code"),
)

_APPLIANCE_DISCRIMINATORS = (
    ("firmware_id", "eepromId"),
    ("firmware_version", "fwVersion"),
    ("series", "series"),
    ("series_version", "seriesVersion"),
)

_REQUIRED_PARAMETERS = (
    "applianceType",
    "applianceModelId",
    "macAddress",
    "code",
    "os",
    "appVersion",
    "lang",
)

_PRESENCE_FLAGS = (
    ("firmware", "firmware_id"),
    ("firmware_version", "firmware_version"),
    ("series", "series"),
    ("series_version", "series_version"),
    ("language", "language"),
)


# The ONLY region-qualified tag the official app can send. Its translation table
# (decomp.txt:483593-483650) is `cs de el en es fr he hr it nl pl pt ro ru sk sl sr tr uk
# zh zh-hk ar hu nb fi sv lv et lt ...`: base codes throughout, with this one exception.
# `findBestAvailableLanguage` (decomp.txt:507774-507838) returns the FULL tag only when
# the table contains it, and otherwise falls back to the bare language code -- so a
# pt-BR phone makes the app send `pt`, exactly what truncating gives, while a zh-HK phone
# makes it send `zh-hk`, which truncating would turn into a different language.
_APP_REGIONAL_TAGS = frozenset({"zh-hk"})


def normalize_catalog_language(value: Any) -> str:
    """Return the language tag the official app would send, or English.

    Truncating to the base subtag reproduces the app for every locale but one, because
    the app resolves the tag against the languages it actually ships rather than against
    the device locale. `_APP_REGIONAL_TAGS` carries that one.
    """
    if not isinstance(value, str):
        return "en"
    tag = value.strip().lower()
    if tag in _APP_REGIONAL_TAGS:
        return tag
    base, _, _ = tag.partition("-")
    return base or "en"


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
        identifiers = {
            name: str(getattr(appliance, attribute))
            for name, attribute in _APPLIANCE_IDENTIFIERS
        }
        discriminators = {
            name: info.get(key) for name, key in _APPLIANCE_DISCRIMINATORS
        }
        return cls(
            **identifiers,
            **discriminators,
            language=normalize_catalog_language(language),
        )

    def params(self) -> dict[str, Any]:
        """Create a fresh HTTP parameter mapping for each request."""
        values = (
            self.appliance_type,
            self.appliance_model_id,
            self.mac_address,
            self.code,
            _device.OS,
            _device.APP_VERSION,
            normalize_catalog_language(self.language),
        )
        required = dict(zip(_REQUIRED_PARAMETERS, values, strict=True))
        optional = {
            name: value
            for name, attribute in _OPTIONAL_PARAMETERS
            if (value := getattr(self, attribute))
        }
        return required | optional

    def presence(self) -> dict[str, bool]:
        """Return only privacy-safe discriminator-presence flags."""
        return {
            name: bool(getattr(self, attribute))
            for name, attribute in _PRESENCE_FLAGS
        }


class CommandCatalogResponseError(Exception):
    """A structurally unusable response carrying only a safe probe."""

    def __init__(self, probe: CommandCatalogProbe) -> None:
        self.probe = probe
        super().__init__(f"Command catalog response rejected: {probe.outcome}")
