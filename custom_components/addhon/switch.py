"""Haier hOn switches: washer/dryer pause + air conditioner toggles."""
from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ac_command import async_send_settings, settings_param
from .base_entity import HonBaseEntity
from .const import APPLIANCE_AC, APPLIANCE_WASH_GROUP, DOMAIN, WM_ATTR_STATUS

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HonAcSwitchDescription:
    """Boolean AC switch acting on a 0/1 parameter of the settings command.

    `param` is both the parameter name in the `settings` command (write) and
    the direct 0/1 attribute read via _get_attr (read).
    """

    key: str            # unique_id suffix
    param: str
    icon: str | None = None


# AC switches: 0/1 parameters confirmed in the settings command of Roberto's AC.
# Capability-gated: each is created only if the device actually exposes the parameter.
_AC_SWITCHES: tuple[HonAcSwitchDescription, ...] = (
    HonAcSwitchDescription(key="sleep", param="silentSleepStatus", icon="mdi:power-sleep"),
    HonAcSwitchDescription(key="mute", param="muteStatus", icon="mdi:volume-off"),
    HonAcSwitchDescription(key="eco", param="echoStatus", icon="mdi:leaf"),
    HonAcSwitchDescription(key="rapid", param="rapidMode", icon="mdi:fan-plus"),
    HonAcSwitchDescription(key="health", param="healthMode", icon="mdi:heart-pulse"),
    HonAcSwitchDescription(key="self_clean", param="selfCleaningStatus", icon="mdi:spray-bottle"),
    HonAcSwitchDescription(key="self_clean_56", param="selfCleaning56Status", icon="mdi:spray"),
    HonAcSwitchDescription(key="display", param="screenDisplayStatus", icon="mdi:monitor"),
    HonAcSwitchDescription(key="light", param="lightStatus", icon="mdi:lightbulb"),
    HonAcSwitchDescription(key="ten_degree_heating", param="10degreeHeatingStatus", icon="mdi:snowflake-melt"),
    HonAcSwitchDescription(key="child_lock", param="lockStatus", icon="mdi:lock"),
    HonAcSwitchDescription(key="human_sensing", param="humanSensingStatus", icon="mdi:motion-sensor"),
    HonAcSwitchDescription(key="electric_heating", param="electricHeatingStatus", icon="mdi:radiator"),
    HonAcSwitchDescription(key="fresh_air", param="freshAirStatus", icon="mdi:air-filter"),
    HonAcSwitchDescription(key="half_degree", param="halfDegreeSettingStatus", icon="mdi:thermometer-lines"),
    HonAcSwitchDescription(key="energy_saving", param="energySavingStatus", icon="mdi:meter-electric"),
)


def _command_names(appliance) -> list[str]:
    commands = getattr(appliance, "commands", None)
    return sorted(commands.keys()) if isinstance(commands, dict) else []


def _param_snapshot(params) -> dict:
    if not isinstance(params, dict):
        return {"<non-dict>": type(params).__name__}
    return {
        str(name): getattr(param, "value", None)
        for name, param in params.items()
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # FIX: consistent access to the hass.data[DOMAIN][entry_id]["coordinator"] structure
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities = []
    for appliance_id, data in coordinator.data.items():
        app_type = data.get("type")
        appliance = data.get("appliance")
        _LOGGER.debug(
            "Switch debug: evaluating appliance '%s' id=%s type=%s commands=%s",
            data.get("name"),
            appliance_id,
            app_type,
            _command_names(appliance),
        )
        if app_type in APPLIANCE_WASH_GROUP:
            if appliance and hasattr(appliance, "commands"):
                cmds = getattr(appliance, "commands", None)
                cmds = cmds if isinstance(cmds, dict) else {}
                if "pauseProgram" in cmds and "resumeProgram" in cmds:
                    _LOGGER.debug("Switch debug: creating pause switch for id=%s", appliance_id)
                    entities.append(HonWashingMachinePauseSwitch(coordinator, appliance_id, client))
                    _LOGGER.info("Added switch: %s", data.get("name"))
                else:
                    _LOGGER.debug(
                        "Switch debug: pause switch not created for id=%s; pause/resume missing",
                        appliance_id,
                    )
        elif app_type == APPLIANCE_AC:
            created: list[str] = []
            for desc in _AC_SWITCHES:
                # capability-gate: only if the parameter exists in the settings command
                if settings_param(appliance, desc.param) is None:
                    continue
                entities.append(HonAcSwitch(coordinator, appliance_id, desc, client))
                created.append(desc.key)
            _LOGGER.debug(
                "Switch debug: AC '%s' id=%s -> %d switches %s",
                data.get("name"), appliance_id, len(created), created,
            )
        else:
            _LOGGER.debug("Switch debug: appliance id=%s ignored, type=%s", appliance_id, app_type)
    async_add_entities(entities)

class HonWashingMachinePauseSwitch(HonBaseEntity, SwitchEntity):
    """Switch to pause / resume the washer program."""

    _attr_icon = "mdi:pause-circle"

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._attr_unique_id = f"{appliance_id}_pause"
        self._attr_translation_key = "pause"
        _LOGGER.debug("Switch debug: initialized '%s' id=%s", self._attr_unique_id, appliance_id)

    @property
    def is_on(self) -> bool:
        val = self._get_attr(WM_ATTR_STATUS, "0")
        # machMode 3 = PAUSE_MODE (2 = EXECUTION/running) per the app's MachineMode enum.
        is_paused = str(val) == "3"
        _LOGGER.debug(
            "Switch debug: is_on '%s' id=%s machMode=%s -> %s",
            self._attr_unique_id,
            self._appliance_id,
            val,
            is_paused,
        )
        return is_paused

    async def _send_pause_command(self, command_name: str, pause_value: str) -> None:
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        _LOGGER.debug(
            "Switch debug: sending pause command '%s' value=%s id=%s commands=%s",
            command_name,
            pause_value,
            self._appliance_id,
            _command_names(appliance),
        )
        try:
            def _do():
                async def _inner():
                    commands = getattr(appliance, "commands", None)
                    commands = commands if isinstance(commands, dict) else {}
                    command = commands.get(command_name)
                    if not command:
                        raise RuntimeError(f"Command '{command_name}' not found")
                    params = getattr(command, "parameters", {})
                    _LOGGER.debug(
                        "Switch debug: command '%s' params before=%s",
                        command_name,
                        _param_snapshot(params),
                    )
                    if isinstance(params, dict) and "pause" in params:
                        previous = getattr(params["pause"], "value", None)
                        params["pause"].value = pause_value
                        _LOGGER.debug(
                            "Switch debug: pause parameter set to %s (previous=%s)",
                            pause_value,
                            previous,
                        )
                    else:
                        _LOGGER.debug(
                            "Switch debug: command '%s' without pause parameter; sending unchanged",
                            command_name,
                        )
                    await command.send()
                    _LOGGER.debug("Switch debug: command '%s' send completed", command_name)
                client.run_command_sync(_inner())

            await self.hass.async_add_executor_job(_do)
            _LOGGER.info("Pause: %s sent", command_name)
            await self._async_request_command_refresh()
        except Exception as err:
            _LOGGER.error("Pause %s: Error: %s", command_name, err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_turn_on(self, **kwargs) -> None:
        await self._send_pause_command("pauseProgram", "1")

    async def async_turn_off(self, **kwargs) -> None:
        await self._send_pause_command("resumeProgram", "0")


class HonAcSwitch(HonBaseEntity, SwitchEntity):
    """Boolean air conditioner switch on a parameter of the settings command."""

    def __init__(self, coordinator, appliance_id: str, description: HonAcSwitchDescription, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._desc = description
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{appliance_id}_{description.key}"
        if description.icon:
            self._attr_icon = description.icon
        _LOGGER.debug(
            "Switch debug: initialized AC switch '%s' id=%s param=%s",
            self._attr_unique_id, appliance_id, description.param,
        )

    @property
    def is_on(self) -> bool | None:
        raw = self._get_attr(self._desc.param)
        if raw is None:
            return None
        return str(raw) == "1"

    async def async_turn_on(self, **kwargs) -> None:
        await self._set_param("1")

    async def async_turn_off(self, **kwargs) -> None:
        await self._set_param("0")

    async def _set_param(self, value: str) -> None:
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        param = self._desc.param
        try:
            _LOGGER.debug("Switch debug: AC set %s=%s id=%s", param, value, self._appliance_id)
            await async_send_settings(self.hass, client, appliance, {param: value})
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("AC switch: set error %s=%s: %s", param, value, err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err
