# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for _remove_legacy_entities (#22): the legacy 'power' cleanup must be
scoped to the switch domain, so the legitimate WH `power` and KT `current_power`
SENSORS (which also end in '_power') are not purged on every setup.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _mod(name: str) -> types.ModuleType:
    m = sys.modules.get(name)
    if m is None:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return m


def _install_stubs() -> None:
    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    if not hasattr(core, "callback"):
        core.callback = lambda f: f
    if not hasattr(core, "ServiceCall"):
        core.ServiceCall = object
    exc = _mod("homeassistant.exceptions")
    base = getattr(exc, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exc.HomeAssistantError = base
    exc.ConfigEntryNotReady = getattr(exc, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base,), {}))
    exc.ConfigEntryAuthFailed = getattr(exc, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base,), {}))
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))
    _mod("homeassistant.helpers.entity_registry")  # functions set per-test
    _mod("homeassistant.helpers.device_registry")   # idem
    # The repair registry. Imported lazily by `_raise_ref_program_repair`, and that
    # import is wrapped in a bare except -- so WITHOUT this stub the notice would fail
    # silently and every assertion about it would pass for the wrong reason.
    ir = _mod("homeassistant.helpers.issue_registry")
    ir.IssueSeverity = getattr(
        ir, "IssueSeverity", type("IssueSeverity", (), {"WARNING": "warning"})
    )
    # The two entity BASE classes conftest does not install (it installs their
    # descriptions and device classes). The zone-clone trap below imports the real
    # hob tables to drive itself, which pulls the two platform modules in.
    # getattr-guarded: a module that installs a richer base keeps winning.
    sensor_mod = _mod("homeassistant.components.sensor")
    sensor_mod.SensorEntity = getattr(
        sensor_mod, "SensorEntity", type("SensorEntity", (), {})
    )
    binary_mod = _mod("homeassistant.components.binary_sensor")
    binary_mod.BinarySensorEntity = getattr(
        binary_mod, "BinarySensorEntity", type("BinarySensorEntity", (), {})
    )
    ha = _mod("homeassistant")
    ha.config_entries, ha.core, ha.exceptions = ce, core, exc
    ha.helpers = _mod("homeassistant.helpers")
    ha.helpers.update_coordinator = uc
    ha.helpers.entity_registry = sys.modules["homeassistant.helpers.entity_registry"]
    ha.helpers.device_registry = sys.modules["homeassistant.helpers.device_registry"]
    ha.helpers.issue_registry = sys.modules["homeassistant.helpers.issue_registry"]


_install_stubs()

import homeassistant.helpers.device_registry as dr  # noqa: E402
import homeassistant.helpers.entity_registry as er  # noqa: E402
import homeassistant.helpers.issue_registry as ir  # noqa: E402
from custom_components.addhon import (  # noqa: E402
    DOMAIN,
    _remove_legacy_entities,
    async_remove_config_entry_device,
)


class FakeRegEntry:
    def __init__(self, entity_id: str, unique_id: str) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


class FakeRegistry:
    def __init__(self, entries) -> None:
        self._entries = list(entries)
        self.removed: list = []

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)
        self._entries = [e for e in self._entries if e.entity_id != entity_id]


class FakeEntry:
    def __init__(self, entry_id: str = "entry-1") -> None:
        self.entry_id = entry_id


class FakeDevice:
    def __init__(self, device_id: str, identifiers) -> None:
        self.id = device_id
        self.identifiers = set(identifiers)


class FakeDeviceRegistry:
    def __init__(self, devices) -> None:
        self._devices = list(devices)
        self.detached: list = []

    def async_update_device(self, device_id, remove_config_entry_id=None):
        self.detached.append((device_id, remove_config_entry_id))
        # Mutated IN PLACE, like the real registry: rebinding a new list would
        # leave any iterator over the old one intact and hide exactly the
        # skip-every-other-device bug the materialisation in production prevents.
        self._devices[:] = [d for d in self._devices if d.id != device_id]


def _run(entries, coord_data=None, devices=(), issues=None, on_issue=None):
    # Always rebound, never left pointing at a previous test's list: a run that does not
    # ask about repairs must not append into one that does, or the assertions here would
    # depend on collection order. `on_issue` is the escape hatch for the one test that
    # needs the repair registry to FAIL -- binding it here rather than around the call
    # is what keeps the rebind unconditional and the failing stub reachable.
    if on_issue is not None:
        ir.async_create_issue = on_issue
    else:
        sink = [] if issues is None else issues
        ir.async_create_issue = lambda hass, domain, key, **kw: sink.append(
            (domain, key, kw)
        )
    reg = FakeRegistry(entries)
    er.async_get = lambda hass: reg
    er.async_entries_for_config_entry = lambda registry, entry_id: list(registry._entries)
    dev_reg = FakeDeviceRegistry(devices)
    dr.async_get = lambda hass: dev_reg
    # A LIVE view, exactly like the real helper: iterating it while the loop
    # detaches devices is the mutation the production code materialises against.
    dr.async_entries_for_config_entry = lambda registry, entry_id: registry._devices
    entry = FakeEntry()
    coordinator = types.SimpleNamespace(data=coord_data or {})
    hass = types.SimpleNamespace(data={DOMAIN: {entry.entry_id: {"coordinator": coordinator}}})
    _remove_legacy_entities(hass, entry)
    return reg.removed, dev_reg.detached


class LegacyCleanupTest(unittest.TestCase):
    def test_legacy_power_switch_removed(self) -> None:
        removed, _detached = _run([FakeRegEntry("switch.foo_power", "ID_power")])
        self.assertEqual(removed, ["switch.foo_power"])

    def test_wh_power_sensor_kept(self) -> None:
        # #22: a sensor with unique_id '<id>_power' must NOT be deleted.
        removed, _detached = _run([FakeRegEntry("sensor.foo_power", "ID_power")])
        self.assertEqual(removed, [])

    def test_kt_current_power_sensor_kept(self) -> None:
        removed, _detached = _run([FakeRegEntry("sensor.foo_current_power", "ID_current_power")])
        self.assertEqual(removed, [])

    def test_mixed_only_switch_power_removed(self) -> None:
        entries = [
            FakeRegEntry("switch.foo_power", "ID_power"),         # legacy -> remove
            FakeRegEntry("sensor.foo_power", "ID_power"),         # WH power -> keep
            FakeRegEntry("sensor.foo_current_power", "ID_current_power"),  # KT -> keep
            FakeRegEntry("sensor.foo_temperature", "ID_temperature"),     # unrelated -> keep
        ]
        removed, _detached = _run(entries)
        self.assertEqual(removed, ["switch.foo_power"])

    def test_td_orphan_consumption_removed(self) -> None:
        # Existing behavior preserved: washer-only consumption sensors on a TD device.
        removed, _detached = _run(
            [FakeRegEntry("sensor.td_total_water", "tdid_total_water")],
            coord_data={"tdid": {"type": "TD"}},
        )
        self.assertEqual(removed, ["sensor.td_total_water"])

    def test_td_orphan_not_removed_on_non_td(self) -> None:
        removed, _detached = _run(
            [FakeRegEntry("sensor.wm_total_water", "wmid_total_water")],
            coord_data={"wmid": {"type": "WM"}},
        )
        self.assertEqual(removed, [])

    # --- #93: the fridge program select, replaced rather than dropped.

    @staticmethod
    def _fridge_appliance(*, flags=True, drawer=False, zones="fridge|freezer|vtRoom1"):
        """A fridge whose live schema decides whether it was superseded.

        Built as the production predicate reads it: the program enum off
        `startProgram.program`, the clearable flags off `stopProgram`, and -- for the
        drawer -- one category pinning `tempSelZ3`, zoned on the drawer alone.
        """
        class _Param:
            def __init__(self, values=None, value=None, typology="enum"):
                self.values = values
                self.value = value
                self.typology = typology

        class _Cmd:
            def __init__(self, parameters, categories=None):
                self.parameters = parameters
                if categories is not None:
                    self.categories = categories

        codes = []
        categories = {}
        if flags:
            codes.append("super_cool")
            categories["super_cool"] = _Cmd(
                {"quickModeZ1": _Param(value="1", typology="fixed")}
            )
        if drawer:
            codes.append("zero_fresh")
            categories["zero_fresh"] = _Cmd(
                {
                    "tempSelZ3": _Param(value="0", typology="fixed"),
                    "zone": _Param(values=["vtroom1"]),
                    "programFamily": _Param(values=["dashboard"]),
                }
            )
        commands = {
            "startProgram": _Cmd({"program": _Param(values=codes)}, categories),
            "stopProgram": _Cmd(
                {"quickModeZ1": _Param(value="0", typology="fixed")}
                if flags
                else {"onOffStatus": _Param(value="0", typology="fixed")}
            ),
        }
        return types.SimpleNamespace(
            commands=commands,
            model_attributes={} if zones is None else {"zones": zones},
        )

    def test_the_superseded_fridge_program_select_is_removed(self) -> None:
        # Replaced, not dropped: four mode switches now write the same registers with a
        # per-flag `off`. Left behind, the select would sit `unavailable` with the '?'
        # badge -- and an automation calling `select.select_option` on it logs a WARNING
        # and reports SUCCESS, which is the quietest way this change could fail.
        removed, _detached = _run(
            [FakeRegEntry("select.fridge_ref_program", "refid_ref_program")],
            coord_data={"refid": {"type": "REF", "appliance": self._fridge_appliance()}},
        )
        self.assertEqual(removed, ["select.fridge_ref_program"])

    def test_a_fridge_that_keeps_its_select_keeps_the_entity(self) -> None:
        # THE reason this rule is conditional, and the only one of the five that is. A
        # fridge offering no clearable flag and no drawer program still gets the select,
        # so removing its registry entry would delete a LIVE entity on the next start.
        removed, _detached = _run(
            [FakeRegEntry("select.fridge_ref_program", "refid_ref_program")],
            coord_data={
                "refid": {
                    "type": "REF",
                    "appliance": self._fridge_appliance(flags=False),
                }
            },
        )
        self.assertEqual(removed, [])

    def test_a_drawer_alone_also_supersedes_it(self) -> None:
        # The other half of the predicate: no clearable flag, but the drawer select is
        # buildable, so the single select does step aside and its entry must go.
        removed, _detached = _run(
            [FakeRegEntry("select.fridge_ref_program", "refid_ref_program")],
            coord_data={
                "refid": {
                    "type": "REF",
                    "appliance": self._fridge_appliance(flags=False, drawer=True),
                }
            },
        )
        self.assertEqual(removed, ["select.fridge_ref_program"])

    def test_another_domain_with_the_same_suffix_is_kept(self) -> None:
        # Scoped to `select` for the reason the panel-light rule is: an entity of another
        # domain sharing the unique_id is a different entity.
        removed, _detached = _run(
            [FakeRegEntry("sensor.fridge_ref_program", "refid_ref_program")],
            coord_data={"refid": {"type": "REF", "appliance": self._fridge_appliance()}},
        )
        self.assertEqual(removed, [])

    def test_a_select_of_another_appliance_is_kept(self) -> None:
        # Double-anchored: the id must be a superseded fridge in THIS snapshot, not just
        # any entity whose unique_id ends the right way.
        removed, _detached = _run(
            [FakeRegEntry("select.other_ref_program", "otherid_ref_program")],
            coord_data={"refid": {"type": "REF", "appliance": self._fridge_appliance()}},
        )
        self.assertEqual(removed, [])

    def test_nothing_is_purged_without_a_snapshot(self) -> None:
        # A degraded start postpones the purge instead of guessing, exactly like the
        # tumble-dryer and hob rules.
        removed, _detached = _run(
            [FakeRegEntry("select.fridge_ref_program", "refid_ref_program")],
            coord_data={},
        )
        self.assertEqual(removed, [])

    def test_a_broken_schema_costs_the_purge_and_not_the_setup(self) -> None:
        # The predicate reads a live schema, so it can raise on an appliance the cloud
        # answered badly for. That must postpone this one removal, never abort setup and
        # never take the other four rules with it.
        class _Exploding:
            @property
            def commands(self):
                raise RuntimeError("schema unreadable")

            model_attributes = {"zones": "fridge|vtRoom1"}

        removed, _detached = _run(
            [
                FakeRegEntry("select.fridge_ref_program", "refid_ref_program"),
                FakeRegEntry("switch.foo_power", "ID_power"),
            ],
            coord_data={"refid": {"type": "REF", "appliance": _Exploding()}},
        )
        self.assertEqual(removed, ["switch.foo_power"])

    def test_the_removal_log_redacts_identity(self) -> None:
        import custom_components.addhon as init_mod

        mac = "AA:BB:CC:DD:EE:FF"
        with self.assertLogs(init_mod._LOGGER.name, level="INFO") as logs:
            _run(
                [FakeRegEntry("select.fridge_ref_program", f"{mac}_ref_program")],
                coord_data={
                    "AA:BB:CC:DD:EE:FF": {
                        "type": "REF",
                        "appliance": self._fridge_appliance(),
                    }
                },
            )
        blob = "\n".join(logs.output)
        self.assertIn("Removed the fridge program select", blob)
        self.assertNotIn(mac, blob)
        self.assertNotIn("AA:BB", blob)

    def test_removing_the_select_raises_a_repair(self) -> None:
        """The removal itself is silent, and so is the breakage it causes.

        Home Assistant answers `select.select_option` on an entity that no longer exists
        with a WARNING in the log and a SUCCESSFUL service call, so an automation that
        used to set the fridge to Holiday keeps reporting as run and does nothing. The
        repair is the only place a user is told.
        """
        issues: list = []
        _run(
            [FakeRegEntry("select.fridge_ref_program", "refid_ref_program")],
            coord_data={"refid": {"type": "REF", "appliance": self._fridge_appliance()}},
            issues=issues,
        )
        self.assertEqual(1, len(issues))
        domain, key, kwargs = issues[0]
        self.assertEqual(DOMAIN, domain)
        self.assertEqual("ref_program_select_replaced", key)
        self.assertFalse(kwargs["is_fixable"])
        self.assertEqual("ref_program_select_replaced", kwargs["translation_key"])

    def test_no_repair_when_nothing_was_removed(self) -> None:
        # A fresh install, and every start after the one that did the removal: the purge
        # is idempotent, so the notice must fire once and never again.
        issues: list = []
        _run(
            [FakeRegEntry("sensor.fridge_temp_zone1", "refid_temp_zone1")],
            coord_data={"refid": {"type": "REF", "appliance": self._fridge_appliance()}},
            issues=issues,
        )
        self.assertEqual([], issues)

    def test_no_repair_for_the_other_four_purges(self) -> None:
        # Only THIS removal breaks something a user may still be calling. The legacy
        # power switch and the dryer's washer sensors were dead entities; a notice for
        # them would be noise.
        issues: list = []
        _run([FakeRegEntry("switch.foo_power", "ID_power")], issues=issues)
        self.assertEqual([], issues)

    def test_the_repair_carries_no_appliance_identity(self) -> None:
        # One notice per config entry, and it names the generic keys, never this user's
        # appliance -- the same rule the removal log follows.
        issues: list = []
        mac = "AA:BB:CC:DD:EE:FF"
        _run(
            [FakeRegEntry("select.fridge_ref_program", f"{mac}_ref_program")],
            coord_data={mac: {"type": "REF", "appliance": self._fridge_appliance()}},
            issues=issues,
        )
        blob = repr(issues)
        self.assertNotIn(mac, blob)
        self.assertNotIn("AA:BB", blob)

    def test_a_broken_repair_registry_does_not_cost_the_purge(self) -> None:
        # The notice is a courtesy; the cleanup is the job. A Home Assistant whose repair
        # helper raises must not undo the removal or abort the setup.
        #
        # The failing stub is handed to `_run`, not assigned around it: `_run` rebinds
        # `ir.async_create_issue` unconditionally (so no test appends into another
        # test's list), which used to overwrite it before the cleanup ever ran -- this
        # test passed while exercising the success path.
        calls: list = []

        def _boom(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("no repairs here")

        removed, _detached = _run(
            [FakeRegEntry("select.fridge_ref_program", "refid_ref_program")],
            coord_data={
                "refid": {"type": "REF", "appliance": self._fridge_appliance()}
            },
            on_issue=_boom,
        )
        # The raiser really ran -- otherwise this asserts nothing about the failure path.
        self.assertEqual(1, len(calls))
        self.assertEqual(removed, ["select.fridge_ref_program"])

    # --- #93: the My Zone mode sensor is HIDDEN where the select appears, never removed.

    def test_no_my_zone_entity_is_ever_removed(self) -> None:
        """5.21.0 purged `sensor.<id>_my_zone_mode` wherever the drawer select could be
        built. It is not purged any more, and the reversal is the point: the reading is
        still created there, merely disabled at first registration
        (`sensor.async_setup_entry`), exactly like the four flag readings behind their
        mode switches. It has to survive because it answers where the select cannot --
        `chiller`, `cool_drink`, `cheese` are register values no drawer PROGRAM pins, so
        a mode set from the fridge's own panel leaves the select on unknown (#93,
        reported by rmxs).

        Both domains, both catalogue shapes: the select carries the SAME unique_id
        suffix as the sensor, so a rule reintroduced without a domain scope would take
        the control with the reading.
        """
        for drawer in (True, False):
            for entity_id in (
                "sensor.fridge_my_zone_mode",
                "select.fridge_my_zone_mode",
            ):
                removed, _detached = _run(
                    [FakeRegEntry(entity_id, "refid_my_zone_mode")],
                    coord_data={
                        "refid": {
                            "type": "REF",
                            "appliance": self._fridge_appliance(
                                flags=False, drawer=drawer
                            ),
                        }
                    },
                )
                self.assertEqual(removed, [], f"{entity_id}, drawer={drawer}")

    def test_the_my_zone_sensor_raises_no_repair(self) -> None:
        # The notice belongs to `select.ref_program` alone, whose loss makes
        # `select.select_option` succeed while doing nothing. A registry holding only
        # the drawer reading loses nothing and must stay silent.
        issues: list = []
        _run(
            [FakeRegEntry("sensor.fridge_my_zone_mode", "refid_my_zone_mode")],
            coord_data={
                "refid": {
                    "type": "REF",
                    "appliance": self._fridge_appliance(flags=False, drawer=True),
                }
            },
            issues=issues,
        )
        self.assertEqual([], issues)

    def test_legacy_power_removal_log_redacts_identity(self) -> None:
        # Privacy: the INFO removal log must carry the redacted id, never the
        # entity_id (whose object_id is the nickname slug). INFO is not gated by
        # the debug toggles, so it always reaches home-assistant.log.
        with self.assertLogs("custom_components.addhon", level="INFO") as logs:
            removed, _detached = _run([FakeRegEntry("switch.foo_power", "ID_power")])
        self.assertEqual(removed, ["switch.foo_power"])
        blob = "\n".join(logs.output)
        self.assertIn("id=***", blob)
        self.assertNotIn("foo_power", blob)
        self.assertNotIn("ID_power", blob)

    def test_td_orphan_removal_log_redacts_identity(self) -> None:
        with self.assertLogs("custom_components.addhon", level="INFO") as logs:
            removed, _detached = _run(
                [FakeRegEntry("sensor.td_total_water", "tdid_total_water")],
                coord_data={"tdid": {"type": "TD"}},
            )
        self.assertEqual(removed, ["sensor.td_total_water"])
        blob = "\n".join(logs.output)
        self.assertIn("id=***", blob)
        self.assertNotIn("td_total_water", blob)
        self.assertNotIn("tdid", blob)


class HobZoneCloneCleanupTest(unittest.TestCase):
    """The per-zone clones of an induction hob (#84.2).

    `client/session.py` no longer expands a hob into N+1 appliances, so the
    entities and devices of the four clones an earlier version registered are now
    orphans: 'unavailable' with the '?' badge, forever. Purging them is what turns
    the fix into something the reporting user can see.
    """

    def test_a_hob_zone_clone_entity_is_removed(self) -> None:
        removed, _ = _run(
            [FakeRegEntry("binary_sensor.hob_z1_pan_zone1", "MAC_z1_pan_zone1")],
            coord_data={"MAC": {"type": "IH"}},
        )
        self.assertEqual(["binary_sensor.hob_z1_pan_zone1"], removed)

    def test_the_surviving_base_entities_are_untouched(self) -> None:
        # The whole point of stopping the expansion rather than filtering it: the
        # base device keeps its id, so every entity on it keeps working.
        removed, _ = _run(
            [
                FakeRegEntry("binary_sensor.hob_pan_zone1", "MAC_pan_zone1"),
                FakeRegEntry("sensor.hob_temp_zone1", "MAC_temp_zone1"),
                FakeRegEntry("binary_sensor.hob_connectivity", "MAC_connectivity"),
            ],
            coord_data={"MAC": {"type": "IH"}},
        )
        self.assertEqual([], removed)

    def test_the_hob_alias_is_purged_too(self) -> None:
        removed, _ = _run(
            [FakeRegEntry("binary_sensor.hob_z2_pan_zone2", "MAC_z2_pan_zone2")],
            coord_data={"MAC": {"type": "HOB"}},
        )
        self.assertEqual(["binary_sensor.hob_z2_pan_zone2"], removed)

    def test_a_zoned_appliance_of_another_type_keeps_its_entities(self) -> None:
        # The risk this purge carries. A twin-cavity oven still expands, so its
        # '_z<N>' entities are the ONLY copy of their readings: matching on the id
        # shape alone would delete a user's data.
        removed, _ = _run(
            [FakeRegEntry("sensor.oven_z1_temp_cavity", "MAC_z1_temp_cavity")],
            coord_data={"MAC": {"type": "OV"}, "MAC_z1": {"type": "OV"}},
        )
        self.assertEqual([], removed)

    def test_nothing_is_purged_without_a_snapshot(self) -> None:
        # A degraded start cannot tell a hob from an oven. Postponing is safe (the
        # purge runs on every setup); guessing is not.
        removed, _ = _run(
            [FakeRegEntry("binary_sensor.hob_z1_pan_zone1", "MAC_z1_pan_zone1")],
            coord_data={},
        )
        self.assertEqual([], removed)

    def test_no_hob_entity_key_can_be_mistaken_for_a_clone(self) -> None:
        """The trap: every per-zone KEY the hob tables publish, on the BASE device.

        `pan_zone1`, `temp_zone3`, `power_zone2`, `hot_zone4` all carry the word
        zone followed by a digit, and every one of them is a legitimate entity of
        the surviving device. A pattern that looked for the shape anywhere in the
        id -- rather than anchored in the device half -- would delete all of them
        and leave the user with a hob reporting nothing at all.

        Driven off the real tables so it cannot go stale: a per-zone key added
        later is checked the day it is added.
        """
        from custom_components.addhon.binary_sensor import BINARY_SENSORS
        from custom_components.addhon.sensor import SENSORS

        keys = sorted(
            {d.key for d in SENSORS["IH"]} | {d.key for d in BINARY_SENSORS["IH"]}
        )
        self.assertTrue(keys, "the hob tables would make this vacuous")
        entries = [
            FakeRegEntry(f"sensor.hob_{key}", f"MAC_{key}") for key in keys
        ]
        removed, _ = _run(entries, coord_data={"MAC": {"type": "IH"}})
        self.assertEqual([], removed, f"a hob key was mistaken for a zone clone: {removed}")

    def test_the_emptied_clone_device_is_detached_from_the_entry(self) -> None:
        # Removing the entities leaves the device row behind, empty. An empty
        # device card reads as a broken appliance, which is worse than the
        # duplicate it replaced.
        removed, detached = _run(
            [FakeRegEntry("binary_sensor.hob_z1_pan_zone1", "MAC_z1_pan_zone1")],
            coord_data={"MAC": {"type": "IH"}},
            devices=[
                FakeDevice("dev-base", {(DOMAIN, "MAC")}),
                FakeDevice("dev-z1", {(DOMAIN, "MAC_z1")}),
            ],
        )
        self.assertEqual(["binary_sensor.hob_z1_pan_zone1"], removed)
        self.assertEqual([("dev-z1", "entry-1")], detached)

    def test_every_clone_device_is_detached_not_only_the_first(self) -> None:
        # `async_entries_for_config_entry` returns a live view and detaching
        # mutates it: iterating it directly skips every other device.
        _removed, detached = _run(
            [],
            coord_data={"MAC": {"type": "IH"}},
            devices=[
                FakeDevice("dev-z1", {(DOMAIN, "MAC_z1")}),
                FakeDevice("dev-z2", {(DOMAIN, "MAC_z2")}),
                FakeDevice("dev-z3", {(DOMAIN, "MAC_z3")}),
                FakeDevice("dev-z4", {(DOMAIN, "MAC_z4")}),
            ],
        )
        self.assertEqual(
            [("dev-z1", "entry-1"), ("dev-z2", "entry-1"),
             ("dev-z3", "entry-1"), ("dev-z4", "entry-1")],
            detached,
        )

    def test_a_live_device_is_never_detached(self) -> None:
        # A hypothetical hob whose clone the session still creates (a downgrade,
        # a stale snapshot) must keep its device: the purge removes orphans, not
        # appliances that exist.
        _removed, detached = _run(
            [],
            coord_data={"MAC": {"type": "IH"}, "MAC_z1": {"type": "IH"}},
            devices=[FakeDevice("dev-z1", {(DOMAIN, "MAC_z1")})],
        )
        self.assertEqual([], detached)

    def test_a_foreign_device_is_never_detached(self) -> None:
        _removed, detached = _run(
            [],
            coord_data={"MAC": {"type": "IH"}},
            devices=[FakeDevice("dev-other", {("other_domain", "MAC_z1")})],
        )
        self.assertEqual([], detached)

    def test_the_clone_removal_log_redacts_identity(self) -> None:
        with self.assertLogs("custom_components.addhon", level="INFO") as logs:
            _run(
                [FakeRegEntry("binary_sensor.hob_z1_pan_zone1", "MAC_z1_pan_zone1")],
                coord_data={"MAC": {"type": "IH"}},
                devices=[FakeDevice("dev-z1", {(DOMAIN, "MAC_z1")})],
            )
        blob = "\n".join(logs.output)
        self.assertIn("id=***", blob)
        self.assertNotIn("MAC_z1", blob)


class RemoveConfigEntryDeviceTest(unittest.IsolatedAsyncioTestCase):
    """The manual "Delete" button, which the integration did not offer at all."""

    @staticmethod
    def _hass(coord_data):
        coordinator = types.SimpleNamespace(data=coord_data)
        return types.SimpleNamespace(
            data={DOMAIN: {"entry-1": {"coordinator": coordinator}}}
        )

    async def test_a_stale_device_can_be_deleted(self) -> None:
        allowed = await async_remove_config_entry_device(
            self._hass({"MAC": {"type": "IH"}}),
            FakeEntry(),
            FakeDevice("dev-z1", {(DOMAIN, "MAC_z1")}),
        )
        self.assertTrue(allowed)

    async def test_a_live_device_cannot_be_deleted(self) -> None:
        # Otherwise the user deletes an appliance the next poll recreates, which
        # reads as the button being broken.
        allowed = await async_remove_config_entry_device(
            self._hass({"MAC": {"type": "IH"}}),
            FakeEntry(),
            FakeDevice("dev-base", {(DOMAIN, "MAC")}),
        )
        self.assertFalse(allowed)

    async def test_nothing_is_deletable_without_a_snapshot(self) -> None:
        # With no snapshot every device looks stale, and answering True would
        # offer to delete the whole account.
        allowed = await async_remove_config_entry_device(
            self._hass(None),
            FakeEntry(),
            FakeDevice("dev-z1", {(DOMAIN, "MAC_z1")}),
        )
        self.assertFalse(allowed)

    async def test_a_device_of_another_integration_is_refused(self) -> None:
        allowed = await async_remove_config_entry_device(
            self._hass({"MAC": {"type": "IH"}}),
            FakeEntry(),
            FakeDevice("dev-other", {("other_domain", "whatever")}),
        )
        self.assertFalse(allowed)

    async def test_the_account_diagnostics_device_can_never_be_deleted(self) -> None:
        """The one device EVERY entry of EVERY account owns, and the one this hook
        was offering to destroy.

        It is a synthetic service device, not an appliance, so `coordinator.data`
        never mentions it and the stale/live comparison on its own answered True --
        for every user, whatever they own. Home Assistant then drew a Delete button
        on the card that carries the debug toggles and the "Refresh now" button,
        and pressing it removed the card until the next reload. It also flatly
        contradicted this function's own docstring, which promises to refuse a
        device that is still alive.

        The identifier is read out of `account_device_info`, the function that
        BUILDS the device, rather than spelled again here: that makes this a
        two-sided pin, so renaming the suffix on one side has to fail here instead
        of silently putting the button back.
        """
        from custom_components.addhon.base_entity import account_device_info

        entry = FakeEntry()
        identifiers = set(account_device_info(entry)["identifiers"])
        # Anti-vacuity: a device with no identifiers is refused by the `bool(ours)`
        # guard for an unrelated reason, and would pass this test for free.
        self.assertEqual(1, len(identifiers), identifiers)
        coord_data = {"MAC": {"type": "IH"}}
        self.assertTrue(
            identifiers.isdisjoint(
                {(DOMAIN, appliance_id) for appliance_id in coord_data}
            ),
            "the snapshot already names the account device; the test proves nothing",
        )

        allowed = await async_remove_config_entry_device(
            self._hass(coord_data), entry, FakeDevice("dev-diag", identifiers)
        )
        self.assertFalse(allowed)

    async def test_refusing_the_account_device_did_not_freeze_the_button(self) -> None:
        # The positive control for the test above: a genuinely orphaned device of
        # the SAME entry is still deletable, so the fix narrowed the answer by
        # exactly one device rather than turning the hook off.
        entry = FakeEntry()
        allowed = await async_remove_config_entry_device(
            self._hass({"MAC": {"type": "IH"}}),
            entry,
            FakeDevice("dev-z1", {(DOMAIN, "MAC_z1")}),
        )
        self.assertTrue(allowed)


if __name__ == "__main__":
    unittest.main()
