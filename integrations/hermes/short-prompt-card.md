# Short prompt card

Use agent-lane to delegate this bounded task to Codex. Keep the parent runtime
unchanged and keep reporting responsibility here.

1. Resolve the real project path and authorization boundary.
2. Before the first lane, run `agent-lane --version` and
   `agent-lane doctor --mode independent --probe`.
3. Use `independent` mode unless live Codex App collaboration is required.
4. Start without a user-managed lane ID using `agent-lane codex run --title
   <task-title> ...`; retain the returned identities as machine state.
5. Observe with bounded `status`, `wait`, or `checkpoint` calls.
6. Continue the selected result with `agent-lane codex send --thread-id
   <selected-thread-id>`; use `steer --thread-id <selected-thread-id>` only for
   one active App Sync
   turn.
7. Validate the result independently, then report evidence and Git state.
8. Do not push, publish, deploy, or clean up without the corresponding scope and
   safety checks.

Useful commands:

```bash
agent-lane codex session list --scope all
agent-lane codex status --thread-id <selected-thread-id>
agent-lane codex closeout --thread-id <selected-thread-id>
agent-lane codex session read --thread-id <selected-thread-id> --include-turns
```

If an exact discovered thread is unbound, read-only checks remain safe. Its
`control.attach_argv` gives the explicit binding step; after attaching, issue
the intended control command. If a control attempt itself returns top-level
`attach_argv` and `after_attach_argv`, run those two complete commands in order.
If target selection is ambiguous, choose from the returned targets and never
guess.

App Sync readiness:

```bash
agent-lane config app-sync status
# If disabled, obtain authorization before enabling it:
agent-lane config app-sync enable
agent-lane config app-sync status
# Open the App, or reopen it when app_reopen_required is true.
agent-lane doctor --mode app-sync --probe
```
