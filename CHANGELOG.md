# 📦 Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] 🚧

## [v1.0.0-rc.4] - 2026-08-14 🚀

> Make Codex session discovery compact, live-first, and safer to attach.

### 🔄 Changed

- Made Codex session discovery default to automatic live observation and a
  compact list projection; persisted fallback state is now explicitly stale
  and cannot infer a current terminal execution state.
- Added attach-time workspace preflight using the latest public
  `commandExecution.cwd`, with machine-readable attach-or-run recovery before
  any alias is written while retaining runtime drift protection.

## [v1.0.0-rc.3] - 2026-08-11 🚀

> Separate stable lane identity, live Codex naming, and explicit local title
> overrides.

### ✨ Added

- Added `codex custom-title get/set/clear` for an explicit local lane-title
  override that never renames the Codex task.

### 🔄 Changed

- Defined `lane_title = custom_title ?? codex_title ?? lane_id`; live Codex
  title observations update `codex_title`, while an explicit `custom_title`
  remains authoritative until cleared.

### 🗑️ Removed

- Removed legacy `title`, `title_source`, and `lane_label` fields, and stopped
  persisting the computed `lane_title` and `lane_title_source` values in alias
  schema v4 without migration.

### 🐞 Fixed

- Fixed GitHub Actions annotated-tag validation by fetching the original remote
  tag object into a dedicated verification ref before inspecting it.

## [v1.0.0-rc.2] - 2026-08-09 🚀

> Safer control contracts, clearer defaults, and smoother Codex task handoff.

### ✨ Added

- Added a root `agent-lane --version` command for installation verification.
- Added a user-level default Effort configuration with `xh` normalization,
  explicit-command precedence, and effective value/source reporting.
- Added canonical execution evidence and conflict reporting across execution,
  closeout, and session views.

### 🔄 Changed

- Made beta managed commit-signing injection opt-in. New lanes and lanes
  without stored signing metadata now default to `--commit-signing off`;
  existing lanes continue using their persisted mode.
- Clarified the separate CLI and assistant-Skill installation paths, including
  first-run readiness checks and the source-symlink packaging boundary.
- Made first App-task takeover more direct with control projections, an
  `independent` attach default, lane-free attach, and exact thread-based
  follow-up targets while keeping attach explicit.
- Added unified fail-closed task targeting by thread, exact attached title,
  current working-directory context, or compatible lane ID across execution,
  session, and Goal commands. New `run` and `goal set` operations generate the
  internal lane identity when omitted.
- Kept exact thread-ID reads available when an unrelated lane record is
  unreadable, while control and contextual selection remain fail-closed.
- Made attach and recovery commands self-contained, including observed
  execution mode, complete follow-up arguments, and explicit same-lane mode
  rebinding.
- Made Effort `set` and `clear` repair parseable invalid Effort fields, narrowed
  secret-key detection to credential-shaped names, and allowed trusted daemon
  CLI fallback after candidate-specific probe failures.

### 🚀 Engineering

- Added strict Changelog-driven GitHub Release publication with version,
  test, build, package-install, and duplicate-release checks.

## [v1.0.0-rc.1] - 2026-08-08 🌱

> The first public V1 release candidate and its core execution model.

### ✨ Added

- Explicit `independent` and `app-sync` execution modes.
- App Sync login configuration with protocol-level readiness checks.
- Session list, find, attach, name, outline, and read commands.
- Unified schema-versioned JSON envelopes and JSONL watch output.
- Bilingual product documentation, open-source governance files, and macOS CI.

### 🔄 Changed

- Reorganized the CLI into execution, session-view, and self-configuration
  layers.
- Made `run` the stable create-or-resume entry point for a lane.
- Moved the default lane store to `~/.agent-lane/lanes`.
- Moved provider-specific workflow assets under `integrations/`.
- Raised the formally supported Python version to 3.11 or newer.

### 🗑️ Removed

- App page navigation, `--app-refresh`, and `--no-app-refresh`.
- Pre-1.0 command aliases and output-only options such as `--brief`.
- Direct follow-up execution by task ID; follow-up turns now require a lane.
