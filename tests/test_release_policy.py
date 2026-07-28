# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the release tag policy shared by the three release workflows.

`.github/scripts/release-policy.sh` decides which tags may start a release, which
version string reaches `manifest.json`, and whether the published GitHub release is
marked as a prerelease. The workflows source it from `origin/main`, so a regression
here is only visible once it is already the trusted policy.

The one that matters most is `is_beta_tag`: it drives `--prerelease`. It used to be
a `*-beta` suffix GLOB, which reads a numbered beta (`v1.2.3-beta1`) as a stable
release and publishes it as the latest one. The functions are exercised through a
real bash, not re-implemented in Python, so what is asserted is the code the
workflows actually run.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

POLICY = (
    Path(__file__).resolve().parents[1] / ".github" / "scripts" / "release-policy.sh"
)
BASH = shutil.which("bash")


def _predicate(function: str, tag: str) -> bool:
    """Run one policy predicate in bash and return its exit status as a bool."""
    result = subprocess.run(
        [BASH, "-c", f'source "$1"; {function} "$2"', "_", str(POLICY), tag],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _value(function: str, tag: str) -> str:
    result = subprocess.run(
        [BASH, "-c", f'source "$1"; {function} "$2"', "_", str(POLICY), tag],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"{function}({tag!r}) failed: {result.stderr.strip()}")
    return result.stdout.strip()


@unittest.skipIf(BASH is None, "bash is required to exercise the policy")
class ReleasePolicyTest(unittest.TestCase):
    def test_the_policy_file_is_valid_bash(self) -> None:
        result = subprocess.run(
            [BASH, "-n", str(POLICY)], capture_output=True, text=True
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_release_tags_accepted(self) -> None:
        for tag in ("v5.9.3", "v5.10.0-beta", "v5.10.0-beta1", "v5.10.0-beta12"):
            self.assertTrue(_predicate("is_release_tag", tag), tag)

    def test_release_tags_refused(self) -> None:
        for tag in (
            "5.10.0",            # no v prefix
            "v5.10",             # not three components
            "v5.10.0-rc1",       # only beta prereleases exist
            "v5.10.0-beta-1",    # separator, not a sequence number
            "v5.10.0-betaX",
            "v5.10.0-beta1 ",
        ):
            self.assertFalse(_predicate("is_release_tag", tag), tag)

    def test_trigger_tags_accepted(self) -> None:
        for tag in ("pr-v5.9.3", "pr-v5.10.0-beta", "pr-v5.10.0-beta1"):
            self.assertTrue(_predicate("is_pr_tag", tag), tag)

    def test_trigger_tags_refused(self) -> None:
        # A bare v* tag must NOT be accepted as a trigger: it is the protected,
        # immutable one that post-merge-release creates on the squash commit.
        for tag in ("v5.10.0-beta1", "pr-5.10.0", "pr-v5.10.0-beta1x"):
            self.assertFalse(_predicate("is_pr_tag", tag), tag)

    def test_a_numbered_beta_is_still_a_prerelease(self) -> None:
        """The regression this test exists for: with a `*-beta` suffix glob these
        publish as the latest STABLE release."""
        for tag in ("v5.10.0-beta", "v5.10.0-beta1", "v5.10.0-beta12"):
            self.assertTrue(_predicate("is_beta_tag", tag), tag)

    def test_a_stable_tag_is_not_a_prerelease(self) -> None:
        for tag in ("v5.9.3", "v5.10.0"):
            self.assertFalse(_predicate("is_beta_tag", tag), tag)

    def test_every_accepted_release_tag_is_classified(self) -> None:
        """Beta or stable, never neither: an unclassified tag would publish with the
        wrong prerelease flag rather than failing."""
        for tag in ("v5.9.3", "v5.10.0-beta", "v5.10.0-beta1"):
            self.assertTrue(_predicate("is_release_tag", tag), tag)
            beta = _predicate("is_beta_tag", tag)
            self.assertEqual(tag.split("-")[-1].startswith("beta"), beta, tag)

    def test_the_trigger_maps_to_its_release_tag(self) -> None:
        self.assertEqual("v5.9.3", _value("release_tag_from_pr_tag", "pr-v5.9.3"))
        self.assertEqual(
            "v5.10.0-beta", _value("release_tag_from_pr_tag", "pr-v5.10.0-beta")
        )
        self.assertEqual(
            "v5.10.0-beta1", _value("release_tag_from_pr_tag", "pr-v5.10.0-beta1")
        )

    def test_the_manifest_version_drops_only_the_v(self) -> None:
        self.assertEqual("5.9.3", _value("version_from_tag", "v5.9.3"))
        self.assertEqual("5.10.0-beta", _value("version_from_tag", "v5.10.0-beta"))
        self.assertEqual("5.10.0-beta1", _value("version_from_tag", "v5.10.0-beta1"))

    def test_a_refused_tag_never_yields_a_version(self) -> None:
        for function, tag in (
            ("version_from_tag", "v5.10.0-rc1"),
            ("release_tag_from_pr_tag", "pr-v5.10.0-rc1"),
        ):
            with self.assertRaises(AssertionError):
                _value(function, tag)


if __name__ == "__main__":
    unittest.main()
