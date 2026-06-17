"""Tests for the Tier 3 number platform (writable temperature setpoints).

Modellati sullo schema REALE del frigo (dump pyhОn, REF HDPW5620CNPK): comando
`settings` con i parametri range tempSelZ1[2..8], tempSelZ2[-24..-16],
tempSelZ3[0..5]; nessun Z4/UZ/LZ. Verifica:
- capability-gating: si creano solo i setpoint presenti come parametri scrivibili;
- range (min/max/step) letti dal parametro REALE a runtime, non hardcoded;
- native_value letto dallo shadow (attributes);
- async_set_native_value invia il comando `settings` impostando il parametro
  (intero quando il valore è intero), via il sender generico hon_commands.

Stdlib unittest con stub Home Assistant inline (no install HA richiesto). Stub
getattr-guarded per coesistere con gli altri moduli di test nel processo pytest.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
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


def _install_homeassistant_stubs() -> None:
    ha = _mod("homeassistant")

    config_entries = _mod("homeassistant.config_entries")
    config_entries.ConfigEntry = getattr(config_entries, "ConfigEntry", type("ConfigEntry", (), {}))

    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))

    exceptions = _mod("homeassistant.exceptions")
    base_err = getattr(exceptions, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exceptions.HomeAssistantError = base_err
    exceptions.ConfigEntryNotReady = getattr(exceptions, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base_err,), {}))
    exceptions.ConfigEntryAuthFailed = getattr(exceptions, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base_err,), {}))

    helpers = _mod("homeassistant.helpers")
    entity = _mod("homeassistant.helpers.entity")
    entity.DeviceInfo = getattr(entity, "DeviceInfo", dict)
    entity_platform = _mod("homeassistant.helpers.entity_platform")
    entity_platform.AddEntitiesCallback = getattr(entity_platform, "AddEntitiesCallback", object)

    update_coordinator = _mod("homeassistant.helpers.update_coordinator")

    class CoordinatorEntity:
        def __init__(self, coordinator) -> None:
            self.coordinator = coordinator
            self.hass = getattr(coordinator, "hass", None)

        def async_write_ha_state(self) -> None:
            self.state_writes = getattr(self, "state_writes", 0) + 1

    update_coordinator.CoordinatorEntity = getattr(update_coordinator, "CoordinatorEntity", CoordinatorEntity)
    update_coordinator.DataUpdateCoordinator = getattr(update_coordinator, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    update_coordinator.UpdateFailed = getattr(update_coordinator, "UpdateFailed", type("UpdateFailed", (Exception,), {}))

    components = _mod("homeassistant.components")
    number_mod = _mod("homeassistant.components.number")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class NumberEntityDescription:
        key: str
        name: str | None = None
        icon: str | None = None
        device_class: object | None = None
        native_unit_of_measurement: str | None = None
        native_min_value: float | None = None
        native_max_value: float | None = None
        native_step: float | None = None
        mode: object | None = None

    class NumberEntity:
        pass

    class NumberDeviceClass:
        TEMPERATURE = "temperature"

    class NumberMode:
        AUTO = "auto"
        BOX = "box"
        SLIDER = "slider"

    number_mod.NumberEntityDescription = getattr(number_mod, "NumberEntityDescription", NumberEntityDescription)
    number_mod.NumberEntity = getattr(number_mod, "NumberEntity", NumberEntity)
    number_mod.NumberDeviceClass = getattr(number_mod, "NumberDeviceClass", NumberDeviceClass)
    number_mod.NumberMode = getattr(number_mod, "NumberMode", NumberMode)

    const = _mod("homeassistant.const")

    class UnitOfTemperature:
        CELSIUS = "°C"

    const.UnitOfTemperature = getattr(const, "UnitOfTemperature", UnitOfTemperature)

    ha.config_entries = config_entries
    ha.core = core
    ha.exceptions = exceptions
    ha.helpers = helpers
    ha.components = components
    ha.const = const
    helpers.entity = entity
    helpers.entity_platform = entity_platform
    helpers.update_coordinator = update_coordinator
    components.number = number_mod


_install_homeassistant_stubs()


class RangeParam:
    """Mima HonParameterRange: min/max/step + value che applica str_to_float di
    pyhОn (int() prima, cattura solo ValueError -> un float frazionario verrebbe
    troncato; una stringa "5.5" resta 5.5). Serve a testare il fix del troncamento."""

    def __init__(self, value, mn, mx, step) -> None:
        self.min = mn
        self.max = mx
        self.step = step
        self._v = self._coerce(value)

    @staticmethod
    def _coerce(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return float(str(v).replace(",", "."))

    @property
    def value(self):
        return self._v

    @value.setter
    def value(self, v):
        fv = self._coerce(v)
        # Stessa validazione di HonParameterRange: fuori range o fuori griglia
        # (step) -> ValueError, così il path fail-closed è esercitato davvero.
        if not (self.min <= fv <= self.max) or ((fv - self.min) * 100) % (self.step * 100):
            raise ValueError(f"Allowed: [{self.min}..{self.max}] step {self.step} But was: {fv}")
        self._v = fv


class RecordingCommand:
    def __init__(self, parameters) -> None:
        self.parameters = parameters
        self.send_calls = 0
        self.sent = None

    async def send(self) -> None:
        self.send_calls += 1
        self.sent = {k: p.value for k, p in self.parameters.items()}


class FakeAppliance:
    def __init__(self, commands) -> None:
        self.commands = commands


class FakeClient:
    def run_command_sync(self, coro) -> None:
        asyncio.run(coro)


class FakeCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data
        self.hass = None
        self.refreshes = 0
        self.last_update_success = True
        self.last_exception = None

    async def async_refresh(self) -> None:
        self.refreshes += 1

    async def async_request_refresh(self) -> None:
        self.refreshes += 1


class FakeHass:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}

    async def async_add_executor_job(self, func, *args):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(func, *args).result(timeout=5)


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id


def _fridge_commands() -> dict:
    """Comando `settings` come nel dump reale: solo Z1/Z2/Z3."""
    return {
        "settings": RecordingCommand(
            {
                "tempSelZ1": RangeParam(5, 2, 8, 1),
                "tempSelZ2": RangeParam(-18, -24, -16, 1),
                "tempSelZ3": RangeParam(1, 0, 5, 1),
            }
        )
    }


async def _build(app_type: str, appliance, attributes: dict, client=None) -> list:
    from custom_components.haier_hon import number
    from custom_components.haier_hon.const import DOMAIN

    data = {
        "x-1": {
            "type": app_type,
            "name": "Frigo",
            "attributes": attributes,
            "appliance": appliance,
        }
    }
    coordinator = FakeCoordinator(data)
    hass = FakeHass({DOMAIN: {"entry-1": {"coordinator": coordinator, "client": client}}})
    added: list = []
    await number.async_setup_entry(hass, FakeEntry(), added.extend)
    for ent in added:
        ent.hass = hass
    return added


class NumberSetpointTest(unittest.TestCase):
    def test_gating_only_present_setpoints(self) -> None:
        app = FakeAppliance(_fridge_commands())
        attrs = {"tempSelZ1": "5", "tempSelZ2": "-18", "tempSelZ3": "1"}
        added = asyncio.run(_build("REF", app, attrs))
        keys = sorted(e.entity_description.key for e in added)
        self.assertEqual(
            keys,
            ["target_temp_zone1", "target_temp_zone2", "target_temp_zone3"],
        )

    def test_range_read_from_runtime_param(self) -> None:
        app = FakeAppliance(_fridge_commands())
        added = asyncio.run(_build("REF", app, {}))
        by_key = {e.entity_description.key: e for e in added}
        z1 = by_key["target_temp_zone1"]
        self.assertEqual((z1.native_min_value, z1.native_max_value, z1.native_step), (2.0, 8.0, 1.0))
        z2 = by_key["target_temp_zone2"]
        self.assertEqual((z2.native_min_value, z2.native_max_value), (-24.0, -16.0))

    def test_native_value_from_shadow(self) -> None:
        app = FakeAppliance(_fridge_commands())
        attrs = {"tempSelZ1": "5", "tempSelZ2": "-18", "tempSelZ3": "1"}
        added = asyncio.run(_build("REF", app, attrs))
        by_key = {e.entity_description.key: e for e in added}
        self.assertEqual(by_key["target_temp_zone1"].native_value, 5.0)
        self.assertEqual(by_key["target_temp_zone2"].native_value, -18.0)

    def test_set_native_value_sends_command_as_int(self) -> None:
        commands = _fridge_commands()
        app = FakeAppliance(commands)
        client = FakeClient()
        attrs = {"tempSelZ1": "5", "tempSelZ2": "-18", "tempSelZ3": "1"}
        added = asyncio.run(_build("REF", app, attrs, client=client))
        z1 = next(e for e in added if e.entity_description.key == "target_temp_zone1")
        asyncio.run(z1.async_set_native_value(4.0))
        settings = commands["settings"]
        self.assertEqual(settings.send_calls, 1)
        # tempSelZ1 impostato a 4 INTERO (non 4.0); gli altri invariati.
        self.assertEqual(settings.parameters["tempSelZ1"].value, 4)
        self.assertEqual(settings.parameters["tempSelZ2"].value, -18)

    def test_fractional_value_not_truncated(self) -> None:
        # Regression sul fix troncamento: device con step 0.5 -> 12.5 resta 12.5.
        commands = {"settings": RecordingCommand({"tempSel": RangeParam(10, 5, 20, 0.5)})}
        app = FakeAppliance(commands)
        client = FakeClient()
        added = asyncio.run(_build("WC", app, {"tempSel": "10"}, client=client))
        ent = next(e for e in added if e.entity_description.key == "target_temp")
        asyncio.run(ent.async_set_native_value(12.5))
        self.assertEqual(commands["settings"].parameters["tempSel"].value, 12.5)
        # E un valore intero resta intero pulito (no 13.0).
        asyncio.run(ent.async_set_native_value(13.0))
        self.assertEqual(commands["settings"].parameters["tempSel"].value, 13)

    def test_off_grid_value_fails_closed(self) -> None:
        # Metà portante del fix: un valore fuori griglia (step 0.5) deve far
        # fallire in modo pulito (HomeAssistantError), con rollback e nessun send.
        from homeassistant.exceptions import HomeAssistantError

        commands = {"settings": RecordingCommand({"tempSel": RangeParam(10, 5, 20, 0.5)})}
        app = FakeAppliance(commands)
        client = FakeClient()
        added = asyncio.run(_build("WC", app, {"tempSel": "10"}, client=client))
        ent = next(e for e in added if e.entity_description.key == "target_temp")
        with self.assertRaises(HomeAssistantError):
            asyncio.run(ent.async_set_native_value(12.3))  # fuori dalla griglia 0.5
        self.assertEqual(commands["settings"].parameters["tempSel"].value, 10)  # rollback
        self.assertEqual(commands["settings"].send_calls, 0)

    def test_fourth_zone_appears_when_present(self) -> None:
        commands = _fridge_commands()
        commands["settings"].parameters["tempSelZ4"] = RangeParam(0, -2, 4, 1)
        app = FakeAppliance(commands)
        added = asyncio.run(_build("REF", app, {}))
        keys = sorted(e.entity_description.key for e in added)
        self.assertIn("target_temp_zone4", keys)

    def test_other_types_use_their_own_candidates(self) -> None:
        # Forno: solo tempSel generico (gated).
        app = FakeAppliance({"settings": RecordingCommand({"tempSel": RangeParam(180, 30, 250, 5)})})
        added = asyncio.run(_build("OV", app, {"tempSel": "180"}))
        self.assertEqual([e.entity_description.key for e in added], ["target_temp"])
        self.assertEqual(added[0].native_step, 5.0)


if __name__ == "__main__":
    unittest.main()
