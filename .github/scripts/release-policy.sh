#!/usr/bin/env bash

RELEASE_TAG_REGEX='^v[0-9]+\.[0-9]+\.[0-9]+(-beta)?$'
# Unprotected trigger tag that starts a release. It deliberately does NOT match
# the protected v*.*.* ruleset, so it can be created/deleted freely; the real
# vX.Y.Z tag is created once on the squash commit by post-merge-release.
PR_TAG_REGEX='^pr-v[0-9]+\.[0-9]+\.[0-9]+(-beta)?$'

die() {
  echo "::error::$*" >&2
  exit 1
}

notice() {
  echo "::notice::$*"
}

is_release_tag() {
  [[ "${1:-}" =~ ${RELEASE_TAG_REGEX} ]]
}

validate_release_tag() {
  local tag="${1:-}"

  if ! is_release_tag "${tag}"; then
    die "Invalid release tag '${tag}'. Allowed formats are vX.Y.Z and vX.Y.Z-beta."
  fi
}

is_beta_tag() {
  [[ "${1:-}" == *-beta ]]
}

is_pr_tag() {
  [[ "${1:-}" =~ ${PR_TAG_REGEX} ]]
}

# pr-vX.Y.Z -> vX.Y.Z (the release tag that will be CREATED at merge).
release_tag_from_pr_tag() {
  local pr_tag="${1:-}"

  if ! is_pr_tag "${pr_tag}"; then
    die "Invalid trigger tag '${pr_tag}'. Expected pr-vX.Y.Z or pr-vX.Y.Z-beta."
  fi
  printf '%s\n' "${pr_tag#pr-}"
}

version_from_tag() {
  local tag="${1:-}"

  validate_release_tag "${tag}"
  printf '%s\n' "${tag#v}"
}

manifest_version_at_ref() {
  local ref="${1:-}"

  git show "${ref}:custom_components/addhon/manifest.json" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])'
}

set_manifest_version() {
  local version="${1:-}"

  python3 - "${version}" <<'PY'
import json
import sys
from pathlib import Path

version = sys.argv[1]
path = Path("custom_components/addhon/manifest.json")
data = json.loads(path.read_text())
data["version"] = version
path.write_text(json.dumps(data, indent=2) + "\n")
PY
}

assert_manifest_matches_tag() {
  local ref="${1:-}"
  local tag="${2:-}"
  local expected
  local actual

  expected="$(version_from_tag "${tag}")"
  actual="$(manifest_version_at_ref "${ref}")"

  if [[ "${actual}" != "${expected}" ]]; then
    die "manifest.json version '${actual}' does not match tag '${tag}' (expected '${expected}')."
  fi
}

extract_pr_marker() {
  local marker="${1:-}"

  python3 - "${marker}" <<'PY'
import os
import re
import sys

marker = sys.argv[1]
body = os.environ.get("PR_BODY", "")
pattern = rf"^<!-- {re.escape(marker)}: ([^<\n]+) -->$"
match = re.search(pattern, body, re.MULTILINE)
if not match:
    sys.exit(1)
print(match.group(1).strip())
PY
}

delete_remote_tag() {
  local tag="${1:-}"

  # Best-effort cleanup of the unprotected trigger tag. Stays non-fatal, but a
  # transient failure is surfaced as a warning: if the pr-vX.Y.Z tag is left
  # behind, re-pushing the same trigger is a no-op (ref unchanged) and won't
  # re-fire intake, so the leftover needs manual deletion before re-releasing.
  git push origin ":refs/tags/${tag}" || \
    echo "::warning::Failed to delete tag ${tag} (non-fatal); it may need manual cleanup before this version can be re-released."
}

# Existence probes that DISTINGUISH absent from error. A transient auth/network
# failure must never be silently read as "does not exist" (which would defeat the
# immutability/preflight gates). Echo: present|absent|error.

remote_tag_state() {
  local tag="${1:-}"
  local rc=0
  # git ls-remote --exit-code: 0 = ref found, 2 = no matching ref, other = failure.
  git ls-remote --exit-code --tags origin "refs/tags/${tag}" >/dev/null 2>&1 || rc=$?
  case "${rc}" in
    0) echo "present" ;;
    2) echo "absent" ;;
    *) echo "error" ;;
  esac
}

remote_release_state() {
  local tag="${1:-}"
  local out
  local rc=0
  out="$(gh api "repos/${GITHUB_REPOSITORY}/releases/tags/${tag}" 2>&1)" || rc=$?
  if [[ "${rc}" -eq 0 ]]; then
    echo "present"
  elif printf '%s' "${out}" | grep -qi 'HTTP 404\|Not Found'; then
    echo "absent"
  else
    echo "error"
  fi
}

# Hard gates: die on "present" AND on "error" (fail closed). Used where the only
# acceptable state to proceed is a confirmed "absent".
assert_release_tag_absent() {
  local tag="${1:-}"
  case "$(remote_tag_state "${tag}")" in
    present) die "Tag ${tag} already exists and is immutable." ;;
    error)   die "Could not determine whether tag ${tag} exists (git ls-remote failed); aborting." ;;
  esac
}

assert_release_absent() {
  local tag="${1:-}"
  case "$(remote_release_state "${tag}")" in
    present) die "Release ${tag} already exists." ;;
    error)   die "Could not determine whether release ${tag} exists (gh api failed); aborting." ;;
  esac
}
