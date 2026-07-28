# Release Workflow

This repository uses `dev` as the only release source and `main` as the
published branch. Releases are driven by a trigger tag pushed on the current
`dev` commit.

## Accepted Tags

Two different tags, and only the first one is ever pushed by hand.

The TRIGGER tag starts a release. It is unprotected on purpose, so it can be
created and deleted freely, and the intake workflow deletes it once the release
pull request is open:

```text
pr-vX.Y.Z
pr-vX.Y.Z-beta
pr-vX.Y.Z-betaN
```

The RELEASE tag is derived from it by dropping the `pr-` prefix. It is created
ONCE, by the post-merge workflow, on the squash commit, and it is immutable:

```text
vX.Y.Z
vX.Y.Z-beta
vX.Y.Z-betaN
```

Examples: pushing `pr-v1.2.3-beta2` publishes `v1.2.3-beta2`. The numbered form
exists so more than one prerelease can be cut for the same version; the bare
`-beta` stays valid, so every already-published tag still matches. A `-beta`
suffix, numbered or not, publishes as a GitHub prerelease rather than as the
latest release.

`.github/scripts/release-policy.sh` is the single authority for these formats.
It is sourced from `origin/main`, never from the pushed tag, because the tag
tree is the unreviewed code being released and the release jobs hold write
credentials. A change to the policy therefore takes effect on the release AFTER
the one that lands it on `main`.

The release tag is the source of truth for the integration version. The intake
workflow updates `custom_components/addhon/manifest.json` on `dev` to match the
tag without the leading `v`, so `pr-v1.2.3-beta` writes manifest version
`1.2.3-beta`. Never bump the version by hand.

## Operator Flow

1. Work only on `dev`.
2. Push `dev` first. Intake compares the trigger against `origin/dev` HEAD and,
   on a mismatch, errors AND deletes the trigger tag.
3. Create the trigger tag `pr-vX.Y.Z[-betaN]` on that same commit and push it.
4. Let the workflow update `manifest.json` and open the automatic `dev -> main`
   pull request.
5. If review (e.g. CodeRabbit) asks for changes, just push the fix commits to
   `dev`. The PR updates in place and stays valid: `release-guard` validates the
   live state of `dev` (the manifest version must still match the release tag),
   not a frozen commit, so advancing `dev` no longer invalidates the release.
6. Merge that PR with squash only. `post-merge-release` refuses anything else
   (`PARENT_COUNT != 1`), so a merge commit blocks the release outright.
7. The post-merge workflow moves `dev` to the squash commit, creates the tag
   on that commit, and publishes the GitHub release. `dev` is synchronized only
   when its content matches the squash, so post-review fixes never get lost.

The published `vX.Y.Z` tag is created on the final squash commit at merge time;
during the open PR there is no need to re-tag or re-open the PR after a review
fix.

## Bootstrap

The first PR that adds these workflow files to `main` is not a release PR and
must not include release markers. After it is squash-merged, the post-merge
workflow synchronizes `dev` to the squash commit and exits without creating a
release.

## GitHub Settings Required

The workflow files cannot fully protect `main` by themselves. Configure the
repository settings after the workflow bootstrap PR has reached `main`.

Repository merge settings:

```text
Allow squash merging: enabled
Allow merge commits: disabled
Allow rebase merging: disabled
Automatically delete head branches: disabled
```

Ruleset or branch protection for `main`:

```text
Require pull request before merging
Require status check: release-guard
Block force pushes
Block deletions
Do not allow bypass for administrators
```

The `release-guard` check enforces that PRs into `main` come only from `dev` and
that the PR is tied to a valid release tag.

Ruleset for release tags:

```text
Protect v*.*.*
Block deletion and updates for normal users
Allow only the release automation identity to update the final tag
```

`v*.*.*` covers the prerelease forms too, numbered ones included, since the last
wildcard absorbs the suffix. The `pr-v*` trigger must NOT be protected: intake
deletes it as part of a normal run.

## Token

Set a repository secret named `RELEASE_BOT_TOKEN` using a fine-grained PAT or
GitHub App token with write access to contents and pull requests. The workflows
fall back to `GITHUB_TOKEN`, but a dedicated token is preferred because:

- PRs created with `GITHUB_TOKEN` may not trigger follow-up workflows.
- Protected branch and tag rules often require an explicit bypass identity.

## Current Repository Caveat

Historical tags before this workflow do not use the leading `v` prefix. They are
kept as history; the new validation applies to future tags.
