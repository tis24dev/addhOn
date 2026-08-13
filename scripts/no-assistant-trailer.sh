#!/bin/sh
# Copyright (C) 2026 tis24dev
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Reject AI-assistant authorship trailers in commit messages.
#
# Reads one or more commit messages on stdin and exits non-zero if any of them
# carries a trailer crediting an assistant. Shared by the local `commit-msg`
# hook (scripts/hooks/commit-msg) and by CI (.github/workflows/ci.yml) so the
# two can never disagree about what is forbidden.
#
# WHY ANCHORED AT LINE START: the pattern matches only a line that BEGINS with
# the trailer key, never the words appearing inside prose. This repository's own
# docs quote the rule verbatim ("No `Co-Authored-By: Claude` trailer" in
# docs/superpowers/plans/), and a substring match would flag the document that
# states the prohibition as a violation of it. A trailer is a line; match a line.
#
# The list is deliberately short and literal rather than a clever catch-all: a
# guard that is easy to read is a guard a contributor can trust. Add a case here
# when a new tool starts signing commits.

set -eu

messages=$(cat)

# 1. `Co-Authored-By:` naming an assistant or its noreply address.
# 2. The `Generated with [Claude Code]` line some tools append to PR bodies and
#    commit messages.
# 3. A bare `Author:`/`Co-Authored-By:` pointing at anthropic.com.
if printf '%s\n' "$messages" | grep -qiE \
    '^[[:space:]]*(co-)?authored-by:[[:space:]]*.*(claude|anthropic)|^[[:space:]]*(co-)?authored-by:.*@anthropic\.com|^[[:space:]]*.?.?[[:space:]]*Generated with \[Claude Code\]'
then
    cat >&2 <<'EOF'
Commit rejected: AI-assistant authorship trailer.

This repository does not credit assistants in commit metadata. Remove the
offending line and commit again. The offending lines were:

EOF
    printf '%s\n' "$messages" | grep -inE \
        '^[[:space:]]*(co-)?authored-by:[[:space:]]*.*(claude|anthropic)|^[[:space:]]*(co-)?authored-by:.*@anthropic\.com|^[[:space:]]*.?.?[[:space:]]*Generated with \[Claude Code\]' >&2
    exit 1
fi

exit 0
