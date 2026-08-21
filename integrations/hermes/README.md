# Hermes integration

This optional integration teaches Hermes agents to use the public agent-lane V1
CLI. It is not required by the core package and does not modify Hermes source.

The CLI and Skill are separate installations. Before installing the Skill:

- keep a persistent source checkout of this repository;
- install `agent-lane` and make it available on the agent runtime's `PATH`;
- install and authenticate the `codex` CLI.

Install the Skill as a symlink from that source checkout:

```bash
./integrations/hermes/install-skill
```

Set `HERMES_SKILLS_DIR` to override the target skill root. The installer refuses
to replace an existing file, directory, or unrelated symlink. Because the
installed Skill is a symlink, moving or deleting the checkout breaks it. The
Python wheel contains the runtime CLI only and does not install or register this
optional integration.

Verify both layers before starting the first lane:

```bash
command -v agent-lane
agent-lane --version
agent-lane doctor --mode independent --probe
test -f "${HERMES_SKILLS_DIR:-$HOME/.hermes/skills}/autonomous-ai-agents/agent-lane-codex/SKILL.md"
```

Contents:

- `skills/agent-lane-codex/SKILL.md` — agent workflow contract
- `skills/agent-lane-codex/references/subagent-usage.md` — expanded example
- `short-prompt-card.md` — compact delegation prompt

The integration tracks the stable V1 CLI and intentionally omits
pre-1.0 commands such as `recent`, `adopt`, `--brief`, and App page navigation.
