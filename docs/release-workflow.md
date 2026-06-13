# Release Workflow

This repository uses `dev` as the only release source and `main` as the
published branch. Releases are driven by tags created on the current `dev`
commit.

## Accepted Tags

Only these formats are accepted from now on:

```text
vX.Y.Z
vX.Y.Z-beta
```

Examples:

```text
v1.2.3
v1.2.3-beta
```

The release tag is the source of truth for the integration version. When the tag
is pushed, the intake workflow updates
`custom_components/haier_hon/manifest.json` on `dev` to match the tag without
the leading `v`. For example, tag `v1.2.3-beta` writes manifest version
`1.2.3-beta`.

## Operator Flow

1. Work only on `dev`.
2. Create the release tag on the current `dev` HEAD.
3. Push the tag.
4. Let the workflow update `manifest.json` and open the automatic `dev -> main`
   pull request.
5. If review (e.g. CodeRabbit) asks for changes, just push the fix commits to
   `dev`. The PR updates in place and stays valid: `release-guard` validates the
   live state of `dev` (the manifest version must still match the release tag),
   not a frozen commit, so advancing `dev` no longer invalidates the release.
6. Merge that PR with squash only.
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
Protect v*.*.*-beta
Block deletion and updates for normal users
Allow only the release automation identity to update the final tag
```

## Token

Set a repository secret named `RELEASE_BOT_TOKEN` using a fine-grained PAT or
GitHub App token with write access to contents and pull requests. The workflows
fall back to `GITHUB_TOKEN`, but a dedicated token is preferred because:

- PRs created with `GITHUB_TOKEN` may not trigger follow-up workflows.
- Protected branch and tag rules often require an explicit bypass identity.

## Current Repository Caveat

Historical tags before this workflow do not use the leading `v` prefix. They are
kept as history; the new validation applies to future tags.
