# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

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
import re
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
            step = self.data[lang]["config"]["step"]["user"]
            data = step["data"]
            self.assertIn("email", data)
            self.assertIn("password", data)
            self.assertIn("auth_diagnostics", data)
            self.assertIn("auth_diagnostics", step["data_description"])

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
            self.assertIn("auth_diagnostics", step["data"])
            self.assertIn("auth_diagnostics", step["data_description"])
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


class OptionsScreenTranslationTest(unittest.TestCase):
    """Every option the Configure screen RENDERS must be labelled and described.

    The entity-key parity tests do not reach here: `options.step.init.data` is a
    config-flow schema, not an entity translation_key, so an unlabelled toggle would
    render as a raw key like `enable_experimental` with no test complaining."""

    OPTION_KEYS = ("enable_debug", "enable_mqtt_debug", "enable_experimental")

    def setUp(self) -> None:
        self.data = {lang: _load(lang) for lang in LANGS}
        self.source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")

    def test_every_rendered_option_is_labelled_and_described(self) -> None:
        for lang in LANGS:
            step = self.data[lang]["options"]["step"]["init"]
            self.assertEqual(
                set(self.OPTION_KEYS), set(step["data"]), f"{lang}: option labels"
            )
            self.assertEqual(
                set(self.OPTION_KEYS),
                set(step["data_description"]),
                f"{lang}: option descriptions",
            )

    def test_the_option_list_matches_the_flow_schema(self) -> None:
        # A key added to the schema without a label, or a label for a key the screen
        # no longer renders, both fail here.
        rendered = {
            key
            for key in self.OPTION_KEYS
            if f"CONF_{key.upper()}," in self.source or f"CONF_{key.upper()}\n" in self.source
        }
        self.assertEqual(set(self.OPTION_KEYS), rendered)

    def test_the_experimental_description_warns_about_the_evidence(self) -> None:
        """The toggle creates entities whose meaning is inferred from a single
        observation. The description must say so, or a user reads a wrong value as
        fact."""
        expected = {"en": ("incomplete evidence",), "it": ("indizi incompleti",)}
        for lang in LANGS:
            text = self.data[lang]["options"]["step"]["init"]["data_description"][
                "enable_experimental"
            ]
            for fragment in expected[lang]:
                self.assertIn(fragment, text, f"{lang}: {fragment}")

    def test_the_options_title_is_not_debug_only(self) -> None:
        # The screen carries a non-debug toggle now; a "Debug options" title would
        # mislabel it.
        for lang in LANGS:
            title = self.data[lang]["options"]["step"]["init"]["title"]
            self.assertNotIn("ebug", title, f"{lang}: {title}")


class AirPurifierTranslationTest(unittest.TestCase):
    """The exact air purifier key set, per platform, in BOTH languages.

    The generic parity tests compare code against JSON and en against it; this pins
    the LIST itself, so silently dropping an AP entity's label (or adding one for an
    entity that does not exist) fails here even if code and JSON agree with each
    other."""

    EXPECTED = {
        "sensor": {
            "temp_indoor", "humidity_indoor", "pm25", "pm10", "voc", "air_quality",
            "fan_speed", "filter_life", "filter_cleaning", "total_work_time",
            "errors", "co", "pollen_level", "air_quality_label",
        },
        "binary_sensor": {"eco_active", "problem", "co_alarm"},
        "switch": {"child_lock", "touch_tone"},
        "select": {"aroma", "panel_light"},
        "number": {"aroma_time_on", "aroma_time_off"},
        "fan": {"purifier"},
    }

    def setUp(self) -> None:
        self.data = {lang: _load(lang) for lang in LANGS}

    def test_every_air_purifier_key_is_named_in_both_languages(self) -> None:
        for lang in LANGS:
            entity = self.data[lang]["entity"]
            for platform, keys in self.EXPECTED.items():
                present = set(entity.get(platform, {}))
                missing = keys - present
                self.assertEqual(set(), missing, f"{lang} entity.{platform}: {missing}")
                for key in keys:
                    self.assertTrue(
                        entity[platform][key].get("name"),
                        f"{lang} entity.{platform}.{key}.name is empty",
                    )

    def test_the_purifier_platforms_exist_at_all(self) -> None:
        # A whole missing platform block would leave the generic collector nothing
        # to compare and pass vacuously. `light` is deliberately absent: the panel
        # has three steps and no brightness axis, so it is a select.
        for lang in LANGS:
            entity = self.data[lang]["entity"]
            for platform in ("fan", "select", "switch"):
                self.assertIn(platform, entity, f"{lang}: {platform}")
            self.assertNotIn("light", entity, lang)

    def test_the_aroma_states_are_labelled(self) -> None:
        for lang in LANGS:
            states = self.data[lang]["entity"]["select"]["aroma"]["state"]
            self.assertEqual(
                {"off", "soft", "mid", "h_biotics", "custom"}, set(states)
            )

    def test_the_confirmed_air_quality_label_is_translated(self) -> None:
        for lang in LANGS:
            states = self.data[lang]["entity"]["sensor"]["air_quality_label"]["state"]
            self.assertEqual({"good"}, set(states))

    def test_the_co_entity_denies_being_a_safety_detector(self) -> None:
        """The name is the ONLY place a user sees this. HA gives a binary sensor no
        description field, so the disclaimer has to live in the label."""
        fragments = {"en": "not a certified", "it": "un rilevatore certificato"}
        for lang in LANGS:
            name = self.data[lang]["entity"]["binary_sensor"]["co_alarm"]["name"]
            self.assertIn(fragments[lang], name, f"{lang}: {name}")

    def test_experimental_entities_say_so_in_their_name(self) -> None:
        """They may be wrong or disappear; the label is what tells the user.

        THE INDUCTION HOB IS A DELIBERATE EXCEPTION and is absent from the map
        below. Its experimental families are per-zone and generated: 26 entities
        (`plate_temp_zone1..6`, `program_code_zone1..6`, `program_phase_zone1..6`,
        `combi_mode_zone1..6`, `timer_hh`, `timer_mm`), so carrying the marker in
        every name would fill a dashboard with the same word 26 times and bury the
        four zone labels that tell them apart. The user who sees them has switched
        the option on in the Options flow, whose own description warns what the
        option creates, and none of them is switched on by default -- which is not
        true of the four entities listed here, whose families ship enabled and
        where the name is the only warning a user ever gets.

        What replaces the marker for the hob is HONESTY IN THE DATA rather than in
        the label: those entities carry no `device_class`, no unit and no
        `state_class`, so they claim nothing about what they measure.
        `plate_temp_zone{N}` is the one that had to be walked back -- it declared
        TEMPERATURE and °C on an attribute that has not moved since 2022 on a model
        declaring `probe = "0"` -- and `test_tier2_sensors` pins the raw shape.
        """
        marker = {"en": "experimental", "it": "sperimentale"}
        experimental = {
            "sensor": ("air_quality_label",),
            "binary_sensor": ("co_alarm",),
            "number": ("aroma_time_on", "aroma_time_off"),
        }
        for lang in LANGS:
            for platform, keys in experimental.items():
                for key in keys:
                    name = self.data[lang]["entity"][platform][key]["name"].lower()
                    self.assertIn(marker[lang], name, f"{lang} {platform}.{key}: {name}")

    def test_standard_air_purifier_entities_do_not_claim_to_be_experimental(
        self,
    ) -> None:
        marker = {"en": "experimental", "it": "sperimentale"}
        standard = {
            "sensor": self.EXPECTED["sensor"] - {"air_quality_label"},
            "binary_sensor": {"eco_active", "problem"},
            "switch": self.EXPECTED["switch"],
            "select": self.EXPECTED["select"],
            "fan": self.EXPECTED["fan"],
        }
        for lang in LANGS:
            for platform, keys in standard.items():
                for key in keys:
                    name = self.data[lang]["entity"][platform][key]["name"].lower()
                    self.assertNotIn(marker[lang], name, f"{lang} {platform}.{key}")

    def test_no_air_purifier_label_leaks_implementation_detail(self) -> None:
        """Entity names must not mention parameters, schemas, the decompiled app or
        debug mechanics: they are what a normal user reads in the dashboard."""
        forbidden = (
            "machmode", "aromastatus", "lightstatus", "onoffstatus", "colevel",
            "schema", "apk", "decomp", "mqtt", "debug", "dispatcher",
        )
        for lang in LANGS:
            entity = self.data[lang]["entity"]
            for platform, keys in self.EXPECTED.items():
                for key in keys:
                    blob = json.dumps(entity[platform][key], ensure_ascii=False).lower()
                    for token in forbidden:
                        self.assertNotIn(
                            token, blob, f"{lang} entity.{platform}.{key}: {token}"
                        )

    # Words Italian title case leaves lowercase (articles, prepositions, elisions).
    _IT_MINOR = frozenset({"di", "del", "della", "dell", "delle", "dei", "al", "in"})

    def _it_style(self, name: str) -> str:
        # Ignore a trailing parenthetical qualifier: "(sperimentale)" is a status
        # marker, not part of the entity's name.
        head = name.split("(")[0]
        words = [
            word
            for word in re.findall(r"[^\W\d_]+", head, re.UNICODE)
            if len(word) > 2 and word.lower() not in self._IT_MINOR
        ]
        if len(words) < 2:
            return "single"
        capitalized = sum(1 for word in words[1:] if word[0].isupper())
        if capitalized == len(words) - 1:
            return "title"
        return "sentence" if capitalized == 0 else "mixed"

    def test_the_italian_air_purifier_labels_share_one_capitalization_style(
        self,
    ) -> None:
        """The purifier's entities sit next to each other on one dashboard, so a
        mix of "Durata Filtro" and "durata filtro" reads as a bug. The rest of the
        file is not touched by this: the AP block only has to be internally
        consistent."""
        styles: dict[str, list[str]] = {}
        entity = self.data["it"]["entity"]
        for platform, keys in self.EXPECTED.items():
            for key in keys:
                name = entity[platform][key]["name"]
                style = self._it_style(name)
                if style == "single":
                    continue
                styles.setdefault(style, []).append(f"{platform}.{key}: {name}")
        self.assertEqual(
            ["title"], sorted(styles), f"mixed capitalization styles: {styles}"
        )

    def test_the_english_air_purifier_labels_start_capitalized(self) -> None:
        entity = self.data["en"]["entity"]
        for platform, keys in self.EXPECTED.items():
            for key in keys:
                name = entity[platform][key]["name"]
                self.assertTrue(name[0].isupper(), f"en {platform}.{key}: {name}")

    def test_the_two_filter_labels_are_symmetric(self) -> None:
        """Both report a REMAINING percentage. An asymmetric pair ("Filter life" vs
        "Pre-filter cleaning") reads as one gauge and one alarm.

        Overlap is the right rule HERE and only here: these two read different
        attributes and must stay distinguishable, so they have to share a register
        without naming the same thing. Entities that read the SAME attribute are held
        to equality instead, by SharedAttributeNamingTest in
        test_air_purifier_entities.py, because overlap passed the truncated-referent
        bug that test was written for.
        """
        for lang in LANGS:
            sensors = self.data[lang]["entity"]["sensor"]
            main = sensors["filter_life"]["name"]
            pre = sensors["filter_cleaning"]["name"]
            self.assertNotEqual(main, pre)
            shared = set(main.lower().split()) & set(pre.lower().split())
            self.assertTrue(shared, f"{lang}: {main!r} / {pre!r} share no wording")

    def test_the_aroma_timing_exception_is_translated(self) -> None:
        for lang in LANGS:
            message = self.data[lang]["exceptions"]["aroma_custom_not_active"][
                "message"
            ]
            self.assertTrue(message)
            self.assertNotIn("{", message)

    def test_the_stopped_purifier_exception_is_translated(self) -> None:
        for lang in LANGS:
            message = self.data[lang]["exceptions"]["purifier_not_running"]["message"]
            self.assertTrue(message)
            self.assertNotIn("{", message)


class ExceptionKeyParityTest(unittest.TestCase):
    """Every localized error the code can raise must exist in BOTH languages.

    Nothing checked this before: a `translation_key` with no JSON entry surfaces to
    the user as the raw key, and only at the moment the error fires.

    Derived by AST rather than by pattern, and over the WHOLE component tree. A
    textual scan gets this wrong in both directions: keyword order is free, so
    `translation_placeholders={"error": str(err)}` sitting before `translation_key`
    hides the raise from any expression that cannot cross a bracket, and a scan
    limited to the top level would call a key that moved under client/ unused.
    """

    @staticmethod
    def _raised_keys() -> dict[str, set[str]]:
        """{translation_key: {file, ...}} for every raise of a localized error."""
        import ast

        keys: dict[str, set[str]] = {}
        for path in sorted(COMPONENT.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Raise) or not isinstance(
                    node.exc, ast.Call
                ):
                    continue
                for keyword in node.exc.keywords:
                    if keyword.arg != "translation_key":
                        continue
                    if isinstance(keyword.value, ast.Constant) and isinstance(
                        keyword.value.value, str
                    ):
                        keys.setdefault(keyword.value.value, set()).add(
                            str(path.relative_to(COMPONENT))
                        )
                    else:
                        # A computed key cannot be verified against the JSON, so it
                        # must not exist: it would reach the user unresolved.
                        raise AssertionError(
                            f"{path}:{node.lineno}: non-literal translation_key"
                        )
        return keys

    def test_every_raised_key_exists_in_both_languages(self) -> None:
        keys = self._raised_keys()
        # Anti-vacuity, by SHAPE rather than by count: one raise with placeholders
        # after the key, one with placeholders before it would be equally covered,
        # and the key this test was added for.
        for expected in (
            "command_error",
            "appliance_or_client_unavailable",
            "purifier_not_running",
        ):
            self.assertIn(expected, keys)
        for lang in LANGS:
            exceptions = _load(lang).get("exceptions", {})
            for key, sources in sorted(keys.items()):
                self.assertIn(key, exceptions, f"{lang}: raised in {sorted(sources)}")
                self.assertTrue(exceptions[key].get("message"), f"{lang}: {key}")

    def test_no_language_carries_an_unused_exception(self) -> None:
        """The mirror check: a key no code path raises is dead weight that reads as
        coverage."""
        raised = set(self._raised_keys())
        for lang in LANGS:
            for key in _load(lang).get("exceptions", {}):
                self.assertIn(key, raised, f"{lang}: {key} is never raised")

    def test_the_two_languages_declare_the_same_exceptions(self) -> None:
        en, it = (set(_load(lang).get("exceptions", {})) for lang in LANGS)
        self.assertEqual(en, it)


if __name__ == "__main__":
    unittest.main()
