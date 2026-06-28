"""Tests for the Home Assistant UI translations layout and content.

Covers the i18n fix (commit "load UI translations from translations/ dir"):
- the strings must live in translations/ (plural) so HA actually loads them;
- the stale locations (root en/it.json and the singular translation/ dir) and
  the non-standard config.title key must not come back;
- every config-flow key the code uses (including the reauth flow) must exist in
  both en and it with identical structure.

Pure file/JSON checks: no Home Assistant import required.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "addhon"
TRANSLATIONS = COMPONENT / "translations"
LANGS = ("en", "it")


def _load(lang: str) -> dict:
    return json.loads((TRANSLATIONS / f"{lang}.json").read_text(encoding="utf-8"))


def _dotted_keys(node, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            keys.add(path)
            keys |= _dotted_keys(value, path)
    return keys


class TranslationsLayoutTest(unittest.TestCase):
    def test_translations_dir_is_plural_and_present(self) -> None:
        self.assertTrue((TRANSLATIONS / "en.json").is_file())
        self.assertTrue((TRANSLATIONS / "it.json").is_file())

    def test_stale_locations_are_gone(self) -> None:
        # HA ignored these, so they must not be reintroduced.
        self.assertFalse((COMPONENT / "en.json").exists())
        self.assertFalse((COMPONENT / "it.json").exists())
        self.assertFalse((COMPONENT / "translation").exists())

    def test_files_are_valid_utf8_json_without_bom(self) -> None:
        for lang in LANGS:
            raw = (TRANSLATIONS / f"{lang}.json").read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{lang}.json has a BOM")
            json.loads(raw.decode("utf-8"))


class TranslationsContentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.data = {lang: _load(lang) for lang in LANGS}

    def test_no_legacy_config_title_key(self) -> None:
        for lang in LANGS:
            self.assertNotIn(
                "title",
                self.data[lang]["config"],
                f"{lang}: non-standard config.title should stay removed",
            )

    def test_user_step_exposes_credentials(self) -> None:
        for lang in LANGS:
            data = self.data[lang]["config"]["step"]["user"]["data"]
            self.assertIn("email", data)
            self.assertIn("password", data)

    def test_error_keys_present(self) -> None:
        for lang in LANGS:
            errors = self.data[lang]["config"]["error"]
            for key in ("cannot_connect", "invalid_auth", "unknown"):
                self.assertIn(key, errors, f"{lang}: missing error.{key}")

    def test_per_code_error_strings_carry_error_code_placeholder(self) -> None:
        # #30: the precise ADDHON-NNN codes are injected via {error_code}. Every
        # config.error string must carry the placeholder -- including the two
        # generic buckets (cannot_connect/invalid_auth): a ui=False code routed
        # there by config_flow._error_base_and_code still yields a non-empty
        # code.label (e.g. ADDHON-150/210/320), so the buckets must surface it too
        # (greptile P2: the label was computed and passed but never shown).
        for lang in LANGS:
            errors = self.data[lang]["config"]["error"]
            for key, text in errors.items():
                self.assertIn(
                    "{error_code}", text, f"{lang}: error.{key} must carry {{error_code}}"
                )

    def test_abort_keys_present(self) -> None:
        for lang in LANGS:
            abort = self.data[lang]["config"]["abort"]
            for key in ("already_configured", "reauth_successful", "reauth_account_mismatch"):
                self.assertIn(key, abort, f"{lang}: missing abort.{key}")

    def test_reauth_confirm_step_uses_email_placeholder(self) -> None:
        for lang in LANGS:
            step = self.data[lang]["config"]["step"]["reauth_confirm"]
            self.assertIn("password", step["data"])
            self.assertIn(
                "{email}",
                step["description"],
                f"{lang}: reauth_confirm.description must reference the {{email}} placeholder",
            )

    def test_en_it_have_identical_structure(self) -> None:
        self.assertEqual(_dotted_keys(self.data["en"]), _dotted_keys(self.data["it"]))

    def test_anti_crease_time_distinct_from_anticrease(self) -> None:
        # PR #38 (#7): a WD merges the WM+TD option catalogs, so both the anticrease
        # toggle and the antiCreaseTime control can appear; their labels must differ or
        # the UI shows two indistinguishable "Anti-crease" switches.
        for lang in LANGS:
            switches = self.data[lang]["entity"]["switch"]
            self.assertNotEqual(
                switches["anticrease"]["name"],
                switches["anti_crease_time"]["name"],
                f"{lang}: anti_crease_time must have a label distinct from anticrease",
            )

    def test_no_dead_pyhon_references(self) -> None:
        """#17 regression guard: the strangler fully removed pyhOn (native client in
        hon_client.py), so no user-facing translation string may mention it again.
        A case-insensitive scan of the raw files catches any re-introduction in any
        key (service descriptions, labels, etc.), not only the ones #17 touched."""
        for lang in LANGS:
            offenders = [
                line.strip()
                for line in (TRANSLATIONS / f"{lang}.json")
                .read_text(encoding="utf-8")
                .splitlines()
                if "pyhon" in line.lower()
            ]
            self.assertEqual(
                [],
                offenders,
                f"{lang}.json must not reintroduce a dead pyhOn reference (#17): {offenders}",
            )


class TranslationsMatchConfigFlowTest(unittest.TestCase):
    """Strings must cover exactly the keys config_flow.py references."""

    def setUp(self) -> None:
        self.source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
        self.en = _load("en")["config"]

    def test_error_keys_set_in_code_have_strings(self) -> None:
        for key in ("cannot_connect", "invalid_auth", "unknown"):
            self.assertIn(f'"{key}"', self.source, f"code no longer sets error {key}?")
            self.assertIn(key, self.en["error"])

    def test_explicit_abort_reason_has_string(self) -> None:
        # reauth_successful is HA's default reason; reauth_account_mismatch is
        # the only abort reason this code passes explicitly.
        self.assertIn('reason="reauth_account_mismatch"', self.source)
        self.assertIn("reauth_account_mismatch", self.en["abort"])


if __name__ == "__main__":
    unittest.main()
