# Architecture

This document describes the V1 boundaries behind `agent-lane`. It is a design
contract for contributors, not a promise that private Codex internals are
stable.

## Product boundary

`agent-lane` is an external control layer for durable Codex tasks. It does not
patch Codex, read Codex private databases as a product dependency, or automate
Codex App navigation. The supported integration boundary is the JSON-RPC
protocol exposed by `codex app-server` and, for App Sync, the managed shared
daemon that exposes the same protocol over a local socket.

The upstream app-server surface is experimental. Its behavior must be probed at
runtime, and failures must remain structured and fail closed.

## Three public layers

### Execution layer

Commands under `agent-lane codex` create, resume, observe, and close out work:

- `run` is create-or-resume, never create-only.
- `send` starts a follow-up turn on an explicitly attached task.
- `steer` adds input only to one unambiguous active App Sync turn.
- `status`, `wait`, `watch`, and `checkpoint` observe execution.
- `closeout` summarizes completion and Git state.
- `cleanup` removes only safely owned managed worktrees.
- `goal` stores and advances a durable objective.

### Session view layer

`codex session` exposes task discovery separately from execution:

- `list` and `find` enumerate stored or live task projections.
- `attach` binds an existing Codex task to a lane.
- `name`, `outline`, and `read` inspect task identity and content.

Stored observation is the default. Live observation is explicit and must not
silently fall back to stale state when the caller asked for live data.

### Self-configuration layer

Top-level commands operate agent-lane itself:

- `doctor` checks readiness for a selected execution mode.
- `config app-sync` manages optional login activation for the shared runtime.
- `config effort` manages the user-level default turn effort.
- `signing` manages the project-owned SSH signing agent.

## Target resolution, lane identity, and task binding

A lane ID is the stable internal identifier owned by agent-lane. A Codex task
ID is a replaceable provider binding. Callers can address a task without
knowing its lane ID.

```text
user selection -> target resolver -> internal lane ID -> binding generation -> Codex task ID
```

The resolver accepts one explicit selector: exact `thread-id`, exact
case-insensitive attached title, the unique attached task for the current
working directory, or a lane ID retained for automation compatibility. It does
not apply recency heuristics. Zero matches return not-found or, for an exact
unbound thread and a control operation, attach-required. Multiple matches
return `CODEX_TARGET_AMBIGUOUS` with machine-readable choices. The selected
binding is checked again after acquiring the operation lock; drift returns
`CODEX_TARGET_CHANGED`.

Aliases are JSON documents under `~/.agent-lane/lanes/codex` by default. They
contain public execution metadata and binding state, never secrets. A binding
replacement increments its generation so a caller can distinguish continuity
of intent from continuity of the underlying task.

The lane's execution mode is written with the binding. Ordinary execution
cannot change it in place; an explicit `session attach` for the same task may
rebind the existing lane to a requested mode. Missing mode metadata on a legacy
alias resolves to `independent`; invalid, empty, or conflicting persisted mode
data fails closed until an explicit re-attach repairs it.

Discovery and inspection never create a binding. Session projections expose a
`control` object that distinguishes `unattached` from `attached` tasks and gives
machine-readable attach arguments based on exact thread IDs and the observed
transport. A rejected control request additionally returns the complete
original command as `after_attach_argv`. The authority transition is always a
separate, explicit `session attach` operation; attach without `--lane-id`
generates a deterministic internal ID. A new attach defaults to `independent`;
choosing App Sync remains explicit. Read-only execution and Goal inspection may
target an exact unbound thread directly, but control operations fail before
app-server access until attached.

Alias-registry scans used for control or contextual selection fail closed when
an entry is unreadable because uniqueness cannot then be proven. Exact
thread-ID reads may bypass unrelated unreadable entries because they do not
create control authority. Legacy alias files without an embedded `lane_id`
derive their internal ID from the filename. Successful commands expose
`target_resolution`; lane identity remains in JSON for durable automation even
when the human-facing command did not require it.

## Execution transports

### Independent

Independent mode launches a dedicated `codex app-server` over stdio for the
operation. It is the default because it has the smallest host-level footprint
and does not require login configuration or an open Codex App.

### App Sync

App Sync connects to the managed Codex shared daemon over a local WebSocket.
The App and agent-lane can therefore observe the same tasks, messages, and live
turns. Shared runtime state does not include UI page selection.

App Sync readiness requires all of the following:

1. macOS and a usable `codex` executable;
2. an installed and loaded per-user LaunchAgent;
3. successful daemon discovery and socket reachability;
4. a successful WebSocket and JSON-RPC `initialize` handshake;
5. the login environment flag visible to newly opened App processes.

CLI/shared-runtime app-server version mismatch is a warning only after the
protocol probe succeeds.
Protocol failure remains an error and the login environment is not advertised.
Enabling or disabling while Codex App is running may require reopening it once.

`disable` removes future login activation; it deliberately does not terminate a
daemon that may still be serving the App or another client.

## State and concurrency

Alias writes use a temporary file followed by an atomic replace. Per-lane and
per-task locks prevent concurrent callers from starting conflicting turns or
binding one task to multiple lanes without an explicit replacement path.

`run`, `send`, `status`, `closeout`, `session list`, and `session read` use one
execution projection. Its precedence is:

1. positive active-turn evidence from the observed Codex thread;
2. a live local runner process;
3. an observed terminal turn or inactive thread;
4. local runner and alias history.

Goal status is separate lifecycle evidence and never overrides an active turn.
The `execution` object reports the effective state, source, evidence, and stable
conflict codes. Its `active` value is `null` when neither active nor inactive
can be established. Compatibility fields mirror the decision:
`runner_status` is effective, `local_runner_status` is local/cache-only,
`thread_active` reports observed thread activity, and `last_turn` is normalized
to the active turn while execution is active. Raw Codex and alias data remain
available in detailed views as evidence rather than competing decisions.

Long-running commands separate the durable task from the observation window.
A command timeout limits how long the caller waits; it does not redefine task
completion. Timeout errors carry the same execution projection. A still-active
or unknown turn recommends observation and is not marked safe for a duplicate
control retry.

## User defaults

User defaults are stored in `~/.agent-lane/config.json` with an independent
schema version. Effort resolution is deterministic: explicit `--effort`, then
the user configuration, then unset so Codex may apply its own default. `xh` is
accepted as input and normalized to `xhigh`. The effective value and source are
persisted with the turn metadata and returned in JSON.

Legacy unversioned configuration with a top-level `effort` remains readable;
conflicting legacy and current fields fail closed. A previous lane alias records
what an earlier turn used but is not a default for a new turn. agent-lane does
not write Codex configuration to implement its default.

## Worktree ownership

When `--worktree` is requested, agent-lane records the created worktree,
repository, branch, and ownership markers in the alias. Cleanup checks those
markers, Git state, active-task state, and caller confirmation before removing
anything. App-created or otherwise external worktrees are not agent-lane-owned.

## Signing boundary

Managed signing is a beta, opt-in capability. New lanes and lanes without a
stored signing mode default to `off`; a persisted lane mode remains binding.
Selecting `--commit-signing agent` runs an isolated SSH agent beneath
`~/.agent-lane/signing`. Only the public key path and socket environment are
injected into lane work. Lane aliases do not contain secret material.

An incompatible inherited Git signing configuration is not overwritten unless
the caller passes `--allow-signing-replacement`. Signing readiness and the
effective task shell fail closed; there is no silent fallback to unsigned
commits. `--commit-signing off` omits managed signing from the lane.

## Output contract

Every normal public command returns one envelope:

```json
{
  "schema_version": 1,
  "ok": true,
  "command": "codex.status",
  "data": {},
  "error": null,
  "warnings": []
}
```

Failure envelopes keep `command` and `schema_version`, set `data` to `null`, and
include a stable error code, human message, retryability, and structured
details. `watch` emits the same logical records as JSONL.

Human-readable diagnostics belong in messages; automations must branch on
codes and fields. CLI parse errors use the same envelope instead of argparse's
default mixed stdout/stderr behavior.

## Module map

```text
src/agent_lane/
  cli.py             thin public entry point
  entry.py           parse, dispatch, envelope, migration errors
  parser.py          V1 command grammar
  output.py          JSON/JSONL output contract
  commands/          execution, session, and system registries
  control_plane.py   lane operations and orchestration
  codex_rpc.py       app-server JSON-RPC client
  daemon_transport.py shared-daemon discovery and transport
  app_runtime.py     read-only App runtime discovery
  app_sync.py        macOS login integration
  settings.py        per-user agent-lane defaults
  state.py           lane alias persistence
  workspace.py       worktree and locking rules
  signing.py         isolated SSH signing agent
```

## Compatibility policy

V1 is a hard break from pre-1.0 syntax. Removed UI navigation and legacy options
return explicit migration errors instead of being ignored. The release
candidate may still change before `1.0.0`, but changes to the JSON envelope,
lane identity, or execution-mode semantics require corresponding contract tests
and changelog entries.
