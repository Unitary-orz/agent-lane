# Contributing

Thanks for helping improve agent-lane. The project favors small dependencies,
stable machine-readable contracts, and explicit safety boundaries.

## Before opening a change

- Discuss large command, state-schema, or execution-mode changes in an issue.
- Keep Codex integration behind the `codex app-server` JSON-RPC boundary.
- Do not add dependencies on private App databases, bundled IPC, or UI state.
- Keep `codex run` create-or-resume.
- Never place secrets in lane aliases, fixtures, examples, or logs.

## Local setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Validation

Run the full local checks before submitting a pull request:

```bash
python -m ruff check src tests
python -m pytest
python -m build
git diff --check
```

Tests that exercise platform integration should mock host mutations unless the
test is explicitly an opt-in integration test. A passing mock test does not
claim that a particular Codex App build is compatible.

## Changes and documentation

- Add focused tests for new behavior and failure paths.
- Update both `README.md` and `README.zh-CN.md` when the public workflow changes.
- Add user-visible changes to `CHANGELOG.md`.
- Preserve the versioned JSON envelope and stable error codes.
- Use generic paths and identities in public examples.

By contributing, you agree that your contribution is licensed under the MIT
License.
