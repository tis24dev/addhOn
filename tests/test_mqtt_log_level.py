"""Tests for silencing the pyhOn MQTT realtime log noise + the debug service.

pyhOn's MQTT client (logger 'pyhon.connection.mqtt') logs one INFO line per
reconnect attempt. When the realtime push slot is contended (shared appliance,
the owner holds the channel) these attempts fail in a ~20-minute loop and flood
the log, with nothing actionable on the integration side (data still updates via
polling). The integration lowers that logger to WARNING by default and exposes
the service haier_hon.set_mqtt_log_level to raise it back to debug on demand.

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
COMPONENT = ROOT / "custom_components" / "haier_hon"
LOGGING_UTILS_PATH = COMPONENT / "logging_utils.py"
INIT = COMPONENT / "__init__.py"
CONST = COMPONENT / "const.py"
SERVICES = COMPONENT / "services.yaml"


def _load_logging_utils():
    spec = importlib.util.spec_from_file_location(
        "haier_hon_logging_utils_standalone", LOGGING_UTILS_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lu = _load_logging_utils()


class ApplyLevelTest(unittest.TestCase):
    def setUp(self) -> None:
        # Salva i livelli correnti e li ripristina, così questi test non
        # inquinano il livello globale dei logger per gli altri test.
        self._saved = {
            name: logging.getLogger(name).level for name in lu.MQTT_NOISE_LOGGERS
        }

    def tearDown(self) -> None:
        for name, level in self._saved.items():
            logging.getLogger(name).setLevel(level)

    def test_noise_loggers_include_pyhon_mqtt(self) -> None:
        # pyhon è vendorizzato, quindi i suoi logger (che usano __name__) sono
        # sotto il package namespacizzato.
        self.assertIn(
            "custom_components.haier_hon._vendor.pyhon.connection.mqtt",
            lu.MQTT_NOISE_LOGGERS,
        )

    def test_levels_map_to_logging_constants(self) -> None:
        self.assertEqual(lu.MQTT_LOG_LEVELS["debug"], logging.DEBUG)
        self.assertEqual(lu.MQTT_LOG_LEVELS["info"], logging.INFO)
        self.assertEqual(lu.MQTT_LOG_LEVELS["warning"], logging.WARNING)
        self.assertEqual(lu.MQTT_LOG_LEVELS["error"], logging.ERROR)

    def test_default_level_is_warning(self) -> None:
        self.assertEqual(lu.DEFAULT_MQTT_LOG_LEVEL, logging.WARNING)

    def test_silence_sets_warning_even_from_debug(self) -> None:
        # Parte da DEBUG (rumore acceso) e verifica che il silenziamento vinca.
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


class WiringTest(unittest.TestCase):
    """Source-level guards: il service e il silenziamento devono restare cablati."""

    def test_const_declares_service_name(self) -> None:
        self.assertIn(
            'SERVICE_SET_MQTT_LOG_LEVEL = "set_mqtt_log_level"',
            CONST.read_text(encoding="utf-8"),
        )

    def test_services_yaml_defines_service_and_level_field(self) -> None:
        text = SERVICES.read_text(encoding="utf-8")
        self.assertIn("set_mqtt_log_level:", text)
        self.assertIn("level:", text)

    def test_init_silences_by_default_and_registers_service(self) -> None:
        src = INIT.read_text(encoding="utf-8")
        self.assertIn("silence_mqtt_noise", src)
        self.assertIn("_async_register_services", src)
        self.assertIn("SERVICE_SET_MQTT_LOG_LEVEL", src)
        self.assertIn("async_register", src)


if __name__ == "__main__":
    unittest.main()
