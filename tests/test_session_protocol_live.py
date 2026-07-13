# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LIVE check that the real `NativeHon` satisfies the seam's HonSession Protocol.

This is the "load-bearing" session check: the adapter returns a `NativeHon`
(piece 4a) and the whole plan rests on that object being conformant to
client/interfaces.HonSession. (Conformance is also tested offline in
test_native_session; here we verify it on the real object, with the real
dependencies.)

It requires aiohttp/awsiotsdk (the native transport's runtime dependencies): if
absent (e.g. unit CI without HA), the test is SKIPPED cleanly. When present (real
HA, or the /tmp/hon-dump-venv venv), it actually runs. The import happens in an
isolated SUBPROCESS so it does not pollute the pytest process's sys.modules (the
trick pre-registers empty packages to skip the heavy integration __init__).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def _probe_dependencies() -> tuple[bool, str]:
    # Probe in a clean interpreter. Other test modules deliberately install fake
    # aiohttp/awscrt packages in this process during collection; inspecting
    # sys.modules/find_spec here consequently skipped this live test even when all
    # real dependencies were installed. The protocol assertion already runs in a
    # subprocess, so its dependency gate must use the same isolation boundary.
    result = subprocess.run(
        [sys.executable, "-c", "import aiohttp, awscrt, yarl"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    detail = (result.stderr or result.stdout).strip()
    return result.returncode == 0, detail


class LiveSessionProtocolTest(unittest.TestCase):
    def test_real_hon_satisfies_honsession(self) -> None:
        available, detail = _probe_dependencies()
        if not available:
            suffix = f" ({detail})" if detail else ""
            self.skipTest(
                "aiohttp/awscrt/yarl not available: skipping the real Hon check"
                f"{suffix}"
            )
        script = textwrap.dedent(
            f"""
            import sys, types, importlib.util
            from pathlib import Path
            root = Path({str(_ROOT)!r}); sys.path.insert(0, str(root))
            for pkg in ("custom_components", "custom_components.addhon"):
                m = types.ModuleType(pkg)
                m.__path__ = [str(root / pkg.replace(".", "/"))]
                sys.modules[pkg] = m
            spec = importlib.util.spec_from_file_location(
                "ifc", root / "custom_components/addhon/client/interfaces.py")
            ifc = importlib.util.module_from_spec(spec); spec.loader.exec_module(ifc)
            from custom_components.addhon.client.session import NativeHon
            h = NativeHon(email="x@example.com", password="y")
            assert isinstance(h, ifc.HonSession), "NativeHon NOT conformant to HonSession"
            print("CONFORME")
            """
        )
        res = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(res.returncode, 0, f"stderr:\n{res.stderr}")
        self.assertIn("CONFORME", res.stdout)


if __name__ == "__main__":
    unittest.main()
