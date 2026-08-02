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

IMPORTING THIS MODULE DOES NOTHING. Every side effect -- the `sys.path` entries, the
package shim, the aiohttp probe and the checks themselves -- lives inside `run()`. That
matters because in `tests/` a leading underscore otherwise means "helper module that is
imported" (`_golden`, `_fake_stream`, `_fingerprint` all are), so this file wears a
costume it does not fit. An accidental `import _aiohttp_contract` at module scope would
otherwise have killed the interpreter through `sys.exit(77)`, or shadowed the real
`custom_components` package for the rest of the session.

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

EXIT_NO_AIOHTTP = 77

CATALOG = {"WM_WD": {"HQD_AUTOCLEAN": "Drum Cleaning", "IOT_WASH_WOOL": "Wool"}}


def _body() -> bytes:
    """A catalog whose PROGRAMS member sits after a multi-megabyte pad.

    The pad is the point: a short read must not be able to reach PROGRAMS, otherwise a
    truncated body would still satisfy the assertion.
    """
    return json.dumps({"PAD": "x" * 3_000_000, "PROGRAMS": CATALOG}).encode()


def _prepare():
    """Make the transport importable here, and return the pieces the checks need.

    Imports the transport submodule WITHOUT executing
    `custom_components/addhon/__init__.py`, which pulls in Home Assistant -- the same
    shim the `apk/` probes use. Called only from `run()`, never at import.
    """
    for path in (str(REPO_ROOT), str(TESTS_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)

    for name in (
        "custom_components",
        "custom_components.addhon",
        "custom_components.addhon.client",
        "custom_components.addhon.client.transport",
    ):
        module = types.ModuleType(name)
        module.__path__ = [str(REPO_ROOT / name.replace(".", "/"))]
        sys.modules[name] = module

    from custom_components.addhon.client.transport import translations
    from _fake_stream import FakeContent

    return translations, FakeContent


async def _serve(web, body: bytes):
    """Start a local aiohttp server on an ephemeral port; return (runner, url)."""

    async def handler(_request):
        return web.Response(body=body, content_type="application/json")

    app = web.Application()
    app.router.add_get("/catalog.json", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{runner.addresses[0][1]}/catalog.json"


async def _checks(aiohttp, web, translations, FakeContent) -> list[str]:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'} {name}{(' -- ' + detail) if detail else ''}")
        if not ok:
            failures.append(name)

    body = _body()
    runner, url = await _serve(web, body)
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
                real_partial = await response.content.read(len(body) + 1)
        fake_partial = await FakeContent(body).read(len(body) + 1)
        check(
            "FakeContent models aiohttp's short read",
            (real_partial != body) == (fake_partial != body),
            f"real short={real_partial != body} fake short={fake_partial != body}",
        )
    finally:
        await runner.cleanup()
    return failures


def run() -> int:
    """Run every check; return the process exit code."""
    try:
        import aiohttp
        from aiohttp import web
    except ImportError:
        return EXIT_NO_AIOHTTP

    translations, FakeContent = _prepare()
    failures = asyncio.run(_checks(aiohttp, web, translations, FakeContent))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
