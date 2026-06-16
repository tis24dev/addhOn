"""Idempotency and best-effort tests for the HonParameterEnum BABYCARE patch.

Covers the fix (commit "make HonParameterEnum patch idempotent and
thread-safe"): the monkey-patch now lives in a module-level, once-only
_ensure_enum_patch(). Repeated setup/reauth cycles must NOT re-wrap pyhon's
global enum setter, the patched semantics (accept values already in _values)
must hold, and a failed best-effort attempt must leave the flag clear so a
later setup can retry.

Uses stdlib unittest with a stubbed pyhon, so no Home Assistant install needed.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _mod(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        sys.modules[name] = module
    return module


def _install_min_ha_stubs() -> None:
    """Minimal HA stubs so importing the package __init__ succeeds without HA."""
    ha = _mod("homeassistant")
    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(
        config_entries, "ConfigEntry", type("ConfigEntry", (), {})
    )
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exceptions = _mod("homeassistant.exceptions")
    base_err = getattr(exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exceptions.HomeAssistantError = base_err
    exceptions.ConfigEntryNotReady = getattr(
        exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {})
    )
    exceptions.ConfigEntryAuthFailed = getattr(
        exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {})
    )
    helpers = _mod("homeassistant.helpers")
    update_coordinator = _mod("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = getattr(
        update_coordinator, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {})
    )
    update_coordinator.UpdateFailed = getattr(
        update_coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {})
    )
    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    helpers.update_coordinator = update_coordinator


_install_min_ha_stubs()

# pyhon è vendorizzato sotto questo namespace e _ensure_enum_patch importa da lì.
# Stubbiamo l'intera catena in sys.modules così l'import del patch non esegue i
# veri __init__ vendorizzati (che tirerebbero dentro aiohttp/awsiotsdk).
_VENDOR_ENUM = "custom_components.haier_hon._vendor.pyhon.parameter.enum"
_VENDOR_STUB_CHAIN = (
    "custom_components.haier_hon._vendor",
    "custom_components.haier_hon._vendor.pyhon",
    "custom_components.haier_hon._vendor.pyhon.parameter",
    _VENDOR_ENUM,
)


def _install_buggy_pyhon() -> type:
    """Install a stub pyhon whose enum setter rejects anything except 'OK'.

    Mirrors the real pyhon bug: a value present in _values (e.g. BABYCARE) is
    still rejected by the original setter, which is exactly what the patch fixes.
    """
    class HonParameterEnum:
        def __init__(self) -> None:
            self._value = None
            self._values = ["BABYCARE"]

        def _get(self):
            return self._value

        def _set(self, value):
            if value != "OK":
                raise ValueError("bad value")
            self._value = value

        value = property(_get, _set)

    enum_mod = types.ModuleType(_VENDOR_ENUM)
    enum_mod.HonParameterEnum = HonParameterEnum
    for name in _VENDOR_STUB_CHAIN[:-1]:
        sys.modules[name] = types.ModuleType(name)
    sys.modules[_VENDOR_ENUM] = enum_mod
    return HonParameterEnum


class EnsureEnumPatchTest(unittest.TestCase):
    def setUp(self) -> None:
        import custom_components.haier_hon.hon_client as hon_client

        self.hc = hon_client
        # The applied flag is process-global; reset it so each test is isolated.
        self.hc._ENUM_PATCH_APPLIED = False
        self.HonParameterEnum = _install_buggy_pyhon()

    def tearDown(self) -> None:
        self.hc._ENUM_PATCH_APPLIED = False
        # Rimuovi gli stub dal sys.modules globale così non shadowano i veri
        # moduli vendorizzati per gli altri test.
        for name in _VENDOR_STUB_CHAIN:
            sys.modules.pop(name, None)

    def test_patch_sets_applied_flag(self) -> None:
        self.assertFalse(self.hc._ENUM_PATCH_APPLIED)
        self.hc._ensure_enum_patch()
        self.assertTrue(self.hc._ENUM_PATCH_APPLIED)

    def test_idempotent_no_rewrap_after_repeated_calls(self) -> None:
        self.hc._ensure_enum_patch()
        first_setter = self.HonParameterEnum.value.fset
        for _ in range(5):  # simulate initial setup + several reauth cycles
            self.hc._ensure_enum_patch()
        self.assertIs(
            self.HonParameterEnum.value.fset,
            first_setter,
            "setter was re-wrapped: patch is not idempotent",
        )

    def test_patched_setter_accepts_value_already_in_values(self) -> None:
        self.hc._ensure_enum_patch()
        inst = self.HonParameterEnum()
        inst.value = "BABYCARE"  # rejected by original setter, accepted via fallback
        self.assertEqual("BABYCARE", inst._value)

    def test_patched_setter_passes_through_valid_value(self) -> None:
        self.hc._ensure_enum_patch()
        inst = self.HonParameterEnum()
        inst.value = "OK"
        self.assertEqual("OK", inst._value)

    def test_patched_setter_still_rejects_unknown_value(self) -> None:
        self.hc._ensure_enum_patch()
        inst = self.HonParameterEnum()
        with self.assertRaises(ValueError):
            inst.value = "NOPE"

    def test_failure_keeps_flag_false_for_retry(self) -> None:
        # If pyhon's shape is unexpected, the patch must fail soft and leave the
        # flag clear so a later setup_sync can retry.
        sys.modules[_VENDOR_ENUM] = types.ModuleType(_VENDOR_ENUM)
        self.hc._ENUM_PATCH_APPLIED = False
        with self.assertLogs(self.hc._LOGGER.name, level="WARNING"):
            self.hc._ensure_enum_patch()
        self.assertFalse(self.hc._ENUM_PATCH_APPLIED)


if __name__ == "__main__":
    unittest.main()
