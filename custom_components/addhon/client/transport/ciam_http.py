# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The one HTTP exchange of every CIAM step (`ciam.CiamAuth`).

`authorize` is a GET whose query carries the password, as the app sends it
(apk2 decomp.txt @3315470-3315511); the other steps are JSON POSTs. Whatever the step,
an error that could print the request never leaves with it.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .ciam_protocol import check
from .headers import USER_AGENT
from .values import CIAM_URL

_LOGGER = logging.getLogger(__name__)
_HEADERS = {"user-agent": USER_AGENT, "Content-Type": "application/json;charset=utf-8"}


async def exchange(auth: Any, step: str, payload: dict[str, str]) -> dict[str, Any]:
    """Run `step` of `auth` (a `CiamAuth`) and return its answer, checked.

    The step is the login phase meanwhile (`auth._current_phase`, the shared phase
    tracker, the DEBUG trace), as `HonAuth` names its own steps. An error that holds
    the request (aiohttp's `request_info`) or prints the password holds the password:
    it is re-raised bare, without its cause. The others (DNS, refused, timeout) name
    the host only and keep their classification (`ciam_protocol.check` judges the
    answers).
    """
    auth._current_phase = f"ciam_{step}"
    if auth._phase_tracker is not None:
        auth._phase_tracker.step(auth._current_phase)
    _LOGGER.debug("auth phase %s", auth._current_phase)
    session: aiohttp.ClientSession = auth._session
    url = f"{CIAM_URL}/{step}"
    try:
        request = (
            session.get(url, params=payload, headers=_HEADERS)
            if step == "authorize"
            else session.post(url, json=payload, headers=_HEADERS)
        )
        async with request as response:
            status = response.status
            try:
                data = await response.json(content_type=None)
            except ValueError:
                data = None
    except Exception as err:
        if getattr(err, "request_info", None) is not None or auth._password in str(err):
            raise RuntimeError(f"ciam {step}: transport error {type(err).__name__}") from None
        raise
    body = data if isinstance(data, dict) else {}
    check(step, status, body)
    return body
