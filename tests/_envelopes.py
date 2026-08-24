# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The two appliance-list envelopes the ADDHON-210 investigation turns on.

ONE definition, shared by the three suites that need it (`test_transport_parse.py`
for the flags, `test_transport_api.py` for the census the transport assembles, and
`test_diagnostics.py` for the document a reporter downloads). A fixture copied into
three files is three fixtures, and the entire value of these two is that they are
compared against each other.

`healthy()` is a LIVE CAPTURE, not an authored shape: taken on 2026-08-24 against a
real account holding one appliance, through `apk/probe_2fa_envelope.py`, and written
up in `apk/analysis/addhon210-healthy-envelope-baseline.md`. Everything about it that
the census reads is reproduced here exactly as observed:

    HTTP 200
    top level                                 keys ['executionTime','modules','success']
    top level success                         True
    modules                                   keys ['applianceList']
    modules.applianceList                     keys ['authInfo','executionTime','payload','success']
    modules.applianceList.success             True
    modules.applianceList.authInfo            {}   <- empty on a healthy session
    modules.applianceList.payload             keys ['appliances']
    modules.applianceList.payload.appliances  <list of 1>, carrying PK / SK /
                                              sfPersonAccountId on 1/1 entries

The identifier VALUES are synthetic (`ACCOUNT-OURS`, the MAC, the serial): the shape
is the evidence, the values were never the point, and a fixture in a public repo is
not where a real account id belongs. The MAC and serial are deliberately hostile
strings so the leak tests over the finished dump have something to look for.

`reporter()` is the envelope of the user who sees NO appliances -- and it is an
honest reconstruction, not a capture. Their log records exactly two levels (log lines
1989/1997): the `result` key set and the `modules` key set. Both are IDENTICAL to the
healthy capture, which is what closed the "the API changed" hypothesis. Everything
below `modules.applianceList` is unrecorded, so it is filled here with the healthy
shape ON PURPOSE: that is the null hypothesis this block exists to test, and the only
difference between the two dicts is the one thing their dump did report -- an empty
list. If the fields added on top of it cannot separate these two documents, they are
not worth shipping.
"""
from __future__ import annotations

import copy
from typing import Any

# The account id the id_token of the session claims (`custom_attributes
# .PersonAccountId`), and the one every appliance of a healthy response repeats under
# `sfPersonAccountId`. Never emitted by anything under test: the transport compares
# them and publishes a verdict.
OUR_ACCOUNT = "ACCOUNT-OURS"
OTHER_ACCOUNT = "ACCOUNT-SOMEONE-ELSE"

_APPLIANCE: dict[str, Any] = {
    # The DynamoDB single-table markers, present on 1/1 entries of the live capture:
    # the list is a query BY IDENTITY, which is why an identity check on it is
    # meaningful at all.
    "PK": "user#eu-west-1:SYNTHETIC-IDENTITY",
    "SK": "app#AA:BB:CC:DD:EE:FF",
    "sfPersonAccountId": OUR_ACCOUNT,
    "applianceTypeName": "REF",
    "macAddress": "AA:BB:CC:DD:EE:FF",
    "serialNumber": "PLAINTEXT-SERIAL",
    "nickName": "Kitchen Fridge",
}


def _envelope(appliances: list) -> dict[str, Any]:
    return {
        "executionTime": 114,
        "success": True,
        "modules": {
            "applianceList": {
                "authInfo": {},
                "executionTime": 114,
                "success": True,
                "payload": {"appliances": appliances},
            }
        },
    }


def healthy() -> dict[str, Any]:
    """The live 2026-08-24 capture: 200, one appliance, both `success` true."""
    return _envelope([copy.deepcopy(_APPLIANCE)])


def reporter() -> dict[str, Any]:
    """ADDHON-210 as reported: the same envelope, and nothing in the list."""
    return _envelope([])
