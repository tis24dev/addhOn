# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Static guard for the #32/#34/#35 cluster: device identity must never reach the
logs raw.

Redacting a value wraps it in a `redact_*(...)` call, which turns the argument node
into an `ast.Call`; a *bare* identity reference (a `Name`/`Attribute`) passed straight
to `_LOGGER.*` is the leak. This AST guard fails on any such bare argument, so it both
proves the cluster fix and catches future regressions (a new log line that forgets to
redact). Pure AST, no Home Assistant import required.

Scope/limitation: this checks the TOP-LEVEL argument node only, which fits the
entity-layer + diagnostics + mqtt logs (all simple `%s, <ref>` forms). It deliberately
does NOT cover hon_client.py, whose identity passes through helper calls and f-strings
(`_get_name(a)`, `f"name={...}"`) that a top-level check can't reason about; those are
covered behaviorally by test_hon_client_realtime.DiscoveryLogRedactionTest.

Beyond bare Name/Attribute refs, a second check (`_identity_call_offender`) forbids the
appliance NAME/NICKNAME being resolved inline for a NON-gated log: `<x>.get("name")` /
`_get_name(...)` passed to `_LOGGER.info/.warning/.error/.exception/.critical`. That is
the exact leak class found on v5.2.0 (the nickname reached home-assistant.log at INFO,
which the debug toggles do not gate). The SAME pattern at `_LOGGER.debug` is allowed on
purpose (debug is gated), so this check is level-aware and skips `.debug`. A redacted
form `redact_id(_get_name(a))` is a `redact_*` Call at the top level and is not flagged.
"""
from __future__ import annotations

import ast
import json
import logging
import time
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from tests._golden import install_stubs

install_stubs()

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "addhon"

_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}

# Dict keys / helpers that resolve the appliance nickname (identity). _get_name()
# in hon_client.py prefers nick_name/nickName, and coordinator data["name"] is that
# same nickname; logging either in clear at a non-gated level is a leak.
_IDENTITY_GET_KEYS = frozenset({"name", "nick_name", "nickName", "nickname"})

# Entity layer: the appliance id (MAC/serial/code), the entity unique_id, and the
# raw device-identity attributes a future log line might reach for directly.
_ENTITY_NAMES = frozenset({"appliance_id", "aid"})
_ENTITY_ATTRS = frozenset(
    {
        "_appliance_id",
        "_attr_unique_id",
        "mac_address",
        "serial_number",
        "_serial",
        "nick_name",
        "nickName",  # camelCase sibling of nick_name (_get_name reads both via getattr)
    }
)

# rel path -> (forbidden bare Name ids, forbidden bare Attribute attrs)
_FILES = {
    "base_entity.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "select.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "switch.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "button.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "number.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "program_options.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "sensor.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "binary_sensor.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "climate.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    # Setup orchestration: the legacy-entity cleanup logs a registry entry's
    # entity_id (object_id = nickname slug) / unique_id (MAC-derived). They must go
    # through redact_id(...). The 2026-06-25 INFO leak was a bare reg_entry.entity_id
    # here; this registration (absent before) gives forward protection.
    "__init__.py": (
        _ENTITY_NAMES,
        _ENTITY_ATTRS | frozenset({"entity_id", "unique_id"}),
    ),
    # The dump builder walks the entity registry like __init__.py does, so it gets
    # the same forbidden attributes: entity_id embeds the nickname slug and
    # unique_id embeds the appliance id. `row` is the loop variable it walks with.
    # Both spellings are forbidden: the registry walk reads them as attributes
    # (row.unique_id) and immediately binds them as plain locals, so an
    # attribute-only rule would let a later `_LOGGER.debug("uid=%s", unique_id)`
    # straight through.
    "diagnostics.py": (
        frozenset({"appliance_id", "row", "entries", "entity_id", "unique_id"}),
        frozenset({"entity_id", "unique_id"}),
    ),
    # Setup orchestration: the raw cloud appliance dict (CR#2 malformed-appliance
    # log). It must never be passed bare to _LOGGER -- key-name redaction cannot mask
    # nested identity (attributes[].parValue), so the malformed path logs structure
    # only (field names + error type). This guards against a future bare-dict log.
    "client/session.py": (frozenset({"appliance", "appliance_data"}), frozenset()),
    # MQTT handler: the whole parsed payload dict, the topic (embeds the MAC) and
    # the appliance nick_name.
    "client/transport/mqtt.py": (
        frozenset({"payload", "topic", "parameter"}),
        frozenset({"nick_name"}),
    ),
    # Auth/login: the new DEBUG breadcrumbs must log STRUCTURE only. Forbid passing any
    # raw page text / URL / href / remoting entry / OTP code bare to _LOGGER (the
    # remoting summary goes through redact_remoting_summary(...), a Call, so it is fine).
    "client/transport/auth.py": (
        frozenset(
            {
                "text", "prog_text", "prog_url", "href", "code", "entry", "body",
                "url", "done_url", "login_url", "redirect", "token_url", "payload",
                # token-bearing locals: a future bare log of these would be a leak
                "tokens", "t", "data", "params", "new_refresh", "result",
                "id_token", "access_token", "refresh_token", "loaded_str",
                "device_payload", "headers", "descriptor", "context",
                "r1", "r2", "match", "challenge", "device", "email", "password",
            }
        ),
        frozenset(
            {"access_token", "refresh_token", "id_token", "cognito_token",
             "_email", "_password", "_fw_uid"}
        ),
    ),
}


def _is_logger_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOG_METHODS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "_LOGGER"
    )


def _bare_offender(arg: ast.AST, names: frozenset, attrs: frozenset) -> str | None:
    """Label the arg if it is a BARE identity reference, else None.

    Only the top-level arg node is inspected: a redacted value is an `ast.Call`
    (`redact_id(appliance_id)`, `payload.get(...)`), so it is never flagged; a bare
    `appliance_id` / `self._appliance_id` / `appliance.nick_name` / `payload` is."""
    if isinstance(arg, ast.Name) and arg.id in names:
        return arg.id
    if isinstance(arg, ast.Attribute) and arg.attr in attrs:
        return f".{arg.attr}"
    return None


def _dict_dump_offender(arg: ast.AST) -> str | None:
    """Label the arg if it is `dict(<name>)` -- a raw mapping dumped to a log.

    `_bare_offender` only inspects top-level Name/Attribute nodes, so a `dict(store)`
    (an ast.Call) slips through while still dumping the mapping's raw KEYS (e.g. the
    MAC-derived appliance ids keying PROGRAM_PENDING_STORE -- CR#1). Any
    `dict(<single bare Name>)` with no kwargs passed to _LOGGER must instead go through
    a redactor (debug_utils.redact_store), so flag it. A dict LITERAL `{...}` or a
    `redact_store(store)` call is a different node and is not flagged."""
    if (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Name)
        and arg.func.id == "dict"
        and not arg.keywords
        and len(arg.args) == 1
        and isinstance(arg.args[0], ast.Name)
    ):
        return f"dict({arg.args[0].id})"
    return None


def _identity_call_offender(arg: ast.AST) -> str | None:
    """Label the arg if it resolves the appliance NAME/NICKNAME inline, else None.

    Catches `<x>.get("name"|"nick_name"|...)` and `_get_name(...)` -- the nickname
    leak class. The caller applies this ONLY to non-gated logs (everything but
    `_LOGGER.debug`): the nickname in clear is acceptable at DEBUG (gated by the
    toggles) but not at INFO+ (always in home-assistant.log). A redacted form like
    `redact_id(_get_name(a))` has `redact_id(...)` as its TOP-LEVEL node, so it is
    not flagged (only the outermost call is inspected, as everywhere in this guard)."""
    if not isinstance(arg, ast.Call):
        return None
    if isinstance(arg.func, ast.Name) and arg.func.id == "_get_name":
        return "_get_name(...)"
    if (
        isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "get"
        and arg.args
        and isinstance(arg.args[0], ast.Constant)
        and arg.args[0].value in _IDENTITY_GET_KEYS
    ):
        return f'.get("{arg.args[0].value}")'
    return None


class LogIdentityRedactionTest(unittest.TestCase):
    def test_command_event_redacts_identity_and_bounds_record(self) -> None:
        from custom_components.addhon.command_diagnostics import emit_command_event

        secrets = {
            "email": "private@example.invalid",
            "password": "private-password",
            "token": "private-token",
            "mac": "AA:BB:CC:DD:EE:FF",
            "serial": "PRIVATE-SERIAL",
            "cloud_id": "PRIVATE-CLOUD-ID",
        }
        fields = {
            **secrets,
            "nested": {
                "identity": {
                    "serialNumber": "PRIVATE-NESTED-SERIAL",
                    "access_token": "PRIVATE-NESTED-TOKEN",
                }
            },
            "many_keys": {f"key-{index:03d}": index for index in range(200)},
            "oversized": "x" * (10 * 1024),
        }

        with self.assertLogs(
            "custom_components.addhon.command_diagnostics",
            level="DEBUG",
        ) as captured:
            emit_command_event("command_payload", fields)

        self.assertEqual(len(captured.records), 1)
        record = captured.records[0].getMessage()
        decoded = json.loads(record)
        self.assertEqual(decoded["event"], "command_payload")
        self.assertLessEqual(len(record), 4096)
        self.assertLessEqual(len(decoded["many_keys"]), 80)
        self.assertLessEqual(len(decoded["oversized"]), 512)
        for secret in (
            *secrets.values(),
            "PRIVATE-NESTED-SERIAL",
            "PRIVATE-NESTED-TOKEN",
        ):
            self.assertNotIn(secret, record)

    def test_command_event_is_silent_when_debug_is_disabled(self) -> None:
        from custom_components.addhon.command_diagnostics import emit_command_event

        logger = logging.getLogger("custom_components.addhon.command_diagnostics")
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            with patch.object(logger, "debug") as debug:
                emit_command_event("command_intent", {"action": "set_mode"})
        finally:
            logger.setLevel(previous_level)

        debug.assert_not_called()

    def test_command_event_failure_uses_only_fixed_message(self) -> None:
        import custom_components.addhon.command_diagnostics as diagnostics

        secret = "PRIVATE-RAW-SECRET"
        with (
            patch.object(
                diagnostics,
                "redact_identity",
                side_effect=RuntimeError(secret),
            ),
            self.assertLogs(
                "custom_components.addhon.command_diagnostics",
                level="DEBUG",
            ) as captured,
        ):
            diagnostics.emit_command_event("command_payload", {"token": secret})

        self.assertEqual(
            [record.getMessage() for record in captured.records],
            ["command diagnostic event failed"],
        )
        self.assertNotIn(secret, "\n".join(captured.output))

    def test_no_raw_identity_in_logger_calls(self) -> None:
        offenders: list[str] = []
        for rel, (names, attrs) in _FILES.items():
            path = COMPONENT / rel
            self.assertTrue(path.is_file(), f"missing source file: {rel}")
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not _is_logger_call(node):
                    continue
                # Nickname in clear is allowed at DEBUG (gated by the toggles), not at
                # INFO+ (always written). The bare Name/Attr and dict-dump checks apply
                # at every level (a raw id must never be logged, even at debug).
                gated = node.func.attr == "debug"
                for arg in node.args:
                    label = _bare_offender(arg, names, attrs) or _dict_dump_offender(arg)
                    if not label and not gated:
                        label = _identity_call_offender(arg)
                    if label:
                        offenders.append(f"{rel}:{node.lineno}: raw {label}")
        self.assertEqual(
            [],
            offenders,
            "raw device identity passed to _LOGGER (wrap it in redact_id/"
            "redact_identity):\n" + "\n".join(offenders),
        )

    def test_auth_diagnostic_logger_never_references_raw_inputs(self) -> None:
        """The opt-in WARNING dump may use only already-sanitized local values."""
        path = COMPONENT / "client/auth_diagnostics.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        raw_names = {
            "body",
            "content_type",
            "content_type_values",
            "error",
            "headers",
            "hrefs",
            "location_values",
            "message",
            "source",
            "text",
            "url",
            "value",
        }
        offenders = []
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in _LOG_METHODS
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "logger"
            ):
                continue
            for argument in (*call.args, *(item.value for item in call.keywords)):
                for node in ast.walk(argument):
                    if isinstance(node, ast.Name) and node.id in raw_names:
                        offenders.append((call.lineno, node.id))

        self.assertEqual(
            [],
            offenders,
            "auth diagnostic logger references raw input variables",
        )

    def test_guard_actually_detects_a_leak(self) -> None:
        # Meta: prove the guard is not vacuous (would catch a regression).
        leak = ast.parse('_LOGGER.debug("x %s", appliance_id)').body[0].value
        self.assertTrue(_is_logger_call(leak))
        self.assertEqual(
            _bare_offender(leak.args[1], _ENTITY_NAMES, _ENTITY_ATTRS), "appliance_id"
        )
        safe = ast.parse('_LOGGER.debug("x %s", redact_id(appliance_id))').body[0].value
        self.assertIsNone(
            _bare_offender(safe.args[1], _ENTITY_NAMES, _ENTITY_ATTRS)
        )

    def test_guard_detects_dict_dump_leak(self) -> None:
        # Meta: the CR#1 class -- `dict(store)` dumped to a log -- is caught, while a
        # redacted dump or a dict literal is not.
        leak = ast.parse('_LOGGER.debug("x %s", dict(store))').body[0].value
        self.assertEqual(_dict_dump_offender(leak.args[1]), "dict(store)")
        safe = ast.parse('_LOGGER.debug("x %s", redact_store(store))').body[0].value
        self.assertIsNone(_dict_dump_offender(safe.args[1]))
        literal = ast.parse('_LOGGER.debug("x %s", {"k": v})').body[0].value
        self.assertIsNone(_dict_dump_offender(literal.args[1]))

    def test_guard_detects_identity_call_leak(self) -> None:
        # Meta: the nickname-resolving call class is caught, while a redacted form,
        # a non-identity key, and a non-call arg are not.
        get_leak = ast.parse('_LOGGER.info("x %s", data.get("name"))').body[0].value
        self.assertEqual(_identity_call_offender(get_leak.args[1]), '.get("name")')
        nick_leak = ast.parse('_LOGGER.warning("x %s", a.get("nick_name"))').body[0].value
        self.assertEqual(_identity_call_offender(nick_leak.args[1]), '.get("nick_name")')
        fn_leak = ast.parse('_LOGGER.error("x %s", _get_name(a))').body[0].value
        self.assertEqual(_identity_call_offender(fn_leak.args[1]), "_get_name(...)")
        safe = ast.parse('_LOGGER.info("x %s", redact_id(_get_name(a)))').body[0].value
        self.assertIsNone(_identity_call_offender(safe.args[1]))
        benign = ast.parse('_LOGGER.info("x %s", d.get("count"))').body[0].value
        self.assertIsNone(_identity_call_offender(benign.args[1]))

    def test_nickname_call_allowed_at_debug_not_at_info(self) -> None:
        # Meta: the level gate. The SAME data.get("name") arg is a leak at INFO but
        # allowed at DEBUG (gated). Mirrors the loop's `gated = attr == "debug"`.
        for method, gated in (("debug", True), ("info", False), ("warning", False)):
            call = ast.parse(f'_LOGGER.{method}("x %s", data.get("name"))').body[0].value
            flagged = (not gated) and _identity_call_offender(call.args[1]) is not None
            self.assertEqual(flagged, not gated, f"method={method}")


class CommandDiagnosticReviewTest(unittest.TestCase):
    _LOGGER_NAME = "custom_components.addhon.command_diagnostics"

    def test_command_event_astral_name_is_valid_bounded_json(self) -> None:
        from custom_components.addhon.command_diagnostics import emit_command_event

        with self.assertLogs(self._LOGGER_NAME, level="DEBUG") as captured:
            emit_command_event("\U0001f600" * 512, {"action": "set_mode"})

        record = captured.records[0].getMessage()
        decoded = json.loads(record)
        self.assertIn(
            decoded["event"],
            {
                "command_intent",
                "command_payload",
                "command_result",
                "shadow_update",
                "contract_check",
            },
        )
        self.assertLessEqual(len(record), 4096)
        self.assertLessEqual(len(record.encode("utf-8")), 4096)

    def test_command_event_redacts_non_dict_mapping_recursively(self) -> None:
        from custom_components.addhon.command_diagnostics import emit_command_event

        password = "PRIVATE-PROXY-PASSWORD"
        serial = "PRIVATE-PROXY-SERIAL"
        mac = "AA:BB:CC:DD:EE:FF"
        fields = MappingProxyType(
            {
                "nested": MappingProxyType(
                    {
                        "password": password,
                        "serial": serial,
                        "neutral": mac,
                    }
                )
            }
        )

        with self.assertLogs(self._LOGGER_NAME, level="DEBUG") as captured:
            emit_command_event("command_payload", fields)

        record = captured.records[0].getMessage()
        decoded = json.loads(record)
        self.assertEqual(decoded["nested"]["password"], "***")
        self.assertEqual(decoded["nested"]["serial"], "***")
        self.assertEqual(decoded["nested"]["neutral"], "***")
        self.assertNotIn(password, record)
        self.assertNotIn(serial, record)
        self.assertNotIn(mac, record)

    def test_command_event_survives_a_self_referencing_cycle(self) -> None:
        from custom_components.addhon.command_diagnostics import emit_command_event

        cyclic: dict[str, object] = {"token": "PRIVATE-CYCLE-TOKEN"}
        cyclic["self"] = cyclic

        with self.assertLogs(self._LOGGER_NAME, level="DEBUG") as captured:
            emit_command_event("command_payload", {"payload": cyclic})

        self.assertEqual(len(captured.records), 1)
        record = captured.records[0].getMessage()
        decoded = json.loads(record)
        self.assertEqual(decoded["event"], "command_payload")
        self.assertLessEqual(len(record), 4096)
        self.assertNotIn("PRIVATE-CYCLE-TOKEN", record)

    def test_command_event_survives_a_deeply_nested_structure(self) -> None:
        from custom_components.addhon.command_diagnostics import emit_command_event

        deep: object = "PRIVATE-DEEP-LEAF"
        for _ in range(2000):
            deep = {"next": deep}

        with self.assertLogs(self._LOGGER_NAME, level="DEBUG") as captured:
            emit_command_event("command_payload", {"payload": deep})

        self.assertEqual(len(captured.records), 1)
        record = captured.records[0].getMessage()
        decoded = json.loads(record)
        self.assertEqual(decoded["event"], "command_payload")
        self.assertLessEqual(len(record), 4096)
        self.assertNotIn("PRIVATE-DEEP-LEAF", record)

    def _time_payload(self, size: int) -> float:
        from custom_components.addhon.command_diagnostics import emit_command_event

        payload = {f"key-{index:07d}": index for index in range(size)}
        with self.assertLogs(self._LOGGER_NAME, level="DEBUG"):
            started = time.monotonic()
            emit_command_event("command_payload", {"payload": payload})
            return time.monotonic() - started

    def test_command_event_bounds_a_very_large_mapping_quickly(self) -> None:
        """The invariant is that the work is BOUNDED, not that it takes under some
        number of milliseconds.

        A full sort and copy of 200k items before trimming to 80 measured ~0.6s; a
        bounded traversal that samples then trims is flat regardless of the
        collection's real size. This used to assert an absolute 0.2s against a real
        measurement of well under a millisecond, which pinned nothing about the shape
        and would have gone red on a contended runner that simply stalled.

        So the large collection is compared against a SMALL one measured in the same
        process. The ratio cancels machine speed and load: flat work stays within a
        wide factor, while restoring the unbounded version puts a thousandfold between
        them. The absolute ceiling stays too, generous enough to be noise-proof, as a
        guard for the case where both measurements are pathological.
        """
        small = self._time_payload(200)
        large = self._time_payload(200_000)

        floor = 5e-5  # timer granularity; below this a ratio is meaningless
        self.assertLess(large, max(small, floor) * 50, f"{small=} {large=}")
        self.assertLess(large, 2.0)

    def test_a_very_large_mapping_is_still_trimmed(self) -> None:
        from custom_components.addhon.command_diagnostics import emit_command_event

        huge = {f"key-{index:07d}": index for index in range(200_000)}
        with self.assertLogs(self._LOGGER_NAME, level="DEBUG") as captured:
            emit_command_event("command_payload", {"payload": huge})

        record = captured.records[0].getMessage()
        decoded = json.loads(record)
        self.assertLessEqual(len(decoded["payload"]), 80)
        self.assertLessEqual(len(record), 4096)

    def test_command_event_bounds_a_large_list_and_set(self) -> None:
        from custom_components.addhon.command_diagnostics import emit_command_event

        with self.assertLogs(self._LOGGER_NAME, level="DEBUG") as captured:
            emit_command_event(
                "command_payload",
                {
                    "as_list": list(range(50_000)),
                    "as_set": set(range(50_000)),
                },
            )

        record = captured.records[0].getMessage()
        decoded = json.loads(record)
        self.assertLessEqual(len(decoded["as_list"]), 80)
        self.assertLessEqual(len(decoded["as_set"]), 80)
        self.assertLessEqual(len(record), 4096)


class ExtraIdentityKeysBehaviouralTest(unittest.TestCase):
    """`command_diagnostics._EXTRA_IDENTITY_KEYS` is the THIRD identity key set, and
    it was the thinnest-covered constant in the repository: five of its six names
    (accountid, applianceid, deviceid, uniqueid, userid) could be deleted -- one at a
    time or all five at once -- with the whole suite green at rc=0. Unlike
    `_IDENTITY_KEYS` it never even had a set-equality pin. Only `cloudid` had an
    observer, and only incidentally, because one unrelated test's record carries a
    `cloud_id` field.

    These names are not decoration: `deviceId` is the key api.py:72-76 posts the phone
    install id under (`json={"deviceId": device_id}` where device_id is
    `device.mobile_id`), the same value diagnostics.py:105-107 calls "the phone-install
    id of whoever issued the last command (often a third party)" when it arrives as
    `mobileId`.
    """

    _LOGGER_NAME = "custom_components.addhon.command_diagnostics"
    _CANARY_VALUE = "CANARY-IDENTIFIER-VALUE"

    # Named, not derived -- a loop over the constant goes quiet when a member leaves.
    _MUST_MASK = ("accountid", "applianceid", "cloudid", "deviceid", "uniqueid", "userid")

    def test_named_extra_identity_keys_are_masked_on_the_command_path(self) -> None:
        from custom_components.addhon.command_diagnostics import emit_command_event

        for key in self._MUST_MASK:
            stem = key[: -len("id")]
            # _identity_key strips every non-alphanumeric before lookup, so ONE entry
            # is meant to cover all of these spellings. That matching RULE is part of
            # the guarantee, so it is asserted here rather than assumed.
            for spelling in (key, key.upper(), f"{stem}_id", f"{stem}Id", f"{stem}-ID"):
                with self.subTest(key=spelling):
                    with self.assertLogs(self._LOGGER_NAME, level="DEBUG") as captured:
                        emit_command_event("command_payload", {spelling: self._CANARY_VALUE})
                    decoded = json.loads(captured.records[0].getMessage())
                    # Assert the VALUE, not just absence: _encode_record enforces a
                    # 4096-byte budget by DROPPING whole keys, so an assertNotIn alone
                    # could pass because the key was budgeted out rather than masked.
                    self.assertEqual("***", decoded[spelling])

    def test_every_extra_identity_key_has_a_behavioural_assertion(self) -> None:
        from custom_components.addhon import command_diagnostics

        self.assertEqual(
            set(self._MUST_MASK), set(command_diagnostics._EXTRA_IDENTITY_KEYS)
        )

    def test_non_identity_key_survives_the_command_path(self) -> None:
        # Positive control: the assertions above are not passing because the command
        # path masks or drops everything it is handed.
        from custom_components.addhon.command_diagnostics import emit_command_event

        with self.assertLogs(self._LOGGER_NAME, level="DEBUG") as captured:
            emit_command_event("command_payload", {"programName": "ECO40"})
        self.assertEqual("ECO40", json.loads(captured.records[0].getMessage())["programName"])


if __name__ == "__main__":
    unittest.main()
