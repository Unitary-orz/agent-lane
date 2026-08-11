# Subagent usage guide

This guide shows how a parent Hermes agent can delegate one bounded task to
Codex through agent-lane while retaining orchestration responsibility.

## 1. Bound the task

Before starting a lane, determine:

- the exact project directory;
- whether edits are authorized;
- required tests or acceptance evidence;
- whether a worktree is needed;
- whether local commit, push, or publication is authorized;
- whether the task needs live Codex App collaboration.

Do not turn a read-only request into an implementation task. Do not treat local
tests or a commit as production acceptance.

Managed commit-signing injection is a beta feature and defaults to `off`. Do not
initialize it as routine lane setup. When managed signing is explicitly required,
obtain authority to initialize and test it, then pass `--commit-signing agent`.

When the user requested or authorized a persistent effort default, configure it
in agent-lane and verify the result:

```bash
agent-lane config effort set xh
agent-lane config effort status
```

Explicit `--effort` always wins. Read `effective_effort_source`; do not reuse a
previous alias value as an implicit default or edit Codex configuration.

## 2. Select the mode

Choose `independent` unless App collaboration is specifically useful. App Sync
shares task/messages/live turn state, not App page selection.

Before the first independent lane, verify the installed CLI and default
transport:

```bash
agent-lane --version
agent-lane doctor --mode independent --probe
```

For App Sync, read readiness first:

```bash
agent-lane config app-sync status
```

If App Sync is disabled, obtain authorization before running
`agent-lane config app-sync enable`, then read status again.

The status result covers login activation and daemon protocol readiness. Open
the App when `app_running` is false, or reopen it once when
`app_reopen_required` is true, then run
`agent-lane doctor --mode app-sync --probe` to verify full App attachment.
Enabling login integration and reopening Codex App are host changes that require
the appropriate user authorization.

## 3. Start the lane

```bash
agent-lane codex run \
  --title "project-bounded-change" \
  --mode independent \
  --cwd "/path/to/project" \
  --worktree \
  --prompt-file "/path/to/bounded-task.md"
```

The response is one JSON envelope. Retain its exact thread target and internal
`lane_id` as machine state; the human does not need to create or remember the
lane ID. `--title` creates an explicit `custom_title`; omit it when the lane
should keep following the latest observed Codex task name. Read the resolved
`lane_title` from later results. Do not treat an observation timeout as
cancellation.

## 4. Observe proportionally

For a short task:

```bash
agent-lane codex wait \
  --thread-id "<selected-thread-id>" \
  --timeout 600
```

For a longer task, use bounded checkpoints and report only meaningful changes:

```bash
agent-lane codex checkpoint \
  --thread-id "<selected-thread-id>" \
  --after 300
```

Use `status` for an immediate snapshot. Use `watch` only when the parent can
consume JSONL and has a reason to monitor continuously.

Branch on `execution.state` and its evidence. Positive active-thread or live
runner evidence wins over Goal and cached terminal fields. If the state is
`unknown`, observe again instead of issuing another `send`.

## 5. Continue or correct

If the completed turn needs a follow-up:

```bash
agent-lane codex send \
  --thread-id "<selected-thread-id>" \
  --prompt "Run the focused regression test and fix only the reported failure."
```

If an App Sync turn is still running and needs new context, use `steer`. Do not
use `send` to race an active turn.

For a Codex App task with no lane, use `session list` or read it by thread ID
first. The returned `control.attach_argv` is only a suggestion; run an explicit
lane-free `session attach`, inspect the attached result, then use its thread ID
in a separate `send`. Read-only discovery never creates control ownership.
When discovery or a contextual selector returns `CODEX_TARGET_AMBIGUOUS`, choose
one returned `choices[].target_argv`; never infer the most recent candidate.

## 6. Independently inspect the result

```bash
agent-lane codex closeout --thread-id "<selected-thread-id>"
agent-lane codex session outline --thread-id "<selected-thread-id>"
agent-lane codex session read \
  --thread-id "<selected-thread-id>" \
  --include-turns
```

The parent remains responsible for checking that the result matches user scope,
that reported tests are relevant, and that no unrelated changes were included.

## 7. Report and clean up

Give the user:

- the resolved `lane_title` and exact thread target;
- outcome and changed artifacts;
- exact validation performed;
- remaining risks or blockers;
- Git state and any separately authorized external actions.

Only after confirming the task is inactive and the managed worktree is safe:

```bash
agent-lane codex cleanup \
  --thread-id "<selected-thread-id>" \
  --confirm-thread-inactive
```

Never use this cleanup command for an App-owned or manually created worktree.
