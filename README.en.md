# agent-lane

`agent-lane` lets an assistant use Codex for coding work while keeping every
coding session easy to find, inspect, and continue. When a human wants direct
control, the same session can also be opened and continued in Codex App.

V1 supports Codex as its first coding agent.

[中文文档](https://github.com/Unitary-orz/agent-lane/blob/main/README.md) ·
[Architecture](https://github.com/Unitary-orz/agent-lane/blob/main/docs/architecture.md) ·
[Changelog](https://github.com/Unitary-orz/agent-lane/blob/main/CHANGELOG.md)

> Current version: `1.0.0`.

## See what it does in a real conversation

Suppose several coding sessions have already been created. The human does not
need to remember their Codex task IDs or the commands that created them:

> **Human:** Show me my recent coding sessions.
>
> **Assistant:**
> 1. `login-flow` — tests are still failing
> 2. `settings-page` — design review completed
> 3. `api-cleanup` — implementation completed
>
> **Human:** Continue 2. Start implementing the approved settings changes.

The assistant uses agent-lane to find the selected session and sends the new
instruction to the same Codex session. Its earlier conversation, decisions, and
workspace context remain available.

```bash
agent-lane codex session list --scope all --limit 10
agent-lane codex send \
  --thread-id "<thread-id-from-item-2>" \
  --prompt "Implement the approved settings changes and run focused tests."
```

This is the core job of agent-lane: an assistant can start coding work, show the
human what sessions exist, inspect what happened, and continue the right session
instead of creating disconnected coding tasks.

## Common scenarios

### “Start implementing the new login flow”

The assistant starts a named task and delegates it to Codex. No lane ID is
needed for the normal create path:

```bash
agent-lane codex run \
  --title login-flow \
  --cwd /path/to/project \
  --commit-signing off \
  --prompt "Implement the new login flow and run its focused tests."
```

agent-lane generates and persists an internal stable lane ID. Explicit
`--title login-flow` stores `login-flow` as `custom_title` and also seeds the
new Codex task name. If `--title` is omitted, no custom title is stored and the
lane follows the latest observed Codex task name. Results expose
`lane_title = custom_title ?? codex_title ?? lane_id` together with
`lane_title_source`; the human does not have to name or remember a lane ID.

To keep one reasoning effort as the user default, configure agent-lane once:

```bash
agent-lane config effort set xh
agent-lane config effort status
```

`xh` is normalized to `xhigh`. A later `run`, `send`, or `goal run` uses this
default unless that command passes `--effort`; the explicit command value wins.
The JSON reports `effective_effort` and `effective_effort_source`. This setting
is stored in `~/.agent-lane/config.json` and does not modify Codex configuration.

### “What happened in the login session?”

The assistant can inspect a session without starting another coding turn:

```bash
agent-lane codex status --thread-id "<selected-thread-id>"
agent-lane codex session outline --thread-id "<selected-thread-id>"
agent-lane codex session read --thread-id "<selected-thread-id>" --include-turns
agent-lane codex closeout --thread-id "<selected-thread-id>"
```

This lets the assistant answer practical questions such as:

- Is Codex still working, waiting, or finished?
- What did it change and what did the tests report?
- Is there unfinished work or a Git state that needs attention?

Reading a session does not silently start more work.

State-bearing results use `execution` as the canonical decision object.
`execution.state` is `active`, `inactive`, or `unknown`; `execution.evidence`
records the observed thread, local runner, last turn, and Goal, while
`execution.conflicts` preserves disagreements. Positive active-thread or live
runner evidence wins over cached terminal fields and Goal lifecycle state.
`runner_status` is the effective turn status; `local_runner_status` is only the
local runner/cache status. A blocked or completed Goal therefore does not make
an actually running turn inactive.

### “Continue that session and finish the tests”

The assistant sends a follow-up to the selected task:

```bash
agent-lane codex send \
  --thread-id "<selected-thread-id>" \
  --prompt "Fix the remaining test failures and rerun the focused suite."
```

Codex continues with the same conversation and workspace context. `run` also
has create-or-resume behavior. Automation that already owns a lane ID may still
pass `--lane-id`; normal interactive use can continue from the selected thread.

### Task targets and fail-closed selection

Commands that operate on one task accept one of four mutually exclusive
targets: `--thread-id` for an exact discovered session, `--target-title` for an
exact known attached custom or Codex title, `--current` for the only attached
task whose stored workspace equals the process working directory, or
`--lane-id` for compatible automation. `run` and `goal set` may omit all four
to create a new task with an internally generated lane ID.

Title matching is exact and case-insensitive; `--current` does not mean “most
recent.” If a title or current directory matches more than one task, the command
fails with `CODEX_TARGET_AMBIGUOUS` and returns `choices[].target_argv`. It never
guesses. Successful results report the requested and resolved identities in
`target_resolution`. A binding change between selection and control fails with
`CODEX_TARGET_CHANGED`.

## Codex support

The integration boundary is the JSON-RPC surface exposed by `codex app-server`.
`independent` mode connects over stdio; `app-sync` connects to a managed shared
runtime over a local WebSocket. agent-lane does not depend on Codex private
databases or App UI automation.

### Task execution and observation

| agent-lane command | Codex surface | Implementation |
| --- | --- | --- |
| `doctor --mode independent --probe` | app-server `initialize` | Verifies the Codex CLI and independent stdio JSON-RPC path before task execution. |
| `codex run` | `thread/start`, `thread/resume`, `turn/start` | Creates or resumes a lane, persists its Codex task binding, then starts one turn. |
| `codex send` | `thread/resume`, `turn/start` | Starts a follow-up turn on the task already bound to the lane. |
| `codex steer` | `thread/read`, `turn/steer` | Adds input to one verified active App Sync turn using its expected turn ID. |
| `codex status` | `thread/read` | Combines live task state with the persisted lane and runner state. |
| `codex wait` | task and turn observation | Polls until the current turn reaches a terminal state or the observation timeout expires. |
| `codex watch` | task and turn observation | Emits the same observation flow as JSONL snapshots. |
| `codex checkpoint` | `thread/read` | Waits once, then returns one lane snapshot for scheduled or bounded workflows. |
| `codex closeout` | `thread/read` plus local Git state | Returns task completion, final output, worktree, and Git closeout information. |
| `codex cleanup` | task activity plus managed-worktree metadata | Removes only an inactive worktree recorded as owned by agent-lane, after safety checks. |

### Session access

| agent-lane command | Codex surface | Implementation |
| --- | --- | --- |
| `codex session list` | `thread/list` | Lists recent main tasks or all task threads; defaults to automatic live observation and a compact projection. |
| `codex session find` | `thread/list` with search and local matching | Searches titles, prompts, lane metadata, workspace information, and task summaries. |
| `codex session attach` | `thread/read` | Validates an existing task's public workspace evidence (latest command first, then task cwd) before binding it to an internal stable lane ID and execution mode; a caller-supplied lane ID is optional. |
| `codex session name get` | `thread/read` | Reads the stored or live Codex task name. |
| `codex session name set` | `thread/name/set`, then `thread/read` | Updates the Codex task name with optional conflict checking and exact read-back. |
| `codex custom-title get/set/clear` | local lane alias | Reads, sets, or clears the explicit local override used by `lane_title`; it never renames the Codex task. |
| `codex session outline` | `thread/read` | Returns a compact projection of task identity, prompts, and historical turn status. |
| `codex session read` | `thread/read` | Reads the full task, all turns, or one selected turn. |

`session list --detail metadata`, `session list --detail summary`, and
`session read` include a machine-readable `control` object; the default compact
list exposes only `requires_attach` from that contract.
An unbound task remains read-only and reports `requires_explicit_attach: true`
plus an `attach_argv` suggestion. Stored observation suggests `independent`;
live observation suggests `app-sync`. Control begins only after an explicit
attach:

```bash
agent-lane codex session read --thread-id "<task-id>" --include-turns
agent-lane codex session attach \
  --thread-id "<task-id>"
agent-lane codex send \
  --thread-id "<task-id>" \
  --prompt "Continue the verified task."
```

Read-only session queries default to `--observe auto`; `session list` and
`session find` also default to `--detail compact`. Auto observation uses live
App Sync state when available. If live observation is unavailable, persisted
data remains visible as historical evidence, but current execution is reported
as `state: unknown`, `stale: true`, with an explicit warning; persisted turn
status is never promoted to an authoritative terminal state. `outline` likewise
retains turn statuses only as history under that warning. Use
`--detail metadata` or `--detail summary` for the larger list projections.

Attach defaults to `independent`; pass `--mode app-sync` explicitly when shared
App control is required. The first attach generates an internal stable lane ID
when none is supplied, and a repeated attach of the same thread reuses it. An
explicit repeated attach may change that lane's execution mode without creating
a second binding.
Before writing the binding, attach compares the requested cwd with the latest
public `commandExecution.cwd`, falling back to the public `thread.cwd` when no
command cwd is available. A different workspace fails with
`CODEX_ATTACH_WORKSPACE_DRIFT`; a first or App-adopted binding receives an exact
attach retry, while an existing managed lane is directed through the established
`run` task-replacement path. `workspace_preflight.status` is `unavailable` only
when neither source exposes a cwd. The runtime workspace-drift check remains the
final guard.
Neither discovery nor reading creates a lane binding. Read-only `status`,
`wait`, `checkpoint`, `closeout`, and `goal get` can inspect an exact unbound
thread without attaching; a control command instead returns
`CODEX_TARGET_ATTACH_REQUIRED` with separate `attach_argv` and
`after_attach_argv` steps. The latter preserves the complete original control
request; read-only projections do not invent a follow-up prompt.

### Goals and runtime controls

| agent-lane command or option | Codex surface | Implementation |
| --- | --- | --- |
| `codex goal set` | `thread/goal/set` | Creates or updates the objective, status, and optional token budget. |
| `codex goal run` | `thread/goal/get`, `turn/start` | Advances the active goal across bounded turns, runtime, or turn count. |
| `codex goal get` | `thread/goal/get` | Reads the current task goal. |
| `codex goal complete` | `thread/goal/set` | Marks the current goal complete. |
| `codex goal clear` | `thread/goal/clear` | Removes the goal from the Codex task. |
| `--sandbox`, `--model`, `--profile`, `--effort`, `--add-dir`, `--config` | app-server startup plus thread and turn parameters | Passes supported runtime selection and configuration into the Codex execution path. |
| `config effort set/status/clear` | agent-lane user configuration | Manages the default turn effort without rewriting Codex configuration; explicit `--effort` takes precedence. |
| `codex run --worktree` | Git worktree plus `runtimeWorkspaceRoots` | Creates an isolated workspace, records ownership, and binds it to the Codex task. |

Normal commands return one structured JSON envelope. `codex watch` emits JSONL.
Command timeouts limit how long the caller observes a task; they do not redefine
the durable Codex task's completion state. Timeout errors include the same
`execution` evidence and recommend observation instead of a duplicate `send`
when the turn is still active or its state is unknown.

### App Sync

App Sync exposes the following Codex App integration directly:

| Command or option | Supported function | Implementation |
| --- | --- | --- |
| `config app-sync enable` | Enables shared App/agent task access at login. | Installs and loads the per-user managed runtime and advertises the login environment to newly opened App processes. |
| `config app-sync status` | Reports persistent App Sync readiness. | Checks the managed runtime, socket, compatible Codex CLI, and login configuration. |
| `doctor --mode app-sync --probe` | Verifies end-to-end shared control. | Opens the local WebSocket and completes a JSON-RPC `initialize` probe. |
| `codex run --mode app-sync` | Creates or resumes a task visible to both agent-lane and Codex App. | Uses the shared runtime transport and persists `app-sync` as the lane's execution mode. |
| `codex session list`, `codex session find` | Lists or searches current App-visible tasks when App Sync is available. | Defaults to `--observe auto`; it queries the shared control plane first and marks a persisted fallback non-authoritative. |
| `codex session name get --observe live`, `codex session outline --observe live`, `codex session read --observe live` | Reads current App-visible task metadata, messages, and turn state. | Queries `thread/read` through the shared control plane. |
| `codex session attach --mode app-sync` | Brings an App-created Codex task under lane management. | Validates the task, acquires task/lane locks, and stores the lane binding. |
| `codex steer` | Adds input to the active shared turn. | Sends `turn/steer` only after identifying one unambiguous active turn. |
| `config app-sync disable` | Stops App Sync from activating at future logins. | Removes future login activation without terminating a runtime that may still have clients. |

App Sync shares tasks, messages, and live turns. It does not control which page
Codex App displays, and it does not permit conflicting turns to be started
safely at the same time. It is optional, macOS-only, and uses experimental
Codex capabilities that may change between releases.

### Commit-signing injection (Beta, opt-in)

| Command or option | Supported function | Implementation |
| --- | --- | --- |
| `signing init --generate` | Creates the managed signing identity. | Generates a dedicated Ed25519 key and starts an isolated SSH agent under `~/.agent-lane/signing`. |
| `signing status` | Reports key and agent state. | Returns the public-key path, fingerprint, agent status, and whether the key is loaded. |
| `signing test` | Verifies signed commits before a Codex task uses the identity. | Creates a temporary Git repository and checks a local signed-commit smoke test. |
| `signing stop` | Stops the managed SSH agent. | Stops the agent and removes its socket and environment record without deleting the key. |
| `--commit-signing agent` | Enables beta managed signing for `run`, `send`, `goal set`, or `goal run`. | Supplies the SSH agent socket, public-key path, and temporary Git config, then probes the effective Codex task shell. |
| `--commit-signing off` | Runs the lane without managed signing. | Omits all agent-lane signing environment injection. |

Private-key material is not stored in lane aliases, and repository or global
Git config is not rewritten. An incompatible inherited signing setup is not
replaced without `--allow-signing-replacement`.

This feature is beta and opt-in. New lanes, and lanes without a stored signing
mode, default to `off`; an existing lane continues using its stored mode.
Selecting `agent` fails closed if the signing identity or effective Codex shell
cannot be verified. Applying signing to an already loaded App Sync task may
require a replacement task and explicit `--allow-signing-replacement`.

## Install

Requirements:

- macOS
- Python 3.11 or newer
- An installed and authenticated `codex` CLI

### Install the CLI

```bash
git clone https://github.com/Unitary-orz/agent-lane.git
cd agent-lane
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install .
agent-lane --version
agent-lane --help
agent-lane doctor --mode independent --probe
```

The final command verifies the default independent path through the authenticated
Codex app-server before the first lane is started.

### Install the assistant Skill (optional)

The CLI and assistant Skill are installed separately. To let a compatible
assistant runtime discover the bundled operating guide, run this from the same
source checkout:

```bash
./integrations/hermes/install-skill
```

The installer creates a symlink to the checkout, so keep the checkout at the
same path. The Python wheel installs the CLI only; it does not register the
optional Skill. See the
[assistant integration guide](https://github.com/Unitary-orz/agent-lane/blob/main/integrations/hermes/README.md)
for the target directory, override, and verification details.

Run `agent-lane <surface> --help` to see every option. The exact output contract
and runtime design are documented in
[Architecture](https://github.com/Unitary-orz/agent-lane/blob/main/docs/architecture.md).

## Development

```bash
python3.11 -m pip install -e ".[dev]"
python3.11 -m ruff check src tests
python3.11 -m pytest
python3.11 -m build
```

See [CONTRIBUTING.md](https://github.com/Unitary-orz/agent-lane/blob/main/CONTRIBUTING.md)
for contribution rules and
[SECURITY.md](https://github.com/Unitary-orz/agent-lane/blob/main/SECURITY.md)
for private vulnerability reporting. The project is licensed under the
[MIT License](https://github.com/Unitary-orz/agent-lane/blob/main/LICENSE).
