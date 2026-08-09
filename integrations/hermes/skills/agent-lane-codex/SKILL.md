---
name: agent-lane-codex
description: "Use agent-lane to start, inspect, continue, steer, and close out durable Codex coding tasks, including optional App Sync."
version: 1.0.0-rc.2
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

This skill is the operational entrypoint for the public V1 CLI/JSON contract.
Use it for Codex task selection, execution, continuity, monitoring, reporting,
and acceptance through `agent-lane`.

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

Ordinary execution does not change a lane's mode. To switch the same task
between `independent` and `app-sync`, explicitly repeat `session attach` with
the exact thread ID and requested `--mode`; do not create a second binding.

## Configure a default effort when useful

For a persistent user-level default, use agent-lane configuration rather than
editing Codex configuration:

```bash
agent-lane config effort set xh
agent-lane config effort status
```

Set or clear this persistent preference only when the user requested or
authorized it; otherwise, pass a turn-scoped `--effort` when needed.
`xh` normalizes to `xhigh`. An explicit `--effort` on `run`, `send`, or
`goal run` overrides the user default. Read `effective_effort` and
`effective_effort_source` from JSON instead of assuming which value Codex used.
Use `agent-lane config effort clear` to remove the agent-lane default.

## Start or resume work

`run` is create-or-resume:

```bash
agent-lane codex run \
  --title "<human-facing-task-title>" \
  --mode independent \
  --cwd "<absolute-project-path>" \
  --prompt "<bounded task with acceptance criteria>"
```

When no target is supplied, agent-lane generates the internal stable lane ID.
Use the returned thread ID or another exact selector for later calls. Automation
may still supply and reuse `--lane-id`. Use `--prompt-file` for long or
carefully quoted instructions.

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

When `--effort` is omitted, do not copy the previous alias value into the next
command. Let agent-lane resolve the current user default and verify its reported
effective source.

## Select one existing task

For commands that operate on one task, prefer the exact `--thread-id` returned
by discovery or an earlier result. The other selectors are:

```text
--target-title <exact-known-attached-title>
--current
--lane-id <internal-stable-id>
```

Title matching is exact and case-insensitive. `--current` means the only
attached task whose stored workspace equals the current process directory; it
does not mean the most recent task. Treat `CODEX_TARGET_AMBIGUOUS` as a required
selection step and choose only from `choices[].target_argv`. Never guess among
candidates. Read `target_resolution` to verify the requested and resolved
identities. `CODEX_TARGET_CHANGED` means the binding changed after selection;
rediscover instead of retrying control against the stale result.

## Continue an existing task

```bash
agent-lane codex send \
  --thread-id "<selected-thread-id>" \
  --prompt "<follow-up>"
```

The thread must already be explicitly attached. An unbound control request must
fail with `CODEX_TARGET_ATTACH_REQUIRED`; follow its separate `attach_argv`,
inspect that result, then execute `after_attach_argv`. Do not combine these into
an implicit takeover.

For one active App Sync turn only:

```bash
agent-lane codex steer \
  --thread-id "<selected-thread-id>" \
  --prompt "<additional input>"
```

`steer` must fail rather than start or queue a different turn when the active
turn is absent, ambiguous, or changed.

## Observe without starting work

```bash
agent-lane codex status --thread-id "<selected-thread-id>"
agent-lane codex wait --thread-id "<selected-thread-id>" --timeout 600
agent-lane codex checkpoint --thread-id "<selected-thread-id>" --after 300
agent-lane codex closeout --thread-id "<selected-thread-id>"
```

Use `watch` only when the parent can consume JSONL polling snapshots. A timeout
limits the observation window; it does not prove that the underlying task was
cancelled or completed.

For state decisions, read `execution.state`, `execution.active`,
`execution.evidence`, and `execution.conflicts`. Active thread or live runner
evidence takes precedence over cached `last_turn`, local runner state, and Goal
status. Treat `runner_status` as the effective status and
`local_runner_status` as local/cache evidence only. If `execution.state` is
`unknown`, observe again; do not infer that a new `send` is safe.

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

Discovery, session inspection, and exact-thread `status`, `wait`, `checkpoint`,
`closeout`, and `goal get` are read-only and do not require a lane. Inspect the
returned `control`: `requires_explicit_attach: true` means the task is not
controllable through agent-lane yet, and `attach_argv` gives a machine-readable
explicit next step.

Attach an existing task explicitly:

```bash
agent-lane codex session attach \
  --thread-id "<task-id>"
```

The omitted mode defaults to `independent`; pass `--mode app-sync` explicitly
when shared App control is required. Attach creates only the lane binding and
returns `control.send_target_argv`; it never starts a turn. When no lane ID is
supplied, it generates a deterministic internal binding ID and repeated attach
of that thread reuses it. Follow-up execution is a separate `send` against the
exact thread target.

Use `session name get/set` to read or change the task name. Do not infer that a
name observed in one store has been written to another; live read-back is an
explicit operation.

## Persistent goals

```bash
agent-lane codex goal set \
  --title "<human-facing-task-title>" \
  --cwd "<absolute-project-path>" \
  --objective "<durable objective>"

agent-lane codex goal run \
  --thread-id "<selected-thread-id>" \
  --max-turns 20 \
  --max-runtime 7200
```

A goal keeps the objective durable across turns. Token budget, runtime, and turn
limits bound automation; they are not acceptance evidence.

## Report back to the parent

Report the human-facing task identity, exact thread target, current state,
material artifacts, validation evidence, blockers, and whether another turn
remains. Retain the stable lane ID as machine metadata when useful, but do not
make the human remember it. Distinguish clearly among:

- a task still running;
- an unknown execution state or reported state conflict;
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
  --thread-id "<selected-thread-id>" \
  --confirm-thread-inactive
```

Inspect `closeout` first. Do not delete an uncommitted worktree, an external
worktree, or an active task.

See `references/subagent-usage.md` for a longer orchestration example.
