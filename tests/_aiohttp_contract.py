# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Contract check of `async_fetch_catalog` against the REAL aiohttp, in a clean process.

WHY A SUBPROCESS. The suite installs a minimal `aiohttp` stub (`tests/_golden.py`) so the
modules that must import without the library can. In a full run that stub is already in
`sys.modules` long before any test that wants the real thing, so an in-process check
would either skip forever (never running in CI, where aiohttp IS installed) or drive the
stub and fail for the wrong reason. A fresh interpreter has no stubs at all.

WHY IT EXISTS AT ALL. Every other test of this code path talks to a hand-written fake,
and a fake can only assert the semantics its author believed in. That is not
hypothetical: the first version of this suite passed completely while the feature was
dead in production, because the fake's `read(size)` returned the whole body while
aiohttp's returns at most one buffered chunk. Only the real library is self-checking.

Exit codes: 0 = all checks passed, 1 = a check failed (details on stdout),
77 = aiohttp is not installed (the caller skips).
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
for path in (str(REPO_ROOT), str(TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    sys.exit(77)

# Import the transport submodule WITHOUT executing custom_components/addhon/__init__.py,
# which pulls in Home Assistant. Same shim the apk/ probes use.
for _pkg in (
    "custom_components",
    "custom_components.addhon",
    "custom_components.addhon.client",
    "custom_components.addhon.client.transport",
):
    _module = types.ModuleType(_pkg)
    _module.__path__ = [str(REPO_ROOT / _pkg.replace(".", "/"))]
    sys.modules[_pkg] = _module

from custom_components.addhon.client.transport import translations  # noqa: E402
from _fake_stream import FakeContent  # noqa: E402

CATALOG = {"WM_WD": {"HQD_AUTOCLEAN": "Drum Cleaning", "IOT_WASH_WOOL": "Wool"}}

# PROGRAMS sits AFTER a multi-megabyte pad on purpose: a short read must not be able to
# reach it, otherwise a truncated body would still satisfy the assertion.
BODY = json.dumps({"PAD": "x" * 3_000_000, "PROGRAMS": CATALOG}).encode()

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}{(' -- ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


async def serve(body: bytes):
    async def handler(_request):
        return web.Response(body=body, content_type="application/json")

    app = web.Application()
    app.router.add_get("/catalog.json", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return runner, f"http://127.0.0.1:{port}/catalog.json"


async def main() -> None:
    runner, url = await serve(BODY)
    try:
        # 1. The check that would have caught the shipped bug.
        async with aiohttp.ClientSession() as session:
            catalog = await translations.async_fetch_catalog(session, url)
        check(
            "multi-megabyte catalog arrives whole",
            catalog == CATALOG,
            f"got {catalog!r}",
        )

        # 2. The size cap must act on a real stream, not only on the fake.
        original = translations._MAX_CATALOG_BYTES
        translations._MAX_CATALOG_BYTES = 1024
        try:
            async with aiohttp.ClientSession() as session:
                try:
                    await translations.async_fetch_catalog(session, url)
                    check("cap enforced against a real stream", False, "no ValueError")
                except ValueError:
                    check("cap enforced against a real stream", True)
        finally:
            translations._MAX_CATALOG_BYTES = original

        # 3. Fidelity: FakeContent must agree with aiohttp on the short read, so the
        #    offline tests keep catching a regression to `content.read(cap)`. Compared
        #    rather than hard-coded: if a future aiohttp read to EOF instead, this tells
        #    us the fake may be relaxed, rather than leaving it modelling a dead hazard.
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                real_partial = await response.content.read(len(BODY) + 1)
        fake_partial = await FakeContent(BODY).read(len(BODY) + 1)
        check(
            "FakeContent models aiohttp's short read",
            (real_partial != BODY) == (fake_partial != BODY),
            f"real short={real_partial != BODY} fake short={fake_partial != BODY}",
        )
    finally:
        await runner.cleanup()


asyncio.run(main())
sys.exit(1 if failures else 0)
