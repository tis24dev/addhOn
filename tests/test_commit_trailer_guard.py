# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The guard that keeps AI-assistant authorship out of commit metadata.

`scripts/no-assistant-trailer.sh` is shared by the local `commit-msg` hook and
by the CI job, so a silent regression in it disarms BOTH halves at once with a
green suite. These tests are what makes that impossible.

Two failure directions matter and they are not symmetric:

  * a MISS lets the trailer through, and the cure is a history rewrite plus 90
    days of reflog copies nobody can reach;
  * a FALSE POSITIVE blocks an honest commit, and the most likely victim is a
    commit that quotes the rule. This repository already contains prose that
    does exactly that (`docs/superpowers/plans/2026-07-14-...md` states "No
    `Co-Authored-By: Claude` trailer"), and a substring match would flag the
    document defining the prohibition as a violation of it. Hence the matcher
    anchors at line start, and `test_prose_quoting_the_rule_is_not_a_trailer`
    pins that decision against a future "simplification".
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "scripts" / "no-assistant-trailer.sh"
HOOK = REPO / "scripts" / "hooks" / "commit-msg"


def _check(message: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(GUARD)],
        input=message,
        capture_output=True,
        text=True,
        check=False,
    )


class GuardRejectsTest(unittest.TestCase):
    """Messages that must NOT be committable."""

    def test_the_exact_trailer_this_repo_has_seen(self) -> None:
        # Verbatim from the five unreachable commits found in the reflog.
        result = _check(
            "fix: something\n\n"
            "Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\n"
        )
        self.assertEqual(1, result.returncode)
        self.assertIn("Co-Authored-By", result.stderr)

    def test_lowercase_spelling(self) -> None:
        self.assertEqual(
            1, _check("fix: x\n\nco-authored-by: claude <a@b.c>\n").returncode
        )

    def test_authored_by_without_the_co(self) -> None:
        self.assertEqual(
            1, _check("fix: x\n\nAuthored-by: Claude <a@b.c>\n").returncode
        )

    def test_the_anthropic_address_under_any_name(self) -> None:
        # The name can be anything; the address is the tell.
        self.assertEqual(
            1,
            _check("fix: x\n\nCo-Authored-By: Somebody <bot@anthropic.com>\n").returncode,
        )

    def test_the_generated_with_line(self) -> None:
        self.assertEqual(
            1,
            _check("fix: x\n\n\U0001F916 Generated with [Claude Code](https://x)\n").returncode,
        )

    def test_leading_whitespace_does_not_hide_it(self) -> None:
        self.assertEqual(
            1, _check("fix: x\n\n   Co-Authored-By: Claude <a@b.c>\n").returncode
        )

    def test_the_offending_line_is_reported_back(self) -> None:
        # A guard that says "rejected" without saying WHICH line sends the
        # committer hunting through a long message.
        result = _check(
            "feat: a long message\n\nline two\nline three\n"
            "Co-Authored-By: Claude <a@b.c>\n"
        )
        self.assertIn("Claude", result.stderr)
        self.assertIn("5:", result.stderr)  # line number, not just the text


class GuardAcceptsTest(unittest.TestCase):
    """Messages that MUST stay committable."""

    def test_an_ordinary_message(self) -> None:
        result = _check("feat(diagnostics): date the dump\n\nBody paragraph.\n")
        self.assertEqual(0, result.returncode)

    def test_a_human_co_author(self) -> None:
        # The trailer itself is legitimate; only assistant credit is not.
        self.assertEqual(
            0,
            _check("fix: x\n\nCo-authored-by: telard-pixel <telard@gmail.com>\n").returncode,
        )

    def test_the_github_actions_bot(self) -> None:
        self.assertEqual(
            0,
            _check(
                "Release v5.12.0\n\nCo-authored-by: github-actions[bot] "
                "<41898282+github-actions[bot]@users.noreply.github.com>\n"
            ).returncode,
        )

    def test_prose_quoting_the_rule_is_not_a_trailer(self) -> None:
        # The regression this repository is most likely to hit: a commit whose
        # body explains the prohibition. Anchoring at line start is the whole
        # defence -- a substring match fails here.
        self.assertEqual(
            0,
            _check(
                "docs: write down the commit conventions\n\n"
                "Conventional-commit messages. No `Co-Authored-By: Claude` trailer.\n"
            ).returncode,
        )

    def test_a_body_mentioning_the_assistant_in_passing(self) -> None:
        self.assertEqual(
            0,
            _check("chore: update CLAUDE.md with the anthropic model ids\n").returncode,
        )


class GuardWiringTest(unittest.TestCase):
    """The two consumers really point at this script."""

    def test_both_files_are_executable(self) -> None:
        for path in (GUARD, HOOK):
            self.assertTrue(path.exists(), f"{path} missing")
            self.assertTrue(path.stat().st_mode & 0o111, f"{path} not executable")

    def test_the_hook_delegates_to_the_shared_matcher(self) -> None:
        # If the hook ever grows its own copy of the pattern, the two halves of
        # the guard can drift and only one of them will be tested here.
        self.assertIn("no-assistant-trailer.sh", HOOK.read_text(encoding="utf-8"))

    def test_ci_runs_the_shared_matcher(self) -> None:
        workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("no-assistant-trailer.sh", workflow)
        # Full history, or `base..head` has no ends to walk.
        self.assertIn("fetch-depth: 0", workflow)

    def test_ci_passes_the_shas_through_env_not_interpolation(self) -> None:
        # Workflow-injection hygiene: a `${{ }}` expansion inside `run:` is
        # substituted into the shell source before it executes.
        workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn('git log --format=\'%B\' "$BASE..$HEAD"', workflow)


class GuardAgainstHistoryTest(unittest.TestCase):
    """Run the matcher over the real history it exists to police."""

    def _messages(self, ref: str) -> list[str]:
        result = subprocess.run(
            ["git", "log", "--format=%H", ref],
            cwd=REPO, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            self.skipTest(f"git unavailable or {ref} missing")
        out = []
        for sha in result.stdout.split():
            body = subprocess.run(
                ["git", "log", "-1", "--format=%B", sha],
                cwd=REPO, capture_output=True, text=True, check=False,
            )
            out.append(body.stdout)
        return out

    def test_the_shipped_history_is_clean(self) -> None:
        # Doubles as the answer to "is the published history already tainted".
        offenders = [m for m in self._messages("HEAD") if _check(m).returncode != 0]
        self.assertEqual([], offenders, f"{len(offenders)} commits carry the trailer")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
