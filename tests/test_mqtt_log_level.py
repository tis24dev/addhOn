"""Tests for silencing pyhOn MQTT noise and enabling integration debug logs.

pyhOn's MQTT client (logger 'pyhon.connection.mqtt') logs one INFO line per
reconnect attempt. When the realtime push slot is contended (shared appliance,
the owner holds the channel) these attempts fail in a ~20-minute loop and flood
the log, with nothing actionable on the integration side (data still updates via
polling). The integration lowers that logger to WARNING by default and exposes
the service addhon.set_mqtt_log_level to raise it back to debug on demand.

logging_utils.py is loaded directly by file path (no intra-package imports, no
homeassistant), like test_debug_utils_redact. The wiring in __init__.py /
services.yaml / const.py is checked at source level (the stub harness can't run
async_setup_entry), matching test_coordinator_config_entry.
"""
from __future__ import annotations

import importlib.util
import logging
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "addhon"
LOGGING_UTILS_PATH = COMPONENT / "logging_utils.py"
INIT = COMPONENT / "__init__.py"
CONST = COMPONENT / "const.py"
SERVICES = COMPONENT / "services.yaml"


def _load_logging_utils():
    spec = importlib.util.spec_from_file_location(
        "addhon_logging_utils_standalone", LOGGING_UTILS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lu = _load_logging_utils()


class ApplyMqttLevelTest(unittest.TestCase):
    def setUp(self) -> None:
        # Save the current levels and restore them, so these tests do not
        # pollute the global logger levels for the other tests.
        self._saved = {
            name: logging.getLogger(name).level for name in lu.MQTT_NOISE_LOGGERS
        }

    def tearDown(self) -> None:
        for name, level in self._saved.items():
            logging.getLogger(name).setLevel(level)

    def test_noise_loggers_are_native_only(self) -> None:
        # The MQTT client is OURS (pyhOn deleted in Phase 4): the only noise logger
        # is the native one; no leftover _vendor/pyhon namespace.
        self.assertEqual(
            lu.MQTT_NOISE_LOGGERS, ("custom_components.addhon.client.transport.mqtt",)
        )

    def test_levels_map_to_logging_constants(self) -> None:
        self.assertEqual(lu.MQTT_LOG_LEVELS["debug"], logging.DEBUG)
        self.assertEqual(lu.MQTT_LOG_LEVELS["info"], logging.INFO)
        self.assertEqual(lu.MQTT_LOG_LEVELS["warning"], logging.WARNING)
        self.assertEqual(lu.MQTT_LOG_LEVELS["error"], logging.ERROR)

    def test_default_level_is_warning(self) -> None:
        self.assertEqual(lu.DEFAULT_MQTT_LOG_LEVEL, logging.WARNING)

    def test_silence_sets_warning_even_from_debug(self) -> None:
        # Start from DEBUG (noise on) and verify that the silencing wins.
        for name in lu.MQTT_NOISE_LOGGERS:
            logging.getLogger(name).setLevel(logging.DEBUG)
        lu.silence_mqtt_noise()
        for name in lu.MQTT_NOISE_LOGGERS:
            self.assertEqual(logging.getLogger(name).level, logging.WARNING)

    def test_apply_raises_back_to_debug(self) -> None:
        lu.silence_mqtt_noise()
        lu.apply_mqtt_log_level(logging.DEBUG)
        for name in lu.MQTT_NOISE_LOGGERS:
            self.assertEqual(logging.getLogger(name).level, logging.DEBUG)


class ApplyIntegrationLevelTest(unittest.TestCase):
    def setUp(self) -> None:
        # Save the levels of BOTH sets: the silence_mqtt test also mutates the
        # MQTT loggers, which must be restored so the other tests are not dirtied.
        names = set(lu.INTEGRATION_DEBUG_LOGGERS) | set(lu.MQTT_NOISE_LOGGERS)
        self._saved = {name: logging.getLogger(name).level for name in names}

    def tearDown(self) -> None:
        for name, level in self._saved.items():
            logging.getLogger(name).setLevel(level)

    def test_integration_debug_loggers_native_only(self) -> None:
        # pyhOn deleted: the only namespace is the integration's native one.
        self.assertEqual(lu.INTEGRATION_DEBUG_LOGGERS, ("custom_components.addhon",))

    def test_apply_integration_log_level_sets_all_debug_loggers(self) -> None:
        lu.apply_integration_log_level(logging.DEBUG)
        for name in lu.INTEGRATION_DEBUG_LOGGERS:
            self.assertEqual(logging.getLogger(name).level, logging.DEBUG)

    def test_apply_integration_log_level_does_not_override_mqtt_noise_level(self) -> None:
        lu.silence_mqtt_noise()
        lu.apply_integration_log_level(logging.DEBUG)
        for name in lu.MQTT_NOISE_LOGGERS:
            self.assertEqual(logging.getLogger(name).level, logging.WARNING)


class WiringTest(unittest.TestCase):
    """Source-level guards: the service and the silencing must stay wired."""

    def test_const_declares_service_name(self) -> None:
        self.assertIn(
            'SERVICE_SET_MQTT_LOG_LEVEL = "set_mqtt_log_level"',
            CONST.read_text(encoding="utf-8"),
        )
        self.assertIn(
            'SERVICE_SET_LOG_LEVEL = "set_log_level"',
            CONST.read_text(encoding="utf-8"),
        )

    def test_services_yaml_defines_service_and_level_field(self) -> None:
        text = SERVICES.read_text(encoding="utf-8")
        self.assertIn("set_mqtt_log_level:", text)
        self.assertIn("set_log_level:", text)
        self.assertIn("level:", text)

    def test_init_silences_by_default_and_registers_service(self) -> None:
        src = INIT.read_text(encoding="utf-8")
        self.assertIn("silence_mqtt_noise", src)
        self.assertIn("apply_integration_log_level", src)
        self.assertIn("_async_register_services", src)
        self.assertIn("SERVICE_SET_MQTT_LOG_LEVEL", src)
        self.assertIn("SERVICE_SET_LOG_LEVEL", src)
        self.assertIn("async_register", src)


if __name__ == "__main__":
    unittest.main()
