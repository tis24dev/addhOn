"""Helpers for the native engine golden tests (Phase 4 slice 5b).

After deleting `_vendor/`, the old differential tests (native vs pyhOn) become
golden tests: the NATIVE output (proven == pyhOn at checkpoint 5a, commit
520f036, refuter-validated) is frozen into a JSON and we verify the native side
does not regress.

Usage:
    from _golden import install_stubs, frozen
    install_stubs()
    ...
    self.assertEqual(snapshot, frozen("engine_parameters", snapshot))

Regenerate the golden files (after an INTENTIONAL change of the native behavior):
    GEN_GOLDEN=1 python3 -m pytest tests/test_engine_*.py
"""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_GOLDEN_DIR = REPO / "tests" / "golden"


def _mod(name: str) -> types.ModuleType:
    m = sys.modules.get(name)
    if m is None:
        m = types.ModuleType(name)
        sys.modules[name] = m
    return m


def install_stubs() -> None:
    """Minimal stubs for homeassistant/aiohttp/yarl: importing the addhon package
    pulls in homeassistant; the engine then imports without awscrt."""
    ce = _mod("homeassistant.config_entries")
    ce.ConfigEntry = getattr(ce, "ConfigEntry", type("ConfigEntry", (), {}))
    core = _mod("homeassistant.core")
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exc = _mod("homeassistant.exceptions")
    base = getattr(exc, "HomeAssistantError", type("HomeAssistantError", (Exception,), {}))
    exc.HomeAssistantError = base
    exc.ConfigEntryNotReady = getattr(exc, "ConfigEntryNotReady", type("ConfigEntryNotReady", (base,), {}))
    exc.ConfigEntryAuthFailed = getattr(exc, "ConfigEntryAuthFailed", type("ConfigEntryAuthFailed", (base,), {}))
    uc = _mod("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = getattr(uc, "DataUpdateCoordinator", type("DataUpdateCoordinator", (), {}))
    uc.UpdateFailed = getattr(uc, "UpdateFailed", type("UpdateFailed", (Exception,), {}))
    ha = _mod("homeassistant")
    ha.config_entries, ha.core, ha.exceptions = ce, core, exc
    ha.helpers = _mod("homeassistant.helpers")
    ha.helpers.update_coordinator = uc
    yarl = _mod("yarl")
    if not hasattr(yarl, "URL"):
        yarl.URL = type("URL", (), {"__init__": lambda self, s, encoded=False: None})
    aio = _mod("aiohttp")
    aio.ClientSession = getattr(aio, "ClientSession", type("ClientSession", (), {}))
    aio.ClientResponse = getattr(aio, "ClientResponse", type("ClientResponse", (), {}))
    aio.ContentTypeError = getattr(aio, "ContentTypeError", type("ContentTypeError", (Exception,), {}))
    aio.client = _mod("aiohttp.client")
    aio.client._RequestContextManager = type("_RCM", (), {})


def normalize(value):
    """JSON round-trip for stable comparisons (tuple->list, datetime->str, etc.).
    Must be applied to the 'current' side of the comparison too, not just the golden."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


_normalize = normalize  # internal alias


def frozen(name: str, value):
    """Return the golden for `name`. With GEN_GOLDEN it writes `value` as the new
    golden; otherwise it loads it from disk. The value is always normalized via JSON."""
    path = _GOLDEN_DIR / f"{name}.json"
    norm = _normalize(value)
    if os.environ.get("GEN_GOLDEN"):
        _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(norm, indent=2, sort_keys=True), encoding="utf-8")
        return norm
    return json.loads(path.read_text(encoding="utf-8"))
