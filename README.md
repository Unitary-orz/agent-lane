# agent-lane

`agent-lane` lets an assistant use Codex for coding work while keeping every
coding session easy to find, inspect, and continue. When a human wants direct
control, the same session can also be opened and continued in Codex App.

V1 supports Codex as its first coding agent.

[中文文档](https://github.com/Unitary-orz/agent-lane/blob/main/README.zh-CN.md) ·
[Architecture](https://github.com/Unitary-orz/agent-lane/blob/main/docs/architecture.md) ·
[Changelog](https://github.com/Unitary-orz/agent-lane/blob/main/CHANGELOG.md)

> Current version: `1.0.0-rc.1`. Python packaging tools may display the
> equivalent version `1.0.0rc1`.

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
  --lane-id settings-page \
  --prompt "Implement the approved settings changes and run focused tests."
```

This is the core job of agent-lane: an assistant can start coding work, show the
human what sessions exist, inspect what happened, and continue the right session
instead of creating disconnected coding tasks.

## Common scenarios

### “Start implementing the new login flow”

The assistant creates a named lane and delegates the task to Codex:

```bash
agent-lane codex run \
  --lane-id login-flow \
  --cwd /path/to/project \
  --commit-signing off \
  --prompt "Implement the new login flow and run its focused tests."
```

`login-flow` becomes the durable name for that coding session. If the work is
paused today, the assistant can return to it tomorrow without asking the human
for a Codex task ID.

### “What happened in the login session?”

The assistant can inspect a session without starting another coding turn:

```bash
agent-lane codex status --lane-id login-flow
agent-lane codex session outline --lane-id login-flow
agent-lane codex session read --lane-id login-flow --include-turns
agent-lane codex closeout --lane-id login-flow
```

This lets the assistant answer practical questions such as:

- Is Codex still working, waiting, or finished?
- What did it change and what did the tests report?
- Is there unfinished work or a Git state that needs attention?

Reading a session does not silently start more work.

### “Continue that session and finish the tests”

The assistant sends a follow-up to the existing lane:

```bash
agent-lane codex send \
  --lane-id login-flow \
  --prompt "Fix the remaining test failures and rerun the focused suite."
```

Codex continues with the same conversation and workspace context. `run` also
has create-or-resume behavior, so the assistant can safely use the same lane in
repeatable workflows.

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
| `codex session list` | `thread/list` | Lists recent main tasks or all task threads using stored or live observation. |
| `codex session find` | `thread/list` with search and local matching | Searches titles, prompts, lane metadata, workspace information, and task summaries. |
| `codex session attach` | `thread/read` | Validates an existing task, then binds it to a stable lane ID and execution mode. |
| `codex session name get` | `thread/read` | Reads the stored or live Codex task name. |
| `codex session name set` | `thread/name/set`, then `thread/read` | Updates the Codex task name with optional conflict checking and exact read-back. |
| `codex session outline` | `thread/read` | Returns a compact projection of task identity, turns, prompts, and execution state. |
| `codex session read` | `thread/read` | Reads the full task, all turns, or one selected turn. |

### Goals and runtime controls

| agent-lane command or option | Codex surface | Implementation |
| --- | --- | --- |
| `codex goal set` | `thread/goal/set` | Creates or updates the objective, status, and optional token budget. |
| `codex goal run` | `thread/goal/get`, `turn/start` | Advances the active goal across bounded turns, runtime, or turn count. |
| `codex goal get` | `thread/goal/get` | Reads the current task goal. |
| `codex goal complete` | `thread/goal/set` | Marks the current goal complete. |
| `codex goal clear` | `thread/goal/clear` | Removes the goal from the Codex task. |
| `--sandbox`, `--model`, `--profile`, `--effort`, `--add-dir`, `--config` | app-server startup plus thread and turn parameters | Passes supported runtime selection and configuration into the Codex execution path. |
| `codex run --worktree` | Git worktree plus `runtimeWorkspaceRoots` | Creates an isolated workspace, records ownership, and binds it to the Codex task. |

Normal commands return one structured JSON envelope. `codex watch` emits JSONL.
Command timeouts limit how long the caller observes a task; they do not redefine
the durable Codex task's completion state.

### App Sync

App Sync exposes the following Codex App integration directly:

| Command or option | Supported function | Implementation |
| --- | --- | --- |
| `config app-sync enable` | Enables shared App/agent task access at login. | Installs and loads the per-user managed runtime and advertises the login environment to newly opened App processes. |
| `config app-sync status` | Reports persistent App Sync readiness. | Checks the managed runtime, socket, compatible Codex CLI, and login configuration. |
| `doctor --mode app-sync --probe` | Verifies end-to-end shared control. | Opens the local WebSocket and completes a JSON-RPC `initialize` probe. |
| `codex run --mode app-sync` | Creates or resumes a task visible to both agent-lane and Codex App. | Uses the shared runtime transport and persists `app-sync` as the lane's fixed execution mode. |
| `codex session list --observe live`, `codex session find --observe live` | Lists or searches current App-visible tasks. | Queries `thread/list` through the shared control plane instead of the independent stdio transport. |
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
