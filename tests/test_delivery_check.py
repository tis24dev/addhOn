# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The delivery check (issue #115, O4): a command the cloud accepted, never applied.

The cloud answers resultCode "0" to a command it accepted whether or not the
appliance then runs it (live AC test, apk2/analysis/issue115-hw/decisioni/
9-prova-ac-mik.md). Two traces of the miss arrive anyway with the next cloud read:
the `commandHistory` slot, whose `timestampExecuted` equals `timestampAccepted` for
a command that never reached the device (4 of 4) and trails it by 0.7-1.2 s for one
that did, and the shadow, where the value sent never shows up. One WARNING per send,
nothing else: no error to the user, no extra request.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

import pytest

from tests._golden import install_stubs

install_stubs()

from custom_components.addhon import command_diagnostics as diagnostics  # noqa: E402
from custom_components.addhon.client.engine.attributes import HonAttribute  # noqa: E402
from custom_components.addhon.client.engine.commands import HonCommand  # noqa: E402
from custom_components.addhon.client.engine.exceptions import ApiError  # noqa: E402
from custom_components.addhon.command_dispatch import (  # noqa: E402
    CommandDispatcher,
    CommandPatch,
)
from custom_components.addhon.send_profiles import HPWH, LEGACY  # noqa: E402

_LOGGER_NAME = "custom_components.addhon.command_diagnostics"
_BEFORE = ("2026-10-01T10:00:00.0Z", "2026-10-01T10:00:00.9Z")
_SENT_AT = 1000.0


class _Appliance:
    """The two things a delivery check reads: the shadow and the history slot."""

    def __init__(self, appliance_type: str = "HW", **shadow: str) -> None:
        self.appliance_type = appliance_type
        self.attributes: dict[str, Any] = {
            "parameters": {key: HonAttribute(value) for key, value in shadow.items()},
            "commandHistory": {
                "timestampAccepted": _BEFORE[0],
                "timestampExecuted": _BEFORE[1],
            },
        }

    def cloud_read(self, accepted: str, executed: str, **shadow: str) -> None:
        """What `load_attributes` leaves behind: a new slot, the cloud's values."""
        self.attributes["commandHistory"] = {
            "timestampAccepted": accepted,
            "timestampExecuted": executed,
        }
        for key, value in shadow.items():
            self.attributes["parameters"][key] = HonAttribute(value)


def _arm(appliance: _Appliance, payload: Mapping[str, str], *, at: float = _SENT_AT) -> None:
    baseline = diagnostics.delivery_baseline(appliance, payload)
    diagnostics.record_delivery_check(
        appliance, "set_temperature", "setParameters", baseline, timestamp=at
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _LOGGER_NAME and record.levelno == logging.WARNING
    ]


@pytest.fixture(autouse=True)
def _clean_state(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)
    yield


# --- the two signals --------------------------------------------------------------


def test_accepted_and_executed_in_the_same_instant_warns_once(caplog) -> None:
    appliance = _Appliance(tempSel="65")
    _arm(appliance, {"tempSel": "60"})
    appliance.cloud_read("2026-10-01T10:18:01.3Z", "2026-10-01T10:18:01.3Z", tempSel="65")

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)
    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 107)

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "set_temperature" in warnings[0]
    assert "setParameters" in warnings[0]
    assert "HW" in warnings[0]
    assert "0.0 s" in warnings[0]
    assert "tempSel is 65, 60 was sent" in warnings[0]


def test_a_delivered_and_applied_command_is_silent(caplog) -> None:
    appliance = _Appliance(tempSel="65")
    _arm(appliance, {"tempSel": "60"})
    appliance.cloud_read("2026-10-01T10:17:48.4Z", "2026-10-01T10:17:49.1Z", tempSel="60.0")

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    assert _warnings(caplog) == []


def test_a_value_that_went_back_warns_even_if_the_slot_says_executed(caplog) -> None:
    """(b) alone: the device got it (1.0 s) and the value is still the old one."""
    appliance = _Appliance(tempSel="65")
    _arm(appliance, {"tempSel": "60"})
    appliance.cloud_read("2026-10-01T10:18:51.0Z", "2026-10-01T10:18:52.0Z", tempSel="65")

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "tempSel is 65, 60 was sent" in warnings[0]
    assert "never reached" not in warnings[0]


def test_the_slot_alone_never_warns(caplog) -> None:
    """PR #119 review (Greptile): the slot is shared with every other client. With
    nothing in the shadow to compare, a short gap may be another command's."""
    appliance = _Appliance()
    _arm(appliance, {"tempSel": "60"})
    appliance.cloud_read("2026-10-01T10:18:01.3Z", "2026-10-01T10:18:01.3Z")

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    assert _warnings(caplog) == []


def test_an_applied_value_is_silent_whatever_the_slot_says(caplog) -> None:
    """PR #119 review (Greptile): our command applied, its MQTT push missed, then the
    official app's command moved the slot with a short gap. Not ours to blame."""
    appliance = _Appliance(tempSel="65")
    _arm(appliance, {"tempSel": "60"})
    appliance.cloud_read("2026-10-01T10:18:01.3Z", "2026-10-01T10:18:01.3Z", tempSel="60")

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    ("executed", "explains"),
    [
        ("2026-10-01T10:18:01.3Z", True),   # 0.0 s: 4 of 4 undelivered on the AC
        ("2026-10-01T10:18:01.5Z", True),   # 0.2 s: under the threshold
        ("2026-10-01T10:18:01.6Z", False),  # 0.3 s: the threshold itself
        ("2026-10-01T10:18:01.9Z", False),  # 0.6 s: fastest delivered seen (REF)
        ("2026-10-01T10:17:00.0Z", False),  # older than accepted: no verdict
        ("not a time", False),
    ],
)
def test_the_slot_threshold(caplog, executed: str, explains: bool) -> None:
    """With the value not applied, the slot only adds the likely reason."""
    appliance = _Appliance(tempSel="65")
    _arm(appliance, {"tempSel": "60"})
    appliance.cloud_read("2026-10-01T10:18:01.3Z", executed, tempSel="65")

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert ("never reached" in warnings[0]) is explains


def test_a_slot_that_did_not_move_is_no_verdict(caplog) -> None:
    """The slot still shows the command before ours: nothing to say about ours."""
    appliance = _Appliance(tempSel="65")
    appliance.attributes["commandHistory"] = {
        "timestampAccepted": "2026-10-01T09:00:00.0Z",
        "timestampExecuted": "2026-10-01T09:00:00.0Z",
    }
    _arm(appliance, {"tempSel": "60"})
    appliance.cloud_read("2026-10-01T09:00:00.0Z", "2026-10-01T09:00:00.0Z", tempSel="60")

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    assert _warnings(caplog) == []


# --- when it judges ---------------------------------------------------------------


def test_no_verdict_before_the_settle_time(caplog) -> None:
    """The refresh right after the command reads inside the shadow's 10 s shield,
    and maybe before the device ran it: it must neither warn nor close the check."""
    appliance = _Appliance(tempSel="65")
    _arm(appliance, {"tempSel": "60"})
    appliance.cloud_read("2026-10-01T10:18:01.3Z", "2026-10-01T10:18:01.3Z", tempSel="65")

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 1)
    assert _warnings(caplog) == []

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 61)
    assert len(_warnings(caplog)) == 1


def test_a_read_after_the_ttl_drops_the_check_silently(caplog) -> None:
    appliance = _Appliance(tempSel="65")
    _arm(appliance, {"tempSel": "60"})
    appliance.cloud_read("2026-10-01T10:18:01.3Z", "2026-10-01T10:18:01.3Z", tempSel="65")

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 181)
    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 241)

    assert _warnings(caplog) == []


# --- what it checks ---------------------------------------------------------------


def test_sending_the_value_already_there_arms_nothing(caplog) -> None:
    """No false positive on a value the appliance already has: the cloud may not
    forward it at all (the AC's same-value restore came back in 0.1 s)."""
    appliance = _Appliance(tempSel="60.0")
    _arm(appliance, {"tempSel": "60"})
    appliance.cloud_read("2026-10-01T10:07:58.9Z", "2026-10-01T10:07:59.0Z", tempSel="60")

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    assert _warnings(caplog) == []


def test_only_the_keys_that_change_are_compared(caplog) -> None:
    appliance = _Appliance(tempSel="65", boostStatus="0")
    _arm(appliance, {"tempSel": "60", "boostStatus": "0"})
    appliance.cloud_read(
        "2026-10-01T10:18:51.0Z", "2026-10-01T10:18:52.0Z", tempSel="65", boostStatus="1"
    )

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "tempSel" in warnings[0]
    assert "boostStatus" not in warnings[0]


@pytest.mark.parametrize(
    ("key", "sent", "published"),
    [
        # The app writes the sterilization hour unpadded (apk2 decomp.txt:4592323-
        # 4592332); #115 publishes "15:00".
        ("sterilizationTime", "15:0", "15:00"),
        ("sterilizationTime", "3:30", "03:30"),
        # It writes the eco day mask in lower case (4505245); #113 and #115 publish "7F".
        ("opp1EcoDays", "1f", "1F"),
    ],
)
def test_the_appliances_own_spelling_is_the_value_sent(caplog, key, sent, published) -> None:
    appliance = _Appliance(**{key: "0:0" if key == "sterilizationTime" else "7F"})
    _arm(appliance, {key: sent})
    appliance.cloud_read("2026-10-01T10:17:48.4Z", "2026-10-01T10:17:49.1Z",
                         **{key: published})

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    assert _warnings(caplog) == []


def test_a_spelling_already_there_arms_nothing(caplog) -> None:
    appliance = _Appliance(sterilizationTime="15:00", opp1EcoDays="1F")
    _arm(appliance, {"sterilizationTime": "15:0", "opp1EcoDays": "1f"})
    appliance.cloud_read("2026-10-01T10:07:58.9Z", "2026-10-01T10:07:59.0Z",
                         sterilizationTime="3:00", opp1EcoDays="7F")

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    assert _warnings(caplog) == []


def test_an_mqtt_push_in_the_appliances_spelling_closes_the_check(caplog) -> None:
    appliance = _Appliance(sterilizationTime="0:0")
    _arm(appliance, {"sterilizationTime": "15:0"})

    diagnostics.observe_mqtt_update(appliance, {"sterilizationTime": "15:00"},
                                    timestamp=_SENT_AT + 2)
    appliance.cloud_read("2026-10-01T10:18:01.3Z", "2026-10-01T10:18:01.3Z",
                         sterilizationTime="4:00")
    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    ("key", "sent", "published"),
    [
        ("sterilizationTime", "15:0", "15:30"),
        ("sterilizationTime", "15:0", "5:00"),
        ("opp1EcoDays", "1f", "7F"),
        # Only a clock and a hex mask are respelled: other text stays exact.
        ("operationName", "grSetEcoTime", "GRSETECOTIME"),
    ],
)
def test_another_value_still_warns(caplog, key, sent, published) -> None:
    appliance = _Appliance(**{key: "x"})
    _arm(appliance, {key: sent})
    appliance.cloud_read("2026-10-01T10:18:51.0Z", "2026-10-01T10:18:52.0Z",
                         **{key: published})

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert f"{key} is {published}, {sent} was sent" in warnings[0]


def test_a_newer_send_replaces_the_check(caplog) -> None:
    """The slot only ever shows the latest command: an older check is dropped."""
    appliance = _Appliance(tempSel="65")
    _arm(appliance, {"tempSel": "60"})
    _arm(appliance, {"tempSel": "65"}, at=_SENT_AT + 5)  # same value: arms nothing
    appliance.cloud_read("2026-10-01T10:18:01.3Z", "2026-10-01T10:18:01.3Z", tempSel="65")

    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    assert _warnings(caplog) == []


def test_an_mqtt_push_with_the_value_closes_the_check(caplog) -> None:
    """The device's own push is the earliest proof; a later change from the app
    must not turn into a warning about our command."""
    appliance = _Appliance(tempSel="65")
    _arm(appliance, {"tempSel": "60"})

    diagnostics.observe_mqtt_update(appliance, {"tempSel": "60.0"}, timestamp=_SENT_AT + 2)
    appliance.cloud_read("2026-10-01T10:18:01.3Z", "2026-10-01T10:18:01.3Z", tempSel="55")
    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    assert _warnings(caplog) == []


def test_an_mqtt_push_with_another_value_keeps_the_check(caplog) -> None:
    appliance = _Appliance(tempSel="65")
    _arm(appliance, {"tempSel": "60"})

    diagnostics.observe_mqtt_update(appliance, {"tempSel": "65"}, timestamp=_SENT_AT + 2)
    appliance.cloud_read("2026-10-01T10:18:51.0Z", "2026-10-01T10:18:52.0Z", tempSel="65")
    diagnostics.observe_shadow_read(appliance, timestamp=_SENT_AT + 47)

    assert len(_warnings(caplog)) == 1


def test_the_mqtt_correlation_still_runs_with_a_check_armed(caplog) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)
    appliance = _Appliance(tempSel="65")
    _arm(appliance, {"tempSel": "60"})
    diagnostics.record_expected_update(appliance, "set_temperature", {"tempSel": "60"})

    diagnostics.observe_mqtt_update(appliance, {"tempSel": "60"})

    events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == _LOGGER_NAME and record.levelno == logging.DEBUG
    ]
    assert [event["event"] for event in events] == ["shadow_update", "contract_check"]
    assert events[0]["method"] == "time_window_key_value"


def test_a_broken_appliance_never_raises(caplog) -> None:
    class _Broken:
        @property
        def attributes(self):
            raise RuntimeError("boom")

    broken = _Broken()
    assert diagnostics.delivery_baseline(broken, {"tempSel": "60"}) is None
    diagnostics.record_delivery_check(broken, "a", "c", None, timestamp=_SENT_AT)
    diagnostics.observe_shadow_read(broken, timestamp=_SENT_AT + 47)
    diagnostics.observe_shadow_read(object(), timestamp=_SENT_AT + 47)
    assert _warnings(caplog) == []


# --- the profile gate and the dispatcher -------------------------------------------


def test_only_the_hpwh_profile_verifies_delivery() -> None:
    assert HPWH.verify_delivery is True
    assert LEGACY.verify_delivery is False


class _DispatchAppliance(_Appliance):
    def __init__(self, appliance_type: str) -> None:
        super().__init__(appliance_type, tempSel="65")
        self.zone = 0
        self.options: dict[str, str] = {}
        self.info: dict[str, Any] = {}
        self.commands: dict[str, HonCommand] = {}

    def sync_payload_to_params(self, payload: Mapping[str, str | float]) -> None:
        for key, value in payload.items():
            if attribute := self.attributes["parameters"].get(key):
                attribute.update(str(value), shield=True)

    def sync_command_to_params(self, name: str) -> None:
        pass


class _Api:
    def __init__(self, result: bool | Exception = True) -> None:
        self.result = result

    async def send_command(self, *args, **kwargs):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _dispatch(appliance_type: str, api: _Api) -> _DispatchAppliance:
    appliance = _DispatchAppliance(appliance_type)
    command = HonCommand(
        "settings",
        {
            "parameters": {
                "tempSel": {"typology": "range", "minimumValue": "35",
                            "maximumValue": "75", "incrementValue": "1",
                            "defaultValue": "40", "category": "command",
                            "mandatory": 0},
            },
            "ancillaryParameters": {},
        },
        appliance,
        category_name="setParameters",
    )
    command._api = api
    appliance.commands["settings"] = command
    patch = CommandPatch("settings", {"tempSel": "60"}, action="set_temperature")
    try:
        asyncio.run(CommandDispatcher().dispatch(appliance, patch))
    except ApiError:
        pass
    return appliance


def test_an_accepted_hw_send_arms_the_check(caplog) -> None:
    appliance = _dispatch("HW", _Api(True))
    appliance.cloud_read("2026-10-01T10:18:01.3Z", "2026-10-01T10:18:01.3Z", tempSel="65")

    diagnostics.observe_shadow_read(appliance, timestamp=time.monotonic() + 47)

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "set_temperature (setParameters)" in warnings[0]


def test_a_legacy_send_arms_nothing(caplog) -> None:
    appliance = _dispatch("AC", _Api(True))
    appliance.cloud_read("2026-10-01T10:18:01.3Z", "2026-10-01T10:18:01.3Z", tempSel="65")

    diagnostics.observe_shadow_read(appliance, timestamp=time.monotonic() + 47)

    assert _warnings(caplog) == []


def test_a_refused_hw_send_arms_nothing(caplog) -> None:
    appliance = _dispatch("HW", _Api(False))
    appliance.cloud_read("2026-10-01T10:18:01.3Z", "2026-10-01T10:18:01.3Z", tempSel="65")

    diagnostics.observe_shadow_read(appliance, timestamp=time.monotonic() + 47)

    assert _warnings(caplog) == []


# --- the cloud read hook ------------------------------------------------------------


def test_every_cloud_read_runs_the_check(caplog) -> None:
    """`HonAppliance.load_attributes` is where the next cloud read lands, whoever
    asked for it: the refresh after the command, the 60 s poll, the fallback."""
    from custom_components.addhon.client import factory

    class _ContextApi:
        async def load_attributes(self, _appliance):
            return {
                "shadow": {"parameters": {
                    "tempSel": {"parNewVal": "65", "lastUpdate": "2026-10-01T09:00:00Z"},
                }},
                "commandHistory": {
                    "timestampAccepted": "2026-10-01T10:18:01.3Z",
                    "timestampExecuted": "2026-10-01T10:18:01.3Z",
                },
                "lastConnEvent": {"category": "CONNECTED"},
            }

    appliance = factory._native_engine_appliance_cls()(
        _ContextApi(), {"applianceTypeName": "HW", "macAddress": "11-22-33-44-55-66"}
    )
    appliance.attributes["parameters"] = {"tempSel": HonAttribute("65")}
    appliance.attributes["commandHistory"] = {
        "timestampAccepted": _BEFORE[0],
        "timestampExecuted": _BEFORE[1],
    }
    baseline = diagnostics.delivery_baseline(appliance, {"tempSel": "60"})
    diagnostics.record_delivery_check(
        appliance, "set_temperature", "setParameters", baseline,
        timestamp=time.monotonic() - 47,
    )

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(appliance.load_attributes())
    finally:
        loop.close()

    assert len(_warnings(caplog)) == 1
