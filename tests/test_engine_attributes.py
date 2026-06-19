"""Golden test of the native attribute (Phase 4). Freezes construction/update/lock
of HonAttribute on the REAL fridge shadow data + synthetic cases.

History: it used to be differential vs pyhOn; with `_vendor/` deleted it is golden
(native output proven == pyhOn at checkpoint 5a). Intentional divergence pinned:
lock with `datetime.now(timezone.utc)` (aware) instead of the deprecated `utcnow()`.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _golden import REPO, frozen, install_stubs, normalize  # noqa: E402

install_stubs()
_DUMP = REPO / "tests" / "fixtures" / "ref_10136" / "attributes.json"

from custom_components.addhon.client.engine.attributes import HonAttribute as NaAttr  # noqa: E402


def _real_shadow_params() -> dict:
    data = json.loads(_DUMP.read_text(encoding="utf-8"))
    return data.get("shadow", {}).get("parameters", {})


def _snap(a) -> dict:
    return {"value": a.value, "str": str(a), "last_update": a.last_update, "lock": a.lock,
            "value_type": type(a.value).__name__}


def _native_snapshot() -> dict:
    params = _real_shadow_params()
    out: dict = {"construct": {}, "str_update": {}, "dict_update": {}}
    new = {"parNewVal": "7", "lastUpdate": "2024-05-01T12:00:00"}
    for name, data in params.items():
        out["construct"][name] = _snap(NaAttr(dict(data)))
        a = NaAttr(dict(data))
        a.update("42")
        out["str_update"][name] = _snap(a)
        b = NaAttr(dict(data))
        b.update(dict(new))
        out["dict_update"][name] = _snap(b)
    # synthetic cases
    out["synthetic_values"] = {
        v: _snap(NaAttr({"parNewVal": v}))
        for v in ["5.5", "5,5", "-3,25", "12.0", "abc", "00", "-16", " 5 ", ""]
    }
    out["missing_parnewval"] = [_snap(NaAttr({"lastUpdate": "2024-01-01T00:00:00"})), _snap(NaAttr({}))]
    out["nonstring"] = {str(v): NaAttr({"parNewVal": v}).value for v in (7, 5.5, True)}
    return out


class AttributeGoldenTest(unittest.TestCase):
    def test_dump_has_params(self) -> None:
        self.assertTrue(_real_shadow_params())

    def test_native_matches_golden(self) -> None:
        snap = _native_snapshot()
        self.assertEqual(normalize(snap), frozen("engine_attributes", snap))


class NativeAttributeBehaviorTest(unittest.TestCase):
    def test_invalid_last_update_after_valid_resets(self) -> None:
        a = NaAttr({"parNewVal": "1", "lastUpdate": "2024-01-01T00:00:00"})
        self.assertIsNotNone(a.last_update)
        a.update({"parNewVal": "2", "lastUpdate": "garbage"})
        self.assertIsNone(a.last_update)

    def test_nonstring_last_update_no_crash(self) -> None:
        # non-string lastUpdate from the cloud: fromisoformat raises TypeError, which
        # is now handled (last_update=None) instead of propagating during construction.
        a = NaAttr({"parNewVal": "5", "lastUpdate": 1717000000})
        self.assertIsNone(a.last_update)

    def test_missing_parnewval_on_update_resets(self) -> None:
        a = NaAttr({"parNewVal": "5", "lastUpdate": "2024-01-01T00:00:00"})
        a.update({"lastUpdate": "2024-02-02T00:00:00"})  # no parNewVal
        self.assertEqual(a._value, "")

    def test_nonstring_none_raises(self) -> None:
        with self.assertRaises(TypeError):
            _ = NaAttr({"parNewVal": None}).value

    def test_fresh_lock_blocks_nonshield_update(self) -> None:
        a = NaAttr({"parNewVal": "0"})
        self.assertTrue(a.update({"parNewVal": "5"}, shield=True))
        self.assertTrue(a.lock)
        self.assertFalse(a.update({"parNewVal": "999"}))  # rejected while locked
        self.assertEqual(a.value, 5)
        self.assertTrue(a.update({"parNewVal": "999"}, shield=True))  # shield passes through
        self.assertEqual(a.value, 999)

    def test_no_lock_by_default(self) -> None:
        self.assertFalse(NaAttr({"parNewVal": "0"}).lock)

    def test_stale_lock_expires(self) -> None:
        a = NaAttr({"parNewVal": "0"})
        a._lock_timestamp = datetime.now(timezone.utc) - timedelta(seconds=20)
        self.assertFalse(a.lock)
        self.assertTrue(a.update({"parNewVal": "3"}))

    def test_lock_timestamp_is_timezone_aware(self) -> None:
        a = NaAttr({"parNewVal": "0"})
        a.update({"parNewVal": "1"}, shield=True)
        self.assertIsNotNone(a._lock_timestamp.tzinfo)
        self.assertEqual(a._lock_timestamp.utcoffset(), timedelta(0))
        self.assertTrue(a.lock)


if __name__ == "__main__":
    unittest.main()
