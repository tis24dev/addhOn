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
        # The integration is fully native: the legacy library "pyhon" must not appear
        # anywhere in the code (not in imports, identifiers, comments or docstrings).
        # The migration is complete; its provenance lives in the gitignored
        # diagnostics/pyhon-provenance.md, not in the shipped code.
        rx = re.compile(r"pyhon", re.IGNORECASE)
        offenders: list[str] = []
        repo_root = COMPONENT.parents[1]
        for path in sorted(COMPONENT.rglob("*.py")):
            if "translations" in path.parts:
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if rx.search(line):
                    rel = path.relative_to(repo_root)
                    offenders.append(f"{rel}:{lineno}: {line.strip()[:80]}")
        self.assertEqual(
            [],
            offenders,
            "Reference to the legacy 'pyhon' library found in the code. The client is "
            "native; keep provenance in diagnostics/pyhon-provenance.md instead:\n"
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
