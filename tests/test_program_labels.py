# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Readable program names from the hOn translation catalog (issue #71).

The appliance schema names a program with its i18n KEY: the startProgram category
`PROGRAMS.WM_WD.HQD_AUTOCLEAN` is reduced by the engine to the slug `hqd_autoclean`,
which is what both the program select and the program-name sensor used to display.
These tests pin the three layers that turn that key back into a label:

  * transport  -- the anonymous app-config -> jsonPath -> catalog chain, and the
    memory-bounded extraction of the `PROGRAMS` subtree out of a ~7.6 MB document;
  * program_labels -- the appliance-type -> namespace mapping and the lookup, including
    the misses that MUST leave the text alone;
  * entities -- the select translating only what it DISPLAYS (codes stay raw, because
    they are what gets sent to the appliance) and the program-name sensor.

The label expectations below are the real catalog's (`language-en-6192.json`, fetched
2026-08-02 by apk/probe_translations_live.py), not guesses: several differ from the
obvious slug-to-title-case rendering, which is precisely why a formatter cannot replace
the catalog.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

# Importing these installs the homeassistant stubs and gives us the fakes: the select
# module brings the coordinator/command fakes, the sensor module brings the
# `homeassistant.components.sensor` stubs the sensor platform needs at import time.
from test_program_select import (  # noqa: E402
    FakeClient,
    FakeCoordinator,
    Param,
    RecordingCommand,
)
import test_sensor_per_type  # noqa: E402,F401 - imported for its stub installation
from _fake_stream import FakeContent  # noqa: E402

from custom_components.addhon import program_labels  # noqa: E402
from custom_components.addhon.client.transport import translations  # noqa: E402

# A trimmed slice of the real catalog: the labels are verbatim from
# language-en-6192.json.
CATALOG = {
    "WM_WD": {
        "HQD_AUTOCLEAN": "Drum Cleaning",
        "HQD_QUICK_15": "Quick 15'",
        "HQD_ECO_40_60_DEGREES": "Eco 40-60",
        "HQD_20_DEGREES": "Cotton 20°C",
        "HQD_SMART": "Smart A.I.",
        "IOT_WASH_BED_LINEN": "Bed Linen",
        "IOT_WASH_WOOL": "Wool",
    },
    "TD": {"HQD_COTTON": "Cotton", "HQD_ECO": "Eco"},
}


def _washer_with_type(commands: dict, app_type: str = "WM") -> dict:
    """`_washer` from test_program_select, with a settable appliance type.

    The type is what selects the catalog namespace, so it has to vary per test.
    """
    return {
        "washer-1": {
            "type": app_type,
            "name": "Washer",
            "appliance": types.SimpleNamespace(commands=commands),
            "attributes": {},
            "settings": {},
        }
    }


def _coordinator_with_catalog(data: dict, catalog=CATALOG) -> FakeCoordinator:
    coordinator = FakeCoordinator(data)
    setattr(
        coordinator,
        program_labels.COORDINATOR_ATTR,
        program_labels.ProgramLabels(catalog),
    )
    return coordinator


class FakeResponse:
    def __init__(self, *, payload=None, body: bytes | None = None, chunk: int = 64 * 1024) -> None:
        self._payload = payload
        self._body = body or b""
        self.content = FakeContent(self._body, chunk)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def raise_for_status(self) -> None:
        return None

    async def json(self, content_type=None):
        return self._payload

    async def read(self) -> bytes:
        return self._body


class FakeSession:
    """Records every request so the tests can assert the exact wire contract."""

    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None):
        self.calls.append({"url": url, "params": params, "headers": headers or {}})
        for prefix, response in self._responses.items():
            if url.startswith(prefix):
                return response
        raise AssertionError(f"unexpected GET {url}")


class SliceProgramsTest(unittest.TestCase):
    """`_slice_programs` returns the ROOT `PROGRAMS` member, and only that one."""

    def test_extracts_the_programs_object(self) -> None:
        document = json.dumps({"AC": {"X": "y"}, "PROGRAMS": CATALOG, "UI": {"a": "b"}})
        self.assertEqual(CATALOG, translations._slice_programs(document))

    def test_ignores_a_nested_programs_object(self) -> None:
        # THE regression that a substring search gets wrong. The real catalog holds 22
        # occurrences of '"PROGRAMS":' and the FIRST one is nested (a `{"SCENARIOS":...}`
        # belonging to another screen), several thousand bytes before the root member.
        # Only depth tells them apart.
        document = json.dumps(
            {
                "DEMO": {"PROGRAMS": {"SCENARIOS": {"A": "nested, not ours"}}},
                "PROGRAMS": CATALOG,
            }
        )
        self.assertEqual(CATALOG, translations._slice_programs(document))

    def test_ignores_a_decoy_occurrence_inside_a_string_value(self) -> None:
        document = json.dumps(
            {"HELP": 'see "PROGRAMS": in the manual', "PROGRAMS": CATALOG}
        )
        self.assertEqual(CATALOG, translations._slice_programs(document))

    def test_tolerates_whitespace_and_indentation(self) -> None:
        document = json.dumps({"AC": {"X": "y"}, "PROGRAMS": CATALOG}, indent=2)
        self.assertEqual(CATALOG, translations._slice_programs(document))

    def test_raises_when_the_branch_is_absent(self) -> None:
        with self.assertRaises(ValueError):
            translations._slice_programs(json.dumps({"AC": {"X": "y"}}))

    def test_raises_when_the_branch_is_empty(self) -> None:
        # An empty object is indistinguishable from "no catalog" for our purposes and
        # must not be accepted as a successful load.
        with self.assertRaises(ValueError):
            translations._slice_programs(json.dumps({"PROGRAMS": {}}))

    def test_raises_on_a_non_object_document(self) -> None:
        with self.assertRaises(ValueError):
            translations._slice_programs('["PROGRAMS"]')


class ProgramNamespacesTest(unittest.TestCase):
    def test_keeps_only_the_dict_branches(self) -> None:
        # The real PROGRAMS branch mixes per-type namespaces (dicts) with loose string
        # leaves belonging to other screens; only the dicts are addressable as
        # PROGRAMS.<TYPE>.<KEY>.
        programs = {
            "WM_WD": {"HQD_SMART": "Smart A.I."},
            "WM_WD_PROGRAM_IOT_WASH_NAME_WOOL": "Wool",
            "EMPTY": {},
            "NESTED_ONLY": {"SUB": {"deep": "value"}},
        }
        self.assertEqual(
            {"WM_WD": {"HQD_SMART": "Smart A.I."}},
            translations._program_namespaces(programs),
        )


class LoadProgramLabelsTest(unittest.IsolatedAsyncioTestCase):
    """The two-step chain and, crucially, the headers each step needs."""

    def _session(self) -> FakeSession:
        catalog_url = "https://assets.he-cdn.services/languages/language-en-6192.json"
        return FakeSession(
            {
                "https://api-iot.he.services/config/app-config": FakeResponse(
                    payload={
                        "payload": {
                            "forceUpdate": False,
                            "language": {"version": 6192, "jsonPath": catalog_url},
                        }
                    }
                ),
                catalog_url: FakeResponse(
                    body=json.dumps({"PROGRAMS": CATALOG}).encode()
                ),
            }
        )

    async def test_returns_the_program_namespaces(self) -> None:
        catalog = await translations.async_load_program_labels(self._session(), "en")
        self.assertEqual(CATALOG, catalog)

    async def test_a_catalog_spanning_many_chunks_is_read_whole(self) -> None:
        # REGRESSION: the real catalog is ~7.6 MB and arrives over ~120 chunks. Reading it
        # with `content.read(n)` yields only the first one, and the truncated JSON still
        # passes the `startswith(b"{")` guard -- so the failure surfaces as "no labels"
        # rather than as an error. Verified against real aiohttp 3.14: a 3 MB body gave
        # back 98,155 bytes. A tiny chunk size here reproduces the same shape cheaply.
        big = {"PAD": "x" * 200_000, "PROGRAMS": CATALOG}
        catalog_url = "https://assets.he-cdn.services/languages/language-en-6192.json"
        session = FakeSession(
            {
                "https://api-iot.he.services/config/app-config": FakeResponse(
                    payload={"payload": {"language": {"jsonPath": catalog_url}}}
                ),
                catalog_url: FakeResponse(
                    body=json.dumps(big).encode(), chunk=8 * 1024
                ),
            }
        )
        self.assertEqual(
            CATALOG, await translations.async_load_program_labels(session, "en")
        )

    async def test_a_body_over_the_cap_is_refused_while_reading(self) -> None:
        # The cap must be enforced DURING the read, not after buffering everything.
        catalog_url = "https://assets.he-cdn.services/languages/language-en-6192.json"
        session = FakeSession(
            {
                "https://api-iot.he.services/config/app-config": FakeResponse(
                    payload={"payload": {"language": {"jsonPath": catalog_url}}}
                ),
                catalog_url: FakeResponse(
                    body=b"{" + b"x" * 4096, chunk=1024
                ),
            }
        )
        original = translations._MAX_CATALOG_BYTES
        translations._MAX_CATALOG_BYTES = 2048
        try:
            with self.assertRaises(ValueError):
                await translations.async_load_program_labels(session, "en")
        finally:
            translations._MAX_CATALOG_BYTES = original

    async def test_app_config_is_anonymous_but_api_keyed(self) -> None:
        # No session token (the app calls this with useAuth=false) but the SUFFIXED
        # api-key is mandatory: the gateway answers 403 to the bare key.
        session = self._session()
        await translations.async_load_program_labels(session, "it")
        config_call = session.calls[0]
        self.assertTrue(config_call["url"].endswith("/config/app-config"))
        # No version segment: /config/v1/app-config is rejected by the gateway.
        self.assertNotIn("/v1/", config_call["url"])
        self.assertEqual("it", config_call["params"]["languageCode"])
        self.assertEqual(
            translations.CONFIG_API_KEY, config_call["headers"]["x-api-key"]
        )
        self.assertNotIn("Authorization", config_call["headers"])

    async def test_catalog_download_sends_a_user_agent_and_no_api_key(self) -> None:
        # The CDN is behind Cloudflare: no User-Agent -> 403 "error code: 1010". It is
        # a plain asset host, so the api-key has no business being sent there.
        session = self._session()
        await translations.async_load_program_labels(session, "en")
        catalog_call = session.calls[1]
        self.assertTrue(catalog_call["headers"].get("User-Agent"))
        self.assertNotIn("x-api-key", catalog_call["headers"])

    async def test_missing_json_path_raises(self) -> None:
        session = FakeSession(
            {
                "https://api-iot.he.services/config/app-config": FakeResponse(
                    payload={"payload": {"forceUpdate": False}}
                )
            }
        )
        with self.assertRaises(ValueError):
            await translations.async_load_program_labels(session, "en")

    async def test_non_json_body_is_refused(self) -> None:
        # Mirrors the app's own startsWith('{') guard: a captive-portal HTML page must
        # not be accepted as a catalog.
        catalog_url = "https://assets.he-cdn.services/languages/language-en-6192.json"
        session = FakeSession(
            {
                "https://api-iot.he.services/config/app-config": FakeResponse(
                    payload={"payload": {"language": {"jsonPath": catalog_url}}}
                ),
                catalog_url: FakeResponse(body=b"<!DOCTYPE html><html></html>"),
            }
        )
        with self.assertRaises(ValueError):
            await translations.async_load_program_labels(session, "en")

    def test_catalog_url_is_defensive_on_every_hop(self) -> None:
        for broken in (None, {}, {"payload": None}, {"payload": {"language": "x"}}):
            self.assertIsNone(translations.catalog_url(broken))


class RealAiohttpContractTest(unittest.TestCase):
    """Drive `async_fetch_catalog` against the REAL aiohttp, in a clean subprocess.

    Everything else in this file talks to `FakeSession`, and a hand-written fake can only
    assert the semantics its author believed in. That is not hypothetical: the first
    version of this suite passed completely while the feature was dead in production,
    because the fake's `read(size)` returned the whole body while aiohttp's returns at
    most one buffered chunk. Only the real library is self-checking.

    A SUBPROCESS is what makes this runnable at all. The suite installs a minimal
    `aiohttp` stub (tests/_golden.py) that is already in `sys.modules` by the time this
    module is imported in a full run, so an in-process check would drive the stub, or
    skip forever and never run in CI where aiohttp really is installed. The checks
    themselves live in tests/_aiohttp_contract.py.
    """

    def test_real_aiohttp_contract(self) -> None:
        script = Path(__file__).resolve().parent / "_aiohttp_contract.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 77:
            self.skipTest(
                "aiohttp is not installed; Home Assistant supplies it and CI installs "
                "it explicitly (.github/workflows/ci.yml)"
            )
        self.assertEqual(
            0,
            result.returncode,
            f"real-aiohttp contract failed:\n{result.stdout}\n{result.stderr}",
        )


class ProgramLabelsLookupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.labels = program_labels.ProgramLabels(CATALOG)

    def test_washer_resolves_through_the_wm_wd_namespace(self) -> None:
        # A plain washing machine reports PROGRAMS.WM_WD.*: the mapping is deliberately
        # not the identity.
        self.assertEqual("Drum Cleaning", self.labels.label("WM", "hqd_autoclean"))
        self.assertEqual("Bed Linen", self.labels.label("WM", "iot_wash_bed_linen"))

    def test_washer_dryer_shares_the_washer_namespace(self) -> None:
        self.assertEqual("Wool", self.labels.label("WD", "iot_wash_wool"))

    def test_dryer_uses_its_own_namespace(self) -> None:
        self.assertEqual("Cotton", self.labels.label("TD", "hqd_cotton"))
        # ...and must not leak the washer's namespace.
        self.assertIsNone(self.labels.label("TD", "hqd_autoclean"))

    def test_unknown_code_type_and_empty_catalog_return_none(self) -> None:
        self.assertIsNone(self.labels.label("WM", "hqd_not_in_catalog"))
        self.assertIsNone(self.labels.label("XX", "hqd_autoclean"))
        self.assertIsNone(self.labels.label("WM", None))
        self.assertIsNone(program_labels.EMPTY.label("WM", "hqd_autoclean"))

    def test_apply_translates_and_preserves_everything_else(self) -> None:
        # "Ariel Fresh Clean" is a favourite: the schema stores it under the name the
        # user gave it in the app, it has no catalog key, and it must survive verbatim.
        programs = {
            "hqd_autoclean": "hqd_autoclean",
            "Ariel Fresh Clean": "Ariel Fresh Clean",
            "hqd_unknown": "hqd_unknown",
        }
        self.assertEqual(
            {
                "hqd_autoclean": "Drum Cleaning",
                "Ariel Fresh Clean": "Ariel Fresh Clean",
                "hqd_unknown": "hqd_unknown",
            },
            self.labels.apply("WM", programs),
        )

    def test_apply_preserves_insertion_order(self) -> None:
        # The select builds its option list from this map; a reshuffle would move the
        # entries around in the UI on every restart.
        programs = {"iot_wash_wool": "iot_wash_wool", "hqd_smart": "hqd_smart"}
        self.assertEqual(
            ["iot_wash_wool", "hqd_smart"], list(self.labels.apply("WM", programs))
        )

    def test_for_coordinator_falls_back_to_the_empty_catalog(self) -> None:
        self.assertIs(
            program_labels.EMPTY, program_labels.for_coordinator(types.SimpleNamespace())
        )

    def test_real_catalog_labels_defeat_a_generic_formatter(self) -> None:
        # Guards the design decision: these are the catalog's real labels, and none of
        # them is reachable by stripping the prefix and title-casing the slug. If this
        # ever passes with a formatter, the formatter is wrong, not this test.
        for code, label in (
            ("hqd_quick_15", "Quick 15'"),
            ("hqd_eco_40_60_degrees", "Eco 40-60"),
            ("hqd_20_degrees", "Cotton 20°C"),
            ("hqd_smart", "Smart A.I."),
        ):
            with self.subTest(code=code):
                self.assertEqual(label, self.labels.label("WM", code))
                formatted = code.replace("hqd_", "").replace("_", " ").title()
                self.assertNotEqual(formatted, label)


@contextlib.contextmanager
def _patched(module, name, replacement):
    """Temporarily swap a module attribute, restoring it even on failure."""
    original = getattr(module, name)
    setattr(module, name, replacement)
    try:
        yield
    finally:
        setattr(module, name, original)


class FakeStore:
    """Stand-in for homeassistant.helpers.storage.Store, shared across instances.

    `async_load` constructs its own Store, so the test cannot inject one: the saved
    payload is kept on the CLASS, which is also what makes it survive the simulated
    restart in the cache tests.
    """

    saved: dict | None = None
    load_error: Exception | None = None
    saves: int = 0

    def __init__(self, hass, version, key) -> None:
        self.key = key

    async def async_load(self):
        if FakeStore.load_error is not None:
            raise FakeStore.load_error
        return FakeStore.saved

    async def async_save(self, data) -> None:
        FakeStore.saved = data
        FakeStore.saves += 1

    @classmethod
    def reset(cls) -> None:
        cls.saved = None
        cls.load_error = None
        cls.saves = 0


def _install_ha_helper_stubs(session_factory) -> list[tuple[object, str, object, bool]]:
    """Wire the two helpers `async_load` imports lazily; return what to restore.

    `sys.modules.setdefault` hands back the EXISTING module when one is already there, so
    these assignments land on process-global objects and would otherwise outlive the test
    that made them. Today only the modules under test resolve these two symbols, so the
    leak is inert -- but the file already has `_patched` for exactly this hazard, and an
    inert leak is still a trap for whoever adds the next consumer.
    """
    client = sys.modules.setdefault(
        "homeassistant.helpers.aiohttp_client",
        types.ModuleType("homeassistant.helpers.aiohttp_client"),
    )
    storage = sys.modules.setdefault(
        "homeassistant.helpers.storage",
        types.ModuleType("homeassistant.helpers.storage"),
    )
    previous = [
        (client, "async_get_clientsession",
         getattr(client, "async_get_clientsession", None),
         hasattr(client, "async_get_clientsession")),
        (storage, "Store", getattr(storage, "Store", None), hasattr(storage, "Store")),
    ]
    client.async_get_clientsession = session_factory
    storage.Store = FakeStore
    return previous


def _restore_ha_helper_stubs(previous) -> None:
    """Undo `_install_ha_helper_stubs`, deleting attributes that did not exist before."""
    for module, name, value, existed in previous:
        if existed:
            setattr(module, name, value)
        else:
            delattr(module, name)


class AsyncLoadDegradationTest(unittest.IsolatedAsyncioTestCase):
    """`async_load` runs inside async_setup_entry, so it must NEVER raise."""

    def setUp(self) -> None:
        FakeStore.reset()
        self._stubs = _install_ha_helper_stubs(lambda hass: FakeSession({}))
        self.addCleanup(lambda: _restore_ha_helper_stubs(self._stubs))
        self.hass = types.SimpleNamespace(config=types.SimpleNamespace(language="en"))

    async def test_a_failing_fetch_degrades_to_the_empty_catalog(self) -> None:
        # FakeSession({}) raises AssertionError on any URL: an arbitrary non-network
        # failure must still be absorbed.
        self.assertIs(program_labels.EMPTY, await program_labels.async_load(self.hass))

    async def test_a_timeout_degrades_to_the_empty_catalog(self) -> None:
        # A slow CDN must not stall the integration's startup.
        original = program_labels.FETCH_TIMEOUT
        program_labels.FETCH_TIMEOUT = 0.01

        async def _slow(session, language):
            await asyncio.sleep(1)

        with _patched(translations, "async_app_config", _slow):
            try:
                labels = await program_labels.async_load(self.hass)
            finally:
                program_labels.FETCH_TIMEOUT = original
        self.assertIs(program_labels.EMPTY, labels)

    async def test_a_regional_language_falls_back_to_its_base_code(self) -> None:
        # HA reports e.g. "pt-BR"; the catalog is keyed by the base language.
        seen: list[str] = []

        async def _capture(session, language):
            seen.append(language)
            raise RuntimeError("stop here")

        with _patched(translations, "async_app_config", _capture):
            hass = types.SimpleNamespace(config=types.SimpleNamespace(language="pt-BR"))
            await program_labels.async_load(hass)
        self.assertEqual(["pt"], seen)


class CatalogCacheTest(unittest.IsolatedAsyncioTestCase):
    """The cache exists so the select's OPTION LIST cannot change under a failed fetch.

    In HA a select's options are its state: if a restart that cannot reach the CDN
    reverted them to raw codes, every automation calling `select.select_option` with a
    label would fail and the recorder history would split in two.
    """

    CATALOG_URL = "https://assets.he-cdn.services/languages/language-en-6192.json"

    def setUp(self) -> None:
        FakeStore.reset()
        # Snapshot ONCE, before any install: later installs overwrite what an earlier one
        # put there, so restoring the first snapshot is what returns the modules to their
        # pre-test state.
        self._stubs = _install_ha_helper_stubs(lambda hass: FakeSession({}))
        self.addCleanup(lambda: _restore_ha_helper_stubs(self._stubs))
        self.hass = types.SimpleNamespace(config=types.SimpleNamespace(language="en"))

    def _session(self, version=6192, downloads=None):
        session = FakeSession(
            {
                "https://api-iot.he.services/config/app-config": FakeResponse(
                    payload={
                        "payload": {
                            "language": {
                                "version": version,
                                "jsonPath": self.CATALOG_URL,
                            }
                        }
                    }
                ),
                self.CATALOG_URL: FakeResponse(
                    body=json.dumps({"PROGRAMS": CATALOG}).encode()
                ),
            }
        )
        _install_ha_helper_stubs(lambda hass: session)
        return session

    def _catalog_downloads(self, session) -> int:
        return sum(1 for c in session.calls if c["url"] == self.CATALOG_URL)

    async def test_first_run_downloads_and_persists(self) -> None:
        session = self._session()
        labels = await program_labels.async_load(self.hass)
        self.assertEqual("Drum Cleaning", labels.label("WM", "hqd_autoclean"))
        self.assertEqual(1, self._catalog_downloads(session))
        self.assertEqual("en", FakeStore.saved["language"])
        self.assertEqual("6192", FakeStore.saved["version"])

    async def _seed_cache(self) -> None:
        """A first successful run, i.e. the state a restart inherits."""
        self._session()
        await program_labels.async_load(self.hass)
        self.assertIsNotNone(FakeStore.saved)

    async def test_an_unchanged_version_skips_the_download(self) -> None:
        # app-config is ~1 KB and carries the version, so confirming freshness must not
        # cost the ~7.6 MB catalog.
        await self._seed_cache()
        session = self._session()
        labels = await program_labels.async_load(self.hass)
        self.assertEqual(0, self._catalog_downloads(session))
        self.assertEqual("Drum Cleaning", labels.label("WM", "hqd_autoclean"))

    async def test_a_new_version_re_downloads_and_re_persists(self) -> None:
        await self._seed_cache()
        session = self._session(version=7000)
        await program_labels.async_load(self.hass)
        self.assertEqual(1, self._catalog_downloads(session))
        self.assertEqual("7000", FakeStore.saved["version"])

    async def test_a_failed_refresh_keeps_the_cached_options(self) -> None:
        # THE reason the cache exists: a restart that cannot reach the cloud must not
        # change the select's option strings under the user's automations.
        await self._seed_cache()
        _install_ha_helper_stubs(lambda hass: FakeSession({}))  # every URL raises
        labels = await program_labels.async_load(self.hass)
        self.assertEqual("Drum Cleaning", labels.label("WM", "hqd_autoclean"))

    async def test_a_cache_for_another_language_is_not_reused(self) -> None:
        await self._seed_cache()
        _install_ha_helper_stubs(lambda hass: FakeSession({}))
        italian = types.SimpleNamespace(config=types.SimpleNamespace(language="it"))
        self.assertIs(program_labels.EMPTY, await program_labels.async_load(italian))

    async def test_an_unreadable_store_does_not_break_setup(self) -> None:
        FakeStore.load_error = RuntimeError("corrupt .storage")
        self._session()
        labels = await program_labels.async_load(self.hass)
        self.assertEqual("Drum Cleaning", labels.label("WM", "hqd_autoclean"))

    async def test_no_cache_and_no_network_is_still_the_empty_catalog(self) -> None:
        _install_ha_helper_stubs(lambda hass: FakeSession({}))
        self.assertIs(program_labels.EMPTY, await program_labels.async_load(self.hass))

    async def test_a_failed_save_keeps_the_freshly_downloaded_catalog(self) -> None:
        # Persisting is best-effort; DOWNLOADING is what the labels depend on. With the
        # save inside the fetch guard, an unwritable `.storage` sent a successful download
        # down the failure path and the user saw raw codes plus an "unavailable" warning.
        self._session()

        # `_patched` replaces a CLASS attribute, so the call binds the instance: without
        # `_self` this raised TypeError instead of the OSError the test means to drive.
        # The assertion held either way, which is exactly why it was worth fixing.
        async def _boom(_self, _data):
            raise OSError("read-only .storage")

        with _patched(FakeStore, "async_save", _boom):
            labels = await program_labels.async_load(self.hass)
        self.assertEqual("Drum Cleaning", labels.label("WM", "hqd_autoclean"))

    async def test_a_cache_hit_gets_the_short_budget(self) -> None:
        # With a usable cache the request is only a freshness check, and every second of
        # it is setup latency: no entity is created until async_load returns.
        await self._seed_cache()
        seen: list[float] = []
        real_timeout = asyncio.timeout

        def _record(budget):
            seen.append(budget)
            return real_timeout(budget)

        self._session()
        with _patched(asyncio, "timeout", _record):
            await program_labels.async_load(self.hass)
        # Outer budget first, then the app-config slice nested inside it: without that
        # inner bound a slow gateway could spend the whole budget and leave the catalog
        # download no time at all.
        self.assertEqual(
            [
                program_labels.REFRESH_TIMEOUT,
                min(program_labels.CONFIG_TIMEOUT, program_labels.REFRESH_TIMEOUT),
            ],
            seen,
        )

    async def test_a_slow_app_config_cannot_starve_the_download(self) -> None:
        # The failure the split budget prevents: app-config hanging past its own slice
        # must abort there, not after eating the time the ~7.6 MB download needs.
        # `_slow` is DELIBERATELY a working app-config that merely takes its time: a stub
        # that simply hung would make this pass with or without the inner bound, because
        # the chain would fail on the missing config anyway. Here the request eventually
        # SUCCEEDS, so the only thing that can produce EMPTY is the app-config slice
        # expiring first.
        self._session()
        real_app_config = translations.async_app_config

        async def _slow(session, language):
            await asyncio.sleep(0.2)
            return await real_app_config(session, language)

        original = program_labels.CONFIG_TIMEOUT
        program_labels.CONFIG_TIMEOUT = 0.01
        try:
            with _patched(translations, "async_app_config", _slow):
                labels = await program_labels.async_load(self.hass)
        finally:
            program_labels.CONFIG_TIMEOUT = original
        self.assertIs(program_labels.EMPTY, labels)


class ProgramSelectLabelTest(unittest.IsolatedAsyncioTestCase):
    def _select(self, values, app_type="WM", catalog=CATALOG):
        from custom_components.addhon.select import HonProgramSelect

        commands = {"startProgram": RecordingCommand({"program": Param(values=values)})}
        coordinator = _coordinator_with_catalog(
            _washer_with_type(commands, app_type), catalog
        )
        return HonProgramSelect(coordinator, "washer-1", FakeClient())

    async def test_options_show_catalog_labels(self) -> None:
        entity = self._select(["hqd_autoclean", "iot_wash_bed_linen"])
        self.assertEqual(["Drum Cleaning", "Bed Linen"], entity._attr_options)

    async def test_codes_sent_to_the_appliance_stay_raw(self) -> None:
        # The whole point: only the DISPLAY changes. The reverse map must still hand the
        # send path the untouched hOn code.
        entity = self._select(["hqd_autoclean"])
        self.assertEqual({"Drum Cleaning": "hqd_autoclean"}, entity._program_reverse)

    async def test_untranslatable_entries_keep_their_text(self) -> None:
        entity = self._select(["hqd_autoclean", "Ariel Fresh Clean", "hqd_unknown"])
        self.assertEqual(
            ["Drum Cleaning", "Ariel Fresh Clean", "hqd_unknown"], entity._attr_options
        )

    async def test_without_a_catalog_the_raw_codes_are_kept(self) -> None:
        # No network at setup must degrade to today's behaviour, never to a broken list.
        entity = self._select(["hqd_autoclean"], catalog={})
        self.assertEqual(["hqd_autoclean"], entity._attr_options)

    async def test_selecting_a_translated_label_buffers_the_raw_code(self) -> None:
        entity = self._select(["hqd_autoclean", "iot_wash_bed_linen"])
        entity.hass = None
        await entity.async_select_option("Bed Linen")
        self.assertEqual(
            {"washer-1": "iot_wash_bed_linen"}, entity.coordinator.pending_programs
        )


class ProgramNameSensorTest(unittest.IsolatedAsyncioTestCase):
    def _sensor(self, program: str, app_type: str = "WM", catalog=CATALOG):
        from custom_components.addhon import sensor as sensor_module

        data = _washer_with_type({}, app_type)
        data["washer-1"]["attributes"] = {"programName": program}
        coordinator = _coordinator_with_catalog(data, catalog)
        description = next(
            d
            for d in sensor_module.SENSORS["WM"]
            if d.key == sensor_module.PROGRAM_NAME_KEY
        )
        return sensor_module.HonProgramNameSensor(coordinator, "washer-1", description)

    async def test_translates_the_program_key(self) -> None:
        self.assertEqual("Drum Cleaning", self._sensor("hqd_autoclean").native_value)

    async def test_keeps_an_untranslatable_value(self) -> None:
        # "No Program" is the engine's own sentinel and has no catalog key.
        self.assertEqual("No Program", self._sensor("No Program").native_value)
        self.assertEqual("hqd_unknown", self._sensor("hqd_unknown").native_value)

    async def test_without_a_catalog_the_raw_key_is_kept(self) -> None:
        self.assertEqual(
            "hqd_autoclean", self._sensor("hqd_autoclean", catalog={}).native_value
        )

    async def test_setup_builds_the_translating_class_for_program_name(self) -> None:
        # The subclass exists only because a value_fn cannot see the appliance type or
        # the coordinator; pin that the platform actually wires it up.
        from custom_components.addhon import sensor as sensor_module
        from custom_components.addhon.const import DOMAIN

        data = _washer_with_type({})
        data["washer-1"]["attributes"] = {"programName": "hqd_smart"}
        coordinator = _coordinator_with_catalog(data)
        hass = types.SimpleNamespace(
            data={DOMAIN: {"entry-1": {"coordinator": coordinator}}}
        )
        entry = types.SimpleNamespace(entry_id="entry-1", options={})
        added: list = []

        await sensor_module.async_setup_entry(hass, entry, added.extend)

        # The platform also adds account-level sensors that carry no entity_description.
        program_sensors = [
            e
            for e in added
            if getattr(getattr(e, "entity_description", None), "key", None)
            == sensor_module.PROGRAM_NAME_KEY
        ]
        self.assertEqual(1, len(program_sensors))
        self.assertIsInstance(program_sensors[0], sensor_module.HonProgramNameSensor)
        self.assertEqual("Smart A.I.", program_sensors[0].native_value)



class DiscoveryLogPrivacyTest(unittest.TestCase):
    """The prPosition discovery line must not carry user-typed names.

    Category names normally come from the appliance schema, but `_add_favourites` files a
    favourite under the name the user typed for it -- and this line asks to be attached
    to a diagnostics report.
    """

    def _param(self):
        from custom_components.addhon.client.engine.parameter.program import (
            HonParameterProgram,
        )

        def category(prcode, favourite=False):
            parameters = {"prCode": types.SimpleNamespace(value=prcode)}
            if favourite:
                parameters["favourite"] = types.SimpleNamespace(value="1")
            return types.SimpleNamespace(parameters=parameters)

        command = types.SimpleNamespace(
            category="PROGRAMS.WM_WD.HQD_COTTONS",
            categories={
                "hqd_cottons": category("115"),
                "Anna's nightgowns": category("115", favourite=True),
            },
        )
        return HonParameterProgram("program", command, "custom")

    def test_the_log_counts_favourites_but_never_names_them(self) -> None:
        program = self._param()
        with self.assertLogs(
            "custom_components.addhon.client.engine.parameter.program", level="INFO"
        ) as captured:
            program.name_for_code(115, 3)
        message = captured.records[0].getMessage()
        self.assertNotIn("Anna", message)
        self.assertIn("hqd_cottons", message)
        # The counts stay whole: 2 candidates, one of which is the favourite.
        self.assertIn("alone: 2", message)


if __name__ == "__main__":
    unittest.main()
