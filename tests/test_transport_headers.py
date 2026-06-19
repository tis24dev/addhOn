"""Differential test of the transport's 4th piece: build_auth_headers.

Oracle = pyhOn's header construction: `ConnectionHandler._HEADERS | headers`
(handler/base.py:18-21 + handler/hon.py:66-68), where `headers` = caller's extra
+ the two tokens. `_HEADERS` uses `const.USER_AGENT`: we load it from the real
const.py (pure, importable on its own) so the test also pins the UA drift.
handler/base.py imports aiohttp -> not importable on its own, so `_HEADERS`
(2 keys) is transcribed verbatim.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_OUR_HEADERS = _ROOT / "custom_components" / "addhon" / "client" / "transport" / "headers.py"
# pyhOn USER_AGENT (now OURS): transcribed after deleting `_vendor/`.
_USER_AGENT = "Chrome/999.999.999.999"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _pyhon_headers(user_agent, cognito_token, id_token, extra=None):
    """Verbatim: pyhon _HEADERS | (extra + token)."""
    base = {"user-agent": user_agent, "Content-Type": "application/json"}
    headers = dict(extra) if extra else {}
    headers["cognito-token"] = cognito_token
    headers["id-token"] = id_token
    return base | headers


class BuildAuthHeadersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.build = _load(_OUR_HEADERS, "addhon_transport_headers").build_auth_headers
        self.ua = _USER_AGENT

    def test_matches_pyhon(self) -> None:
        cases = [
            ("COG", "IDT", None),
            ("", "", None),
            ("c", "i", {}),
            ("c", "i", {"x-extra": "1"}),
            ("c", "i", {"user-agent": "OVERRIDE/1.0"}),          # extra overrides the base UA
            ("c", "i", {"cognito-token": "WILL_BE_REPLACED"}),    # the real token wins over the extra
            ("c", "i", {"Content-Type": "text/plain", "id-token": "X"}),
        ]
        for cog, idt, extra in cases:
            with self.subTest(extra=extra):
                self.assertEqual(
                    self.build(cog, idt, extra),
                    _pyhon_headers(self.ua, cog, idt, extra),
                )

    def test_pinned(self) -> None:
        self.assertEqual(
            self.build("C", "I"),
            {
                "user-agent": "Chrome/999.999.999.999",
                "Content-Type": "application/json",
                "cognito-token": "C",
                "id-token": "I",
            },
        )

    def test_ua_matches_vendored_const(self) -> None:
        # Drift pin: our USER_AGENT must equal pyhOn's.
        our_ua = _load(_OUR_HEADERS, "addhon_transport_headers2").USER_AGENT
        self.assertEqual(our_ua, self.ua)

    def test_tokens_always_present_and_win(self) -> None:
        h = self.build("REAL_COG", "REAL_ID", {"cognito-token": "fake", "id-token": "fake"})
        self.assertEqual(h["cognito-token"], "REAL_COG")
        self.assertEqual(h["id-token"], "REAL_ID")


if __name__ == "__main__":
    unittest.main()
