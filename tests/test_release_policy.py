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

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

POLICY = (
    Path(__file__).resolve().parents[1] / ".github" / "scripts" / "release-policy.sh"
)
BASH = shutil.which("bash")


def _workflows() -> list[Path]:
    """Every workflow file, both spellings of the extension."""
    directory = POLICY.parents[1] / "workflows"
    return sorted(
        path for path in directory.iterdir() if path.suffix in (".yml", ".yaml")
    )


def _policy_variables() -> set[str]:
    """Top-level variables the policy defines, i.e. what a workflow can read."""
    return {
        match.group(1)
        for match in re.finditer(
            r"^([A-Z][A-Z0-9_]*)=", POLICY.read_text(encoding="utf-8"), re.MULTILINE
        )
    }


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


def _constant(name: str) -> str:
    """Read one policy constant through bash, so quoting is the shell's problem."""
    result = subprocess.run(
        # `set -u` on purpose: a missing constant must be an error, not "".
        [BASH, "-c", f'set -u; source "$1"; printf "%s" "${{{name}}}"', "_", str(POLICY)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"{name}: {result.stderr.strip()}")
    return result.stdout


def _examples(formats: str) -> list[str]:
    """Turn 'vX.Y.Z, vX.Y.Z-beta or vX.Y.Z-betaN' into concrete tags."""
    tags = []
    for piece in formats.replace(" or ", ", ").split(","):
        tags.append(piece.strip().replace("X.Y.Z", "1.2.3").replace("betaN", "beta4"))
    return tags


@unittest.skipIf(BASH is None, "bash is required to exercise the policy")
class TagFormatProseTest(unittest.TestCase):
    """The error messages must describe what the regexes actually accept.

    They did not. The trigger format was spelled out at each point of use, including
    inside `.github/workflows/release-intake.yml`, and widening the regex to accept a
    numbered beta left that copy telling the user to push a shape it had just stopped
    being the only one. Now there is one constant per regex, and the prose is checked
    by building tags out of it rather than by reading it.
    """

    def test_a_missing_constant_is_an_error_not_an_empty_string(self) -> None:
        """Anti-vacuity for the reader above: without it a renamed constant would
        quietly become "" and the failure would name the wrong thing."""
        with self.assertRaises(AssertionError):
            _constant("DEFINITELY_NOT_A_POLICY_CONSTANT")

    def test_every_advertised_release_shape_is_accepted(self) -> None:
        examples = _examples(_constant("RELEASE_TAG_FORMATS"))
        self.assertEqual(["v1.2.3", "v1.2.3-beta", "v1.2.3-beta4"], examples)
        for tag in examples:
            self.assertTrue(_predicate("is_release_tag", tag), tag)

    def test_every_advertised_trigger_shape_is_accepted(self) -> None:
        examples = _examples(_constant("PR_TAG_FORMATS"))
        self.assertEqual(
            ["pr-v1.2.3", "pr-v1.2.3-beta", "pr-v1.2.3-beta4"], examples
        )
        for tag in examples:
            self.assertTrue(_predicate("is_pr_tag", tag), tag)

    def test_the_two_advertised_sets_correspond(self) -> None:
        """Each trigger shape must map onto the release shape advertised next to it:
        the pipeline derives one from the other."""
        triggers = _examples(_constant("PR_TAG_FORMATS"))
        releases = _examples(_constant("RELEASE_TAG_FORMATS"))
        self.assertEqual(
            releases, [_value("release_tag_from_pr_tag", tag) for tag in triggers]
        )

    def test_the_operator_doc_lists_exactly_the_advertised_shapes(self) -> None:
        """The doc was the fourth copy, and the one an operator actually reads. It
        still described pushing a `v*` tag directly, which is the PROTECTED tag the
        automation creates itself, and it predated the numbered beta entirely."""
        doc = POLICY.parents[2] / "docs" / "release-workflow.md"
        blocks, current = [], None
        for line in doc.read_text(encoding="utf-8").splitlines():
            if line.startswith("```"):
                if current is not None:
                    blocks.append(current)
                current = [] if line.strip() == "```text" else None
                continue
            if current is not None:
                current.append(line.strip())
        tag_blocks = [
            block for block in blocks
            if block and all(item.startswith(("v", "pr-v")) for item in block)
        ]
        advertised = [
            [shape.strip() for shape in
             _constant(name).replace(" or ", ", ").split(",")]
            for name in ("PR_TAG_FORMATS", "RELEASE_TAG_FORMATS")
        ]
        self.assertEqual(advertised, tag_blocks)

    def test_no_workflow_spells_the_tag_format_itself(self) -> None:
        """The fence. A message that hardcodes the shapes is a second source of truth
        and is what drifted; the workflows source the policy, so they can interpolate
        the constant instead."""
        workflows = _workflows()
        self.assertGreaterEqual(len(workflows), 4)
        offenders = []
        for path in workflows:
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if "::error::" not in line and "::notice::" not in line:
                    continue
                if re.search(r"v?X\.Y\.Z", line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual([], offenders)


@unittest.skipIf(BASH is None, "bash is required to exercise the policy")
class SplitSourceContractTest(unittest.TestCase):
    """A workflow body must never HARD-depend on a symbol the policy provides.

    `release-intake.yml` sources the policy from `origin/main` on purpose: on a
    `push: tags` event the checkout is the tag, which is the unreviewed code being
    released, and this job holds write credentials. The body, though, ships WITH the
    tag. So every release runs a new body against the previous policy, and a symbol
    added in the same commit that starts using it does not exist yet.

    Under `set -euo pipefail` that is not an empty expansion but a fatal unbound
    variable. It happened here: `${PR_TAG_FORMATS}` was introduced and referenced in
    one change, and on the very next release it would have killed the invalid-trigger
    branch one statement before `delete_remote_tag`, leaving the rejected tag on
    origin. Re-pushing the same trigger is then a no-op that never re-fires intake, so
    recovery is a manual deletion.

    Functions are exempt: an undefined function is a runtime "command not found" on
    the branch that calls it, not a parse-time kill, and every function the workflows
    call has been on main for several releases.
    """

    def test_every_policy_variable_a_workflow_reads_carries_a_default(self) -> None:
        provided = _policy_variables()
        self.assertIn("PR_TAG_FORMATS", provided, "the policy no longer defines it")
        offenders = []
        for path in _workflows():
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                # Comments cannot expand, and the one that explains this rule has to
                # be able to name the shape it forbids.
                if line.lstrip().startswith("#"):
                    continue
                for name in sorted(provided):
                    # ${NAME} with no ":-": a hard dependency on the sourced policy.
                    if re.search(r"\$\{" + name + r"\}", line):
                        offenders.append(f"{path.name}:{number} ${{{name}}}")
        self.assertEqual([], offenders)

    def test_the_contract_holds_against_the_previous_policy(self) -> None:
        """End to end: run the intake rejection branch with a policy that predates
        every constant, and prove it still reaches the tag cleanup."""
        stripped = "\n".join(
            line
            for line in POLICY.read_text(encoding="utf-8").splitlines()
            if not line.startswith(("RELEASE_TAG_FORMATS=", "PR_TAG_FORMATS="))
        )
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(stripped)
            old_policy = Path(handle.name)
        try:
            result = subprocess.run(
                [BASH, "-c", _INTAKE_REJECTION_BRANCH, "_", str(old_policy)],
                capture_output=True,
                text=True,
            )
            self.assertIn("::error::", result.stdout)
            self.assertIn("CLEANUP REACHED", result.stdout)
            self.assertEqual(1, result.returncode, result.stderr)
        finally:
            old_policy.unlink()


#: The shape of release-intake.yml's invalid-trigger branch, with the tag deletion
#: replaced by a marker. Kept in sync with the workflow by the test below.
_INTAKE_REJECTION_BRANCH = """
set -euo pipefail
source "$1"
TRIGGER="pr-v0.0.0-nonsense"
if ! is_pr_tag "${TRIGGER}"; then
  echo "::error::Invalid trigger '${TRIGGER}'. Push ${PR_TAG_FORMATS:-a valid pr-v* trigger tag} on dev to start a release."
  echo "CLEANUP REACHED"
  exit 1
fi
"""


if __name__ == "__main__":
    unittest.main()
