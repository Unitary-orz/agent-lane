# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Added a root `agent-lane --version` command for installation verification.

### Changed

- Made beta managed commit-signing injection opt-in. New lanes and lanes
  without stored signing metadata now default to `--commit-signing off`;
  existing lanes continue using their persisted mode.
- Clarified the separate CLI and assistant-Skill installation paths, including
  first-run readiness checks and the source-symlink packaging boundary.

## 1.0.0-rc.1 - 2026-08-08

### Added

- Explicit `independent` and `app-sync` execution modes.
- App Sync login configuration with protocol-level readiness checks.
- Session list, find, attach, name, outline, and read commands.
- Unified schema-versioned JSON envelopes and JSONL watch output.
- Bilingual product documentation, open-source governance files, and macOS CI.

### Changed

- Reorganized the CLI into execution, session-view, and self-configuration
  layers.
- Made `run` the stable create-or-resume entry point for a lane.
- Moved the default lane store to `~/.agent-lane/lanes`.
- Moved provider-specific workflow assets under `integrations/`.
- Raised the formally supported Python version to 3.11 or newer.

### Removed

- App page navigation, `--app-refresh`, and `--no-app-refresh`.
- Pre-1.0 command aliases and output-only options such as `--brief`.
- Direct follow-up execution by task ID; follow-up turns now require a lane.
