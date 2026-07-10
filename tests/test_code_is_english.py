# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Guard: the integration code is English / ASCII.

Every `.py` under custom_components/addhon (excluding translations/) must contain
no non-ASCII characters, except a small allow-list of scientific UNIT symbols that
have no clean ASCII equivalent and are device data, not language. This keeps code,
comments and log messages English-only and stops non-English text (e.g. Italian
accented letters, or decorative box-drawing) from creeping back in. All
user-facing strings belong in translations/ instead.

Tests are intentionally NOT scanned: their fixtures simulate real device data
(unit symbols like "C, accented program names, etc.) which is legitimately
non-ASCII.
"""
from __future__ import annotations

import re
import tokenize
import unittest
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "addhon"

# Unambiguous Italian words (none collide with an English word) that must never
# appear in code/comments/logs. The ASCII check above misses Italian written
# without accents (e.g. "Alimentazione"); this denylist closes that gap. Keep it
# conservative: only add words with zero English collision to avoid false positives.
ITALIAN_WORDS = (
    "alimentazione", "impostazioni", "impostazione", "errore", "avviso",
    "attenzione", "sconosciuto", "sconosciuta", "disponibile", "lavaggio",
    "asciugatrice", "frigorifero", "congelatore", "lavastoviglie", "aspirapolvere",
    "scaldabagno", "spegnimento", "accensione", "programmazione", "caricamento",
    "annulla", "conferma", "riavvio",
)
_ITALIAN_RE = re.compile(r"\b(" + "|".join(ITALIAN_WORDS) + r")\b", re.IGNORECASE)

# Scientific unit symbols with no clean ASCII equivalent (device data, not
# language): MICRO SIGN, SUPERSCRIPT TWO/THREE, DEGREE SIGN. Italian accented
# letters are deliberately NOT here, so they remain caught.
ALLOWED_NON_ASCII = {"µ", "²", "³", "°"}


class CodeIsEnglishTest(unittest.TestCase):
    def test_production_code_is_ascii_only(self) -> None:
        offenders: list[str] = []
        repo_root = COMPONENT.parents[1]
        for path in sorted(COMPONENT.rglob("*.py")):
            if "translations" in path.parts:
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                bad = sorted(
                    {c for c in line if ord(c) > 127 and c not in ALLOWED_NON_ASCII}
                )
                if bad:
                    rel = path.relative_to(repo_root)
                    codes = [hex(ord(c)) for c in bad]
                    offenders.append(f"{rel}:{lineno}: {codes}  {line.strip()[:70]}")
        self.assertEqual(
            [],
            offenders,
            "Non-ASCII (non-English) characters found in integration code. Keep "
            "code/comments/logs English and move user-facing text to translations/:\n"
            + "\n".join(offenders),
        )

    def test_no_pyhon_references(self) -> None:
        # Cheap tripwire against RE-VENDORING the legacy pyhon library: "pyhon" must
        # not appear as an IMPORT or IDENTIFIER (module name, attribute, symbol) in the
        # shipped code. It is deliberately ALLOWED in comments/docstrings, where it
        # documents legitimate provenance (e.g. "the pyhOn 8h heuristic is gone"); the
        # real, structural proof of independence is the harness under
        # tests/independence/ (Gate A structural + Gate B provenance), not this grep.
        #
        # We tokenize so only NAME tokens (identifiers/imports) are scanned; COMMENT
        # and STRING tokens (docstrings, provenance notes) are exempt.
        rx = re.compile(r"pyhon", re.IGNORECASE)
        offenders: list[str] = []
        repo_root = COMPONENT.parents[1]
        for path in sorted(COMPONENT.rglob("*.py")):
            if "translations" in path.parts:
                continue
            with tokenize.open(path) as handle:
                try:
                    tokens = list(tokenize.generate_tokens(handle.readline))
                except (tokenize.TokenError, IndentationError, SyntaxError):
                    # A non-tokenizable file is a different problem; skip it here.
                    continue
            for tok in tokens:
                if tok.type == tokenize.NAME and rx.search(tok.string):
                    rel = path.relative_to(repo_root)
                    offenders.append(f"{rel}:{tok.start[0]}: identifier {tok.string!r}")
        self.assertEqual(
            [],
            offenders,
            "The legacy 'pyhon' library is referenced as an import/identifier in the "
            "shipped code (re-vendoring). The client is native; provenance belongs in "
            "comments only. See the independence harness under tests/independence/:\n"
            + "\n".join(offenders),
        )

    def test_no_known_italian_words(self) -> None:
        offenders: list[str] = []
        repo_root = COMPONENT.parents[1]
        for path in sorted(COMPONENT.rglob("*.py")):
            if "translations" in path.parts:
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if match := _ITALIAN_RE.search(line):
                    rel = path.relative_to(repo_root)
                    offenders.append(f"{rel}:{lineno}: '{match.group(0)}'  {line.strip()[:70]}")
        self.assertEqual(
            [],
            offenders,
            "Italian words found in integration code. Keep code/comments/logs "
            "English and move user-facing text to translations/:\n" + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
