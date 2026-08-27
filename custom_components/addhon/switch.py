# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Haier hOn switches: washer/dryer pause, AC toggles, wine-cooler and hood lights,
and the cooker hood's power."""
from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .ac_command import async_send_settings, settings_param
from .base_entity import HonAccountEntity, HonBaseEntity, coordinator_data_map
from .air_purifier import (
    AirPurifierCapabilities,
    ap_patch,
    discover_capabilities,
    raw_text,
    reports_attribute,
)
from .command_dispatch import CommandPatch, async_dispatch_patch
from .const import (
    APPLIANCE_AC,
    APPLIANCE_AP,
    APPLIANCE_HO,
    APPLIANCE_TD,
    APPLIANCE_WASH_GROUP,
    APPLIANCE_WC,
    APPLIANCE_WD,
    APPLIANCE_WM,
    CONF_ENABLE_DEBUG,
    CONF_ENABLE_MQTT_DEBUG,
    DOMAIN,
    WM_ATTR_STATUS,
)
from .debug_utils import redact_id
from .hon_commands import command_param
from .hood import (
    HOOD_DELAY_STATUS_PARAM,
    HOOD_LIGHT_PARAM,
    HOOD_POWER_PARAM,
    HOOD_SETTINGS_COMMAND,
    HOOD_SPEED_PARAM,
    HOOD_START_COMMAND,
    HOOD_STOP_COMMAND,
    hood_patch,
)
from .param_rollback import restore_params, snapshot_params
from .program_options import (
    HonProgramOptionEntity,
    normalize_code,
    option_choices,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HonSettingsSwitchDescription:
    """Boolean switch acting on a 0/1 (or range[0,1]) parameter of the `settings` command.

    `param` is both the parameter name in the `settings` command (write) and
    the direct 0/1 attribute read via _get_attr (read). Used by the air conditioner
    toggles, the wine-cooler interior light and the cooker hood's light and delayed
    switch-off (same settings-command convention).

    `sparse_command` picks the WRITE CHANNEL, and it is the whole reason this class
    is still shared by three appliance families:

    * None (air conditioner, wine cooler) -- the legacy sender, which applies the
      value to the whole `settings` command and transmits EVERY parameter of its
      `parameters` group. That is what those two have always done and what their
      devices are known to accept; the AC in particular NEEDS the full group,
      because `ac_command.sanitize_wind_direction` fixes sibling parameters on the
      way out and a sparse payload would leave them unfixed on the device.
    * a command name (cooker hood) -- one SPARSE patch through the transactional
      dispatcher, carrying this parameter alone plus whatever the live schema marks
      mandatory. The hood's `settings` group also holds `clockHH`/`clockMM`/
      `clockSS`, which the device does NOT mirror into its shadow: nothing can ever
      refresh them, they sit at the 0 the schema loaded, and a full-group send
      therefore resets the hood's clock on every light or timer toggle. It also
      restates `filterCleaningAlarmStatus="1"`. Same family of bug as the wine
      cooler's #62, found before a user had to report it.

    The channel is a FIELD and not a subclass because the read half, the gating, the
    unique_id and the state mapping are identical for all three; only the sender
    differs, and `_set_param` branches on it exactly once.
    """

    key: str            # unique_id suffix
    param: str
    icon: str | None = None
    sparse_command: str | None = None


# AC switches: 0/1 parameters confirmed in the settings command of Roberto's AC.
# Capability-gated: each is created only if the device actually exposes the parameter.
_AC_SWITCHES: tuple[HonSettingsSwitchDescription, ...] = (
    HonSettingsSwitchDescription(key="sleep", param="silentSleepStatus", icon="mdi:power-sleep"),
    HonSettingsSwitchDescription(key="mute", param="muteStatus", icon="mdi:volume-off"),
    HonSettingsSwitchDescription(key="eco", param="echoStatus", icon="mdi:leaf"),
    HonSettingsSwitchDescription(key="rapid", param="rapidMode", icon="mdi:fan-plus"),
    HonSettingsSwitchDescription(key="health", param="healthMode", icon="mdi:heart-pulse"),
    HonSettingsSwitchDescription(key="self_clean", param="selfCleaningStatus", icon="mdi:spray-bottle"),
    HonSettingsSwitchDescription(key="self_clean_56", param="selfCleaning56Status", icon="mdi:spray"),
    HonSettingsSwitchDescription(key="display", param="screenDisplayStatus", icon="mdi:monitor"),
    HonSettingsSwitchDescription(key="light", param="lightStatus", icon="mdi:lightbulb"),
    HonSettingsSwitchDescription(key="ten_degree_heating", param="10degreeHeatingStatus", icon="mdi:snowflake-melt"),
    HonSettingsSwitchDescription(key="child_lock", param="lockStatus", icon="mdi:lock"),
    HonSettingsSwitchDescription(key="human_sensing", param="humanSensingStatus", icon="mdi:motion-sensor"),
    HonSettingsSwitchDescription(key="electric_heating", param="electricHeatingStatus", icon="mdi:radiator"),
    HonSettingsSwitchDescription(key="fresh_air", param="freshAirStatus", icon="mdi:air-filter"),
    HonSettingsSwitchDescription(key="half_degree", param="halfDegreeSettingStatus", icon="mdi:thermometer-lines"),
    HonSettingsSwitchDescription(key="energy_saving", param="energySavingStatus", icon="mdi:meter-electric"),
)


# Wine cooler (WC) switches: interior light. Ground-truthed on a real HWS77GDAU1 (discussion
# #62): the settings command carries lightStatus as range[0,1] (writable). Capability-gated
# like the AC switches, so a WC without a writable lightStatus creates no entity.
_WC_SWITCHES: tuple[HonSettingsSwitchDescription, ...] = (
    HonSettingsSwitchDescription(key="light", param="lightStatus", icon="mdi:lightbulb"),
)


# Cooker hood (HO) switches, ground-truthed on a real HADG6DS46BWIFI (issue #83):
# the settings command declares lightStatus and delayTimeStatus as range[0,1]
# (writable). The hood light is the SAME 0/1 model the wine cooler and the air
# conditioner already use, so it needs no class and no translation key of its own.
#
# `delay_timer` arms the hood's delayed SWITCH-OFF (keep extracting for delayTime
# minutes, then stop) -- not a delayed start. The official app arms it with one
# combined dispatch of {windSpeed, delayTime, delayTimeStatus}, forcing at least
# speed 1; we write the number and the flag separately. If the timer turns out not
# to arm on a real hood, the fix is that combined dispatch, not a different command.
#
# Both are SPARSE (see `sparse_command` above): the hood's `settings` group carries
# three clock fields the device never mirrors back, so the full-group send the AC
# and the wine cooler use would zero the hood's clock on every toggle.
#
# The hood's THIRD switch, power, is deliberately not in this table: it writes
# `onOffStatus`, which the `settings` command does not declare, and it needs one
# command per direction. See `HonHoodPowerSwitch`.
_HOOD_SWITCHES: tuple[HonSettingsSwitchDescription, ...] = (
    HonSettingsSwitchDescription(
        key="light",
        param=HOOD_LIGHT_PARAM,
        icon="mdi:lightbulb",
        sparse_command=HOOD_SETTINGS_COMMAND,
    ),
    HonSettingsSwitchDescription(
        key="delay_timer",
        param=HOOD_DELAY_STATUS_PARAM,
        icon="mdi:timer-sand",
        sparse_command=HOOD_SETTINGS_COMMAND,
    ),
)


# Per-type settings-command switch tables. Every appliance here shares HonSettingsSwitch:
# each switch reads/writes a 0/1 parameter of the device's `settings` command, capability-gated.
_SETTINGS_SWITCHES: dict[str, tuple[HonSettingsSwitchDescription, ...]] = {
    APPLIANCE_AC: _AC_SWITCHES,
    APPLIANCE_WC: _WC_SWITCHES,
    APPLIANCE_HO: _HOOD_SWITCHES,
}


@dataclass(frozen=True, kw_only=True)
class HonAirPurifierSwitchDescription(SwitchEntityDescription):
    """Air purifier 0/1 toggle written as a SPARSE settings patch.

    Deliberately separate from HonSettingsSwitchDescription: that one drives
    HonSettingsSwitch, which applies the value to the whole `settings` command and
    sends it through the legacy sender. The purifier writes only its own field
    through the transactional dispatcher, so it needs its own description and its
    own entity class rather than a flag on the legacy pair.

    `capability` names the AirPurifierCapabilities property that gates the write
    half; `action` names the `ap_patch` intent that performs it.

    Subclasses SwitchEntityDescription because HonAirPurifierSwitch publishes it as
    `entity_description`, and Home Assistant reads `entity_description.device_class`
    while computing the entity's name at ADD time. A plain dataclass has no such
    field, so every purifier toggle raised AttributeError inside
    `entity_platform._async_add_entity` and was dropped -- built, logged as built,
    never registered (issue #67). HonSettingsSwitchDescription stays a plain
    dataclass because its entity keeps it in `_desc`, out of HA's reach.
    """

    param: str          # the settings parameter, and the attribute read back
    capability: str
    action: str


# Confirmed 0/1 purifier toggles (AP_PARAMS_ENUM "Toggles"). `child_lock` reuses the
# air conditioner's translation key: same parameter name, same meaning. `touch_tone`
# is NOT folded into the AC's `mute`, whose polarity is the opposite (mute on = sound
# off, touch tone on = sound on).
_AIR_PURIFIER_SWITCHES: tuple[HonAirPurifierSwitchDescription, ...] = (
    HonAirPurifierSwitchDescription(
        key="child_lock",
        param="lockStatus",
        capability="supports_lock",
        action="set_lock",
        icon="mdi:lock",
    ),
    HonAirPurifierSwitchDescription(
        key="touch_tone",
        param="touchToneStatus",
        capability="supports_tone",
        action="set_tone",
        icon="mdi:volume-high",
    ),
)


@dataclass(frozen=True, kw_only=True)
class HonProgramOptionSwitchDescription:
    """Boolean program-option switch buffered onto the startProgram command (#35).

    `param` is the startProgram parameter name (read + write). `types` gates which
    appliance families the option applies to. on/off tokens are derived from the device
    schema at runtime (handles 0/1 AND value-pair ranges like antiCreaseTime[0,360]).
    """

    key: str            # translation_key + base of the unique_id suffix
    param: str
    types: tuple[str, ...]
    icon: str | None = None


# Candidate program-option switches (decomp/andre0512 superset), capability-gated by the
# device's startProgram schema (a param that is fixed / single-value on the model creates
# NO entity, so no "No disponible" clutter). WM/WD vs TD families are kept distinct
# (anticrease is the WM/WD opt-toggle; antiCreaseTime is the TD timed variant).
_WASH_TYPES = (APPLIANCE_WM, APPLIANCE_WD)
_DRY_TYPES = (APPLIANCE_TD,)
_PROGRAM_OPTION_SWITCHES: tuple[HonProgramOptionSwitchDescription, ...] = (
    HonProgramOptionSwitchDescription(key="extra_rinse_1", param="extraRinse1", types=_WASH_TYPES, icon="mdi:water-plus"),
    HonProgramOptionSwitchDescription(key="extra_rinse_2", param="extraRinse2", types=_WASH_TYPES, icon="mdi:water-plus"),
    HonProgramOptionSwitchDescription(key="extra_rinse_3", param="extraRinse3", types=_WASH_TYPES, icon="mdi:water-plus"),
    HonProgramOptionSwitchDescription(key="acquaplus", param="acquaplus", types=_WASH_TYPES, icon="mdi:water"),
    HonProgramOptionSwitchDescription(key="prewash", param="prewash", types=_WASH_TYPES, icon="mdi:water-sync"),
    HonProgramOptionSwitchDescription(key="hygiene", param="hygiene", types=_WASH_TYPES, icon="mdi:bacteria"),
    HonProgramOptionSwitchDescription(key="anticrease", param="anticrease", types=_WASH_TYPES, icon="mdi:tshirt-crew"),
    HonProgramOptionSwitchDescription(key="good_night", param="goodNight", types=_WASH_TYPES, icon="mdi:weather-night"),
    HonProgramOptionSwitchDescription(key="sterilization", param="sterilizationStatus", types=_DRY_TYPES, icon="mdi:bacteria-outline"),
    HonProgramOptionSwitchDescription(key="tumbling", param="tumblingStatus", types=_DRY_TYPES, icon="mdi:tumble-dryer"),
    HonProgramOptionSwitchDescription(key="permanent_press", param="permanentPressStatus", types=_DRY_TYPES, icon="mdi:tshirt-crew-outline"),
    HonProgramOptionSwitchDescription(key="anti_crease_time", param="antiCreaseTime", types=_DRY_TYPES, icon="mdi:tshirt-crew"),
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


def _appliance_switches(coordinator, appliance_id: str, data: dict, client) -> list:
    """Every switch ONE appliance contributes, or [] when it contributes none.

    Extracted from the setup loop so an appliance whose schema or state trips an
    unexpected error costs only its own switches. Inline, one raised exception
    aborted `async_setup_entry` before `async_add_entities` ever ran, so a single
    bad appliance silently removed EVERY switch of the entry, the account debug
    toggles included -- a failure that reads exactly like "the integration does
    not offer that control". Returning the list instead of appending to a shared
    one also keeps a partial failure from leaving half an appliance behind.
    """
    found: list = []
    app_type = data.get("type")
    appliance = data.get("appliance")
    _LOGGER.debug(
        "Switch debug: evaluating appliance '%s' id=%s type=%s commands=%s",
        redact_id(data.get("name"), appliance_id),
        redact_id(appliance_id),
        app_type,
        _command_names(appliance),
    )
    if app_type in APPLIANCE_WASH_GROUP:
        if appliance and hasattr(appliance, "commands"):
            cmds = getattr(appliance, "commands", None)
            cmds = cmds if isinstance(cmds, dict) else {}
            if "pauseProgram" in cmds and "resumeProgram" in cmds:
                _LOGGER.debug("Switch debug: creating pause switch for id=%s", redact_id(appliance_id))
                found.append(HonWashingMachinePauseSwitch(coordinator, appliance_id, client))
                _LOGGER.info("Added pause switch: id=%s", redact_id(appliance_id))
            else:
                _LOGGER.debug(
                    "Switch debug: pause switch not created for id=%s; pause/resume missing",
                    redact_id(appliance_id),
                )
        # Writable program-option switches (#35): created only for the params this
        # model genuinely exposes as settable in its startProgram schema.
        created_opts: list[str] = []
        for desc in _PROGRAM_OPTION_SWITCHES:
            if app_type not in desc.types:
                continue
            if not HonProgramOptionSwitch.supports(appliance, desc.param):
                continue
            found.append(HonProgramOptionSwitch(coordinator, appliance_id, desc, client))
            created_opts.append(desc.key)
        if created_opts:
            _LOGGER.info(
                "Added %d program-option switches: id=%s",
                len(created_opts),
                redact_id(appliance_id),
            )
        _LOGGER.debug(
            "Switch debug: option switches for id=%s type=%s -> %s",
            redact_id(appliance_id),
            app_type,
            created_opts,
        )
    elif app_type == APPLIANCE_AP:
        attributes = data.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        capabilities = discover_capabilities(appliance, attributes)
        created_ap: list[str] = []
        for desc in _AIR_PURIFIER_SWITCHES:
            # Both halves are gated: the write schema via the capability, the
            # read state via the reported attribute. A toggle that cannot be
            # read back would ship permanently unknown.
            #
            # A REJECTION is logged, with both verdicts and the whole capability
            # set. These two gates used to `continue` in silence and the summary
            # below names only what WAS built, so a purifier missing a toggle
            # read exactly like a purifier that never reached this branch -- the
            # state a field report sat in for two weeks. The sibling platforms
            # (light.py, select.py) have always logged their skips this way.
            #
            # The capability set is the deciding input and nothing else carries
            # it: the gate compares MATERIALISED schema values, while the
            # diagnostics dump casts a range's bounds through float(), so "0"/"1"
            # and "0.0"/"1.0" look identical there though only the first passes.
            # Logged whole because `settings_command` answers a second question
            # in the same line, namely which command the parameters were resolved
            # against. Every field is derived data -- raw values, bools, a command
            # name -- and none of it is identity.
            writable = bool(getattr(capabilities, desc.capability, False))
            readable = reports_attribute(attributes, desc.param)
            if not writable or not readable:
                _LOGGER.debug(
                    "Switch debug: no purifier '%s' switch for id=%s "
                    "(%s=%s reports_%s=%s caps=%s)",
                    desc.key,
                    redact_id(appliance_id),
                    desc.capability,
                    writable,
                    desc.param,
                    readable,
                    capabilities,
                )
                continue
            found.append(
                HonAirPurifierSwitch(
                    coordinator, appliance_id, desc, capabilities, client
                )
            )
            created_ap.append(desc.key)
        _LOGGER.debug(
            "Switch debug: purifier switches id=%s -> %d %s",
            redact_id(appliance_id), len(created_ap), created_ap,
        )
    elif app_type in _SETTINGS_SWITCHES:
        if app_type == APPLIANCE_HO:
            # The power switch is NOT a `_SETTINGS_SWITCHES` row and cannot be one:
            # every row there is gated on `settings_param`, and this hood's
            # `settings` command does not declare `onOffStatus` at all, so the row
            # would be gated out and the entity would never exist. It also needs
            # two DIFFERENT commands, one per direction, which that description
            # cannot express.
            if HonHoodPowerSwitch.supports(appliance):
                found.append(HonHoodPowerSwitch(coordinator, appliance_id, client))
                _LOGGER.info("Added hood power switch: id=%s", redact_id(appliance_id))
            else:
                _LOGGER.debug(
                    "Switch debug: no hood power switch for id=%s "
                    "(needs '%s' on '%s' and a '%s' command; commands=%s)",
                    redact_id(appliance_id),
                    HOOD_POWER_PARAM,
                    HOOD_START_COMMAND,
                    HOOD_STOP_COMMAND,
                    _command_names(appliance),
                )
        created: list[str] = []
        for desc in _SETTINGS_SWITCHES[app_type]:
            # capability-gate: only if the parameter exists in the settings command
            if settings_param(appliance, desc.param) is None:
                continue
            found.append(HonSettingsSwitch(coordinator, appliance_id, desc, client))
            created.append(desc.key)
        _LOGGER.debug(
            "Switch debug: settings switches '%s' id=%s type=%s -> %d %s",
            redact_id(data.get("name"), appliance_id), redact_id(appliance_id),
            app_type, len(created), created,
        )
    else:
        _LOGGER.debug("Switch debug: appliance id=%s ignored, type=%s", redact_id(appliance_id), app_type)
    return found


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # FIX: consistent access to the hass.data[DOMAIN][entry_id]["coordinator"] structure
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    # `.get`, like the light and fan platforms: a missing client leaves the READ half
    # of every switch working, while a KeyError here would take the whole platform
    # down, account debug toggles included. The write path already refuses without
    # one, with a localized error.
    client = entry_data.get("client")
    entities = []
    for appliance_id, data in coordinator_data_map(coordinator).items():
        try:
            entities.extend(
                _appliance_switches(coordinator, appliance_id, data, client)
            )
        except Exception:  # noqa: BLE001 - one appliance must not cost the rest
            _LOGGER.exception(
                "Switch debug: appliance id=%s contributed no switches",
                redact_id(appliance_id),
            )
    # Account-level debug switches (one set per config entry, independent of the
    # appliances): they mirror the two persisted toggles of the Options flow.
    sw_version = entry_data.get("integration_version")
    entities.append(
        HonDebugSwitch(
            entry,
            CONF_ENABLE_DEBUG,
            sw_version,
            translation_key="debug_logging",
            icon="mdi:bug",
        )
    )
    entities.append(
        HonDebugSwitch(
            entry,
            CONF_ENABLE_MQTT_DEBUG,
            sw_version,
            translation_key="mqtt_realtime_debug",
            icon="mdi:radio-tower",
        )
    )
    async_add_entities(entities)

class HonWashingMachinePauseSwitch(HonBaseEntity, SwitchEntity):
    """Switch to pause / resume the washer program."""

    _attr_icon = "mdi:pause-circle"

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._attr_unique_id = f"{appliance_id}_pause"
        self._attr_translation_key = "pause"
        _LOGGER.debug("Switch debug: initialized '%s' id=%s", redact_id(self._attr_unique_id, appliance_id), redact_id(appliance_id))

    @property
    def is_on(self) -> bool:
        val = self._get_attr(WM_ATTR_STATUS, "0")
        # machMode 3 = PAUSE_MODE (2 = EXECUTION/running) per the app's MachineMode enum.
        is_paused = str(val) == "3"
        _LOGGER.debug(
            "Switch debug: is_on '%s' id=%s machMode=%s -> %s",
            redact_id(self._attr_unique_id, self._appliance_id),
            redact_id(self._appliance_id),
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
            redact_id(self._appliance_id),
            _command_names(appliance),
        )
        # Rollback state: the pause param is mutated in memory before send; on a send
        # failure restore it so the switch does not report a local pause/resume the
        # cloud never accepted until the next poll. Populated inside _inner.
        restore_pause: dict = {}
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
                        pause_param = params["pause"]
                        # Snapshot only the pause param (shared helper, keyed dict form)
                        # so a send failure restores it without re-firing rules.
                        restore_pause["params"] = params
                        restore_pause["snap"] = snapshot_params({"pause": pause_param})
                        previous = getattr(pause_param, "value", None)
                        pause_param.value = pause_value
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
                    restore_pause.clear()  # sent: nothing to roll back
                    _LOGGER.debug("Switch debug: command '%s' send completed", command_name)
                client.run_command_sync(_inner())

            await self.hass.async_add_executor_job(_do)
            _LOGGER.info("Pause: %s sent", command_name)
            await self._async_request_command_refresh()
        except Exception as err:
            restore_params(restore_pause.get("params"), restore_pause.get("snap", {}))
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


class HonAirPurifierSwitch(HonBaseEntity, SwitchEntity):
    """Air purifier 0/1 toggle written as a sparse settings patch.

    NOT a HonSettingsSwitch subclass: that class applies the value to the whole
    `settings` command and sends it, which would restate every sibling setting.
    This one dispatches a patch carrying its own field alone.

    Never gated on power: a lock and a beep setting are meaningful and changeable
    with the purifier stopped.
    """

    entity_description: HonAirPurifierSwitchDescription

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        description: HonAirPurifierSwitchDescription,
        capabilities: AirPurifierCapabilities,
        client=None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self.entity_description = description
        self._capabilities = capabilities
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{appliance_id}_{description.key}"
        if description.icon:
            self._attr_icon = description.icon
        _LOGGER.debug(
            "Switch debug: initialized purifier switch '%s' id=%s param=%s",
            description.key, redact_id(appliance_id), description.param,
        )

    @property
    def is_on(self) -> bool | None:
        raw = self._get_attr(self.entity_description.param)
        if raw is None:
            return None
        return raw_text(raw) == "1"

    async def async_turn_on(self, **kwargs) -> None:
        await self._dispatch("1")

    async def async_turn_off(self, **kwargs) -> None:
        await self._dispatch("0")

    async def _dispatch(self, value: str) -> None:
        """Send this toggle's intent, then refresh.

        The intent is built INSIDE the try so a value the live schema rejects
        surfaces as the same localized command error as a transport failure.
        """
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        description = self.entity_description
        try:
            patch = ap_patch(
                description.action, self._capabilities, value=value
            )
            _LOGGER.debug(
                "Switch debug: purifier %s=%s id=%s",
                description.param, value, redact_id(self._appliance_id),
            )
            await async_dispatch_patch(self.hass, client, appliance, patch)
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error(
                "Purifier switch: set %s=%s failed: %s",
                description.param, value, err, exc_info=True,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err


class HonHoodPowerSwitch(HonBaseEntity, SwitchEntity):
    """Cooker hood power: the lit-or-dark control panel, `onOffStatus`.

    NOT the extraction fan, which is its own entity on `windSpeed`. The hood has
    three states, not two -- panel dark, panel lit with the fan stopped, panel lit
    with the fan running -- and while the panel is dark the device ignores every
    speed and light command it receives without even beeping. This switch owns the
    first axis; `fan.HonHoodFan` owns the second. `hood.py`'s module docstring
    carries the evidence.

    TWO COMMANDS, ONE PER DIRECTION, which is why this is a class and not a
    `HonSettingsSwitchDescription` row:

    * ON  -> `startProgram` with no values of ours at all. The dispatcher adds the
      one field the live schema marks mandatory, `onOffStatus`, pinned to "1" by
      the schema, so the body is a bare wake-up: the panel lights and whatever
      speed and light the hood remembers come back, and nothing we did not ask for
      is written. This is the one hood action the official app has no exact
      equivalent for -- it never sends `onOffStatus` alone -- because the app has
      no "wake the panel" button; its user taps the glass instead.
    * OFF -> `stopProgram` carrying `windSpeed` and `lightStatus` at the zeroes the
      schema pins them to, plus the mandatory `onOffStatus`. That is byte-for-byte
      the payload this hood's own `commandHistory` shows it accepted AND executed,
      and it is the only proven-executed command in the whole dossier. Switching
      the hood off switches its light off too: the device declares those values
      itself, they are not a choice of ours.

    Both directions are sparse patches through the transactional dispatcher, built
    by `hood.hood_patch` so the `programName` suppression cannot be forgotten.
    """

    _attr_translation_key = "power"
    _attr_icon = "mdi:power"

    @staticmethod
    def supports(appliance) -> bool:
        """True when this hood declares both halves of the power axis.

        Gated on the write schema rather than on the reported attribute: a hood that
        publishes `onOffStatus` but cannot be told to change it would ship a switch
        that reads correctly and does nothing, which is the one failure mode a
        capability gate exists to prevent.

        The two halves are gated differently on purpose. The ON side needs the
        parameter itself, because without it `startProgram` cannot say "wake up".
        The OFF side needs only the COMMAND: `stopProgram` pins its own values, so
        whatever it declares is what switching off means on that hood, and
        `async_turn_off` names only the parameters it finds rather than assuming
        this specimen's three.
        """
        if command_param(appliance, HOOD_START_COMMAND, HOOD_POWER_PARAM) is None:
            return False
        commands = getattr(appliance, "commands", None)
        return isinstance(commands, dict) and HOOD_STOP_COMMAND in commands

    def __init__(self, coordinator, appliance_id: str, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._attr_unique_id = f"{appliance_id}_power"
        _LOGGER.debug(
            "Switch debug: initialized hood power switch id=%s",
            redact_id(appliance_id),
        )

    @property
    def is_on(self) -> bool | None:
        # The bare shadow attribute, which this hood publishes alongside the dotted
        # per-command mirrors. Unknown rather than off when it is missing: a hood
        # that stopped reporting its own power flag has not told us it is off.
        raw = self._get_attr(HOOD_POWER_PARAM)
        if raw is None:
            return None
        return str(raw) == "1"

    async def async_turn_on(self, **kwargs) -> None:
        await self._dispatch(
            "turn_on",
            hood_patch(HOOD_START_COMMAND, {}, action="hood_power_on"),
        )

    async def async_turn_off(self, **kwargs) -> None:
        # Only the zeroes this hood's OWN `stopProgram` declares. The reporting hood
        # declares all three, so the payload is byte-for-byte the one its command
        # history shows it executed; a hood declaring fewer -- the minimal
        # `{onOffStatus}` shape is perfectly ordinary in this cloud, and a hood with
        # no lamp is a device family this platform already gates for -- gets the
        # subset it understands instead of a `ValueError` from the dispatcher, which
        # would surface as a command error and leave the switch stuck on. The
        # mandatory `onOffStatus` is added by the dispatcher either way, so the
        # command is never empty.
        values = {
            name: "0"
            for name in (HOOD_SPEED_PARAM, HOOD_LIGHT_PARAM)
            if command_param(self._appliance, HOOD_STOP_COMMAND, name) is not None
        }
        await self._dispatch(
            "turn_off",
            hood_patch(HOOD_STOP_COMMAND, values, action="hood_power_off"),
        )

    async def _dispatch(self, action: str, patch: CommandPatch) -> None:
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        try:
            _LOGGER.debug(
                "Switch debug: hood power %s id=%s command=%s values=%s",
                action,
                redact_id(self._appliance_id),
                patch.command_name,
                dict(patch.values),
            )
            await async_dispatch_patch(self.hass, client, appliance, patch)
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Hood power switch: %s failed: %s", action, err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err


class HonSettingsSwitch(HonBaseEntity, SwitchEntity):
    """Boolean switch on a parameter of the `settings` command.

    Serves the air conditioner toggles, the wine-cooler interior light and the cooker
    hood's light and delayed switch-off: all write a 0/1 parameter of the same
    `settings` command and read it back via _get_attr. They differ only in the write
    CHANNEL, which `HonSettingsSwitchDescription.sparse_command` selects and
    `_set_param` branches on once -- see that class for why the hood cannot use the
    full-group sender the other two need.
    """

    def __init__(self, coordinator, appliance_id: str, description: HonSettingsSwitchDescription, client=None) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._desc = description
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{appliance_id}_{description.key}"
        if description.icon:
            self._attr_icon = description.icon
        _LOGGER.debug(
            "Switch debug: initialized settings switch '%s' id=%s param=%s",
            redact_id(self._attr_unique_id, appliance_id), redact_id(appliance_id), description.param,
        )

    @property
    def is_on(self) -> bool | None:
        # Read the mirrored shadow value only. A settings-command param that the device
        # does not mirror into the shadow is refreshed only at load time / by our own
        # writes (sync_params_to_command skips shadow-absent keys), so falling back to the
        # command value would manufacture a confidently-stale state; honest unknown (None)
        # is correct. The real WC/AC mirror lightStatus, so this returns a value in practice.
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
        sparse_command = self._desc.sparse_command
        try:
            _LOGGER.debug(
                "Switch debug: settings set %s=%s id=%s sparse=%s",
                param, value, redact_id(self._appliance_id), sparse_command,
            )
            if sparse_command is None:
                # AC and wine cooler: unchanged. The whole `settings` group goes out,
                # windDirection* sanitized on the way.
                await async_send_settings(self.hass, client, appliance, {param: value})
            else:
                # Cooker hood: this parameter alone (plus whatever the live schema
                # marks mandatory, which on that command is nothing). `action` is
                # what the command diagnostics record the intent under; the
                # appliance type travels beside it, so the key needs no prefix.
                await async_dispatch_patch(
                    self.hass,
                    client,
                    appliance,
                    CommandPatch(
                        sparse_command,
                        {param: value},
                        action=f"set_{self._desc.key}",
                    ),
                )
            await self._async_request_command_refresh()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Settings switch: set error %s=%s: %s", param, value, err, exc_info=True)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err


class HonProgramOptionSwitch(HonProgramOptionEntity, SwitchEntity):
    """Boolean program option (extra rinse, prewash, sterilization, ...) buffered onto
    the startProgram command; the real send happens on the "Start program" button."""

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        description: HonProgramOptionSwitchDescription,
        client=None,
    ) -> None:
        super().__init__(coordinator, appliance_id, description.param, client)
        self._desc = description
        self._attr_translation_key = description.key
        self._attr_unique_id = f"{appliance_id}_opt_{description.key}"
        if description.icon:
            self._attr_icon = description.icon
        # Resolve on/off tokens from the device schema (the param is resolved + cached once
        # by the mixin): off = "0" (or the lowest value), on = the first value that is not
        # off. Handles plain 0/1 AND value-pair ranges such as antiCreaseTime[0,360].
        choices = option_choices(self._option_param) if self._option_param is not None else []
        self._off = "0" if "0" in choices else (choices[0] if choices else "0")
        self._on = next((c for c in choices if c != self._off), "1")
        _LOGGER.debug(
            "Switch debug: init option switch '%s' id=%s param=%s off=%s on=%s",
            redact_id(self._attr_unique_id, appliance_id),
            redact_id(appliance_id),
            description.param,
            self._off,
            self._on,
        )

    @property
    def is_on(self) -> bool | None:
        raw = self._current_raw()
        if raw is None:
            return None
        return normalize_code(raw) != self._off

    async def async_turn_on(self, **kwargs) -> None:
        self._buffer(self._on)

    async def async_turn_off(self, **kwargs) -> None:
        self._buffer(self._off)


class HonDebugSwitch(HonAccountEntity, SwitchEntity):
    """Persistent debug toggle, mirrored to ``entry.options``.

    ``is_on`` reads ``entry.options`` (single source of truth); turning it on/off
    persists the option via ``async_update_entry``, which fires the options update
    listener in ``__init__`` so the log levels are re-applied live (no reload). An
    entry update listener keeps the switch in sync when the same option is changed
    from the Options flow or the Reset button.

    Admin-only: the option it writes drives process-global log levels, so the
    entity route is gated the same way the log-level services are (see
    ``_async_assert_admin``).
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        entry,
        option_key: str,
        sw_version: str | None = None,
        *,
        translation_key: str,
        icon: str,
    ) -> None:
        super().__init__(entry, translation_key, sw_version)
        self._option_key = option_key
        self._attr_translation_key = translation_key
        self._attr_icon = icon

    @property
    def is_on(self) -> bool:
        return bool(self._entry_options.get(self._option_key, False))

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_assert_admin()
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_assert_admin()
        await self._async_set(False)

    async def _async_assert_admin(self) -> None:
        """Admin gate, mirroring async_register_admin_service on the entity route.

        Both toggles end in ``logging.getLogger().setLevel()``, which is global to
        the Python process, and the two log-level services are already admin-only.
        ``switch.turn_on`` carries no admin gate in Home Assistant, so without this
        check any authenticated non-admin could reach the same capability from the
        entity. Home Assistant sets the entity context to the calling service call
        before the handler runs, so ``_context.user_id`` is the caller; a call with
        no user attached (automation, internal) is trusted, exactly as the helper
        does. The Reset debug button stays open on purpose: it only turns the
        toggles off, which is not a privileged capability.
        """
        context = getattr(self, "_context", None)
        user_id = getattr(context, "user_id", None)
        if not user_id:
            return
        user = await self.hass.auth.async_get_user(user_id)
        if user is None or not user.is_admin:
            _LOGGER.debug(
                "Switch debug: non-admin user denied on '%s' (entry=%s)",
                self._option_key,
                getattr(self._entry, "entry_id", None),
            )
            raise Unauthorized(context=context)

    async def _async_set(self, value: bool) -> None:
        options = self._entry_options
        if bool(options.get(self._option_key, False)) == value:
            return
        _LOGGER.debug(
            "Switch debug: debug toggle '%s' -> %s (entry=%s)",
            self._option_key,
            value,
            getattr(self._entry, "entry_id", None),
        )
        self.hass.config_entries.async_update_entry(
            self._entry, options={**options, self._option_key: value}
        )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._entry.add_update_listener(self._async_entry_updated)
        )

    async def _async_entry_updated(self, hass, entry) -> None:
        self.async_write_ha_state()
