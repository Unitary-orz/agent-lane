# Release process

agent-lane uses `CHANGELOG.md` as the single source for GitHub Release notes.
Pushing a matching `v*` tag authorizes the release workflow to validate, build,
and publish the corresponding GitHub Release without a draft or a separate
manual confirmation step.

## Release contract

Before pushing a tag, all of the following must already be committed:

- the target version in `pyproject.toml` and the package version sources;
- one exact `CHANGELOG.md` heading in the form
  `## [vX.Y.Z] - YYYY-MM-DD`, with an optional prerelease suffix and trailing
  presentation text;
- the release's user-facing changes and relevant engineering changes.

Emoji belong in the trailing presentation text and change-category headings,
not before the version token. For example:

```markdown
## [v1.0.0-rc.2] - 2026-08-09 🚀

> One concise release summary.

### ✨ Added

- Added one user-facing capability.

### 🚀 Engineering

- Added one release or validation improvement.
```

## Local preview

Generate the exact Release body before tagging:

```bash
python scripts/release_notes.py \
  --tag vX.Y.Z \
  --repository-url https://github.com/<owner>/<repository> \
  --commit-sha <commit-sha> \
  --previous-tag vPREVIOUS \
  --output release-notes.md
```

Generation fails when the tag is malformed, the project version differs, or
the matching Changelog section is missing, duplicated, or empty. There is no
fallback Release text.

## Automatic publication

The release entrypoint is an annotated tag pushed after its commit is present
on the remote:

```bash
git push origin main
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

The tag workflow then:

1. runs Ruff and the full test suite on Python 3.11, 3.12, and 3.13;
2. builds the wheel and source distribution;
3. installs the wheel in a clean virtual environment and verifies its version;
4. generates Release Notes from the exact matching Changelog section;
5. publishes the GitHub Release and uploads both distributions.

Prerelease tags such as `v1.0.0-rc.2` are published with GitHub's prerelease
flag and are not marked as Latest. Stable tags use GitHub's normal latest
release selection. An existing Release is never overwritten automatically.

Any failed validation stops publication. Deleting, replacing, or republishing
a tag or Release remains a separate destructive recovery operation.
