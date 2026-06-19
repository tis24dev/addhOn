"""Native ROOT HonAppliance.

Puts together our whole engine: native attributes (engine.attributes), native loader/
commands/rules/program (engine.command_loader), native per-type layer
(engine.appliances.registry).

Implements ONLY the surface actually consumed (measured across integration + session
+ engine): identity/state properties, load_*/update, settings/data/command_parameters,
sync_*. `api` is OUR transport.api.HonApi.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from .appliances import registry as _native_appliances
from .attributes import HonAttribute
from .command_loader import HonCommandLoader
from .commands import HonCommand
from .exceptions import NoAuthenticationException
from .parameter.base import HonParameter
from .parameter.range import HonParameterRange

_LOGGER = logging.getLogger(__name__)


class HonAppliance:
    _MINIMAL_UPDATE_INTERVAL = 5  # seconds

    def __init__(self, api: Any, info: dict[str, Any], zone: int = 0) -> None:
        if attributes := info.get("attributes"):
            info["attributes"] = {v["parName"]: v["parValue"] for v in attributes}
        self._info: dict[str, Any] = info
        self._api = api
        self._appliance_model: dict[str, Any] = {}
        self._commands: dict[str, HonCommand] = {}
        self._statistics: dict[str, Any] = {}
        self._attributes: dict[str, Any] = {}
        self._zone = zone
        self._additional_data: dict[str, Any] = {}
        self._last_update: Optional[datetime] = None
        self._default_setting = HonParameter("", {}, "")
        self._connection = (
            not self._attributes.get("lastConnEvent", {}).get("category", "")
            == "DISCONNECTED"
        )
        # Per-type layer (resolved via the static registry).
        self._extra = _native_appliances.get_extra(self)

    def _check_name_zone(self, name: str, frontend: bool = True) -> str:
        zone = " Z" if frontend else "_z"
        attribute: str = self._info.get(name, "")
        if attribute and self._zone:
            return f"{attribute}{zone}{self._zone}"
        return attribute

    # --- identity / metadata ---
    @property
    def appliance_model_id(self) -> str:
        return str(self._info.get("applianceModelId", ""))

    @property
    def appliance_type(self) -> str:
        return str(self._info.get("applianceTypeName", ""))

    @property
    def mac_address(self) -> str:
        return str(self.info.get("macAddress", ""))

    @property
    def unique_id(self) -> str:
        default_mac = "xx-xx-xx-xx-xx-xx"
        import_name = f"{self.appliance_type.lower()}_{self.appliance_model_id}"
        result = self._check_name_zone("macAddress", frontend=False)
        return result.replace(default_mac, import_name)

    @property
    def model_name(self) -> str:
        return self._check_name_zone("modelName")

    @property
    def brand(self) -> str:
        brand = self._check_name_zone("brand")
        return brand[0].upper() + brand[1:] if brand else brand

    @property
    def nick_name(self) -> str:
        result = self._check_name_zone("nickName")
        if not result or re.findall("^[xX1\\s-]+$", result):
            return self.model_name
        return result

    @property
    def code(self) -> str:
        code: str = self.info.get("code", "")
        if code:
            return code
        serial_number: str = self.info.get("serialNumber", "")
        return serial_number[:8] if len(serial_number) < 18 else serial_number[:11]

    @property
    def model_id(self) -> int:
        return int(self._info.get("applianceModelId", 0))

    # --- state / data ---
    @property
    def options(self) -> dict[str, Any]:
        return dict(self._appliance_model.get("options", {}))

    @property
    def commands(self) -> dict[str, HonCommand]:
        return self._commands

    @property
    def attributes(self) -> dict[str, Any]:
        return self._attributes

    @property
    def statistics(self) -> dict[str, Any]:
        return self._statistics

    @property
    def info(self) -> dict[str, Any]:
        return self._info

    @property
    def additional_data(self) -> dict[str, Any]:
        return self._additional_data

    @property
    def zone(self) -> int:
        return self._zone

    @property
    def api(self) -> Any:
        if self._api is None:
            raise NoAuthenticationException("Missing hOn login")
        return self._api

    @property
    def connection(self) -> bool:
        return self._connection

    @connection.setter
    def connection(self, connection: bool) -> None:
        self._connection = connection

    # --- loading ---
    async def load_commands(self) -> None:
        command_loader = HonCommandLoader(self.api, self)
        await command_loader.load_commands()
        self._commands = command_loader.commands
        self._additional_data = command_loader.additional_data
        self._appliance_model = command_loader.appliance_data
        self.sync_params_to_command("settings")

    async def load_attributes(self) -> None:
        attributes = await self.api.load_attributes(self)
        for name, values in attributes.pop("shadow", {}).get("parameters", {}).items():
            if name in self._attributes.get("parameters", {}):
                self._attributes["parameters"][name].update(values)
            else:
                self._attributes.setdefault("parameters", {})[name] = HonAttribute(values)
        self._attributes |= attributes
        # Authoritative connectivity = lastConnEvent.category (app model
        # ApplianceConnectionState, see apk/analysis/per-type-derivations.md #5).
        # Updating `_connection` here on every poll keeps the per-type layers that zero
        # based on `self.connection` (td/wd/dw/ov) accurate and consistent with wm (which
        # reads lastConnEvent). Validated live: an offline TD showed a stale machMode=1,
        # now 0 like WM. Does not overwrite a fresher MQTT state if lastConnEvent is missing.
        lce = self._attributes.get("lastConnEvent")
        # Only the authoritative `category` updates connectivity. If lastConnEvent is
        # absent, malformed, or a dict without `category`, keep the (MQTT-derived) state
        # rather than forcing it True.
        if isinstance(lce, dict) and "category" in lce:
            self._connection = lce["category"] != "DISCONNECTED"
        # `available` UNIVERSAL (even for types without a per-type layer, e.g. AC):
        # connectivity is first-class as in the app. The per-type layer re-sets it (same value).
        self._attributes["available"] = self._connection
        if self._extra:
            self._attributes = self._extra.attributes(self._attributes)

    async def load_statistics(self) -> None:
        self._statistics = await self.api.load_statistics(self)
        self._statistics |= await self.api.load_maintenance(self)

    async def update(self, force: bool = False) -> None:
        now = datetime.now()
        min_age = now - timedelta(seconds=self._MINIMAL_UPDATE_INTERVAL)
        if force or not self._last_update or self._last_update < min_age:
            self._last_update = now
            await self.load_attributes()
            self.sync_params_to_command("settings")

    # --- derived views ---
    @property
    def command_parameters(self) -> dict[str, dict[str, str | float]]:
        return {n: c.parameter_value for n, c in self._commands.items()}

    @property
    def settings(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, command in self._commands.items():
            for key in command.setting_keys:
                setting = command.settings.get(key, self._default_setting)
                result[f"{name}.{key}"] = setting
        if self._extra:
            return self._extra.settings(result)
        return result

    @property
    def available_settings(self) -> list[str]:
        result = []
        for name, command in self._commands.items():
            for key in command.setting_keys:
                result.append(f"{name}.{key}")
        return result

    @property
    def data(self) -> dict[str, Any]:
        return {
            "attributes": self.attributes,
            "appliance": self.info,
            "statistics": self.statistics,
            "additional_data": self._additional_data,
            **self.command_parameters,
            **self.attributes,
        }

    # --- sync attributes <-> commands ---
    def sync_command_to_params(self, command_name: str) -> None:
        if not (command := self.commands.get(command_name)):
            return
        for key in self.attributes.get("parameters", {}):
            if new := command.parameters.get(key):
                self.attributes["parameters"][key].update(
                    str(new.intern_value), shield=True
                )

    def sync_params_to_command(self, command_name: str) -> None:
        if not (command := self.commands.get(command_name)):
            return
        for key in command.setting_keys:
            if (
                new := self.attributes.get("parameters", {}).get(key)
            ) is None or new.value == "":
                continue
            setting = command.settings[key]
            try:
                if not isinstance(setting, HonParameterRange):
                    command.settings[key].value = str(new.value)
                else:
                    command.settings[key].value = float(new.value)
            except ValueError as error:
                _LOGGER.info("Can't set %s - %s", key, error)
                continue
