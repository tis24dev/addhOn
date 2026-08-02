# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The response-stream fake, in its own module so the contract check can import it.

Lives apart from `test_program_labels.py` because `tests/_aiohttp_contract.py` runs in a
clean subprocess -- importing the test module there would drag in the whole Home
Assistant stub installation, which is exactly what that subprocess exists to avoid.
"""
from __future__ import annotations


class FakeContent:
    """Stream that honours aiohttp's ACTUAL `StreamReader` contract.

    The first version of this fake returned the whole body from `read(size)` regardless
    of `size`. aiohttp does not do that: `read(n)` returns *at most* n bytes and returns
    as soon as the buffer holds anything, so on a multi-megabyte body it hands back a
    single ~64 KiB high-water-mark slice. The forgiving fake made a short-read bug in
    `async_fetch_catalog` invisible while the whole suite stayed green -- the feature was
    dead for every user and nothing failed. Modelling the real semantics is what turns
    that class of bug back into a test failure.

    `tests/_aiohttp_contract.py` pins this fidelity against the real library, so the fake
    cannot drift back into being more forgiving than aiohttp.
    """

    def __init__(self, body: bytes, chunk: int = 64 * 1024) -> None:
        self._chunks = [body[i : i + chunk] for i in range(0, len(body), chunk)] or [b""]

    async def read(self, size: int | None = None) -> bytes:
        # aiohttp-like short read: at most ONE buffered chunk, never the whole body.
        first = self._chunks[0]
        return first if size is None else first[:size]

    async def iter_chunked(self, size: int):
        for chunk in self._chunks:
            yield chunk
