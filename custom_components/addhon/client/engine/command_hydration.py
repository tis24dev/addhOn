# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Immutable outcome of one command-catalog hydration attempt."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ...error_codes import APPLIANCE_COMMANDS_UNAVAILABLE, HonCodedError
from .commands import HonCommand

CATALOG_REQUIRED_TYPES = frozenset({"REF", "FR", "FRE"})


@dataclass(frozen=True, slots=True)
class CommandHydration:
    """A complete engine candidate ready for atomic appliance adoption."""

    commands: dict[str, HonCommand]
    appliance_model: dict[str, Any]
    additional_data: dict[str, Any]
    source: Literal["live", "cache"]
    live_outcome: str | None
    raw_entry_count: int
    parsed_command_count: int
    favourites_outcome: str
    history_outcome: str
    # The `/history` list exactly as the cloud sent it, whatever the enrichment made of
    # it. Kept for the diagnostics dump: it holds the official app's own `startProgram`
    # payloads, which the `commandHistory` attribute (one slot, and one the cloud resets
    # to null -- issue #112) cannot be relied on to still carry when a dump is taken.
    command_history: list[dict[str, Any]] = field(default_factory=list)


class CommandCatalogUnavailable(HonCodedError):
    """Neither the live response nor a compatible cache is semantically usable."""

    def __init__(self) -> None:
        super().__init__(
            APPLIANCE_COMMANDS_UNAVAILABLE,
            phase="load_appliance/commands",
        )
