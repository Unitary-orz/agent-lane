# Short prompt card

Use agent-lane to delegate this bounded task to Codex. Keep the parent runtime
unchanged and keep reporting responsibility here.

1. Resolve the real project path and authorization boundary.
2. Before the first lane, run `agent-lane --version` and
   `agent-lane doctor --mode independent --probe`.
3. Use `independent` mode unless live Codex App collaboration is required.
4. Start or resume with `agent-lane codex run --lane-id <stable-id> ...`.
5. Observe with bounded `status`, `wait`, or `checkpoint` calls.
6. Continue with `agent-lane codex send --lane-id <stable-id>`; use
   `agent-lane codex steer --lane-id <stable-id>` only for one active App Sync
   turn.
7. Validate the result independently, then report evidence and Git state.
8. Do not push, publish, deploy, or clean up without the corresponding scope and
   safety checks.

Useful commands:

```bash
agent-lane codex status --lane-id <stable-id>
agent-lane codex closeout --lane-id <stable-id>
agent-lane codex session read --lane-id <stable-id> --include-turns
```

App Sync readiness:

```bash
agent-lane config app-sync status
# If disabled, obtain authorization before enabling it:
agent-lane config app-sync enable
agent-lane config app-sync status
# Open the App, or reopen it when app_reopen_required is true.
agent-lane doctor --mode app-sync --probe
```
