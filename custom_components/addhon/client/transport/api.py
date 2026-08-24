# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Authenticated HTTP API client of the hOn cloud (addhOn transport).

Implements the authenticated methods on top of `HonConnection` (which injects the
per-request tokens and handles the retry on expiry/401-403). Every method returns the
JSON shape that the parser/command_loader engine expects: it is the contract towards
`HonAppliance`/`HonCommandLoader`, which receive THIS injected api.

TWO philosophies, deliberate:
  * REQUEST CONSTRUCTION (verb, path, params, body) is exact: the cloud is strict about
    the exact request shape (the quirks matter, e.g. the send_command timestamp).
  * RESPONSE EXTRACTION is defensive: on a malformed response we fall back to the safe
    empty default rather than raising KeyError/AttributeError.

ANONYMOUS methods (appliance_configuration / app_config / translation_keys) are NOT
here: they use a handler without auth and do not enter the setup flow of our
appliances; not implemented because not used.

`appliance` is duck-typed (`Any`): we only read `.appliance_type`, `.appliance_model_id`,
`.mac_address`, `.code`, `.info` (dict), `.options`. This way `transport/` stays
decoupled from the engine, as the whole native layer is.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from . import device as _device
from .connection import HonConnection
from .parse import parse_appliance_list, probe_appliance_list
from .values import API_URL
from ...debug_utils import redact_identity
from ...error_codes import APPLIANCE_LIST_EMPTY, classify

_LOGGER = logging.getLogger(__name__)


def _command_timestamp() -> str:
    """Command UTC timestamp in milliseconds + "Z" (e.g. 2026-06-18T12:34:56.789Z).

    The cloud expects exactly 3 fractional digits, so we use
    `isoformat(timespec="milliseconds")` which always renders them (including
    "...56.000Z" when the microseconds are 0). `replace(tzinfo=None)` avoids the
    "+00:00" suffix, keeping the naive UTC value.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return f"{now.isoformat(timespec='milliseconds')}Z"


class HonApi:
    """HTTP methods of the hOn cloud on top of an authenticated `HonConnection`.

    Exposes the signatures (duck-typed appliance) and return shapes the parser
    engine consumes.
    """

    def __init__(self, connection: HonConnection) -> None:
        self._connection = connection
        # Census of the last appliance-list fetch: closed-domain primitives only, read
        # by NativeHon.last_appliance_fetch -> HonClient -> Download Diagnostics. There
        # is one HonApi per session (built in NativeHon.create(), session.py:194) and it
        # is rebuilt with the session, so this always describes THIS session and needs
        # no clear anywhere: a census that outlived its session is how a dump ends up
        # answering "what did the fetch do" with a call that ran on an object the client
        # has already thrown away.
        #
        # That lifetime is the whole contract, and it BOUNDS what a dump can show: the
        # census dies with the session, so a fetch whose failure also killed the session
        # is not readable from a dump. See load_appliances' `except` branch below.
        self.last_appliance_fetch: dict | None = None

    @property
    def auth(self) -> Any:
        """The connection's auth object."""
        return self._connection.auth

    async def load_appliances(self) -> list:
        # The hOn app reads the appliance list from the unified-api aggregator via POST
        # (fix v2.7.1: the old GET commands/v1/appliance returns [] for every
        # account). The defensive extraction lives in parse.parse_appliance_list.
        device_id = self._connection.device.mobile_id or _device.MOBILE_ID
        status: int | None = None
        try:
            async with self._connection.post(
                f"{API_URL}/unified-api/v1/view/appliance-list",
                json={"deviceId": device_id},
            ) as resp:
                # getattr, not resp.status: a session double without the attribute must
                # answer "unknown status", never abort a setup over a diagnostic field.
                status = getattr(resp, "status", None)
                result = await resp.json(content_type=None)
        except Exception as error:
            # connection.py:322-355 raises BEFORE a body exists on 429 (:335), on any
            # >= 500 (:337) and on a non-JSON body -- a CDN or maintenance page (:355).
            # Without this branch the session's own census would still read `None`
            # after a call that demonstrably happened, so the record is written where
            # the fetch is: on the api that made it. Only the catalog LABEL of the
            # classified error is kept: it is a closed-domain token, unlike the
            # exception message, which carries whatever the vendor put in the body.
            #
            # WHAT THIS DOES NOT DO, stated here because the obvious reading is wrong.
            # It does not put `outcome: "raised"` into a diagnostics dump. This raise
            # propagates out of setup() -> NativeHon.create()'s `except BaseException`
            # -> close(), which nulls `self._api` (session.py:737-738), so the session
            # stops reporting a census; HonClient.setup_sync then calls _close_sync()
            # and async_setup_entry pops the whole entry bucket (__init__.py:662, :673).
            # The dump for that entry reads `{"state": "client_absent"}`, and by design:
            # the design note refuses to make a FAILED SETUP diagnosable in this change
            # and inscribes the shape that will (a `setup_failure` record in
            # hass.data), because doing it here would change the meaning of an existing
            # key. Pinned end to end by
            # tests/test_native_session.py::FetchCensusLifetimeTest so the limit is
            # behaviour under test, not folklore. This census is the value that record
            # will carry when it lands.
            self.last_appliance_fetch = {
                "at": datetime.now(timezone.utc),
                "status": status,
                "code": getattr(classify(error), "label", None),
                "outcome": "raised",
                "stopped_at": None,
                "node_type": None,
                "siblings": None,
                "count": None,
            }
            raise
        # Recorded BEFORE the parse, and independently of it. "The cloud sent an empty
        # list" and "our walk stopped at modules.applianceList" both leave `appliances`
        # empty and both read as `"appliances": []` in the diagnostics dump; the probe
        # is the only thing that separates them there, because the WARNING below carries
        # the real key names and a dump can never carry those. parse_appliance_list
        # stays the single source of the returned list; the probe only describes the walk.
        self.last_appliance_fetch = {
            "at": datetime.now(timezone.utc),
            "status": status,
            "code": None,
            **probe_appliance_list(result),
        }
        appliances = parse_appliance_list(result)
        if not appliances:
            # Request/auth OK but 0 appliances: log the response structure to
            # distinguish a truly empty account from an API change (the
            # unified-api list includes offline ones too).
            modules = result.get("modules") if isinstance(result, dict) else None
            _LOGGER.warning(
                "[%s] hOn API: 0 appliances (request OK). result keys=%s; modules keys=%s. "
                "If the appliances appear in the hOn app, it is more likely an API change "
                "than an empty/unshared account.",
                APPLIANCE_LIST_EMPTY.label,
                sorted(result.keys()) if isinstance(result, dict) else "n/a",
                sorted(modules.keys()) if isinstance(modules, dict) else "n/a",
            )
            _LOGGER.debug("hOn raw appliance response: %s", redact_identity(result))
        return appliances

    async def load_commands(self, appliance: Any) -> dict:
        params: dict[str, Any] = {
            "applianceType": appliance.appliance_type,
            "applianceModelId": appliance.appliance_model_id,
            "macAddress": appliance.mac_address,
            "os": _device.OS,
            "appVersion": _device.APP_VERSION,
            "code": appliance.code,
        }
        if firmware_id := appliance.info.get("eepromId"):
            params["firmwareId"] = firmware_id
        if firmware_version := appliance.info.get("fwVersion"):
            params["fwVersion"] = firmware_version
        if series := appliance.info.get("series"):
            params["series"] = series
        url = f"{API_URL}/commands/v1/retrieve"
        async with self._connection.get(url, params=params) as response:
            data = await response.json(content_type=None)
        payload = data.get("payload") if isinstance(data, dict) else None
        # Error-branch on any invalid shape (non-dict or empty payload) -> {}. The pop
        # below REMOVES resultCode from the returned dict (the parser does not want it
        # in the command entries) while validating it.
        if not isinstance(payload, dict) or not payload:
            # data is the raw cloud response (mirrors the device context: macAddress,
            # etc.) and this ERROR is never gated -> redact identity before logging.
            _LOGGER.error("hOn load_commands: invalid payload: %s", redact_identity(data))
            return {}
        if payload.pop("resultCode", None) != "0":
            _LOGGER.error("hOn load_commands: resultCode != 0: %s", redact_identity(data))
            return {}
        return payload

    async def load_command_history(self, appliance: Any) -> list:
        url = f"{API_URL}/commands/v1/appliance/{appliance.mac_address}/history"
        async with self._connection.get(url) as response:
            result = await response.json(content_type=None)
        if not isinstance(result, dict) or not result.get("payload"):
            return []
        payload = result["payload"]
        history = payload.get("history", []) if isinstance(payload, dict) else []
        return history if isinstance(history, list) else []

    async def load_favourites(self, appliance: Any) -> list:
        url = f"{API_URL}/commands/v1/appliance/{appliance.mac_address}/favourite"
        async with self._connection.get(url) as response:
            result = await response.json(content_type=None)
        if not isinstance(result, dict) or not result.get("payload"):
            return []
        payload = result["payload"]
        favourites = payload.get("favourites", []) if isinstance(payload, dict) else []
        return favourites if isinstance(favourites, list) else []

    async def load_last_activity(self, appliance: Any) -> dict:
        url = f"{API_URL}/commands/v1/retrieve-last-activity"
        params = {"macAddress": appliance.mac_address}
        async with self._connection.get(url, params=params) as response:
            result = await response.json(content_type=None)
        if isinstance(result, dict):
            activity = result.get("attributes")
            if isinstance(activity, dict) and activity:
                return activity
        return {}

    async def load_appliance_data(self, appliance: Any) -> dict:
        url = f"{API_URL}/commands/v1/appliance-model"
        params = {"code": appliance.code, "macAddress": appliance.mac_address}
        async with self._connection.get(url, params=params) as response:
            result = await response.json(content_type=None)
        if isinstance(result, dict):
            payload = result.get("payload")
            if isinstance(payload, dict):
                data = payload.get("applianceModel", {})
                return data if isinstance(data, dict) else {}
        return {}

    async def load_attributes(self, appliance: Any) -> dict:
        params = {
            "macAddress": appliance.mac_address,
            "applianceType": appliance.appliance_type,
            "category": "CYCLE",
        }
        url = f"{API_URL}/commands/v1/context"
        async with self._connection.get(url, params=params) as response:
            data = await response.json(content_type=None)
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        return payload if isinstance(payload, dict) else {}

    async def load_statistics(self, appliance: Any) -> dict:
        params = {
            "macAddress": appliance.mac_address,
            "applianceType": appliance.appliance_type,
        }
        url = f"{API_URL}/commands/v1/statistics"
        async with self._connection.get(url, params=params) as response:
            data = await response.json(content_type=None)
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        return payload if isinstance(payload, dict) else {}

    async def load_maintenance(self, appliance: Any) -> dict:
        url = f"{API_URL}/commands/v1/maintenance-cycle"
        params = {"macAddress": appliance.mac_address}
        async with self._connection.get(url, params=params) as response:
            data = await response.json(content_type=None)
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        return payload if isinstance(payload, dict) else {}

    async def load_aws_token(self) -> str:
        url = f"{API_URL}/auth/v1/introspection"
        async with self._connection.get(url) as response:
            data = await response.json(content_type=None)
        payload = data.get("payload", {}) if isinstance(data, dict) else {}
        token = payload.get("tokenSigned", "") if isinstance(payload, dict) else ""
        return token if isinstance(token, str) else ""

    async def send_command(
        self,
        appliance: Any,
        command: str,
        parameters: dict[str, Any],
        ancillary_parameters: dict[str, Any],
        program_name: str = "",
    ) -> bool:
        timestamp = _command_timestamp()
        data: dict[str, Any] = {
            "macAddress": appliance.mac_address,
            "timestamp": timestamp,
            "commandName": command,
            "transactionId": f"{appliance.mac_address}_{timestamp}",
            "applianceOptions": appliance.options,
            "device": self._connection.device.payload(mobile=True),
            "attributes": {
                "channel": "mobileApp",
                "origin": "standardProgram",
                "energyLabel": "0",
            },
            "ancillaryParameters": ancillary_parameters,
            "parameters": parameters,
            "applianceType": appliance.appliance_type,
        }
        if command == "startProgram" and program_name:
            data["programName"] = program_name.upper()
        url = f"{API_URL}/commands/v1/send"
        async with self._connection.post(url, json=data) as response:
            json_data = await response.json(content_type=None)
            payload = json_data.get("payload") if isinstance(json_data, dict) else None
            if isinstance(payload, dict) and payload.get("resultCode") == "0":
                return True
            # The request payload (data) carries macAddress, transactionId (= MAC)
            # and device.mobileId; the response may echo them too. Log only the
            # command + resultCode at ERROR (no identity, always emitted), and the
            # full REDACTED payload/response at DEBUG (gated) for troubleshooting.
            result_code = payload.get("resultCode") if isinstance(payload, dict) else None
            _LOGGER.error(
                "hOn send_command failed: command=%s resultCode=%s", command, result_code
            )
            _LOGGER.debug(
                "hOn send_command failed payload (redacted)=%s response=%s",
                redact_identity(data),
                redact_identity(json_data),
            )
        return False

    async def close(self) -> None:
        await self._connection.close()
