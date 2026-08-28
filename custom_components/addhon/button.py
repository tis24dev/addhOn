# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Buttons for explicit Haier hOn actions."""
from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import HonAccountEntity, HonBaseEntity
from .const import (
    APPLIANCE_FR,
    APPLIANCE_FRE,
    APPLIANCE_REF,
    APPLIANCE_WASH_GROUP,
    CONF_ENABLE_DEBUG,
    CONF_ENABLE_MQTT_DEBUG,
    DOMAIN,
    PROGRAM_PARAM_NAMES,
    PROGRAM_PENDING_OPTIONS,
    PROGRAM_PENDING_STORE,
)
from .debug_utils import command_names, param_snapshot, redact_id, redact_store
from .logging_utils import reset_integration_log_level, silence_mqtt_noise
from .param_rollback import restore_params, snapshot_params
from .program_options import apply_pending_options, async_send_program
from .ref_programs import download_codes

_LOGGER = logging.getLogger(__name__)

# Fridge family (REF/FR/FRE): the QuickSet download presets, one button each (#93).
_COOLING_TYPES = (APPLIANCE_REF, APPLIANCE_FR, APPLIANCE_FRE)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configure the buttons for explicit physical actions."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    client = entry_data["client"]
    entities = []
    for appliance_id, data in coordinator.data.items():
        app_type = data.get("type")
        _LOGGER.debug(
            "Button debug: evaluating appliance '%s' id=%s type=%s commands=%s",
            data.get("name"),
            redact_id(appliance_id),
            app_type,
            command_names(data.get("appliance")),
        )
        if app_type in _COOLING_TYPES:
            # The fridge's QuickSet download presets (#93). One BUTTON per preset,
            # because that is what they are: `apk/analysis/deep/`
            # `ref-active-program-detection.md` proves across nine targets that a
            # running preset sets no flag and leaves NO program-identity field in the
            # shadow -- no prCode, no prStr, no prPhase, no machMode, `programName`
            # stuck at addhOn's own "No Program", `activity` empty -- on both REF models
            # anyone has dumped. The official app does not recover it either: it reads no
            # shadow identity field (T5), runs no setpoint reverse-match (T6, and the
            # static triples collide so it could not), and shows the last one only from
            # its own device-local AsyncStorage `@quickSet` (T1, T8). Its card list is
            # built from the write-side catalogue with `available` hardcoded true (T3):
            # a "what you can SEND" menu.
            #
            # A switch would therefore have to lie about being off, and a select would
            # have to lie about which one is current. A button claims nothing it cannot
            # know, and is the only shape that survives contact with that evidence.
            presets = download_codes(data.get("appliance"))
            for code in presets:
                entities.append(
                    HonRefPresetButton(coordinator, appliance_id, code, client)
                )
            if presets:
                _LOGGER.info(
                    "Added %d REF preset buttons: id=%s -> %s",
                    len(presets),
                    redact_id(appliance_id),
                    presets,
                )
            else:
                _LOGGER.debug(
                    "Button debug: no REF preset buttons for '%s' id=%s; needs a "
                    "startProgram category with programFamily=download that this "
                    "repository names (ref_programs.REF_DOWNLOAD_PRESETS)",
                    data.get("name"),
                    redact_id(appliance_id),
                )
            continue
        if app_type not in APPLIANCE_WASH_GROUP:
            _LOGGER.debug("Button debug: appliance id=%s ignored, type=%s", redact_id(appliance_id), app_type)
            continue
        appliance = data.get("appliance")
        commands = getattr(appliance, "commands", None)
        commands = commands if isinstance(commands, dict) else {}
        if "startProgram" in commands:
            _LOGGER.debug("Button debug: creating startProgram button for id=%s", redact_id(appliance_id))
            entities.append(
                HonProgramCommandButton(
                    coordinator,
                    appliance_id,
                    client,
                    command_name="startProgram",
                    unique_suffix="start_program",
                    translation_key="start_program",
                    icon="mdi:play-circle",
                )
            )
        if "stopProgram" in commands:
            _LOGGER.debug("Button debug: creating stopProgram button for id=%s", redact_id(appliance_id))
            entities.append(
                HonProgramCommandButton(
                    coordinator,
                    appliance_id,
                    client,
                    command_name="stopProgram",
                    unique_suffix="stop_program",
                    translation_key="stop_program",
                    icon="mdi:stop-circle",
                    command_parameters={"onOffStatus": "0"},
                )
            )
    # Account-level debug action buttons (one set per config entry).
    sw_version = entry_data.get("integration_version")
    entities.append(HonForceRefreshButton(coordinator, entry, sw_version))
    entities.append(HonResetDebugButton(entry, sw_version))
    async_add_entities(entities)


class HonProgramCommandButton(HonBaseEntity, ButtonEntity):
    """Button to send clearly explicit start/stop commands."""

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        client=None,
        *,
        command_name: str,
        unique_suffix: str,
        translation_key: str,
        icon: str,
        command_parameters: dict[str, str] | None = None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._command_name = command_name
        self._command_parameters = command_parameters or {}
        self._attr_unique_id = f"{appliance_id}_{unique_suffix}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        _LOGGER.debug(
            "Button debug: initialized '%s' id=%s command=%s fixed_params=%s",
            redact_id(self._attr_unique_id, appliance_id),
            redact_id(appliance_id),
            command_name,
            self._command_parameters,
        )

    async def async_press(self) -> None:
        """Send the explicit physical command."""
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )

        # Start: we apply the program chosen from the select (if present).
        # We read it here on the HA event loop and pass it into _inner.
        store = self._coordinator_store(PROGRAM_PENDING_STORE)
        pending_program = (
            store.get(self._appliance_id)
            if self._command_name == "startProgram"
            else None
        )
        # (a) Snapshot-copy the buffered program OPTIONS (#35): only startProgram carries
        # them; stopProgram never touches the option buffer. Applied AFTER the program
        # swap, BELOW. A copy so a concurrent buffer write cannot mutate it mid-send.
        options_store = self._coordinator_store(PROGRAM_PENDING_OPTIONS)
        pending_options = (
            dict(options_store.get(self._appliance_id) or {})
            if self._command_name == "startProgram"
            else {}
        )
        _LOGGER.debug(
            "Button debug: press '%s' id=%s pending_program=%s options=%s store=%s commands=%s",
            self._command_name,
            redact_id(self._appliance_id),
            pending_program,
            sorted(pending_options),
            redact_store(store),
            command_names(appliance),
        )
        # Rollback state for a failed send: applying the pending program both mutates
        # the program parameter AND swaps appliance.commands[name] to the selected
        # category. Populated inside _inner BEFORE the mutation and consumed by the
        # except below, so a send failure does not leave the appliance pointing at a
        # program/command the cloud never accepted (which would otherwise skew the
        # per-program option ranges until the next poll self-heals it). Uses the shared
        # snapshot/restore in param_rollback (same as hon_commands.async_send_command).
        rollback: dict = {}
        try:
            def _do():
                async def _inner():
                    commands = getattr(appliance, "commands", None)
                    commands = commands if isinstance(commands, dict) else {}
                    command = commands.get(self._command_name)
                    if not command:
                        raise RuntimeError(
                            f"Command '{self._command_name}' not found. "
                            f"Available: {list(commands.keys())}"
                        )
                    params = getattr(command, "parameters", {})
                    # Record the pre-swap state so a failed send can be fully rolled back.
                    rollback["commands"] = commands
                    rollback["name"] = self._command_name
                    rollback["original_command"] = command
                    rollback["snapshots"] = [(params, snapshot_params(params))]
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "Button debug: before command '%s' params=%s",
                            self._command_name,
                            param_snapshot(params),
                        )
                    if pending_program is not None:
                        # Fail-safe: if we cannot attach the chosen program to
                        # startProgram, do NOT start (this avoids starting a
                        # program different from the selected one).
                        applied = False
                        if isinstance(params, dict):
                            for pname in PROGRAM_PARAM_NAMES:
                                if pname in params:
                                    previous = getattr(params[pname], "value", None)
                                    params[pname].value = pending_program
                                    applied = True
                                    _LOGGER.debug(
                                        "Button debug: applied pending_program=%s to parameter '%s' "
                                        "(previous=%s)",
                                        pending_program,
                                        pname,
                                        previous,
                                    )
                                    break
                        if not applied:
                            available = (
                                list(params.keys()) if isinstance(params, dict) else params
                            )
                            raise RuntimeError(
                                "Selected program not applicable to "
                                f"'{self._command_name}': no parameter "
                                f"{PROGRAM_PARAM_NAMES} among {available}"
                            )
                    # Applying a program parameter can SWAP the active command in the
                    # appliance dict: a HonParameterProgram setter does
                    # command.category = value -> appliance.commands[name] =
                    # categories[selected], replacing the object. Re-read the now active
                    # command so the fixed parameters AND send() target the selected
                    # program's command, not the stale pre-swap object. (#6)
                    refreshed = commands.get(self._command_name)
                    if refreshed is not None and refreshed is not command:
                        _LOGGER.debug(
                            "Button debug: active command '%s' swapped after program apply",
                            self._command_name,
                        )
                        command = refreshed
                        params = getattr(command, "parameters", {})
                        # Snapshot the post-swap command's params too, so options/fixed
                        # applied below are rolled back with the swap on a send failure.
                        rollback["snapshots"].append((params, snapshot_params(params)))
                    # (b) Apply the buffered program options to the POST-SWAP command
                    # (#35): selecting the program swaps the active startProgram command,
                    # so the options must land on the new one. apply_pending_options skips
                    # an option the selected program does not expose (debug). The engine
                    # setter validates each value; a bad value raises BEFORE send() below,
                    # so nothing is transmitted and the option buffer is kept for retry
                    # (the in-memory param mutation is overwritten on the next refresh).
                    if pending_options:
                        applied = apply_pending_options(params, pending_options)
                        _LOGGER.debug(
                            "Button debug: applied %d/%d program options %s",
                            len(applied),
                            len(pending_options),
                            applied,
                        )
                    for name, value in self._command_parameters.items():
                        if name in params:
                            previous = getattr(params[name], "value", None)
                            params[name].value = value
                            _LOGGER.debug(
                                "Button debug: set fixed parameter '%s'=%s (previous=%s)",
                                name,
                                value,
                                previous,
                            )
                        else:
                            _LOGGER.debug(
                                "Button debug: fixed parameter '%s' not present in command '%s'",
                                name,
                                self._command_name,
                            )
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "Button debug: sending command '%s' final_params=%s",
                            self._command_name,
                            param_snapshot(params),
                        )
                    await command.send()
                    rollback.clear()  # sent: nothing to roll back
                    _LOGGER.debug("Button debug: command '%s' send completed", self._command_name)

                client.run_command_sync(_inner())

            await self.hass.async_add_executor_job(_do)
            # Start succeeded: the program is now "real", we clear the pending
            # choice so the select goes back to reflecting the device state.
            if pending_program is not None:
                store.pop(self._appliance_id, None)
                _LOGGER.debug(
                    "Button debug: pending program consumed and removed, store=%s",
                    redact_store(store),
                )
            # (c) Clear the buffered options on a successful startProgram send (#35).
            # Gated on the command name, NOT on pending_program: options are cleared even
            # when only options changed and no program was re-selected. Clear ONLY the keys
            # we actually SENT (the pre-send snapshot) whose current store value still equals
            # what we sent, so an option the user (re)wrote AFTER the snapshot but before this
            # clear survives for the next Start (PR #38 / CodeRabbit). Pop the appliance entry
            # only once empty. On a failure the try aborts before here -> the whole buffer is
            # kept and a retry re-sends it.
            if self._command_name == "startProgram":
                current = options_store.get(self._appliance_id)
                if isinstance(current, dict):
                    for opt_name, opt_value in pending_options.items():
                        if current.get(opt_name) == opt_value:
                            current.pop(opt_name, None)
                    if not current:
                        options_store.pop(self._appliance_id, None)
                _LOGGER.debug(
                    "Button debug: pending options cleared, store=%s",
                    redact_store(options_store),
                )
            _LOGGER.info("Button: command '%s' sent", self._command_name)
            await self._async_request_command_refresh()
        except Exception as err:
            # Roll back the command swap + parameter mutations left by a failed send,
            # so the appliance does not keep pointing at a program the cloud rejected.
            if rollback:
                cmds = rollback.get("commands")
                name = rollback.get("name")
                original = rollback.get("original_command")
                if isinstance(cmds, dict) and original is not None and cmds.get(name) is not original:
                    cmds[name] = original
                for ps, snap in reversed(rollback.get("snapshots", [])):
                    restore_params(ps, snap)
            _LOGGER.error(
                "Button %s: command error: %s",
                self._command_name, err, exc_info=True,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err


class HonRefPresetButton(HonBaseEntity, ButtonEntity):
    """One fridge QuickSet download preset: press to send it (#93).

    Fire-and-forget by construction, not by choice. The preset writes a `tempSel`
    triple, sets no flag, and leaves nothing in the shadow that says it ran, so there is
    no state to publish and no "stop" to offer -- pressing another preset, or moving a
    setpoint, simply moves the setpoints again.

    NOT a diagnostic and NOT a config entity, and the choice is worth stating because it
    decides where a user finds them. Home Assistant's CONFIG category is for changing
    the configuration of a device and DIAGNOSTIC for information about its health; both
    fold the entity off the device's main card. These presets change the fridge and
    freezer setpoints -- what the appliance is doing to the food, which is its primary
    function -- and the app agrees about where they belong: the QuickSet cards render on
    the REF dashboard itself (`QuicksetTab`, decomp.txt:2875962-2876099), not in a
    settings screen. `button.start_program` and `button.stop_program` carry no category
    for exactly this reason, while `force_refresh` and `reset_debug` carry CONFIG because
    what they configure is the INTEGRATION. Statelessness was considered as an argument
    for CONFIG and rejected: it is a property of the control, not of what it does.

    The send goes through `async_send_program`, the same swap-aware path the program
    selects use, and NOT through this module's `HonProgramCommandButton`: that class
    exists to carry the washer's pending-program and pending-options buffers onto
    `startProgram`, and none of that applies here. The preset's own fixed parameters ride
    along in the swapped category's schema and are serialized by `command.send()`; we
    never set them by hand, which is what keeps a per-model triple (IOT_EXTRA_ICE writes
    `tempSelZ2` alone) correct without a table of triples in this repository.
    """

    _attr_icon = "mdi:snowflake-alert"

    def __init__(
        self,
        coordinator,
        appliance_id: str,
        code: str,
        client=None,
    ) -> None:
        super().__init__(coordinator, appliance_id, client)
        self._code = code
        # `code` is always a member of `ref_programs.REF_DOWNLOAD_PRESETS`, a constant of
        # this repository -- see that tuple for why the gate intersects with it. The
        # unique_id suffix vocabulary therefore stays closed, which is the invariant
        # `diagnostics._entity_section` rests its privacy argument on.
        self._attr_unique_id = f"{appliance_id}_ref_preset_{code}"
        self._attr_translation_key = f"ref_preset_{code}"
        _LOGGER.debug(
            "Button debug: init REF preset '%s' id=%s code=%s",
            redact_id(self._attr_unique_id, appliance_id),
            redact_id(appliance_id),
            code,
        )

    async def async_press(self) -> None:
        appliance = self._appliance
        client = self._hon_client
        if not appliance or not client:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="appliance_or_client_unavailable",
            )
        try:
            _LOGGER.info(
                "Button: REF preset '%s' -> startProgram id=%s",
                self._code,
                redact_id(self._appliance_id),
            )
            await async_send_program(self.hass, client, appliance, self._code)
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error(
                "Button: REF preset '%s' error: %s", self._code, err, exc_info=True
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_error",
                translation_placeholders={"error": str(err)},
            ) from err
        # The setpoints the preset writes ARE mirrored into the shadow, so the refresh
        # is not cosmetic: `number.target_temp_zone1` and its siblings show the new
        # values on the next poll. The preset's own identity is not recoverable and
        # nothing here pretends otherwise.
        await self._async_request_command_refresh()


class HonForceRefreshButton(HonAccountEntity, ButtonEntity):
    """Force an immediate coordinator refresh (debug polling/discovery)."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:refresh"
    _attr_translation_key = "force_refresh"

    def __init__(self, coordinator, entry, sw_version: str | None = None) -> None:
        super().__init__(entry, "force_refresh", sw_version)
        self._coordinator = coordinator

    async def async_press(self) -> None:
        _LOGGER.debug(
            "Button debug: force refresh requested (entry=%s)",
            getattr(self._entry, "entry_id", None),
        )
        await self._coordinator.async_request_refresh()


class HonResetDebugButton(HonAccountEntity, ButtonEntity):
    """Turn both debug toggles off and restore the default log levels.

    Always resets the loggers (clears any runtime ``set_log_level`` override) and,
    if either toggle is on, persists them off via ``async_update_entry`` (which the
    options listener then re-applies).
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:restart"
    _attr_translation_key = "reset_debug"

    def __init__(self, entry, sw_version: str | None = None) -> None:
        super().__init__(entry, "reset_debug", sw_version)

    async def async_press(self) -> None:
        _LOGGER.debug(
            "Button debug: reset debug requested (entry=%s)",
            getattr(self._entry, "entry_id", None),
        )
        reset_integration_log_level()
        silence_mqtt_noise()
        options = self._entry_options
        if options.get(CONF_ENABLE_DEBUG) or options.get(CONF_ENABLE_MQTT_DEBUG):
            self.hass.config_entries.async_update_entry(
                self._entry,
                options={
                    **options,
                    CONF_ENABLE_DEBUG: False,
                    CONF_ENABLE_MQTT_DEBUG: False,
                },
            )
