# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Factory for the native hOn session and appliance.

Building the session and the appliance behind these two functions keeps
`hon_client.py` decoupled from the concrete client classes.

`create_session` returns an object conforming to `interfaces.HonSession` (our
`client.session.NativeHon`); `create_appliance` returns an `interfaces.Appliance`
(`client.engine.appliance.HonAppliance`). The BABYCARE bug fix lives natively in the
enum class (`client.engine.parameter.enum`).
"""
from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Cache of the native ROOT appliance class (lazy import: the engine imports without awscrt).
_NATIVE_APPLIANCE_CLS: Any = None


def create_session(
    email: str,
    password: str,
    *,
    enable_mqtt: bool = True,
    minimal: bool = False,
    refresh_token: str = "",
) -> Any:
    """Create the NATIVE hOn session (`client.session.NativeHon`).

    Auth, connection, api, MQTT, orchestration and parser engine are all OURS.
    The caller uses it as an async context manager (`__aenter__()` -> `.appliances`).

    `enable_mqtt=False` skips the AWS IoT realtime push and `minimal=True` skips the
    per-appliance command/attribute/statistics loads: the config-flow validation uses
    both so it only authenticates and counts the appliances, instead of doing the full
    (and far slower / more fragile) runtime setup (issue #30).

    Lazy import of `NativeHon`: avoids the cycle (session.py imports this module) and
    keeps `factory` importable dry (`NativeHon` pulls in awscrt via MQTT).
    """
    from .session import NativeHon

    return NativeHon(
        email=email,
        password=password,
        enable_mqtt=enable_mqtt,
        minimal=minimal,
        refresh_token=refresh_token,
    )


def _native_engine_appliance_cls() -> Any:
    """Return the NATIVE ROOT appliance class (`engine.appliance.HonAppliance`).

    The ROOT is a standalone class that uses attributes/loader/commands/rules/program/
    per-type, ALL native. Lazy import (the engine imports without awscrt), cached per process.
    """
    global _NATIVE_APPLIANCE_CLS
    if _NATIVE_APPLIANCE_CLS is None:
        from .engine.appliance import HonAppliance as _NativeRoot

        _NATIVE_APPLIANCE_CLS = _NativeRoot
    return _NATIVE_APPLIANCE_CLS


def create_appliance(api: Any, appliance_data: dict, zone: int = 0) -> Any:
    """Build the NATIVE ROOT appliance.

    The whole engine (loader/commands/rules/program/parameters/attributes/per-type + ROOT)
    is ours. The returned object conforms to the Protocol `interfaces.Appliance`
    (duck-typing). Lazy import.
    """
    return _native_engine_appliance_cls()(api, appliance_data, zone=zone)
