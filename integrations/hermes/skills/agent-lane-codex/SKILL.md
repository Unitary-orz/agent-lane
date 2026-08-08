---
name: agent-lane-codex
description: Delegate durable coding work to Codex through a stable agent-lane lane, then inspect and continue it without changing the parent runtime.
version: 1.0.0-rc.1
author: Unitary-orz
license: MIT
platforms: [macos]
created_by: agent
metadata:
  hermes:
    tags:
      - codex
      - delegation
      - durable-task
---

# Agent Lane Codex

Use this skill when a task should run in Codex while the parent agent remains
responsible for user intent, authorization, progress reporting, and acceptance.

Operational commands are JSON-first. Except for discovery output from `--help`
and `--version`, read `ok`, `error.code`, `data`, and `warnings`; do not parse
human-readable messages as a control protocol.

## Locate the CLI

Prefer an installed `agent-lane`. From a source checkout, use:

```bash
<repo-root>/bin/agent-lane
```

Never assume the sample paths below exist. Resolve the real repository and task
working directory first.

Before the first lane, verify the resolved command and the default independent
execution path. Substitute the source-checkout runner above when necessary:

```bash
agent-lane --version
agent-lane doctor --mode independent --probe
```

Managed commit-signing injection is a beta feature and defaults to `off`. Do not
initialize signing as routine lane setup. Only when managed signing is explicitly
required, obtain authority to run `agent-lane signing init --generate`, verify it
with `agent-lane signing test`, and pass `--commit-signing agent`.

## Choose one execution mode

Use `independent` by default. It starts a dedicated stdio app-server process and
does not require Codex App integration.

Use `app-sync` only when the user needs Codex App and the delegated task to share
messages and live turn state, or when an active turn must be steered. Confirm
readiness first:

```bash
agent-lane config app-sync status
```

If App Sync is not enabled, do not enable it automatically. After the host-level
change is explicitly authorized, enable it and read readiness again:

```bash
agent-lane config app-sync enable
agent-lane config app-sync status
```

The status `ready` field covers login activation and daemon protocol readiness,
not App attachment. If `app_running` is false, the App must be opened; if
`app_reopen_required` is true, it must be reopened once. Then verify the full
connection with `agent-lane doctor --mode app-sync --probe`.

Do not enable App Sync, change login configuration, or ask the user to reopen
Codex App unless that host-level change is authorized.

The mode is fixed for a lane. Create another lane rather than attempting to
change it in place.

## Start or resume work

`run` is create-or-resume:

```bash
agent-lane codex run \
  --lane-id "<stable-lane-id>" \
  --mode independent \
  --cwd "<absolute-project-path>" \
  --prompt "<bounded task with acceptance criteria>"
```

Reuse the same lane ID for the same durable task. Use `--prompt-file` for long
or carefully quoted instructions.

Common optional controls:

```text
--sandbox read-only|workspace-write|danger-full-access
--model <model>
--profile <codex-profile>
--effort <effort>
--add-dir <absolute-path>
--config KEY=VALUE
--worktree
--commit-signing off|agent (default: off; agent is Beta)
--allow-signing-replacement
--timeout <seconds>
```

`--worktree` is valid on the first `run`. Treat cleanup, signing replacement,
Git commit, push, and publication as separate permissions.

## Continue an existing lane

```bash
agent-lane codex send \
  --lane-id "<stable-lane-id>" \
  --prompt "<follow-up>"
```

Follow-ups require a lane ID. Direct `send --thread-id` was removed in V1.

For one active App Sync turn only:

```bash
agent-lane codex steer \
  --lane-id "<stable-lane-id>" \
  --prompt "<additional input>"
```

`steer` must fail rather than start or queue a different turn when the active
turn is absent, ambiguous, or changed.

## Observe without starting work

```bash
agent-lane codex status --lane-id "<stable-lane-id>"
agent-lane codex wait --lane-id "<stable-lane-id>" --timeout 600
agent-lane codex checkpoint --lane-id "<stable-lane-id>" --after 300
agent-lane codex closeout --lane-id "<stable-lane-id>"
```

Use `watch` only when the parent can consume JSONL polling snapshots. A timeout
limits the observation window; it does not prove that the underlying task was
cancelled or completed.

## Discover or attach sessions

```bash
agent-lane codex session list --scope all --threads main
agent-lane codex session find "<query>" --observe live
agent-lane codex session outline --thread-id "<task-id>" --observe live
agent-lane codex session read --thread-id "<task-id>" --include-turns
```

Stored observation is the default. If the parent requires current Codex state,
request `--observe live` and surface any failure; do not silently substitute a
cached view.

Attach an existing task explicitly:

```bash
agent-lane codex session attach \
  --lane-id "<stable-lane-id>" \
  --thread-id "<task-id>" \
  --mode independent
```

Use `session name get/set` to read or change the task name. Do not infer that a
name observed in one store has been written to another; live read-back is an
explicit operation.

## Persistent goals

```bash
agent-lane codex goal set \
  --lane-id "<stable-lane-id>" \
  --cwd "<absolute-project-path>" \
  --objective "<durable objective>"

agent-lane codex goal run \
  --lane-id "<stable-lane-id>" \
  --max-turns 20 \
  --max-runtime 7200
```

A goal keeps the objective durable across turns. Token budget, runtime, and turn
limits bound automation; they are not acceptance evidence.

## Report back to the parent

Report the stable lane ID, current state, material artifacts, validation
evidence, blockers, and whether another turn remains. Distinguish clearly among:

- a task still running;
- a completed Codex turn;
- tests that passed;
- a local Git commit;
- user or production acceptance.

Do not claim a push, release, deployment, or external message unless that action
was separately authorized and verified.

## Cleanup

Cleanup is only for an agent-lane-owned managed worktree:

```bash
agent-lane codex cleanup \
  --lane-id "<stable-lane-id>" \
  --confirm-thread-inactive
```

Inspect `closeout` first. Do not delete an uncommitted worktree, an external
worktree, or an active task.

See `references/subagent-usage.md` for a longer orchestration example.
