# Security Policy

## Supported versions

Security fixes are prepared for the latest release candidate or stable V1
release. Pre-1.0 command surfaces are not supported.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use the
repository's **Security** tab to submit a private vulnerability report through
GitHub Security Advisories:

https://github.com/Unitary-orz/agent-lane/security/advisories/new

Include the affected version, impact, reproduction steps, and any suggested
mitigation. Avoid including real credentials, private repository contents, or
sensitive Codex task data.

We aim to acknowledge reports within seven days. Validation and fix timelines
depend on severity and whether the issue lies in agent-lane or an experimental
upstream Codex interface.

## Scope notes

agent-lane starts local processes, manages optional per-user login integration,
creates worktrees, and can inject a managed SSH signing socket. Reports about
path validation, command construction, alias integrity, worktree ownership,
signing isolation, or shared-daemon trust boundaries are in scope.
