#!/usr/bin/env bash

RELEASE_TAG_REGEX='^v[0-9]+\.[0-9]+\.[0-9]+(-beta)?$'

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

version_from_tag() {
  local tag="${1:-}"

  validate_release_tag "${tag}"
  printf '%s\n' "${tag#v}"
}

manifest_version_at_ref() {
  local ref="${1:-}"

  git show "${ref}:custom_components/haier_hon/manifest.json" \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])'
}

set_manifest_version() {
  local version="${1:-}"

  python3 - "${version}" <<'PY'
import json
import sys
from pathlib import Path

version = sys.argv[1]
path = Path("custom_components/haier_hon/manifest.json")
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

  git push origin ":refs/tags/${tag}" || true
}
