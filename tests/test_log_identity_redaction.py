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
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "addhon"

_LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}

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
    }
)

# rel path -> (forbidden bare Name ids, forbidden bare Attribute attrs)
_FILES = {
    "base_entity.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "select.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "switch.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "button.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "number.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "sensor.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "binary_sensor.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "climate.py": (_ENTITY_NAMES, _ENTITY_ATTRS),
    "diagnostics.py": (frozenset({"appliance_id"}), frozenset()),
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


class LogIdentityRedactionTest(unittest.TestCase):
    def test_no_raw_identity_in_logger_calls(self) -> None:
        offenders: list[str] = []
        for rel, (names, attrs) in _FILES.items():
            path = COMPONENT / rel
            self.assertTrue(path.is_file(), f"missing source file: {rel}")
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not _is_logger_call(node):
                    continue
                for arg in node.args:
                    label = _bare_offender(arg, names, attrs) or _dict_dump_offender(arg)
                    if label:
                        offenders.append(f"{rel}:{node.lineno}: raw {label}")
        self.assertEqual(
            [],
            offenders,
            "raw device identity passed to _LOGGER (wrap it in redact_id/"
            "redact_identity):\n" + "\n".join(offenders),
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


if __name__ == "__main__":
    unittest.main()
