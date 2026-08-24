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
from .parse import APPLIANCE_LIST_PATH, parse_appliance_list, probe_appliance_list
from .tokens import token_person_account_id
from .values import API_URL
from ...debug_utils import redact_identity, safe_key_names, structure_only
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


# The verdicts `account_match` may return. PUBLIC (no leading underscore) for the same
# reason `parse.APPLIANCE_LIST_PATH` is: a reader outside this module depends on it --
# `tests/test_diagnostics.py` pins `diagnostics._FETCH_ACCOUNTS` against this tuple, so
# the domain the dump is allowed to print cannot drift away from the domain the writer
# produces. A verdict this build does not declare here would reach a document as
# "other", which for an identity check is the one answer nobody can act on.
ACCOUNT_TOKENS = ("match", "mismatch", "mixed", "no_appliances", "no_claim", "unknown")

# The account id the cloud stamps on every element of the appliance list. Present on
# 1/1 entries of the healthy live capture, alongside the DynamoDB `PK`/`SK` markers
# that make the list a per-identity query rather than a per-account table
# (apk/analysis/addhon210-healthy-envelope-baseline.md).
_APPLIANCE_ACCOUNT_KEY = "sfPersonAccountId"


def account_match(result: Any, person_account_id: str | None) -> str:
    """Do the appliances the cloud returned belong to the account WE authenticated as?

    The self-check the ADDHON-210 investigation kept needing and could not make: our
    id_token carries `custom_attributes.PersonAccountId`, every appliance in the
    response carries `sfPersonAccountId`, and until now nothing compared the two. A
    session that silently resolves to a different account answers 200 with a
    well-formed envelope and a list that is empty or simply not ours -- and every
    other field of the dump reads like a legitimately empty account. What this field
    adds is the account BOUNDARY; whether crossing it is a fault is a question the
    complaint answers, not this function (see the note under the verdicts).

    RETURNS A TOKEN OF `ACCOUNT_TOKENS`, NEVER AN IDENTIFIER. Both values compared here
    are account ids; neither is emitted, neither is logged. That is the whole design
    rule of the census block and the reason this function returns a verdict instead of
    the two strings that produced it.

    The verdicts, and why each is a different diagnosis:
      * `no_claim`     -- OUR token has no readable PersonAccountId, so the check could
                          not be attempted. Takes priority over everything below: it
                          says the problem may be on our side of the wire.
      * `unknown`      -- the claim is there but the response could not be read that
                          far, or no appliance in it declares an owner.
      * `no_appliances` -- the claim is there and the cloud returned an EMPTY list.
                          Distinct from `unknown` for the one reason this function
                          walks the response itself instead of calling
                          `parse_appliance_list`: that parser returns `[]` for an empty
                          list AND for a drift our walk cannot follow (its sec9
                          fail-safe), which is exactly the distinction being made here.
      * `match`        -- every appliance whose owner we could read is ours.
      * `mismatch`     -- none of them is.
      * `mixed`        -- some are and some are not.

    `mismatch` AND `mixed` ARE NOT FAULTS ON THEIR OWN, and reading them as one would
    mis-triage a healthy install. hOn family sharing puts appliances a member does not
    OWN into that member's own list -- the same `view/appliance-list` serves owned and
    shared alike, proven by the app re-running `retrieveInitialAppliances()` when an
    invitation turns CONFIRMED (apk/analysis/addhon210-empty-appliance-list.md). A user
    who joined someone else's group therefore sees every appliance stamped with the
    OWNER's account id, and reads `mismatch` while everything works. A household where
    one of two appliances is shared reads `mixed`, likewise correctly.

    What the two verdicts actually mean is "the list is not stamped with our account
    id", which is a fact, not a diagnosis. They earn their weight only NEXT TO the
    symptom: a `mismatch` on a user who reports missing appliances says the session and
    the appliances are on different sides of an account boundary, which is worth
    chasing; the same `mismatch` on a working install says the user is a group member,
    which is worth nothing. `no_appliances` is the verdict that carries a complaint on
    its own, because an empty list is the complaint.

    Appliances with no readable owner do not vote and are silent rather than counted
    as a mismatch: a missing or non-string `sfPersonAccountId` is a schema question,
    and the schema questions are what `probe_appliance_list` already answers. When
    they are ALL silent the verdict is `unknown`, so a "match" is never returned by an
    empty vote. `count` sits beside this field in the same block, so a reader can see
    how many appliances the verdict was drawn from.

    `type(owner) is str`, not isinstance: this is an equality test against a value
    chosen by the cloud, and `json.loads` cannot produce a str subclass but a
    duck-typed response double can -- one with an `__eq__` that returns True would turn
    a mismatch into a match, which is the single most misleading thing this field could
    say.

    Never raises, for the reason `probe_appliance_list` gives at length: it runs inside
    `NativeHon.setup()`, where an exception does not spoil a diagnostic field, it takes
    the config entry down.
    """
    try:
        if type(person_account_id) is not str or not person_account_id:
            return "no_claim"
        node: Any = result
        for key in APPLIANCE_LIST_PATH:
            if not isinstance(node, dict) or key not in node:
                return "unknown"
            node = node[key]
        if not isinstance(node, list):
            return "unknown"
        if not node:
            return "no_appliances"
        ours = 0
        theirs = 0
        for entry in node:
            owner = entry.get(_APPLIANCE_ACCOUNT_KEY) if isinstance(entry, dict) else None
            if type(owner) is not str or not owner:
                continue
            if owner == person_account_id:
                ours += 1
            else:
                theirs += 1
        if not ours and not theirs:
            return "unknown"
        if not theirs:
            return "match"
        if not ours:
            return "mismatch"
        return "mixed"
    except Exception:  # noqa: BLE001 - a census field must never abort a setup
        return "unknown"


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

    def _person_account_id(self) -> str | None:
        """The account id OUR id_token claims, for `account_match`. Never emitted.

        GUARDED, and the guard is not decoration: `HonConnection.auth` RAISES when the
        connection was never created (connection.py:102-105), and every test double and
        every duck-typed connection in this transport is free to have no `auth` at all.
        This value feeds one census field, and the rule the census lives under is the
        one the `getattr` on `resp.status` follows a few lines below -- a diagnostic
        field must answer "unknown", never abort a setup.
        """
        try:
            token = self.auth.id_token
        except Exception:  # noqa: BLE001 - a census field must never abort a setup
            return None
        return token_person_account_id(token) if type(token) is str else None

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
                # Every field downstream of a body, written out as null rather than
                # left off: the census key set must be the same on both paths, or the
                # reader has to distinguish "the call never got a response" from "this
                # build did not record that field yet".
                "envelope_ok": None,
                "module_ok": None,
                "auth_keys": None,
                "account": None,
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
            # The identity self-check, and the only census field that needs something
            # the RESPONSE does not carry -- which is why it is assembled here, where
            # the connection (and through it our id_token) is in scope, instead of
            # inside the probe. It walks the response itself for the reason its
            # docstring gives: `parse_appliance_list` collapses "empty" and "drift"
            # into the same `[]`, and telling those apart is half of what it answers.
            "account": account_match(result, self._person_account_id()),
        }
        appliances = parse_appliance_list(result)
        if not appliances:
            # Request/auth OK but 0 appliances: log the response structure to
            # distinguish a truly empty account from an API change (the
            # unified-api list includes offline ones too).
            # Reading the key NAMES is the last unguarded stretch of this method, and
            # it is a diagnostic: `.get` and `.keys()` are calls on an object the cloud
            # supplied, so on the hostile mapping the parse now survives they are what
            # would still abort the setup -- and abort it while BUILDING the message
            # that exists to explain the failure. The `n/a` above is the shape-mismatch
            # answer; `unreadable` is the object-fought-back one, and they are worth
            # telling apart in the log for the same reason `outcome: "other"` is worth
            # telling apart in the dump.
            # `safe_key_names`, not `sorted(...keys())`. Reading "just the names" looks
            # obviously harmless and is not: a mapping the vendor keyed BY a serial or
            # by a cognito identity partition carries that identity in the key
            # position, and this line goes to a level meant for pasting into public
            # issues. Same shape rule as the response shape below, so the summary and
            # the detail cannot disagree about what is safe to print.
            try:
                modules = result.get("modules") if isinstance(result, dict) else None
                result_keys = safe_key_names(result)
                module_keys = safe_key_names(modules)
            except Exception:  # noqa: BLE001 - a log line must never abort a setup
                result_keys = module_keys = "unreadable"
            # Emitted OUTSIDE the try so the code the user is told to search for is
            # emitted whatever the body did: ADDHON-210 is the signal, the key names
            # are the detail.
            #
            # THE CENSUS TRAVELS WITH THE WARNING, at WARNING level, and that is a
            # deliberate departure from "diagnostics live in the dump". This branch is
            # reached only when the account shows no appliances -- the single hardest
            # report to act on, and the one where asking for a downloaded dump has
            # repeatedly failed to produce one. Every value here is a closed-domain
            # token or a bounded int produced by OUR code (`probe_appliance_list`,
            # `account_match`), not a value chosen by the cloud, so the line is
            # leak-proof by the same construction the dump block relies on and can be
            # pasted into a public issue as it stands. A reporter who can copy one log
            # line can now hand over a complete diagnosis without touching a toggle.
            census = self.last_appliance_fetch or {}
            _LOGGER.warning(
                "[%s] hOn API: 0 appliances (request OK). result keys=%s; modules keys=%s; "
                "envelope_ok=%s module_ok=%s auth_keys=%s account=%s. "
                "If the appliances appear in the hOn app, it is more likely an API change "
                "than an empty/unshared account.",
                APPLIANCE_LIST_EMPTY.label,
                result_keys,
                module_keys,
                census.get("envelope_ok"),
                census.get("module_ok"),
                census.get("auth_keys"),
                census.get("account"),
            )
            # WARNING, not DEBUG. This is the artefact the investigation asked for about
            # ten times and never got, because it required the reporter to enable a
            # toggle BEFORE a reload -- the appliance-list call happens once, during
            # setup, so turning debug on afterwards captures nothing.
            #
            # SHAPE, not the redacted body. `redact_identity` is a blacklist keyed on
            # names, and a blacklist is always one vendor spelling behind: `token` was
            # in it and `cognitoTokenNew` -- the replacement bearer credential the
            # aggregator returns inside `authInfo` -- was not. Promoting a
            # masked-by-blacklist body to a level meant for pasting into public issues
            # is the wrong trade; `structure_only` copies no leaf value at all, so
            # there is nothing left to have forgotten to mask.
            #
            # It costs almost nothing HERE, which is why it is affordable: this branch
            # fires when `appliances` is `[]`, so the body carries no appliance data to
            # begin with. What survives is the envelope -- both `success` flags, the
            # shape of `payload`, how many keys `authInfo` holds -- which is precisely
            # what "why is the list empty" is asking about.
            try:
                _LOGGER.warning("hOn appliance response shape: %s", structure_only(result))
            except Exception:  # noqa: BLE001 - a log line must never abort a setup
                _LOGGER.warning("hOn appliance response shape: <unreadable>", exc_info=True)
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
